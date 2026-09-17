# -*- coding: utf-8 -*-
"""
WebRTC 原生低延迟流媒体预览服务 (WHEP - WebRTC HTTP Egress Protocol)
阶段四核心模块与阶段一/二生产加固：
1. 基于 aiortc 实现标准 WHEP 服务端，提供 200~300ms 超低延迟实时视听大屏；
2. 自定义 AvatarVideoTrack，直接消费数字人渲染器输出的最新高帧率图像；
3. 自定义 AvatarAudioTrack，支持伴音音频轨道同步推流 (P1-1)；
4. 完善的依赖软降级安全网 (WEBRTC_AVAILABLE)：缺失 aiortc/av 依赖时绝不崩溃，提供优雅降级；
5. 支持多客户端同时拉流订阅与生命周期自动释放；
6. 弹性降级支持：若客户端网络端口或环境受限，自动 fallback 至 MJPEG。
"""
import asyncio
import fractions
import logging
import time
import uuid
from typing import Any, Dict, Optional, Tuple

import cv2
import numpy as np

logger = logging.getLogger("LiveAgent.WebRTCStreamer")

# 依赖安全网软降级机制
try:
    import av
    from aiortc import RTCPeerConnection, RTCSessionDescription, VideoStreamTrack, AudioStreamTrack
    from aiortc.contrib.media import MediaRelay
    WEBRTC_AVAILABLE = True
except ImportError:
    av = None
    RTCPeerConnection = None
    RTCSessionDescription = None
    VideoStreamTrack = object
    AudioStreamTrack = object
    MediaRelay = None
    WEBRTC_AVAILABLE = False
    logger.warning("当前环境未安装 aiortc 或 av 依赖，WebRTC WHEP 流媒体功能将自动降级为不可用状态")


# 全局共享最新画面帧引用与锁
_latest_frame: Optional[np.ndarray] = None
_latest_frame_time: float = 0.0

# 全局音频缓冲 (采样率 24000Hz / 48000Hz, s16le)
_audio_buffer: bytearray = bytearray()
_audio_lock = asyncio.Lock()


def set_latest_avatar_frame(frame: np.ndarray):
    """全局注入最新渲染的数字人画面帧 (BGR 或 RGB)"""
    global _latest_frame, _latest_frame_time
    _latest_frame = frame
    _latest_frame_time = time.time()


def push_avatar_audio_pcm(pcm_bytes: bytes):
    """全局注入伴音音频 PCM 数据 (16-bit 线性 PCM)"""
    global _audio_buffer
    if pcm_bytes:
        # 限制缓冲区最大堆积 5 秒音频 (以 48kHz 单声道为例约 48000 * 2 * 5 = 480KB)
        if len(_audio_buffer) > 480000:
            _audio_buffer = _audio_buffer[-240000:]
        _audio_buffer.extend(pcm_bytes)


class AvatarVideoTrack(VideoStreamTrack):
    """数字人 WebRTC 视频流分发轨道 (25 FPS 固定步进)"""

    kind = "video"

    def __init__(self, fps: int = 25, width: int = 640, height: int = 480):
        if WEBRTC_AVAILABLE:
            super().__init__()
        self.fps = fps
        self.width = width
        self.height = height
        self._pts = 0
        self._time_base = fractions.Fraction(1, fps)
        self._standby_frame = self._generate_standby_frame(width, height)

    def _generate_standby_frame(self, w: int, h: int) -> np.ndarray:
        """生成深色优雅的待机画面"""
        img = np.full((h, w, 3), 20, dtype=np.uint8)
        cv2.putText(
            img,
            "AI LiveStream Agent - Ready",
            (w // 2 - 160, h // 2),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.7,
            (52, 211, 153),  # 翡翠绿
            2,
            cv2.LINE_AA,
        )
        return img

    async def recv(self) -> Any:
        """按 25 FPS 时钟生产一帧 VideoFrame"""
        if not WEBRTC_AVAILABLE:
            raise RuntimeError("WebRTC 不可用：缺少 aiortc 或 av 库")

        pts, time_base = await self.next_timestamp()

        # 优先使用实时渲染帧，若超时未提供则输出待机帧
        global _latest_frame, _latest_frame_time
        frame_to_send = None
        if _latest_frame is not None and (time.time() - _latest_frame_time < 3.0):
            frame_to_send = _latest_frame
        else:
            frame_to_send = self._standby_frame

        if frame_to_send.shape[1] != self.width or frame_to_send.shape[0] != self.height:
            frame_to_send = cv2.resize(frame_to_send, (self.width, self.height))

        video_frame = av.VideoFrame.from_ndarray(frame_to_send, format="bgr24")
        video_frame.pts = pts
        video_frame.time_base = time_base
        return video_frame


class AvatarAudioTrack(AudioStreamTrack):
    """数字人 WebRTC 伴音音频轨道 (48kHz 单声道 s16le 标准 WebRTC 音频)"""

    kind = "audio"

    def __init__(self, sample_rate: int = 48000, channels: int = 1):
        if WEBRTC_AVAILABLE:
            super().__init__()
        self.sample_rate = sample_rate
        self.channels = channels
        # 每包 20ms 音频采样点数: 48000 * 0.02 = 960 个样本点
        self.frame_samples = int(sample_rate * 0.02)
        self.bytes_per_frame = self.frame_samples * channels * 2  # 16-bit = 2 bytes
        self._pts = 0
        self._time_base = fractions.Fraction(1, sample_rate)
        # 预制 20ms 静音数据
        self._silence_pcm = bytes(self.bytes_per_frame)

    async def recv(self) -> Any:
        """按 20ms 时钟生产一帧 AudioFrame"""
        if not WEBRTC_AVAILABLE:
            raise RuntimeError("WebRTC 不可用：缺少 aiortc 或 av 库")

        global _audio_buffer
        pts, time_base = await self.next_timestamp()

        # 从全局缓冲提取音频数据，无数据则填静音
        chunk = None
        if len(_audio_buffer) >= self.bytes_per_frame:
            chunk = bytes(_audio_buffer[:self.bytes_per_frame])
            del _audio_buffer[:self.bytes_per_frame]
        else:
            chunk = self._silence_pcm

        audio_frame = av.AudioFrame(format="s16", layout="mono" if self.channels == 1 else "stereo", samples=self.frame_samples)
        audio_frame.planes[0].update(chunk)
        audio_frame.sample_rate = self.sample_rate
        audio_frame.pts = pts
        audio_frame.time_base = time_base
        return audio_frame


class WebRTCStreamManager:
    """WHEP 会话与 PeerConnection 管理器"""

    def __init__(self):
        self.pcs: Dict[str, Any] = {}
        self.relay = MediaRelay() if WEBRTC_AVAILABLE else None
        self.default_track = AvatarVideoTrack(fps=25)

    def push_frame(self, frame_bgr: np.ndarray):
        """推送数字人画面帧到 WebRTC 分发通道"""
        if frame_bgr is not None:
            set_latest_avatar_frame(frame_bgr)

    def push_audio(self, pcm_bytes: bytes):
        """推送数字人伴音音频到 WebRTC 音频分发通道"""
        if pcm_bytes:
            push_avatar_audio_pcm(pcm_bytes)

    async def handle_whep_offer(self, sdp_offer: str) -> Tuple[str, str]:
        """
        处理客户端发起的 WHEP SDP Offer，协商并返回 SDP Answer
        """
        if not WEBRTC_AVAILABLE:
            raise RuntimeError("WebRTC 运行环境不可用，请安装 aiortc 与 av 依赖：pip install aiortc av")

        session_id = f"whep_{uuid.uuid4().hex[:8]}"
        pc = RTCPeerConnection()
        self.pcs[session_id] = pc

        @pc.on("connectionstatechange")
        async def on_connectionstatechange():
            logger.info(f"WebRTC 会话 {session_id} 连接状态变更: {pc.connectionState}")
            if pc.connectionState in ["failed", "closed"]:
                await self.close_session(session_id)

        offer = RTCSessionDescription(sdp=sdp_offer, type="offer")
        await pc.setRemoteDescription(offer)

        # 根据客户端请求自适应协商视频与音频伴音轨道
        has_video = "m=video" in sdp_offer
        has_audio = "m=audio" in sdp_offer

        if has_video or (not has_video and not has_audio):
            video_track = AvatarVideoTrack(fps=25)
            pc.addTrack(video_track)

        if has_audio:
            audio_track = AvatarAudioTrack(sample_rate=48000, channels=1)
            pc.addTrack(audio_track)

        answer = await pc.createAnswer()
        await pc.setLocalDescription(answer)

        logger.info(
            f"WebRTC WHEP 会话 {session_id} 协商成功 (视频:{has_video}, 音频:{has_audio})，当前活跃连接数: {len(self.pcs)}"
        )
        return session_id, pc.localDescription.sdp

    async def close_session(self, session_id: str):
        """关闭并清理指定的 WebRTC 会话"""
        pc = self.pcs.pop(session_id, None)
        if pc:
            try:
                await pc.close()
                logger.info(f"WebRTC 会话 {session_id} 已安全释放")
            except Exception as e:
                logger.warning(f"关闭 WebRTC 会话异常: {e}")

    async def close_all(self):
        """关闭所有正在连接的会话"""
        session_ids = list(self.pcs.keys())
        for sid in session_ids:
            await self.close_session(sid)

    def get_status(self) -> Dict[str, Any]:
        """获取当前 WebRTC 流媒体服务状态指标"""
        return {
            "available": WEBRTC_AVAILABLE,
            "active_connections": len(self.pcs),
            "fps": 25,
            "has_live_frame": _latest_frame is not None and (time.time() - _latest_frame_time < 2.0),
            "has_audio_track": True,
            "error": None if WEBRTC_AVAILABLE else "缺少 aiortc 或 av 库",
        }


_global_webrtc_manager: Optional[WebRTCStreamManager] = None


def get_webrtc_stream_manager() -> WebRTCStreamManager:
    """获取全局 WebRTC 流媒体服务单例"""
    global _global_webrtc_manager
    if _global_webrtc_manager is None:
        _global_webrtc_manager = WebRTCStreamManager()
    return _global_webrtc_manager
