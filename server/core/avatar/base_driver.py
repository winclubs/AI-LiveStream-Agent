# -*- coding: utf-8 -*-
"""
数字人核心驱动协议与基础接口定义 (BaseAvatarDriver)
实现统一人设驱动接口，支持多模式切换：
  1. 本地 LiveTalking 深度学习驱动 (Local LiveTalking Engine, D:/LiveTalking)
  2. 本地 CPU+内存 + 第三方云端显卡对接 (Cloud GPU Sidecar)
  3. 轻量级免显卡程序化头像渲染 (Procedural 2D Avatar)
  4. 仿真降级模式 (Mock / Fallback)
"""
from abc import ABC, abstractmethod
from typing import Any, Dict, Optional
import time


class BaseAvatarDriver(ABC):
    """数字人渲染与口型动作驱动统一抽象基类"""

    def __init__(self, config: Optional[Dict[str, Any]] = None):
        self.config = config or {}
        self.driver_id: str = self.config.get("driver_id", "default")
        self.is_active: bool = False
        self._speaking: bool = False
        self._current_action_state: int = 0
        self.virtual_cam: Optional[Any] = None
        self.rtmp_streamer: Optional[Any] = None
        self.webrtc_streamer: Optional[Any] = None
        self.recorder: Optional[Any] = None
        self.total_published_frames: int = 0
        self.total_audio_bytes: int = 0
        self._last_speech_time: float = 0.0
        from server.core.avatar.action_state_machine import ActionStateMachine
        self.action_state_machine = self.config.get("action_state_machine") or ActionStateMachine()

    def attach_virtual_cam(self, virtual_cam_service: Any) -> None:
        """挂载 OBS 虚拟摄像头服务"""
        self.virtual_cam = virtual_cam_service

    def attach_rtmp_streamer(self, rtmp_streamer_service: Any) -> None:
        """挂载 RTMP 直推引擎服务"""
        self.rtmp_streamer = rtmp_streamer_service

    def attach_webrtc_streamer(self, webrtc_streamer_service: Any) -> None:
        """挂载 WebRTC WHEP 流媒体预览分发服务"""
        self.webrtc_streamer = webrtc_streamer_service

    def attach_recorder(self, recorder_service: Any) -> None:
        """挂载短视频切片录制服务"""
        self.recorder = recorder_service

    def bind_default_outputs(self) -> None:
        """自动发现并挂载全局虚拟摄像头、RTMP 推流、WebRTC 与切片录制服务"""
        if self.config.get("auto_bind_streams", True):
            try:
                from server.core.media.virtual_cam import global_virtual_cam
                self.attach_virtual_cam(global_virtual_cam)
            except Exception:
                pass
            try:
                from server.core.media.rtmp_streamer import global_rtmp_streamer
                self.attach_rtmp_streamer(global_rtmp_streamer)
            except Exception:
                pass
            try:
                from server.core.media.webrtc_streamer import get_webrtc_stream_manager
                self.attach_webrtc_streamer(get_webrtc_stream_manager())
            except Exception:
                pass
            try:
                from server.core.media.recorder import get_record_manager
                self.attach_recorder(get_record_manager())
            except Exception:
                pass

    def publish_frame(self, frame_rgb: Any, pcm_bytes: Optional[bytes] = None) -> bool:
        """
        统一分发音画帧至挂载的全部输出通道 (虚拟摄像头、RTMP、WebRTC 视窗与切片录制管道)
        """
        dispatched = False
        if frame_rgb is not None:
            self.total_published_frames += 1
            # 叠加电商动态挂件与防录播微噪点/环境光微动
            try:
                from server.core.media.scene_overlay import compose_scene_overlays, global_scene_overlay_state
                composed_frame = compose_scene_overlays(
                    frame_rgb,
                    global_scene_overlay_state.snapshot(),
                    enable_anti_recording=True,
                    timestamp=time.time(),
                )
            except Exception:
                composed_frame = frame_rgb

            # 1. 投递至 OBS 虚拟摄像头
            if self.virtual_cam and getattr(self.virtual_cam, "is_active", False):
                try:
                    self.virtual_cam.send_frame(composed_frame, owner="avatar_driver", priority=1)
                    dispatched = True
                except Exception:
                    pass

            # 2. 投递至 RTMP 直推引擎 (视频)
            if self.rtmp_streamer and getattr(self.rtmp_streamer, "is_streaming", False):
                try:
                    self.rtmp_streamer.send_video_frame(composed_frame)
                    dispatched = True
                except Exception:
                    pass

            # 3. 投递至 WebRTC WHEP 视窗分发轨道 (WebRTC 接收通道标准为 BGR 格式)
            if self.webrtc_streamer:
                try:
                    import cv2
                    if hasattr(composed_frame, "shape") and len(composed_frame.shape) == 3 and composed_frame.shape[2] == 3:
                        webrtc_frame = cv2.cvtColor(composed_frame, cv2.COLOR_RGB2BGR)
                    else:
                        webrtc_frame = composed_frame
                    self.webrtc_streamer.push_frame(webrtc_frame)
                    dispatched = True
                except Exception:
                    pass

            # 4. 投递至切片录制管道 (视频)
            if self.recorder and getattr(self.recorder, "is_recording", lambda: False)():
                try:
                    self.recorder.feed_frame(composed_frame)
                    dispatched = True
                except Exception:
                    pass

        # 5. 可选投递音频流 (显式传入伴音时累加并分发至输出通道)
        if pcm_bytes:
            self.total_audio_bytes += len(pcm_bytes)
            if self.rtmp_streamer and getattr(self.rtmp_streamer, "is_streaming", False):
                try:
                    self.rtmp_streamer.send_audio_pcm(pcm_bytes)
                    dispatched = True
                except Exception:
                    pass

            if self.recorder and getattr(self.recorder, "is_recording", lambda: False)():
                try:
                    self.recorder.feed_audio(pcm_bytes)
                    dispatched = True
                except Exception:
                    pass

        return dispatched

    @abstractmethod
    async def start(self) -> bool:
        """启动数字人驱动引擎与推流管道"""
        pass

    @abstractmethod
    async def stop(self) -> None:
        """安全停止驱动引擎并释放硬件/显存资源"""
        pass

    @abstractmethod
    async def push_audio_chunk(self, pcm_bytes: bytes, eventpoint: Optional[Dict[str, Any]] = None) -> bool:
        """
        流式推送待发声与唇形对齐的音频块 (标准 16kHz, 16-bit Mono PCM)

        参数:
          - pcm_bytes: 20ms 音频数据块 (320 samples = 640 bytes)
          - eventpoint: 事件信令 (如 {'status': 'start'} / {'status': 'end'} / 话术元数据)
        """
        pass

    @abstractmethod
    async def flush_talk(self) -> None:
        """
        极速打断机制 (Interrupt)：
        瞬间清空当前正在播放的音频队列，重置唇形对齐状态回待机位，杜绝拖尾延迟。
        """
        pass

    async def set_custom_state(
        self,
        state_code: int,
        duration: Optional[float] = None,
        priority: Optional[int] = None,
        source: str = "manual",
    ) -> bool:
        """
        设置主播动作状态 (Custom Action State Machine)：
          0: 呼吸静音待机 (Idle Loop)
          1: 热情挥手欢迎
          2: 引导点赞关注
          3: 促单指引购物车
          4: 大额打赏致谢
        """
        self._current_action_state = state_code
        if hasattr(self, "action_state_machine") and self.action_state_machine:
            effective_priority = priority if priority is not None else (10 if source == "manual" else None)
            self.action_state_machine.trigger_action(
                state_code,
                source=source,
                duration=duration,
                priority=effective_priority,
            )
        return True

    def is_speaking(self) -> bool:
        """查询当前数字人是否正在发声/播报"""
        # 超时防死锁保护：若超过 5 秒无新音频注入，自动回落为 False
        if self._speaking and (time.time() - self._last_speech_time > 5.0):
            self._speaking = False
        return self._speaking

    def get_current_action(self) -> int:
        """获取当前动作状态枚举 (同步动作衰减时钟)"""
        if hasattr(self, "action_state_machine") and self.action_state_machine:
            self._current_action_state = self.action_state_machine.update_tick()
        return self._current_action_state

    def get_status(self) -> Dict[str, Any]:
        """获取当前驱动器健康度与遥测指标"""
        cur_action = self.get_current_action()
        asm_status = self.action_state_machine.get_status() if hasattr(self, "action_state_machine") else {}
        return {
            "driver_id": self.driver_id,
            "is_active": self.is_active,
            "speaking": self.is_speaking(),
            "action_state": cur_action,
            "action_name": asm_status.get("action_name", ""),
            "action_priority": asm_status.get("priority", 0),
            "action_time_remaining": asm_status.get("time_remaining_sec", 0.0),
            "driver_type": getattr(self, "driver_type", "unknown"),
            "published_frames": self.total_published_frames,
            "virtual_cam_active": bool(self.virtual_cam and getattr(self.virtual_cam, "is_active", False)),
            "rtmp_streaming": bool(self.rtmp_streamer and getattr(self.rtmp_streamer, "is_streaming", False)),
        }
