# -*- coding: utf-8 -*-
"""
数字人核心演进阶段一全功能验证测试套件 (Phase 1 Test Suite)
覆盖：
1. 驱动注册中心与工厂创建各种驱动 (Local/Cloud/Procedural/Mock)；
2. 虚拟摄像头 (OBS Virtual Camera) 与 RTMP 推流模块绑定与帧投递；
3. flush_talk 瞬间打断与 is_speaking 状态查询 API；
4. 带货商品话术/促单逼单动作切换联动。
"""
import pytest
import numpy as np
from unittest.mock import MagicMock, AsyncMock, patch
from httpx import AsyncClient, ASGITransport

from server.app import app
from server.core.avatar import (
    BaseAvatarDriver,
    AvatarDriverFactory,
    get_active_avatar_driver,
    set_active_avatar_driver,
    Procedural2DDriver,
    MockAvatarDriver,
    LocalLiveTalkingDriver,
    CloudSidecarDriver,
)


@pytest.fixture
def anyio_backend():
    return "asyncio"



@pytest.mark.anyio
async def test_avatar_driver_stream_attachments():
    """测试 BaseAvatarDriver 挂载虚拟摄像头与 RTMP 并分发音画帧"""
    driver = MockAvatarDriver({"auto_bind_streams": False})

    # 模拟虚拟摄像头与 RTMP 直推服务
    mock_cam = MagicMock()
    mock_cam.is_active = True
    mock_cam.send_frame = MagicMock()

    mock_rtmp = MagicMock()
    mock_rtmp.is_streaming = True
    mock_rtmp.send_video_frame = MagicMock()
    mock_rtmp.send_audio_pcm = MagicMock()

    driver.attach_virtual_cam(mock_cam)
    driver.attach_rtmp_streamer(mock_rtmp)

    dummy_frame = np.zeros((720, 1280, 3), dtype=np.uint8)
    dummy_pcm = b"\x00\x00" * 320  # 20ms pcm

    # 投递音画帧
    dispatched = driver.publish_frame(dummy_frame, dummy_pcm)
    assert dispatched is True
    assert driver.total_published_frames == 1
    assert driver.total_audio_bytes == len(dummy_pcm)

    mock_cam.send_frame.assert_called_once()
    mock_rtmp.send_video_frame.assert_called_once()
    mock_rtmp.send_audio_pcm.assert_called_once_with(dummy_pcm)

    # 验证状态查询
    status = driver.get_status()
    assert status["virtual_cam_active"] is True
    assert status["rtmp_streaming"] is True
    assert status["published_frames"] == 1


@pytest.mark.anyio
async def test_avatar_auto_bind_defaults():
    """测试四大驱动在启动时自动发现并绑定输出通道"""
    for mode in ["livetalking", "cloud_sidecar", "procedural", "mock"]:
        drv = AvatarDriverFactory.create_driver(mode, {"auto_bind_streams": True})
        await drv.start()
        assert drv.is_active is True
        # 应该已成功挂载输出属性 (即使物理设备未启动，属性也已安全绑定)
        assert hasattr(drv, "virtual_cam")
        assert hasattr(drv, "rtmp_streamer")
        await drv.stop()
        assert drv.is_active is False


@pytest.mark.anyio
async def test_virtual_cam_endpoints():
    """测试 /api/v1/live/virtual-cam/* 端点"""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # 查询状态
        res = await client.get("/api/v1/live/virtual-cam/status")
        assert res.status_code == 200
        data = res.json()
        assert data["code"] == 0
        assert "is_active" in data["data"]

        # 启动虚拟摄像头
        res_start = await client.post(
            "/api/v1/live/virtual-cam/start",
            json={"width": 1280, "height": 720, "fps": 25}
        )
        assert res_start.status_code == 200

        # 停止虚拟摄像头
        res_stop = await client.post("/api/v1/live/virtual-cam/stop")
        assert res_stop.status_code == 200
        assert res_stop.json()["code"] == 0


@pytest.mark.anyio
async def test_avatar_speaking_and_flush_talk_endpoints():
    """测试 /api/v1/live/is-speaking 与 /api/v1/live/avatar/flush-talk 端点"""
    test_driver = MockAvatarDriver()
    await test_driver.start()
    set_active_avatar_driver(test_driver)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # 初始未发声
        res = await client.get("/api/v1/live/is-speaking")
        assert res.status_code == 200
        data = res.json()["data"]
        assert data["is_speaking"] is False

        # 推送音频触发发声
        await test_driver.push_audio_chunk(b"dummy_pcm_audio")
        res_speaking = await client.get("/api/v1/live/is-speaking")
        assert res_speaking.json()["data"]["is_speaking"] is True

        # 调用 flush-talk 极速打断
        res_flush = await client.post("/api/v1/live/avatar/flush-talk")
        assert res_flush.status_code == 200
        assert res_flush.json()["code"] == 0

        # 打断后立即恢复静音/待机
        res_after = await client.get("/api/v1/live/is-speaking")
        assert res_after.json()["data"]["is_speaking"] is False


@pytest.mark.anyio
async def test_avatar_action_state_machine_endpoint():
    """测试 /api/v1/live/avatar/action 切换动作切片状态机"""
    test_driver = MockAvatarDriver()
    await test_driver.start()
    set_active_avatar_driver(test_driver)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # 切换至促单逼单指引购物车动作 (3)
        res = await client.post("/api/v1/live/avatar/action", json={"action_code": 3})
        assert res.status_code == 200
        assert res.json()["code"] == 0
        assert test_driver.get_current_action() == 3

        # 切换至打赏致谢动作 (4)
        res2 = await client.post("/api/v1/live/avatar/action", json={"action_code": 4})
        assert res2.status_code == 200
        assert test_driver.get_current_action() == 4

        # 恢复呼吸待机 (0)
        res0 = await client.post("/api/v1/live/avatar/action", json={"action_code": 0})
        assert res0.status_code == 200
        assert test_driver.get_current_action() == 0


@pytest.mark.anyio
async def test_ecommerce_words_trigger_action_state():
    """测试商品带货话术智能研判自动触发数字人动作状态码"""
    from server.routes.live import global_live_controller

    test_driver = MockAvatarDriver()
    await test_driver.start()
    set_active_avatar_driver(test_driver)

    # 模拟角色
    mock_role = MagicMock()
    mock_role.role_name = "金牌带货主播"
    mock_role.role_type = "ecommerce"

    # 模拟合成包含“购物车抢购”的促单话术
    with patch.object(global_live_controller, "_collect_tts_sentence", new_callable=AsyncMock) as mock_tts:
        mock_tts.return_value = b"\x00" * 640
        await global_live_controller._speak_sentence(
            "各位家人们，左下角1号链接购物车赶紧下单抢购，手慢无！",
            mock_role
        )

    # 验证数字人动作已智能流转至促单动作 (3)
    assert test_driver.get_current_action() == 3
