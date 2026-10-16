# -*- coding: utf-8 -*-
"""硬件探测与算力档位推荐：GPU 委托探测、硬件负载载荷、运行模式推荐。

由 `server/routes/live.py` 拆分而来（原 3419-3535 行）。

**测试缝（late-binding seam）约定**
本模块实现侧凡需引用 `server.routes.live` 上的缝，一律写在**函数体内**做
`from server.routes.live import X`，禁止模块顶层 import。顶层 import 会把
缝变成早绑定，导致 `patch("server.routes.live.<X>")` 静默失效 —— 测试照样绿，
线上行为却变了。相关守卫见 `server/tests/test_live_seam_late_binding.py`。
"""
import asyncio
import logging
import os
import time
from typing import Any, Dict

import psutil
from fastapi import APIRouter

router = APIRouter()
logger = logging.getLogger("LiveAgent.LiveController")


# ---------------------------------------------------------------------------
# 硬件探测与运行档位推荐 (规划 §8.1)
# ---------------------------------------------------------------------------
def probe_gpu() -> dict:
    """本机 GPU 探测 —— 委托 `gpu_capability.probe_local_gpu` 单一事实源。

    历史缺陷：本函数曾自带一套完整的 torch → nvidia-smi → WMI 三级探测
    (`_get_torch` / `_find_nvidia_smi` / `_wmi_gpu_probe`)，与
    `core/hardware/gpu_capability.probe_local_gpu` 并存。两套实现口径一旦
    不一致，就会出现「预检说硬件达标、开播时说不够」的矛盾，且探测逻辑需改
    两处极易漏改。现已统一：探测事实源只有gpu_capability 一处。

    保留本函数（而非让调用方直接引用 gpu_capability）有两个原因：
      1. `hardware_payload` 需要把它卸载到线程池执行（子进程探测会阻塞事件循环）；
      2. 测试以 patch `server.routes.live._probe_gpu` 的方式注入假硬件数据。

    返回结构固定为既有 4 键契约，不得增减字段。
    """
    from server.core.hardware.gpu_capability import probe_local_gpu

    gpu = probe_local_gpu()
    return {
        "gpu_name": gpu.gpu_name,
        "vram_total_gb": gpu.vram_total_gb,
        "vram_used_gb": gpu.vram_used_gb,
        "cuda_available": gpu.cuda_available,
    }


def _resolve_landmarks_for_avatar(avatar_path: str) -> str:
    """
    按主播底图解析人脸关键点缓存 (规划 §4.2)：
    - 已有缓存 (同目录 <图名>_landmarks.json) 直接复用，不重复检测
    - 无缓存则执行一次 MediaPipe/Haar 检测并落盘 (线程池中调用)
    - 底图缺失或检测结果无效时返回空串，渲染器回退比例定位
    """
    if not avatar_path:
        return ""
    from pathlib import Path as _Path
    import json as _json
    from server.core.vision.face_landmarks import detect_face_landmarks

    photo = _Path(avatar_path)
    if not photo.exists():
        return ""
    cache = photo.with_name(f"{photo.stem}_landmarks.json")
    if not cache.exists():
        try:
            detect_face_landmarks(str(photo), str(cache))
        except Exception as e:
            logger.debug(f"人脸关键点检测失败: {e}")
            return ""
    try:
        data = _json.loads(cache.read_text(encoding="utf-8"))
        box = data.get("face_box")
        if box and len(box) == 4 and box[2] > 0:
            return str(cache)
    except Exception:
        pass
    return ""


def recommend_tier(gpu_info: dict) -> str:
    """按显卡硬件与算力推荐运行模式 (纯本地硬件 vs 本地云端混合)"""
    vram = gpu_info.get("vram_total_gb", 0.0) or 0.0
    if gpu_info.get("cuda_available") and vram >= 7.5:
        return "纯本地硬件 (本地单机运行)"
    return "本地云端混合 (端云协同模式)"


# 硬件负载 TTL 缓存 (避免多客户端 5s 轮询反复 spawn nvidia-smi/WMI 子进程)
_HW_PAYLOAD_CACHE: Dict[str, Any] = {"ts": 0.0, "data": None}
_HW_CACHE_TTL_SEC = 3.0


async def hardware_payload() -> dict:
    import platform

    # 测试缝（late-binding seam）：函数体内解析，保证 patch live 上的缝生效
    from server.routes.live import _probe_gpu, _recommend_tier

    now = time.time()
    cached_data = _HW_PAYLOAD_CACHE.get("data")
    last_ts = float(_HW_PAYLOAD_CACHE.get("ts") or 0.0)
    if isinstance(cached_data, dict) and now - last_ts < _HW_CACHE_TTL_SEC:
        # 动态指标 (CPU/内存占用) 实时更新，静态探测 (GPU/型号) 走缓存
        cached = dict(cached_data)
        cpu_percent = psutil.cpu_percent(interval=None)
        memory = psutil.virtual_memory()
        cached.update(
            cpu_percent=cpu_percent,
            ram_used_gb=round(memory.used / (1024 ** 3), 2),
            ram_percent=memory.percent,
        )
        return cached

    cpu_percent = psutil.cpu_percent(interval=None)
    memory = psutil.virtual_memory()
    # 重量级 GPU 探测 (nvidia-smi/WMI 子进程) 卸载到线程池，严禁阻塞事件循环 (ADR-08)
    gpu_info = await asyncio.to_thread(_probe_gpu)
    from server.core.hardware.gpu_capability import evaluate_compute
    compute_plan = await evaluate_compute(feature_name="全局高性能计算", required_vram_gb=2.0)
    cpu_name = platform.processor() or os.environ.get("PROCESSOR_IDENTIFIER", "") or "未知处理器"

    payload = {
        "cpu_name": cpu_name,
        "cpu_cores": psutil.cpu_count(logical=True) or 0,
        "cpu_percent": cpu_percent,
        "ram_used_gb": round(memory.used / (1024 ** 3), 2),
        "ram_total_gb": round(memory.total / (1024 ** 3), 2),
        "ram_percent": memory.percent,
        "gpu": gpu_info,
        "gpu_capability": compute_plan.to_dict(),
        "recommended_mode": _recommend_tier(gpu_info)
    }
    _HW_PAYLOAD_CACHE["ts"] = now
    _HW_PAYLOAD_CACHE["data"] = payload
    return payload


@router.get("/hardware")
async def get_hardware_status():
    """获取本地硬件资源负载与显存建议 (兼容路由)

    注意：本子路由的 ``APIRouter()`` **无前缀**，路径写相对段 ``/hardware``。
    对外完整 URL ``/api/v1/live/hardware`` 由 ``live.py`` 的
    ``APIRouter(prefix="/live")`` + app 挂载前缀 ``/api/v1`` 拼接而来。
    写成 ``/live/hardware`` 会得到 ``/api/v1/live/live/hardware``。
    """
    # 测试缝（late-binding seam）：端点也必须经由 live 的缝在调用时解析
    from server.routes.live import _hardware_payload

    return {"code": 0, "data": await _hardware_payload()}
