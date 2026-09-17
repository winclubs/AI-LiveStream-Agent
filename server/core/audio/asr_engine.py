# -*- coding: utf-8 -*-
"""
ASR (自动语音识别) 与 VAD (语音活动检测) 核心引擎
支持本地 SenseVoiceSmall、FunASR、Faster-Whisper 以及轻量能量 VAD。
解决系统"无 ASR/VAD 语音输入通道"短板，支持真人麦克风语音输入与实时打断。
"""
import io
import os
import time
import logging
import threading
from pathlib import Path
from typing import Optional, Tuple, Union

import numpy as np

logger = logging.getLogger("LiveAgent.ASREngine")

_model_lock = threading.Lock()
_asr_model = None
_asr_backend_type = None


def is_voice_active(
    pcm_bytes: bytes,
    energy_threshold: float = 300.0,
    sample_rate: int = 16000
) -> bool:
    """
    轻量时域能量 VAD 检测：判断音频块中是否包含人声发言

    参数:
      - pcm_bytes: 16-bit 单声道 PCM 字节流
      - energy_threshold: 均方根 RMS 能量阈值
    """
    if not pcm_bytes or len(pcm_bytes) < 320:
        return False
    try:
        audio_array = np.frombuffer(pcm_bytes, dtype=np.int16)
        if len(audio_array) == 0:
            return False
        # 计算 RMS 均方根能量
        rms = np.sqrt(np.mean(audio_array.astype(np.float32) ** 2))
        return bool(rms > energy_threshold)
    except Exception:
        return False


def _load_asr_model():
    """懒加载本地 ASR 模型 (优先 SenseVoice，其次 Faster-Whisper)"""
    global _asr_model, _asr_backend_type
    if _asr_model is not None:
        return _asr_model, _asr_backend_type

    with _model_lock:
        if _asr_model is not None:
            return _asr_model, _asr_backend_type

        # 算力感知：低配显卡 (<= 2GB) 强制使用 CPU 推理，避免抢占显存
        from server.core.hardware.gpu_capability import probe_local_gpu
        local_gpu = probe_local_gpu()
        safe_device = "cpu"
        if not local_gpu.is_low_spec and local_gpu.cuda_available:
            safe_device = "cuda:0"
        else:
            logger.info(f"本地显卡显存仅 {local_gpu.vram_total_gb}GB，ASR 语音模型已安全选用 CPU 推理，确保显存稳定")

        # 1. 尝试加载阿里 SenseVoiceSmall / FunASR
        try:
            from funasr import AutoModel
            device = safe_device
            logger.info(f"正在加载本地 SenseVoiceSmall ASR 模型 (device={device})...")
            model = AutoModel(
                model="iic/SenseVoiceSmall",
                vad_model="fsmn-vad",
                vad_kwargs={"max_single_segment_time": 30000},
                device=device,
                disable_update=True,
                trust_remote_code=True,
            )
            _asr_model = model
            _asr_backend_type = "sensevoice"
            logger.info("SenseVoiceSmall ASR 模型就绪")
            return _asr_model, _asr_backend_type
        except Exception as e:
            logger.info(f"FunASR/SenseVoice 未安装或加载跳过 ({e})")

        # 2. 尝试加载 Faster-Whisper
        try:
            from faster_whisper import WhisperModel
            device = "cuda" if safe_device != "cpu" else "cpu"
            compute_type = "float16" if device == "cuda" else "int8"
            logger.info(f"正在加载 Faster-Whisper ASR 模型 (base, device={device})...")
            model = WhisperModel("base", device=device, compute_type=compute_type)
            _asr_model = model
            _asr_backend_type = "faster_whisper"
            logger.info("Faster-Whisper ASR 模型就绪")
            return _asr_model, _asr_backend_type
        except Exception as e:
            logger.info(f"Faster-Whisper 未安装或加载跳过 ({e})")

        _asr_backend_type = "mock_fallback"
        return None, _asr_backend_type


def transcribe_audio_bytes(audio_bytes: bytes, sample_rate: int = 16000) -> str:
    """
    将输入的音频二进制转录为中文文本

    参数:
      - audio_bytes: 格式支持 WAV / MP3 / 原始 PCM 16kHz
    """
    if not audio_bytes or len(audio_bytes) < 100:
        return ""

    model, backend = _load_asr_model()

    if backend == "sensevoice" and model is not None:
        try:
            # SenseVoice 支持直接传入 wav 内存字节
            res = model.generate(input=audio_bytes, cache={}, language="auto", use_itn=True)
            if res and isinstance(res, list) and len(res) > 0:
                text = res[0].get("text", "").strip()
                # 过滤 SenseVoice 特殊情感标签如 <|zh|><|NEUTRAL|>
                import re
                clean_text = re.sub(r"<\|[^|>]+\|>", "", text).strip()
                return clean_text
        except Exception as e:
            logger.error(f"SenseVoice 推理发生异常: {e}")

    elif backend == "faster_whisper" and model is not None:
        try:
            segments, _ = model.transcribe(io.BytesIO(audio_bytes), language="zh")
            text = "".join([s.text for s in segments]).strip()
            return text
        except Exception as e:
            logger.error(f"Faster-Whisper 推理发生异常: {e}")

    # 兜底：未安装深度学习 ASR 时，安全提示
    logger.info("当前环境未部署本地 ASR 神经网络权重，若需离线语音转写请执行: pip install funasr modelscope")
    return ""
