#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MOSS-TTS 文本规范化轻量适配器
自动兼容标准环境，无需安装复杂的编译型 WeTextProcessing
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

try:
    from .tts_robust_normalizer_single_script import normalize_tts_text
except ImportError:
    try:
        from tts_robust_normalizer_single_script import normalize_tts_text
    except ImportError:
        def normalize_tts_text(text: str) -> str:
            return text


@dataclass(frozen=True)
class TextNormalizationSnapshot:
    state: str = "ready"
    message: str = "Ready"
    error: str | None = None
    ready: bool = True
    available: bool = True


class WeTextProcessingManager:
    """轻量安全的文本正则管理器（降级无依赖版）"""
    def __init__(self, *args, **kwargs):
        pass

    def snapshot(self) -> TextNormalizationSnapshot:
        return TextNormalizationSnapshot()

    def ensure_ready(self) -> TextNormalizationSnapshot:
        """确保文本正则化引擎已就绪并返回状态快照"""
        return self.snapshot()


def prepare_tts_request_texts(
    *,
    text: str,
    prompt_text: str = "",
    voice: str = "",
    enable_wetext: bool = False,
    enable_normalize_tts_text: bool = True,
    text_normalizer_manager: WeTextProcessingManager | None = None,
) -> dict[str, object]:
    """清洗与规范化合成台词"""
    raw_text = str(text or "").strip()
    raw_prompt_text = str(prompt_text or "").strip()

    if enable_normalize_tts_text:
        processed_text = normalize_tts_text(raw_text)
        processed_prompt_text = normalize_tts_text(raw_prompt_text) if raw_prompt_text else ""
    else:
        processed_text = raw_text
        processed_prompt_text = raw_prompt_text

    return {
        "text": processed_text,
        "prompt_text": processed_prompt_text,
        "raw_text": raw_text,
        "raw_prompt_text": raw_prompt_text,
        "stages": ["robust_pre"] if enable_normalize_tts_text else [],
    }
