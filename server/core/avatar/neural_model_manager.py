# -*- coding: utf-8 -*-
"""
数字人神经模型自包含资产管理器 (NeuralModelManager)
实现目标：
1. 统一管理本项目原生内置的所有数字人神经渲染模型权重 (Wav2Lip / MuseTalk / ONNX)；
2. 优先检索当前系统内部数据目录 DATA_DIR / "models" 以及项目根目录下的 models/；
3. 彻底解耦外部独立系统路径，提供自包含的权重存在性检测、状态上报与下载引导；
4. 严格保障模型缺失时的平滑降级，绝不阻断系统启动与主直播流程。
"""
import hashlib
import json
import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

from server.config import BASE_DIR, DATA_DIR

logger = logging.getLogger("LiveAgent.NeuralModelManager")

# 统一内置神经模型存放根目录
INTERNAL_MODELS_DIR = DATA_DIR / "models"
INTERNAL_MODELS_DIR.mkdir(parents=True, exist_ok=True)

# 预设官方权重清单与参考规格
MODEL_REGISTRY: Dict[str, Dict[str, Any]] = {
    "wav2lip_256": {
        "name": "Wav2Lip-256 (标清极速模型)",
        "file_name": "wav2lip.pth",
        "description": "256x256 分辨率唇形重绘模型，单卡推理极快 (60~120 FPS)，适合主流显卡实时带货",
        "size_mb": 416,
        "sha256": "",
        "download_sources": [
            "https://huggingface.co/Rudrabha/Wav2Lip/resolve/main/wav2lip.pth",
            "https://modelscope.cn/models/iic/Wav2Lip/resolve/master/wav2lip.pth",
        ],
    },
    "wav2lip_384": {
        "name": "Wav2Lip-GAN-384 (高清写实模型)",
        "file_name": "wav2lip_gan.pth",
        "description": "384x384 高清判别模型，牙齿与下颌细节更清晰，适合 1080P 高清直播",
        "size_mb": 416,
        "sha256": "",
        "download_sources": [
            "https://huggingface.co/Rudrabha/Wav2Lip/resolve/main/wav2lip_gan.pth",
        ],
    },
    "musetalk": {
        "name": "MuseTalk 面部神经重绘模型",
        "file_name": "musetalk.json",
        "description": "MuseTalk 1080P 高精度微表情重绘，适合 RTX 3090 / 4090 旗舰显卡或云端 GPU",
        "size_mb": 2048,
        "sha256": "",
        "download_sources": [
            "https://huggingface.co/TMElyralab/MuseTalk",
        ],
    },
    "onnx_lipsync": {
        "name": "ONNX 轻量神经唇形模型 (CPU通用)",
        "file_name": "onnx_lipsync.onnx",
        "description": "精简量化 ONNX 神经口型模型，支持 CPU/DirectML 快速推理，0 显存依赖",
        "size_mb": 45,
        "sha256": "",
        "download_sources": [
            "https://huggingface.co/winclubs/AI-LiveStream-Agent-Assets/resolve/main/onnx_lipsync.onnx",
        ],
    },
}


class NeuralModelManager:
    """自包含神经模型管理器"""

    def __init__(self, models_dir: Optional[Path] = None):
        self.models_dir = models_dir or INTERNAL_MODELS_DIR
        self._cached_status: Dict[str, bool] = {}

    def get_candidate_paths(self, model_key: str) -> List[Path]:
        """获取指定模型在系统内部可能存在的路径候选列表 (按优先级排序)"""
        meta = MODEL_REGISTRY.get(model_key)
        if not meta:
            return []
        f_name = meta["file_name"]
        return [
            self.models_dir / f_name,
            self.models_dir / model_key / f_name,
            BASE_DIR / "models" / f_name,
            BASE_DIR / "models" / model_key / f_name,
        ]

    def find_model_path(self, model_key: str) -> Optional[Path]:
        """定位指定模型是否存在于本项目内部目录"""
        for p in self.get_candidate_paths(model_key):
            if p.exists() and p.is_file() and p.stat().st_size > 1024:
                return p
        return None

    def is_model_available(self, model_key: str) -> bool:
        """检查指定模型是否已就绪"""
        return self.find_model_path(model_key) is not None

    def get_model_status_matrix(self) -> Dict[str, Any]:
        """获取全量内置神经模型的就绪状态矩阵与下载引导"""
        result = {}
        has_any_neural = False

        for key, meta in MODEL_REGISTRY.items():
            path = self.find_model_path(key)
            is_ready = path is not None
            if is_ready:
                has_any_neural = True

            result[key] = {
                "name": meta["name"],
                "file_name": meta["file_name"],
                "description": meta["description"],
                "size_mb": meta["size_mb"],
                "is_installed": is_ready,
                "installed_path": str(path) if path else "",
                "download_sources": meta["download_sources"],
            }

        return {
            "models_dir": str(self.models_dir),
            "has_neural_model": has_any_neural,
            "models": result,
            "instructions": "将下载好的权重文件直接放入 data/models/ 目录，系统将自动原生加载，无需外部服务！",
        }

    def import_local_model_file(self, src_path: str, model_key: str) -> bool:
        """支持用户从已有路径一键导入模型权重到本项目自包含目录"""
        src = Path(src_path)
        if not src.exists() or not src.is_file():
            logger.warning(f"导入源模型文件不存在: {src_path}")
            return False

        meta = MODEL_REGISTRY.get(model_key)
        if not meta:
            logger.warning(f"未知模型标识: {model_key}")
            return False

        import shutil
        target_path = self.models_dir / meta["file_name"]
        try:
            shutil.copy2(src, target_path)
            logger.info(f"已成功将模型权重导入至自包含目录: {target_path}")
            return True
        except Exception as e:
            logger.error(f"导入模型文件失败: {e}")
            return False


# 全局单例
global_neural_model_manager = NeuralModelManager()
