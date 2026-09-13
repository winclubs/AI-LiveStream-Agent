import asyncio
import threading
import time
from pathlib import Path


def test_speak_sentence_commits_complete_audio_with_contract(monkeypatch):
    from server.routes import live as live_mod
    from server.routes.ws_live import ws_manager

    class Driver:
        audio_codec = "mp3"
        audio_mime_type = "audio/mpeg"
        audio_sample_rate = 24000
        audio_channels = 1

        async def synthesize_stream(self, text):
            yield b"first-"
            yield b"second"

    class Queue:
        @staticmethod
        def is_cancelled():
            return False

    class Role:
        role_name = "测试主播"

    class Media:
        def __init__(self):
            self.calls = []

        async def feed_audio_chunk(self, data, text):
            self.calls.append((data, text))

    class VirtualAudio:
        def __init__(self):
            self.calls = []

        def play_chunk(self, data, fallback_sample_rate=24000):
            self.calls.append((data, fallback_sample_rate))

    media = Media()
    virtual = VirtualAudio()
    packets = []

    async def broadcast(event, payload):
        packets.append((event, payload))

    monkeypatch.setattr(live_mod, "global_media_driver", media)
    monkeypatch.setattr(live_mod, "global_virtual_audio", virtual)
    monkeypatch.setattr(ws_manager, "broadcast", broadcast)

    controller = live_mod.LiveSessionController()
    controller.tts_driver = Driver()
    controller.event_queue = Queue()
    controller._audio_generation = 7
    asyncio.run(controller._speak_sentence("完整句子", Role()))

    assert media.calls == [(b"first-second", "完整句子")]
    assert virtual.calls == [(b"first-second", 24000)]
    payload = packets[0][1]
    assert packets[0][0] == "AUDIO_CHUNK"
    assert payload["codec"] == "mp3"
    assert payload["mime_type"] == "audio/mpeg"
    assert payload["sample_rate"] == 24000
    assert payload["channels"] == 1
    assert payload["audio_generation"] == 7
    assert payload["audio_id"]
    assert payload["pts_ms"] > 0


def test_runtime_tts_failure_discards_partial_and_switches_to_edge(monkeypatch):
    from server.routes import live as live_mod
    from server.routes.ws_live import ws_manager

    class FailingDriver:
        audio_codec = "mp3"
        audio_mime_type = "audio/mpeg"
        audio_sample_rate = 24000
        audio_channels = 1

        async def synthesize_stream(self, text):
            yield b"partial-must-not-play"
            raise RuntimeError("stream interrupted")

        async def stop(self):
            return None

    class FallbackDriver:
        audio_codec = "mp3"
        audio_mime_type = "audio/mpeg"
        audio_sample_rate = 24000
        audio_channels = 1

        async def start(self):
            return None

        async def synthesize_stream(self, text):
            yield b"complete-fallback"

    class Queue:
        @staticmethod
        def is_cancelled():
            return False

    class Role:
        role_name = "测试主播"

    class Media:
        async def feed_audio_chunk(self, data, text):
            assert data == b"complete-fallback"

    played = []
    packets = []

    class Virtual:
        def play_chunk(self, data, fallback_sample_rate=24000):
            played.append(data)

    async def broadcast(event, payload):
        packets.append(payload)

    monkeypatch.setattr(live_mod, "EdgeTTSMediaDriver", FallbackDriver)
    monkeypatch.setattr(live_mod, "global_media_driver", Media())
    monkeypatch.setattr(live_mod, "global_virtual_audio", Virtual())
    monkeypatch.setattr(ws_manager, "broadcast", broadcast)

    controller = live_mod.LiveSessionController()
    controller.tts_driver = FailingDriver()
    controller.event_queue = Queue()
    asyncio.run(controller._speak_sentence("需要完整降级", Role()))

    assert isinstance(controller.tts_driver, FallbackDriver)
    assert played == [b"complete-fallback"]
    assert b"partial-must-not-play" not in played
    assert packets and packets[0]["audio_base64"]


def test_virtual_audio_idle_stop_closes_stream_and_shutdown_restarts(monkeypatch):
    from server.core.media import virtual_audio as va

    calls = []

    class FakeStream:
        def __init__(self, **kwargs):
            self.active = True

        def start(self):
            calls.append(("start", threading.get_ident()))

        def write(self, data):
            calls.append(("write", threading.get_ident()))

        def abort(self):
            calls.append(("abort", threading.get_ident()))
            self.active = False

        def close(self):
            calls.append(("close", threading.get_ident()))
            self.active = False

    class FakeSD:
        OutputStream = FakeStream

    monkeypatch.setattr(va, "SD_AVAILABLE", True)
    monkeypatch.setattr(va, "sd", FakeSD())
    service = va.VirtualAudioService()
    service.play_chunk(b"\x01\x00" * 2400, 24000)

    deadline = time.time() + 2
    while time.time() < deadline and not any(op == "write" for op, _ in calls):
        time.sleep(0.01)
    assert any(op == "write" for op, _ in calls)

    service.stop()
    deadline = time.time() + 2
    while time.time() < deadline and not any(op == "close" for op, _ in calls):
        time.sleep(0.01)
    assert any(op == "close" for op, _ in calls), "空闲流必须由 worker 线程及时关闭"

    service.shutdown()
    assert service._worker is None or not service._worker.is_alive()

    calls.clear()
    service.play_chunk(b"\x01\x00" * 1200, 24000)
    deadline = time.time() + 2
    while time.time() < deadline and not any(op == "write" for op, _ in calls):
        time.sleep(0.01)
    assert any(op == "write" for op, _ in calls), "shutdown 后应可按需重建 worker"
    service.shutdown()


def test_browser_audio_contract_cancels_delayed_stale_audio():
    root = Path(__file__).parents[2]
    js = (root / "server/static/js/console.js").read_text(encoding="utf-8")
    assert "pendingAudioTimers" in js
    assert "audioPlaybackGeneration" in js
    assert "clearTimeout(timerId)" in js
    assert 'data.mime_type || "audio/mpeg"' in js
    assert "data.audio_generation" in js


def test_speak_sentence_drops_stale_audio_after_media_await(monkeypatch):
    from server.routes import live as live_mod
    from server.routes.ws_live import ws_manager

    media_entered = asyncio.Event()
    release_media = asyncio.Event()
    played = []
    packets = []
    call_order = []

    class Driver:
        audio_codec = "mp3"
        audio_mime_type = "audio/mpeg"
        audio_sample_rate = 24000
        audio_channels = 1

        async def synthesize_stream(self, text):
            yield b"complete-mp3"

        async def interrupt(self, reason):
            call_order.append("tts_interrupt")

    class Queue:
        @staticmethod
        def is_cancelled():
            return False

    class Role:
        role_name = "测试主播"

    class Media:
        async def feed_audio_chunk(self, data, text, **metadata):
            media_entered.set()
            await release_media.wait()

        async def interrupt(self, reason):
            call_order.append("media_interrupt")

    class Virtual:
        def play_chunk(self, data, **metadata):
            played.append((data, metadata))

        def stop(self, next_generation=None):
            call_order.append(("virtual_stop", next_generation))

    async def broadcast(event, payload):
        packets.append((event, payload))

    async def scenario():
        controller = live_mod.LiveSessionController()
        controller.tts_driver = Driver()
        controller.event_queue = Queue()
        controller._audio_generation = 4
        speak_task = asyncio.create_task(controller._speak_sentence("旧句子", Role()))
        await media_entered.wait()
        await controller._on_barge_in("测试抢占")
        release_media.set()
        await speak_task
        return controller

    monkeypatch.setattr(live_mod, "global_media_driver", Media())
    monkeypatch.setattr(live_mod, "global_virtual_audio", Virtual())
    monkeypatch.setattr(ws_manager, "broadcast", broadcast)

    controller = asyncio.run(scenario())
    assert controller._audio_generation == 5
    assert call_order[0] == ("virtual_stop", 5), "generation fence 必须先于异步媒体/TTS 中断"
    assert played == []
    assert not any(event == "AUDIO_CHUNK" for event, _ in packets)


def test_virtual_audio_generation_fence_rejects_late_old_producer(monkeypatch):
    from server.core.media import virtual_audio as va

    written_first_samples = []

    class FakeStream:
        active = True

        def __init__(self, **kwargs):
            self.active = True

        def start(self):
            return None

        def write(self, data):
            written_first_samples.append(float(data[0][0]))

        def abort(self):
            self.active = False

        def close(self):
            self.active = False

    class FakeSD:
        OutputStream = FakeStream

    monkeypatch.setattr(va, "SD_AVAILABLE", True)
    monkeypatch.setattr(va, "sd", FakeSD())
    service = va.VirtualAudioService()
    service.stop(next_generation=2)
    service.play_chunk(
        b"\x01\x00" * 1200,
        fallback_sample_rate=24000,
        codec="pcm_s16le",
        channels=1,
        audio_generation=1,
        session_generation=9,
    )
    service.play_chunk(
        b"\x02\x00" * 1200,
        fallback_sample_rate=24000,
        codec="pcm_s16le",
        channels=1,
        audio_generation=2,
        session_generation=9,
    )

    deadline = time.time() + 2
    while time.time() < deadline and not written_first_samples:
        time.sleep(0.01)
    service.shutdown()

    assert written_first_samples
    assert all(abs(value - (2 / 32768.0)) < 1e-6 for value in written_first_samples)


def test_declared_container_decode_failure_never_falls_back_to_pcm(monkeypatch):
    from server.core.media import audio_decode

    class BrokenSoundFile:
        @staticmethod
        def read(*args, **kwargs):
            raise RuntimeError("container decode failed")

    monkeypatch.setattr(audio_decode, "SF_AVAILABLE", True)
    monkeypatch.setattr(audio_decode, "sf", BrokenSoundFile())
    malformed = b"ID3" + (b"\x01\x00" * 64)

    for codec in ("mp3", "wav", "ogg", "flac"):
        samples, sample_rate = audio_decode.decode_audio_to_float32(
            malformed, fallback_sample_rate=24000, codec=codec
        )
        assert samples is None and sample_rate == 0

    samples, sample_rate = audio_decode.decode_audio_to_float32(
        b"\x01\x00" * 64,
        fallback_sample_rate=16000,
        codec="pcm_s16le",
        channels=1,
    )
    assert samples is not None and sample_rate == 16000

    samples2, sample_rate2 = audio_decode.decode_audio_to_float32(
        b"\x01\x00" * 64,
        fallback_sample_rate=16000,
        allow_raw_pcm=True,
    )
    assert samples2 is not None and sample_rate2 == 16000


def test_musetalk_rejects_stale_lipsync_generation(monkeypatch):
    from server.adapters.media import musetalk_driver as muse_mod

    driver = muse_mod.MuseTalkMediaDriver()
    driver.cv_available = True
    driver._accepted_audio_generation = 1
    asyncio.run(driver.interrupt("barge-in", next_generation=2))
    asyncio.run(driver.feed_audio_chunk(
        b"\x01\x00" * 800,
        "旧口型",
        codec="pcm_s16le",
        channels=1,
        audio_generation=1,
        session_generation=3,
        sample_rate=16000,
    ))
    assert driver.mouth_open_queue.empty()

    asyncio.run(driver.feed_audio_chunk(
        b"\x02\x00" * 800,
        "新口型",
        codec="pcm_s16le",
        channels=1,
        audio_generation=2,
        session_generation=3,
        sample_rate=16000,
    ))
    assert not driver.mouth_open_queue.empty()


def test_barge_in_attempts_tts_and_signal_when_media_interrupt_fails(monkeypatch):
    from server.routes import live as live_mod
    from server.routes.ws_live import ws_manager

    calls = []

    class Media:
        async def interrupt(self, reason):
            calls.append("media")
            raise RuntimeError("media interrupt failed")

    class TTS:
        async def interrupt(self, reason):
            calls.append("tts")

    class Virtual:
        def stop(self, next_generation=None):
            calls.append(("virtual", next_generation))

    async def broadcast(event, payload):
        calls.append((event, payload["audio_generation"]))

    async def scenario():
        controller = live_mod.LiveSessionController()
        controller.tts_driver = TTS()
        controller._audio_generation = 10
        await controller._on_barge_in("failure isolation")

    monkeypatch.setattr(live_mod, "global_media_driver", Media())
    monkeypatch.setattr(live_mod, "global_virtual_audio", Virtual())
    monkeypatch.setattr(ws_manager, "broadcast", broadcast)

    asyncio.run(scenario())
    assert calls == [
        ("virtual", 11),
        "media",
        "tts",
        ("TRIGGER_BARGE_IN", 11),
    ]


def test_remote_gpu_timeout_discards_partial_and_resets_connection(monkeypatch):
    """回归：远程 partial 音频后超时必须整句失败，不得提交半句或复用旧连接。"""
    import pytest
    from server.adapters.media.remote_gpu_driver import RemoteGPUMediaDriver

    class FakeWebSocket:
        def __init__(self):
            self.recv_count = 0
            self.closed = False
            self.sent = []

        async def send(self, data):
            self.sent.append(data)

        async def recv(self):
            self.recv_count += 1
            if self.recv_count == 1:
                return b"partial-must-not-escape"
            raise asyncio.TimeoutError

        async def close(self):
            self.closed = True

    async def run():
        driver = RemoteGPUMediaDriver()
        socket = FakeWebSocket()
        driver._ws = socket
        driver.is_connected = True

        async def connected():
            return True

        monkeypatch.setattr(driver, "_ensure_connection", connected)
        chunks = []
        with pytest.raises(RuntimeError, match="响应超时"):
            async for chunk in driver.synthesize_stream("完整句子"):
                chunks.append(chunk)
        return driver, socket, chunks

    driver, socket, chunks = asyncio.run(run())
    assert chunks == []
    assert socket.closed is True
    assert driver._ws is None
    assert driver.is_connected is False
    assert driver.has_frames is False


def test_remote_gpu_requires_matching_end_before_staging_video(monkeypatch):
    """回归：请求 ID 匹配且 audio_end 到达后，才提交音频和视频时间线。"""
    import base64
    import json
    from server.adapters.media.remote_gpu_driver import RemoteGPUMediaDriver

    jpeg = b"\xff\xd8frame\xff\xd9"

    class FakeWebSocket:
        def __init__(self):
            self.request_id = None
            self.responses = []

        async def send(self, data):
            msg = json.loads(data)
            if msg.get("event") == "tts_request":
                self.request_id = msg["request_id"]
                self.responses = [
                    json.dumps({
                        "event": "video_frame",
                        "request_id": self.request_id,
                        "pts_ms": 40,
                        "data": base64.b64encode(jpeg).decode("ascii"),
                    }),
                    b"complete-audio",
                    json.dumps({"event": "audio_end", "request_id": self.request_id}),
                ]

        async def recv(self):
            return self.responses.pop(0)

        async def close(self):
            return None

    async def run():
        driver = RemoteGPUMediaDriver()
        driver._ws = FakeWebSocket()
        driver.is_connected = True

        async def connected():
            return True

        monkeypatch.setattr(driver, "_ensure_connection", connected)
        chunks = [chunk async for chunk in driver.synthesize_stream("请求")]
        assert driver.latest_jpeg == b"", "音频提交前不得提前发布远程视频"
        assert driver._pending_video_frames == [(40, jpeg)]
        return chunks

    assert asyncio.run(run()) == [b"complete-audio"]
