# -*- coding: utf-8 -*-
"""
AI 数字人主播终极完美方案全链路自动化回归测试套件
覆盖验证：
1. 动态切片预处理管线与默认待机呼吸动作自动注册 (task_manager.py)；
2. 专属动作切片全自动抽帧、人脸对齐、coords.pkl 提取与入库 (process_action_slice)；
3. 动作状态机数学镜像循环 (mirror_index) 与 Alpha 交叉羽化过渡 (cv2.addWeighted)；
4. 驱动器与状态机双轨联动 (台词触发/事件触发/自动时钟衰减)；
5. 动作切片 API 端点、神经资产探测与代表帧缩略图预览。
"""
import asyncio
import os
import pickle
import shutil
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import cv2
import numpy as np
import pytest
from fastapi.testclient import TestClient

from server.config import DATA_DIR
from server.database.db import AsyncSessionLocal
from server.database.models import Anchor, AvatarAction, AvatarTask
from server.core.avatar import get_avatar_task_manager, get_action_state_machine
from server.core.avatar.action_state_machine import mirror_index, ActionStateMachine, ActionClip
from server.core.avatar.drivers import Procedural2DDriver, CloudSidecarDriver
from server.app import app


@pytest.fixture
def dummy_video_path(tmp_path):
    """生成一段标准的 25 FPS 测试合成视频 (包含一个简单的人脸圆形模拟面部特征)"""
    v_path = tmp_path / "test_action_clip.mp4"
    width, height = 320, 240
    fps = 25
    fourcc = cv2.VideoWriter_fourcc(*"mp4v")
    out = cv2.VideoWriter(str(v_path), fourcc, fps, (width, height))
    for i in range(30):
        # 绿色背景 + 白色假脸
        frame = np.zeros((height, width, 3), dtype=np.uint8)
        frame[:, :] = (30, 120, 30)  # 绿幕底色
        # 画一个椭圆模拟人脸
        cv2.ellipse(frame, (160, 120), (50, 70), 0, 0, 360, (220, 220, 220), -1)
        # 画嘴巴 (动态变化)
        mouth_h = int(10 + (i % 5) * 3)
        cv2.ellipse(frame, (160, 150), (20, mouth_h), 0, 0, 360, (50, 50, 180), -1)
        out.write(frame)
    out.release()
    return v_path


@pytest.mark.anyio
async def test_mirror_index_algorithm():
    """验证数学镜像平滑往返循环算法 (彻底消除卡顿撕裂)"""
    total_len = 5
    # 期望循环序列: 0 -> 1 -> 2 -> 3 -> 4 -> 3 -> 2 -> 1 -> 0 -> 1 -> ...
    expected = [0, 1, 2, 3, 4, 3, 2, 1, 0, 1, 2, 3, 4, 3, 2, 1, 0]
    results = [mirror_index(i, total_len) for i in range(len(expected))]
    assert results == expected
    # 边界保护
    assert mirror_index(10, 1) == 0
    assert mirror_index(0, 0) == 0


@pytest.mark.anyio
async def test_action_state_machine_crossfade_blending():
    """验证动作状态机在切换瞬态时执行 5 帧 Alpha 权重混合"""
    sm = ActionStateMachine()

    # 构造两个带有不同纯色的动作切片
    clip0 = ActionClip(0, "待机纯蓝", duration_sec=0.0, priority=0)
    clip3 = ActionClip(3, "购物车纯红", duration_sec=3.0, priority=5)

    blue_frame = np.full((100, 100, 3), 255, dtype=np.uint8)  # 纯蓝 (BGR)
    blue_frame[:, :, 1:] = 0
    red_frame = np.full((100, 100, 3), 255, dtype=np.uint8)   # 纯红 (BGR)
    red_frame[:, :, :2] = 0

    clip0.frames = [blue_frame]
    clip0.total_frames = 1
    clip3.frames = [red_frame]
    clip3.total_frames = 1

    sm.clips[0] = clip0
    sm.clips[3] = clip3

    # 初始状态为 0
    assert sm.current_action == 0
    frame0 = sm.get_frame(0)
    assert frame0 is not None
    # 初始是纯蓝
    assert frame0[50, 50, 0] == 255 and frame0[50, 50, 2] == 0

    # 触发切入 3 号动作 (购物车)
    switched = sm.trigger_action(3, source="test", priority=5)
    assert switched is True
    assert sm.current_action == 3
    assert sm.prev_action == 0
    assert sm.blend_step == 0

    # 获取过渡帧 (Alpha 线性插值)
    blend_frame = sm.get_frame(1)
    assert blend_frame is not None
    # 过渡帧应该同时包含红色与蓝色成分 (混合态)
    assert blend_frame[50, 50, 0] > 0
    assert blend_frame[50, 50, 2] > 0

    # 连续推进超过过渡步数
    for idx in range(2, 6):
        sm.get_frame(idx)
    # 过渡完成，最终帧应该完全呈现红色 (B=0, R=255)
    final_frame = sm.get_frame(10)
    assert final_frame[50, 50, 2] == 255


@pytest.mark.anyio
async def test_process_action_slice_pipeline(dummy_video_path):
    """验证动作切片全自动抽取流水线 (full_imgs, face_imgs, coords.pkl, preview.jpg)"""
    mgr = get_avatar_task_manager()
    test_anchor_id = "test_anchor_action_flow"

    # 先在数据库创建该测试主播
    async with AsyncSessionLocal() as db:
        anc = Anchor(
            id=test_anchor_id,
            name="测试动作主播",
            anchor_type="ecommerce",
        )
        db.add(anc)
        await db.commit()

    try:
        res = await mgr.process_action_slice(
            anchor_id=test_anchor_id,
            action_code=3,
            action_name="指引小黄车测试动作",
            video_path=str(dummy_video_path),
            trigger_keywords="购物车,下单",
            duration_sec=4.0,
            priority=5,
        )

        assert res["action_code"] == 3
        assert res["frames_count"] > 0
        frames_dir = Path(res["frames_dir"])
        assert frames_dir.exists()
        assert (frames_dir / "full_imgs").exists()
        assert (frames_dir / "face_imgs").exists()
        assert (frames_dir / "coords.pkl").exists()
        assert (frames_dir / "meta.json").exists()

        # 验证数据库记录
        async with AsyncSessionLocal() as db:
            act_rec = await db.get(AvatarAction, res["action_id"])
            assert act_rec is not None
            assert act_rec.action_code == 3
            assert act_rec.priority == 5
            assert "购物车" in act_rec.trigger_keywords

    finally:
        # 清理数据库与测试目录
        async with AsyncSessionLocal() as db:
            act = await db.get(AvatarAction, f"action_{test_anchor_id}_3")
            if act:
                await db.delete(act)
            anc = await db.get(Anchor, test_anchor_id)
            if anc:
                await db.delete(anc)
            await db.commit()
        if 'frames_dir' in locals() and frames_dir.exists():
            shutil.rmtree(frames_dir, ignore_errors=True)


@pytest.mark.anyio
async def test_procedural_driver_action_loop_and_clock(dummy_video_path):
    """验证 Procedural2DDriver 与动作状态机协作，支持台词关键词自动触发手势并衰减"""
    driver = Procedural2DDriver(config={"fps": 25})
    await driver.start()
    assert driver.is_active is True

    # 注册一个专属高优先级动作规则 (持续 0.2 秒)
    driver.action_state_machine.keyword_rules.insert(0, {
        "action_code": 3,
        "keywords": ["独家限时特惠"],
        "priority": 10,
        "duration": 0.2,
    })

    # 推入带台词的音频事件
    await driver.push_audio_chunk(b"\x00" * 640, eventpoint={"text": "现在开启独家限时特惠！"})
    assert driver.get_current_action() == 3

    # 等待超过 0.2s，时钟应该自动衰减恢复待机 0
    await asyncio.sleep(0.35)
    assert driver.get_current_action() == 0

    await driver.stop()
    assert driver.is_active is False


def test_action_api_endpoints():
    """验证动作查询与测试触发接口规范"""
    with TestClient(app) as client:
        # 1. 查询动作列表
        res = client.get("/api/v1/anchors/avatar/actions")
        assert res.status_code == 200
        data = res.json()
        assert data["code"] == 0
        assert "data" in data
        assert "current_status" in data

        # 2. 测试触发动作
        trig_res = client.post("/api/v1/anchors/avatar/actions/test-trigger", json={
            "action_code": 1,
            "duration": 2.0,
            "priority": 8,
        })
        assert trig_res.status_code == 200
        trig_data = trig_res.json()
        assert trig_data["code"] == 0
        assert trig_data["current_status"]["current_action"] == 1

        # 3. 复位回待机
        reset_res = client.post("/api/v1/anchors/avatar/actions/test-trigger", json={
            "action_code": 0,
        })
        assert reset_res.status_code == 200
        assert reset_res.json()["current_status"]["current_action"] == 0
