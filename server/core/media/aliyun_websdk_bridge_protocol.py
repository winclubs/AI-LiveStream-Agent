"""AI-LiveStream-Agent 到本地阿里云 WebSDK bridge 的私有 IPC 协议。

这不是阿里云官方 API 或 RTC 协议。bridge 仅负责调用公开的
lm-avatar-chat-sdk API，并把可观察回调如实转发给 Python 端。
"""

from __future__ import annotations

import json
import struct
from dataclasses import dataclass
from typing import Any, Mapping

from server.core.media.audio_frame import AudioFormat, AudioFrame

PROTOCOL_NAME = "las_aliyun_websdk_bridge"
PROTOCOL_VERSION = 1
SUPPORTED_VERSIONS = (1,)
SDK_PACKAGE = "lm-avatar-chat-sdk"
PINNED_SDK_VERSION = "1.1.0"
ENVELOPE_MAGIC = b"LASA"
KIND_AUDIO = 1
MAX_HEADER_BYTES = 32 * 1024
MAX_AUDIO_BYTES = 1024 * 1024
MAX_CONTROL_BYTES = 256 * 1024
_PREFIX = struct.Struct("!4sBII")


@dataclass(frozen=True, slots=True)
class AliyunBridgeEnvelope:
    kind: int
    metadata: Mapping[str, Any]
    payload: bytes


def build_hello_message(bridge_auth_token: str, expected_sdk_sha256: str) -> dict[str, Any]:
    if not bridge_auth_token:
        raise ValueError("bridge_auth_token 不能为空")
    return {
        "event": "bridge_hello",
        "protocol_name": PROTOCOL_NAME,
        "supported_versions": list(SUPPORTED_VERSIONS),
        "protocol_version": PROTOCOL_VERSION,
        "bridge_auth_token": bridge_auth_token,
        "expected_sdk": {
            "package": SDK_PACKAGE,
            "version": PINNED_SDK_VERSION,
            "sha256": expected_sdk_sha256,
        },
    }


def validate_hello_reply(
    message: Mapping[str, Any],
    *,
    expected_sdk_sha256: str,
) -> dict[str, Any]:
    if message.get("event") != "bridge_hello_ok":
        raise ValueError(str(message.get("message") or "bridge 握手失败"))
    if message.get("protocol_name") != PROTOCOL_NAME:
        raise ValueError("bridge protocol_name 不匹配")
    selected_version = message.get("selected_version")
    if isinstance(selected_version, bool) or not isinstance(selected_version, int):
        raise ValueError("bridge selected_version 必须是 int")
    if selected_version not in SUPPORTED_VERSIONS:
        raise ValueError(f"bridge 未协商到受支持版本: {selected_version}")
    sdk = message.get("sdk")
    if not isinstance(sdk, Mapping):
        raise ValueError("bridge 未返回 SDK evidence")
    if sdk.get("package") != SDK_PACKAGE or sdk.get("version") != PINNED_SDK_VERSION:
        raise ValueError("bridge 未加载固定版本 lm-avatar-chat-sdk")
    if expected_sdk_sha256 and sdk.get("sha256") != expected_sdk_sha256:
        raise ValueError("bridge SDK sha256 与配置不一致")
    return {
        "selected_version": selected_version,
        "bridge_build": str(message.get("bridge_build") or "unknown"),
        "sdk_package": SDK_PACKAGE,
        "sdk_version": PINNED_SDK_VERSION,
        "sdk_sha256": str(sdk.get("sha256") or ""),
    }


def validate_session_ready(message: Mapping[str, Any]) -> AudioFormat:
    if message.get("event") != "session_ready":
        raise ValueError(str(message.get("message") or "bridge session 未就绪"))
    raw_format = message.get("input_format")
    if not isinstance(raw_format, Mapping):
        raise ValueError("bridge session_ready 缺少 input_format")
    try:
        codec = raw_format["codec"]
        sample_rate = raw_format["sample_rate"]
        channels = raw_format["channels"]
        sample_width = raw_format["sample_width_bytes"]
    except KeyError as exc:
        raise ValueError("bridge input_format 字段不完整") from exc
    if not isinstance(codec, str):
        raise ValueError("bridge input_format.codec 必须是 string")
    for field, value in (
        ("sample_rate", sample_rate),
        ("channels", channels),
        ("sample_width_bytes", sample_width),
    ):
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(f"bridge input_format.{field} 必须是 int")
    return AudioFormat(
        codec=codec,
        sample_rate=sample_rate,
        channels=channels,
        sample_width_bytes=sample_width,
    )


def encode_audio_frame(request_id: str, frame: AudioFrame) -> bytes:
    if not request_id or len(request_id.encode("utf-8")) > 128:
        raise ValueError("request_id 为空或过长")
    metadata = {
        "request_id": request_id,
        "audio_id": frame.audio_id,
        "sequence": frame.sequence,
        "input_pts_samples": frame.pts_samples,
        "audio_generation": frame.audio_generation,
        "session_generation": frame.session_generation,
        "is_first": frame.is_first,
        "is_final": frame.is_final,
        "format": frame.format.to_dict(),
    }
    header = json.dumps(metadata, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if not header or len(header) > MAX_HEADER_BYTES:
        raise ValueError("bridge audio header 为空或过大")
    if not frame.data or len(frame.data) > MAX_AUDIO_BYTES:
        raise ValueError("bridge audio payload 为空或过大")
    return _PREFIX.pack(
        ENVELOPE_MAGIC,
        KIND_AUDIO,
        len(header),
        len(frame.data),
    ) + header + frame.data


def decode_audio_frame(raw: bytes) -> AliyunBridgeEnvelope:
    """供 bridge 实现和本地协议验证严格解析输入音频 envelope。"""
    if not isinstance(raw, bytes) or len(raw) < _PREFIX.size:
        raise ValueError("bridge envelope 太短")
    magic, kind, header_len, payload_len = _PREFIX.unpack(raw[: _PREFIX.size])
    if magic != ENVELOPE_MAGIC or kind != KIND_AUDIO:
        raise ValueError("bridge envelope magic 或 kind 非法")
    if header_len <= 0 or header_len > MAX_HEADER_BYTES:
        raise ValueError("bridge header 长度非法")
    if payload_len <= 0 or payload_len > MAX_AUDIO_BYTES:
        raise ValueError("bridge payload 长度非法")
    expected = _PREFIX.size + header_len + payload_len
    if len(raw) != expected:
        raise ValueError("bridge envelope 声明长度与实际长度不一致")
    try:
        metadata = json.loads(raw[_PREFIX.size : _PREFIX.size + header_len].decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("bridge header JSON 非法") from exc
    if not isinstance(metadata, dict):
        raise ValueError("bridge header 必须是 object")
    return AliyunBridgeEnvelope(
        kind=kind,
        metadata=metadata,
        payload=raw[-payload_len:],
    )
