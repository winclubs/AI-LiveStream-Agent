# -*- coding: utf-8 -*-
"""
数字人驱动架构与多模式工厂单元测试
覆盖注册中心、模式切换 (Local LiveTalking, Cloud Sidecar, Procedural, Mock)、
说话状态与超时回落、瞬间打断 (flush_talk) 以及动作状态机 (set_custom_state)。
"""
import pytest
import time
from server.core.avatar import (
    AvatarDriverFactory,
    LocalLiveTalkingDriver,
    CloudSidecarDriver,
    Procedural2DDriver,
    MockAvatarDriver,
)


@pytest.fixture
def anyio_backend():
    return "asyncio"



@pytest.mark.anyio
async def test_driver_factory_list_and_create():
    """测试工厂驱动列表获取与实例化"""
    types = AvatarDriverFactory.list_available_types()
    assert "livetalking" in types
    assert "cloud_sidecar" in types
    assert "procedural" in types
    assert "mock" in types

    driver = AvatarDriverFactory.create("livetalking", {"avatar_id": "test_avatar"})
    assert isinstance(driver, LocalLiveTalkingDriver)
    assert driver.avatar_id == "test_avatar"


@pytest.mark.anyio
async def test_driver_lifecycle_and_speech_state():
    """测试驱动生命周期、音频流推送与说话状态"""
    driver = AvatarDriverFactory.create("procedural")
    assert isinstance(driver, Procedural2DDriver)
    assert driver.is_active is False
    assert driver.is_speaking() is False

    # 启动
    ok = await driver.start()
    assert ok is True
    assert driver.is_active is True

    # 推送音频帧 -> 说话状态变为 True
    dummy_pcm = b"\x00\x00" * 320
    await driver.push_audio_chunk(dummy_pcm)
    assert driver.is_speaking() is True

    # 瞬间打断 -> 说话状态重置为 False
    await driver.flush_talk()
    assert driver.is_speaking() is False

    # 停止
    await driver.stop()
    assert driver.is_active is False


@pytest.mark.anyio
async def test_cloud_sidecar_driver():
    """测试本地 CPU + 第三方云端显卡模式驱动"""
    driver = AvatarDriverFactory.create("cloud_sidecar", {"sidecar_url": "ws://example.com/sidecar"})
    assert isinstance(driver, CloudSidecarDriver)

    await driver.start()
    assert driver.tunnel_status == "connected"

    # 动作状态切换
    await driver.set_custom_state(3)
    assert driver.get_current_action() == 3

    await driver.stop()
    assert driver.tunnel_status == "disconnected"


@pytest.mark.anyio
async def test_fallback_driver():
    """测试请求未知驱动类型时优雅降级回 procedural 驱动"""
    driver = AvatarDriverFactory.create("unknown_nonexistent_type")
    assert isinstance(driver, Procedural2DDriver)
