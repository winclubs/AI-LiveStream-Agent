"""
单元测试：低配电脑真人微动态与轻量口型融合渲染器 (RealAvatarLiteRenderer)
"""

import time
import pytest
from server.core.media.real_avatar_lite import RealAvatarLiteRenderer, CV_AVAILABLE

try:
    import numpy as np
except ImportError:
    np = None


def test_real_avatar_lite_initialization():
    renderer = RealAvatarLiteRenderer(width=640, height=480, fps=25)
    assert renderer.width == 640
    assert renderer.height == 480
    assert renderer.fps == 25
    assert renderer.total_frames_rendered == 0


@pytest.mark.skipif(not CV_AVAILABLE, reason="需要 OpenCV 环境")
def test_real_avatar_lite_render_frame_without_landmarks():
    renderer = RealAvatarLiteRenderer(width=360, height=480, fps=25)
    frame = renderer.render_frame(t=0.5, mouth_open=0.0, mouth_form=0.0)
    assert frame is not None
    assert frame.shape == (480, 360, 3)
    assert frame.dtype == np.uint8


@pytest.mark.skipif(not CV_AVAILABLE, reason="需要 OpenCV 环境")
def test_real_avatar_lite_render_frame_with_speaking():
    renderer = RealAvatarLiteRenderer(width=360, height=480, fps=25)
    # 闭嘴状态
    frame_closed = renderer.render_frame(t=1.0, mouth_open=0.0, mouth_form=0.0)
    # 说话张嘴状态
    frame_open = renderer.render_frame(t=1.0, mouth_open=0.8, mouth_form=0.5)
    assert frame_closed is not None
    assert frame_open is not None
    assert frame_open.shape == (480, 360, 3)
    # 闭嘴与张嘴画面像素应该存在形变差异
    diff = np.abs(frame_open.astype(int) - frame_closed.astype(int))
    assert np.sum(diff) > 0


@pytest.mark.skipif(not CV_AVAILABLE, reason="需要 OpenCV 环境")
def test_real_avatar_lite_performance_cpu_benchmark():
    renderer = RealAvatarLiteRenderer(width=360, height=480, fps=25)
    # 预热
    renderer.render_frame(t=0.1, mouth_open=0.5)

    # 连续渲染 25 帧 (相当于 1 秒真实直播输出)，测试纯 CPU 耗时
    t0 = time.perf_counter()
    for i in range(25):
        renderer.render_frame(t=i * 0.04, mouth_open=0.6, mouth_form=0.2)
    total_cost_sec = time.perf_counter() - t0
    avg_frame_ms = (total_cost_sec / 25.0) * 1000.0

    # 纯 CPU 单帧渲染耗时必须显著小于帧间隔 40ms (25fps 门槛)，一般在 1~5ms 之间
    assert avg_frame_ms < 30.0, f"单帧耗时 {avg_frame_ms:.2f}ms 超过低配门槛"
