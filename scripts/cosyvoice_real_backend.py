# -*- coding: utf-8 -*-
"""
CosyVoice 真实本地克隆推理后端 (Real Zero-Shot Clone Backend)
支持加载本地开源 CosyVoice / CosyVoice2 模型执行声音克隆流式合成。
当本地依赖未就绪或显存不足时，提供清晰指引与优雅降级策略。
"""
import os
import sys
import logging
from pathlib import Path
from typing import AsyncGenerator, Optional

logger = logging.getLogger("CosyVoice.RealBackend")

_cosyvoice_model = None
_model_initialized = False


def _get_cosyvoice_model():
    """按需懒加载本地 CosyVoice 官方模型 (单例模式)"""
    global _cosyvoice_model, _model_initialized
    if _model_initialized:
        return _cosyvoice_model

    _model_initialized = True
    model_dir = os.getenv("COSYVOICE_MODEL_DIR", "pretrained_models/CosyVoice-300M")

    try:
        from cosyvoice.cli.cosyvoice import CosyVoice
        if Path(model_dir).exists():
            logger.info(f"正在加载本地 CosyVoice 模型: {model_dir}")
            _cosyvoice_model = CosyVoice(model_dir)
            logger.info("CosyVoice 本地克隆推理模型加载就绪")
        else:
            logger.warning(
                f"CosyVoice 模型权重目录不存在: {model_dir}。"
                "请从 ModelScope 下载 CosyVoice-300M 或 CosyVoice2-0.5B 权重，"
                "或通过环境变量 COSYVOICE_MODEL_DIR 指定路径。"
            )
    except ImportError:
        logger.warning(
            "本地未检测到 cosyvoice 运行库。"
            "如需使用真正的本地离线声音克隆，请执行: pip install cosyvoice torchaudio"
        )
    except Exception as e:
        logger.error(f"初始化本地 CosyVoice 模型发生异常: {e}")

    return _cosyvoice_model


async def synthesize_audio(
    text: str,
    prompt_wav: str,
    speed: float = 1.0,
    volume: float = 1.0
) -> AsyncGenerator[bytes, None]:
    """
    流式音频合成生成器

    参数:
      - text: 待合成的目标文本
      - prompt_wav: 用户上传的参考克隆音频文件路径 (16kHz WAV/MP3)
      - speed: 语速比例 (0.5 ~ 2.0)
      - volume: 音量增益比例 (0.5 ~ 2.0)

    输出:
      - 流式音频二进制块 (PCM / WAV / MP3 bytes)
    """
    model = _get_cosyvoice_model()

    # 1. 优先使用本地 CosyVoice 真实零样本声音复刻模型
    if model is not None and prompt_wav and Path(prompt_wav).exists():
        try:
            import torch
            logger.info(f"使用本地 CosyVoice 执行零样本声音复刻: 文本='{text[:20]}...', 样本='{prompt_wav}'")
            # 官方 CosyVoice 推理接口
            output = model.inference_zero_shot(text, "", prompt_wav, stream=True, speed=float(speed or 1.0))
            for chunk in output:
                tts_audio = chunk["tts_speech"]  # 1D Tensor 22050Hz 或 24000Hz
                audio_bytes = (tts_audio * 32767).to(torch.int16).cpu().numpy().tobytes()
                yield audio_bytes
            return
        except Exception as e:
            logger.error(f"CosyVoice 真实克隆推理过程发生异常: {e}，将自动切换至高可用兜底流")

    # 2. 优雅降级保护：当无本地权重或模型异常时，使用 Edge-TTS 高保真流式输出兜底
    logger.info("CosyVoice 真实后端未满足本地模型加载条件，自动采用极速 Edge-TTS 进行高保真语音兜底输出")
    import edge_tts
    import io

    rate_pct = f"{int(round((float(speed or 1.0) - 1.0) * 100)):+d}%"
    vol_pct = f"{int(round((float(volume or 1.0) - 1.0) * 100)):+d}%"
    communicate = edge_tts.Communicate(text, "zh-CN-XiaoxiaoNeural", rate=rate_pct, volume=vol_pct)

    async for chunk in communicate.stream():
        if chunk.get("type") == "audio":
            yield chunk.get("data", b"")
