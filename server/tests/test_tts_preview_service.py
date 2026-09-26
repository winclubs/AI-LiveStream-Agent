# -*- coding: utf-8 -*-
"""共享 TTS 试听合成服务回归测试"""
import pytest


def _patch_edge_tts(monkeypatch):
    """用确定性假音频替换 edge_tts 网络调用"""
    import types

    fake = types.ModuleType("edge_tts")

    class _Stream:
        def __init__(self, payload: bytes):
            self._payload = payload
            self._yielded = False

        def __aiter__(self):
            return self

        async def __anext__(self):
            if self._yielded:
                raise StopAsyncIteration
            self._yielded = True
            return {"type": "audio", "data": self._payload}

        async def aclose(self):
            pass

    class _Communicate:
        def __init__(self, text: str, voice: str):
            self.text = text
            self.voice = voice

        def stream(self):
            return _Stream(b"FAKE-MP3-BYTES")

    fake.Communicate = _Communicate
    monkeypatch.setitem(__import__("sys").modules, "edge_tts", fake)


@pytest.mark.anyio
async def test_synthesize_edge_tts_returns_bytes(monkeypatch):
    _patch_edge_tts(monkeypatch)
    from server.core.audio.tts_preview_service import (
        PreviewSpeechParams,
        synthesize_preview_audio,
    )

    data, media_type = await synthesize_preview_audio(
        PreviewSpeechParams(provider_name="edge_tts", voice_name="zh-CN-XiaoxiaoNeural", text="你好")
    )
    assert data == b"FAKE-MP3-BYTES"
    assert media_type == "audio/mpeg"


@pytest.mark.anyio
async def test_synthesize_empty_text_uses_profile_text(monkeypatch):
    _patch_edge_tts(monkeypatch)
    from server.core.audio.tts_preview_service import (
        PreviewSpeechParams,
        synthesize_preview_audio,
    )

    # 未知 provider 且无 base_url 时最终走 edge 兜底，空文本会用预置台词
    data, _ = await synthesize_preview_audio(
        PreviewSpeechParams(provider_name="", voice_name="", text="")
    )
    assert data == b"FAKE-MP3-BYTES"
