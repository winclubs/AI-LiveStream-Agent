# -*- coding: utf-8 -*-
"""画面叠加层与媒体诊断：优惠券/逼单画层、媒体组件健康快照。

由 server/routes/live.py 拆分而来。
本模块实现侧调用「缝」时一律写在函数体内，
以保住 server.routes.live 上的晚绑定语义（见设计文档 §5.2）。
"""
import logging
from typing import Optional

from fastapi import APIRouter
from pydantic import BaseModel

from server.adapters.media.media_router import global_media_router
from server.core.media.av_sync import global_av_sync
from server.core.vision.capture import global_vision

router = APIRouter()
logger = logging.getLogger("LiveAgent.LiveController")


def _safe_media_status(component: str, getter, default: Optional[dict] = None) -> dict:
    """单个媒体组件故障不能让诊断端点整体不可用。"""
    try:
        value = getter()
        if not isinstance(value, dict):
            raise TypeError("status getter 未返回 dict")
        return value
    except Exception as exc:
        logger.warning("读取 %s 状态失败: %s", component, exc)
        return {
            **(default or {}),
            "available": False,
            "component_up": False,
            "stale": True,
            "error": str(exc),
        }


@router.get("/media/status")
async def get_media_status():
    """获取媒体状态；任一子组件故障时保留其余诊断信息。"""

    from server.routes.live import global_live_controller, global_virtual_audio
    from server.core.media.audio_pipeline import global_audio_frame_pipeline
    from server.core.media.incremental_audio import global_incremental_audio_pipeline

    data = _safe_media_status(
        "media_router",
        global_media_router.get_preview_status,
        {"driver_type": "unknown", "is_running": False},
    )
    data["audio_pipeline"] = _safe_media_status(
        "audio_pipeline", global_audio_frame_pipeline.get_status
    )
    data["incremental_audio"] = _safe_media_status(
        "incremental_audio", global_incremental_audio_pipeline.get_status
    )
    data["virtual_audio"] = _safe_media_status(
        "virtual_audio", global_virtual_audio.get_status
    )
    vision = _safe_media_status("vision", global_vision.get_status)
    vision["active_frame"] = bool(global_live_controller.live_context.get("vision_image_b64"))
    data["vision"] = vision
    data["av_sync"] = _safe_media_status("av_sync", global_av_sync.get_status)
    from server.core.monitoring.system_resource_watchdog import global_resource_watchdog
    data["resource_watchdog"] = global_resource_watchdog.get_status()
    return {
        "code": 0,
        "data": data,
    }


class OverlayCouponRequest(BaseModel):
    sku: str = ""
    title: str = "直播间专属优惠券"
    amount: str = "立减 30 元"
    desc: str = "点击左下角领券立减"
    seconds: int = 20


@router.post("/overlay/coupon")
async def trigger_overlay_coupon(req: OverlayCouponRequest):
    """一键触发画中画优惠券画层 (方案 4.2)"""
    from server.core.tools.tool_bus import trigger_pip_coupon
    res = await trigger_pip_coupon(
        sku=req.sku,
        title=req.title,
        amount=req.amount,
        desc=req.desc,
        seconds=req.seconds,
    )
    return {"code": 0, "message": "画中画优惠券已触发", "data": res}


class OverlayUrgencyRequest(BaseModel):
    sku: str = ""
    title: str = "🔥 限量抢购 倒计时"
    desc: str = "库存告急，先到先得！"
    seconds: int = 60


@router.post("/overlay/urgency")
async def trigger_overlay_urgency(req: OverlayUrgencyRequest):
    """一键触发逼单倒计时画层 (方案 4.2)"""
    from server.core.tools.tool_bus import trigger_urgency_countdown
    res = await trigger_urgency_countdown(
        sku=req.sku,
        title=req.title,
        seconds=req.seconds,
        desc=req.desc,
    )
    return {"code": 0, "message": "逼单倒计时已触发", "data": res}
