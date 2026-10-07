# -*- coding: utf-8 -*-
"""
数字人神经模型自包含资产管理器 (NeuralModelManager)
实现目标：
1. 统一管理本项目原生内置的所有数字人神经渲染模型权重 (ByteDance LatentSync / ONNX / MuseTalk)；
2. 优先检索当前系统内部数据目录 DATA_DIR / "models" 以及项目根目录下的 models/；
3. 彻底解耦外部独立系统路径，提供自包含的权重存在性检测、状态上报与下载引导；
4. 模型缺失时如实上报不可用（下游渲染驱动将拒绝启动并暴露根因，而非伪造 CPU 假唇形），
   但绝不阻断系统服务本身与音频链路的正常启动。
"""
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

from server.config import BASE_DIR, DATA_DIR

logger = logging.getLogger("LiveAgent.NeuralModelManager")

# 统一内置神经模型存放根目录
INTERNAL_MODELS_DIR = DATA_DIR / "models"
INTERNAL_MODELS_DIR.mkdir(parents=True, exist_ok=True)

# 预设官方权重清单与参考规格
MODEL_REGISTRY: Dict[str, Dict[str, Any]] = {
    "latentsync_unet": {
        "name": "ByteDance LatentSync 扩散模型 (官方高清)",
        "file_name": "latentsync_unet.pt",
        "description": "ByteDance LatentSync 官方扩散重绘模型 (1.5/1.6)，支持 25fps 高清唇形同步与端到端音频语义驱动",
        "size_mb": 1400,
        "sha256": "",
        "download_sources": [
            "https://hf-mirror.com/ByteDance/LatentSync/resolve/main/latentsync_unet.pt",
            "https://huggingface.co/ByteDance/LatentSync/resolve/main/latentsync_unet.pt",
        ],
    },
    "latentsync_onnx": {
        "name": "ByteDance LatentSync 神经唇形模型 (ONNX 加速)",
        "file_name": "onnx_lipsync.onnx",
        "description": "LatentSync 256x256 标准 ONNX 格式，支持 GPU/DirectML/CUDA 实时极速推理",
        "size_mb": 205,
        "sha256": "",
        "download_sources": [
            "https://hf-mirror.com/vnalex/wav2lip-256-onnx/resolve/main/wav2lip_256.onnx",
            "https://huggingface.co/vnalex/wav2lip-256-onnx/resolve/main/wav2lip_256.onnx",
        ],
    },
    "onnx_lipsync": {
        "name": "ONNX 神经唇形模型 (LatentSync-256, 通用加速)",
        "file_name": "onnx_lipsync.onnx",
        "description": "LatentSync 256x256 标准 ONNX 导出 (mel[1,1,80,16] + face[1,6,256,256] → [1,3,256,256])，支持 GPU/DirectML 快速推理，0 显存依赖",
        "size_mb": 205,
        "sha256": "",
        "download_sources": [
            "https://hf-mirror.com/vnalex/wav2lip-256-onnx/resolve/main/wav2lip_256.onnx",
            "https://huggingface.co/vnalex/wav2lip-256-onnx/resolve/main/wav2lip_256.onnx",
        ],
    },
    "wav2lip_temporal_256": {
        "name": "Wav2Lip-Temporal-256 (时序上下文模型)",
        "file_name": "wav2lip_temporal_256.onnx",
        "description": (
            "Wav2Lip 官方 5 帧时序上下文导出版 "
            "(face[1,6,256,256,T] + mel[1,1,80,16] → [1,3,256,256])，"
            "支持 T=1~5 动态时序平滑，消除帧间突变，需 GPU。"
            "【无自动下载源】该文件不是公开发布物，须先用 "
            "scripts/export_wav2lip_temporal_onnx.py 从官方 wav2lip_gan.pth 离线导出。"
        ),
        "size_mb": 205,
        "sha256": "",
        "download_sources": [],
        # 无官方发布物，只能本地导出
        "manual_only": True,
        "build_command": (
            "python scripts/export_wav2lip_temporal_onnx.py "
            "--weights data/models/wav2lip_gan.pth "
            "--output data/models/wav2lip_temporal_256.onnx --verify"
        ),
        "prerequisite_weights": ["wav2lip_gan.pth"],
    },
    "musetalk_v15": {
        "name": "MuseTalk-1.5 神经唇形模型 (Whisper 语义驱动)",
        "file_name": "musetalkV15/unet.pth",
        "description": (
            "MuseTalk 1.5 官方 UNet 权重，由 Whisper-tiny 提取 50x384 音素语义特征。"
            "【注意】仅下载 UNet 不足以运行：还须 sd-vae-ft-mse、whisper-tiny，"
            "以及官方实时管线的 dwpose / face-parse-bisent 与 MMLab 生态"
            "(mmcv==2.0.1 / mmdet==3.1.0 / mmpose==1.1.0)。"
            "这些依赖许可各异，商用前须逐项核对。"
        ),
        "size_mb": 1250,
        "sha256": "",
        "download_sources": [
            "https://huggingface.co/TMElyralab/MuseTalk/resolve/main/musetalkV15/unet.pth",
        ],
        # 未就绪时的真实原因会由 sidecar 的 _musetalk_missing_reasons() 如实上报
        "required_companions": [
            "musetalkV15/musetalk.json",
            "sd-vae-ft-mse/",
            "whisper-tiny/",
        ],
        "required_packages": [
            "torch", "diffusers",
            "mmcv==2.0.1", "mmdet==3.1.0", "mmpose==1.1.0",
        ],
        "license_note": (
            "code 为 MIT；model 可商用；但 whisper / sd-vae-ft-mse / dwpose / "
            "face-parse-bisent 各有独立许可，商用前必须逐项确认。"
        ),
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
            Path("D:/LiveTalking/models") / f_name,
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
            target_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, target_path)
            logger.info(f"已成功将模型权重导入至自包含目录: {target_path}")
            return True
        except Exception as e:
            logger.error(f"导入模型文件失败: {e}")
            return False

    def get_model_metadata(self, model_key: str) -> Optional[Dict[str, Any]]:
        """获取指定模型的元数据规格"""
        return MODEL_REGISTRY.get(model_key)

    def is_musetalk_ready(self) -> bool:
        """MuseTalk 1.5 管线是否**真正**就绪（架构诚实规约）。

        历史缺陷：本方法只检查 UNet 权重是否存在。但 MuseTalk 1.5 需要
        UNet + VAE + Whisper 三者齐备才能推理，缺一即无法产出。
        仅凭 UNet 存在就宣称 ready，会让上层误以为可以走MuseTalk 链路，
        实际在节点侧静默回落到 LatentSync。

        现改为检查全部必需组件；任一缺失都返回 False。
        """
        meta = MODEL_REGISTRY.get("musetalk_v15")
        if meta is None:
            return False
        if not self.is_model_available("musetalk_v15"):
            return False
        for companion in meta.get("required_companions", []):
            if not self._companion_available(companion):
                return False
        return True

    def musetalk_missing_components(self) -> list:
        """列出 MuseTalk 缺失的组件，供 /health 与设置页如实展示。"""
        meta = MODEL_REGISTRY.get("musetalk_v15") or {}
        missing = []
        if not self.is_model_available("musetalk_v15"):
            missing.append(str(meta.get("file_name") or "musetalkV15/unet.pth"))
        for companion in meta.get("required_companions", []):
            if not self._companion_available(companion):
                missing.append(companion)
        return missing

    def _companion_available(self, rel_path: str) -> bool:
        """检查 MuseTalk 配套组件（文件或目录）是否就位。

        以 musetalk_v15 主权重的实际所在目录为基准去找配套组件，
        避免出现「UNet 在 A 目录、VAE 在 B 目录」这种拼接出来的假就绪。
        """
        anchor = self.find_model_path("musetalk_v15")
        roots = []
        if anchor is not None:
            roots.append(anchor.parent.parent)      # .../musetalk
            roots.append(anchor.parent)
        roots.extend([self.models_dir, BASE_DIR / "models"])
        for root in roots:
            try:
                if (root / rel_path).exists():
                    return True
            except Exception:
                continue
        return False


# 全局单例
global_neural_model_manager = NeuralModelManager()
