# -*- coding: utf-8 -*-
"""神经渲染 sidecar v3 参考节点。

当前 backend 是确定性的程序化协议夹具，只用于联调 handshake、PCM frame、credit、
取消和 sample PTS；它不会加载或冒充 Wav2Lip/MuseTalk 神经模型。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from server.core.media.sidecar_protocol import (  # noqa: E402
    KIND_AUDIO,
    PROTOCOL_VERSION,
    SidecarVideoFrame,
    decode_envelope,
    encode_video_frame,
)

try:
    import websockets
except ImportError:  # pragma: no cover
    websockets = None

try:
    from server.core.media.procedural_renderer import (  # noqa: E402
        CV_AVAILABLE,
        audio_rms_to_mouth,
        encode_jpeg,
        generate_default_portrait,
        synth_frame,
    )
except Exception:  # pragma: no cover - 可选媒体依赖
    CV_AVAILABLE = False

logger = logging.getLogger("LiveAgent.SidecarFixture")
AUTH_TOKEN = ""
WIDTH, HEIGHT, FPS = 720, 960, 25
INITIAL_CREDIT = 16
_BASE_PORTRAIT = generate_default_portrait(WIDTH, HEIGHT) if CV_AVAILABLE else None


class RenderTransaction:
    def __init__(self, message: dict) -> None:
        self.request_id = str(message.get("request_id") or "")
        self.audio_id = str(message.get("audio_id") or "")
        self.audio_generation = int(message.get("audio_generation") or 0)
        self.session_generation = int(message.get("session_generation") or 0)
        audio_format = message.get("format") or {}
        self.sample_rate = int(audio_format.get("sample_rate") or 0)
        self.channels = int(audio_format.get("channels") or 0)
        self.next_sequence = 0
        self.next_pts_samples = 0
        self.frames_rendered = 0
        self.cancelled = False
        if not self.request_id or not self.audio_id or self.sample_rate <= 0 or self.channels <= 0:
            raise ValueError("render_open 字段不完整")

    def accept_audio(self, metadata: dict, payload: bytes) -> SidecarVideoFrame:
        if metadata.get("request_id") != self.request_id or metadata.get("audio_id") != self.audio_id:
            raise ValueError("音频帧事务身份不匹配")
        sequence = int(metadata.get("sequence", -1))
        pts_samples = int(metadata.get("pts_samples", -1))
        if sequence != self.next_sequence or pts_samples != self.next_pts_samples:
            raise ValueError("音频帧 sequence/PTS 不连续")
        if int(metadata.get("audio_generation", -1)) != self.audio_generation:
            raise ValueError("audio_generation 不匹配")
        if int(metadata.get("session_generation", -1)) != self.session_generation:
            raise ValueError("session_generation 不匹配")
        bytes_per_sample = self.channels * 2
        if not payload or len(payload) % bytes_per_sample:
            raise ValueError("PCM payload 未按样本边界对齐")
        duration_samples = len(payload) // bytes_per_sample
        self.next_sequence += 1
        self.next_pts_samples += duration_samples

        mouth = audio_rms_to_mouth(payload)
        frame = synth_frame(
            _BASE_PORTRAIT,
            WIDTH,
            HEIGHT,
            pts_samples / float(self.sample_rate),
            mouth,
        )
        jpeg = encode_jpeg(frame, 78)
        video = SidecarVideoFrame(
            request_id=self.request_id,
            audio_id=self.audio_id,
            sequence=self.frames_rendered,
            pts_samples=pts_samples,
            audio_generation=self.audio_generation,
            session_generation=self.session_generation,
            jpeg=jpeg,
        )
        self.frames_rendered += 1
        return video


async def handler(ws) -> None:
    authed = AUTH_TOKEN == ""
    transaction: RenderTransaction | None = None
    try:
        async for raw in ws:
            if isinstance(raw, bytes):
                if not authed or transaction is None or transaction.cancelled:
                    continue
                try:
                    envelope = decode_envelope(raw)
                    if envelope.kind != KIND_AUDIO:
                        raise ValueError("只接受音频 envelope")
                    video = transaction.accept_audio(dict(envelope.metadata), envelope.payload)
                    await ws.send(encode_video_frame(video))
                    await ws.send(json.dumps({
                        "event": "render_credit",
                        "request_id": transaction.request_id,
                        "credit": 1,
                    }))
                except Exception as exc:
                    await ws.send(json.dumps({
                        "event": "error",
                        "request_id": transaction.request_id,
                        "message": str(exc),
                    }))
                continue

            try:
                message = json.loads(raw)
            except json.JSONDecodeError:
                continue
            event = message.get("event")
            if event == "auth":
                if AUTH_TOKEN and message.get("token") != AUTH_TOKEN:
                    await ws.send(json.dumps({"event": "error", "message": "auth failed"}))
                    await ws.close()
                    return
                versions = message.get("supported_versions") or [message.get("protocol_version")]
                if PROTOCOL_VERSION not in versions:
                    await ws.send(json.dumps({"event": "error", "message": "v3 required"}))
                    continue
                authed = True
                await ws.send(json.dumps({
                    "event": "auth_ok",
                    "selected_version": PROTOCOL_VERSION,
                    "node_version": "procedural-fixture-v1",
                    "capabilities": {
                        "renderer_available": bool(CV_AVAILABLE),
                        "neural_lipsync": False,
                        "input_codecs": ["pcm_s16le"],
                        "sample_rates": [16000, 24000, 48000],
                        "channels": [1],
                        "fps": FPS,
                        "supports_cancel_ack": True,
                        "supports_credit": True,
                        "render_backends": [{
                            "id": "procedural-fixture",
                            "model_version": None,
                            "available": bool(CV_AVAILABLE),
                            "neural": False,
                        }],
                    },
                }))
                continue
            if not authed:
                await ws.send(json.dumps({"event": "error", "message": "not authenticated"}))
                continue
            if event == "render_open":
                try:
                    if not CV_AVAILABLE or _BASE_PORTRAIT is None:
                        raise RuntimeError("程序化协议夹具缺少 OpenCV/numpy")
                    transaction = RenderTransaction(message)
                    await ws.send(json.dumps({
                        "event": "render_accepted",
                        "request_id": transaction.request_id,
                        "initial_credit": INITIAL_CREDIT,
                        "backend_id": "procedural-fixture",
                    }))
                except Exception as exc:
                    await ws.send(json.dumps({
                        "event": "error",
                        "request_id": message.get("request_id"),
                        "message": str(exc),
                    }))
                continue
            if event == "render_finish" and transaction is not None:
                request_id = transaction.request_id
                await ws.send(json.dumps({
                    "event": "render_complete",
                    "request_id": request_id,
                    "audio_id": transaction.audio_id,
                    "rendered_frames": transaction.frames_rendered,
                    "last_pts_samples": transaction.next_pts_samples,
                }))
                transaction = None
                continue
            if event == "render_cancel":
                request_id = str(message.get("request_id") or "")
                if transaction is not None and (
                    not request_id or request_id == transaction.request_id
                ):
                    transaction.cancelled = True
                    transaction = None
                await ws.send(json.dumps({
                    "event": "render_cancelled",
                    "request_id": request_id,
                }))
    except Exception:
        logger.debug("sidecar 客户端连接结束", exc_info=True)


async def main() -> None:
    global AUTH_TOKEN
    parser = argparse.ArgumentParser(description="AI-LiveStream-Agent sidecar v3 程序化协议夹具")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8890)
    parser.add_argument("--token", default="")
    args = parser.parse_args()
    AUTH_TOKEN = args.token
    if websockets is None:
        raise RuntimeError("缺少 websockets 依赖")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    logger.info("sidecar v3 程序化协议夹具: ws://%s:%s/ws/render-v3", args.host, args.port)
    async with websockets.serve(handler, args.host, args.port, max_size=16 * 1024 * 1024):
        await asyncio.Future()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
