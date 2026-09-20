#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
从国内镜像下载 MOSS-TTS-Nano 官方 ONNX 权重文件
"""
import os
import sys
from pathlib import Path

# 强制使用国内镜像
os.environ["HF_ENDPOINT"] = "https://hf-mirror.com"

from huggingface_hub import snapshot_download

PROJECT_ROOT = Path(__file__).resolve().parent.parent
MODEL_DIR = PROJECT_ROOT / "data" / "models" / "moss_tts_nano"
MODEL_DIR.mkdir(parents=True, exist_ok=True)

TTS_DIR = MODEL_DIR / "MOSS-TTS-Nano-100M-ONNX"
CODEC_DIR = MODEL_DIR / "MOSS-Audio-Tokenizer-Nano-ONNX"

print(f"[1/2] 开始下载 MOSS-Audio-Tokenizer-Nano-ONNX 至 {CODEC_DIR} ...")
snapshot_download(
    repo_id="OpenMOSS-Team/MOSS-Audio-Tokenizer-Nano-ONNX",
    local_dir=str(CODEC_DIR),
    allow_patterns=["*.onnx", "*.data", "*.json"],
)
print("[1/2] Codec 模型下载完成！")

print(f"[2/2] 开始下载 MOSS-TTS-Nano-100M-ONNX 至 {TTS_DIR} ...")
snapshot_download(
    repo_id="OpenMOSS-Team/MOSS-TTS-Nano-100M-ONNX",
    local_dir=str(TTS_DIR),
    allow_patterns=["*.onnx", "*.data", "*.json", "tokenizer.model"],
)
print("[2/2] TTS 模型下载完成！全部 MOSS 官方模型下载就绪！")
