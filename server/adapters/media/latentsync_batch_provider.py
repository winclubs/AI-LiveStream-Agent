"""LatentSync 批处理神经渲染 Provider (路径二专用)。

该 Provider 针对扩散模型 (LatentSync/UNet3D) 的非流式计算特性，
支持整句批处理 (AvatarRenderMode.BATCH) 与前瞻预渲染。
将整句音频一次性打包提交给云端 GPU，云端完成多步去噪后整批返回，
避免逐帧推流造成的严重断流与延迟累积。
"""

from __future__ import annotations

import asyncio
import base64
import io
import json
import logging
import math
import struct
import time
import uuid
import wave
from typing import Any, Mapping, Optional, Sequence
from urllib.parse import urlparse

import aiohttp

from server.adapters.media.avatar_provider import (
    AvatarProviderCapabilities,
    AvatarRenderMode,
    ProviderError,
    ProviderErrorCode,
    ProviderRenderResult,
    ProviderVerification,
)
from server.adapters.media.latentsync_batch_playback import (
    MAX_SLICE_FRAMES,
    SlicePlaybackTimeline,
    build_timeline,
)
from server.core.media.audio_frame import AudioFrame, validate_audio_frame_batch

logger = logging.getLogger("LiveAgent.LatentSyncBatch")

# 帧总线 owner 与优先级：与 sidecar 同级，高于本地 procedural shadow(1)，
# 确保云端高清帧真正抢占 RTMP/虚拟摄像头/录制器，而不是只活在预览里。
_FRAME_BUS_OWNER = "latentsync_batch"
_FRAME_BUS_PRIORITY = 100


class LatentSyncBatchAvatarProvider:
    """基于 LatentSync 的批处理高精口型渲染 Provider。"""

    def __init__(
        self,
        provider_id: str,
        display_name: str,
        node_url: str,
        auth_token: str = "",
        avatar_id: str = "default",
        guidance_scale: float = 1.0,
        inference_steps: int = 20,
        seed: int = 1247,
        connect_timeout: float = 5.0,
        message_timeout: float = 30.0,
        request_timeout: float = 180.0,
    ) -> None:
        provider_id = str(provider_id).strip()
        if not provider_id:
            raise ValueError("provider_id 不能为空")
        self._provider_id = provider_id
        self._display_name = str(display_name).strip() or provider_id
        self.node_url = node_url.rstrip("/")
        self.auth_token = auth_token.strip()
        self.avatar_id = str(avatar_id).strip() or "default"
        self.guidance_scale = max(0.1, float(guidance_scale))
        self.inference_steps = max(1, int(inference_steps))
        self.seed = int(seed)

        self.connect_timeout = float(connect_timeout)
        self.message_timeout = float(message_timeout)
        self.request_timeout = float(request_timeout)

        self._session: aiohttp.ClientSession | None = None
        self._latest_jpeg: bytes = b""
        self._cached_slices: dict[str, list[bytes]] = {}
        self._is_ready: bool = False
        self._last_error: str = ""
        # BATCH 播放时间轴：整句渲染完成后按音频播放头实时上屏 (P0-1)
        self._timeline: Optional[SlicePlaybackTimeline] = None
        self._playback_audio_id: str = ""
        self._last_sample_rate: int = 16000



    @property
    def provider_id(self) -> str:
        return self._provider_id

    @property
    def display_name(self) -> str:
        return self._display_name

    @property
    def capabilities(self) -> AvatarProviderCapabilities:
        return AvatarProviderCapabilities(
            render_modes=frozenset({AvatarRenderMode.BATCH, AvatarRenderMode.REALTIME}),
            input_codecs=frozenset({"pcm_s16le"}),
            renderer_only=True,
            neural_lipsync=True,
            transactional_sentence=True,
            supports_cancel=True,
            supports_cancel_ack=True,
            supports_credit=False,
            supports_render_started=True,
            supports_sample_pts=True,
            strict_completion=True,
            protocol_name="latentsync_batch_v1",
            protocol_version=1,
            # 验证态必须跟随实测探活结果：未探活成功时不得向状态页宣称"已验证"
            # (ADR-16 能力诚实)。此前为无条件 VERIFIED，与节点是否就绪无关。
            verification=(
                ProviderVerification.VERIFIED
                if self._is_ready
                else ProviderVerification.UNVERIFIED
            ),
            extra={
                "adapter": "latentsync_batch",
                "max_safe_concurrency": 2,
                "model_family": "latentsync_unet3d",
                "whisper_guided": True,
            },
        )

    @property
    def has_frames(self) -> bool:
        return bool(self._latest_jpeg)

    @property
    def is_playing(self) -> bool:
        """切片播放时间轴是否处于运行态 (供状态页与打断逻辑消费)。"""
        return bool(self._timeline and self._timeline.is_playing)

    @property
    def published_frames(self) -> int:
        return self._timeline.published_frames if self._timeline else 0

    @property
    def dropped_frames(self) -> int:
        return self._timeline.dropped_frames if self._timeline else 0

    @property
    def late_arrival_count(self) -> int:
        """批处理迟到次数：音频已播完才拿到切片的次数 (音画同步健康度关键指标)。"""
        return self._timeline.late_arrival_count if self._timeline else 0

    def has_cached_slice(self, audio_id: str) -> bool:
        """预取缓存是否命中该句切片。"""
        return bool(self._cached_slices.get(audio_id))

    def get_cached_slice(self, audio_id: str) -> list[bytes]:
        return list(self._cached_slices.get(audio_id) or ())

    def clear_slice_cache(self) -> None:
        """清空全部预取切片 (打断/停播共用)。

        打断后残留的预取切片会让已废弃的旧画面在后续播放中复活，
        造成拖尾与音画错位，必须与打断同时清空。
        """
        self._cached_slices.clear()

    def get_latest_jpeg(self) -> bytes:
        return self._latest_jpeg

    def get_preview_status(self) -> dict[str, Any]:
        return {
            "is_ready": self._is_ready,
            "has_frames": self.has_frames,
            "provider_id": self._provider_id,
            "node_url": self.node_url,
            "cached_slices_count": len(self._cached_slices),
            "last_error": self._last_error,
            # 播放侧遥测：批处理路径的音画同步健康度 (ADR-16 如实上报)
            "is_playing": self.is_playing,
            "playback_audio_id": self._playback_audio_id,
            "published_frames": self.published_frames,
            "dropped_frames": self.dropped_frames,
            "late_arrival_count": self.late_arrival_count,
        }

    def get_media_capabilities(self) -> dict[str, Any]:
        return {
            "backend_id": "latentsync",
            "model_version": "latentsync-unet3d-v1",
            "neural_lipsync": self._is_ready,
            "whisper_guided": True,
            "renderer_available": self._is_ready,
            "batch_render_capable": True,
            # 能力诚实：明确声明本适配器具备「整句渲染 + 实时切片播放」与前瞻预取
            "batch_slice_playback": True,
            "slice_prefetch": True,
        }

    def get_status(self) -> dict[str, Any]:
        """统一远端 Provider 状态契约。"""
        return self.get_preview_status()

    async def start(self) -> None:
        """初始化 HTTP 会话并探测云端健康状态。"""
        if self._session is None or self._session.closed:
            timeout = aiohttp.ClientTimeout(
                total=self.request_timeout,
                connect=self.connect_timeout,
                sock_read=self.message_timeout,
            )
            self._session = aiohttp.ClientSession(timeout=timeout)

        # 尝试健康检查
        health_url = f"{self.node_url}/health"
        try:
            headers = {}
            if self.auth_token:
                headers["Authorization"] = f"Bearer {self.auth_token}"
            async with self._session.get(health_url, headers=headers, timeout=self.connect_timeout) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    backend_ready = bool(data.get("backend_ready", False))
                    if backend_ready:
                        self._is_ready = True
                        self._last_error = ""
                        logger.info("LatentSync 云端渲染节点连接就绪: %s", self.node_url)
                        return
                    else:
                        self._is_ready = False
                        self._last_error = "云端节点已连接但模型/渲染引擎未就绪 (backend_ready=False)"
                        logger.warning("LatentSync 节点模型尚未就绪: %s", self.node_url)
                        return
                self._is_ready = False
                self._last_error = f"健康检查失败 HTTP {resp.status}"
        except Exception as exc:
            self._is_ready = False
            self._last_error = f"无法连接云端节点: {exc}"
            logger.warning("LatentSync 节点探测异常: %s", exc)

    async def close(self) -> None:
        self.stop_playback()
        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None
        self._cached_slices.clear()

    def stop_playback(self) -> None:
        """停止切片播放时间轴 (打断/停播/句末共用)。"""
        if self._timeline is not None:
            self._timeline.stop()
            self._timeline = None
        self._playback_audio_id = ""

    async def interrupt(self, reason: str = "") -> None:
        """中断当前进行中的渲染任务与切片播放。"""
        logger.info("LatentSync 收到中断信号: %s", reason)
        # 先停播放：旧画面绝不允许拖尾到下一句
        self.stop_playback()
        if self._session and not self._session.closed:
            try:
                cancel_url = f"{self.node_url}/render/cancel"
                headers = {}
                if self.auth_token:
                    headers["Authorization"] = f"Bearer {self.auth_token}"
                async with self._session.post(cancel_url, headers=headers, timeout=2.0) as _:
                    pass
            except Exception:
                pass

    async def _publish_slice_frame(self, jpeg: bytes, pts_ms: float) -> None:
        """把单帧切片发布到统一帧总线。

        与 sidecar 同语义：先做画层合成与预览重编码，再以高优先级抢占本地
        shadow 租约扇出到 RTMP / WebRTC / 虚拟摄像头 / 录制器。
        """
        image_rgb = None
        try:
            import cv2
            import numpy as _np

            image = cv2.imdecode(_np.frombuffer(jpeg, _np.uint8), cv2.IMREAD_COLOR)
            if image is None:
                raise ValueError("JPEG 解码失败")
            image_rgb = cv2.cvtColor(image, cv2.COLOR_BGR2RGB)
        except ImportError:
            # 无 OpenCV 环境仍保留结构完整的 JPEG 供 MJPEG 预览，
            # 但不上屏 (无 RGB 帧就无法保证画层合成与帧总线一致性)。
            logger.debug("OpenCV 不可用，LatentSync 切片仅供预览不上屏")
        except Exception as exc:
            logger.warning("LatentSync 切片解码失败: %s", exc)
            raise

        if image_rgb is None:
            return

        try:
            from server.core.media.scene_overlay import (
                compose_scene_overlays,
                global_scene_overlay_state,
            )

            composed = compose_scene_overlays(
                image_rgb,
                global_scene_overlay_state.snapshot(),
                enable_anti_recording=True,
                timestamp=time.time(),
            )
            if composed is not None:
                image_rgb = composed
            import cv2

            bgr = cv2.cvtColor(image_rgb, cv2.COLOR_RGB2BGR)
            ret, enc = cv2.imencode(".jpg", bgr, [int(cv2.IMWRITE_JPEG_QUALITY), 90])
            if ret:
                self._latest_jpeg = enc.tobytes()
        except Exception:
            self._latest_jpeg = jpeg

        from server.core.media.frame_bus import global_frame_bus

        global_frame_bus.publish_frame(
            image_rgb,
            owner=_FRAME_BUS_OWNER,
            priority=_FRAME_BUS_PRIORITY,
            frame_index=self.published_frames + 1,
            pts_ms=pts_ms,
            compose=False,
        )

    def _start_slice_playback(self, audio_id: str, frames: Sequence[bytes], sample_rate: int) -> None:
        """按音频播放头启动整句切片实时上屏。"""
        self.stop_playback()
        if not frames:
            return
        self._timeline = build_timeline(
            owner=_FRAME_BUS_OWNER,
            priority=_FRAME_BUS_PRIORITY,
            sample_rate=sample_rate,
            publish_frame=self._publish_slice_frame,
        )
        self._playback_audio_id = audio_id
        self._timeline.start(audio_id, frames)

    async def play_cached_slice(self, audio_id: str) -> bool:
        """命中预取缓存时直接上屏，不再发起云端批处理 (零延迟)。"""
        frames = self._cached_slices.get(audio_id)
        if not frames:
            return False
        # 取出即消费：避免同一句重复上屏
        self._cached_slices.pop(audio_id, None)
        first = self._last_sample_rate
        self._start_slice_playback(audio_id, list(frames), first)
        logger.info(
            "LatentSync 命中预取切片缓存，直接实时上屏 (audio_id=%s, 帧数=%d)",
            audio_id, len(frames),
        )
        return True

    def _convert_frames_to_wav_bytes(self, frames: Sequence[AudioFrame]) -> bytes:
        """把批量 AudioFrame 转换为标准 16kHz 单声道 16bit PCM WAV 字节流。"""
        raw_pcm = b"".join(f.data for f in frames)
        if not raw_pcm:
            return b""
        in_sr = frames[0].format.sample_rate
        # 若需要重采样到 16000
        if in_sr != 16000:
            samples_count = len(raw_pcm) // 2
            samples = struct.unpack(f"<{samples_count}h", raw_pcm)
            target_count = int(samples_count * 16000 / in_sr)
            if target_count <= 0:
                return b""
            # 线性插值
            ratio = float(samples_count - 1) / max(1, target_count - 1) if target_count > 1 else 0.0
            resampled = [
                samples[min(samples_count - 1, int(round(i * ratio)))]
                for i in range(target_count)
            ]
            raw_pcm = struct.pack(f"<{len(resampled)}h", *resampled)

        bio = io.BytesIO()
        with wave.open(bio, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(16000)
            wf.writeframes(raw_pcm)
        return bio.getvalue()

    async def render_sentence(
        self,
        frames: Sequence[AudioFrame],
        *,
        render_mode: AvatarRenderMode,
        publish: bool = True,
    ) -> ProviderRenderResult:
        """批处理整句渲染事务。

        参数
        ----
        publish:
            * ``True`` (正式开播路径)：渲染完成后按音频播放头把整句切片实时上屏，
              经统一帧总线抢占 RTMP / WebRTC / 虚拟摄像头 / 录制器。
            * ``False`` (前瞻预取路径)：只落切片缓存不上屏，等待正式播放时命中缓存，
              避免「预取阶段就上屏」与「正式播放再上屏」造成重复发布。
        """
        if not frames:
            raise ProviderError(
                ProviderErrorCode.INVALID_REQUEST,
                "传入音频帧序列为空",
                provider_id=self.provider_id,
            )
        first = frames[0]
        request_id = uuid.uuid4().hex
        started_at = time.monotonic()
        self._last_sample_rate = int(first.format.sample_rate or 16000)

        wav_bytes = self._convert_frames_to_wav_bytes(frames)
        if not wav_bytes:
            raise ProviderError(
                ProviderErrorCode.INVALID_REQUEST,
                "音频转换 PCM 失败",
                provider_id=self.provider_id,
                request_id=request_id,
            )

        payload = {
            "request_id": request_id,
            "audio_id": first.audio_id,
            "avatar_id": self.avatar_id,
            "audio_b64": base64.b64encode(wav_bytes).decode("ascii"),
            "guidance_scale": self.guidance_scale,
            "num_inference_steps": self.inference_steps,
            "seed": self.seed,
        }

        render_url = f"{self.node_url}/render/batch"
        headers = {"Content-Type": "application/json"}
        if self.auth_token:
            headers["Authorization"] = f"Bearer {self.auth_token}"

        if self._session is None or self._session.closed:
            await self.start()

        session = self._session
        if session is None:
            raise ProviderError(
                ProviderErrorCode.REMOTE_UNAVAILABLE,
                "网络会话未初始化",
                provider_id=self.provider_id,
                retryable=True,
            )

        try:
            async with session.post(
                render_url, json=payload, headers=headers, timeout=self.request_timeout
            ) as resp:
                if resp.status == 401 or resp.status == 403:
                    raise ProviderError(
                        ProviderErrorCode.AUTHENTICATION,
                        f"云端节点鉴权失败 HTTP {resp.status}",
                        provider_id=self.provider_id,
                        request_id=request_id,
                    )
                if resp.status != 200:
                    text = await resp.text()
                    raise ProviderError(
                        ProviderErrorCode.REMOTE_UNAVAILABLE,
                        f"云端批处理渲染失败 HTTP {resp.status}: {text[:200]}",
                        provider_id=self.provider_id,
                        retryable=True,
                        request_id=request_id,
                    )
                data = await resp.json()
        except asyncio.TimeoutError as exc:
            raise ProviderError(
                ProviderErrorCode.REQUEST_TIMEOUT,
                f"云端 LatentSync 批处理超时 ({self.request_timeout}s)",
                provider_id=self.provider_id,
                retryable=True,
                request_id=request_id,
            ) from exc
        except ProviderError:
            raise
        except Exception as exc:
            raise ProviderError(
                ProviderErrorCode.REMOTE_UNAVAILABLE,
                f"云端通信异常: {exc}",
                provider_id=self.provider_id,
                retryable=True,
                request_id=request_id,
            ) from exc

        # 解析回包帧数据 (base64 的 jpeg 列表)
        frames_b64 = data.get("frames", [])
        output_frames_count = int(data.get("output_frames", len(frames_b64)))
        decoded_frames: list[bytes] = []
        if frames_b64:
            if len(frames_b64) > MAX_SLICE_FRAMES:
                raise ProviderError(
                    ProviderErrorCode.REMOTE_RESOURCE_EXHAUSTED,
                    f"云端回包帧数 {len(frames_b64)} 超过上限 {MAX_SLICE_FRAMES}",
                    provider_id=self.provider_id,
                    request_id=request_id,
                )
            for item in frames_b64:
                try:
                    f_bytes = base64.b64decode(item)
                except Exception:
                    continue
                decoded_frames.append(f_bytes)

        if decoded_frames:
            self._latest_jpeg = decoded_frames[-1]
            self._cached_slices[first.audio_id] = decoded_frames
            # P0-1：渲染完成不等于交付完成 —— 必须按音频播放头把整句切片真实上屏，
            # 否则云端帧永远不会进入 RTMP/虚拟摄像头/录制器（此前仅存于预览缓存）。
            if publish:
                self._cached_slices.pop(first.audio_id, None)
                self._start_slice_playback(
                    first.audio_id, decoded_frames, self._last_sample_rate
                )
        elif output_frames_count > 0:
            self._last_error = (
                f"云端声明渲染 {output_frames_count} 帧但未返回任何可用帧数据"
            )
            logger.warning("LatentSync 批处理: %s (audio_id=%s)", self._last_error, first.audio_id)

        latency_ms = (time.monotonic() - started_at) * 1000.0
        return ProviderRenderResult(
            provider_id=self.provider_id,
            request_id=request_id,
            audio_id=first.audio_id,
            render_mode=render_mode,
            latency_ms=latency_ms,
            output_frames=output_frames_count,
            billed_units=1,
            cost_minor=0,
            evidence={
                "backend_id": "latentsync",
                "model_version": "latentsync-unet3d-v1",
                "inference_steps": self.inference_steps,
            },
        )
