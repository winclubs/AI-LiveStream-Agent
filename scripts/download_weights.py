#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AI-LiveStream-Agent 模型权重一键下载器
支持从 ModelScope(魔搭，国内推荐) 或 HuggingFace 自动下载：
- ONNX 轻量神经唇形重绘模型 (NeuralLipRenderer 核心权重，单文件)
- MuseTalk 2.0 唇形驱动权重 (musetalk.json, unet.pth, dwpose, whisper)
- CosyVoice2-0.5B 声音克隆模型
- RetinaFace 人脸检测与 468 关键点检测模型
- Whisper-tiny 音频特征提取模型
- BGE-small-zh-v1.5 本地向量检索模型
"""

import os
import sys
import shutil
import argparse
import urllib.request
from pathlib import Path

# 适配终端编码
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

PROJECT_ROOT = Path(__file__).resolve().parent.parent
WEIGHTS_DIR = PROJECT_ROOT / "weights"

# 神经唇形权重的落盘目录必须与 NeuralModelManager 的检索路径一致
# (server/core/avatar/neural_model_manager.py: find_model_path 优先检索 DATA_DIR/models/<file_name>)
try:
    from server.config import DATA_DIR
except Exception:  # 允许脱离主包独立运行
    DATA_DIR = PROJECT_ROOT / "data"
NEURAL_MODELS_DIR = Path(DATA_DIR) / "models"

# 单文件下载的 User-Agent，避免被 CDN 拦截
_HTTP_HEADERS = {"User-Agent": "AI-LiveStream-Agent-WeightDownloader/1.0"}


def _hf_to_modelscope_url(hf_url: str) -> str:
    """将 HuggingFace 的 /resolve/main/ 单文件地址转换为 ModelScope 镜像地址"""
    return hf_url.replace(
        "https://huggingface.co/", "https://modelscope.cn/models/"
    ).replace("/resolve/main/", "/resolve/master/")


def _resolve_single_file_urls(cfg: dict, source: str) -> list:
    """
    为单文件模型构造有序下载地址列表：
    - 优先用户指定的镜像源 (modelscope 镜像优先)；
    - 追加注册表声明的权威地址作为兜底，任一源失败自动切换。
    """
    urls: list = []
    raw_sources = list(cfg.get("urls") or [])
    if source == "modelscope":
        for url in raw_sources:
            if "huggingface.co" in url:
                converted = _hf_to_modelscope_url(url)
                if converted not in urls:
                    urls.append(converted)
    for url in raw_sources:
        if url not in urls:
            urls.append(url)
    return urls


def _download_single_file(urls: list, target_path: Path) -> bool:
    """流式下载单文件 (断点保护：先写 .part 再原子重命名)，多源自动回退"""
    target_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = target_path.with_suffix(target_path.suffix + ".part")

    for attempt, url in enumerate(urls, start=1):
        print(f"    [>] 源 {attempt}/{len(urls)}: {url}")
        try:
            req = urllib.request.Request(url, headers=_HTTP_HEADERS)
            with urllib.request.urlopen(req, timeout=30) as resp:
                total = int(resp.headers.get("Content-Length", 0) or 0)
                done = 0
                last_pct = -1
                with open(tmp_path, "wb") as out:
                    while True:
                        chunk = resp.read(1024 * 256)
                        if not chunk:
                            break
                        out.write(chunk)
                        done += len(chunk)
                        if total > 0:
                            pct = int(done * 100 / total)
                            if pct != last_pct and pct % 10 == 0:
                                print(f"        进度: {pct}% ({done // (1024*1024)}MB / {total // (1024*1024)}MB)", end="\r")
                                last_pct = pct
            print()
            # 原子重命名，避免半成品文件被误识别为已就绪权重
            shutil.move(str(tmp_path), str(target_path))
            size_mb = target_path.stat().st_size / (1024 * 1024)
            print(f"    [OK] 下载完成: {target_path.name} ({size_mb:.1f} MB)")
            return True
        except urllib.error.HTTPError as e:
            print(f"    [!] 该源不可用 (HTTP {e.code})，尝试下一个源...")
        except Exception as e:
            print(f"    [!] 该源下载异常: {e}，尝试下一个源...")
        finally:
            if tmp_path.exists():
                try:
                    tmp_path.unlink()
                except Exception:
                    pass

    print(f"    [FAIL] 全部 {len(urls)} 个下载源均不可用")
    return False


# 单文件神经唇形权重地址与 NeuralModelManager.MODEL_REGISTRY 同源，杜绝双份维护漂移
try:
    from server.core.avatar.neural_model_manager import MODEL_REGISTRY as _NEURAL_MODEL_REGISTRY
    _ONNX_LIPSYNC_URLS = list(_NEURAL_MODEL_REGISTRY.get("onnx_lipsync", {}).get("download_sources") or [])
except Exception:
    _ONNX_LIPSYNC_URLS = []

MODELS_CONFIG = {
    "onnx-lipsync": {
        "name": "ONNX 轻量神经唇形重绘模型 (NeuralLipRenderer 核心权重)",
        "target_dir": NEURAL_MODELS_DIR,
        "single_file": True,
        "filename": "onnx_lipsync.onnx",
        "urls": _ONNX_LIPSYNC_URLS,
    },
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

    # 单文件权重 (如 ONNX 神经唇形模型)：多源流式下载，直接落盘到 NeuralModelManager 检索目录
    if cfg.get("single_file"):
        filename = cfg.get("filename")
        if not filename:
            print(f"    [!] 单文件模型 {model_key} 缺少 filename 配置")
            return False
        target_path = target / filename
        if target_path.exists() and target_path.stat().st_size > 1024:
            print(f"    [i] {filename} 已存在 ({target_path.stat().st_size / (1024*1024):.1f} MB)，跳过下载")
            return True
        print(f"\n[*] 准备下载: {name}")
        print(f"    - 存放路径: {target_path}")
        urls = _resolve_single_file_urls(cfg, source)
        if not urls:
            print("    [FAIL] 未配置任何下载源")
            return False
        ok = _download_single_file(urls, target_path)
        if ok:
            print(f"    [OK] 神经唇形权重已就绪，NeuralLipRenderer 将在下次启动时自动加载并启用真实推理")
        return ok

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
    parser.add_argument("--model", type=str, default="all",
                        choices=["all", "onnx-lipsync", "musetalk", "cosyvoice", "retinaface", "whisper", "bge-small-zh"],
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
        "  · 真实神经唇形：执行 python scripts/download_weights.py --model onnx-lipsync\n"
        "    将 onnx_lipsync.onnx 放入 data/models/ 后，NeuralLipRenderer 自动启用真实 ONNX 唇形重绘\n"
        "    (未下载时自动平滑回退 RealAvatarLite 微动态引擎，直播不中断)\n"
        "  · 真实语义 RAG：将 BGE-small-zh 的 model.onnx 与 vocab.txt 放入 weights/bge-small-zh/ 即启用真实向量检索\n"
        "    (未部署则自动回退零依赖哈希投影向量)\n"
        "  · 高精度人脸：pip install mediapipe 后，形象特征将自动升级为 468 点 FaceMesh 检测\n"
    )

if __name__ == "__main__":
    main()
