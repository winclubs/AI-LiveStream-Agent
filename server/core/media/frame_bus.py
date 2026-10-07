"""
统一视频帧发布总线 (P0 修复：云端渲染帧与本地 shadow 帧的输出一致性)

背景缺陷：云端 sidecar / 远端 Avatar Provider 的帧只投递到虚拟摄像头与 MJPEG 预览，
而本地 procedural shadow 渲染循环从未暂停，导致 RTMP 公网推流、WebRTC 大屏与
本地录制器在云端模式下仍在静默广播 shadow 帧——运营者看预览是云端高清真人画面，
平台观众收到的却是本地 CPU 低质画面，且无任何告警。

本模块抽取「合成一次、扇出多处」的发布逻辑为单一总线，并引入与虚拟摄像头同源的
短租约写入者仲裁：同一时刻只有一个帧源 (云端或本地 shadow) 向全部输出通道发布帧。
云端帧持续到达时租约自动续期，本地 shadow 帧被静默抑制；云端断流后租约迅速过期，
本地 shadow 无缝接管，实现真正的热备兜底。
"""
import logging
import threading
import time
from typing import Optional

logger = logging.getLogger("LiveAgent.FrameBus")


class FramePublishBus:
    """单帧多通道发布总线：合成画层一次，按租约仲裁扇出到全部输出通道。"""

    # 租约时长需略大于两帧间隔 (25fps ≈ 40ms)，容忍云端网络抖动；
    # 过长会导致云端断流后 shadow 接管迟钝，过短会导致帧间误抑制。
    DEFAULT_LEASE_SECONDS = 0.30

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._frame_owner = ""
        self._frame_owner_priority = -1
        self._owner_lease_until = 0.0
        self.total_published = 0
        self.frames_suppressed_by_owner = 0
        # 最近一次实际发布的帧 (供诊断与测试断言)
        self.last_published_frame = None

    # ------------------------------------------------------------------
    # 租约仲裁
    # ------------------------------------------------------------------
    def _acquire_ownership(self, owner: str, priority: int, lease_seconds: float) -> bool:
        """短租约仲裁：高优先级帧源可抢占，同源续期；低优先级在租约期内被抑制。"""
        now = time.monotonic()
        with self._lock:
            owner = str(owner or "default")
            another_owner_active = bool(
                self._frame_owner
                and self._frame_owner != owner
                and now < self._owner_lease_until
            )
            if another_owner_active and priority <= self._frame_owner_priority:
                self.frames_suppressed_by_owner += 1
                return False
            self._frame_owner = owner
            self._frame_owner_priority = int(priority)
            self._owner_lease_until = now + max(0.04, float(lease_seconds))
            return True

    def release_ownership(self, owner: str = "") -> None:
        """显式释放租约 (如云端事务取消/停止)，让 shadow 立即接管。"""
        with self._lock:
            if not owner or self._frame_owner == owner:
                self._frame_owner = ""
                self._frame_owner_priority = -1
                self._owner_lease_until = 0.0

    @property
    def current_owner(self) -> str:
        with self._lock:
            return self._frame_owner

    def get_status(self) -> dict:
        with self._lock:
            return {
                "frame_owner": self._frame_owner,
                "frame_owner_priority": self._frame_owner_priority,
                "owner_lease_until": self._owner_lease_until,
                "total_published": self.total_published,
                "frames_suppressed_by_owner": self.frames_suppressed_by_owner,
            }

    # ------------------------------------------------------------------
    # 帧发布
    # ------------------------------------------------------------------
    def publish_frame(
        self,
        frame_rgb,
        *,
        owner: str = "default",
        priority: int = 0,
        lease_seconds: float = DEFAULT_LEASE_SECONDS,
        frame_index: Optional[int] = None,
        pts_ms: Optional[float] = None,
        sinks: Optional[dict] = None,
        compose: bool = True,
    ) -> tuple[bool, object]:
        """合成画层并扇出到虚拟摄像头 / RTMP / WebRTC / 录制器。

        返回 (published, composed_frame)：
        - published=False 表示被更高优先级帧源抑制，composed_frame 为 None；
        - published=True 时 composed_frame 为实际扇出的合成帧 (调用方可用它编码 JPEG 预览，
          保证预览与 RTMP/摄像头画面完全一致)。
        compose=False 用于调用方已完成画层合成的场景 (如 sidecar 自行合成 latest_jpeg)，
        避免反录播微扰与优惠券画层被叠加两次。
        sinks 可显式传入 {'virtual_cam','rtmp','webrtc','recorder'}，用于驱动实例
        自挂载的输出通道；缺省时使用全局单例。
        """
        if frame_rgb is None:
            return False, None

        if not self._acquire_ownership(owner, priority, lease_seconds):
            return False, None

        self.total_published += 1
        self.last_published_frame = frame_rgb

        # 1. 共享单调时钟打视频 PTS (音画漂移补偿的视频侧锚点)
        try:
            from server.core.media.shared_playback_clock import global_shared_playback_clock
            global_shared_playback_clock.stamp_video(frame_index, pts_ms)
        except Exception:
            pass

        # 2. 合成反录播微扰与优惠券/商品画层 (只合成一次，全部通道共享同一帧)
        publish_frame = frame_rgb
        if compose:
            try:
                from server.core.media.scene_overlay import compose_scene_overlays, global_scene_overlay_state
                composed = compose_scene_overlays(
                    frame_rgb,
                    global_scene_overlay_state.snapshot(),
                    enable_anti_recording=True,
                    timestamp=time.time(),
                )
                if composed is not None:
                    publish_frame = composed
            except Exception:
                pass

        # sinks 语义：sinks=None 时全部通道使用全局单例；显式传入 dict 时，
        # 缺失或 None 的通道表示"跳过"，绝不回退全局 (驱动自挂载语义)。
        explicit_sinks = sinks is not None
        resolved = sinks or {}
        virtual_cam = resolved.get("virtual_cam")
        rtmp_streamer = resolved.get("rtmp")
        webrtc_streamer = resolved.get("webrtc")
        recorder = resolved.get("recorder")

        # 3. 虚拟摄像头 (复用其内部 letterbox 与短租约仲裁)
        try:
            if virtual_cam is None and not explicit_sinks:
                from server.core.media.virtual_cam import global_virtual_cam
                virtual_cam = global_virtual_cam
            if virtual_cam is not None and getattr(virtual_cam, "is_active", False):
                virtual_cam.send_frame(publish_frame, owner=owner, priority=priority)
        except Exception:
            pass

        # 4. RTMP 公网推流 (RGB 原帧)
        try:
            if rtmp_streamer is None and not explicit_sinks:
                from server.core.media.rtmp_streamer import global_rtmp_streamer
                rtmp_streamer = global_rtmp_streamer
            if rtmp_streamer is not None and getattr(rtmp_streamer, "is_streaming", False):
                rtmp_streamer.send_video_frame(publish_frame)
        except Exception:
            pass

        # 5. WebRTC / WHEP 大屏 (BGR)
        try:
            if webrtc_streamer is None and not explicit_sinks:
                from server.core.media.webrtc_streamer import get_webrtc_stream_manager
                webrtc_streamer = get_webrtc_stream_manager()
            if webrtc_streamer is not None:
                bgr_frame = self._to_bgr(publish_frame)
                if bgr_frame is not None:
                    webrtc_streamer.push_frame(bgr_frame)
        except Exception:
            pass

        # 6. 短视频切片录制器 (RGB 原帧)
        try:
            if recorder is None and not explicit_sinks:
                from server.core.media.recorder import get_record_manager
                recorder = get_record_manager()
            if recorder is not None and getattr(recorder, "is_recording", lambda: False)():
                recorder.feed_frame(publish_frame)
        except Exception:
            pass

        return True, publish_frame

    def encode_jpeg(self, frame_rgb, quality: int = 80):
        """把 RGB 帧编码为 JPEG，供 MJPEG 预览端点拉取。失败返回 b"" 。"""
        try:
            import cv2
            bgr = cv2.cvtColor(frame_rgb, cv2.COLOR_RGB2BGR)
            ok, buf = cv2.imencode(".jpg", bgr, [cv2.IMWRITE_JPEG_QUALITY, quality])
            if ok:
                return buf.tobytes()
        except Exception:
            pass
        return b""

    @staticmethod
    def _to_bgr(frame_rgb):
        try:
            import cv2
            return cv2.cvtColor(frame_rgb, cv2.COLOR_RGB2BGR)
        except Exception:
            return None


# 全局单例
global_frame_bus = FramePublishBus()
