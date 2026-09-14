from fastapi import APIRouter, Request
import os
import sys
import shutil
import logging
from typing import Dict, List, Any

import datetime
import threading
from server.config import APP_VERSION, BASE_DIR, DATA_DIR, SERVICE_NAME
from server.routes.live import _hardware_payload

logger = logging.getLogger("LiveAgent.System")
SERVER_START_TIME = datetime.datetime.now(datetime.timezone.utc).isoformat()

router = APIRouter(prefix="/system", tags=["系统运维"])


@router.get("/version")
async def get_version():
    """后端版本号与诊断元数据 (控制台启动时校验前后端版本一致性与进程归属)"""
    return {
        "code": 0,
        "version": APP_VERSION,
        "service": SERVICE_NAME,
        "api_version": "v1",
        "process_id": os.getpid(),
        "started_at": SERVER_START_TIME,
        "project_root": str(BASE_DIR),
        "data_dir": str(DATA_DIR.resolve()),
    }


@router.post("/shutdown")
async def shutdown_system(request: Request):
    """请求托管 Uvicorn 完成 lifespan 清理后退出；其他入口保留兼容回退。"""
    managed_server = getattr(request.app.state, "uvicorn_server", None)

    def _do_shutdown():
        import time
        time.sleep(0.2)
        if "pytest" in sys.modules or os.getenv("LIVE_AGENT_TESTING") == "1":
            logger.info("测试环境检测生效，跳过实际退出操作。")
            return
        if managed_server is not None:
            logger.info("收到关闭请求，等待 Uvicorn 执行 lifespan 清理，PID: %s", os.getpid())
            managed_server.should_exit = True
            return
        logger.info("当前入口不支持托管退出，发送兼容终止信号，PID: %s", os.getpid())
        import signal
        try:
            os.kill(os.getpid(), signal.SIGTERM)
        except Exception:
            os._exit(0)

    threading.Thread(target=_do_shutdown, daemon=True).start()
    return {"code": 0, "message": f"服务正在关闭 (PID: {os.getpid()})", "process_id": os.getpid()}


@router.get("/hardware")
async def get_system_hardware():
    """获取本地显存、CPU 占用及推荐运行模式 (规划 §12.1 /system/hardware)"""
    return {"code": 0, "data": await _hardware_payload()}


def _scan_running_processes() -> set:
    """获取当前系统运行中的小写进程名集合 (极速非阻塞)"""
    try:
        import psutil
        return {p.name().lower() for p in psutil.process_iter(['name']) if p.info.get('name')}
    except Exception:
        return set()


def _scan_windows_registry_apps() -> Dict[str, Dict[str, str]]:
    """扫描 Windows 注册表已安装软件 (防报错与平滑降级)"""
    if sys.platform != "win32":
        return {}

    found = {}
    try:
        import winreg
        uninstall_keys = [
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"),
            (winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Uninstall"),
        ]
        for root, subkey in uninstall_keys:
            try:
                with winreg.OpenKey(root, subkey) as key:
                    num_subkeys = winreg.QueryInfoKey(key)[0]
                    for i in range(num_subkeys):
                        try:
                            sub_name = winreg.EnumKey(key, i)
                            with winreg.OpenKey(key, sub_name) as item:
                                values = {}
                                num_values = winreg.QueryInfoKey(item)[1]
                                for j in range(num_values):
                                    v_name, v_data, _ = winreg.EnumValue(item, j)
                                    values[v_name] = v_data
                                disp_name = values.get("DisplayName", "")
                                if disp_name and isinstance(disp_name, str):
                                    found[disp_name.lower()] = {
                                        "name": disp_name,
                                        "version": str(values.get("DisplayVersion", "")),
                                        "location": str(values.get("InstallLocation", "")),
                                        "icon": str(values.get("DisplayIcon", "")),
                                        "uninstall": str(values.get("UninstallString", "")),
                                        "publisher": str(values.get("Publisher", "")),
                                    }
                        except Exception:
                            continue
            except Exception:
                continue
    except Exception as e:
        logger.debug(f"注册表扫描跳过: {e}")
    return found


def find_obs_studio() -> Dict[str, Any]:
    """
    全局统一的 OBS Studio 探测函数 (单一事实来源 SSOT)
    跨盘符、多版本、注册表与进程探测，供开播向导与开播前检查共同调用。
    返回:
        {
            "is_running": bool,
            "is_installed": bool,
            "version": str,
            "path": str,
            "status": "running" | "installed" | "missing",
            "badge": str,
            "tip": str,
        }
    """
    procs = _scan_running_processes()
    obs_running = any(p in procs for p in ["obs64.exe", "obs32.exe", "obs.exe"])
    obs_path = shutil.which("obs64") or shutil.which("obs")
    obs_version = ""

    # 1. 注册表扫描辅助探测 (优先提取精确安装路径与版本)
    registry_apps = _scan_windows_registry_apps()
    for app_key, app_info in registry_apps.items():
        if "obs studio" in app_key or "obs-studio" in app_key or app_key.startswith("obs studio"):
            if not obs_version and app_info.get("version"):
                obs_version = app_info["version"]

            # 优先从 DisplayIcon 提取可执行文件路径
            raw_icon = app_info.get("icon", "").strip()
            if raw_icon:
                clean_icon = raw_icon.strip('"').split(",")[0].strip()
                if os.path.exists(clean_icon) and os.path.isfile(clean_icon):
                    if not obs_path:
                        obs_path = clean_icon

            # 其次从 InstallLocation 提取
            loc = app_info.get("location", "").strip().strip('"')
            if loc and not obs_path:
                for candidate in [
                    os.path.join(loc, "bin", "64bit", "obs64.exe"),
                    os.path.join(loc, "bin", "32bit", "obs32.exe"),
                    os.path.join(loc, "obs64.exe"),
                ]:
                    if os.path.exists(candidate):
                        obs_path = candidate
                        break

            # 从 UninstallString 推导安装目录
            uninst = app_info.get("uninstall", "").strip().strip('"')
            if uninst and not obs_path:
                u_dir = os.path.dirname(uninst.split('"')[0].strip())
                for candidate in [
                    os.path.join(u_dir, "bin", "64bit", "obs64.exe"),
                    os.path.join(u_dir, "bin", "32bit", "obs32.exe"),
                    os.path.join(u_dir, "obs64.exe"),
                ]:
                    if os.path.exists(candidate):
                        obs_path = candidate
                        break
            if obs_version or obs_path:
                break

    # 2. 常见全盘驱动器遍历扫描 (覆盖 C/D/E/F 盘的标准与自定义路径)
    if not obs_path:
        candidate_roots = ["C:", "D:", "E:", "F:"]
        candidate_relatives = [
            r"Program Files\obs-studio\bin\64bit\obs64.exe",
            r"Program Files\obs-studio\bin\32bit\obs32.exe",
            r"Program Files (x86)\obs-studio\bin\64bit\obs64.exe",
            r"Program Files (x86)\obs-studio\bin\32bit\obs32.exe",
            r"OBS\obs-studio\bin\64bit\obs64.exe",
            r"OBS\obs-studio\bin\32bit\obs32.exe",
            r"obs-studio\bin\64bit\obs64.exe",
            r"obs-studio\bin\32bit\obs32.exe",
        ]
        for drive in candidate_roots:
            for rel in candidate_relatives:
                p = os.path.join(drive + "\\", rel)
                if os.path.exists(p):
                    obs_path = p
                    break
            if obs_path:
                break

    is_installed = bool(obs_running or obs_path or obs_version)

    if obs_running:
        status = "running"
        badge = "运行中"
        tip = "OBS Studio 进程正在运行；场景、采集源和平台发布状态尚未验证"
    elif is_installed:
        status = "installed"
        badge = "已安装 (未启动)"
        ver_str = f" (v{obs_version})" if obs_version else ""
        tip = f"检测到 OBS Studio 已安装{ver_str}，开播前请启动推流工作台"
    else:
        status = "missing"
        badge = "未安装"
        tip = "未检测到 OBS Studio。推流、图层混音与公域直播强烈推荐安装"

    return {
        "is_running": obs_running,
        "is_installed": is_installed,
        "version": obs_version,
        "path": obs_path or "",
        "status": status,
        "badge": badge,
        "tip": tip,
    }


def find_live_partner() -> Dict[str, Any]:
    """
    全局统一的第三方直播伴侣探测函数 (SSOT 单一事实来源)
    覆盖：抖音直播伴侣 (ByteDance / webcast_mate 中英文版及自定义路径)、快手直播伴侣、微信视频号直播工具、B站直播姬、淘宝主播工作台等。
    返回:
        {
            "is_running": bool,
            "is_installed": bool,
            "running_names": List[str],
            "installed_names": List[str],
            "path": str,
            "version": str,
            "status": "running" | "installed" | "missing",
            "badge": str,
            "tip": str,
        }
    """
    procs = _scan_running_processes()
    registry_apps = _scan_windows_registry_apps()

    running_names: List[str] = []
    installed_names: List[str] = []
    best_path: str = ""
    best_version: str = ""

    # 1. 进程扫描 (覆盖中英文及变体进程名，严禁匹配普通微信小程序 wechatappex.exe)
    partner_proc_map = {
        # 抖音直播伴侣
        "直播伴侣.exe": "抖音直播伴侣",
        "直播伴侣 launcher.exe": "抖音直播伴侣",
        "直播伴侣launcher.exe": "抖音直播伴侣",
        "livepartner.exe": "抖音直播伴侣",
        "douyinlivepartner.exe": "抖音直播伴侣",
        "douyin_live_partner.exe": "抖音直播伴侣",
        "webcast_mate.exe": "抖音直播伴侣",
        "webcastmate.exe": "抖音直播伴侣",
        # 快手直播伴侣
        "快手直播伴侣.exe": "快手直播伴侣",
        "kuaishou.exe": "快手直播伴侣",
        "kwai.exe": "快手直播伴侣",
        "kslivepartner.exe": "快手直播伴侣",
        # 微信视频号直播工具 (官方独立推流客户端)
        "wechatlive.exe": "微信视频号直播工具",
        "channelslive.exe": "微信视频号直播工具",
        "wxlive.exe": "微信视频号直播工具",
        # B站直播姬
        "bililive.exe": "Bilibili 直播姬",
        "livehime.exe": "Bilibili 直播姬",
        # 淘宝主播工作台
        "taobaolive.exe": "淘宝主播工作台",
        "tblive.exe": "淘宝主播工作台",
    }
    for proc_name, label in partner_proc_map.items():
        if proc_name in procs and label not in running_names:
            running_names.append(label)

    # 2. 注册表深度识别 (识别 ByteDance 直播伴侣、快手、视频号等)
    for app_key, app_info in registry_apps.items():
        name = app_info.get("name", "")
        pub = app_info.get("publisher", "").lower()
        loc = app_info.get("location", "").lower()
        icon = app_info.get("icon", "").lower()
        uninst = app_info.get("uninstall", "").lower()
        ver = app_info.get("version", "")

        # 判定 A: 抖音直播伴侣 (官方发布者 ByteDance / 路径 webcast_mate / 名称包含 直播伴侣+ByteDance)
        is_douyin = (
            ("bytedance" in pub or "字节跳动" in pub or "webcast" in loc or "webcast" in icon or "webcast" in uninst)
            and ("直播伴侣" in name or "webcast" in loc or "livepartner" in loc)
        ) or "抖音直播伴侣" in name
        if is_douyin:
            disp_title = f"抖音直播伴侣{f' (v{ver})' if ver else ''}"
            if disp_title not in installed_names:
                installed_names.append(disp_title)
            if not best_version and ver:
                best_version = ver
            if not best_path:
                raw_icon = app_info.get("icon", "").strip().strip('"').split(",")[0].strip()
                if raw_icon and os.path.exists(raw_icon):
                    best_path = raw_icon
                elif app_info.get("location") and os.path.exists(app_info["location"]):
                    best_path = app_info["location"]
            continue

        # 判定 B: 快手直播伴侣
        is_kuaishou = "快手直播伴侣" in name or ("kuaishou" in pub and "直播" in name)
        if is_kuaishou:
            disp_title = f"快手直播伴侣{f' (v{ver})' if ver else ''}"
            if disp_title not in installed_names:
                installed_names.append(disp_title)
            continue

        # 判定 C: B站直播姬
        if "直播姬" in name or ("bilibili" in pub and "直播" in name):
            disp_title = f"Bilibili 直播姬{f' (v{ver})' if ver else ''}"
            if disp_title not in installed_names:
                installed_names.append(disp_title)
            continue

        # 判定 D: 淘宝主播工作台
        if "淘宝主播" in name:
            disp_title = f"淘宝主播工作台{f' (v{ver})' if ver else ''}"
            if disp_title not in installed_names:
                installed_names.append(disp_title)
            continue

        # 判定 E: 微信视频号直播
        if "视频号直播" in name or ("tencent" in pub and "视频号直播" in name):
            disp_title = f"微信视频号直播工具{f' (v{ver})' if ver else ''}"
            if disp_title not in installed_names:
                installed_names.append(disp_title)
            continue

        # 判定 F: 通用 "直播伴侣" 兜底
        if "直播伴侣" in name:
            disp_title = f"{name}"
            if disp_title not in installed_names:
                installed_names.append(disp_title)

    # 3. 常见全盘驱动器与默认目录扫描 (覆盖用户自定义目录如 D:\DouyinZhibo\webcast_mate)
    candidate_dirs = [
        # 用户已知自定义及常见目录
        r"D:\DouyinZhibo\webcast_mate",
        r"C:\DouyinZhibo\webcast_mate",
        r"E:\DouyinZhibo\webcast_mate",
        r"C:\webcast_mate",
        r"D:\webcast_mate",
        r"E:\webcast_mate",
        r"C:\Program Files\webcast_mate",
        r"D:\Program Files\webcast_mate",
        r"C:\Program Files (x86)\webcast_mate",
        r"D:\Program Files (x86)\webcast_mate",
    ]
    appdata = os.environ.get("LOCALAPPDATA", "")
    if appdata:
        candidate_dirs.extend([
            os.path.join(appdata, "DouyinLivePartner"),
            os.path.join(appdata, "webcast_mate"),
            os.path.join(appdata, "Programs", "webcast_mate"),
        ])
    for c_dir in candidate_dirs:
        if os.path.exists(c_dir):
            has_douyin = any("抖音直播伴侣" in x for x in installed_names)
            if not has_douyin:
                installed_names.append("抖音直播伴侣")
            if not best_path:
                best_path = c_dir

    is_running = len(running_names) > 0
    is_installed = is_running or (len(installed_names) > 0)

    if is_running:
        status = "running"
        badge = "运行中"
        tip = f"检测到正在运行: {', '.join(running_names)}。在伴侣中添加【摄像头】并选择【OBS Virtual Camera】即可拉取数字人画面！"
    elif is_installed:
        status = "installed"
        badge = "已安装"
        display_names = installed_names[:2]
        tip = f"检测到已安装: {', '.join(display_names)}。商业开播时启动伴侣并接入虚拟摄像头即可推流"
    else:
        status = "missing"
        badge = "未检测到"
        tip = "未检测到常用直播伴侣（抖音/快手/视频号/B站）。正式商业推流需启动对应平台客户端并接入数字人画面"

    return {
        "is_running": is_running,
        "is_installed": is_installed,
        "running_names": running_names,
        "installed_names": installed_names,
        "path": best_path,
        "version": best_version,
        "status": status,
        "badge": badge,
        "tip": tip,
    }


def _detect_prerequisites() -> Dict[str, Any]:
    """
    全量探测直播必备及配套软件生态状态 (用于本地调试与商业演播室开播诊断)
    覆盖: OBS Studio, 虚拟摄像头驱动, pyvirtualcam库, 平台直播伴侣, 多媒体核心
    """
    procs = _scan_running_processes()
    registry_apps = _scan_windows_registry_apps()

    items: List[Dict[str, Any]] = []

    # 1. OBS Studio 检测 (调用全局统一 SSOT 探测函数)
    obs_info = find_obs_studio()
    obs_status = obs_info["status"]
    obs_badge = obs_info["badge"]
    obs_tip = obs_info["tip"]
    obs_version = obs_info["version"]
    obs_path = obs_info["path"]

    items.append({
        "key": "obs",
        "name": "OBS Studio",
        "category": "推流主控",
        "required": True,
        "status": obs_status,
        "badge": obs_badge,
        "version": obs_version,
        "path": obs_path or "",
        "desc": "专业级流媒体音视频混合编排工作台，商业直播必选底座",
        "tip": obs_tip,
        "url": "https://obsproject.com/" if obs_status not in ("installed", "running") else "",
        "action_text": "前往 OBS 官网下载" if obs_status not in ("installed", "running") else "",
        "action_type": "url" if obs_status not in ("installed", "running") else ""
    })

    # 2. OBS Virtual Camera / DirectShow 虚拟摄像头驱动检测
    vcam_registered = False
    vcam_desc = ""
    # 检查 Windows DirectShow 或 OBS 虚拟摄像头驱动模块
    common_vcam_dlls = [
        r"C:\Program Files\obs-studio\data\obs-plugins\win-dshow\virtualcam-install.bat",
        r"C:\Program Files\obs-studio\obs-plugins\64bit\obs-virtualcam.dll",
        r"C:\Windows\System32\obs-virtualcam-module64.dll",
        r"C:\Windows\SysWOW64\obs-virtualcam-module32.dll",
    ]
    if obs_path:
        # 动态将实际安装目录下的驱动安装批处理加入检查
        obs_base_dir = os.path.abspath(os.path.join(os.path.dirname(obs_path), "..", ".."))
        dyn_bat = os.path.join(obs_base_dir, "data", "obs-plugins", "win-dshow", "virtualcam-install.bat")
        if dyn_bat not in common_vcam_dlls:
            common_vcam_dlls.insert(0, dyn_bat)
    for dll in common_vcam_dlls:
        if os.path.exists(dll):
            vcam_registered = True
            vcam_desc = "OBS 虚拟摄像头驱动组件已就绪"
            break

    # 注册表 DirectShow 检查
    if not vcam_registered and sys.platform == "win32":
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, r"CLSID\{860BB310-5D01-11d0-BD3B-00A0C911CE86}\Instance") as key:
                num = winreg.QueryInfoKey(key)[0]
                for i in range(num):
                    sub = winreg.EnumKey(key, i)
                    with winreg.OpenKey(key, sub) as sk:
                        try:
                            fname = str(winreg.QueryValueEx(sk, "FriendlyName")[0])
                            if "obs virtual" in fname.lower() or "virtual camera" in fname.lower():
                                vcam_registered = True
                                vcam_desc = f"DirectShow 捕获设备可用: {fname}"
                                break
                        except Exception:
                            pass
        except Exception:
            pass

    if vcam_registered:
        vcam_status = "installed"
        vcam_badge = "驱动就绪"
        vcam_tip = vcam_desc or "虚拟摄像头系统级 DirectShow 驱动正常挂载"
    else:
        vcam_status = "missing"
        vcam_badge = "未激活/未检测到"
        vcam_tip = "未检测到虚拟摄像头驱动。在 OBS 界面中点击一次【启动虚拟摄像机】即可自动注册激活驱动。"

    items.append({
        "key": "vcam_driver",
        "name": "虚拟摄像头驱动 (OBS Virtual Cam)",
        "category": "画面中继",
        "required": True,
        "status": vcam_status,
        "badge": vcam_badge,
        "version": "",
        "path": "",
        "desc": "将 AI 数字人生成的 25fps 视频帧直接呈现为系统免驱摄像头，供直播伴侣抓取",
        "tip": vcam_tip,
        "url": "https://obsproject.com/wiki/OBS-Virtual-Camera" if vcam_status not in ("installed", "running") else "",
        "action_text": "查看驱动激活指引" if vcam_status not in ("installed", "running") else "",
        "action_type": "tip" if vcam_status not in ("installed", "running") else ""
    })

    # 3. Python 虚拟摄像头管道 pyvirtualcam
    pyvcam_installed = False
    pyvcam_version = ""
    try:
        import pyvirtualcam
        pyvcam_installed = True
        pyvcam_version = getattr(pyvirtualcam, "__version__", "已安装")
    except ImportError:
        pyvcam_installed = False

    if pyvcam_installed:
        pyvcam_status = "installed"
        pyvcam_badge = "已就绪"
        pyvcam_tip = f"Python pyvirtualcam ({pyvcam_version}) 本地管道支持正常"
    else:
        pyvcam_status = "missing"
        pyvcam_badge = "未安装"
        pyvcam_tip = "未安装 pyvirtualcam 库，数字人将以 Web MJPEG 模式降级运行。建议执行 pip 安装"

    items.append({
        "key": "pyvirtualcam",
        "name": "pyvirtualcam 核心驱动包",
        "category": "Python管道",
        "required": False,
        "status": pyvcam_status,
        "badge": pyvcam_badge,
        "version": pyvcam_version,
        "path": "",
        "desc": "数字人引擎与虚拟摄像头硬件设备的极速帧缓冲通讯中继",
        "tip": pyvcam_tip,
        "command": "pip install pyvirtualcam" if pyvcam_status not in ("installed", "running") else "",
        "action_text": "复制安装命令" if pyvcam_status not in ("installed", "running") else "",
        "action_type": "copy" if pyvcam_status not in ("installed", "running") else ""
    })

    # 4. 主流第三方直播伴侣客户端 (调用全局统一 SSOT 探测函数)
    partner_info = find_live_partner()
    partner_status = partner_info["status"]
    partner_badge = partner_info["badge"]
    partner_tip = partner_info["tip"]
    partner_path = partner_info["path"]
    partner_version = partner_info["version"]

    items.append({
        "key": "live_partner",
        "name": "第三方公域直播伴侣",
        "category": "平台推流",
        "required": False,
        "status": partner_status,
        "badge": partner_badge,
        "version": partner_version,
        "path": partner_path,
        "desc": "抖音直播伴侣 / 快手直播伴侣 / 微信视频号直播工具，负责公域平台推流与观众弹幕互动",
        "tip": partner_tip,
        "url": "https://streamingtool.douyin.com/" if partner_status not in ("installed", "running") else "",
        "action_text": "抖音直播伴侣下载" if partner_status not in ("installed", "running") else "",
        "action_type": "url" if partner_status not in ("installed", "running") else ""
    })

    # 5. 本地多媒体编解码与模型底座 (FFmpeg, OpenCV, ONNXRuntime)
    ffmpeg_ok = bool(shutil.which("ffmpeg"))
    cv2_ok = False
    try:
        import cv2
        cv2_ok = True
    except ImportError:
        pass
    ort_ok = False
    try:
        import onnxruntime
        ort_ok = True
    except ImportError:
        pass

    media_ready = ffmpeg_ok and cv2_ok
    if media_ready and ort_ok:
        media_status = "installed"
        media_badge = "全部就绪"
        media_tip = "FFmpeg + OpenCV + ONNXRuntime 环境健全，支持 25fps 数字人视频实时合成与混合 RAG"
    elif media_ready:
        media_status = "installed"
        media_badge = "基础就绪"
        media_tip = "FFmpeg + OpenCV 已安装，可用于本地媒体处理；未验证外部发布"
    else:
        media_status = "missing"
        media_badge = "部分缺失"
        missing_parts = []
        if not ffmpeg_ok:
            missing_parts.append("FFmpeg")
        if not cv2_ok:
            missing_parts.append("OpenCV")
        media_tip = f"缺少关键音视频组件: {', '.join(missing_parts)}，可能影响本地视频推流或录制"

    items.append({
        "key": "media_core",
        "name": "多媒体与AI引擎核心 (FFmpeg / CV)",
        "category": "音画引擎",
        "required": True,
        "status": media_status,
        "badge": media_badge,
        "version": "",
        "path": shutil.which("ffmpeg") or "",
        "desc": "负责音视频实时转码、数字人 25fps 视频帧缓冲合成与向量检索底层运算",
        "tip": media_tip,
        "command": "pip install opencv-python onnxruntime" if media_status not in ("installed", "running") else "",
        "action_text": "复制依赖安装命令" if media_status not in ("installed", "running") else "",
        "action_type": "copy" if media_status not in ("installed", "running") else ""
    })

    # 6. Python 运行时与核心基础
    py_ver = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    is_64bit = sys.maxsize > 2**32
    if is_64bit and (3, 10) <= sys.version_info[:2] <= (3, 13):
        py_status = "installed"
        py_badge = f"Python {py_ver} (64位)"
        py_tip = f"Python 64位运行时已就绪 ({sys.executable})"
    else:
        py_status = "missing"
        py_badge = f"Python {py_ver} ({'32位' if not is_64bit else '版本不适'})"
        py_tip = "推荐使用 64 位 Python 3.12 或 3.13"

    items.append({
        "key": "python_runtime",
        "name": "Python 64位运行时",
        "category": "基础环境",
        "required": True,
        "status": py_status,
        "badge": py_badge,
        "version": py_ver,
        "path": sys.executable,
        "desc": "支撑 AI LiveStream Agent 调度大脑、音画渲染与业务状态流转的宿主执行环境",
        "tip": py_tip,
        "url": "https://www.python.org/downloads/" if py_status not in ("installed", "running") else "",
        "action_text": "前往 Python 官网下载" if py_status not in ("installed", "running") else "",
        "action_type": "url" if py_status not in ("installed", "running") else ""
    })

    # 7. 声卡播放与虚拟音频 (VB-Cable)
    sd_installed = False
    has_cable = False
    audio_count = 0
    try:
        import sounddevice as sd
        # 强制重置底层 PortAudio 驱动缓存，确保在运行时安装的虚拟声卡能立即被识别
        if hasattr(sd, "_terminate") and hasattr(sd, "_initialize"):
            try:
                sd._terminate()
                sd._initialize()
            except Exception:
                pass
        sd_installed = True
        devs = sd.query_devices()
        out_devs = [d for d in devs if d.get("max_output_channels", 0) > 0]
        audio_count = len(out_devs)
        has_cable = any("cable" in str(d.get("name", "")).lower() or "virtual" in str(d.get("name", "")).lower() for d in out_devs)
    except Exception:
        pass

    # Windows 平台双重保险：若 sounddevice 未捕获，进一步检查注册表已安装软件
    if not has_cable and sys.platform == "win32":
        try:
            for app_k in registry_apps.keys():
                if "vbcable" in app_k or "vb-audio" in app_k or "virtual audio cable" in app_k:
                    has_cable = True
                    break
        except Exception:
            pass

    if sd_installed and has_cable:
        audio_status = "installed"
        audio_badge = "VB-Cable 已挂载"
        audio_tip = f"检测到 {audio_count} 个播放设备，包含推荐虚拟声卡 VB-Cable，直播伴侣可无损采集纯净 AI 声音"
    elif sd_installed and audio_count > 0:
        audio_status = "installed"
        audio_badge = f"{audio_count} 个音频设备"
        audio_tip = f"检测到 {audio_count} 个物理音频设备，但未安装 VB-Cable 虚拟声卡；开播存在微信提示音与系统杂音串录风险，商用直播强烈建议安装"
    else:
        audio_status = "missing"
        audio_badge = "未就绪"
        audio_tip = "未检测到可用的系统音频输出设备或 sounddevice 驱动包"

    items.append({
        "key": "audio_devices",
        "name": "声卡输出与虚拟音频 (VB-Cable)",
        "category": "音频中继",
        "required": False,
        "has_cable": has_cable,
        "status": audio_status,
        "badge": audio_badge,
        "version": "",
        "path": "",
        "desc": "将 TTS 合成的语音实时路由至系统扬声器或虚拟声卡，供直播伴侣抓取纯净音源",
        "tip": audio_tip,
        "url": "https://vb-audio.com/Cable/" if not has_cable else "",
        "action_text": "下载 VB-Cable 虚拟声卡" if not has_cable else "",
        "action_type": "url" if not has_cable else ""
    })


    # 计算整体统计与评估状态
    ready_count = sum(1 for item in items if item["status"] in ["running", "installed"])
    missing_count = sum(1 for item in items if item["status"] == "missing")
    critical_missing = sum(1 for item in items if item["required"] and item["status"] == "missing")

    if critical_missing == 0:
        overall_level = "success"
        overall_text = "核心本机媒体组件已检测；外部平台发布仍需人工配置和验收"
    elif critical_missing == 1:
        overall_level = "warning"
        overall_text = "发现 1 项核心本机媒体组件未就绪，可按指引配置本地音画环境"
    else:
        overall_level = "alert"
        overall_text = f"发现 {critical_missing} 项核心本机媒体组件未就绪，请先完成本地环境配置"

    return {
        "summary": {
            "total": len(items),
            "ready_count": ready_count,
            "missing_count": missing_count,
            "critical_missing": critical_missing,
            "overall_level": overall_level,
            "overall_text": overall_text
        },
        "items": items
    }


@router.get("/prerequisites")
async def get_system_prerequisites():
    """
    直播必备软件生态检测端点 (开播向导必备项)
    检测 OBS Studio、虚拟摄像头驱动、pyvirtualcam、平台直播伴侣及音画底座
    """
    try:
        data = _detect_prerequisites()
        return {"code": 0, "data": data}
    except Exception as e:
        logger.error(f"必备软件检测失败: {e}", exc_info=True)
        return {"code": 500, "message": f"探测失败: {str(e)}", "data": None}

