"""P1-5: VAD 打断代际栅栏专项测试

修复前缺陷：full_duplex_asr 的打断只调驱动 flush_talk()，既不推进
_audio_generation 也不停 virtual_audio，刚提交的句子仍可能播出来；
且 fire-and-forget create_task 用 except: pass 吞掉所有错误。

验证要点：
1. 提供 on_barge_in 时，VAD 打断优先走控制器级栅栏回调；
2. 栅栏回调抛错时回退 flush_talk，打断不丢失；
3. 未提供栅栏回调时保持原有 flush_talk 行为；
4. 协程型栅栏回调被正确调度，不阻塞 feed_pcm。
"""
import asyncio

import numpy as np
import pytest

from server.core.audio.full_duplex_asr import ASRSession


def _voice_pcm(duration_sec: float = 0.04, volume: float = 0.9) -> bytes:
    sr = 16000
    t = np.linspace(0, duration_sec, int(sr * duration_sec), endpoint=False)
    return (volume * np.sin(2 * np.pi * 440.0 * t) * 32767).astype(np.int16).tobytes()


def _silent_pcm(duration_sec: float = 0.04) -> bytes:
    return np.zeros(int(16000 * duration_sec), dtype=np.int16).tobytes()


class _FakeSpeakingDriver:
    """假驱动：is_speaking 可控，记录 flush_talk 调用"""

    def __init__(self):
        self._speaking = False
        self.flush_calls = 0

    def is_speaking(self):
        return self._speaking

    async def flush_talk(self):
        self.flush_calls += 1
        self._speaking = False


@pytest.fixture
def speaking_driver(monkeypatch):
    driver = _FakeSpeakingDriver()
    driver._speaking = True
    monkeypatch.setattr(
        "server.core.audio.full_duplex_asr.get_active_avatar_driver",
        lambda: driver,
    )
    return driver


def test_vad_interrupt_prefers_generation_fence(speaking_driver):
    """提供 on_barge_in 时必须优先调用栅栏回调，flush_talk 不再被调用"""
    fence_calls = []

    session = ASRSession(
        session_id="fence_1",
        energy_threshold=200.0,
        on_barge_in=lambda: fence_calls.append(1),
    )
    session.feed_pcm(_voice_pcm())
    session.feed_pcm(_voice_pcm())

    assert fence_calls == [1]
    assert speaking_driver.flush_calls == 0
    assert session.interrupted_triggered is True


@pytest.mark.anyio
async def test_vad_interrupt_falls_back_to_flush_talk_on_fence_error(speaking_driver):
    """栅栏回调抛错时必须回退 flush_talk，打断效果不丢失"""

    def broken_fence():
        raise RuntimeError("控制器不可用")

    session = ASRSession(
        session_id="fence_2",
        energy_threshold=200.0,
        on_barge_in=broken_fence,
    )
    session.feed_pcm(_voice_pcm())
    session.feed_pcm(_voice_pcm())
    # 回退路径是 create_task，需事件循环推进
    await asyncio.sleep(0.02)

    assert session.interrupted_triggered is True
    assert speaking_driver.is_speaking() is False


@pytest.mark.anyio
async def test_vad_interrupt_without_fence_uses_flush_talk(speaking_driver):
    """未提供栅栏回调时保持原有 flush_talk 行为 (兼容)"""
    session = ASRSession(
        session_id="fence_3",
        energy_threshold=200.0,
    )
    session.feed_pcm(_voice_pcm())
    session.feed_pcm(_voice_pcm())
    await asyncio.sleep(0.02)

    assert session.interrupted_triggered is True
    assert speaking_driver.flush_calls >= 1


@pytest.mark.anyio
async def test_vad_interrupt_schedules_coroutine_fence(speaking_driver):
    """协程型栅栏回调被 ensure_future 调度，feed_pcm 不阻塞"""
    fence_started = asyncio.Event()

    async def async_fence():
        fence_started.set()

    session = ASRSession(
        session_id="fence_4",
        energy_threshold=200.0,
        on_barge_in=async_fence,
    )
    session.feed_pcm(_voice_pcm())
    session.feed_pcm(_voice_pcm())

    await asyncio.wait_for(fence_started.wait(), timeout=1.0)
    assert session.interrupted_triggered is True


def test_vad_no_interrupt_when_avatar_silent(speaking_driver):
    """数字人未在说话时 VAD 不触发打断"""
    speaking_driver._speaking = False
    fence_calls = []
    session = ASRSession(
        session_id="fence_5",
        energy_threshold=200.0,
        on_barge_in=lambda: fence_calls.append(1),
    )
    res = session.feed_pcm(_voice_pcm())
    res = session.feed_pcm(_voice_pcm())
    assert "interrupted" not in res["events"]
    assert fence_calls == []


def test_silent_frames_never_trigger_interrupt(speaking_driver):
    """静音帧不触发说话态与打断"""
    fence_calls = []
    session = ASRSession(
        session_id="fence_6",
        energy_threshold=200.0,
        on_barge_in=lambda: fence_calls.append(1),
    )
    for _ in range(5):
        res = session.feed_pcm(_silent_pcm())
        assert res["has_voice"] is False
    assert fence_calls == []
    assert session.interrupted_triggered is False
