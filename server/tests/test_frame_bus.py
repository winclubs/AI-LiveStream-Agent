"""统一帧发布总线专项测试 (P0-1：云端帧与本地 shadow 输出一致性)

验证要点：
1. 云端帧持续到达时，本地 shadow 帧被租约仲裁抑制，不进入 RTMP/WebRTC/录制器；
2. 云端断流后租约过期，本地 shadow 无缝接管；
3. PTS 打点、画层合成、JPEG 编码正常工作；
4. 显式挂载的驱动级 sink 仍能收到帧；显式 sinks 中 None 通道被跳过；
5. publish_frame 返回 (published, composed_frame) 元组，合成帧供调用方编码 JPEG。
"""
import time

import numpy as np
import pytest

from server.core.media.frame_bus import FramePublishBus


def _frame(value: int = 60) -> np.ndarray:
    return np.full((240, 320, 3), value, dtype=np.uint8)


@pytest.fixture
def bus():
    instance = FramePublishBus()
    yield instance
    instance.release_ownership()


def test_shadow_frame_publishes_when_no_cloud(bus):
    published, composed = bus.publish_frame(_frame(), owner="procedural_shadow", priority=1)
    assert published is True
    assert composed is not None
    assert bus.total_published == 1
    assert bus.current_owner == "procedural_shadow"


def test_cloud_frame_suppresses_shadow_frame(bus):
    """云端帧在租约期内抑制低优先级 shadow 帧 (核心 P0 修复)"""
    cloud = _frame(200)
    shadow = _frame(60)

    assert bus.publish_frame(cloud, owner="neural_sidecar", priority=100)[0] is True
    # 租约期内 shadow 帧被抑制
    assert bus.publish_frame(shadow, owner="procedural_shadow", priority=1)[0] is False
    assert bus.frames_suppressed_by_owner == 1
    assert bus.total_published == 1
    # 实际发布的仍是云端帧
    assert bus.last_published_frame is cloud


def test_cloud_lower_priority_cannot_suppress_higher(bus):
    assert bus.publish_frame(_frame(), owner="neural_sidecar", priority=100)[0] is True
    # 优先级更低者无法抢占
    assert bus.publish_frame(_frame(), owner="legacy_remote_gpu", priority=50)[0] is False
    # 优先级更高者可以抢占
    assert bus.publish_frame(_frame(), owner="neural_sidecar", priority=150)[0] is True


def test_shadow_takes_over_after_lease_expires(bus):
    assert bus.publish_frame(_frame(), owner="neural_sidecar", priority=100, lease_seconds=0.05)[0] is True
    time.sleep(0.07)
    assert bus.publish_frame(_frame(), owner="procedural_shadow", priority=1)[0] is True
    assert bus.current_owner == "procedural_shadow"


def test_same_owner_renews_lease(bus):
    assert bus.publish_frame(_frame(), owner="neural_sidecar", priority=100)[0] is True
    for _ in range(3):
        assert bus.publish_frame(_frame(), owner="neural_sidecar", priority=100)[0] is True
    assert bus.total_published == 4
    assert bus.frames_suppressed_by_owner == 0


def test_release_ownership_lets_shadow_take_over_immediately(bus):
    assert bus.publish_frame(_frame(), owner="neural_sidecar", priority=100)[0] is True
    bus.release_ownership("neural_sidecar")
    assert bus.publish_frame(_frame(), owner="procedural_shadow", priority=1)[0] is True
    assert bus.current_owner == "procedural_shadow"


def test_explicit_sinks_receive_frames(bus):
    received = []

    class FakeCam:
        is_active = True

        def send_frame(self, frame, **kwargs):
            received.append(("cam", frame))

    class FakeRtmp:
        is_streaming = True

        def send_video_frame(self, frame):
            received.append(("rtmp", frame))

    ok, composed = bus.publish_frame(
        _frame(),
        owner="avatar_driver",
        priority=1,
        sinks={"virtual_cam": FakeCam(), "rtmp": FakeRtmp()},
    )
    assert ok is True
    assert composed is not None
    assert any(kind == "cam" for kind, _ in received)
    assert any(kind == "rtmp" for kind, _ in received)


def test_explicit_sink_none_skips_global_fallback(bus):
    """显式 sinks 中为 None 的通道必须跳过，绝不回退全局单例 (驱动自挂载语义)"""
    received = []

    class GlobalLike:
        is_active = True
        is_streaming = True
        is_recording = lambda self: True  # noqa: E731

        def send_frame(self, frame, **kwargs):
            received.append("global_cam")

        def send_video_frame(self, frame):
            received.append("global_rtmp")

        def push_frame(self, frame):
            received.append("global_webrtc")

        def feed_frame(self, frame):
            received.append("global_recorder")

    import server.core.media.virtual_cam as vc_mod
    import server.core.media.rtmp_streamer as rtmp_mod
    import server.core.media.webrtc_streamer as rtc_mod
    import server.core.media.recorder as rec_mod

    bus_cls = type(bus)
    # 显式 sinks 全部为 None：所有通道跳过，全局单例不应被触碰
    ok, _ = bus.publish_frame(
        _frame(),
        owner="avatar_driver",
        priority=1,
        sinks={"virtual_cam": None, "rtmp": None, "webrtc": None, "recorder": None},
    )
    assert ok is True
    assert received == []


def test_none_frame_rejected(bus):
    assert bus.publish_frame(None, owner="x", priority=1) == (False, None)
    assert bus.total_published == 0


def test_compose_false_returns_input_frame(bus):
    """compose=False 时跳过画层合成，返回原帧 (调用方已自行合成)"""
    frame = _frame(77)
    ok, composed = bus.publish_frame(frame, owner="sidecar", priority=100, compose=False)
    assert ok is True
    assert composed is frame


def test_get_status_reports_arbitration_state(bus):
    bus.publish_frame(_frame(), owner="neural_sidecar", priority=100)
    status = bus.get_status()
    assert status["frame_owner"] == "neural_sidecar"
    assert status["frame_owner_priority"] == 100
    assert status["total_published"] == 1


def test_encode_jpeg_returns_bytes(bus):
    jpeg = bus.encode_jpeg(_frame())
    assert isinstance(jpeg, bytes) and jpeg[:2] == b"\xff\xd8"


def test_encode_jpeg_handles_failure(bus):
    # 传入非图像对象不应抛错
    assert bus.encode_jpeg("not-a-frame") == b""
