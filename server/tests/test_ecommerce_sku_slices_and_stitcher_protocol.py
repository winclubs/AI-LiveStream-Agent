# -*- coding: utf-8 -*-
"""
电商商品切片资产模型与无缝缝合状态机协议单元测试
覆盖：
1. SkuSlice ORM 实体字段定义与基本持久化读写
2. StitcherState 四阶段状态定义与合法转移矩阵
3. SliceSuspendContext 挂起现场与句首回退/顺延重入算法
4. StitcherStateMachine 4-step Smoothstep 缓动与事件监听机制
"""
import pytest
from sqlalchemy import select

from server.core.media.stitcher_protocol import (
    ResumePolicy,
    SliceSuspendContext,
    StitcherState,
    StitcherStateMachine,
    calculate_smoothstep,
)
from server.database.db import AsyncSessionLocal
from server.database.models import Product, SkuSlice


def test_calculate_smoothstep_math_properties():
    """测试 S-Curve 缓动函数 3t^2 - 2t^3 的数学性质与边界截断"""
    # 边界
    assert calculate_smoothstep(0.0) == pytest.approx(0.0)
    assert calculate_smoothstep(1.0) == pytest.approx(1.0)
    # 对称中点
    assert calculate_smoothstep(0.5) == pytest.approx(0.5)
    # 超出边界截断
    assert calculate_smoothstep(-0.5) == pytest.approx(0.0)
    assert calculate_smoothstep(1.5) == pytest.approx(1.0)
    # 单调递增
    prev = -1.0
    for step in range(11):
        val = calculate_smoothstep(step / 10.0)
        assert val >= prev
        prev = val


def test_slice_suspend_context_resume_offset_calculations():
    """测试切片挂起上下文在不同恢复策略下的音视频秒数重入定位算法"""
    # 场景1：默认 REWIND_SENTENCE，带有句子边界
    # 句子断句点位于 0.0, 3.2, 6.8, 10.5 秒
    boundaries = [0.0, 3.2, 6.8, 10.5]
    ctx = SliceSuspendContext(
        slice_id="slice_101",
        product_id="prod_01",
        slot_code="selling_point_1",
        suspended_pts_ms=45000.0,
        playback_offset_sec=7.5,  # 播到 7.5 秒被打断
        duration_sec=12.0,
        remaining_sec=4.5,
        resume_policy=ResumePolicy.REWIND_SENTENCE,
        sentence_boundary_offsets=boundaries,
    )
    # 7.5 秒前最近的句子断句点是 6.8 秒，重播应精确定位到 6.8 秒
    assert ctx.get_resume_offset_sec() == pytest.approx(6.8)

    # 场景2：无句子边界时，回退最多 1.5 秒
    ctx_no_boundary = SliceSuspendContext(
        slice_id="slice_102",
        product_id="prod_01",
        slot_code="intro",
        suspended_pts_ms=10000.0,
        playback_offset_sec=4.0,
        duration_sec=10.0,
        remaining_sec=6.0,
        resume_policy=ResumePolicy.REWIND_SENTENCE,
        sentence_boundary_offsets=[],
    )
    assert ctx_no_boundary.get_resume_offset_sec() == pytest.approx(2.5)

    # 场景3：DROP_CONTINUE 丢弃当前切片，偏移为总时长
    ctx_drop = SliceSuspendContext(
        slice_id="slice_103",
        product_id="prod_01",
        slot_code="coupon",
        suspended_pts_ms=20000.0,
        playback_offset_sec=3.0,
        duration_sec=5.0,
        remaining_sec=2.0,
        resume_policy=ResumePolicy.DROP_CONTINUE,
    )
    assert ctx_drop.get_resume_offset_sec() == pytest.approx(5.0)

    # 场景4：REPLAY_SLICE 从头播放
    ctx_replay = SliceSuspendContext(
        slice_id="slice_104",
        product_id="prod_01",
        slot_code="faq_size",
        suspended_pts_ms=30000.0,
        playback_offset_sec=4.0,
        duration_sec=8.0,
        remaining_sec=4.0,
        resume_policy=ResumePolicy.REPLAY_SLICE,
    )
    assert ctx_replay.get_resume_offset_sec() == pytest.approx(0.0)

    # 场景5：字典序列化健全
    info = ctx.to_dict()
    assert info["slice_id"] == "slice_101"
    assert info["slot_code"] == "selling_point_1"
    assert info["resume_policy"] == "rewind_sentence"
    assert info["resume_offset_sec"] == pytest.approx(6.8)


def test_stitcher_state_machine_transitions_and_blending():
    """测试缝合状态机的合法转移、非法拦截与 4-step 缓动循环"""
    sm = StitcherStateMachine(initial_state=StitcherState.PLAYING_BATCH_SLICE, blend_total_steps=4)
    assert sm.state == StitcherState.PLAYING_BATCH_SLICE

    events_log = []

    def on_state_change(old, new, payload):
        events_log.append((old, new, payload))

    sm.add_listener(on_state_change)

    # 非法跃迁拦截：不能直接从 PLAYING_BATCH_SLICE 跳到 STREAMING_REALTIME (必须先经过 INTERRUPTED_HOLDING 缓动)
    assert not sm.transition_to(StitcherState.STREAMING_REALTIME)
    assert sm.state == StitcherState.PLAYING_BATCH_SLICE

    # 1. 触发弹幕打断
    suspend_ctx = SliceSuspendContext(
        slice_id="slice_001",
        product_id="prod_01",
        slot_code="selling_point_1",
        suspended_pts_ms=12000.0,
        playback_offset_sec=4.2,
        duration_sec=10.0,
        remaining_sec=5.8,
    )
    assert sm.trigger_interrupt(suspend_ctx)
    assert sm.state == StitcherState.INTERRUPTED_HOLDING
    assert sm.active_suspend_context == suspend_ctx
    assert len(events_log) == 1

    # 2. 推进 4-step 打断缓动停顿
    for step in range(1, 4):
        done, alpha = sm.step_holding_blend()
        assert not done
        assert 0.0 < alpha < 1.0
        assert sm.state == StitcherState.INTERRUPTED_HOLDING

    # 第 4 步完成，自动转移到 STREAMING_REALTIME
    done, alpha = sm.step_holding_blend()
    assert done
    assert alpha == pytest.approx(1.0)
    assert sm.state == StitcherState.STREAMING_REALTIME

    # 3. 实时回复完毕，转入过渡回切
    assert sm.finish_realtime_speech()
    assert sm.state == StitcherState.BLENDING_TRANSITION

    # 4. 推进 4-step 回切缓动
    for step in range(1, 4):
        done, alpha = sm.step_transition_blend()
        assert not done
        assert sm.state == StitcherState.BLENDING_TRANSITION

    done, alpha = sm.step_transition_blend()
    assert done
    assert sm.state == StitcherState.PLAYING_BATCH_SLICE

    # 5. 取出挂起上下文，重入播放
    popped = sm.pop_suspend_context()
    assert popped == suspend_ctx
    assert sm.active_suspend_context is None


@pytest.mark.anyio
async def test_sku_slice_orm_model_lifecycle():
    """测试 SkuSlice 数据库模型创建、外键级联与查询"""
    async with AsyncSessionLocal() as session:
        # 先创建一个测试商品
        prod = Product(
            id="test_sku_prod_101",
            sku_code="SKU_TEST_101",
            title="测试动静分离真丝衬衫",
            category="女装",
            original_price=299.0,
            live_price=129.0,
            current_stock=50,
            selling_points='["100%桑蚕丝", "不勾丝不起球"]',
        )
        session.add(prod)
        await session.commit()

        # 创建切片
        slice_item = SkuSlice(
            id="slice_sku_101_intro",
            product_id="test_sku_prod_101",
            slot_code="intro",
            slice_type="main_pitch",
            script_text="欢迎各位新进来的宝子们！今天给大家带来的是我们家镇店之宝桑蚕丝衬衫！",
            video_path="/data/slices/sku_101_intro.mp4",
            audio_path="/data/slices/sku_101_intro.wav",
            duration_sec=6.5,
            loudness_lufs=-16.0,
            bg_noise_profile='{"noise_floor_db": -52.0}',
            render_engine="latentsync_cloud",
            is_active=1,
        )
        session.add(slice_item)
        await session.commit()

        # 查询切片
        result = await session.execute(
            select(SkuSlice).where(SkuSlice.id == "slice_sku_101_intro")
        )
        loaded = result.scalar_one_or_none()
        assert loaded is not None
        assert loaded.slot_code == "intro"
        assert loaded.duration_sec == 6.5
        assert loaded.render_engine == "latentsync_cloud"
        assert loaded.loudness_lufs == -16.0

        # 验证外键级联删除：删除商品后，关联切片自动清空
        await session.delete(prod)
        await session.commit()

        # 再次查询切片应为空
        res_after_cascade = await session.execute(
            select(SkuSlice).where(SkuSlice.id == "slice_sku_101_intro")
        )
        assert res_after_cascade.scalar_one_or_none() is None
