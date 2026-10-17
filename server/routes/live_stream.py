# -*- coding: utf-8 -*-
"""推流与流媒体分发：内置 RTMP 直推、LLM 预算、WebRTC/WHEP。

由 server/routes/live.py 拆分而来。
本模块不引用任何「缝」，因此无需函数体内 import。
"""
from typing import Optional

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, Field

from server.core.media.webrtc_streamer import get_webrtc_stream_manager

router = APIRouter()


class RtmpStartRequest(BaseModel):
    rtmp_url: str = Field(..., description="RTMP 服务器地址 (例如 rtmp://live-push.bilivideo.com/live-bvc/)")
    stream_key: Optional[str] = Field("", description="直播码 / 推流密钥")
    width: Optional[int] = Field(720, ge=320, le=3840)
    height: Optional[int] = Field(960, ge=240, le=3840)
    fps: Optional[int] = Field(25, ge=15, le=60)
    bitrate_kbps: Optional[int] = Field(2500, ge=500, le=12000)
    audio_sample_rate: Optional[int] = Field(24000, ge=8000, le=96000)


@router.post("/rtmp/start", summary="启动内置 RTMP 直推引擎")
async def start_rtmp_streaming(req: RtmpStartRequest):
    """启动内置 RTMP 直推引擎，直接将数字人音画推向直播平台"""
    from server.core.media.rtmp_streamer import global_rtmp_streamer
    success, msg = global_rtmp_streamer.start(
        rtmp_url=req.rtmp_url,
        stream_key=req.stream_key or "",
        width=req.width or 720,
        height=req.height or 960,
        fps=req.fps or 25,
        bitrate_kbps=req.bitrate_kbps or 2500,
        audio_sample_rate=req.audio_sample_rate or 24000,
    )
    if not success:
        return {"code": 400, "message": msg, "data": global_rtmp_streamer.get_status()}
    return {"code": 0, "message": msg, "data": global_rtmp_streamer.get_status()}


@router.post("/rtmp/stop", summary="停止内置 RTMP 直推引擎")
async def stop_rtmp_streaming():
    """停止内置 RTMP 直推引擎并释放资源"""
    from server.core.media.rtmp_streamer import global_rtmp_streamer
    global_rtmp_streamer.stop()
    return {"code": 0, "message": "推流已安全停止", "data": global_rtmp_streamer.get_status()}


@router.get("/rtmp/status", summary="查询内置 RTMP 直推状态与运行指标")
async def get_rtmp_status():
    """获取内置 RTMP 直推状态、时长与码率统计"""
    from server.core.media.rtmp_streamer import global_rtmp_streamer
    return {"code": 0, "message": "success", "data": global_rtmp_streamer.get_status()}


# ------------------------------------------------------------------
# LLM 会话预算与 Token 熔断监控端点 (P2-1)
# ------------------------------------------------------------------
@router.get("/llm/budget", summary="查询 LLM 会话预算与熔断状态")
async def get_llm_budget_status():
    """获取当前直播会话的 LLM Token 累计消耗与熔断状态"""
    from server.core.llm.budget_manager import get_llm_budget_manager
    return {"code": 0, "data": get_llm_budget_manager().get_status()}


class LLMBudgetRequest(BaseModel):
    max_tokens: Optional[int] = Field(None, ge=1000, le=50000000, description="单场 Token 上限")
    eco_mode: Optional[bool] = Field(None, description="是否开启节能冷场模式 (使用本地商品话术模板)")


@router.post("/llm/budget", summary="配置 LLM 会话预算与节能模式")
async def update_llm_budget(req: LLMBudgetRequest):
    """动态调整当前直播场次的 LLM Token 预算线与冷场策略"""
    from server.core.llm.budget_manager import get_llm_budget_manager
    mgr = get_llm_budget_manager()
    if req.max_tokens is not None:
        mgr.max_tokens = req.max_tokens
        mgr.is_tripped = mgr.used_tokens >= mgr.max_tokens
    if req.eco_mode is not None:
        mgr.eco_mode = req.eco_mode
    return {"code": 0, "message": "预算参数已更新", "data": mgr.get_status()}


# ============================================================================
# ⚡ WebRTC 原生流媒体大屏 (WHEP 标准协商) (阶段四)
# ============================================================================

@router.post("/webrtc/whep", summary="WHEP 协议创建低延迟 WebRTC 媒体流会话")
async def whep_endpoint(request: Request):
    """
    符合 IETF WHEP (WebRTC HTTP Egress Protocol) 标准规范：
    客户端 POST 发送 SDP Offer，服务端返回 201 Created 与 SDP Answer。
    同时兼容 application/json 格式。
    """
    content_type = request.headers.get("content-type", "")
    sdp_offer = ""
    if "application/json" in content_type:
        body = await request.json()
        sdp_offer = body.get("sdp", "")
    else:
        raw_body = await request.body()
        sdp_offer = raw_body.decode("utf-8", errors="ignore")

    if not sdp_offer.strip():
        raise HTTPException(status_code=400, detail="SDP Offer 不能为空")

    from server.core.media.webrtc_streamer import get_webrtc_stream_manager, WEBRTC_AVAILABLE
    if not WEBRTC_AVAILABLE:
        raise HTTPException(
            status_code=503,
            detail="WebRTC 媒体服务暂不可用：服务器未安装 aiortc/av 多媒体依赖，请降级使用 MJPEG 协议或安装依赖 (pip install aiortc av)",
        )

    stream_mgr = get_webrtc_stream_manager()
    session_id, sdp_answer = await stream_mgr.handle_whep_offer(sdp_offer)

    headers = {
        "Location": f"/api/v1/live/webrtc/whep/{session_id}",
        "Content-Type": "application/sdp",
    }
    accept = request.headers.get("accept", "")
    if "application/json" in accept or "application/json" in content_type:
        return {
            "code": 0,
            "session_id": session_id,
            "sdp": sdp_answer,
            "type": "answer",
        }

    return Response(
        content=sdp_answer,
        status_code=201,
        headers=headers,
        media_type="application/sdp",
    )


@router.delete("/webrtc/whep/{session_id}", summary="断开释放 WebRTC 会话")
async def close_whep_session(session_id: str):
    """释放指定的 WHEP WebRTC PeerConnection 连接"""
    stream_mgr = get_webrtc_stream_manager()
    await stream_mgr.close_session(session_id)
    return {"code": 0, "message": f"会话 {session_id} 已释放"}


@router.get("/webrtc/status", summary="查询 WebRTC 流媒体服务状态")
async def get_webrtc_status():
    """获取当前 WebRTC 在线拉流客户端数与帧率"""
    stream_mgr = get_webrtc_stream_manager()
    return {
        "code": 0,
        "data": stream_mgr.get_status()
    }
