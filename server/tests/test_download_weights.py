# -*- coding: utf-8 -*-
"""
download_weights.py 一键权重下载器单元测试
覆盖 onnx-lipsync 单文件下载分支：
1. MODELS_CONFIG 单文件配置完整性 (落盘目录与 NeuralModelManager 检索路径一致)；
2. ModelScope 镜像地址转换与多源回退顺序；
3. 已存在权重自动跳过，杜绝重复下载；
4. _download_single_file 多源失败时如实返回 False 并清理 .part 临时文件。
"""
import importlib.util
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

_SCRIPT = Path(__file__).resolve().parent.parent.parent / "scripts" / "download_weights.py"


def _load_download_module():
    """以独立模块形式加载 scripts/download_weights.py (不含 server 包前缀)"""
    spec = importlib.util.spec_from_file_location("ai_download_weights", _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def dw():
    return _load_download_module()


def test_onnx_lipsync_config_targets_neural_model_dir(dw):
    """onnx-lipsync 必须落盘到 NeuralModelManager 的内部模型目录，与 find_model_path 检索路径完全一致"""
    from server.core.avatar.neural_model_manager import INTERNAL_MODELS_DIR

    cfg = dw.MODELS_CONFIG["onnx-lipsync"]
    assert cfg["single_file"] is True
    assert cfg["filename"] == "onnx_lipsync.onnx"
    # 目标目录必须与 NeuralModelManager 的 SSOT 内部目录逐字节一致 (支持 LIVE_AGENT_DATA_DIR 重定向)
    assert Path(cfg["target_dir"]) == Path(INTERNAL_MODELS_DIR)
    # 必须至少有一个权威下载源
    assert len(cfg["urls"]) >= 1


def test_modelscope_mirror_conversion(dw):
    """HF /resolve/main/ 地址应正确转换为 ModelScope /resolve/master/ 镜像地址"""
    hf_url = "https://huggingface.co/winclubs/AI-LiveStream-Agent-Assets/resolve/main/onnx_lipsync.onnx"
    ms_url = dw._hf_to_modelscope_url(hf_url)
    assert ms_url == "https://modelscope.cn/models/winclubs/AI-LiveStream-Agent-Assets/resolve/master/onnx_lipsync.onnx"


def test_single_file_source_fallback_order(dw):
    """modelscope 源应镜像优先 + HF 权威兜底；huggingface 源保持原始地址"""
    cfg = dw.MODELS_CONFIG["onnx-lipsync"]
    ms_order = dw._resolve_single_file_urls(cfg, "modelscope")
    hf_order = dw._resolve_single_file_urls(cfg, "huggingface")

    assert ms_order[0].startswith("https://modelscope.cn/")
    assert ms_order[-1].startswith("https://huggingface.co/")
    assert all(u.startswith("https://huggingface.co/") for u in hf_order)


def test_existing_weight_skips_download(dw, tmp_path):
    """已存在的权重文件必须跳过下载，避免重复消耗带宽"""
    cfg = dw.MODELS_CONFIG["onnx-lipsync"].copy()
    cfg["target_dir"] = tmp_path
    # 预置一个有效权重文件 (>1KB，通过 find_model_path 的体积校验)
    existing = tmp_path / cfg["filename"]
    existing.write_bytes(b"\x00" * 2048)

    # 必须把改过的 cfg 注入回全局配置，download_model 读取的是 MODELS_CONFIG
    with patch.object(dw, "MODELS_CONFIG", {"onnx-lipsync": cfg}), \
         patch.object(dw, "_download_single_file") as mock_dl:
        result = dw.download_model("onnx-lipsync", source="huggingface")

    assert result is True
    mock_dl.assert_not_called()  # 已存在则绝不触发下载


def test_download_model_single_file_success(dw, tmp_path):
    """单文件下载成功分支：调用 _download_single_file 并落盘到目标目录"""
    cfg = dw.MODELS_CONFIG["onnx-lipsync"].copy()
    cfg["target_dir"] = tmp_path
    with patch.object(dw, "MODELS_CONFIG", {"onnx-lipsync": cfg}), \
         patch.object(dw, "_download_single_file", return_value=True) as mock_dl:
        result = dw.download_model("onnx-lipsync", source="modelscope")

    assert result is True
    mock_dl.assert_called_once()
    # 传入的目标路径必须直接指向 data/models/onnx_lipsync.onnx
    called_target = mock_dl.call_args[0][1]
    assert called_target == tmp_path / "onnx_lipsync.onnx"


def test_download_single_file_all_sources_fail(dw, tmp_path):
    """全部源失败时必须返回 False 并清理 .part 临时文件"""
    target = tmp_path / "onnx_lipsync.onnx"
    urls = ["https://invalid.example.com/missing.onnx"]

    with patch("urllib.request.urlopen", side_effect=Exception("network down")):
        result = dw._download_single_file(urls, target)

    assert result is False
    assert not target.exists()
    assert not target.with_suffix(".onnx.part").exists()


def test_download_single_file_atomic_rename(dw, tmp_path):
    """下载成功后 .part 临时文件必须原子重命名为正式权重，杜绝半成品被误识别"""
    target = tmp_path / "onnx_lipsync.onnx"
    urls = ["https://example.com/onnx_lipsync.onnx"]

    class _FakeResp:
        def __init__(self):
            self.headers = {"Content-Length": "8"}

        def read(self, size=-1):
            if not hasattr(self, "_sent", ):
                self._sent = False
            if not self._sent:
                self._sent = True
                return b"\x00" * 8
            return b""

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    with patch("urllib.request.urlopen", return_value=_FakeResp()):
        result = dw._download_single_file(urls, target)

    assert result is True
    assert target.exists()
    assert target.stat().st_size == 8
    assert not target.with_suffix(".onnx.part").exists()
