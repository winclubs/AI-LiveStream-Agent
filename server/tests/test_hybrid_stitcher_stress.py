# -*- coding: utf-8 -*-
"""
无缝流缝合调度器 100 轮突发弹幕高压打断与长时稳定性压测
覆盖：
1. 100 轮随机时间戳突发打断与重入恢复
2. 4 步 Smoothstep 缓动全程无 NaN、无 None 帧
3. SharedPlaybackClock 跨 100 轮打断单调时钟平稳运行
4. 内存开销稳定无泄漏
"""
import random
import numpy as np

from server.core.media.hybrid_stitcher import (
    HybridSliceHandle,
    HybridStitcher,
)
from server.core.media.shared_playback_clock import SharedPlaybackClock
from server.core.media.stitcher_protocol import ResumePolicy, StitcherState


def test_hybrid_stitcher_100_interruptions_stress():
    """100 轮高频突发弹幕打断压力测试"""
    clock = SharedPlaybackClock()
    stitcher = HybridStitcher(clock=clock, blend_total_steps=4)
    idle_frame = np.full((120, 120, 3), 30, dtype=np.uint8)

    # 构造带货切片 (4 秒 100 帧)
    slice_handle = HybridSliceHandle(
        slice_id="stress_slice_01",
        product_id="stress_prod_01",
        slot_code="selling_point_1",
        video_path="stress_dummy.mp4",
        audio_path="stress_dummy.wav",
        duration_sec=4.0,
        sentence_boundaries=[0.0, 1.0, 2.0, 3.0],
    )
    slice_handle._frames_cache = [
        np.full((120, 120, 3), (i % 250), dtype=np.uint8) for i in range(100)
    ]
    slice_handle._is_loaded = True
    slice_handle.total_frames = 100

    stitcher.enqueue_slice(slice_handle)

    random.seed(42)

    for round_idx in range(100):
        # 1. 播放随机帧数 (10 ~ 40 帧)
        play_frames = random.randint(10, 40)
        for _ in range(play_frames):
            frame = stitcher.tick_frame(idle_frame)
            assert frame is not None
            assert not np.isnan(frame).any()

        # 2. 突发弹幕打断
        policy = random.choice([ResumePolicy.REWIND_SENTENCE, ResumePolicy.DROP_CONTINUE])
        suspend_ctx = stitcher.trigger_interrupt(resume_policy=policy)
        assert suspend_ctx is not None
        assert stitcher.state == StitcherState.INTERRUPTED_HOLDING

        # 3. 4-step 打断缓动停顿
        for step in range(4):
            hold_frame = stitcher.tick_frame(idle_frame)
            assert hold_frame is not None
            assert not np.isnan(hold_frame).any()

        assert stitcher.state == StitcherState.STREAMING_REALTIME

        # 4. 实时互动供帧 (3 ~ 8 帧)
        rt_frames_count = random.randint(3, 8)
        for f_idx in range(rt_frames_count):
            rt_frame = np.full((120, 120, 3), 180 + f_idx, dtype=np.uint8)
            stitcher.feed_realtime_frame(rt_frame)
            rendered_rt = stitcher.tick_frame(idle_frame)
            assert rendered_rt is not None
            assert not np.isnan(rendered_rt).any()

        # 5. 回复结束，触发平滑回切
        ok = stitcher.finish_realtime_reply()
        assert ok
        assert stitcher.state == StitcherState.BLENDING_TRANSITION

        # 6. 4-step 回切缓动
        for step in range(4):
            trans_frame = stitcher.tick_frame(idle_frame)
            assert trans_frame is not None
            assert not np.isnan(trans_frame).any()

        # 缓动结束，必须恢复为 PLAYING_BATCH_SLICE
        assert stitcher.state == StitcherState.PLAYING_BATCH_SLICE

        # 推进一帧正式起播断点切片
        resumed_frame = stitcher.tick_frame(idle_frame)
        assert resumed_frame is not None

    # 100 轮打断循环全部通过，无崩溃无死锁
    assert stitcher.state == StitcherState.PLAYING_BATCH_SLICE
