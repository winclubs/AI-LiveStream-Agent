#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AI-LiveStream-Agent 模型权重一键下载器
支持从 ModelScope(魔搭，国内推荐) 或 HuggingFace 自动下载：
- MuseTalk 2.0 唇形驱动权重 (musetalk.json, unet.pth, dwpose, whisper)
- CosyVoice2-0.5B 声音克隆模型
- RetinaFace 人脸检测与 468 关键点检测模型
- Whisper-tiny 音频特征提取模型
- BGE-small-zh-v1.5 本地向量检索模型
"""

import os
import sys
import argparse
from pathlib import Path

# 适配终端编码
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

PROJECT_ROOT = Path(__file__).resolve().parent.parent
WEIGHTS_DIR = PROJECT_ROOT / "weights"

MODELS_CONFIG = {
    "musetalk": {
        "name": "MuseTalk 2.0 实时数字人唇形驱动模型",
        "target_dir": WEIGHTS_DIR / "musetalk",
        "sources": {
            "modelscope": "damo/MuseTalk",
            "huggingface": "TMElyralab/MuseTalk"
        }
    },
    "cosyvoice": {
        "name": "CosyVoice2-0.5B 零样本声音克隆模型",
        "target_dir": WEIGHTS_DIR / "cosyvoice",
        "sources": {
            "modelscope": "iic/CosyVoice2-0.5B",
            "huggingface": "FunAudioLLM/CosyVoice2-0.5B"
        }
    },
    "retinaface": {
        "name": "RetinaFace 人脸检测与 468 关键点定位模型",
        "target_dir": WEIGHTS_DIR / "face_analysis",
        "sources": {
            "modelscope": "damo/cv_resnet50_face-detection_retinaface",
            "huggingface": "biubug6/Pytorch_Retinaface"
        }
    },
    "whisper": {
        "name": "Whisper-tiny 音频声学特征提取模型 (MuseTalk 唇形对齐依赖)",
        "target_dir": WEIGHTS_DIR / "whisper",
        "sources": {
            "modelscope": "AI-ModelScope/whisper-tiny",
            "huggingface": "openai/whisper-tiny"
        }
    },
    "bge-small-zh": {
        "name": "BGE-small-zh-v1.5 本地 RAG 知识库向量嵌入模型",
        "target_dir": WEIGHTS_DIR / "bge-small-zh",
        "sources": {
            "modelscope": "AI-ModelScope/bge-small-zh-v1.5",
            "huggingface": "BAAI/bge-small-zh-v1.5"
        }
    }
}

def download_model(model_key: str, source: str = "modelscope"):
    cfg = MODELS_CONFIG.get(model_key)
    if not cfg:
        print(f"[!] 未知模型标识: {model_key}")
        return False

    name = cfg["name"]
    target = cfg["target_dir"]
    target.mkdir(parents=True, exist_ok=True)
    repo_id = cfg["sources"].get(source, cfg["sources"]["modelscope"])

    print(f"\n[*] 准备下载: {name}")
    print(f"    - 存放路径: {target}")
    print(f"    - 下载镜像源: {source} (Repo: {repo_id})")

    if source == "modelscope":
        try:
            from modelscope.hub.snapshot_download import snapshot_download
            print("    [>] 正在调用 ModelScope SDK 高速拉取...")
            snapshot_download(repo_id, local_dir=str(target))
            print(f"    [OK] {name} 下载就绪！")
            return True
        except ImportError:
            print("    [!] 本地未安装 modelscope 库，请使用: pip install modelscope")
            print(f"    [i] 或者使用 git lfs clone: git clone https://www.modelscope.cn/{repo_id}.git {target}")
            return False
        except Exception as e:
            print(f"    [FAIL] 下载异常: {e}")
            return False
    else:
        try:
            from huggingface_hub import snapshot_download
            print("    [>] 正在调用 HuggingFace Hub 拉取...")
            snapshot_download(repo_id=repo_id, local_dir=str(target))
            print(f"    [OK] {name} 下载就绪！")
            return True
        except ImportError:
            print("    [!] 本地未安装 huggingface_hub 库，请使用: pip install huggingface_hub")
            return False
        except Exception as e:
            print(f"    [FAIL] 下载异常: {e}")
            return False

def main():
    parser = argparse.ArgumentParser(description="AI-LiveStream-Agent 离线模型权重一键下载器")
    parser.add_argument("--model", type=str, default="all", choices=["all", "musetalk", "cosyvoice", "retinaface", "whisper", "bge-small-zh"],
                        help="选择要下载的模型组件，默认全部 (all)")
    parser.add_argument("--source", type=str, default="modelscope", choices=["modelscope", "huggingface"],
                        help="下载镜像源，国内推荐 modelscope")
    args = parser.parse_args()

    print("=" * 60)
    print("  AI-LiveStream-Agent 预训练权重下载中心")
    print("=" * 60)

    if args.model == "all":
        for k in MODELS_CONFIG.keys():
            download_model(k, args.source)
    else:
        download_model(args.model, args.source)

    print("\n[OK] 任务结束。提示：在未下载权重时系统将自动以 Edge-TTS 和仿真轻量模式流畅运行。")
    print(
        "\n[进阶提示]\n"
        "  · 真实语义 RAG：将 BGE-small-zh 的 model.onnx 与 vocab.txt 放入 weights/bge-small-zh/ 即启用真实向量检索\n"
        "    (未部署则自动回退零依赖哈希投影向量)\n"
        "  · 高精度人脸：pip install mediapipe 后，形象特征将自动升级为 468 点 FaceMesh 检测\n"
        "  · 真实神经唇形：本仓库默认程序化渲染；接入 MuseTalk 神经推理需另行部署权重与 TensorRT 环境\n"
    )

if __name__ == "__main__":
    main()
