#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
CosyVoice 兼容参考服务端 (v1.8.0)
让本项目的声音克隆客户端 (server/adapters/media/cosyvoice_driver.py) 开箱可验证、可联调。

API 契约 (与 CosyVoiceMediaDriver 完全对齐)：
  GET  /                    健康检查 -> {"status": "ok", "backend": ...}
  POST /clone_speaker       {"sample_wav": "...", "speaker_id": "..."} -> {"ok": true, ...}
                            注册参考音色样本 (复制到 data/cosyvoice_ref/ 供推理引用)
  POST /inference_stream    {"text","prompt_wav","speed","volume","stream"} -> 音频字节流

后端可插拔 (能力边界，诚实声明)：
  - 默认 backend="edge"：使用微软 Edge-TTS 实时合成真实语音 (零权重、免费)，
    完整打通 健康检查 -> 克隆注册 -> 流式合成 链路，但音色不受参考样本影响。
  - 设置 COSYVOICE_BACKEND=real 并在 scripts/cosyvoice_real_backend.py 实现
    `async def synthesize_audio(text, prompt_wav, speed, volume)` 即接入真实
    CosyVoice2 零样本克隆推理 (需自行部署权重，见规划 §4.1/§4.3)。

启动：python scripts/cosyvoice_server.py --host 127.0.0.1 --port 9233
"""
import argparse
import asyncio
import importlib.util
import os
import shutil
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
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

app = FastAPI(title="CosyVoice 兼容参考服务端", version="1.0.0")

BACKEND = os.getenv("COSYVOICE_BACKEND", "edge")
REF_DIR = Path(os.getenv("LIVE_AGENT_DATA_DIR") or str(PROJECT_ROOT / "data")) / "cosyvoice_ref"
REF_DIR.mkdir(parents=True, exist_ok=True)

# speaker_id -> 参考样本路径 (进程内注册表)
_SPEAKERS = {}


class CloneRequest(BaseModel):
    sample_wav: str
    speaker_id: str


class InferenceRequest(BaseModel):
    text: str
    prompt_wav: str = ""
    speed: float = 1.0
    volume: float = 1.0
    stream: bool = True


@app.get("/")
async def health():
    return {"status": "ok", "backend": BACKEND, "speakers": len(_SPEAKERS)}


@app.post("/clone_speaker")
async def clone_speaker(req: CloneRequest):
    src = Path(req.sample_wav)
    if not req.speaker_id.strip():
        raise HTTPException(status_code=400, detail="speaker_id 不可为空")
    if not src.exists():
        raise HTTPException(status_code=400, detail=f"sample_wav 不存在: {req.sample_wav}")
    dest = REF_DIR / f"{req.speaker_id.strip()}{src.suffix or '.wav'}"
    shutil.copyfile(str(src), str(dest))
    _SPEAKERS[req.speaker_id.strip()] = str(dest)
    return {"ok": True, "speaker_id": req.speaker_id.strip(), "prompt_wav": str(dest)}


async def _edge_synth(text: str, speed: float, volume: float) -> AsyncGenerator[bytes, None]:
    """默认后端：Edge-TTS 实时合成真实语音 (零权重，音色不受参考样本影响)"""
    import edge_tts
    rate = f"{int(round((float(speed or 1.0) - 1.0) * 100)):+d}%"
    vol = f"{int(round((float(volume or 1.0) - 1.0) * 100)):+d}%"
    communicate = edge_tts.Communicate(text, "zh-CN-XiaoxiaoNeural", rate=rate, volume=vol)
    async for chunk in communicate.stream():
        if chunk["type"] == "audio":
            yield chunk["data"]


async def synthesize_audio(text: str, prompt_wav: str, speed: float, volume: float) -> AsyncGenerator[bytes, None]:
    """后端调度：默认 edge；COSYVOICE_BACKEND=real 时加载真实克隆推理实现"""
    if BACKEND == "real":
        real_path = PROJECT_ROOT / "scripts" / "cosyvoice_real_backend.py"
        spec = importlib.util.spec_from_file_location("cosyvoice_real_backend", str(real_path))
        if spec is None or not real_path.exists():
            raise HTTPException(
                status_code=500,
                detail="COSYVOICE_BACKEND=real 但未找到 scripts/cosyvoice_real_backend.py，"
                       "请实现 async def synthesize_audio(text, prompt_wav, speed, volume) 接入真实 CosyVoice2 推理",
            )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        async for chunk in module.synthesize_audio(text, prompt_wav, speed, volume):
            yield chunk
        return
    async for chunk in _edge_synth(text, speed, volume):
        yield chunk


@app.post("/inference_stream")
async def inference_stream(req: InferenceRequest):
    if not (req.text or "").strip():
        raise HTTPException(status_code=400, detail="text 不可为空")
    return StreamingResponse(
        synthesize_audio(req.text, req.prompt_wav, req.speed, req.volume),
        media_type="application/octet-stream",
    )


def main():
    parser = argparse.ArgumentParser(description="CosyVoice 兼容参考服务端")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=9233)
    args = parser.parse_args()

    import uvicorn
    print(f"[CosyVoice 参考服务端] 启动于 http://{args.host}:{args.port} (backend={BACKEND})")
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
