# -*- coding: utf-8 -*-
"""
验证阶段五音频防查：±1.5% 微变速算法与语气过场词注入系统
"""
import numpy as np

from server.core.avatar.sku_slice_renderer import apply_audio_varispeed
from server.core.avatar.transition_filler import (
    TransitionFillerManager,
    global_transition_filler_manager,
)


def test_audio_varispeed_time_stretch_ratio():
    # 生成 1 秒 16kHz 正弦波音频 (16000 采样点)
    sample_rate = 16000
    t = np.linspace(0, 1.0, sample_rate, endpoint=False)
    sine_wave = (np.sin(2 * np.pi * 440 * t) * 0.5 * 32767.0).astype(np.int16)
    pcm_bytes = sine_wave.tobytes()

    # 1. 恒速测试 (ratio=1.0)
    identity_bytes = apply_audio_varispeed(pcm_bytes, sample_rate=sample_rate, speed_ratio=1.0)
    assert identity_bytes == pcm_bytes

    # 2. 减速 1.5% (ratio=0.985 -> 音频点数应大约增加 1.5%)
    slower_bytes = apply_audio_varispeed(pcm_bytes, sample_rate=sample_rate, speed_ratio=0.985)
    slower_arr = np.frombuffer(slower_bytes, dtype=np.int16)
    expected_slower_len = int(round(16000 / 0.985))
    assert abs(len(slower_arr) - expected_slower_len) <= 5
    assert not np.isnan(slower_arr).any()

    # 3. 加速 1.5% (ratio=1.015 -> 音频点数应大约减少 1.5%)
    faster_bytes = apply_audio_varispeed(pcm_bytes, sample_rate=sample_rate, speed_ratio=1.015)
    faster_arr = np.frombuffer(faster_bytes, dtype=np.int16)
    expected_faster_len = int(round(16000 / 1.015))
    assert abs(len(faster_arr) - expected_faster_len) <= 5
    assert not np.isnan(faster_arr).any()

    # 4. 边界安全：空数据或极短音频
    assert apply_audio_varispeed(b"", speed_ratio=1.015) == b""
    assert apply_audio_varispeed(b"123", speed_ratio=1.015) == b"123"


def test_transition_filler_manager():
    mgr = TransitionFillerManager(injection_probability=0.5, enabled=True)
    assert mgr.enabled is True

    # 禁用状态下绝对不注入
    mgr.enabled = False
    assert mgr.should_inject() is False

    # 100% 概率下必定注入
    mgr.enabled = True
    mgr.injection_probability = 1.0
    assert mgr.should_inject() is True

    # 0% 概率下必定不注入
    mgr.injection_probability = 0.0
    assert mgr.should_inject() is False

    # 测试各个插槽转场文本挑选
    text_intro = mgr.pick_filler_text(from_slot="intro", to_slot="selling_point_1")
    assert len(text_intro) > 0
    assert any(w in text_intro for w in ["干货", "亮点", "划走", "好"])

    text_coupon = mgr.pick_filler_text(from_slot="selling_point_1", to_slot="coupon")
    assert any(w in text_coupon for w in ["福利", "优惠", "机制"])

    # 自定义过场词追加
    mgr.add_custom_filler("pitch_to_coupon", "【测试】老铁们福利直接放出来了！")
    assert "【测试】老铁们福利直接放出来了！" in mgr._custom_library["pitch_to_coupon"]


def test_global_transition_filler_singleton():
    assert global_transition_filler_manager is not None
    assert global_transition_filler_manager.injection_probability == 0.35


def test_slice_handle_loop_varispeed(tmp_path):
    from server.core.media.hybrid_stitcher import HybridSliceHandle

    # 创建测试音频文件 (1 秒 16kHz 正弦波)
    sample_rate = 16000
    t = np.linspace(0, 1.0, sample_rate, endpoint=False)
    sine_wave = (np.sin(2 * np.pi * 440 * t) * 0.5 * 32767.0).astype(np.int16)
    audio_file = tmp_path / "test_slice_audio.pcm"
    audio_file.write_bytes(sine_wave.tobytes())

    handle = HybridSliceHandle(
        slice_id="slice_varispeed_test",
        product_id="sku_01",
        slot_code="selling_point_1",
        video_path=str(tmp_path / "non_exist.mp4"),
        audio_path=str(audio_file),
        duration_sec=1.0,
    )

    # 1. 验证初始比率在合规范围 [0.985, 1.015]
    assert 0.985 <= handle.current_loop_speed_ratio <= 1.015

    # 2. 模拟长时间 7x24 轮播，多次刷新微变速比率
    ratios = [handle.refresh_varispeed_for_new_loop() for _ in range(10)]
    assert all(0.985 <= r <= 1.015 for r in ratios)
    assert len(set(ratios)) > 1  # 验证比率不是常量，每次轮播不同

    # 3. 验证动态微变速拉伸对 PCM 字节流生效
    handle.current_loop_speed_ratio = 0.985
    slower_pcm = handle.get_audio_pcm(dynamic_varispeed=True)
    raw_pcm = handle.get_audio_pcm(dynamic_varispeed=False)
    assert len(slower_pcm) > len(raw_pcm)

    handle.current_loop_speed_ratio = 1.015
    faster_pcm = handle.get_audio_pcm(dynamic_varispeed=True)
    assert len(faster_pcm) < len(raw_pcm)


def test_hybrid_stitcher_transition_hook_and_loop_varispeed(tmp_path):
    from server.core.media.hybrid_stitcher import HybridStitcher, HybridSliceHandle

    frame_a = np.zeros((100, 100, 3), dtype=np.uint8)
    frame_b = np.ones((100, 100, 3), dtype=np.uint8) * 128

    handle_a = HybridSliceHandle(
        slice_id="slice_a",
        product_id="prod_01",
        slot_code="intro",
        video_path=str(tmp_path / "a.mp4"),
        audio_path=str(tmp_path / "a.pcm"),
        duration_sec=0.08,
        preloaded_frames=[frame_a, frame_a],  # 2 帧
    )
    handle_b = HybridSliceHandle(
        slice_id="slice_b",
        product_id="prod_01",
        slot_code="selling_point_1",
        video_path=str(tmp_path / "b.mp4"),
        audio_path=str(tmp_path / "b.pcm"),
        duration_sec=0.08,
        preloaded_frames=[frame_b, frame_b],  # 2 帧
    )

    filler_mgr = TransitionFillerManager(injection_probability=1.0, enabled=True)
    stitcher = HybridStitcher(
        transition_filler_manager=filler_mgr,
        loop_playlist=True,
    )

    hook_calls = []

    def hook(old_slice, next_slice, filler_text):
        hook_calls.append((old_slice.slice_id, next_slice.slice_id, filler_text))

    stitcher.on_transition_hook = hook

    stitcher.enqueue_slice(handle_a)
    stitcher.enqueue_slice(handle_b)

    # 初始状态：正在播放 handle_a
    assert stitcher.current_slice.slice_id == "slice_a"

    # tick 推进第 1 帧
    stitcher.tick_frame()
    # tick 推进第 2 帧（第 2 帧播完切片 a 结束，应触发转场过场词，切到 handle_b）
    stitcher.tick_frame()

    # 验证过场词钩子被调用
    assert len(hook_calls) == 1
    old_id, next_id, filler = hook_calls[0]
    assert old_id == "slice_a"
    assert next_id == "slice_b"
    assert len(filler) > 0

    # 验证当前播发切片已平滑接替为 handle_b
    assert stitcher.current_slice.slice_id == "slice_b"

    # 验证 handle_a 在循环排期入队时已刷新微变速比率
    assert 0.985 <= handle_a.current_loop_speed_ratio <= 1.015

