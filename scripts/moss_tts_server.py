#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MOSS-TTS-Nano 本地轻量零样本参考推理服务端 (v2.0.0)
基于复旦大学开源 MOSS-TTS-Nano (https://github.com/OpenMOSS/MOSS-TTS-Nano)

特性与设计规范：
1. 模型规格：~100M 超轻量参数，仅约 500MB 显存开销，端侧极速启动；
2. 音质规范：原生支持 48kHz 广播级真人高保真双声道立体声语音合成；
3. 零样本克隆：支持传入 5~30 秒真人语音参考样本 (prompt_wav)，结合目标全新台词进行即时端侧克隆发声；
4. 【铁律】：严禁直接回放原录音，严禁输出蜂鸣噪音，生成的每一秒音频都必须是针对目标台词的新发音。

启动方式：
  python scripts/moss_tts_server.py --host 127.0.0.1 --port 9880
"""
import argparse
import asyncio
import io
import logging
import os
import sys
from pathlib import Path
from typing import AsyncGenerator, Optional

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse, Response, JSONResponse
from pydantic import BaseModel

from server.core.audio.moss_nano.moss_cloner import moss_cloner

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("LiveAgent.MossTTSServer")

app = FastAPI(title="MOSS-TTS-Nano 零样本克隆推理服务端", version="2.0.0")

MOSS_MODEL_DIR = os.getenv("MOSS_TTS_MODEL_DIR", str(PROJECT_ROOT / "data" / "models" / "moss_tts_nano"))
REF_DIR = Path(os.getenv("LIVE_AGENT_DATA_DIR") or str(PROJECT_ROOT / "data")) / "moss_ref"
REF_DIR.mkdir(parents=True, exist_ok=True)


class TTSRequest(BaseModel):
    text: str
    prompt_wav: Optional[str] = None
    speed: Optional[float] = 1.0
    volume: Optional[float] = 1.0
    sample_rate: Optional[int] = 48000
    stream: Optional[bool] = False


class CloneRequest(BaseModel):
    speaker_id: str
    sample_wav_path: str


@app.get("/")
@app.get("/health")
async def health():
    """健康检查与就绪探针"""
    return {
        "status": "ok",
        "engine": "MOSS-TTS-Nano",
        "official_repo": "https://github.com/OpenMOSS/MOSS-TTS-Nano",
        "params": "100M",
        "vram_mb": 500,
        "sample_rate": 48000,
        "zero_shot_ready": True,
        "mode": "end_to_end_zero_shot"
    }


@app.post("/tts")
@app.post("/inference")
async def tts_endpoint(req: TTSRequest):
    """
    MOSS-TTS-Nano 零样本语音合成主接口：
    接收目标台词 (text) 与参考录音 (prompt_wav)，使用参考音色全新合成台词音频！
    """
    clean_text = (req.text or "").strip()
    if not clean_text:
        raise HTTPException(status_code=400, detail="文本不可为空")

    logger.info(f"收到 MOSS-TTS 合成请求: text='{clean_text[:30]}...', prompt_wav={req.prompt_wav}")

    try:
        if req.stream:
            # 真流式：边自回归生成边经 ffmpeg 管道编码为 MP3 帧推送，
            # 首字节延迟从「整段生成完毕」降至「prefill + 首帧解码」，直播/长句显著降卡顿。
            try:
                import numpy as np
                proc = await asyncio.create_subprocess_exec(
                    "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                    "-f", "f32le", "-ar", "48000", "-ac", "2", "-i", "-",
                    "-c:a", "libmp3lame", "-b:a", "128k", "-f", "mp3", "-",
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                )
            except Exception as spawn_err:
                logger.warning(f"ffmpeg 流式管道不可用 ({spawn_err})，回退整段合成后流式传输...")
                out_wav_path = await moss_cloner.clone_and_synthesize(
                    text=clean_text,
                    prompt_audio_path=req.prompt_wav,
                    speed=req.speed or 1.0,
                    volume=req.volume or 1.0
                )
                with open(out_wav_path, "rb") as f:
                    wav_bytes = f.read()

                async def _file_chunk_generator():
                    chunk_size = 4096
                    for i in range(0, len(wav_bytes), chunk_size):
                        yield wav_bytes[i:i + chunk_size]

                return StreamingResponse(_file_chunk_generator(), media_type="audio/wav")

            async def _real_stream_generator():
                async def _feed():
                    try:
                        async for arr in moss_cloner.stream_synthesize_chunks(
                            text=clean_text,
                            prompt_audio_path=req.prompt_wav
                        ):
                            data = np.clip(arr, -1.0, 1.0).astype(np.float32).tobytes()
                            proc.stdin.write(data)
                            await proc.stdin.drain()
                    except Exception as feed_err:
                        logger.error(f"流式生成异常: {feed_err}")
                    finally:
                        try:
                            proc.stdin.close()
                        except Exception:
                            pass

                feeder = asyncio.create_task(_feed())
                try:
                    while True:
                        mp3_chunk = await proc.stdout.read(4096)
                        if not mp3_chunk:
                            break
                        yield mp3_chunk
                finally:
                    feeder.cancel()
                    try:
                        proc.kill()
                    except Exception:
                        pass

            return StreamingResponse(_real_stream_generator(), media_type="audio/mpeg")

        # 非流式：整段合成后返回 WAV (克隆入库/试听文件落盘路径)
        out_wav_path = await moss_cloner.clone_and_synthesize(
            text=clean_text,
            prompt_audio_path=req.prompt_wav,
            speed=req.speed or 1.0,
            volume=req.volume or 1.0
        )

        with open(out_wav_path, "rb") as f:
            wav_bytes = f.read()

        return Response(content=wav_bytes, media_type="audio/wav")

    except Exception as e:
        logger.error(f"MOSS-TTS-Nano 零样本合成异常: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"MOSS-TTS 合成失败: {str(e)}")


@app.post("/clone")
async def clone_endpoint(req: CloneRequest):
    """MOSS-TTS-Nano 零样本参考样本注册与首段台词试听预热"""
    src_path = Path(req.sample_wav_path)
    if not src_path.exists():
        raise HTTPException(status_code=404, detail=f"样本音频文件不存在: {req.sample_wav_path}")

    target_ref = REF_DIR / f"{req.speaker_id}.wav"
    try:
        import shutil
        shutil.copy2(src_path, target_ref)

        # 预先提取声学指纹验证可用性
        profile = moss_cloner.extract_voice_timbre_profile(target_ref)
        logger.info(f"已成功挂载 MOSS-TTS-Nano 参考样本并提取声学画像: {target_ref} -> {profile}")

        return {
            "ok": True,
            "speaker_id": req.speaker_id,
            "ref_path": str(target_ref),
            "profile": profile,
            "message": "参考声学指纹提取成功，已就绪供端侧大模型零样本发声"
        }
    except Exception as e:
        logger.error(f"挂载参考样本失败: {e}")
        raise HTTPException(status_code=500, detail=f"注册失败: {str(e)}")


@app.on_event("startup")
async def warmup_on_startup():
    """启动即后台预热 ONNX 运行时 (8 个 session 约 7s)，消除首次合成的冷启动卡顿，不阻塞服务就绪。"""
    async def _warmup():
        try:
            import time as _time
            _t = _time.perf_counter()
            runtime = moss_cloner._get_runtime()
            await asyncio.to_thread(runtime.warmup)
            logger.info(
                "MOSS-TTS-Nano 运行时预热完成 (%.2fs)，provider=%s，首次合成已就绪",
                _time.perf_counter() - _t,
                getattr(runtime, "execution_provider", "unknown"),
            )
        except Exception as e:
            logger.warning(f"MOSS-TTS-Nano 启动预热失败 (首次合成时将自动重试): {e}")

    asyncio.create_task(_warmup())


def main():
    parser = argparse.ArgumentParser(description="MOSS-TTS-Nano 本地轻量推理服务")
    parser.add_argument("--host", default="127.0.0.1", help="监听地址 (默认 127.0.0.1)")
    parser.add_argument("--port", type=int, default=9880, help="监听端口 (默认 9880)")
    args = parser.parse_args()

    import uvicorn
    logger.info(f"启动 MOSS-TTS-Nano 推理服务于 http://{args.host}:{args.port}")
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
