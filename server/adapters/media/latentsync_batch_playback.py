"""LatentSync BATCH 路径的实时切片播放时间轴 (SlicePlaybackTimeline)。

背景 (P0-1)
----------
LatentSync 是扩散模型 (ByteDance UNet3D)，推理需要多步去噪，无法像 ONNX 逐帧模型
那样边算边出。它的真实交付形态是：

    整句音频 -> 云端一次性扩散去噪 -> 整句 JPEG 序列 -> **本地按音频播放头实时上屏**

历史缺陷：`LatentSyncBatchAvatarProvider.render_sentence()` 渲染完成后只把最后一帧
写进 `_latest_jpeg` 供 MJPEG 预览，**从不向 frame_bus 发布任何帧**。结果是：

* RTMP 公网推流 / OBS 虚拟摄像头 / 短视频录制器在整个句子期间始终停留在
  本地 procedural shadow 画面 —— 运营者以为在播云端高清，平台观众收到的是
  本地低质帧，且无任何告警（典型「静默分裂」）。
* 云端 GPU 算力被完整烧掉，却没有一帧真正上屏。

本模块补齐缺失的**播放侧时间轴**，语义严格对齐 `neural_sidecar_driver`
的 `_play_video_timeline`（同为「按采样级 PTS 对齐音频播放头上屏」）：

* 逐帧按 `virtual_audio.get_playback_clock()` 的 `samples_played` 推进；
* 播放头未到达该帧 PTS 时挂起等待，绝不抢跑；
* 播放头已越过多帧时只展示当前应显示的最新帧，并如实计入 `dropped_frames`；
* 打断/拒播即退出；浏览器兜底播放（虚拟音频不可用）时改用墙钟继续上屏；
* 整句已播完才拿到帧（批处理耗时 > 音频时长）时判定为 **迟到 (late arrival)**，
  如实上报并 **放弃抢占** 本地 shadow —— 宁可留在 shadow，也绝不播出错位嘴型。

架构诚实 (ADR-16)：本模块只做「已渲染帧的忠实播放」，不合成、不补帧、不伪造
唇形。帧源与嘴型正确性完全由云端 LatentSync 权重决定。
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Callable, Optional, Sequence

logger = logging.getLogger("LiveAgent.LatentSyncSlicePlayback")

# 目标帧率：与 LatentSync 官方契约一致 (25 FPS)
TARGET_FPS = 25

# 时间轴宽限：音频时长结束后仍允许收尾，避免最后一帧被硬切
TIMELINE_GRACE_SECONDS = 2.0

# 单句切片帧数上限 (防爆：约 8 分钟 @25FPS)
MAX_SLICE_FRAMES = 12000

# 播放头轮询节拍
_POLL_INTERVAL = 0.005

# 虚拟音频不可用时的兜底原因集合：这些情况下必须改用墙钟推进而非终止。
# 与 neural_sidecar_driver._play_video_timeline 保持同一语义。
PLAYBACK_FALLBACK_REASONS = frozenset(
    {
        "service_disabled",
        "audio_unavailable",
        "queue_full",
        "audio_capacity_exceeded",
        "stream_unavailable",
        "decode_empty",
        "playback_error",
    }
)


def _is_structural_jpeg(payload: bytes) -> bool:
    """校验 JPEG 起止标记，拦截结构损坏的回包数据。"""
    return bool(
        payload
        and len(payload) > 4
        and payload.startswith(b"\xff\xd8")
        and payload.endswith(b"\xff\xd9")
    )


class SlicePlaybackTimeline:
    """把整句缓存 JPEG 序列按音频播放头实时发布到统一帧总线。"""

    def __init__(
        self,
        *,
        owner: str,
        priority: int,
        sample_rate: int,
        publish_frame: Callable[..., object],
        clock_reader: Callable[[str], Optional[dict]],
        on_progress: Optional[Callable[[int, int], None]] = None,
        grace_seconds: float = TIMELINE_GRACE_SECONDS,
        fps: int = TARGET_FPS,
    ) -> None:
        self._owner = owner
        self._priority = priority
        self._sample_rate = max(1, int(sample_rate))
        self._publish_frame = publish_frame
        self._clock_reader = clock_reader
        self._on_progress = on_progress
        self._grace = float(grace_seconds)
        self._frame_interval = 1.0 / max(1, int(fps))
        self._task: Optional[asyncio.Task] = None
        self._dropped = 0
        self._published = 0
        self._late_arrival = 0
        self._last_pts_ms: Optional[float] = None

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------
    def start(self, audio_id: str, frames: Sequence[bytes]) -> asyncio.Task:
        """启动播放协程；同一 Provider 同时只允许一个时间轴。"""
        self.stop()
        self._task = asyncio.create_task(
            self._run(audio_id, tuple(frames)), name=f"LatentSyncSlicePlayback:{audio_id}"
        )
        return self._task

    def stop(self) -> None:
        """取消播放协程 (打断/句末/停播共用)。"""
        task, self._task = self._task, None
        if task is not None and not task.done():
            task.cancel()

    @property
    def is_playing(self) -> bool:
        return self._task is not None and not self._task.done()

    @property
    def dropped_frames(self) -> int:
        return self._dropped

    @property
    def published_frames(self) -> int:
        return self._published

    @property
    def late_arrival_count(self) -> int:
        return self._late_arrival

    @property
    def last_pts_ms(self) -> Optional[float]:
        return self._last_pts_ms

    # ------------------------------------------------------------------
    # 播放主循环
    # ------------------------------------------------------------------
    async def _run(self, audio_id: str, frames: Sequence[bytes]) -> None:
        if not frames:
            return
        total_frames = len(frames)
        self._dropped = 0
        self._published = 0
        self._late_arrival = 0
        self._last_pts_ms = None

        started = time.monotonic()
        deadline = started + (total_frames * self._frame_interval) + self._grace
        using_wall_clock = False

        for index, payload in enumerate(frames):
            pts_samples = index * (self._sample_rate // TARGET_FPS)

            while True:
                if self._task is None or self._task.done():
                    return
                if time.monotonic() > deadline:
                    logger.warning(
                        "LatentSync 切片播放超时 (audio_id=%s, 已上屏 %d/%d)",
                        audio_id, self._published, total_frames,
                    )
                    return

                clock = self._read_clock(audio_id)
                reject_reason = (clock or {}).get("reject_reason")
                interrupted = bool(clock) and (
                    clock.get("is_interrupted") or clock.get("is_rejected")
                )
                if interrupted and reject_reason not in PLAYBACK_FALLBACK_REASONS:
                    logger.info(
                        "LatentSync 切片播放随音频打断退出 (audio_id=%s, 已上屏 %d/%d)",
                        audio_id, self._published, total_frames,
                    )
                    return
                if interrupted:
                    using_wall_clock = True

                if not using_wall_clock:
                    if clock is None:
                        using_wall_clock = True
                    elif not clock.get("has_started"):
                        # 音频尚未起播：等待真实播放头，绝不抢跑制造音画错位
                        await asyncio.sleep(_POLL_INTERVAL)
                        continue
                    else:
                        elapsed_samples = int(clock.get("samples_played", 0) or 0)
                        if elapsed_samples >= pts_samples:
                            break
                        await asyncio.sleep(
                            min(
                                _POLL_INTERVAL,
                                (pts_samples - elapsed_samples) / self._sample_rate,
                            )
                        )
                        continue
                else:
                    elapsed_sec = time.monotonic() - started
                    if elapsed_sec >= pts_samples / self._sample_rate:
                        break
                    await asyncio.sleep(
                        min(_POLL_INTERVAL, pts_samples / self._sample_rate - elapsed_sec)
                    )

            # 迟到判定：音频已播过本帧，且已播过整句 —— 此刻上屏只会播出错位嘴型
            if using_wall_clock:
                audio_elapsed_sec = time.monotonic() - started
            else:
                audio_elapsed_sec = elapsed_samples / self._sample_rate
            slice_duration_sec = total_frames * self._frame_interval
            if audio_elapsed_sec >= slice_duration_sec:
                self._late_arrival += 1
                if index == 0:
                    logger.warning(
                        "LatentSync 批处理迟到：音频(%.2fs)已播完才拿到切片(%.2fs)，"
                        "本次放弃抢占本地画面，避免播出错位嘴型",
                        audio_elapsed_sec, slice_duration_sec,
                    )
                self._dropped += total_frames - index
                return

            # 播放头越过：本帧已过期，丢弃并继续（不抢播过期嘴型）
            if not using_wall_clock and elapsed_samples >= pts_samples + (
                self._sample_rate // TARGET_FPS
            ):
                self._dropped += 1
                self._report(audio_id, index + 1, total_frames)
                continue

            if not _is_structural_jpeg(payload):
                self._dropped += 1
                self._report(audio_id, index + 1, total_frames)
                continue

            pts_ms = (pts_samples / self._sample_rate) * 1000.0
            try:
                await self._publish_frame(payload, pts_ms)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._dropped += 1
                logger.warning("LatentSync 切片单帧上屏失败 (frame=%d): %s", index, exc)
            else:
                self._published += 1
                self._last_pts_ms = pts_ms
            self._report(audio_id, index + 1, total_frames)

        logger.info(
            "LatentSync 切片播放完成 (audio_id=%s, 上屏 %d/%d, 丢弃 %d)",
            audio_id, self._published, total_frames, self._dropped,
        )

    def _read_clock(self, audio_id: str) -> Optional[dict]:
        try:
            return self._clock_reader(audio_id)
        except Exception:
            logger.debug("读取音频播放头失败，按墙钟兜底", exc_info=True)
            return None

    def _report(self, audio_id: str, done: int, total: int) -> None:
        if self._on_progress is None:
            return
        try:
            self._on_progress(done, total)
        except Exception:
            logger.debug("切片播放进度回调异常", exc_info=True)


def build_timeline(
    *,
    owner: str,
    priority: int,
    sample_rate: int,
    publish_frame: Callable[..., object],
    clock_reader: Optional[Callable[[str], Optional[dict]]] = None,
    on_progress: Optional[Callable[[int, int], None]] = None,
) -> SlicePlaybackTimeline:
    """构造播放时间轴；未显式注入播放头读取器时回落到全局虚拟音频游标。"""
    if clock_reader is None:
        def clock_reader(audio_id: str) -> Optional[dict]:  # type: ignore[misc]
            from server.core.media.virtual_audio import global_virtual_audio

            return global_virtual_audio.get_playback_clock(audio_id)

    return SlicePlaybackTimeline(
        owner=owner,
        priority=priority,
        sample_rate=sample_rate,
        publish_frame=publish_frame,
        clock_reader=clock_reader,
        on_progress=on_progress,
    )
