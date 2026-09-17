# -*- coding: utf-8 -*-
"""
server/tests/test_resource_watchdog.py
低配机 CPU/内存自适应降频看门狗与音频保活机制单元测试 (任务 4 契约保障)
"""

import time
import asyncio
import pytest
from unittest.mock import patch, MagicMock

from server.core.monitoring.system_resource_watchdog import (
    SystemResourceWatchdog,
    global_resource_watchdog,
)
from server.adapters.media.musetalk_driver import global_procedural_avatar_driver
from server.adapters.media.media_router import global_media_router


@pytest.fixture(scope="module")
def client():
    from fastapi.testclient import TestClient
    from server.app import app
    with TestClient(app) as c:
        yield c


def test_system_resource_watchdog_overload_and_throttling():
    """验证当 CPU 占用达到 85% 警戒线时，自适应降频看门狗自动将渲染帧率降至 16FPS"""
    fps_updates = []

    def on_fps(fps: int):
        fps_updates.append(fps)

    watchdog = SystemResourceWatchdog(on_fps_change=on_fps)
    assert watchdog.current_fps == 25
    assert watchdog.is_throttled is False

    # 1. 模拟常规 CPU (50%) -> 不触发降频
    asyncio.run(watchdog._evaluate_and_adapt(cpu=50.0, mem=40.0))
    assert watchdog.is_throttled is False
    assert watchdog.current_fps == 25
    assert len(fps_updates) == 0

    # 2. 模拟高负荷过载 (91.5% >= 85%) -> 触发自适应降频
    asyncio.run(watchdog._evaluate_and_adapt(cpu=91.5, mem=45.0))
    assert watchdog.is_throttled is True
    assert watchdog.current_fps == 16
    assert watchdog.throttle_count == 1
    assert fps_updates == [16]

    # 3. 再次高负荷，状态维持降频，不重复触发无效回调
    asyncio.run(watchdog._evaluate_and_adapt(cpu=88.0, mem=46.0))
    assert watchdog.is_throttled is True
    assert watchdog.current_fps == 16
    assert len(fps_updates) == 1


def test_system_resource_watchdog_recovery_smoothing():
    """验证当 CPU 负荷回落时，需连续 3 次采样稳定 (<70%) 才平滑恢复 25FPS (防抖)"""
    fps_updates = []

    def on_fps(fps: int):
        fps_updates.append(fps)

    watchdog = SystemResourceWatchdog(on_fps_change=on_fps)
    # 预置为降频状态
    asyncio.run(watchdog._evaluate_and_adapt(cpu=90.0, mem=50.0))
    assert watchdog.is_throttled is True
    assert watchdog.current_fps == 16

    # 第 1 次低负荷 (65% < 70%) -> 仍在防抖缓冲，不立即恢复
    asyncio.run(watchdog._evaluate_and_adapt(cpu=65.0, mem=50.0))
    assert watchdog.is_throttled is True
    assert watchdog.current_fps == 16

    # 第 2 次低负荷 (62% < 70%) -> 仍保持防抖
    asyncio.run(watchdog._evaluate_and_adapt(cpu=62.0, mem=50.0))
    assert watchdog.is_throttled is True
    assert watchdog.current_fps == 16

    # 第 3 次低负荷 (58% < 70%) -> 达到 RECOVERY_STABLE_CYCLES (3)，平滑恢复 25FPS
    asyncio.run(watchdog._evaluate_and_adapt(cpu=58.0, mem=50.0))
    assert watchdog.is_throttled is False
    assert watchdog.current_fps == 25
    assert watchdog.recovery_count == 1
    assert fps_updates[-1] == 25


def test_system_resource_watchdog_memory_gc_and_status():
    """验证内存达到警戒线时触发 GC，以及状态字典快照格式完整性"""
    watchdog = SystemResourceWatchdog()
    initial_gc = watchdog.last_gc_at

    # 模拟内存使用达到 90% (>= 88%)
    asyncio.run(watchdog._evaluate_and_adapt(cpu=40.0, mem=90.0))
    assert watchdog.last_gc_at >= initial_gc

    status = watchdog.get_status()
    assert "current_fps" in status
    assert "is_throttled" in status
    assert "last_cpu_percent" in status
    assert "last_mem_percent" in status
    assert "throttle_count" in status
    assert "overload_threshold" in status
    assert status["overload_threshold"] == 85.0


def test_system_resource_watchdog_boost_priority_safe():
    """验证 boost_audio_process_priority 在各种平台上安全执行不抛致命异常"""
    watchdog = SystemResourceWatchdog()
    res = watchdog.boost_audio_process_priority()
    assert isinstance(res, bool)


def test_musetalk_driver_dynamic_fps_integration():
    """验证数字人媒体驱动 set_target_fps 与 media_router 联动生效且范围有界"""
    # 1. 直接设置驱动帧率
    new_fps = global_procedural_avatar_driver.set_target_fps(16)
    assert new_fps == 16
    assert global_procedural_avatar_driver.fps == 16

    # 2. 越界保护 (下限 10, 上限 60)
    assert global_procedural_avatar_driver.set_target_fps(5) == 10
    assert global_procedural_avatar_driver.set_target_fps(100) == 60

    # 恢复基准
    global_procedural_avatar_driver.set_target_fps(25)
    assert global_procedural_avatar_driver.fps == 25

    # 3. 通过媒体路由中枢下发
    routed_fps = global_media_router.set_render_fps(18)
    assert routed_fps == 18
    assert global_procedural_avatar_driver.fps == 18

    # 恢复
    global_media_router.set_render_fps(25)


def test_media_status_contains_resource_watchdog(client):
    """验证 GET /api/v1/live/media/status 诊断接口包含 resource_watchdog 实时指标"""
    resp = client.get("/api/v1/live/media/status")
    assert resp.status_code == 200
    data = resp.json()
    assert data["code"] == 0
    media_data = data["data"]
    assert "resource_watchdog" in media_data
    rw = media_data["resource_watchdog"]
    assert "current_fps" in rw
    assert "overload_threshold" in rw
    assert rw["overload_threshold"] == 85.0
