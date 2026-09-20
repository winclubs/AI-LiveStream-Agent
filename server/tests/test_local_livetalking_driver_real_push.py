# -*- coding: utf-8 -*-
import pytest
from unittest.mock import AsyncMock, patch, MagicMock
from server.core.avatar.drivers import LocalLiveTalkingDriver

@pytest.fixture
def anyio_backend():
    return "asyncio"

@pytest.mark.anyio
async def test_local_livetalking_driver_lifecycle_and_push():
    driver = LocalLiveTalkingDriver(config={
        "api_endpoint": "http://127.0.0.1:8010",
        "session_id": "test_session_1",
        "avatar_id": "test_avatar_1",
    })

    # 1. 模拟 /status 探活
    mock_resp_status = MagicMock(status_code=200)
    mock_resp_audio = MagicMock(status_code=200)

    mock_client = AsyncMock()
    mock_client.is_closed = False
    mock_client.get = AsyncMock(return_value=mock_resp_status)
    mock_client.post = AsyncMock(return_value=mock_resp_audio)

    with patch.object(driver, "_get_client", AsyncMock(return_value=mock_client)):
        # 启动驱动
        started = await driver.start()
        assert started is True
        assert driver.is_connected is True

        # 2. 推送音频块 (真实推流)
        test_pcm = b"\x00\x01" * 320  # 16kHz 20ms chunk
        pushed = await driver.push_audio_chunk(test_pcm, eventpoint={"text": "测试欢迎进店"})
        assert pushed is True
        assert driver.total_audio_chunks == 1
        assert driver.total_audio_bytes == len(test_pcm)
        assert driver._speaking is True
        # 验证调用了 /humanaudio
        assert mock_client.post.call_count >= 1
        call_args = mock_client.post.call_args_list[-1]
        assert "humanaudio" in call_args[0][0]

        # 3. 瞬间打断 (flush_talk)
        await driver.flush_talk()
        assert driver._speaking is False
        call_args = mock_client.post.call_args_list[-1]
        assert "interrupt_talk" in call_args[0][0]

        # 4. 动作切片切换 (set_custom_state)
        action_ok = await driver.set_custom_state(3, duration=4.0)
        assert action_ok is True
        call_args = mock_client.post.call_args_list[-1]
        assert "set_audiotype" in call_args[0][0]

        # 5. 状态检查
        status = driver.get_status()
        assert status["api_endpoint"] == "http://127.0.0.1:8010"
        assert status["is_connected"] is True
        assert status["total_audio_chunks"] == 1

        # 6. 停止驱动
        await driver.stop()
        assert driver.is_active is False
        assert driver.is_connected is False
