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
    is_active: bool = False
    api_key: str = ""

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


# -----------------------------------------------------------------------------
# 2. 云端显卡感知与探查
# -----------------------------------------------------------------------------
async def get_active_cloud_gpu() -> CloudGpuInfo:
    """
    检查系统当前是否配置并激活了私有化/第三方云端显卡算力节点：
    1. 检查数据库 api_provider_configs 中的 neural_renderer (如 sidecar_v3, liveavatar_lite)；
    2. 检查数据库 api_provider_configs 中的 remote_gpu；
    3. 检查系统环境变量 CLOUD_GPU_URL 或 SIDECAR_URL。
    """
    # 优先检查环境变量
    env_sidecar = os.environ.get("CLOUD_GPU_URL") or os.environ.get("SIDECAR_URL")
    if env_sidecar and env_sidecar.strip():
        return CloudGpuInfo(
            configured=True,
            provider_name="env_sidecar",
            adapter="sidecar_v3",
            base_url=env_sidecar.strip(),
            is_active=True,
            api_key=os.environ.get("CLOUD_GPU_KEY", ""),
        )

    # 查询数据库配置
    try:
        from server.database.db import AsyncSessionLocal
        from server.database.models import ApiProviderConfig
        from sqlalchemy import select

        async with AsyncSessionLocal() as db:
            # 优先查询 neural_renderer (sidecar_v3 等)
            stmt = select(ApiProviderConfig).where(
                ApiProviderConfig.config_group.in_(["neural_renderer", "remote_gpu"]),
                ApiProviderConfig.is_active == 1,
            )
            res = await db.execute(stmt)
            cfg = res.scalars().first()
            if cfg and (cfg.base_url or cfg.provider_name in ["sidecar_v3", "remote_gpu"]):
                return CloudGpuInfo(
                    configured=True,
                    provider_name=cfg.provider_name,
                    adapter=cfg.provider_name,
                    base_url=cfg.base_url or "",
                    is_active=True,
                    api_key=cfg.encrypted_api_key or "",
                )
    except Exception as e:
        logger.debug(f"查询云端显卡数据库配置忽略: {e}")

    return CloudGpuInfo(configured=False)


def get_active_cloud_gpu_sync() -> CloudGpuInfo:
    """同步版本云端显卡查询 (供非异步环境调用)"""
    # 检查环境变量
    env_sidecar = os.environ.get("CLOUD_GPU_URL") or os.environ.get("SIDECAR_URL")
    if env_sidecar and env_sidecar.strip():
        return CloudGpuInfo(
            configured=True,
            provider_name="env_sidecar",
            adapter="sidecar_v3",
            base_url=env_sidecar.strip(),
            is_active=True,
            api_key=os.environ.get("CLOUD_GPU_KEY", ""),
        )

    # 尝试在事件循环外运行查询
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            # 已在运行的循环中无法 run_until_complete，进行轻量假定或通过缓存
            return CloudGpuInfo(configured=False)
        return loop.run_until_complete(get_active_cloud_gpu())
    except Exception:
        return CloudGpuInfo(configured=False)


# -----------------------------------------------------------------------------
# 3. 核心决策调度器：evaluate_compute
# -----------------------------------------------------------------------------
async def evaluate_compute(
    feature_name: str = "当前功能",
    required_vram_gb: float = MIN_HIGH_PERFORMANCE_VRAM_GB,
    cloud_gpu_override: Optional[CloudGpuInfo] = None,
) -> ComputePlan:
    """
    针对需要高性能显卡的功能进行算力调度研判：
    规则：
    1. 只要配置了云端显卡，系统尽可能【自动优先调用云端显卡】！
    2. 若未配置云端显卡：
       - 若本地显卡不足 (显存 < required_vram_gb 或无 CUDA)：
         明确提示用户硬件不足，阻断或提示降级，并引导配置云端显卡；
       - 若本地显卡满足 (显存 >= required_vram_gb 且 CUDA 可用)：
         允许本地执行。
    """
    local_gpu = probe_local_gpu()
    cloud_gpu = cloud_gpu_override if cloud_gpu_override is not None else await get_active_cloud_gpu()

    local_gpu_dict = local_gpu.to_dict()
    cloud_gpu_dict = cloud_gpu.to_dict() if cloud_gpu.configured else None

    # 分支 1：系统已配置可用的云端显卡 -> 尽可能优先调用云端显卡！
    if cloud_gpu.configured:
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

    # 分支 2：未配置云端显卡，且本地显卡显存不足（< 2GB 或无 CUDA） -> 必须明确提示用户！
    if local_gpu.is_low_spec or local_gpu.vram_total_gb < required_vram_gb or not local_gpu.cuda_available:
        hardware_label = f"{local_gpu.gpu_name or '核显/轻薄本显卡'} (显存 {local_gpu.vram_total_gb}GB)"
        msg = (
            f"⚠️【硬件显存不足警示】当前功能【{feature_name}】必须用到高性能显卡（显存需 > {required_vram_gb}GB）。\n"
            f"检测到您的本地显卡为 {hardware_label}，显存不足以承载深度学习模型，且系统当前【尚未对接云端显卡】！\n"
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
            has_cloud_gpu=False,
            local_gpu=local_gpu_dict,
            cloud_gpu=None,
            alert_type="insufficient_hardware",
            user_message=msg,
            recommended_driver="procedural",
        )

    # 分支 3：本地拥有充足显存（>= 2GB 且 CUDA 可用），本地执行
    return ComputePlan(
        feature_name=feature_name,
        required_vram_gb=required_vram_gb,
        use_cloud=False,
        can_execute=True,
        is_low_spec_local=False,
        has_cloud_gpu=False,
        local_gpu=local_gpu_dict,
        cloud_gpu=None,
        alert_type="none",
        user_message="",
        recommended_driver="livetalking",
    )
