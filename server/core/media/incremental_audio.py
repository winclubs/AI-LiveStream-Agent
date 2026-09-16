"""可取消的增量 PCM 帧化与原子暂存事务。

本模块只处理已经是 PCM16 的 TTS 输出。输入 transport chunk 可以落在任意字节位置，
事务会持续重组为固定时长 :class:`AudioFrame`，但在 ``finish()`` 前不会暴露批次，
从而保持“完整语义块成功后才提交”的可靠性契约。
"""

from __future__ import annotations

import threading
import time
from typing import Callable

from server.core.media.audio_frame import (
    DEFAULT_MAX_FRAME_BATCH_BYTES,
    AudioFormat,
    AudioFrame,
    validate_audio_frame_batch,
)

TerminalCallback = Callable[["IncrementalPCMTransaction", str, str], None]


class IncrementalPCMTransaction:
    """单个有界 PCM16 流事务；支持 append、finish 和 abort。"""

    def __init__(
        self,
        *,
        audio_format: AudioFormat,
        audio_id: str,
        audio_generation: int,
        session_generation: int,
        text: str,
        frame_duration_ms: int,
        max_duration_seconds: int,
        max_total_bytes: int,
        on_terminal: TerminalCallback,
    ) -> None:
        if not audio_id or not audio_id.strip():
            raise ValueError("audio_id 不能为空")
        if frame_duration_ms < 10 or frame_duration_ms > 200:
            raise ValueError("frame_duration_ms 必须位于 10~200ms")
        if max_duration_seconds < 1:
            raise ValueError("max_duration_seconds 必须大于 0")
        self.format = audio_format
        self.audio_id = audio_id
        self.audio_generation = int(audio_generation)
        self.session_generation = int(session_generation)
        self.text = str(text or "")
        self.frame_duration_ms = int(frame_duration_ms)
        self.max_duration_seconds = int(max_duration_seconds)
        duration_bytes = (
            self.format.sample_rate
            * self.format.bytes_per_sample_frame
            * self.max_duration_seconds
        )
        self.max_total_bytes = min(int(max_total_bytes), duration_bytes)
        self.frame_samples = max(
            1,
            round(self.format.sample_rate * self.frame_duration_ms / 1000.0),
        )
        self.frame_bytes = self.frame_samples * self.format.bytes_per_sample_frame
        self.state = "open"
        self.opened_at = time.monotonic()
        self.finished_at: float | None = None
        self.total_input_bytes = 0
        self._buffer = bytearray()
        self._pending_payload: bytes | None = None
        self._frames: list[AudioFrame] = []
        self._next_sequence = 0
        self._next_pts_samples = 0
        self._on_terminal = on_terminal
        self._terminal_notified = False

    @property
    def staged_frames(self) -> int:
        return len(self._frames) + (1 if self._pending_payload is not None else 0)

    @property
    def staged_bytes(self) -> int:
        return sum(len(frame.data) for frame in self._frames) + (
            len(self._pending_payload) if self._pending_payload is not None else 0
        ) + len(self._buffer)

    @property
    def capacity_remaining_bytes(self) -> int:
        return max(0, self.max_total_bytes - self.total_input_bytes)

    def _ensure_open(self) -> None:
        if self.state != "open":
            raise RuntimeError(f"PCM 事务状态为 {self.state}，不能继续写入")

    def append(self, chunk: bytes) -> int:
        """追加任意边界 PCM bytes，返回本次新形成的完整帧数量。"""
        self._ensure_open()
        if not isinstance(chunk, bytes):
            raise TypeError("PCM chunk 必须是 bytes")
        if not chunk:
            return 0
        if self.total_input_bytes + len(chunk) > self.max_total_bytes:
            self.abort("capacity_exceeded")
            raise ValueError(f"PCM 事务超过 {self.max_total_bytes} bytes")

        before = self.staged_frames
        self.total_input_bytes += len(chunk)
        self._buffer.extend(chunk)
        while len(self._buffer) >= self.frame_bytes:
            payload = bytes(self._buffer[: self.frame_bytes])
            del self._buffer[: self.frame_bytes]
            self._hold_back(payload)
        return self.staged_frames - before

    def _hold_back(self, payload: bytes) -> None:
        # 始终保留最后一个完整 payload，直到 finish 时才能正确标记唯一终帧。
        if self._pending_payload is not None:
            self._emit(self._pending_payload, is_final=False)
        self._pending_payload = payload

    def _emit(self, payload: bytes, *, is_final: bool) -> None:
        frame = AudioFrame(
            data=payload,
            format=self.format,
            audio_id=self.audio_id,
            sequence=self._next_sequence,
            pts_samples=self._next_pts_samples,
            audio_generation=self.audio_generation,
            session_generation=self.session_generation,
            text=self.text if self._next_sequence == 0 else "",
            is_first=self._next_sequence == 0,
            is_final=is_final,
        )
        self._frames.append(frame)
        self._next_sequence += 1
        self._next_pts_samples += frame.duration_samples

    def finish(self) -> tuple[AudioFrame, ...]:
        """封闭输入并原子返回完整帧批次；失败事务不会返回 partial。"""
        self._ensure_open()
        sample_width = self.format.bytes_per_sample_frame
        if len(self._buffer) % sample_width:
            self.abort("unaligned_final_sample")
            raise ValueError("PCM 流结束于不完整的样本边界")
        if self._buffer:
            if self._pending_payload is not None:
                self._emit(self._pending_payload, is_final=False)
            self._pending_payload = bytes(self._buffer)
            self._buffer.clear()
        if self._pending_payload is None:
            self.abort("empty_transaction")
            raise ValueError("PCM 事务为空")

        try:
            self._emit(self._pending_payload, is_final=True)
            self._pending_payload = None
            frames = validate_audio_frame_batch(
                self._frames,
                max_total_bytes=self.max_total_bytes,
                max_duration_seconds=self.max_duration_seconds,
            )
        except Exception:
            self.abort("commit_validation_failed")
            raise
        self.state = "committed"
        self.finished_at = time.monotonic()
        self._notify_terminal("committed", "")
        return frames

    def abort(self, reason: str = "cancelled") -> bool:
        """幂等取消事务并清除所有未提交 PCM。"""
        if self.state != "open":
            return False
        self.state = "aborted"
        self.finished_at = time.monotonic()
        self._buffer.clear()
        self._pending_payload = None
        self._frames.clear()
        self._notify_terminal("aborted", str(reason or "cancelled"))
        return True

    def _notify_terminal(self, outcome: str, reason: str) -> None:
        if self._terminal_notified:
            return
        self._terminal_notified = True
        self._on_terminal(self, outcome, reason)


class IncrementalAudioPipeline:
    """创建并统计有界增量 PCM 原子事务。"""

    def __init__(
        self,
        *,
        frame_duration_ms: int = 40,
        max_duration_seconds: int = 30,
        max_total_bytes: int = DEFAULT_MAX_FRAME_BATCH_BYTES,
        max_active_transactions: int = 8,
    ) -> None:
        self.frame_duration_ms = int(frame_duration_ms)
        self.max_duration_seconds = int(max_duration_seconds)
        self.max_total_bytes = int(max_total_bytes)
        self.max_active_transactions = int(max_active_transactions)
        self._lock = threading.Lock()
        self._active = 0
        self._opened = 0
        self._committed = 0
        self._aborted = 0
        self._frames = 0
        self._pcm_bytes = 0
        self._last_audio_id: str | None = None
        self._last_outcome = ""
        self._last_error = ""
        self._last_latency_ms = 0.0

    def open_transaction(
        self,
        *,
        sample_rate: int,
        channels: int,
        audio_id: str,
        audio_generation: int,
        session_generation: int,
        text: str = "",
    ) -> IncrementalPCMTransaction:
        audio_format = AudioFormat(sample_rate=int(sample_rate), channels=int(channels))
        with self._lock:
            if self._active >= self.max_active_transactions:
                raise RuntimeError("增量 PCM 活跃事务超过上限")
            self._active += 1
            self._opened += 1
        try:
            return IncrementalPCMTransaction(
                audio_format=audio_format,
                audio_id=audio_id,
                audio_generation=audio_generation,
                session_generation=session_generation,
                text=text,
                frame_duration_ms=self.frame_duration_ms,
                max_duration_seconds=self.max_duration_seconds,
                max_total_bytes=self.max_total_bytes,
                on_terminal=self._on_terminal,
            )
        except Exception:
            with self._lock:
                self._active = max(0, self._active - 1)
            raise

    def _on_terminal(
        self,
        transaction: IncrementalPCMTransaction,
        outcome: str,
        reason: str,
    ) -> None:
        with self._lock:
            self._active = max(0, self._active - 1)
            self._last_audio_id = transaction.audio_id
            self._last_outcome = outcome
            self._last_error = reason
            self._last_latency_ms = (
                max(0.0, (transaction.finished_at or time.monotonic()) - transaction.opened_at)
                * 1000.0
            )
            if outcome == "committed":
                self._committed += 1
                self._frames += transaction.staged_frames
                self._pcm_bytes += transaction.total_input_bytes
            else:
                self._aborted += 1

    def get_status(self) -> dict:
        with self._lock:
            return {
                "stage": "incremental_pcm_atomic_staging",
                "incremental_pcm": True,
                "progressive_playback": False,
                "true_streaming_tts": False,
                "transaction_mode": "atomic_staged",
                "frame_duration_ms": self.frame_duration_ms,
                "max_duration_seconds": self.max_duration_seconds,
                "max_total_bytes": self.max_total_bytes,
                "max_active_transactions": self.max_active_transactions,
                "active_transactions": self._active,
                "transactions_opened": self._opened,
                "transactions_committed": self._committed,
                "transactions_aborted": self._aborted,
                "frames_total": self._frames,
                "pcm_bytes_total": self._pcm_bytes,
                "last_audio_id": self._last_audio_id,
                "last_outcome": self._last_outcome,
                "last_error": self._last_error,
                "last_transaction_ms": round(self._last_latency_ms, 2),
            }


global_incremental_audio_pipeline = IncrementalAudioPipeline()
