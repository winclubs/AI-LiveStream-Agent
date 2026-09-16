# -*- coding: utf-8 -*-
"""
AI-LiveStream-Agent 端云分离参考节点。

协议：
  客户端->云 : {"event":"auth","token":"...","protocol_version":2}
  云->客户端 : {"event":"auth_ok","protocol_version":2} 或 error
  客户端->云 : {"event":"tts_request","request_id":"...","text":"...","speed":1.0}
  云->客户端 : v2 request_id 音频 envelope、带 request_id/pts_ms 的 video_frame、audio_end
  客户端->云 : {"event":"cancel","request_id":"...","reason":"..."}

协议版本由认证握手协商；旧客户端未声明 v2 时继续接收原始二进制音频。
服务端在启动新请求前有界等待旧任务取消，避免旧音频混入新请求。
当前参考实现使用 Edge-TTS 与程序化渲染，不代表神经 TTS/数字人 GPU 推理能力。
"""
import argparse
import asyncio
import base64
import json
import logging
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

try:
    import websockets
except ImportError:
    print("[!] 缺少 websockets 依赖，请执行: pip install websockets")
    sys.exit(1)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("CloudNode")

VOICE = "zh-CN-XiaoxiaoNeural"
AUTH_TOKEN = ""
RENDER_FRAMES = True
WIDTH, HEIGHT, FPS = 720, 960, 25
PROTOCOL_VERSION = 2
AUDIO_MAGIC = b"RGA2"
MAX_REQUEST_ID_BYTES = 64
CANCEL_TIMEOUT = 5.0

try:
    from server.core.media.procedural_renderer import (
        generate_default_portrait,
        synth_frame,
        encode_jpeg,
        audio_rms_to_mouth,
        CV_AVAILABLE,
    )
    _RENDER_OK = CV_AVAILABLE
except Exception as exc:  # pragma: no cover - 取决于可选媒体依赖
    _RENDER_OK = False
    logger.warning("云端渲染器不可用，将仅回传音频: %s", exc)

_BASE_PORTRAIT = generate_default_portrait(WIDTH, HEIGHT) if _RENDER_OK else None


def _encode_audio_envelope(request_id: str, audio: bytes) -> bytes:
    request_bytes = request_id.encode("utf-8")
    if not request_bytes or len(request_bytes) > MAX_REQUEST_ID_BYTES:
        raise ValueError("request_id 长度非法")
    return AUDIO_MAGIC + bytes([len(request_bytes)]) + request_bytes + audio


async def _iter_edge_stream(communicate):
    """确保客户端取消或提前退出时同步关闭 Edge-TTS 的 aiohttp 资源。"""
    stream = communicate.stream()
    try:
        async for chunk in stream:
            yield chunk
    finally:
        try:
            await stream.aclose()
        except Exception:
            logger.debug("关闭 Edge-TTS 云节点流失败", exc_info=True)


async def _synthesize_and_send(
    ws,
    text: str,
    speed: float,
    request_id: str,
    protocol_version: int,
):
    rate = f"{int(round((float(speed or 1.0) - 1.0) * 100)):+d}%"
    try:
        import edge_tts

        communicate = edge_tts.Communicate(text, VOICE, rate=rate)
        total = 0
        frames = 0
        t = 0.0
        async for chunk in _iter_edge_stream(communicate):
            if chunk["type"] != "audio":
                continue
            audio = chunk["data"]
            total += len(audio)
            if RENDER_FRAMES and _RENDER_OK and _BASE_PORTRAIT is not None:
                mouth = audio_rms_to_mouth(audio)
                for _ in range(2):
                    t += 1.0 / FPS
                    frame = synth_frame(_BASE_PORTRAIT, WIDTH, HEIGHT, t, mouth)
                    await ws.send(json.dumps({
                        "event": "video_frame",
                        "request_id": request_id,
                        "pts_ms": int(t * 1000),
                        "data": base64.b64encode(encode_jpeg(frame, 78)).decode("ascii"),
                    }))
                    frames += 1
            payload = _encode_audio_envelope(request_id, audio) if protocol_version >= 2 else audio
            await ws.send(payload)
        await ws.send(json.dumps({
            "event": "audio_end",
            "request_id": request_id,
            "bytes": total,
            "frames": frames,
        }))
        logger.info("请求 %s 已回传音频 %s bytes / 视频帧 %s: %s", request_id, total, frames, text[:30])
    except asyncio.CancelledError:
        try:
            await ws.send(json.dumps({
                "event": "error",
                "request_id": request_id,
                "message": "cancelled",
            }))
        except Exception:
            pass
        raise
    except Exception as exc:
        logger.warning("云端合成异常 [%s]: %s", request_id, exc)
        try:
            await ws.send(json.dumps({
                "event": "error",
                "request_id": request_id,
                "message": str(exc),
            }))
        except Exception:
            pass


async def _cancel_task(task) -> bool:
    if task is None:
        return True
    if task.done():
        await asyncio.gather(task, return_exceptions=True)
        return True
    task.cancel()
    done, pending = await asyncio.wait({task}, timeout=CANCEL_TIMEOUT)
    if pending:
        logger.error("旧合成任务在 %.1f 秒内未响应取消，将关闭连接隔离", CANCEL_TIMEOUT)
        return False
    await asyncio.gather(*done, return_exceptions=True)
    return True


async def handler(ws):
    authed = AUTH_TOKEN == ""
    protocol_version = 1
    current_task = None
    current_request_id = None
    peer = getattr(ws, "remote_address", "unknown")
    logger.info("新连接: %s", peer)
    try:
        async for message in ws:
            if isinstance(message, (bytes, bytearray)):
                continue
            try:
                msg = json.loads(message)
            except Exception:
                continue
            event = msg.get("event")
            if event == "auth":
                if AUTH_TOKEN and msg.get("token") != AUTH_TOKEN:
                    await ws.send(json.dumps({"event": "error", "message": "auth failed"}))
                    await ws.close()
                    return
                authed = True
                try:
                    requested_version = int(msg.get("protocol_version") or 1)
                except (TypeError, ValueError):
                    requested_version = 1
                protocol_version = min(PROTOCOL_VERSION, max(1, requested_version))
                await ws.send(json.dumps({
                    "event": "auth_ok",
                    "protocol_version": protocol_version,
                }))
            elif event == "tts_request":
                request_id = str(msg.get("request_id") or "legacy")
                if not authed:
                    await ws.send(json.dumps({
                        "event": "error",
                        "request_id": request_id,
                        "message": "not authenticated",
                    }))
                    continue
                # 旧任务不响应取消时关闭连接，绝不与新任务共用同一传输通道。
                if not await _cancel_task(current_task):
                    await ws.close()
                    return
                current_request_id = request_id
                current_task = asyncio.create_task(
                    _synthesize_and_send(
                        ws,
                        msg.get("text", ""),
                        msg.get("speed", 1.0),
                        request_id,
                        protocol_version,
                    )
                )
            elif event == "cancel":
                request_id = msg.get("request_id")
                if not request_id or request_id == current_request_id:
                    if not await _cancel_task(current_task):
                        await ws.close()
                        return
                    current_task = None
                    current_request_id = None
                    logger.info("请求 %s 已取消", request_id or "current")
    except websockets.ConnectionClosed:
        pass
    finally:
        await _cancel_task(current_task)
        logger.info("连接断开: %s", peer)


async def main():
    global VOICE, AUTH_TOKEN
    parser = argparse.ArgumentParser(description="AI-LiveStream-Agent 云端参考渲染节点服务")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8888)
    parser.add_argument("--token", default="", help="鉴权 Token，留空则不校验")
    parser.add_argument("--voice", default=VOICE)
    args = parser.parse_args()
    VOICE = args.voice
    AUTH_TOKEN = args.token

    logger.info("=" * 60)
    logger.info("  AI-LiveStream-Agent 云端参考节点已启动")
    logger.info("  WebSocket 网关: ws://%s:%s/ws/render", args.host, args.port)
    logger.info("  鉴权 Token    : %s", "(未设置，开放访问)" if not AUTH_TOKEN else "(已设置)")
    logger.info("=" * 60)

    async with websockets.serve(handler, args.host, args.port, max_size=2 ** 23):
        await asyncio.Future()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n[云节点] 已停止")
