#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AI-LiveStream-Agent 跨平台商用级智能启动器 (Launcher)
专为商用客户与现场开播设计：
1. 全景环境体检：操作系统、硬件配置 (CPU/内存/GPU显存)、Python 运行时 (版本/架构/路径)
2. 音画生态设备感知：虚拟摄像头 (OBS Virtual Camera)、声卡/虚拟音频 (VB-Cable)
3. 分级依赖深度体检：核心基础库 + 25fps 音视频多媒体库
4. 交互式智能自愈：依赖缺失时支持一键自动从国内高速镜像源补齐，免除人工配置痛点
5. 端口深度探测与历史旧版本进程树安全回收 (防止多实例与端口冲突)
6. 启动状态轮询与自愈式呼起浏览器中控台
"""

import os
import sys
import time
import json
import shutil
import argparse
import platform
import threading
import webbrowser
import urllib.request
import urllib.error
import socket
import subprocess
from pathlib import Path

# 适配终端编码，防止 Windows 控制台输出 emoji 或中文报错
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
if hasattr(sys.stderr, "reconfigure"):
    try:
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

# 锁定项目根目录
PROJECT_ROOT = Path(__file__).resolve().parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from server.config import SERVER_HOST, SERVER_PORT, APP_VERSION, SERVICE_NAME

# 国内高速镜像源推荐
DEFAULT_PIP_INDEX = "https://pypi.tuna.tsinghua.edu.cn/simple"

# 依赖需求定义 (库导入名: pip安装包名)
CORE_DEPENDENCIES = {
    "fastapi": "fastapi",
    "uvicorn": "uvicorn",
    "aiosqlite": "aiosqlite",
    "sqlalchemy": "sqlalchemy",
    "cryptography": "cryptography",
    "psutil": "psutil",
    "pydantic": "pydantic",
    "httpx": "httpx",
    "websockets": "websockets",
    "ahocorasick": "pyahocorasick",
}

MEDIA_DEPENDENCIES = {
    "numpy": "numpy",
    "cv2": "opencv-python",
    "pyvirtualcam": "pyvirtualcam",
    "sounddevice": "sounddevice",
    "edge_tts": "edge-tts",
    # 轻量 ASR 语音转写引擎 (默认推荐)：无 torch 全家桶负担，CPU 友好
    "faster_whisper": "faster-whisper",
}


def print_banner():
    print("=" * 76)
    print(f"       AI-LiveStream-Agent 智能直播中控系统 (商业生产版: v{APP_VERSION})")
    print("=" * 76)
    print(f"   [+] 中控控制大屏: http://{SERVER_HOST}:{SERVER_PORT}/console")
    print(f"   [+] 交互接口文档: http://{SERVER_HOST}:{SERVER_PORT}/docs")
    print(f"   [+] WebSocket 信令: ws://{SERVER_HOST}:{SERVER_PORT}/ws/live_control")
    print("=" * 76)


def probe_hardware_summary() -> dict:
    """探测商用客户本机硬件环境：CPU、内存、GPU、操作系统"""
    info = {
        "os": f"{platform.system()} {platform.release()} ({platform.architecture()[0]})",
        "cpu": f"{os.cpu_count() or 4} 核心",
        "memory": "未知",
        "gpu": "未检测到独立显卡 (使用 CPU 软解)",
        "vram_gb": 0.0,
    }
    # 内存探测
    try:
        import psutil
        vm = psutil.virtual_memory()
        info["memory"] = f"{vm.total / (1024 ** 3):.1f} GB (可用: {vm.available / (1024 ** 3):.1f} GB)"
    except Exception:
        pass

    # GPU 探测：1. 尝试多路径 nvidia-smi；2. 失败时回退 WMI/CIM 探测物理显卡
    gpu_detected = False
    nvidia_candidates = [
        "nvidia-smi",
        r"C:\Windows\System32\nvidia-smi.exe",
        r"C:\Program Files\NVIDIA Corporation\NVSMI\nvidia-smi.exe",
        r"C:\Program Files\NVIDIA Corporation\Driver\nvidia-smi.exe",
    ]
    for cand in nvidia_candidates:
        if cand != "nvidia-smi" and not Path(cand).exists():
            continue
        try:
            cmd = [cand, "--query-gpu=name,memory.total,driver_version", "--format=csv,noheader,nounits"]
            res = subprocess.run(cmd, capture_output=True, text=True, errors="replace", timeout=3)
            if res.returncode == 0 and res.stdout.strip():
                lines = res.stdout.strip().splitlines()
                if lines:
                    parts = [p.strip() for p in lines[0].split(",")]
                    gpu_name = parts[0]
                    vram_mb = float(parts[1]) if len(parts) > 1 else 0
                    driver = parts[2] if len(parts) > 2 else ""
                    info["gpu"] = f"{gpu_name} (驱动: {driver})"
                    info["vram_gb"] = round(vram_mb / 1024.0, 1)
                    gpu_detected = True
                    break
        except Exception:
            pass

    # WMI 兜底探测 (支持老旧入门显卡如 GT 710、AMD Radeon 或 Intel 独显)
    if not gpu_detected and platform.system() == "Windows":
        try:
            ps_cmd = "Get-CimInstance Win32_VideoController | Select-Object Name, AdapterRAM, DriverVersion | ConvertTo-Json"
            out = subprocess.run(
                ["powershell", "-NoProfile", "-Command", ps_cmd],
                capture_output=True, text=True, timeout=8
            )
            if out.returncode == 0 and out.stdout.strip():
                raw_data = json.loads(out.stdout)
                items = raw_data if isinstance(raw_data, list) else [raw_data]
                ignore_keywords = {"oray", "virtual", "basic display", "idddriver", "remote", "microsoft 基本显示"}
                valid_gpus = []
                for item in items:
                    name = (item.get("Name") or "").strip()
                    if not name or any(k in name.lower() for k in ignore_keywords):
                        continue
                    ram = item.get("AdapterRAM") or 0
                    driver = item.get("DriverVersion") or ""
                    valid_gpus.append((ram, name, driver))

                if valid_gpus:
                    valid_gpus.sort(key=lambda x: x[0], reverse=True)
                    best_ram, best_name, best_driver = valid_gpus[0]
                    driver_suffix = f" (驱动: {best_driver})" if best_driver else ""
                    info["gpu"] = f"{best_name}{driver_suffix}"
                    if 0 < best_ram <= 4 * (1024 ** 3):
                        info["vram_gb"] = round(best_ram / (1024 ** 3), 1)
                    elif best_ram > 4 * (1024 ** 3):
                        info["vram_gb"] = round(best_ram / (1024 ** 3), 1)
                    gpu_detected = True
        except Exception:
            pass

    return info



def check_audio_devices() -> dict:
    """探测系统声卡与虚拟音频线缆 (VB-Cable)"""
    res = {"available": False, "count": 0, "has_cable": False, "default_out": "未知"}
    try:
        import sounddevice as sd
        devs = sd.query_devices()
        out_devs = [d for d in devs if d.get("max_output_channels", 0) > 0]
        res["available"] = True
        res["count"] = len(out_devs)
        for d in out_devs:
            name = str(d.get("name", "")).lower()
            if "cable" in name or "virtual" in name:
                res["has_cable"] = True
        try:
            def_idx = sd.default.device[1] if isinstance(sd.default.device, (list, tuple)) else sd.default.device
            if def_idx is not None and def_idx < len(devs):
                res["default_out"] = devs[def_idx].get("name", "系统默认")
        except Exception:
            pass
    except Exception:
        pass
    return res


def check_virtual_cam_driver() -> bool:
    """检查系统是否注册了 OBS Virtual Camera 或 DirectShow 虚拟摄像头驱动"""
    if sys.platform != "win32":
        return False
    # 常用驱动 DLL 路径
    dlls = [
        r"C:\Program Files\obs-studio\data\obs-plugins\win-dshow\virtualcam-install.bat",
        r"C:\Program Files\obs-studio\obs-plugins\64bit\obs-virtualcam.dll",
        r"C:\Windows\System32\obs-virtualcam-module64.dll",
    ]
    for d in dlls:
        if os.path.exists(d):
            return True
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, r"CLSID\{860BB310-5D01-11d0-BD3B-00A0C911CE86}\Instance") as key:
            num = winreg.QueryInfoKey(key)[0]
            for i in range(num):
                sub = winreg.EnumKey(key, i)
                with winreg.OpenKey(key, sub) as sk:
                    fname = str(winreg.QueryValueEx(sk, "FriendlyName")[0])
                    if "obs virtual" in fname.lower() or "virtual camera" in fname.lower():
                        return True
    except Exception:
        pass
    return False


def run_commercial_preflight(auto_install: bool = False) -> bool:
    """
    商用客户现场全面环境体检与自愈：
    1. 硬件架构与系统
    2. Python 运行时版本与位数 (3.10 ~ 3.13 64位)
    3. 核心依赖检查与自愈
    4. 音视频与数字人媒体依赖检查与自愈
    5. 虚拟音视频设备探测
    """
    print("\n" + "=" * 76)
    print(" [商用体检] 正在对当前主机执行开播前全景运行环境检测...")
    print("=" * 76)

    hw = probe_hardware_summary()
    print(f" [*] 操作系统平台: {hw['os']}")
    print(f" [*] 处理器与内存: {hw['cpu']} | 总物理内存: {hw['memory']}")
    if hw["vram_gb"] > 0:
        print(f" [*] 独立图形显卡: {hw['gpu']} (可用显存: {hw['vram_gb']} GB) [OK: 支持本地硬件渲染加速]")
    else:
        print(f" [*] 独立图形显卡: {hw['gpu']} [提示: 建议使用轻量直播模式或外接云端服务]")

    # 1. Python 运行时检测
    py_ver = sys.version_info
    py_str = f"{py_ver.major}.{py_ver.minor}.{py_ver.micro}"
    is_64bit = sys.maxsize > 2**32
    print(f" [*] Python 运行时: {sys.executable} (v{py_str}, {'64位' if is_64bit else '32位'})")

    if not is_64bit:
        print(" [!] 致命错误: 当前 Python 为 32 位版本，内存上限严重受限，无法进行音视频与 AI 处理！")
        print("     请安装 64 位 Python 3.12 或 3.13: https://www.python.org/downloads/")
        return False

    if not (3, 10) <= py_ver[:2] <= (3, 13):
        print(f" [!] 警告: 当前 Python 版本 (v{py_str}) 可能存在兼容性风险，推荐使用 Python 3.12 或 3.13。")

    # 2. 核心依赖检查
    missing_core = []
    for mod_name, pip_name in CORE_DEPENDENCIES.items():
        try:
            __import__(mod_name)
        except ImportError:
            missing_core.append((mod_name, pip_name))

    # 3. 音视频多媒体依赖检查
    missing_media = []
    for mod_name, pip_name in MEDIA_DEPENDENCIES.items():
        try:
            __import__(mod_name)
        except ImportError:
            missing_media.append((mod_name, pip_name))

    # 4. 如果发现缺失依赖，尝试触发自愈或提示安装
    all_missing = missing_core + missing_media
    if all_missing:
        print("\n [!] 检测到当前主机缺少以下必要组件库:")
        for _, pkg in all_missing:
            print(f"     - {pkg}")

        do_install = auto_install
        if not do_install and sys.stdin.isatty():
            try:
                choice = input("\n [商用自愈] 是否立即自动从国内镜像源安装缺失组件？[Y/n]: ").strip().lower()
                if choice in ("", "y", "yes"):
                    do_install = True
            except Exception:
                pass

        if do_install:
            print(f"\n [*] 正在通过国内镜像 ({DEFAULT_PIP_INDEX}) 自动安装缺失依赖，请稍候...")
            packages_to_install = [pkg for _, pkg in all_missing]
            cmd = [sys.executable, "-m", "pip", "install", "-i", DEFAULT_PIP_INDEX] + packages_to_install
            try:
                ret = subprocess.run(cmd, text=True, errors="replace")
                if ret.returncode == 0:
                    print(" [OK] 缺失依赖自动安装成功！")
                    # 再次校验
                    missing_after = []
                    for mod_name, _ in all_missing:
                        try:
                            __import__(mod_name)
                        except ImportError:
                            missing_after.append(mod_name)
                    if missing_after:
                        print(f" [!] 仍有组件无法导入: {', '.join(missing_after)}")
                        return False
                else:
                    print(" [!] 自动安装依赖失败，请手动在终端运行: pip install -r server/requirements.txt")
                    return False
            except Exception as e:
                print(f" [!] 自动安装过程异常: {e}")
                return False
        else:
            if missing_core:
                print(" [!] 核心组件缺失，系统无法继续启动！请执行: pip install -r server/requirements.txt")
                return False
            else:
                print(" [!] 媒体组件缺失，系统将以最低兼容离线模式运行 (部分音画功能将受限)。")
    else:
        print(" [OK] 核心业务组件与音视频媒体库均已全部就绪")

    # 5. 音频与虚拟设备感知
    audio = check_audio_devices()
    if audio["available"]:
        cable_tip = " (已检测到 VB-Cable 虚拟声卡 ★)" if audio["has_cable"] else " (未安装 VB-Cable，使用默认扬声器)"
        print(f" [*] 音频输出设备: 找到 {audio['count']} 个可用播放设备{cable_tip}")
    else:
        print(" [!] 音频组件状态: 未能枚举音频设备 (已启用离线软降级)")

    has_vcam = check_virtual_cam_driver()
    if has_vcam:
        print(" [*] 虚拟摄像头驱动: 系统已注册 OBS Virtual Camera 驱动 [OK: 直播伴侣可直连画面]")
    else:
        print(" [?] 虚拟摄像头驱动: 未检测到系统级驱动 (打开 OBS 点击一次'启动虚拟摄像机'即可自动激活)")

    # 6. OBS Studio 与第三方直播伴侣生态探测
    try:
        from server.routes.system import find_obs_studio, find_live_partner
        obs_res = find_obs_studio()
        if obs_res["is_installed"]:
            ver_str = f" (v{obs_res['version']})" if obs_res.get("version") else ""
            print(f" [*] 直播推流工作台: OBS Studio 已安装就绪{ver_str} [OK]")
        else:
            print(" [*] 直播推流工作台: 未在系统中检测到 OBS Studio (正式公域直播推荐安装)")

        partner_res = find_live_partner()
        if partner_res["is_installed"]:
            pnames = ", ".join(partner_res.get("installed_names", [])) or "主流直播伴侣"
            print(f" [*] 平台直播伴侣: {pnames} 已就绪 [OK]")
        else:
            print(" [*] 平台直播伴侣: 未在系统中检测到常用直播伴侣 (可选，商业直播推荐)")
    except Exception:
        pass

    print("=" * 76)
    print(" [体检总结] 主机环境已完成全景体检，满足商用直播中控系统启动标准！\n")
    return True


def is_tcp_port_open(host: str, port: int, timeout: float = 0.6) -> bool:
    """快速检测 TCP 端口是否处于监听状态"""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except (socket.timeout, ConnectionRefusedError, OSError):
        return False


def query_running_version(host: str, port: int, timeout: float = 1.0) -> dict:
    """向目标端口查询服务健康与版本元数据"""
    url = f"http://{host}:{port}/api/v1/system/version"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "AI-LiveStream-Agent-Launcher"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if resp.status == 200:
                data = json.loads(resp.read().decode("utf-8"))
                return data
    except Exception:
        pass
    return {}


def query_readiness(host: str, port: int, timeout: float = 1.0) -> bool:
    """探测数据库和启动阶段均已就绪，而非仅确认版本端点可达。"""
    try:
        with urllib.request.urlopen(f"http://{host}:{port}/readyz", timeout=timeout) as resp:
            return resp.status == 200
    except Exception:
        return False


def find_pids_on_port(port: int) -> list:
    """查找占用指定端口的所有进程 PID"""
    pids = set()
    try:
        import psutil
        for proc in psutil.process_iter(['pid', 'name']):
            try:
                # 优先使用 psutil 6.0+ 推荐的 net_connections，兼容旧版 connections
                conn_getter = getattr(proc, "net_connections", getattr(proc, "connections", None))
                if conn_getter:
                    for conn in conn_getter(kind='inet'):
                        if conn.laddr and conn.laddr.port == port:
                            pids.add(proc.pid)
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                continue
    except Exception:
        pass

    # Windows netstat 辅助补充
    if sys.platform == "win32":
        try:
            cmd = f'netstat -ano | findstr :{port}'
            out = subprocess.check_output(cmd, shell=True, text=True, errors="replace", stderr=subprocess.DEVNULL)
            for line in out.strip().splitlines():
                parts = line.split()
                if len(parts) >= 5 and "LISTENING" in parts:
                    pid = parts[-1]
                    if pid.isdigit():
                        pids.add(int(pid))
        except Exception:
            pass

    current_pid = os.getpid()
    if current_pid in pids:
        pids.remove(current_pid)
    return list(pids)


def terminate_process_tree(pid: int, timeout: float = 3.0):
    """彻底终止进程及其全部子进程树 (清理 uvicorn reload 衍生的子进程)"""
    print(f"[*] 正在彻底安全回收进程树 (PID: {pid})...")
    try:
        import psutil
        parent = psutil.Process(pid)
        children = parent.children(recursive=True)
        for child in children:
            try:
                child.kill()
            except Exception:
                pass
        parent.kill()
    except Exception:
        pass

    # Windows 强杀进程树
    if sys.platform == "win32":
        try:
            subprocess.run(f"taskkill /F /T /PID {pid}", shell=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:
            pass


def wait_for_port_release(host: str, port: int, max_wait: float = 4.0) -> bool:
    """等待端口完全释放"""
    start = time.time()
    while time.time() - start < max_wait:
        if not is_tcp_port_open(host, port):
            return True
        time.sleep(0.3)
    return not is_tcp_port_open(host, port)


def cleanup_stale_launcher_instances(current_pid: int):
    """
    扫描并安全清理属于本项目的历史旧 launcher/python 僵尸进程
    仅清理 cmdline 中明确包含 'launcher.py' 的历史实例，绝不误伤系统其他无关 Python 任务
    """
    try:
        import psutil
        stale_pids = []
        for proc in psutil.process_iter(['pid', 'name', 'cmdline']):
            try:
                if proc.pid == current_pid:
                    continue
                pname = (proc.info.get('name') or '').lower()
                if 'python' in pname:
                    cmd_str = ' '.join(proc.info.get('cmdline') or []).lower()
                    if 'launcher.py' in cmd_str:
                        stale_pids.append(proc.pid)
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        if stale_pids:
            print(f"[*] 发现历史启动器残留进程 (PID: {stale_pids})，正在安全回收...")
            for pid in stale_pids:
                terminate_process_tree(pid)
            time.sleep(0.3)
    except Exception:
        pass


def ensure_port_clean(host: str, port: int, force: bool = False, restart: bool = False) -> bool:
    """
    检查端口占用，执行版本比对与旧进程闭环回收
    """
    if not is_tcp_port_open(host, port):
        return True

    print(f"\n[*] 检测到端口 {port} 处于监听状态，正在深度探测实例归属与版本信息...")
    meta = query_running_version(host, port)
    pids = find_pids_on_port(port)

    remote_pid = meta.get("process_id")
    if remote_pid and remote_pid not in pids:
        pids.append(remote_pid)

    is_our_service = (meta.get("service") == SERVICE_NAME)
    remote_version = meta.get("version")

    if is_our_service:
        if remote_version == APP_VERSION and not force and not restart:
            if not query_readiness(host, port):
                print("⚠️ [初始化中] 目标服务版本匹配，但就绪探测尚未通过，请稍后重试。")
                return False
            print(f"✅ [已就绪] 目标端口正在运行最新版本服务 (v{remote_version}, PID: {pids or remote_pid})。")
            print("[*] 无需重复启动，正在为您直接呼起中控大屏...")
            webbrowser.open(f"http://{host}:{port}/console")
            return False
        else:
            if restart:
                print(f"[*] [重启系统] 收到重启指令，正在安全回收历史服务进程 (PID: {pids or remote_pid})...")
            else:
                print(f"⚠️ [版本更新] 发现旧版本服务正在运行 (现有: v{remote_version}, 待启动: v{APP_VERSION})！")
                print("[*] 正在自动执行全进程树安全回收与端口释放...")
            try:
                req = urllib.request.Request(f"http://{host}:{port}/api/v1/system/shutdown", data=b"{}", headers={"Content-Type": "application/json"})
                urllib.request.urlopen(req, timeout=1.0)
            except Exception:
                pass
            time.sleep(0.5)

            for pid in pids:
                terminate_process_tree(pid)

            if wait_for_port_release(host, port):
                print(f"✅ 历史旧服务进程 (PID: {pids}) 已彻底清理，端口 {port} 已就绪。")
                return True
            else:
                print(f"❌ 端口 {port} 清理超时，请手动检查占用。")
                return False
    else:
        print(f"⚠️ 警告: 端口 {port} 被外部未知进程占用 (PIDs: {pids})！")
        if force or restart:
            print("[*] 已启用强制清理参数，正在强制清理...")
            for pid in pids:
                terminate_process_tree(pid)
            return wait_for_port_release(host, port)
        else:
            print("❌ 启动中止: 端口冲突！如需强制覆盖启动，请附加 --restart 或 --force 参数。")
            return False


def wait_and_open_browser(host: str, port: int, open_browser_flag: bool = True):
    """后台监控轮询：待新服务完全就绪并返回匹配版本后，呼起默认浏览器"""
    if not open_browser_flag:
        return

    url = f"http://{host}:{port}/console"
    print("\n[*] 正在等待后端核心服务初始化...")
    for _ in range(35):
        time.sleep(0.4)
        meta = query_running_version(host, port, timeout=0.8)
        if (
            meta.get("service") == SERVICE_NAME
            and meta.get("version") == APP_VERSION
            and query_readiness(host, port, timeout=0.8)
        ):
            print(f"\n🚀 [服务就绪] 智能直播中控系统已全面上线 (v{APP_VERSION})！自动呼起控制大屏: {url}\n")
            try:
                webbrowser.open(url)
            except Exception:
                print(f"[!] 浏览器自动呼起受限，请在浏览器中访问: {url}")
            return
    print(f"[!] 等待超时，服务可能仍在初始化或正在加载资源，请在浏览器中访问: {url}")


def main():
    parser = argparse.ArgumentParser(description="AI-LiveStream-Agent 商用级跨平台启动器")
    parser.add_argument("--check-only", "--precheck-only", action="store_true", help="仅执行环境与端口版本全景体检，不启动服务")
    parser.add_argument("--auto-install", action="store_true", help="当检测到依赖缺失时，自动静默通过国内镜像安装")
    parser.add_argument("--restart", action="store_true", help="强制终止并重启旧实例")
    parser.add_argument("--force", action="store_true", help="强制清理占用目标端口的历史进程")
    parser.add_argument("--no-browser", action="store_true", help="启动后不自动呼起浏览器")
    parser.add_argument("--host", default=SERVER_HOST, help="指定监听主机地址")
    parser.add_argument("--port", type=int, default=SERVER_PORT, help="指定监听端口")
    args = parser.parse_args()

    print_banner()

    # 1. 若为重启或强制模式，优先清理残留的 launcher 历史进程
    if args.restart or args.force:
        cleanup_stale_launcher_instances(os.getpid())

    # 2. 执行商用现场全景体检
    if not run_commercial_preflight(auto_install=args.auto_install):
        sys.exit(1)

    # 3. 深度端口归属校验与版本回收
    can_start = ensure_port_clean(args.host, args.port, force=args.force, restart=args.restart)

    if args.check_only:
        print("[*] 商用环境自检已完成 (--check-only)，退出。")
        sys.exit(0)

    if not can_start:
        sys.exit(0)

    # 4. 启动后台就绪轮询与浏览器自动唤起
    threading.Thread(
        target=wait_and_open_browser,
        args=(args.host, args.port, not args.no_browser),
        daemon=True
    ).start()

    print(f"\n[*] 本地调度核心正在启动 (Host: {args.host}, Port: {args.port})...\n")
    import uvicorn
    uvicorn.run(
        "server.app:app",
        host=args.host,
        port=args.port,
        reload=False,
    )


if __name__ == "__main__":
    main()
