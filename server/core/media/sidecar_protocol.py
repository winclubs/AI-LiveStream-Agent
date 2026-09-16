"""神经渲染 sidecar v3 的版本化控制面与二进制数据面契约。"""

from __future__ import annotations

import json
import struct
from dataclasses import dataclass
from typing import Any, Mapping

from server.core.media.audio_frame import AudioFrame

PROTOCOL_VERSION = 3
SUPPORTED_VERSIONS = (3,)
ENVELOPE_MAGIC = b"LAS3"
KIND_AUDIO = 1
KIND_VIDEO = 2
MAX_HEADER_BYTES = 64 * 1024
MAX_PAYLOAD_BYTES = 8 * 1024 * 1024
_PREFIX = struct.Struct("!4sBII")


@dataclass(frozen=True, slots=True)
class SidecarEnvelope:
    kind: int
    metadata: Mapping[str, Any]
    payload: bytes


@dataclass(frozen=True, slots=True)
class SidecarVideoFrame:
    request_id: str
    audio_id: str
    sequence: int
    pts_samples: int
    audio_generation: int
    session_generation: int
    jpeg: bytes


def encode_envelope(kind: int, metadata: Mapping[str, Any], payload: bytes) -> bytes:
    """编码带长度前缀的 v3 envelope，并在分配前实施固定上限。"""
    if kind not in {KIND_AUDIO, KIND_VIDEO}:
        raise ValueError(f"未知 sidecar envelope kind={kind}")
    if not isinstance(payload, bytes) or not payload:
        raise ValueError("sidecar payload 必须是非空 bytes")
    if len(payload) > MAX_PAYLOAD_BYTES:
        raise ValueError(f"sidecar payload 超过 {MAX_PAYLOAD_BYTES} bytes")
    header = json.dumps(dict(metadata), ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if not header or len(header) > MAX_HEADER_BYTES:
        raise ValueError("sidecar header 为空或超过容量上限")
    return _PREFIX.pack(ENVELOPE_MAGIC, kind, len(header), len(payload)) + header + payload


def decode_envelope(raw: bytes) -> SidecarEnvelope:
    """严格解码 v3 envelope；长度不一致、未知 kind 或非法 JSON 均拒绝。"""
    if not isinstance(raw, bytes) or len(raw) < _PREFIX.size:
        raise ValueError("sidecar envelope 太短")
    magic, kind, header_len, payload_len = _PREFIX.unpack(raw[: _PREFIX.size])
    if magic != ENVELOPE_MAGIC or kind not in {KIND_AUDIO, KIND_VIDEO}:
        raise ValueError("sidecar envelope magic 或 kind 非法")
    if header_len <= 0 or header_len > MAX_HEADER_BYTES:
        raise ValueError("sidecar header 长度非法")
    if payload_len <= 0 or payload_len > MAX_PAYLOAD_BYTES:
        raise ValueError("sidecar payload 长度非法")
    expected = _PREFIX.size + header_len + payload_len
    if len(raw) != expected:
        raise ValueError("sidecar envelope 声明长度与实际长度不一致")
    try:
        metadata = json.loads(raw[_PREFIX.size : _PREFIX.size + header_len].decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("sidecar header JSON 非法") from exc
    if not isinstance(metadata, dict):
        raise ValueError("sidecar header 必须是 JSON object")
    return SidecarEnvelope(kind=kind, metadata=metadata, payload=raw[-payload_len:])


def encode_audio_frame(request_id: str, frame: AudioFrame) -> bytes:
    """将领域 AudioFrame 无损编码为 v3 音频数据帧。"""
    if not request_id or len(request_id.encode("utf-8")) > 128:
        raise ValueError("request_id 为空或过长")
    metadata = {
        "request_id": request_id,
        "audio_id": frame.audio_id,
        "sequence": frame.sequence,
        "pts_samples": frame.pts_samples,
        "audio_generation": frame.audio_generation,
        "session_generation": frame.session_generation,
        "is_first": frame.is_first,
        "is_final": frame.is_final,
        "format": frame.format.to_dict(),
    }
    return encode_envelope(KIND_AUDIO, metadata, frame.data)


def decode_video_frame(raw: bytes) -> SidecarVideoFrame:
    """解析并校验 sidecar 返回的视频 JPEG 帧元数据。"""
    envelope = decode_envelope(raw)
    if envelope.kind != KIND_VIDEO:
        raise ValueError("收到的不是 sidecar 视频帧")
    metadata = envelope.metadata
    try:
        frame = SidecarVideoFrame(
            request_id=str(metadata["request_id"]),
            audio_id=str(metadata["audio_id"]),
            sequence=int(metadata["sequence"]),
            pts_samples=int(metadata["pts_samples"]),
            audio_generation=int(metadata["audio_generation"]),
            session_generation=int(metadata["session_generation"]),
            jpeg=envelope.payload,
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("sidecar 视频帧元数据缺失或非法") from exc
    if (
        not frame.request_id
        or not frame.audio_id
        or frame.sequence < 0
        or frame.pts_samples < 0
        or frame.audio_generation < 0
        or frame.session_generation < 0
    ):
        raise ValueError("sidecar 视频帧字段越界")
    return frame


def encode_video_frame(frame: SidecarVideoFrame) -> bytes:
    """供参考 sidecar 和协议 smoke 使用的视频帧编码器。"""
    return encode_envelope(
        KIND_VIDEO,
        {
            "request_id": frame.request_id,
            "audio_id": frame.audio_id,
            "sequence": frame.sequence,
            "pts_samples": frame.pts_samples,
            "audio_generation": frame.audio_generation,
            "session_generation": frame.session_generation,
        },
        frame.jpeg,
    )


def build_auth_message(token: str) -> dict[str, Any]:
    return {
        "event": "auth",
        "token": token,
        "supported_versions": list(SUPPORTED_VERSIONS),
        "protocol_version": PROTOCOL_VERSION,
    }


def validate_auth_reply(message: Mapping[str, Any]) -> dict[str, Any]:
    """验证能力协商结果；v3 driver 不会静默降级到 v1/v2 TTS 协议。"""
    if message.get("event") != "auth_ok":
        raise RuntimeError(str(message.get("message") or "sidecar 鉴权失败"))
    selected = int(message.get("selected_version") or message.get("protocol_version") or 0)
    if selected not in SUPPORTED_VERSIONS:
        raise RuntimeError(f"sidecar 未协商到受支持协议: v{selected}")
    capabilities = message.get("capabilities") or {}
    if not isinstance(capabilities, dict):
        raise RuntimeError("sidecar capabilities 必须是 object")
    return {
        "selected_version": selected,
        "node_version": str(message.get("node_version") or "unknown"),
        "capabilities": capabilities,
    }
