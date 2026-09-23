# -*- coding: utf-8 -*-
"""
单元测试：阶段三全双工极速打断、音画时间戳对齐与本地渲染解耦验证
"""
import time
import pytest
from server.core.avatar import AvatarDriverFactory, LocalLiveTalkingDriver, CloudSidecarDriver
from server.core.media.av_sync import global_av_sync


@pytest.mark.anyio
async def test_flush_talk_latency():
    """验证 flush_talk 在 200ms 内瞬间重置发音状态并递增代际"""
    driver = AvatarDriverFactory.create("cloud_sidecar")
    await driver.start()

    # 模拟正在播报
    dummy_pcm = b"\x00\x00" * 320
    await driver.push_audio_chunk(dummy_pcm)
    assert driver.is_speaking() is True
    old_gen = driver._generation

    # 测量 flush_talk 耗时
    t0 = time.perf_counter()
    await driver.flush_talk()
    elapsed_ms = (time.perf_counter() - t0) * 1000.0

    assert elapsed_ms < 200.0, f"打断耗时超过 200ms 阈值: {elapsed_ms}ms"
    assert driver.is_speaking() is False, "打断后必须立刻恢复待机闭嘴"
    assert driver._generation > old_gen, "打断必须递增代际以隔离迟到视频帧"

    await driver.stop()


def test_av_sync_controller():
    """验证音画同步控制器延迟记录与平滑推荐补偿"""
    global_av_sync.reset()
    global_av_sync.record_render_latency(28.5)
    global_av_sync.record_render_latency(31.5)
    global_av_sync.record_tts_latency(120.0)

    status = global_av_sync.get_status()
    assert 25.0 <= status["render_latency_ms"] <= 35.0
    assert status["tts_latency_ms"] == 120.0
    assert status["recommended_delay_ms"] >= 0


@pytest.mark.anyio
async def test_livetalking_dir_decoupling():
    """验证在外部目录完全不存在时，LocalLiveTalkingDriver 也能安全启动不报错"""
    # 传入一个完全不存在的非真实路径
    fake_path = "Z:\\NonExistent\\Path\\To\\LiveTalking"
    driver = AvatarDriverFactory.create("livetalking", {"livetalking_dir": fake_path})
    assert isinstance(driver, LocalLiveTalkingDriver)

    # 启动自检应平滑通过，绝不抛出 FileNotFoundError 或崩溃阻断
    ok = await driver.start()
    assert ok is True
    assert driver.is_active is True

    await driver.stop()
    assert driver.is_active is False
