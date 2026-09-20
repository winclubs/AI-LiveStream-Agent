# -*- coding: utf-8 -*-
"""
单元测试：OBS 虚拟摄像头输出与云端渲染帧画面接管联调 (任务 1.4)
"""
import numpy as np
import pytest

from server.core.media.virtual_cam import VirtualCameraService, letterbox_frame


def test_letterbox_frame():
    """测试不同长宽比下的等比缩放与黑边填充 (letterbox)"""
    # 模拟 720x1280 竖屏图像 (抖音直播常见)
    portrait_frame = np.zeros((1280, 720, 3), dtype=np.uint8)
    portrait_frame[:] = (100, 150, 200)

    # 目标为 1280x720 横屏画布
    fitted = letterbox_frame(portrait_frame, width=1280, height=720)
    assert fitted.shape == (720, 1280, 3)
    # 中心区域应有颜色，两侧应为黑边 (0, 0, 0)
    assert fitted[360, 640, 0] == 100
    assert fitted[360, 10, 0] == 0


def test_virtual_cam_priority_arbitration():
    """测试高优先级云端神经渲染帧抢占本地 shadow 帧"""
    service = VirtualCameraService(width=640, height=360, fps=25)
    
    # 模拟 cam_device
    class MockCamDevice:
        def __init__(self):
            self.last_frame = None
            self.send_count = 0
            self.device = "OBS Virtual Camera"
            self.native_backend = "mock"

        def send(self, frame):
            self.last_frame = frame
            self.send_count += 1

        def close(self):
            pass

    service.cam_device = MockCamDevice()
    service.is_active = True
    service.available = True

    # 1. 本地程序化 shadow 发送帧 (priority=1)
    shadow_frame = np.ones((360, 640, 3), dtype=np.uint8) * 50
    service.send_frame(shadow_frame, owner="procedural_avatar", priority=1)
    assert service.total_frames_sent == 1
    assert service._frame_owner == "procedural_avatar"

    # 2. 云端神经渲染帧进入 (priority=100) -> 瞬间抢占 owner
    neural_frame = np.ones((360, 640, 3), dtype=np.uint8) * 200
    service.send_frame(neural_frame, owner="neural_sidecar", priority=100)
    assert service.total_frames_sent == 2
    assert service._frame_owner == "neural_sidecar"
    assert service._frame_owner_priority == 100

    # 3. 低优先级本地 shadow 试图覆盖 (priority=1) -> 被拒绝丢弃
    service.send_frame(shadow_frame, owner="procedural_avatar", priority=1)
    assert service.total_frames_sent == 2
    assert service.frames_rejected_by_owner == 1

    status = service.get_status()
    assert status["is_active"] is True
    assert status["frames_sent"] == 2
    assert status["frames_rejected_by_owner"] == 1
    assert status["frame_owner"] == "neural_sidecar"

    service.stop()
    assert service.is_active is False
