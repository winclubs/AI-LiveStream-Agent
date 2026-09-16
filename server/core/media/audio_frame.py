"""媒体平面统一音频帧与能力契约。

TTS 供应商返回的 chunk 可能只是容器或 HTTP 传输分片，不能直接视为可播放帧。
本模块中的 :class:`AudioFrame` 只表示已经解码、按采样边界切分的 PCM 数据，
为口型、播放、录制和实时传输提供同一套 sequence/PTS/generation 语义。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping


_PCM_S16_CODECS = {"pcm_s16le", "s16le", "pcm16"}
_CHUNK_SEMANTICS = {
    "transport_fragment",
    "complete_container",
    "pcm_frame",
    "transactional_sentence",
}

MAX_AUDIO_SAMPLE_RATE = 192_000
MAX_AUDIO_CHANNELS = 8
MAX_AUDIO_FRAME_BYTES = 1024 * 1024
MAX_AUDIO_FRAME_DURATION_SECONDS = 1
DEFAULT_MAX_FRAME_BATCH_BYTES = 64 * 1024 * 1024
DEFAULT_MAX_FRAME_BATCH_FRAMES = 3000
DEFAULT_MAX_FRAME_BATCH_SECONDS = 30


@dataclass(frozen=True, slots=True)
class AudioFormat:
    """音频样本格式；帧级媒体总线当前统一使用 little-endian PCM16。"""

    codec: str = "pcm_s16le"
    sample_rate: int = 24000
    channels: int = 1
    sample_width_bytes: int = 2

    def __post_init__(self) -> None:
        codec = str(self.codec or "").strip().lower()
        if codec not in _PCM_S16_CODECS:
            raise ValueError(f"AudioFrame 仅接受 PCM16，收到 codec={self.codec!r}")
        if self.sample_rate <= 0 or self.sample_rate > MAX_AUDIO_SAMPLE_RATE:
            raise ValueError(f"sample_rate 必须位于 1~{MAX_AUDIO_SAMPLE_RATE}")
        if self.channels <= 0 or self.channels > MAX_AUDIO_CHANNELS:
            raise ValueError(f"channels 必须位于 1~{MAX_AUDIO_CHANNELS}")
        if self.sample_width_bytes != 2:
            raise ValueError("PCM16 的 sample_width_bytes 必须为 2")
        object.__setattr__(self, "codec", "pcm_s16le")

    @property
    def bytes_per_sample_frame(self) -> int:
        """一个包含全部声道的采样点占用字节数。"""
        return self.channels * self.sample_width_bytes

    def to_dict(self) -> dict[str, Any]:
        return {
            "codec": self.codec,
            "sample_rate": self.sample_rate,
            "channels": self.channels,
            "sample_width_bytes": self.sample_width_bytes,
        }


@dataclass(frozen=True, slots=True)
class AudioFrame:
    """可独立排序、取消和计时的标准 PCM 音频帧。"""

    data: bytes
    format: AudioFormat
    audio_id: str
    sequence: int
    pts_samples: int
    audio_generation: int
    session_generation: int
    text: str = ""
    is_first: bool = False
    is_final: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.data, bytes) or not self.data:
            raise ValueError("AudioFrame.data 必须是非空 bytes")
        if not self.audio_id or not self.audio_id.strip():
            raise ValueError("audio_id 不能为空")
        if self.sequence < 0 or self.pts_samples < 0:
            raise ValueError("sequence 和 pts_samples 不能为负数")
        if self.audio_generation < 0 or self.session_generation < 0:
            raise ValueError("generation 不能为负数")
        if len(self.data) % self.format.bytes_per_sample_frame != 0:
            raise ValueError("PCM 数据未按样本和声道边界对齐")
        if len(self.data) > MAX_AUDIO_FRAME_BYTES:
            raise ValueError(f"单帧 PCM 超过 {MAX_AUDIO_FRAME_BYTES} bytes")
        if self.duration_samples > self.format.sample_rate * MAX_AUDIO_FRAME_DURATION_SECONDS:
            raise ValueError("单帧时长超过 1 秒")
        if self.sequence == 0 and not self.is_first:
            raise ValueError("sequence=0 的帧必须标记 is_first")
        if self.sequence > 0 and self.is_first:
            raise ValueError("只有 sequence=0 可以标记 is_first")

    @property
    def duration_samples(self) -> int:
        """返回每声道样本数。"""
        return len(self.data) // self.format.bytes_per_sample_frame

    @property
    def pts_ms(self) -> float:
        return self.pts_samples * 1000.0 / self.format.sample_rate

    @property
    def duration_ms(self) -> float:
        return self.duration_samples * 1000.0 / self.format.sample_rate

    def metadata(self) -> dict[str, Any]:
        """构造向兼容驱动传递的无二进制元数据。"""
        return {
            **self.format.to_dict(),
            "audio_id": self.audio_id,
            "sequence": self.sequence,
            "pts_samples": self.pts_samples,
            "pts_ms": self.pts_ms,
            "duration_samples": self.duration_samples,
            "duration_ms": self.duration_ms,
            "audio_generation": self.audio_generation,
            "session_generation": self.session_generation,
            "is_first": self.is_first,
            "is_final": self.is_final,
        }


def validate_audio_frame_batch(
    frames: Iterable[AudioFrame],
    *,
    require_complete: bool = True,
    max_total_bytes: int = DEFAULT_MAX_FRAME_BATCH_BYTES,
    max_frames: int = DEFAULT_MAX_FRAME_BATCH_FRAMES,
    max_duration_seconds: int = DEFAULT_MAX_FRAME_BATCH_SECONDS,
) -> tuple[AudioFrame, ...]:
    """统一校验帧批次的身份、代际、时序、终止标记和容量。"""
    frame_list = tuple(frames or ())
    if not frame_list:
        raise ValueError("AudioFrame 批次不能为空")
    if len(frame_list) > max_frames:
        raise ValueError(f"帧数 {len(frame_list)} 超过上限 {max_frames}")

    first = frame_list[0]
    expected_pts = 0
    total_bytes = 0
    last_index = len(frame_list) - 1
    for index, frame in enumerate(frame_list):
        if (
            frame.audio_id != first.audio_id
            or frame.format != first.format
            or frame.audio_generation != first.audio_generation
            or frame.session_generation != first.session_generation
        ):
            raise ValueError("帧批次的格式、audio_id 和 generation 必须一致")
        if frame.sequence != index or frame.pts_samples != expected_pts:
            raise ValueError("帧批次必须按 sequence 和 PTS 连续排列")
        if frame.is_first != (index == 0):
            raise ValueError("帧批次只能在 sequence=0 标记首帧")
        if frame.is_final and index != last_index:
            raise ValueError("只有批次最后一帧可以标记终帧")
        expected_pts += frame.duration_samples
        total_bytes += len(frame.data)
        if total_bytes > max_total_bytes:
            raise ValueError(f"帧批次 PCM 超过 {max_total_bytes} bytes")

    if require_complete and not frame_list[-1].is_final:
        raise ValueError("完整帧批次的最后一帧必须标记终帧")
    if expected_pts > first.format.sample_rate * max_duration_seconds:
        raise ValueError(f"帧批次时长超过 {max_duration_seconds} 秒")
    return frame_list


@dataclass(frozen=True, slots=True)
class MediaCapabilities:
    """驱动可机器读取的媒体能力声明，默认值保持旧驱动兼容。"""

    accepts_audio_frames: bool = False
    audio_frame_codec: str = "pcm_s16le"
    tts_output_codec: str = "mp3"
    chunk_semantics: str = "transport_fragment"
    transactional_sentence: bool = True
    supports_cancel: bool = True
    supports_shared_clock: bool = False
    remote_protocol_version: int | None = None
    extra: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        semantics = str(self.chunk_semantics or "").strip().lower()
        if semantics not in _CHUNK_SEMANTICS:
            raise ValueError(f"未知 chunk_semantics={self.chunk_semantics!r}")
        if self.remote_protocol_version is not None and self.remote_protocol_version < 1:
            raise ValueError("remote_protocol_version 必须大于等于 1")
        object.__setattr__(self, "chunk_semantics", semantics)

    def to_dict(self) -> dict[str, Any]:
        return {
            "accepts_audio_frames": self.accepts_audio_frames,
            "audio_frame_codec": self.audio_frame_codec,
            "tts_output_codec": self.tts_output_codec,
            "chunk_semantics": self.chunk_semantics,
            "transactional_sentence": self.transactional_sentence,
            "supports_cancel": self.supports_cancel,
            "supports_shared_clock": self.supports_shared_clock,
            "remote_protocol_version": self.remote_protocol_version,
            **dict(self.extra),
        }
