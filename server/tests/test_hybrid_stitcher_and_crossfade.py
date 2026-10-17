# -*- coding: utf-8 -*-
"""
无缝流缝合调度器与音频 Cross-fade 单元测试
覆盖：
1. apply_audio_crossfade 50ms 线性交叉淡入淡出与消除爆音
2. HybridSliceHandle 切片帧读取与边界保护
3. HybridStitcher 全链路状态机驱动：
   切片播放 -> 弹幕打断 -> 4-step 缓动停顿 -> 实时供帧 -> 答复完毕 -> 4-step 回切缓动 -> 句首重入
4. 断点重入与 SharedPlaybackClock PTS 重新锚定
"""

import numpy as np
import pytest

from server.core.media.hybrid_stitcher import (
    HybridSliceHandle,
    HybridStitcher,
    apply_audio_crossfade,
)
from server.core.media.shared_playback_clock import SharedPlaybackClock
from server.core.media.stitcher_protocol import ResumePolicy, StitcherState


def test_audio_crossfade_properties():
    """测试 50ms 音频交叉淡入淡出的拼接平滑度与极端边界"""
    sr = 16000
    # 两段 1 秒正弦波，频率不同
    t = np.linspace(0, 1, sr, endpoint=False, dtype=np.float32)
    tone_a = np.sin(2 * np.pi * 300 * t).astype(np.float32)
    tone_b = np.sin(2 * np.pi * 600 * t).astype(np.float32)

    # 1. 正常 50ms (800 点) 交叉淡化
    stitched = apply_audio_crossfade(tone_a, tone_b, crossfade_samples=800)
    # 长度为 len(a) + len(b) - 800
    assert len(stitched) == len(tone_a) + len(tone_b) - 800
    assert stitched.dtype == np.float32

    # 2. 空数组容错
    assert len(apply_audio_crossfade(np.zeros(0, dtype=np.float32), tone_b)) == len(tone_b)
    assert len(apply_audio_crossfade(tone_a, np.zeros(0, dtype=np.float32))) == len(tone_a)

    # 3. 极短音频容错
    short_a = np.ones(10, dtype=np.float32)
    short_b = np.ones(10, dtype=np.float32)
    res_short = apply_audio_crossfade(short_a, short_b, crossfade_samples=800)
    # fade 长度受限于 min(800, 10, 10) = 10
    assert len(res_short) == 10 + 10 - 10


def test_hybrid_slice_handle_frame_indexing():
    """测试切片句柄安全帧提取"""
    handle = HybridSliceHandle(
        slice_id="slice_demo",
        product_id="prod_demo",
        slot_code="intro",
        video_path="dummy_path.mp4",
        audio_path="dummy_audio.wav",
        duration_sec=2.0,
        sentence_boundaries=[0.0, 1.2],
    )
    # 手工填充 50 帧测试数据
    handle._frames_cache = [
        np.full((100, 100, 3), i, dtype=np.uint8) for i in range(50)
    ]
    handle._is_loaded = True
    handle.total_frames = 50

    # 提取第 0 帧
    f0 = handle.get_frame(0)
    assert f0 is not None
    assert f0[0, 0, 0] == 0

    # 提取超界帧（保护截断到最后一帧）
    f_last = handle.get_frame(100)
    assert f_last is not None
    assert f_last[0, 0, 0] == 49


def test_hybrid_stitcher_full_lifecycle_interrupt_and_resume():
    """测试切片播放 -> 弹幕打断 -> 4-step 缓动 -> 实时驱动 -> 回切缓动 -> 句首续播"""
    clock = SharedPlaybackClock()
    stitcher = HybridStitcher(clock=clock, blend_total_steps=4)

    # 待机底模
    idle_frame = np.full((100, 100, 3), 10, dtype=np.uint8)

    # 构造测试切片句柄 (时长 4 秒，100 帧，句子边界位于 0.0s 和 2.0s 处)
    slice_handle = HybridSliceHandle(
        slice_id="slice_main",
        product_id="prod_001",
        slot_code="selling_point_1",
        video_path="non_exist.mp4",
        audio_path="non_exist.wav",
        duration_sec=4.0,
        sentence_boundaries=[0.0, 2.0],
    )
    slice_handle._frames_cache = [
        np.full((100, 100, 3), 100 + i, dtype=np.uint8) for i in range(100)
    ]
    slice_handle._is_loaded = True
    slice_handle.total_frames = 100

    stitcher.enqueue_slice(slice_handle)
    assert stitcher.state == StitcherState.PLAYING_BATCH_SLICE
    assert stitcher.current_slice == slice_handle

    # 1. 播放前 60 帧 (达到 2.4 秒，大于第二句开头 2.0s)
    for _ in range(60):
        frame = stitcher.tick_frame(idle_frame)
        assert frame is not None

    assert stitcher.current_frame_idx == 60

    # 2. 观众发送弹幕打断带货切片！
    suspend_ctx = stitcher.trigger_interrupt(resume_policy=ResumePolicy.REWIND_SENTENCE)
    assert suspend_ctx is not None
    assert stitcher.state == StitcherState.INTERRUPTED_HOLDING
    assert suspend_ctx.playback_offset_sec == pytest.approx(2.4, abs=0.05)
    # 根据句子边界 [0.0, 2.0]，回退点应当为 2.0 秒！
    assert suspend_ctx.get_resume_offset_sec() == pytest.approx(2.0, abs=0.05)

    # 3. 推进 4 步 Smoothstep 缓动停顿
    for step in range(3):
        holding_frame = stitcher.tick_frame(idle_frame)
        assert holding_frame is not None
        assert stitcher.state == StitcherState.INTERRUPTED_HOLDING

    # 第 4 步完成，自动转移到 STREAMING_REALTIME
    final_hold_frame = stitcher.tick_frame(idle_frame)
    assert final_hold_frame is not None
    assert stitcher.state == StitcherState.STREAMING_REALTIME

    # 4. 实时神经驱动器答复弹幕，供入实时帧
    realtime_frame = np.full((100, 100, 3), 200, dtype=np.uint8)
    stitcher.feed_realtime_frame(realtime_frame)
    output_rt_frame = stitcher.tick_frame(idle_frame)
    assert np.array_equal(output_rt_frame, realtime_frame)

    # 5. 实时答复完毕，调用 finish_realtime_reply 开始平滑回切切片
    assert stitcher.finish_realtime_reply()
    assert stitcher.state == StitcherState.BLENDING_TRANSITION

    # 验证断点重入帧号已被回退到 2.0 秒 (即第 50 帧，2.0 * 25fps)
    assert stitcher.current_frame_idx == 50

    # 6. 推进 4 步回切缓动
    for step in range(3):
        transition_frame = stitcher.tick_frame(idle_frame)
        assert transition_frame is not None
        assert stitcher.state == StitcherState.BLENDING_TRANSITION

    # 第 4 步缓动完成，平滑过渡结束，状态自动切回 PLAYING_BATCH_SLICE
    blend_final_frame = stitcher.tick_frame(idle_frame)
    assert blend_final_frame is not None
    assert stitcher.state == StitcherState.PLAYING_BATCH_SLICE
    assert stitcher.current_frame_idx == 50

    # 正式从重入断点第 50 帧继续向下推进播放
    resumed_first_slice_frame = stitcher.tick_frame(idle_frame)
    assert resumed_first_slice_frame is not None
    assert stitcher.current_frame_idx == 51
