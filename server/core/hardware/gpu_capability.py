# -*- coding: utf-8 -*-
"""
全局硬件显卡与云端算力智能调度引擎 (GPU Capability & Cloud Scheduler)
战略规则与指导思想：
1. 本地显卡配置较低（显存 <= 2GB 或核显/无 CUDA）；
2. 尽可能优先调用已经配置的云端显卡 (Sidecar / 远程 GPU) 进行高性能计算；
3. 如果某功能必须用到高性能（大于 2G）的显卡，在本地显卡不够且未对接云端显卡的情况下，
   向用户明确警示并提供跳转配置引导，阻止系统静默崩溃或卡死。
"""
import asyncio
import json
import logging
import os
import shutil
import subprocess
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional

logger = logging.getLogger("LiveAgent.GPUCapability")

# 显存阈值常数定义 (GB)
MIN_HIGH_PERFORMANCE_VRAM_GB = 2.0  # 高性能显卡分水岭（>=2G 视为具备轻量 GPU 运算条件）
OPTIMAL_LIVE_VRAM_GB = 6.0         # 深度学习数字人实时推流建议显存


@dataclass
class GpuInfo:
    """本地物理显卡规格与显存负载"""
    gpu_name: Optional[str] = None
    vram_total_gb: float = 0.0
    vram_used_gb: float = 0.0
    cuda_available: bool = False
    is_low_spec: bool = True  # 显存 < 2.0GB 或无 CUDA 即视为低配显卡

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class CloudGpuInfo:
    """已配置的私有/第三方云端显卡算力节点 (Sidecar / Remote GPU)"""
    configured: bool = False
    provider_name: str = ""
    adapter: str = ""
    base_url: str = ""
    is_active: bool = False       # 数据库中是否配置激活且经真实探活握手成功
    is_reachable: bool = False    # 真实网络探活结果 (TCP/TLS握手实测)
    reachability_error: str = ""  # 实测未连通的精准原因 (如未开机/超时/域名无效)
    api_key: str = ""
    device_info: str = ""         # 远端识别设备描述 (如 "Tesla T4, 15360 MiB")
    gpu_name: str = ""            # 提取显卡型号 (如 "Tesla T4")
    vram_total_gb: float = 0.0    # 提取显存容量 (如 15.0)

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        if d.get("api_key"):
            d["api_key"] = "***"  # 安全脱敏
        return d



@dataclass
class ComputePlan:
    """算力调度与用户引导研判结果"""
    feature_name: str = "当前功能"
    required_vram_gb: float = 2.0
    use_cloud: bool = False
    can_execute: bool = True
    is_low_spec_local: bool = True
    has_cloud_gpu: bool = False
    local_gpu: Dict[str, Any] = field(default_factory=dict)
    cloud_gpu: Optional[Dict[str, Any]] = None
    alert_type: str = "none"  # "none" | "cloud_dispatched" | "insufficient_hardware"
    user_message: str = ""
    recommended_driver: str = "procedural"  # "cloud_sidecar" | "procedural" | "livetalking"

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


# -----------------------------------------------------------------------------
# 1. 本地显卡探针 (带 3.0s 缓存，避免高频子进程开销)
# -----------------------------------------------------------------------------
_LOCAL_GPU_CACHE: Dict[str, Any] = {"ts": 0.0, "data": None}
_CACHE_TTL_SEC = 3.0


def probe_local_gpu(force_refresh: bool = False) -> GpuInfo:
    """
    探测本机显卡规格与实时状态：
    1. 优先尝试 PyTorch CUDA；
    2. 其次尝试 nvidia-smi；
    3. 再次尝试 Windows WMI 兜底；
    4. 评估是否属于 is_low_spec (< 2.0GB 或无 CUDA)。
    """
    now = time.time()
    if not force_refresh and _LOCAL_GPU_CACHE["data"] is not None:
        if now - _LOCAL_GPU_CACHE["ts"] < _CACHE_TTL_SEC:
            return _LOCAL_GPU_CACHE["data"]

    info = GpuInfo(gpu_name=None, vram_total_gb=0.0, vram_used_gb=0.0, cuda_available=False, is_low_spec=True)

    # 1. PyTorch CUDA
    try:
        import torch
        if torch.cuda.is_available():
            props = torch.cuda.get_device_properties(0)
            used, total = torch.cuda.mem_get_info(0)
            total_gb = round(total / (1024 ** 3), 2)
            used_gb = round((total - used) / (1024 ** 3), 2)
            info = GpuInfo(
                gpu_name=props.name,
                vram_total_gb=total_gb,
                vram_used_gb=used_gb,
                cuda_available=True,
                is_low_spec=(total_gb < MIN_HIGH_PERFORMANCE_VRAM_GB),
            )
            _LOCAL_GPU_CACHE.update(ts=now, data=info)
            return info
    except Exception:
        pass

    # 2. nvidia-smi
    smi = shutil.which("nvidia-smi")
    if not smi and os.name == "nt":
        candidates = [
            r"C:\Program Files\NVIDIA Corporation\NVSMI\nvidia-smi.exe",
            r"C:\Windows\System32\nvidia-smi.exe",
        ]
        for c in candidates:
            if os.path.exists(c):
                smi = c
                break

    if smi:
        try:
            out = subprocess.run(
                [smi, "--query-gpu=name,memory.total,memory.used", "--format=csv,noheader,nounits"],
                capture_output=True,
                text=True,
                timeout=3,
            )
            if out.returncode == 0 and out.stdout.strip():
                line = out.stdout.strip().splitlines()[0]
                name, total, used = [x.strip() for x in line.split(",")]
                total_gb = round(float(total) / 1024, 2)
                used_gb = round(float(used) / 1024, 2)
                info = GpuInfo(
                    gpu_name=name,
                    vram_total_gb=total_gb,
                    vram_used_gb=used_gb,
                    cuda_available=True,
                    is_low_spec=(total_gb < MIN_HIGH_PERFORMANCE_VRAM_GB),
                )
                _LOCAL_GPU_CACHE.update(ts=now, data=info)
                return info
        except Exception:
            pass

    # 3. WMI 兜底 (核显/集显识别)
    if os.name == "nt":
        try:
            out = subprocess.run(
                ["wmic", "path", "win32_VideoController", "get", "name,adapterram", "/value"],
                capture_output=True,
                text=True,
                timeout=3,
            )
            if out.returncode == 0:
                name = None
                ram_gb = 0.0
                for line in out.stdout.splitlines():
                    if line.startswith("Name="):
                        name = line.split("=", 1)[1].strip()
                    elif line.startswith("AdapterRAM="):
                        try:
                            bytes_val = int(line.split("=", 1)[1].strip())
                            ram_gb = round(bytes_val / (1024 ** 3), 2)
                        except Exception:
                            pass
                if name:
                    info = GpuInfo(
                        gpu_name=name,
                        vram_total_gb=ram_gb,
                        vram_used_gb=0.0,
                        cuda_available=False,
                        is_low_spec=True,
                    )
        except Exception:
            pass

    _LOCAL_GPU_CACHE.update(ts=now, data=info)
    return info


def detect_gpu_capability(force_refresh: bool = False) -> Dict[str, Any]:
    """快捷返回规范字典形态的 GPU 探测信息，供语音合成与算力调度器直接消费"""
    gpu = probe_local_gpu(force_refresh=force_refresh)
    return {
        "has_cuda": gpu.cuda_available,
        "device_name": gpu.gpu_name or "CPU",
        "gpu_name": gpu.gpu_name or "CPU",
        "vram_total_gb": gpu.vram_total_gb,
        "vram_used_gb": gpu.vram_used_gb,
        "is_low_spec": gpu.is_low_spec,
    }


# -----------------------------------------------------------------------------
# 2. 云端显卡感知与真实网络探查
# -----------------------------------------------------------------------------
_CLOUD_PROBE_CACHE: Dict[str, Any] = {}
_PROBE_TTL_SEC = 30.0


def parse_device_string(device_str: str) -> tuple[str, float]:
    """解析远端显卡设备字符串，如 'Tesla T4, 15360 MiB' -> ('Tesla T4', 15.0)"""
    if not device_str or not device_str.strip():
        return "", 0.0
    import re
    device_str = device_str.strip()
    parts = [p.strip() for p in device_str.split(",")]
    gpu_name = parts[0] if parts else device_str
    vram_gb = 0.0
    for p in parts[1:]:
        m = re.search(r"(\d+)\s*(?:MiB|MB)", p, re.IGNORECASE)
        if m:
            vram_gb = round(int(m.group(1)) / 1024, 1)
            break
        m_gb = re.search(r"(\d+(?:\.\d+)?)\s*GB", p, re.IGNORECASE)
        if m_gb:
            vram_gb = float(m_gb.group(1))
            break
    return gpu_name, vram_gb


def record_cloud_probe_success(url: str, latency_ms: int = 0, device: str = ""):
    """在外部握手成功 (例如系统设置页点击测试通过) 时，实时同步写入探活缓存"""
    if not url:
        return
    clean_url = url.strip()
    gpu_name, vram_gb = parse_device_string(device)
    _CLOUD_PROBE_CACHE[clean_url] = {
        "ts": time.time(),
        "reachable": True,
        "error": "",
        "latency_ms": latency_ms,
        "device": device,
        "gpu_name": gpu_name,
        "vram_gb": vram_gb,
    }


async def verify_cloud_endpoint_reachability(url: str, timeout_sec: float = 3.5, force_refresh: bool = False) -> tuple[bool, str]:
    """
    对云端 GPU 算力节点的 URL 进行真实的网络连通性握手探活：
    1. 解析 host、port、协议 (http/https/ws/wss)；
    2. 若为 WebSocket 渲染节点，优先握手并提取远端显卡型号与显存 (如 Tesla T4, 15360 MiB)；
    3. 进行真实的 TCP 连接握手或 TLS 探活；
    4. 如果连不上（未开机、超时、拒绝连接、域名解析失败），明确返回 (False, 错误原因)。
    实事求是，绝不伪造连通状态！
    """
    if not url or not url.strip():
        return False, "未配置云端算力节点地址"
    url = url.strip()
    now = time.time()
    if not force_refresh and url in _CLOUD_PROBE_CACHE:
        item = _CLOUD_PROBE_CACHE[url]
        # 成功节点缓存 30 秒，失败节点仅缓存 3 秒允许快速重试
        ttl = _PROBE_TTL_SEC if item.get("reachable") else 3.0
        if now - item["ts"] < ttl:
            return item["reachable"], item["error"]

    from urllib.parse import urlparse
    import socket

    parsed = urlparse(url)
    scheme = (parsed.scheme or "http").lower()
    host = parsed.hostname
    port = parsed.port
    if not host:
        res = (False, "云端节点地址格式无效，无法解析主机名")
        _CLOUD_PROBE_CACHE[url] = {"ts": now, "reachable": False, "error": res[1]}
        return res

    if not port:
        port = 443 if scheme in ("https", "wss") else 80

    use_ssl = scheme in ("https", "wss")

    # 1. 优先执行快速 TCP / TLS 握手探活：毫秒级研判目标主机是否开机、端口是否开放
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port, ssl=use_ssl),
            timeout=timeout_sec
        )
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass
    except asyncio.TimeoutError:
        err = f"网络连接超时 (> {timeout_sec}s)，云端实例可能未开机或端口受阻"
        _CLOUD_PROBE_CACHE[url] = {"ts": now, "reachable": False, "error": err}
        return False, err
    except socket.gaierror:
        err = "DNS 域名解析失败，云端实例地址可能已失效或关机"
        _CLOUD_PROBE_CACHE[url] = {"ts": now, "reachable": False, "error": err}
        return False, err
    except ConnectionRefusedError:
        err = "云端连接被拒绝，Sidecar 服务未运行或端口未监听"
        _CLOUD_PROBE_CACHE[url] = {"ts": now, "reachable": False, "error": err}
        return False, err
    except Exception as e:
        err = f"网络连接异常: {e}"
        _CLOUD_PROBE_CACHE[url] = {"ts": now, "reachable": False, "error": err}
        return False, err

    # 2. TCP 握手成功证实开机后，针对 WebSocket 节点尝试握手并提取远端显卡硬件型号与显存
    dev = ""
    gpu_name = ""
    vram_gb = 0.0
    if scheme in ("ws", "wss"):
        try:
            import websockets
            async with websockets.connect(url, open_timeout=min(timeout_sec, 2.0), close_timeout=0.5) as ws:
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=1.5)
                    if isinstance(raw, str):
                        data = json.loads(raw)
                        if isinstance(data, dict):
                            dev = str(data.get("device") or "")
                except Exception:
                    pass
            if dev:
                gpu_name, vram_gb = parse_device_string(dev)
        except Exception:
            pass

    _CLOUD_PROBE_CACHE[url] = {
        "ts": now,
        "reachable": True,
        "error": "",
        "device": dev or _CLOUD_PROBE_CACHE.get(url, {}).get("device", ""),
        "gpu_name": gpu_name or _CLOUD_PROBE_CACHE.get(url, {}).get("gpu_name", ""),
        "vram_gb": vram_gb or _CLOUD_PROBE_CACHE.get(url, {}).get("vram_gb", 0.0),
    }
    return True, ""


def verify_cloud_endpoint_reachability_sync(url: str, timeout_sec: float = 1.0) -> tuple[bool, str]:
    """同步阻塞版本探活（用于非异步上下文）"""
    if not url or not url.strip():
        return False, "未配置云端算力节点地址"
    url = url.strip()
    from urllib.parse import urlparse
    import socket
    import ssl

    parsed = urlparse(url)
    scheme = (parsed.scheme or "http").lower()
    host = parsed.hostname
    port = parsed.port
    if not host:
        return False, "无效的主机名"
    if not port:
        port = 443 if scheme in ("https", "wss") else 80
    use_ssl = scheme in ("https", "wss")

    try:
        sock = socket.create_connection((host, port), timeout=timeout_sec)
        if use_ssl:
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            sock = ctx.wrap_socket(sock, server_hostname=host)
        sock.close()
        return True, ""
    except Exception as e:
        return False, f"云端未开机或不可达: {e}"


async def get_active_cloud_gpu() -> CloudGpuInfo:
    """
    检查系统当前是否配置并激活了私有化/第三方云端显卡算力节点，
    并进行严格的网络探活握手实测：
    1. 检查环境变量或数据库配置；
    2. 发起真实的 TCP/TLS 网络连通探测；
    3. 只有实测通畅才标记 is_reachable=True / is_active=True；
       若未开机或网络不通，如实标记 is_reachable=False，绝不欺骗系统或用户！
    """
    # 优先检查环境变量
    env_sidecar = os.environ.get("CLOUD_GPU_URL") or os.environ.get("SIDECAR_URL")
    if env_sidecar and env_sidecar.strip():
        url = env_sidecar.strip()
        reachable, err = await verify_cloud_endpoint_reachability(url)
        return CloudGpuInfo(
            configured=True,
            provider_name="env_sidecar",
            adapter="sidecar_v3",
            base_url=url,
            is_active=reachable,
            is_reachable=reachable,
            reachability_error=err,
            api_key=os.environ.get("CLOUD_GPU_KEY", ""),
        )

    # 查询数据库配置
    try:
        from server.database.db import AsyncSessionLocal
        from server.database.models import ApiProviderConfig
        from sqlalchemy import select

        async with AsyncSessionLocal() as db:
            # 优先查询 neural_renderer / remote_gpu，并按更新时间倒序排序 (优先检查最新配置)
            stmt = (
                select(ApiProviderConfig)
                .where(
                    ApiProviderConfig.config_group.in_(["neural_renderer", "remote_gpu"]),
                    ApiProviderConfig.is_active == 1,
                )
                .order_by(ApiProviderConfig.updated_at.desc())
            )
            res = await db.execute(stmt)
            cfgs = res.scalars().all()
            if cfgs:
                best_cfg = None
                best_err = ""
                # 遍历所有激活的节点，优先挑选实测网络连通的在线节点
                for c in cfgs:
                    url = (c.base_url or "").strip()
                    if not url:
                        continue
                    reachable, err = await verify_cloud_endpoint_reachability(url)
                    if reachable:
                        c_meta = _CLOUD_PROBE_CACHE.get(url, {})
                        return CloudGpuInfo(
                            configured=True,
                            provider_name=c.provider_name,
                            adapter=c.provider_name,
                            base_url=url,
                            is_active=True,
                            is_reachable=True,
                            reachability_error="",
                            api_key=c.encrypted_api_key or "",
                            device_info=c_meta.get("device", ""),
                            gpu_name=c_meta.get("gpu_name", ""),
                            vram_total_gb=c_meta.get("vram_gb", 0.0),
                        )
                    elif best_cfg is None:
                        best_cfg = c
                        best_err = err

                if best_cfg:
                    c_meta = _CLOUD_PROBE_CACHE.get(best_cfg.base_url or "", {})
                    return CloudGpuInfo(
                        configured=True,
                        provider_name=best_cfg.provider_name,
                        adapter=best_cfg.provider_name,
                        base_url=best_cfg.base_url or "",
                        is_active=False,
                        is_reachable=False,
                        reachability_error=best_err,
                        api_key=best_cfg.encrypted_api_key or "",
                        device_info=c_meta.get("device", ""),
                        gpu_name=c_meta.get("gpu_name", ""),
                        vram_total_gb=c_meta.get("vram_gb", 0.0),
                    )
    except Exception as e:
        logger.debug(f"查询云端显卡数据库配置忽略: {e}")

    return CloudGpuInfo(configured=False)


def get_active_cloud_gpu_sync() -> CloudGpuInfo:
    """同步版本云端显卡查询 (供非异步环境调用)"""
    env_sidecar = os.environ.get("CLOUD_GPU_URL") or os.environ.get("SIDECAR_URL")
    if env_sidecar and env_sidecar.strip():
        url = env_sidecar.strip()
        reachable, err = verify_cloud_endpoint_reachability_sync(url)
        return CloudGpuInfo(
            configured=True,
            provider_name="env_sidecar",
            adapter="sidecar_v3",
            base_url=url,
            is_active=reachable,
            is_reachable=reachable,
            reachability_error=err,
            api_key=os.environ.get("CLOUD_GPU_KEY", ""),
        )

    try:
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=1) as executor:
            return executor.submit(lambda: asyncio.run(get_active_cloud_gpu())).result(timeout=3.0)
    except Exception as e:
        logger.debug(f"get_active_cloud_gpu_sync 执行异常: {e}")
        return CloudGpuInfo(configured=False)



SETTING_KEY_GPU_TARGET = "gpu_execution_target"  # auto / local / cloud


async def get_user_gpu_target_preference() -> str:
    """读取用户配置的显卡执行目标偏好: auto (自动) / local (本地优先) / cloud (云端优先)"""
    try:
        from server.database.db import AsyncSessionLocal
        from server.database.models import AppSetting
        from sqlalchemy import select

        async with AsyncSessionLocal() as db:
            stmt = select(AppSetting).where(AppSetting.key == SETTING_KEY_GPU_TARGET)
            res = await db.execute(stmt)
            row = res.scalar_one_or_none()
            if row and row.value:
                val = str(row.value).strip().lower()
                if val in ("auto", "local", "cloud"):
                    return val
    except Exception:
        pass
    return "auto"


# -----------------------------------------------------------------------------
# 3. 核心决策调度器：evaluate_compute
# -----------------------------------------------------------------------------
async def evaluate_compute(
    feature_name: str = "当前功能",
    required_vram_gb: float = MIN_HIGH_PERFORMANCE_VRAM_GB,
    cloud_gpu_override: Optional[CloudGpuInfo] = None,
    target_override: Optional[str] = None,
) -> ComputePlan:
    """
    针对需要高性能显卡的功能进行算力调度研判：
    本项目使用者定位：
    1. 硬件要求完全满足的用户 (拥有高性能本地独显)
    2. 硬件不足并且对接了远端租赁 GPU 的用户 (通过云端算力节点)

    两条明确分支：
    - 分支 1 (本地高性能显卡)：优先使用本地 CUDA 显卡执行实时深度学习推理；
    - 分支 2 (远端租赁 GPU)：优先调度至 Sidecar / 远程云端显卡节点接管渲染；
    - 防御阻断：本地不足且未配置远端租赁 GPU 时，明确提示两类用户的配置方案。
    """
    local_gpu = probe_local_gpu()
    cloud_gpu = cloud_gpu_override if cloud_gpu_override is not None else await get_active_cloud_gpu()
    target_pref = target_override or await get_user_gpu_target_preference()

    local_gpu_dict = local_gpu.to_dict()
    cloud_gpu_dict = cloud_gpu.to_dict() if cloud_gpu.configured else None

    local_capable = (not local_gpu.is_low_spec) and (local_gpu.vram_total_gb >= required_vram_gb) and local_gpu.cuda_available

    # -------------------------------------------------------------------------
    # 策略 A: 用户显式指定强制使用【本地高性能显卡】
    # -------------------------------------------------------------------------
    if target_pref == "local":
        if local_capable:
            return ComputePlan(
                feature_name=feature_name,
                required_vram_gb=required_vram_gb,
                use_cloud=False,
                can_execute=True,
                is_low_spec_local=False,
                has_cloud_gpu=cloud_gpu.configured,
                local_gpu=local_gpu_dict,
                cloud_gpu=cloud_gpu_dict,
                alert_type="none",
                user_message="已按用户配置优先采用【本地高性能显卡】执行。",
                recommended_driver="livetalking",
            )
        else:
            hardware_label = f"{local_gpu.gpu_name or '轻薄本/低配显卡'} (显存 {local_gpu.vram_total_gb}GB)"
            msg = (
                f"⚠️【本地显卡算力不足】您配置了偏好使用【本地显卡】，但当前功能【{feature_name}】需显存 > {required_vram_gb}GB。\n"
                f"检测到本地设备为 {hardware_label}，无法流畅承载。\n"
                f"👉 建议方案：请在【系统设置 -> 显卡与渲染设置】中切换为【远端租赁GPU】模式并对接云端节点。"
            )
            return ComputePlan(
                feature_name=feature_name,
                required_vram_gb=required_vram_gb,
                use_cloud=False,
                can_execute=False,
                is_low_spec_local=True,
                has_cloud_gpu=cloud_gpu.configured,
                local_gpu=local_gpu_dict,
                cloud_gpu=cloud_gpu_dict,
                alert_type="insufficient_hardware",
                user_message=msg,
                recommended_driver="procedural",
            )

    # -------------------------------------------------------------------------
    # 策略 B: 用户显式指定强制使用【远端租赁 GPU】
    # -------------------------------------------------------------------------
    if target_pref == "cloud":
        if cloud_gpu.configured and cloud_gpu.is_reachable:
            msg = f"【云端显卡加速】已实测握手连通远端租赁显卡节点【{cloud_gpu.provider_name}】，正在调度处理【{feature_name}】。"
            return ComputePlan(
                feature_name=feature_name,
                required_vram_gb=required_vram_gb,
                use_cloud=True,
                can_execute=True,
                is_low_spec_local=local_gpu.is_low_spec,
                has_cloud_gpu=True,
                local_gpu=local_gpu_dict,
                cloud_gpu=cloud_gpu_dict,
                alert_type="cloud_dispatched",
                user_message=msg,
                recommended_driver="cloud_sidecar",
            )
        elif cloud_gpu.configured and not cloud_gpu.is_reachable:
            # 配置了但未开机或无法连通，实事求是警示，绝对不伪造连通！
            err_reason = cloud_gpu.reachability_error or "云端实例未开机或端口受阻"
            msg = (
                f"⚠️【远端租赁GPU未连通】您偏好指定使用【远端租赁GPU】，已配置节点【{cloud_gpu.provider_name}】({cloud_gpu.base_url})，\n"
                f"但系统发起真实握手探活失败：{err_reason}。\n"
                f"👉 真实状态：远端 GPU 尚未开机或未建立有效握手。请先启动远端 GPU 主机，或在系统设置中切换为自动/本地模式。"
            )
            return ComputePlan(
                feature_name=feature_name,
                required_vram_gb=required_vram_gb,
                use_cloud=False,
                can_execute=False,
                is_low_spec_local=local_gpu.is_low_spec,
                has_cloud_gpu=False,
                local_gpu=local_gpu_dict,
                cloud_gpu=cloud_gpu_dict,
                alert_type="insufficient_hardware",
                user_message=msg,
                recommended_driver="procedural",
            )
        else:
            msg = (
                "⚠️【未配置远端租赁显卡】您配置了偏好使用【远端租赁GPU】，但系统尚未检测到激活的云端算力节点。\n"
                "👉 请前往【系统设置 -> 显卡与渲染设置】添加已租用的 GPU 节点 (如 AutoDL / Sidecar)。"
            )
            return ComputePlan(
                feature_name=feature_name,
                required_vram_gb=required_vram_gb,
                use_cloud=False,
                can_execute=False,
                is_low_spec_local=local_gpu.is_low_spec,
                has_cloud_gpu=False,
                local_gpu=local_gpu_dict,
                cloud_gpu=None,
                alert_type="insufficient_hardware",
                user_message=msg,
                recommended_driver="procedural",
            )

    # -------------------------------------------------------------------------
    # 策略 C: 默认自动判断 (auto)
    # -------------------------------------------------------------------------
    # 1. 只要配置了远端云显卡并处于激活或实测连通状态 (is_active 或 is_reachable)，本地不足时优先调度远端
    if cloud_gpu.configured and (cloud_gpu.is_reachable or cloud_gpu.is_active) and not local_capable:
        msg = (
            f"【云端显卡加速】检测到本地显卡（{local_gpu.gpu_name or '轻量配置'}，"
            f"显存 {local_gpu.vram_total_gb}GB）低于高画质负载需求，"
            f"系统已自动优先调度已配置的云端显卡节点【{cloud_gpu.provider_name}】处理【{feature_name}】，释放本地硬件压力。"
        )
        return ComputePlan(
            feature_name=feature_name,
            required_vram_gb=required_vram_gb,
            use_cloud=True,
            can_execute=True,
            is_low_spec_local=local_gpu.is_low_spec,
            has_cloud_gpu=True,
            local_gpu=local_gpu_dict,
            cloud_gpu=cloud_gpu_dict,
            alert_type="cloud_dispatched",
            user_message=msg,
            recommended_driver="cloud_sidecar",
        )

    # 2. 如果配置了远端云显卡，但实测未连通（未开机）
    remote_notice = ""
    if cloud_gpu.configured and not cloud_gpu.is_reachable and not cloud_gpu.is_active:
        remote_notice = f"（注：已配置的远端GPU【{cloud_gpu.provider_name}】实测未开机/未连通）"

    # 3. 若本地拥有充足显卡能力 (>= 2GB 且 CUDA 可用)，优先走本地高性能显卡分支
    if local_capable:
        return ComputePlan(
            feature_name=feature_name,
            required_vram_gb=required_vram_gb,
            use_cloud=False,
            can_execute=True,
            is_low_spec_local=False,
            has_cloud_gpu=cloud_gpu.is_reachable or cloud_gpu.is_active,
            local_gpu=local_gpu_dict,
            cloud_gpu=cloud_gpu_dict,
            alert_type="none",
            user_message=f"已由【本地高性能显卡】执行{remote_notice}。",
            recommended_driver="livetalking",
        )

    # 4. 本地为低配/核显，且远端未开机或未配置
    hardware_label = f"{local_gpu.gpu_name or '核显/轻薄本显卡'} (显存 {local_gpu.vram_total_gb}GB)"
    # 对于切片类功能，本地具备 CPU/轻量运行能力
    if "切片" in feature_name or "制作" in feature_name or "离线" in feature_name:
        msg = (
            f"【本地硬件处理】本地设备为 {hardware_label}{remote_notice}，"
            f"系统已实事求是采用本地 CPU 与图像处理流水线完成【{feature_name}】。"
        )
        return ComputePlan(
            feature_name=feature_name,
            required_vram_gb=required_vram_gb,
            use_cloud=False,
            can_execute=True,
            is_low_spec_local=True,
            has_cloud_gpu=False,
            local_gpu=local_gpu_dict,
            cloud_gpu=cloud_gpu_dict,
            alert_type="none",
            user_message=msg,
            recommended_driver="procedural",
        )

    # 5. 高性能实时推流功能需要高性能显卡支持
    cloud_status_text = "配置的远端GPU节点未开机连通" if cloud_gpu.configured else "系统当前【尚未对接云端显卡】"
    msg = (
        f"⚠️【硬件显存不足警示】当前功能【{feature_name}】必须用到高性能显卡（显存需 > {required_vram_gb}GB）。\n"
        f"检测到您的本地显卡为 {hardware_label}，显存不足以承载深度学习模型，且{cloud_status_text}！\n"
        f"👉 建议方案：\n"
        f"1. 请前往【系统设置 -> 显卡与渲染设置】配置自建云端显卡算力节点 (Sidecar) 或第三方云端 GPU（如 AutoDL，约1.2元/小时）；\n"
        f"2. 或切换至【轻量 CPU 免显卡模式】保证系统平稳开播。"
    )
    return ComputePlan(
        feature_name=feature_name,
        required_vram_gb=required_vram_gb,
        use_cloud=False,
        can_execute=False,
        is_low_spec_local=True,
        has_cloud_gpu=cloud_gpu.is_reachable,
        local_gpu=local_gpu_dict,
        cloud_gpu=cloud_gpu_dict,
        alert_type="insufficient_hardware",
        user_message=msg,
        recommended_driver="procedural",
    )



