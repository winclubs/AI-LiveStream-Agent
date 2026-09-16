"""整句事务完成后的有界 PCM 帧化管线。

第一阶段保留“完整句成功后才提交”的可靠性语义：压缩容器先完整解码，再切成
固定时长 PCM 帧。该阶段建立统一的 PTS/generation 边界，但不宣称已经实现
TTS 边生成边播放；后续流式供应商可直接产出相同的 :class:`AudioFrame`。
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from dataclasses import dataclass

try:
    import numpy as np
except Exception:  # pragma: no cover - 可选媒体依赖
    np = None

from server.core.media.audio_decode import decode_audio_to_float32
from server.core.media.audio_frame import (
    MAX_AUDIO_CHANNELS,
    MAX_AUDIO_SAMPLE_RATE,
    AudioFormat,
    AudioFrame,
    validate_audio_frame_batch,
)

logger = logging.getLogger("LiveAgent.AudioFramePipeline")

MAX_ENCODED_AUDIO_BYTES = 32 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class FramedAudio:
    """一个已通过整句事务屏障的连续 PCM 帧批次。"""

    frames: tuple[AudioFrame, ...]
    format: AudioFormat
    source_codec: str
    decode_ms: float

    def __post_init__(self) -> None:
        frame_list = validate_audio_frame_batch(self.frames)
        if any(frame.format != self.format for frame in frame_list):
            raise ValueError("FramedAudio.format 必须与所有帧一致")

    @property
    def audio_id(self) -> str:
        return self.frames[0].audio_id

    @property
    def total_samples(self) -> int:
        last = self.frames[-1]
        return last.pts_samples + last.duration_samples

    @property
    def duration_ms(self) -> float:
        return self.total_samples * 1000.0 / self.format.sample_rate

    def to_pcm_bytes(self) -> bytes:
        return b"".join(frame.data for frame in self.frames)


class AudioFramePipeline:
    """将完整音频事务转换为受时长和帧数限制的标准 PCM 帧。"""

    def __init__(self, frame_duration_ms: int = 40, max_duration_seconds: int = 30):
        if frame_duration_ms < 10 or frame_duration_ms > 200:
            raise ValueError("frame_duration_ms 必须位于 10~200ms")
        if max_duration_seconds < 1:
            raise ValueError("max_duration_seconds 必须大于 0")
        self.frame_duration_ms = int(frame_duration_ms)
        self.max_duration_seconds = int(max_duration_seconds)
        self._lock = threading.Lock()
        self._active_transactions = 0
        self._transactions_total = 0
        self._transactions_framed = 0
        self._transactions_fallback = 0
        self._transactions_cancelled = 0
        self._frames_total = 0
        self._pcm_bytes_total = 0
        self._last_decode_ms = 0.0
        self._last_duration_ms = 0.0
        self._last_started_audio_id: str | None = None
        self._last_audio_id: str | None = None
        self._last_outcome = ""
        self._last_error = ""

    async def frame_transaction(
        self,
        audio_bytes: bytes,
        *,
        source_codec: str,
        sample_rate: int,
        channels: int,
        audio_id: str,
        audio_generation: int,
        session_generation: int,
        text: str = "",
    ) -> FramedAudio | None:
        """异步帧化；失败时返回 ``None``，取消时等待底层解码自然回收统计。"""
        with self._lock:
            self._transactions_total += 1
            self._active_transactions += 1
            self._last_started_audio_id = audio_id
        worker = asyncio.create_task(
            asyncio.to_thread(
                self._frame_sync,
                audio_bytes,
                source_codec,
                sample_rate,
                channels,
                audio_id,
                audio_generation,
                session_generation,
                text,
            )
        )
        owns_active = True
        try:
            framed = await asyncio.shield(worker)
            pcm_bytes = sum(len(frame.data) for frame in framed.frames)
            with self._lock:
                self._transactions_framed += 1
                self._frames_total += len(framed.frames)
                self._pcm_bytes_total += pcm_bytes
                self._last_decode_ms = framed.decode_ms
                self._last_duration_ms = framed.duration_ms
                self._last_audio_id = audio_id
                self._last_outcome = "framed"
                self._last_error = ""
            return framed
        except asyncio.CancelledError:
            with self._lock:
                self._transactions_cancelled += 1
                self._last_audio_id = audio_id
                self._last_outcome = "cancelled"
                self._last_error = "cancelled"
            owns_active = False
            worker.add_done_callback(self._cancelled_worker_done)
            raise
        except Exception as exc:
            with self._lock:
                self._transactions_fallback += 1
                self._last_audio_id = audio_id
                self._last_outcome = "fallback"
                self._last_error = str(exc)
            logger.debug("音频帧化失败，回退兼容整句路径: %s", exc)
            return None
        finally:
            if owns_active:
                with self._lock:
                    self._active_transactions = max(0, self._active_transactions - 1)

    def _cancelled_worker_done(self, worker: asyncio.Task) -> None:
        """消费已取消调用留下的线程结果，并在真实退出后更新 in-flight。"""
        try:
            worker.result()
        except BaseException:
            pass
        with self._lock:
            self._active_transactions = max(0, self._active_transactions - 1)

    def _frame_sync(
        self,
        audio_bytes: bytes,
        source_codec: str,
        sample_rate: int,
        channels: int,
        audio_id: str,
        audio_generation: int,
        session_generation: int,
        text: str,
    ) -> FramedAudio:
        if not audio_bytes:
            raise ValueError("音频事务为空")
        if len(audio_bytes) > MAX_ENCODED_AUDIO_BYTES:
            raise ValueError(f"编码音频超过 {MAX_ENCODED_AUDIO_BYTES} bytes")
        if sample_rate <= 0 or sample_rate > MAX_AUDIO_SAMPLE_RATE:
            raise ValueError(f"声明采样率必须位于 1~{MAX_AUDIO_SAMPLE_RATE}")
        if channels <= 0 or channels > MAX_AUDIO_CHANNELS:
            raise ValueError(f"声明声道数必须位于 1~{MAX_AUDIO_CHANNELS}")
        started = time.perf_counter()
        samples, decoded_rate = decode_audio_to_float32(
            audio_bytes,
            sample_rate,
            codec=source_codec,
            channels=channels,
        )
        decode_ms = (time.perf_counter() - started) * 1000.0
        if samples is None or decoded_rate <= 0 or len(samples) == 0:
            raise ValueError(f"无法解码 source_codec={source_codec!r}")
        if np is None:
            raise RuntimeError("numpy 不可用")
        max_samples = decoded_rate * self.max_duration_seconds
        if len(samples) > max_samples:
            raise ValueError(
                f"单句音频 {len(samples) / decoded_rate:.2f}s 超过上限 "
                f"{self.max_duration_seconds}s"
            )

        normalized = np.clip(samples, -1.0, 1.0)
        pcm = (normalized * 32767.0).astype("<i2", copy=False).tobytes()
        audio_format = AudioFormat(sample_rate=int(decoded_rate), channels=1)
        frame_samples = max(1, round(decoded_rate * self.frame_duration_ms / 1000.0))
        frame_bytes = frame_samples * audio_format.bytes_per_sample_frame
        frame_count = (len(pcm) + frame_bytes - 1) // frame_bytes
        max_frames = (self.max_duration_seconds * 1000 + self.frame_duration_ms - 1) // self.frame_duration_ms
        if frame_count > max_frames:
            raise ValueError(f"帧数 {frame_count} 超过上限 {max_frames}")

        frames: list[AudioFrame] = []
        pts_samples = 0
        for sequence, start in enumerate(range(0, len(pcm), frame_bytes)):
            data = pcm[start:start + frame_bytes]
            frame = AudioFrame(
                data=data,
                format=audio_format,
                audio_id=audio_id,
                sequence=sequence,
                pts_samples=pts_samples,
                audio_generation=audio_generation,
                session_generation=session_generation,
                text=text if sequence == 0 else "",
                is_first=sequence == 0,
                is_final=sequence == frame_count - 1,
            )
            frames.append(frame)
            pts_samples += frame.duration_samples

        return FramedAudio(
            frames=tuple(frames),
            format=audio_format,
            source_codec=str(source_codec or "unknown"),
            decode_ms=decode_ms,
        )

    def get_status(self) -> dict:
        with self._lock:
            return {
                "stage": "transaction_post_decode",
                "true_streaming_tts": False,
                "frame_duration_ms": self.frame_duration_ms,
                "max_duration_seconds": self.max_duration_seconds,
                "max_encoded_audio_bytes": MAX_ENCODED_AUDIO_BYTES,
                "active_transactions": self._active_transactions,
                "transactions_total": self._transactions_total,
                "transactions_framed": self._transactions_framed,
                "transactions_fallback": self._transactions_fallback,
                "transactions_cancelled": self._transactions_cancelled,
                "frames_total": self._frames_total,
                "pcm_bytes_total": self._pcm_bytes_total,
                "last_decode_ms": round(self._last_decode_ms, 2),
                "last_duration_ms": round(self._last_duration_ms, 2),
                "last_started_audio_id": self._last_started_audio_id,
                "last_audio_id": self._last_audio_id,
                "last_outcome": self._last_outcome,
                "last_error": self._last_error,
            }


global_audio_frame_pipeline = AudioFramePipeline()
