"""
原生自包含数字人驱动与模型管理器测试集
验证 NativeNeuralAvatarDriver、NeuralLipRenderer 接入以及 NeuralModelManager 的功能完整性
恪守 ADR-16 架构诚实原则，杜绝测试空转与虚假上报
"""

from fastapi.testclient import TestClient
import numpy as np
import pytest

from server.app import app
from server.core.avatar.drivers import LocalLiveTalkingDriver
from server.core.avatar.native_neural_driver import NativeNeuralAvatarDriver
from server.core.avatar.neural_model_manager import NeuralModelManager


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


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
    """测试神经唇形引擎的 80 维 Mel 频谱特征提取器 (Wav2Lip 标准声学特征)"""
    from server.core.avatar.neural_lip_renderer import MelFeatureExtractor

    extractor = MelFeatureExtractor(sample_rate=16000, n_mels=80)

    # 构造 1 秒的 16kHz float32 测试音频 (440Hz 正弦)
    duration = 1.0
    t = np.linspace(0, duration, int(16000 * duration), endpoint=False, dtype=np.float64)
    samples = (0.5 * np.sin(2 * np.pi * 440 * t)).astype(np.float32)

    mel = extractor.extract_mel_window(samples, target_steps=16)
    assert isinstance(mel, np.ndarray)
    # Wav2Lip 标准输入形状: [1, 1, 80, 16]
    assert mel.shape == (1, 1, 80, 16)
    assert not np.isnan(mel).any()

    # 静音输入：幅度极低，但仍须返回合法形状 (由调用方决定是否跳过推理)
    silence = np.zeros(16000, dtype=np.float32)
    mel_silence = extractor.extract_mel_window(silence)
    assert mel_silence.shape == (1, 1, 80, 16)
    assert not np.isnan(mel_silence).any()


@pytest.mark.anyio
async def test_native_neural_avatar_driver_lifecycle_and_non_black_frames():
    """测试驱动生命周期、ADR-16 架构诚实性及待机非黑屏真实画面"""
    config = {
        "fps": 25,
        "width": 1280,
        "height": 720,
        "model_key": "wav2lip_256",
    }
    driver = NativeNeuralAvatarDriver(config=config)

    # 1. ADR-16 诚实上报检测：在未载入深度学习模型时，绝不谎报 neural_lipsync 为 True
    caps = driver.get_capabilities()
    assert caps["driver"] == "native_neural_driver"
    assert caps["self_contained"] is True
    assert caps["neural_lipsync"] is False  # 必须如实报告为 False
    assert caps["engine_type"] == "real_avatar_lite"
    assert "fps" in caps
    assert caps["fps"] == 25

    # 2. 待机非黑屏实质性测试：停顿与待机时画面绝不能是全黑或纯深色占位帧
    standby_frame = driver._render_frame(0)
    assert standby_frame is not None
    assert standby_frame.shape == (720, 1280, 3)
    # 平均像素亮度必须 > 30.0，杜绝 (24, 24, 24) 纯黑帧
    mean_val = float(standby_frame.mean())
    assert mean_val > 30.0, f"待机画面过暗或黑屏，当前像素均值: {mean_val}"

    # 3. 启动驱动
    start_res = await driver.start()
    assert start_res is True
    assert driver.is_active is True

    # 4. 音频推送与发声状态更新
    mock_pcm = (np.sin(np.linspace(0, 100, 3200)) * 32767).astype(np.int16).tobytes()
    push_ok = await driver.push_audio_chunk(mock_pcm, {"text": "欢迎各位朋友进入直播间"})
    assert push_ok is True
    assert driver.is_speaking() is True

    # 5. 打断清除逻辑
    await driver.flush_talk()
    assert driver.is_speaking() is False
    assert driver._audio_queue.empty()

    # 6. 停止驱动
    await driver.stop()
    assert driver.is_active is False


def test_action_clip_priority_over_standby(monkeypatch):
    """测试动作切片帧优先级：当处于动作状态机激活动作时，画面优先呈现动作帧而不被待机底模粗暴覆盖"""
    driver = NativeNeuralAvatarDriver(config={"fps": 25, "width": 100, "height": 100})

    # 模拟动作状态机输出特定的纯绿色测试动作帧 (R=0, G=255, B=0)
    mock_action_frame = np.zeros((100, 100, 3), dtype=np.uint8)
    mock_action_frame[:, :, 1] = 255

    monkeypatch.setattr(driver.action_state_machine, "get_frame", lambda idx: mock_action_frame)

    rendered = driver._render_frame(0)
    assert rendered is not None
    # 验证输出的是动作切片帧（Green 通道为 255）
    assert rendered[50, 50, 1] == 255


def test_webrtc_color_conversion(monkeypatch):
    """测试 BaseAvatarDriver 在分发至 WebRTC 时严格执行 RGB->BGR 色序转换，避免蓝脸"""
    config = {"auto_bind_streams": False}
    driver = NativeNeuralAvatarDriver(config=config)

    class MockWebRTC:
        def __init__(self):
            self.last_frame = None

        def push_frame(self, frame):
            self.last_frame = frame

    mock_webrtc = MockWebRTC()
    driver.attach_webrtc_streamer(mock_webrtc)

    # 构造一块红色纯色图 (R=255, G=0, B=0)
    rgb_test_frame = np.zeros((100, 100, 3), dtype=np.uint8)
    rgb_test_frame[:, :, 0] = 255  # Red 通道

    driver.publish_frame(rgb_test_frame)
    assert mock_webrtc.last_frame is not None
    # 转换为 BGR 后，第 0 通道应为 Blue，第 2 通道应为 Red
    # 容差 ±10 吸收防录播微噪点/环境光律动的合法微小扰动
    blue_val = int(mock_webrtc.last_frame[50, 50, 0])
    red_val = int(mock_webrtc.last_frame[50, 50, 2])
    assert blue_val <= 10, f"Blue 通道应为低值，实际 {blue_val} (RGB->BGR 转换可能失效)"
    assert red_val >= 245, f"Red 通道应接近 255，实际 {red_val} (RGB->BGR 转换可能失效)"


def test_local_livetalking_honest_reporting():
    """测试 LocalLiveTalkingDriver 在未探活外部服务时的诚实上报"""
    driver = LocalLiveTalkingDriver(config={"api_endpoint": "http://127.0.0.1:9999"})
    caps = driver.get_capabilities()
    assert caps["driver"] == "livetalking"
    assert caps["is_connected"] is False
    assert caps["neural_lipsync"] is False


def test_neural_models_settings_api(client):
    """测试系统配置中自包含神经模型状态查询 API"""
    res = client.get("/api/v1/settings/neural-models")
    assert res.status_code == 200
    data = res.json()
    assert data["code"] == 0
    assert "data" in data
    assert "models_dir" in data["data"]
    assert "models" in data["data"]
    assert "wav2lip_256" in data["data"]["models"]


def test_preflight_includes_avatar_engine_check(client):
    """测试开播体检预检接口中包含自包含数字人渲染引擎检测"""
    res = client.get("/api/v1/live/preflight")
    assert res.status_code == 200
    data = res.json()
    assert data["code"] == 0
    checks = data["data"]["checks"]
    avatar_engine_check = next((c for c in checks if c["key"] == "avatar_engine"), None)
    assert avatar_engine_check is not None
    assert avatar_engine_check["status"] == "pass"
    assert "自包含数字人" in avatar_engine_check["title"]
