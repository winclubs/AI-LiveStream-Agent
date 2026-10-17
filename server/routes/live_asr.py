# -*- coding: utf-8 -*-
"""语音识别接入：ASR WebSocket、单段转录、依赖安装与状态查询。

由 server/routes/live.py 拆分而来。
本模块实现侧调用「缝」时一律写在函数体内，
以保住 server.routes.live 上的晚绑定语义（见设计文档 §5.2）。
"""
import asyncio
import json
import logging
import uuid

from fastapi import APIRouter, File, Form, HTTPException, UploadFile, WebSocket, WebSocketDisconnect

from server.core.audio.full_duplex_asr import get_full_duplex_asr_manager

router = APIRouter()
logger = logging.getLogger("LiveAgent.LiveController")


# ============================================================================
# 🎙️ 全双工 ASR 语音识别与麦克风极速打断系统 (阶段四)
# ============================================================================

@router.post("/asr/install-dependency", summary="触发 ASR 转写引擎依赖后台自动安装 (faster-whisper)")
async def install_asr_dependency():
    """
    体检检测到 faster-whisper 缺失时触发：后台通过国内镜像源 pip 安装轻量转写引擎，
    安装完成后首次语音输入自动懒加载。同一时刻仅允许一个安装任务。
    """
    from server.core.audio.asr_dependency_installer import get_asr_dependency_installer
    result = get_asr_dependency_installer().start_install(force=True)
    return {"code": 0 if result["ok"] else 1, **result}


@router.get("/asr/install-status", summary="查询 ASR 依赖自动安装进度与状态")
async def get_asr_install_status():
    """供控制台轮询安装进度 (pending / installing / installed / failed)"""
    from server.core.audio.asr_dependency_installer import get_asr_dependency_installer
    status = get_asr_dependency_installer().get_status()
    return {"code": 0, "data": status}


@router.websocket("/asr/ws")
async def websocket_asr_endpoint(websocket: WebSocket):
    """
    全双工麦克风音频流 WebSocket：
    客户端流式上行 16kHz 16-bit 单声道 PCM 数据，服务端 VAD 检测人声，
    触发瞬间打断与段落转写，并将识别结果作为现场提问注入直播交互。
    """

    # 测试缝（late-binding seam）：必须函数体内解析，
    # patch('server.routes.live.<name>') 才生效（见 test_live_seam_late_binding.py）。
    from server.routes.live import global_live_controller

    from server.core.security.ws_auth import verify_websocket_auth
    if not await verify_websocket_auth(websocket):
        return

    await websocket.accept()
    session_id = f"asr_{uuid.uuid4().hex[:8]}"
    manager = get_full_duplex_asr_manager()

    loop = asyncio.get_running_loop()

    def _notify_interrupt():
        try:
            asyncio.run_coroutine_threadsafe(
                websocket.send_json({"type": "interrupted", "message": "已触发数字人实时闭嘴打断"}),
                loop
            )
        except Exception:
            pass

    def _notify_transcribe(text: str):
        try:
            asyncio.run_coroutine_threadsafe(
                websocket.send_json({"type": "transcription", "text": text}),
                loop
            )
            logger.info(f"全双工 ASR 捕获现场提问: 【{text}】")
            if getattr(global_live_controller, "is_live", False) and text.strip():
                asyncio.run_coroutine_threadsafe(
                    global_live_controller.ingest_event(
                        event_type="chat",
                        user_name="现场语音提问",
                        payload={"content": text.strip(), "text": text.strip()},
                        priority=1,
                        source="asr_mic",
                    ),
                    loop
                )
        except Exception:
            pass

    # P1-5: VAD 打断接入控制器级代际栅栏，与运营手动打断走同一条 _on_barge_in 通路，
    # 推进 _audio_generation 并清空 virtual_audio/媒体驱动/TTS，杜绝打断拖尾竞态。
    def _trigger_barge_in():
        try:
            if getattr(global_live_controller, "is_live", False):
                return global_live_controller._on_barge_in("现场语音打断")
        except Exception:
            logger.exception("VAD 打断控制器栅栏回调失败")
        return None

    session = manager.get_or_create_session(
        session_id=session_id,
        on_interrupt=_notify_interrupt,
        on_transcribe=_notify_transcribe,
        on_barge_in=_trigger_barge_in,
    )

    try:
        while True:
            message = await websocket.receive()
            if "bytes" in message and message["bytes"]:
                pcm_chunk = message["bytes"]
                res = await asyncio.to_thread(session.feed_pcm, pcm_chunk)
                if "speech_start" in res.get("events", []):
                    await websocket.send_json({"type": "speech_start"})
                if "speech_end" in res.get("events", []):
                    await websocket.send_json({"type": "speech_end"})
            elif "text" in message and message["text"]:
                data = json.loads(message["text"])
                if data.get("type") == "ping":
                    await websocket.send_json({"type": "pong"})
    except WebSocketDisconnect:
        logger.info(f"ASR WebSocket 客户端断开: {session_id}")
    except Exception as e:
        logger.warning(f"ASR WebSocket 异常: {e}")
    finally:
        manager.remove_session(session_id)


@router.post("/asr/transcribe", summary="单段语音离线识别转录")
async def transcribe_audio_file(
    file: UploadFile = File(...),
):
    """上传 wav/mp3/pcm 音频文件直接识别为中文文本"""
    from server.core.audio.asr_engine import transcribe_audio_bytes
    content = await file.read()
    if not content:
        raise HTTPException(status_code=400, detail="音频内容为空")
    text = transcribe_audio_bytes(content)
    return {
        "code": 0,
        "message": "转写完成",
        "data": {
            "text": text,
            "file_size": len(content)
        }
    }

# ============================================================================
# 语音插话 (由 live_lifecycle.py 迁入：本端点是 ASR 链路的消费方)
# ============================================================================

@router.post("/voice-interrupt")
async def voice_interrupt_live(
    file: UploadFile = File(...),
    auto_reply: bool = Form(True),
):
    """麦克风实时语音打断与人工插话 (解决 7.4-7 无 ASR/VAD 语音输入通道短板)

    上传麦克风录音切片，自动调用 ASR 引擎识别文本；
    若识别出有效内容，立即触发 P0 级打断当前主播，并作为高优先级事件抢占播报。
    """
    from server.routes.live import global_live_controller

    if not global_live_controller.is_live:
        raise HTTPException(status_code=400, detail="直播尚未开播，禁止语音插播")

    content = await file.read()
    if not content:
        raise HTTPException(status_code=400, detail="上传的语音内容为空")

    from server.core.audio.asr_engine import transcribe_audio_bytes
    text = transcribe_audio_bytes(content)
    if not text.strip():
        text = "收到现场麦克风紧急语音插话"

    res = await global_live_controller.ingest_event(
        event_type="chat",
        user_name="现场麦克风",
        payload={"text": text, "from_voice": True},
        priority=0,  # P0 触发毫秒级强打断
        source="voice_interrupt",
        is_mock=False,
    )
    return {
        "code": 0,
        "message": f"麦克风语音识别成功: '{text}'，已触发 P0 强打断抢占播报",
        "transcribed_text": text,
        "data": res
    }

