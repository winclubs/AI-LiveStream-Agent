"""
原生自包含神经数字人驱动与模型管理器测试集
验证 NativeNeuralAvatarDriver、MelSpectrogramExtractor 以及 NeuralModelManager 的功能完整性
"""

import numpy as np
import pytest

from server.core.avatar.native_neural_driver import (
    MelSpectrogramExtractor,
    NativeNeuralAvatarDriver,
)
from server.core.avatar.neural_model_manager import NeuralModelManager


def test_neural_model_manager():
    """测试自包含神经模型管理器"""
    manager = NeuralModelManager()
    status = manager.get_model_status_matrix()
    assert "models_dir" in status
    assert "models" in status
    assert "wav2lip_256" in status["models"]
    assert "wav2lip_384" in status["models"]
    assert "musetalk" in status["models"]

    # 测试模型候选路径获取
    candidates = manager.get_candidate_paths("wav2lip_256")
    assert len(candidates) > 0
    assert any(p.name == "wav2lip.pth" for p in candidates)

    # 测试不存在的模型候选路径为空
    assert manager.get_candidate_paths("non_existing_model") == []


def test_mel_spectrogram_extractor():
    """测试纯 NumPy 实现的音频梅尔倒频谱特征提取器"""
    extractor = MelSpectrogramExtractor(sample_rate=16000, n_mels=80)

    # 构造 1 秒的 16kHz s16le PCM 测试音频
    duration = 1.0
    t = np.linspace(0, duration, int(16000 * duration), endpoint=False)
    samples = (0.5 * np.sin(2 * np.pi * 440 * t) * 32767).astype(np.int16)
    pcm_bytes = samples.tobytes()

    mel = extractor.extract_mel(pcm_bytes)
    assert isinstance(mel, np.ndarray)
    assert mel.shape[0] == 80  # 80 个 mel 频带
    assert mel.shape[1] > 0   # 帧数大于 0

    # 验证静音测试
    silence = np.zeros(16000, dtype=np.int16).tobytes()
    mel_silence = extractor.extract_mel(silence)
    assert mel_silence.shape[0] == 80
    assert not np.isnan(mel_silence).any()


@pytest.mark.anyio
async def test_native_neural_avatar_driver_lifecycle():
    """测试原生自包含神经驱动器生命周期与核心接口"""
    config = {
        "fps": 25,
        "width": 1280,
        "height": 720,
        "model_key": "wav2lip_256",
    }
    driver = NativeNeuralAvatarDriver(config=config)

    # 能力探测
    caps = driver.get_capabilities()
    assert caps["driver"] == "native_neural_driver"
    assert caps["self_contained"] is True
    assert "fps" in caps
    assert "model_key" in caps
    assert "model_installed" in caps
    assert caps["external_dependencies"] is False
    assert caps["fps"] == 25

    # 启动驱动
    start_res = await driver.start()
    assert start_res is True
    assert driver.is_active is True

    # 音频推送 (模拟 16kHz PCM 16-bit 块)
    mock_pcm = (np.sin(np.linspace(0, 100, 3200)) * 32767).astype(np.int16).tobytes()
    push_ok = await driver.push_audio_chunk(mock_pcm, {"text": "欢迎各位朋友进入直播间"})
    assert push_ok is True
    assert driver.is_speaking() is True

    # 打断清除逻辑
    await driver.flush_talk()
    assert driver.is_speaking() is False
    assert driver._audio_queue.empty()

    # 停止驱动
    await driver.stop()
    assert driver.is_active is False
