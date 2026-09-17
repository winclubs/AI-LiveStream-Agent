# -*- coding: utf-8 -*-
"""
数字人核心演进阶段三全功能验证测试套件 (Phase 3 Test Suite)
覆盖：
1. 平滑镜像索引循环算法 (mirror_index) 数学与边界验证；
2. ActionStateMachine 动作状态机：优先级抢占保护与时钟衰减自动平滑回待机；
3. 双轨驱动触发研判器 (话术关键词匹配与直播间实时场控事件)；
4. 动作切片帧序列加载、Alpha 过渡插值与镜像帧检索；
5. REST API 端点全生命周期：/avatar/actions 查询、配置保存、切片视频上传抽帧、动作调试测试、删除与待机保护；
6. 驱动层与直播路由事件深度联动测试。
"""
import asyncio
import io
import time
from pathlib import Path
import cv2
import numpy as np
import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from server.app import app
from server.database.db import AsyncSessionLocal
from server.database.models import AvatarAction
from server.core.avatar.action_state_machine import (
    ActionStateMachine,
    ActionClip,
    get_action_state_machine,
    mirror_index,
)
from server.core.avatar.drivers import Procedural2DDriver


@pytest.fixture
def anyio_backend():
    return "asyncio"


def _create_mini_mp4_video(output_path: Path, frame_count: int = 8, width: int = 160, height: int = 120) -> Path:
    """生成用于切片测试的合成测试视频"""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    fourcc = cv2.VideoWriter_fourcc(*"mp4v")
    out = cv2.VideoWriter(str(output_path), fourcc, 25.0, (width, height))
    for i in range(frame_count):
        frame = np.full((height, width, 3), 30 + i * 15, dtype=np.uint8)
        cv2.circle(frame, (width // 2, height // 2), 20, (0, 255, 120), -1)
        out.write(frame)
    out.release()
    return output_path


# ============================================================================
# 1. 平滑镜像循环算法数学测试
# ============================================================================
def test_mirror_index_mathematics():
    """验证镜像索引函数消除突变跳变的数学正确性与边界"""
    # 边界情况：长度 <= 1
    assert mirror_index(0, 0) == 0
    assert mirror_index(10, 1) == 0

    # 长度为 5 时 (索引 0, 1, 2, 3, 4)
    # 往返序列期望：0, 1, 2, 3, 4, 3, 2, 1, 0, 1, 2, 3, 4... (周期为 (5-1)*2 = 8)
    expected_sequence = [0, 1, 2, 3, 4, 3, 2, 1, 0, 1, 2, 3, 4, 3, 2, 1]
    actual_sequence = [mirror_index(i, 5) for i in range(16)]
    assert actual_sequence == expected_sequence, f"实际输出: {actual_sequence}"

    # 验证大帧数取模正确，永远在 [0, total_len - 1] 区间内
    for big_idx in [1000, 1001, 999999]:
        idx = mirror_index(big_idx, 10)
        assert 0 <= idx < 10


# ============================================================================
# 2. 动作状态机：优先级抢占与时钟衰减自动复位测试
# ============================================================================
def test_action_state_machine_priority_and_decay():
    """验证动作状态机的高优先级抢占与超时平滑衰减机制"""
    sm = ActionStateMachine()
    # 初始应处于待机动作 0 (P0)
    status = sm.get_status()
    assert status["action_code"] == 0
    assert status["priority"] == 0

    # 1. 触发欢迎动作 (Action 1, P2, 持续 2.0s)
    switched = sm.trigger_action(action_code=1, source="test", duration=2.0, priority=2)
    assert switched is True
    assert sm.current_action == 1
    assert sm.current_priority == 2

    # 2. 低优先级动作尝试打断 (尝试触发 P1 的自定义动作)，应被拦截抢占保护
    switched_low = sm.trigger_action(action_code=99, source="test", duration=2.0, priority=1)
    assert switched_low is False
    assert sm.current_action == 1

    # 3. 高优先级动作打断 (触发 Action 4 大额打赏致谢, P9)
    switched_high = sm.trigger_action(action_code=4, source="test", duration=3.0, priority=9)
    assert switched_high is True
    assert sm.current_action == 4
    assert sm.current_priority == 9

    # 4. 模拟时钟推进衰减
    # 动作 4 持续 3.0s，时钟推前 3.1s 应该触发自动复位回待机态 0
    sm.action_start_time = time.time() - 3.2
    active_code = sm.step_clock()
    assert active_code == 0
    assert sm.current_action == 0
    assert sm.current_priority == 0


# ============================================================================
# 3. 双轨驱动研判器测试 (关键词匹配与场控事件驱动)
# ============================================================================
def test_action_state_machine_dual_triggers():
    """验证主播口播话术文本与实时场控事件的双轨研判精准度"""
    sm = ActionStateMachine()

    # 1. 话术测试：促单逼单触发动作 3
    act_code = sm.evaluate_text("欢迎各位家人们，左下角一号链接大家赶紧去购物车下单，买一送一数量有限！")
    assert act_code == 3
    assert sm.current_action == 3

    # 复位回 0
    sm.reset_to_idle()

    # 2. 话术测试：点赞关注触发动作 2
    act_code = sm.evaluate_text("右上角关注主播不迷路，动动发财的小手给主播点赞双击！")
    assert act_code == 2
    assert sm.current_action == 2

    # 3. 事件测试：大额打赏触发动作 4 (P9 抢占当前动作 2)
    act_code = sm.evaluate_event("gift", {"gift_name": "嘉年华", "combo": 1})
    assert act_code == 4
    assert sm.current_action == 4

    # 4. 低优事件尝试打断高优动作 (进场欢迎 P2 < 致谢 P9)，应被拦截
    act_code = sm.evaluate_event("welcome", {"username": "小李"})
    assert act_code is None
    assert sm.current_action == 4  # 依然保持动作 4


# ============================================================================
# 4. 动作切片帧序列检索与 Alpha 混合平滑过渡测试
# ============================================================================
def test_action_frames_mirror_and_alpha_blending():
    """验证动作切片的镜像读取与动作切换时的 Alpha 过渡混合帧生成"""
    sm = ActionStateMachine()

    # 构造动作 1 的模拟切片帧 (5 帧纯红色)
    clip1 = ActionClip(action_code=1, action_name="测试动作1", duration_sec=5.0, priority=2, mirror_loop=True)
    red_frame = np.zeros((100, 100, 3), dtype=np.uint8)
    red_frame[:, :, 2] = 255  # 纯红
    clip1.frames = [red_frame.copy() for _ in range(5)]
    clip1.total_frames = 5
    sm.clips[1] = clip1

    # 构造动作 2 的模拟切片帧 (5 帧纯绿色)
    clip2 = ActionClip(action_code=2, action_name="测试动作2", duration_sec=5.0, priority=3, mirror_loop=True)
    green_frame = np.zeros((100, 100, 3), dtype=np.uint8)
    green_frame[:, :, 1] = 255  # 纯绿
    clip2.frames = [green_frame.copy() for _ in range(5)]
    clip2.total_frames = 5
    sm.clips[2] = clip2

    # 触发动作 1
    sm.trigger_action(1)
    frame1 = sm.get_current_frame(0)
    assert frame1 is not None
    assert frame1.shape == (100, 100, 3)

    # 切换到动作 2，开始 Alpha 融合阶段
    sm.trigger_action(2)
    assert sm.blend_step < sm.blend_total_steps

    blended = sm.get_current_frame(1)
    assert blended is not None
    # 混合帧应当既有红色分量又有绿色分量 (过渡态)
    assert blended[:, :, 1].mean() > 0
    assert blended[:, :, 2].mean() > 0


# ============================================================================
# 5. REST API 端点闭环测试 (/avatar/actions CRUD 与切片视频上传)
# ============================================================================
@pytest.mark.anyio
async def test_avatar_actions_api_crud_and_upload(tmp_path):
    """测试动作状态机配置列表、创建、上传切片视频、手动触发与删除接口"""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # 1. 查询动作列表
        res = await client.get("/api/v1/anchors/avatar/actions")
        assert res.status_code == 200
        json_data = res.json()
        assert json_data["code"] == 0
        actions = json_data["data"]
        assert len(actions) >= 5  # 默认至少包含 5 组动作

        # 2. 创建或更新自定义动作
        new_action_payload = {
            "id": "act_test_custom_promo",
            "action_code": 10,
            "action_name": "年终大促特惠狂欢",
            "trigger_type": "both",
            "trigger_keywords": "特惠,狂欢,大促",
            "trigger_events": "order",
            "duration_sec": 4.5,
            "priority": 6,
            "mirror_loop": 1,
            "is_active": 1,
        }
        res = await client.post("/api/v1/anchors/avatar/actions", json=new_action_payload)
        assert res.status_code == 200
        save_data = res.json()
        assert save_data["code"] == 0
        assert save_data["data"]["action_code"] == 10

        # 3. 准备微型测试视频并上传切片
        video_path = _create_mini_mp4_video(tmp_path / "action_sample.mp4", frame_count=6)
        with open(video_path, "rb") as f:
            files = {"file": ("action_sample.mp4", f, "video/mp4")}
            res = await client.post(f"/api/v1/anchors/avatar/actions/act_test_custom_promo/upload-clip", files=files)
        assert res.status_code == 200
        upload_data = res.json()
        assert upload_data["code"] == 0
        assert upload_data["data"]["total_frames"] == 6

        # 4. 手动测试触发该动作
        res = await client.post("/api/v1/anchors/avatar/actions/test-trigger", json={"action_code": 10, "duration": 2.0})
        assert res.status_code == 200
        trigger_data = res.json()
        assert trigger_data["code"] == 0
        assert trigger_data["current_status"]["action_code"] == 10

        # 5. 防御性测试：禁止删除默认待机动作 0
        async with AsyncSessionLocal() as db:
            res_idle = await db.execute(select(AvatarAction).where(AvatarAction.action_code == 0))
            idle_act = res_idle.scalars().first()
            if idle_act:
                res = await client.delete(f"/api/v1/anchors/avatar/actions/{idle_act.id}")
                assert res.status_code == 400

        # 6. 删除自定义测试动作
        res = await client.delete("/api/v1/anchors/avatar/actions/act_test_custom_promo")
        assert res.status_code == 200
        del_data = res.json()
        assert del_data["code"] == 0


# ============================================================================
# 6. 驱动层与直播路由联动测试
# ============================================================================
@pytest.mark.anyio
async def test_driver_action_state_machine_integration():
    """验证 Procedural2DDriver 接入 ActionStateMachine 并随渲染推进时钟与状态查询"""
    get_action_state_machine().reset_to_idle()
    driver = Procedural2DDriver()
    await driver.start()

    assert hasattr(driver, "action_state_machine")
    assert driver.action_state_machine is not None

    # 触发动作 3 (促单逼单)
    switched = await driver.set_custom_state(state_code=3, duration=2.5, priority=5)
    assert switched is True
    assert driver.get_current_action() == 3

    # 检查状态机健康度与遥测指标
    status = driver.get_status()
    assert status["action_state"] == 3
    assert status["action_priority"] == 5

    await driver.stop()
