import asyncio
import base64
import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from server.adapters.media.remote_gpu_driver import RemoteGPUMediaDriver


def test_remote_gpu_envelope_decoding():
    # 正常 envelope
    req_id = "req-12345"
    payload = b"test-audio-chunk"
    magic = RemoteGPUMediaDriver.AUDIO_MAGIC
    raw = magic + bytes([len(req_id)]) + req_id.encode("utf-8") + payload

    decoded_id, audio = RemoteGPUMediaDriver._decode_audio_envelope(raw)
    assert decoded_id == req_id
    assert audio == payload

    # 错误 envelope：缺少 magic
    with pytest.raises(ValueError, match="缺少 v2 envelope"):
        RemoteGPUMediaDriver._decode_audio_envelope(b"WRONG" + bytes([5]) + b"12345abc")

    # 错误 envelope：id_len 超过上限
    with pytest.raises(ValueError, match="envelope 非法"):
        RemoteGPUMediaDriver._decode_audio_envelope(magic + bytes([255]) + b"short")


def test_remote_gpu_lifecycle_and_settings():
    async def _test():
        driver = RemoteGPUMediaDriver(node_url="ws://127.0.0.1:8888/ws/render", auth_token="token-123")

        # 初始状态
        status = driver.get_preview_status()
        assert status["render_backend"] == "remote_gpu"
        assert not status["is_running"]
        assert not status["remote_connected"]

        # 参数同步
        role_mock = MagicMock(speech_speed=1.25)
        await driver.apply_role(role_mock)
        assert driver.speed == 1.25

        await driver.apply_speech_speed(1.5)
        assert driver.speed == 1.5

        # 兼容接口 feed_audio_chunk
        await driver.feed_audio_chunk(b"chunk", "snippet")
        assert driver.is_speaking is True
        await driver.feed_audio_chunk(b"", "")
        assert driver.is_speaking is False

    asyncio.run(_test())


def test_remote_gpu_connect_auth_success():
    async def _test():
        driver = RemoteGPUMediaDriver()

        mock_ws = AsyncMock()
        mock_ws.send = AsyncMock()
        mock_ws.recv = AsyncMock(return_value=json.dumps({"event": "auth_ok", "protocol_version": 2}))
        mock_ws.ping = AsyncMock(return_value=asyncio.sleep(0))
        mock_ws.close = AsyncMock()

        with patch("websockets.connect", AsyncMock(return_value=mock_ws)):
            connected = await driver._ensure_connection()
            assert connected is True
            assert driver.is_connected is True
            assert driver._protocol_version == 2

            # 再次 ensure 时复用
            connected2 = await driver._ensure_connection()
            assert connected2 is True

            # 关闭连接
            await driver.stop()
            assert driver.is_running is False
            assert driver.is_connected is False

    asyncio.run(_test())


def test_remote_gpu_synthesize_v2_stream_success():
    async def _test():
        driver = RemoteGPUMediaDriver()
        driver.is_connected = True
        driver._protocol_version = 2

        mock_ws = AsyncMock()
        mock_ws.send = AsyncMock()
        mock_ws.ping = AsyncMock(return_value=asyncio.sleep(0))
        mock_ws.close = AsyncMock()
        driver._ws = mock_ws

        captured_request_id = None

        async def fake_send(data):
            nonlocal captured_request_id
            parsed = json.loads(data)
            if parsed.get("event") == "tts_request":
                captured_request_id = parsed["request_id"]

        mock_ws.send = AsyncMock(side_effect=fake_send)

        step = 0

        async def fake_recv():
            nonlocal step, captured_request_id
            await asyncio.sleep(0.001)
            req_id = captured_request_id or "test-req"

            magic = RemoteGPUMediaDriver.AUDIO_MAGIC
            raw_audio = magic + bytes([len(req_id)]) + req_id.encode("utf-8") + b"sample-pcm"

            if step == 0:
                step += 1
                return raw_audio
            elif step == 1:
                step += 1
                fake_b64 = base64.b64encode(b"\xff\xd8\xff\xe0\x00\x10JFIF").decode("utf-8")
                return json.dumps({"event": "video_frame", "request_id": req_id, "data": fake_b64, "pts_ms": 40})
            else:
                return json.dumps({"event": "audio_end", "request_id": req_id})

        mock_ws.recv = AsyncMock(side_effect=fake_recv)

        chunks = []
        async for chunk in driver.synthesize_stream("测试文本"):
            chunks.append(chunk)

        assert chunks == [b"sample-pcm"]
        assert driver.last_completed_request_id is not None
        assert len(driver._pending_video_frames) == 1

        with patch("server.core.media.virtual_cam.global_virtual_cam.is_active", False):
            await driver.commit_video_frames(b"", request_id=driver.last_completed_request_id)
            await asyncio.sleep(0.05)
            assert driver.frames_received >= 1

    asyncio.run(_test())


def test_remote_gpu_synthesize_remote_error():
    async def _test():
        driver = RemoteGPUMediaDriver()
        driver.is_connected = True
        driver._protocol_version = 2

        mock_ws = AsyncMock()
        mock_ws.send = AsyncMock()
        mock_ws.ping = AsyncMock(return_value=asyncio.sleep(0))
        mock_ws.close = AsyncMock()
        driver._ws = mock_ws

        captured_request_id = None

        async def fake_send(data):
            nonlocal captured_request_id
            parsed = json.loads(data)
            if parsed.get("event") == "tts_request":
                captured_request_id = parsed["request_id"]

        mock_ws.send = AsyncMock(side_effect=fake_send)

        async def fake_recv():
            nonlocal captured_request_id
            req_id = captured_request_id or "dummy"
            return json.dumps({"event": "error", "message": "GPU OOM", "request_id": req_id})

        mock_ws.recv = AsyncMock(side_effect=fake_recv)

        with pytest.raises(RuntimeError, match="GPU OOM"):
            async for _ in driver.synthesize_stream("失败请求"):
                pass

    asyncio.run(_test())


def test_remote_gpu_interrupt():
    async def _test():
        driver = RemoteGPUMediaDriver()
        driver.is_connected = True
        mock_ws = AsyncMock()
        mock_ws.send = AsyncMock()
        mock_ws.close = AsyncMock()
        driver._ws = mock_ws
        driver._current_request_id = "interrupt-me"

        await driver.interrupt("Test Barge-in")

        assert driver._current_request_id is None
        assert driver.is_speaking is False
        assert mock_ws.send.called
        sent_data = json.loads(mock_ws.send.call_args[0][0])
        assert sent_data["event"] == "cancel"
        assert sent_data["request_id"] == "interrupt-me"

    asyncio.run(_test())
