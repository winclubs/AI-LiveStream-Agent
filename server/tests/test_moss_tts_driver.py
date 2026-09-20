# -*- coding: utf-8 -*-
"""
MOSS-TTS-Nano 媒体驱动与端侧零样本语音驱动测试套件
"""
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch
import pytest
from server.adapters.media.moss_driver import MossTTSMediaDriver


@pytest.mark.anyio
async def test_moss_tts_driver_properties():
    """验证 MOSS-TTS-Nano 广播级 48kHz 与基础媒体元数据规格"""
    driver = MossTTSMediaDriver(api_base="http://127.0.0.1:9880", prompt_wav_path="/path/to/ref.wav")
    assert driver.audio_sample_rate == 48000
    assert driver.audio_codec == "wav"
    assert driver.audio_channels == 1
    assert driver.api_base == "http://127.0.0.1:9880"
    assert driver.prompt_wav_path == "/path/to/ref.wav"

    await driver.apply_speech_speed(1.25)
    assert driver.speed == 1.25

    await driver.apply_volume_gain(0.8)
    assert driver.volume == 0.8

    await driver.set_prompt_voice("/path/to/new_ref.wav")
    assert driver.prompt_wav_path == "/path/to/new_ref.wav"


@pytest.mark.anyio
async def test_moss_tts_driver_health_check():
    """验证 MOSS-TTS-Nano 健康探针探测逻辑"""
    driver = MossTTSMediaDriver(api_base="http://127.0.0.1:9880")

    # 1. 模拟健康端点返回 200
    mock_resp = MagicMock()
    mock_resp.status_code = 200

    with patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get:
        mock_get.return_value = mock_resp
        ok = await driver.health_check(timeout=1.0)
        assert ok is True

    # 2. 模拟网络连接断开
    with patch("httpx.AsyncClient.get", side_effect=Exception("Connection refused")):
        ok = await driver.health_check(timeout=1.0)
        assert ok is False


@pytest.mark.anyio
async def test_moss_tts_driver_synthesize_stream():
    """验证 MOSS-TTS-Nano 流式分块音频输出与异常处理"""
    driver = MossTTSMediaDriver(api_base="http://127.0.0.1:9880")

    class FakeAiter:
        def __init__(self, chunks):
            self._chunks = list(chunks)

        def __aiter__(self):
            return self

        async def __anext__(self):
            if not self._chunks:
                raise StopAsyncIteration
            return self._chunks.pop(0)

    class FakeStreamResponse:
        def __init__(self, status_code, chunks):
            self.status_code = status_code
            self._chunks = chunks

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc_val, exc_tb):
            pass

        def aiter_bytes(self, chunk_size=4096):
            return FakeAiter(self._chunks)

    # 1. 正常流式返回音频数据
    mock_chunks = [b"RIFF\x24\x00\x00\x00WAVE", b"fmt \x10\x00\x00\x00", b"data\x00\x00\x00\x00"]
    fake_resp = FakeStreamResponse(200, mock_chunks)

    with patch("httpx.AsyncClient.stream", return_value=fake_resp):
        received = []
        async for chunk in driver.synthesize_stream("测试 MOSS 发声"):
            received.append(chunk)
        assert len(received) == 3
        assert b"".join(received) == b"".join(mock_chunks)

    # 2. 空文本不发请求
    empty_received = []
    async for chunk in driver.synthesize_stream("   "):
        empty_received.append(chunk)
    assert len(empty_received) == 0

    # 3. 打断逻辑
    await driver.start()
    await driver.feed_audio_chunk(b"fake", "fake text")
    assert driver.is_speaking is True
    await driver.interrupt("Test Barge-in")
    assert driver.is_speaking is False
    await driver.stop()
    assert driver.is_running is False


@pytest.mark.anyio
async def test_live_select_moss_tts_driver():
    """验证 live.py 在配置 moss_tts_nano 时正确实例化并优先选择 MOSS 驱动"""
    from server.routes.live import LiveSessionController
    from server.adapters.media.moss_driver import MossTTSMediaDriver

    mgr = LiveSessionController()

    # 模拟健康检查通过
    with patch.object(MossTTSMediaDriver, "health_check", return_value=True):
        with patch.object(MossTTSMediaDriver, "start", new_callable=AsyncMock):
            with patch("server.database.db.AsyncSessionLocal") as mock_db:
                # 构造 mock ApiProviderConfig
                mock_session = AsyncMock()
                mock_db.return_value.__aenter__.return_value = mock_session

                mock_cfg = MagicMock()
                mock_cfg.config_group = "tts"
                mock_cfg.provider_name = "moss_tts_nano"
                mock_cfg.base_url = "http://127.0.0.1:9880"
                mock_cfg.is_active = 1

                mock_result = MagicMock()
                mock_result.scalars.return_value.first.side_effect = [
                    mock_cfg,  # tts_cfg
                    None,      # remote_cfg
                    None,      # active_voice
                    None,      # fallback voice
                ]
                mock_session.execute.return_value = mock_result

                driver = await mgr._select_tts_driver()
                assert isinstance(driver, MossTTSMediaDriver)
                assert driver.api_base == "http://127.0.0.1:9880"


@pytest.mark.anyio
async def test_moss_clone_preview_isolation(tmp_path):
    """验证在 MOSS-TTS-Nano 引擎下试听本地克隆音色时，完全隔离阿里云百炼，杜绝百炼报错"""
    from server.core.audio.clone_preview import generate_cloned_voice_preview

    # 创建一个临时假参考音频
    ref_audio = tmp_path / "ref_AAAA.wav"
    ref_audio.write_bytes(b"RIFF\x24\x00\x00\x00WAVEfmt \x10\x00\x00\x00data\x00\x00\x00\x00")

    # 1. 模拟本地 9880 在线时直接返回 MOSS 推理音频
    mock_moss_resp = MagicMock()
    mock_moss_resp.status_code = 200
    mock_moss_resp.content = b"MOSS_AUDIO_STREAM_DATA" * 50

    with patch("httpx.AsyncClient.post", new_callable=AsyncMock, return_value=mock_moss_resp):
        out_path = await generate_cloned_voice_preview(
            voice_id="clone_AAAA",
            voice_name="AAAA",
            sample_audio_path=str(ref_audio),
            custom_text="欢迎来到直播间！",
            provider="moss_tts_nano",
        )
        assert str(out_path).endswith(".mp3")
        import os
        assert os.path.exists(out_path)

    # 2. 模拟本地 9880 离线时，主进程自动调用原生端侧 MOSS 引擎合成新台词，绝不回放原样本，也绝不报阿里云百炼错误
    with patch("httpx.AsyncClient.post", side_effect=Exception("Connection refused")):
        fallback_out = await generate_cloned_voice_preview(
            voice_id="clone_AAAA",
            voice_name="AAAA",
            sample_audio_path=str(ref_audio),
            custom_text="欢迎来到直播间！",
            force_regenerate=True,
            provider="moss_tts_nano",
        )
        assert os.path.exists(fallback_out)
        # 铁律验证：生成的是包含新台词的全新音频，绝不能直接回放原录音文件！
        assert str(fallback_out) != str(ref_audio)

