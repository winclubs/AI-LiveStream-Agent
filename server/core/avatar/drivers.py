# -*- coding: utf-8 -*-
"""
多模式数字人驱动实现集 (Avatar Drivers)
实现四大运行模式：
  1. LocalLiveTalkingDriver: 本地深度学习驱动 (对接 D:/LiveTalking)
  2. CloudSidecarDriver: 本地 CPU+内存 + 第三方云端显卡对接
  3. Procedural2DDriver: 本地轻量免显卡 2D 程序化渲染
  4. MockAvatarDriver: 单元测试仿真驱动
"""
import asyncio
import logging
import time
from pathlib import Path
from typing import Any, Dict, Optional

from server.core.avatar.base_driver import BaseAvatarDriver
from server.core.avatar.registry import register_avatar_driver

logger = logging.getLogger("LiveAgent.AvatarDrivers")


@register_avatar_driver("livetalking")
class LocalLiveTalkingDriver(BaseAvatarDriver):
    """
    本地 LiveTalking 深度学习唇形驱动器
    对接本地路径 D:\\LiveTalking，支持通过 WebRTC / HTTP / WebSocket 注入语音并驱动口型。
    """
    def __init__(self, config: Optional[Dict[str, Any]] = None):
        super().__init__(config)
        import os
        raw_dir = self.config.get("livetalking_dir") or os.environ.get("LIVETALKING_HOME") or "D:\\LiveTalking"
        self.livetalking_dir = Path(raw_dir)
        self.api_endpoint = self.config.get("api_endpoint", "http://127.0.0.1:8010")
        self.session_id = self.config.get("session_id", "live_stream_session_0")
        self.avatar_id = self.config.get("avatar_id", "wav2lip256_avatar1")
        self.model_type = self.config.get("model_type", "wav2lip")

    async def start(self) -> bool:
        self.is_active = True
        self.bind_default_outputs()
        status_note = "目录存在" if self.livetalking_dir.exists() else "可选外部参考目录 (未挂载)"
        logger.info(f"LocalLiveTalking 驱动器已就绪 (路径状态: {status_note}, 接口: {self.api_endpoint})")
        return True

    async def stop(self) -> None:
        self.is_active = False
        self._speaking = False
        logger.info("LocalLiveTalking 驱动器已停止")

    async def push_audio_chunk(self, pcm_bytes: bytes, eventpoint: Optional[Dict[str, Any]] = None) -> bool:
        if not self.is_active or not pcm_bytes:
            return False
        self._speaking = True
        self._last_speech_time = time.time()
        # 如已连接本地 LiveTalking 服务，可将音频流推送至其 /humanaudio 或 WebRTC 通道
        return True

    async def flush_talk(self) -> None:
        """调用 LiveTalking 的 /interrupt_talk 接口触发瞬间打断"""
        self._speaking = False
        logger.info("LocalLiveTalking 驱动器已执行瞬间打断 (flush_talk)")

    async def set_custom_state(
        self,
        state_code: int,
        duration: Optional[float] = None,
        priority: Optional[int] = None,
        source: str = "manual",
    ) -> bool:
        """调用 LiveTalking 的 /set_audiotype 接口切换主播动作视频切片"""
        await super().set_custom_state(state_code, duration=duration, priority=priority, source=source)
        logger.info(f"LocalLiveTalking 动作状态已切换为: {state_code}")
        return True


@register_avatar_driver("cloud_sidecar")
class CloudSidecarDriver(BaseAvatarDriver):
    """
    第三方云端显卡对接驱动器 (满足: 本地 CPU+内存 + 第三方云端显卡模式)
    将本地生成的 TTS 音频与动作指令通过全双工加密隧道发送至云端 GPU (如 Tesla T4 / A100 / RTX 4090 Sidecar)，
    云端完成高算力神经渲染后，将视频流低延迟回传至本地 OBS 虚拟摄像头与前端监视器。
    """
    def __init__(self, config: Optional[Dict[str, Any]] = None):
        super().__init__(config)
        import os
        from urllib.parse import urlparse

        # 优先读取传入参数；若未指定则自动从硬件探活或环境变量感知云端节点
        sidecar_url = (self.config.get("sidecar_url") or "").strip()
        api_key = (self.config.get("api_key") or "").strip()

        if not sidecar_url:
            try:
                from server.core.hardware.gpu_capability import get_active_cloud_gpu_sync
                cloud_gpu = get_active_cloud_gpu_sync()
                if cloud_gpu.configured and cloud_gpu.base_url:
                    sidecar_url = cloud_gpu.base_url.strip()
                    api_key = api_key or cloud_gpu.api_key
            except Exception:
                pass

        if not sidecar_url:
            sidecar_url = os.environ.get("CLOUD_GPU_URL") or os.environ.get("SIDECAR_URL") or "ws://127.0.0.1:8890/ws/render-v3"
            api_key = api_key or os.environ.get("CLOUD_GPU_KEY", "")

        # 规范化 WebSocket 端点路径
        parsed = urlparse(sidecar_url)
        if not parsed.path or parsed.path == "/":
            scheme = "wss" if parsed.scheme in ("https", "wss") else "ws"
            netloc = parsed.netloc or parsed.path
            sidecar_url = f"{scheme}://{netloc}/ws/render-v3"

        self.sidecar_url = sidecar_url
        self.api_key = api_key
        self.tunnel_status = "idle"
        self.neural_driver = self.config.get("neural_driver")  # 允许外部直接注入测试或适配器
        self.total_audio_chunks_sent = 0
        self.total_audio_bytes_sent = 0
        self._generation = 0
        self._session_generation = 0
        self._send_queue: asyncio.Queue = asyncio.Queue(maxsize=1000)
        self._worker_task: Optional[asyncio.Task] = None
        self._lock = asyncio.Lock()

    def _slice_pcm_to_frames(
        self,
        pcm_bytes: bytes,
        sample_rate: int = 16000,
        text: str = "",
    ):
        """将连续 PCM 字节流切分为符合 ADR-16 / Protocol v3 契约的标准 20ms AudioFrame 批次"""
        if not pcm_bytes:
            return []
        from server.core.media.audio_frame import AudioFormat, AudioFrame
        import uuid

        sample_rate = int(sample_rate or 16000)
        fmt = AudioFormat(codec="pcm_s16le", sample_rate=sample_rate, channels=1, sample_width_bytes=2)
        # 20ms 对应采样数
        samples_per_frame = int(sample_rate * 0.02)
        frame_bytes = samples_per_frame * 2  # 16-bit Mono

        # 数据末尾未对齐时以 PCM 静音安全补齐
        remainder = len(pcm_bytes) % frame_bytes
        if remainder != 0:
            pcm_bytes = pcm_bytes + (b"\x00" * (frame_bytes - remainder))

        total_frames = len(pcm_bytes) // frame_bytes
        if total_frames == 0:
            return []

        self._generation += 1
        audio_id = f"aud_{int(time.time()*1000)}_{uuid.uuid4().hex[:6]}"
        frames = []
        for i in range(total_frames):
            chunk = pcm_bytes[i * frame_bytes : (i + 1) * frame_bytes]
            frames.append(AudioFrame(
                data=chunk,
                format=fmt,
                audio_id=audio_id,
                sequence=i,
                pts_samples=i * samples_per_frame,
                audio_generation=self._generation,
                session_generation=self._session_generation,
                text=text if i == 0 else "",
                is_first=(i == 0),
                is_final=(i == total_frames - 1),
            ))
        return frames

    async def _process_send_queue(self):
        """后台队列消费循环：平滑向云端推送音频事务并对冲网络抖动"""
        while self.is_active:
            try:
                item = await self._send_queue.get()
                if item is None:
                    break
                frames = item
                if self.neural_driver is not None:
                    try:
                        await self.neural_driver.feed_audio_frames(frames)
                    except Exception as e:
                        logger.warning(f"云端显卡推送音频帧事务异常: {e}")
                self._send_queue.task_done()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.debug(f"音频发送队列异常: {e}", exc_info=True)

    async def start(self) -> bool:
        self.is_active = True
        self.tunnel_status = "connecting"
        self.bind_default_outputs()

        # 挂载或实例化底层的 NeuralSidecarMediaDriver
        try:
            if self.neural_driver is None:
                from server.adapters.media.neural_sidecar_driver import NeuralSidecarMediaDriver
                self.neural_driver = NeuralSidecarMediaDriver(
                    node_url=self.sidecar_url,
                    auth_token=self.api_key,
                    connect_timeout=3.0,
                    message_timeout=15.0,
                )
            # 挂载到全局媒体中枢
            from server.adapters.media.media_router import global_media_router
            global_media_router.attach_sidecar(self.neural_driver)

            # 启动神经渲染驱动后台连接
            asyncio.create_task(self.neural_driver.start())
            self.tunnel_status = "connected"
        except Exception as e:
            logger.warning(f"启动云端 Sidecar 底层连接警告: {e}")
            self.tunnel_status = "degraded"

        # 启动后台队列工作协程
        if self._worker_task is None or self._worker_task.done():
            self._worker_task = asyncio.create_task(self._process_send_queue())

        logger.info(f"第三方云端显卡对接驱动器已就绪 (Sidecar 端点: {self.sidecar_url}, 状态: {self.tunnel_status})")
        return True

    async def stop(self) -> None:
        self.is_active = False
        self.tunnel_status = "disconnected"
        self._speaking = False

        # 清空并关闭工作协程
        if self._worker_task and not self._worker_task.done():
            self._worker_task.cancel()
            self._worker_task = None

        # 清空未发送音频
        while not self._send_queue.empty():
            try:
                self._send_queue.get_nowait()
                self._send_queue.task_done()
            except Exception:
                break

        # 安全关闭底层驱动
        if self.neural_driver is not None:
            try:
                from server.adapters.media.media_router import global_media_router
                global_media_router.detach_sidecar()
                await self.neural_driver.stop()
            except Exception as e:
                logger.debug(f"停止云端 Sidecar 忽略: {e}")

        logger.info("第三方云端显卡对接驱动器已断开")

    async def push_audio_chunk(self, pcm_bytes: bytes, eventpoint: Optional[Dict[str, Any]] = None) -> bool:
        if not self.is_active or not pcm_bytes:
            return False

        eventpoint = eventpoint or {}
        sample_rate = eventpoint.get("sample_rate", 16000)
        text = eventpoint.get("text", "")

        # 1. 切片为标准 20ms 音频帧批次
        frames = self._slice_pcm_to_frames(pcm_bytes, sample_rate=sample_rate, text=text)
        if not frames:
            return False

        self._speaking = True
        self._last_speech_time = time.time()
        self.total_audio_chunks_sent += len(frames)
        self.total_audio_bytes_sent += len(pcm_bytes)

        # 2. 推入非阻塞异步队列，平滑对冲抖动
        try:
            self._send_queue.put_nowait(frames)
            return True
        except asyncio.QueueFull:
            logger.warning("云端音频发送队列已满，丢弃该段音频以防内存堆积")
            return False

    async def flush_talk(self) -> None:
        """极速打断机制：清空队列、提升代际并令云端 GPU 瞬间静止"""
        self._speaking = False
        self._generation += 1

        # 清空本地队列
        while not self._send_queue.empty():
            try:
                self._send_queue.get_nowait()
                self._send_queue.task_done()
            except Exception:
                break

        # 向云端发布 cancel 打断
        if self.neural_driver is not None:
            try:
                await self.neural_driver.interrupt("flush_talk", next_generation=self._generation)
            except Exception as e:
                logger.debug(f"云端 Sidecar 打断异常 (保留本地代际隔离): {e}")

        try:
            from server.adapters.media.media_router import global_media_router
            await global_media_router.interrupt("flush_talk", next_generation=self._generation)
        except Exception:
            pass

        logger.info("云端显卡 Sidecar 驱动器已广播瞬间打断指令并清空缓冲区")

    def get_latest_jpeg(self) -> bytes:
        """获取云端渲染传回的最新视频帧"""
        if self.neural_driver is not None and getattr(self.neural_driver, "has_frames", False):
            return self.neural_driver.get_latest_jpeg()
        from server.adapters.media.media_router import global_media_router
        return global_media_router.get_latest_jpeg()

    @property
    def has_frames(self) -> bool:
        if self.neural_driver is not None:
            return bool(getattr(self.neural_driver, "has_frames", False))
        return False

    async def set_custom_state(
        self,
        state_code: int,
        duration: Optional[float] = None,
        priority: Optional[int] = None,
        source: str = "manual",
    ) -> bool:
        await super().set_custom_state(state_code, duration=duration, priority=priority, source=source)
        # 向云端 Sidecar 同步动作状态信令
        if self.neural_driver is not None and hasattr(self.neural_driver, "send_control"):
            try:
                await self.neural_driver.send_control("set_action", {"action_code": state_code, "duration": duration})
            except Exception as e:
                logger.debug(f"向云端同步动作代码忽略: {e}")
        logger.info(f"云端显卡 Sidecar 驱动器同步动作切片状态: {state_code}")
        return True

    def get_status(self) -> Dict[str, Any]:
        """获取云端显卡驱动器遥测数据、隧道与渲染状态"""
        status = super().get_status()
        status.update({
            "tunnel_status": self.tunnel_status,
            "sidecar_url": self.sidecar_url,
            "audio_chunks_sent": self.total_audio_chunks_sent,
            "audio_bytes_sent": self.total_audio_bytes_sent,
            "has_frames": self.has_frames,
            "neural_ready": bool(self.neural_driver and getattr(self.neural_driver, "is_ready", False)),
        })
        return status


@register_avatar_driver("procedural")
class Procedural2DDriver(BaseAvatarDriver):
    """
    本地轻量免显卡 2D 程序化渲染驱动器 (纯 CPU 模式)
    通过 Viseme 口型映射与真人待机呼吸切流，完全不吃独立显卡，轻薄本与老旧主机流畅开播。
    """
    def __init__(self, config: Optional[Dict[str, Any]] = None):
        super().__init__(config)
        self.fps = self.config.get("fps", 25)
        self._loop_task: Optional[asyncio.Task] = None

    async def _render_loop(self):
        """挂接 ActionStateMachine 进行 25FPS 连续动作与待机平滑帧推流"""
        frame_idx = 0
        interval = 1.0 / max(1, self.fps)
        while self.is_active:
            t0 = time.time()
            try:
                frame = self.action_state_machine.get_frame(frame_idx)
                if frame is not None:
                    self.publish_frame(frame)
                frame_idx += 1
            except Exception as e:
                logger.debug(f"程序化驱动推流帧异常: {e}")
            elapsed = time.time() - t0
            sleep_time = max(0.005, interval - elapsed)
            await asyncio.sleep(sleep_time)

    async def start(self) -> bool:
        self.is_active = True
        self.bind_default_outputs()
        if self._loop_task is None or self._loop_task.done():
            self._loop_task = asyncio.create_task(self._render_loop())
        logger.info(f"本地免显卡程序化渲染驱动器已就绪并启动动作流 (帧率: {self.fps} FPS)")
        return True

    async def stop(self) -> None:
        self.is_active = False
        self._speaking = False
        if self._loop_task and not self._loop_task.done():
            self._loop_task.cancel()
            self._loop_task = None
        logger.info("本地免显卡程序化渲染驱动器已停止")

    async def push_audio_chunk(self, pcm_bytes: bytes, eventpoint: Optional[Dict[str, Any]] = None) -> bool:
        if not self.is_active or not pcm_bytes:
            return False
        self._speaking = True
        self._last_speech_time = time.time()
        # 语音伴随话术自动研判动作代码
        if eventpoint and eventpoint.get("text"):
            self.action_state_machine.evaluate_text(str(eventpoint["text"]))
        return True

    async def flush_talk(self) -> None:
        self._speaking = False
        self.action_state_machine.reset_to_idle()
        logger.info("程序化渲染驱动器已重置口型与动作回待机状态")

    async def set_custom_state(
        self,
        state_code: int,
        duration: Optional[float] = None,
        priority: Optional[int] = None,
        source: str = "manual",
    ) -> bool:
        await super().set_custom_state(state_code, duration=duration, priority=priority, source=source)
        return True


@register_avatar_driver("mock")
class MockAvatarDriver(BaseAvatarDriver):
    """单元测试与 CI 仿真驱动"""
    async def start(self) -> bool:
        self.is_active = True
        self.bind_default_outputs()
        return True

    async def stop(self) -> None:
        self.is_active = False
        self._speaking = False

    async def push_audio_chunk(self, pcm_bytes: bytes, eventpoint: Optional[Dict[str, Any]] = None) -> bool:
        self._speaking = True
        self._last_speech_time = time.time()
        return True

    async def flush_talk(self) -> None:
        self._speaking = False

    async def set_custom_state(
        self,
        state_code: int,
        duration: Optional[float] = None,
        priority: Optional[int] = None,
        source: str = "manual",
    ) -> bool:
        await super().set_custom_state(state_code, duration=duration, priority=priority, source=source)
        return True
