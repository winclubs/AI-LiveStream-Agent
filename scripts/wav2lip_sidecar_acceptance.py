# -*- coding: utf-8 -*-
"""Black-box acceptance and stability runner for an already running v3 sidecar."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import math
import ssl
import struct
import sys
import time
import uuid
import wave
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlparse

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from server.core.media.audio_frame import AudioFormat, AudioFrame, validate_audio_frame_batch  # noqa: E402
from server.core.media.sidecar_protocol import (  # noqa: E402
    PROTOCOL_VERSION,
    build_auth_message,
    decode_video_frame,
    encode_audio_frame,
    validate_auth_reply,
)

logger = logging.getLogger("LiveAgent.Wav2LipSidecarAcceptance")
SCHEMA_VERSION = "wav2lip-sidecar-acceptance/v1"
MAX_INITIAL_CREDIT = 256
MAX_TRANSACTION_MESSAGES = 200_000
MAX_VIDEO_FRAMES = 100_000
CONNECT_TIMEOUT_SECONDS = 3.0
MESSAGE_TIMEOUT_SECONDS = 20.0
POST_CANCEL_GUARD_SECONDS = 0.25
RENDER_STARTED_TIMEOUT_SECONDS = 5.0
MIB = 1024.0 * 1024.0
CUDA_RESOURCE_KEYS = (
    "memory_allocated_bytes",
    "memory_reserved_bytes",
    "max_memory_allocated_bytes",
    "max_memory_reserved_bytes",
)


class BlockedError(RuntimeError):
    """The target cannot execute a GPU/neural acceptance run."""


class AcceptanceError(RuntimeError):
    """The target responded but violated protocol or a required criterion."""


@dataclass(slots=True)
class ResourceSample:
    elapsed_seconds: float
    stats: dict[str, Any]


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()


def _criterion(status: str, detail: str, **metrics: Any) -> dict[str, Any]:
    value: dict[str, Any] = {"status": status, "detail": detail}
    value.update(metrics)
    return value


def _base_report(args: argparse.Namespace) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "status": "FAIL",
        "timestamp": _timestamp(),
        "target_url": args.url,
        "audio_fixture": None,
        "descriptor": None,
        "criteria": {
            "authentication_descriptor": _criterion("FAIL", "not run"),
            "performance_transaction": _criterion("FAIL", "not run"),
            "cancellation_transaction": _criterion("FAIL", "not run"),
            "resource_telemetry": _criterion("FAIL", "not run"),
            "long_stability": _criterion("FAIL", "not run"),
        },
        "performance": None,
        "cancellation": None,
        "stability": None,
        "limitations": {
            "physical_av_presentation": "NOT_MEASURED",
            "subjective_lipsync_quality": "NOT_MEASURED",
        },
        "errors": [],
    }


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="连接已启动的 v3 GPU sidecar，执行性能、取消和长稳验收；不会评价主观口型质量",
    )
    parser.add_argument("--url", required=True, help="v3 WebSocket URL，例如 ws://127.0.0.1:8890/ws/render-v3")
    parser.add_argument("--token", default="", help="sidecar 鉴权 token")
    parser.add_argument("--tls-ca", default="", help="可选自签 CA PEM；仍严格验证证书和主机名")
    parser.add_argument("--backend-id", required=True)
    parser.add_argument("--avatar-id", required=True)
    parser.add_argument("--avatar-revision", required=True)
    parser.add_argument("--avatar-digest", required=True)
    parser.add_argument("--license-manifest-digest", required=True)
    parser.add_argument("--weights-sha256", required=True)
    parser.add_argument("--model-version", required=True)
    parser.add_argument("--audio-wav", help="可选 PCM16 WAV；不提供时生成短 sine，仅验证 transport")
    parser.add_argument("--stability-minutes", type=float, default=30.0)
    parser.add_argument("--report", help="可选 JSON 报告文件；stdout 仍输出相同 JSON")
    parser.add_argument("--min-fps", type=float, default=20.0)
    parser.add_argument("--max-real-time-factor", type=float, default=1.0)
    parser.add_argument("--max-first-frame-ms", type=float, default=5000.0)
    parser.add_argument(
        "--max-endpoint-pts-gap-ms",
        dest="max_endpoint_pts_gap_ms",
        type=float,
        default=120.0,
        help="协议时间线末帧起始 PTS 到音频末端的最大 gap；不表示物理 A/V drift",
    )
    parser.add_argument("--min-frame-coverage", type=float, default=0.9)
    parser.add_argument("--max-pts-cadence-error-ms", type=float, default=12.0)
    parser.add_argument("--transaction-timeout-seconds", type=float, default=60.0)
    parser.add_argument("--max-cancel-ms", type=float, default=1000.0)
    parser.add_argument(
        "--render-started-timeout-seconds",
        type=float,
        default=RENDER_STARTED_TIMEOUT_SECONDS,
        help="首个音频发送后等待同事务 inference-active barrier 的独立超时",
    )
    parser.add_argument("--max-vram-slope-mib-min", type=float, default=8.0)
    parser.add_argument("--max-reserved-vram-slope-mib-min", type=float, default=8.0)
    parser.add_argument("--max-vram-allocated-growth-mib", type=float, default=512.0)
    parser.add_argument("--max-vram-reserved-growth-mib", type=float, default=512.0)
    parser.add_argument("--max-rss-slope-mib-min", type=float, default=64.0)
    args = parser.parse_args(argv)
    text_fields = (
        "url",
        "backend_id",
        "avatar_id",
        "avatar_revision",
        "avatar_digest",
        "license_manifest_digest",
        "weights_sha256",
        "model_version",
    )
    for field in text_fields:
        if not isinstance(getattr(args, field), str) or not getattr(args, field).strip():
            parser.error(f"--{field.replace('_', '-')} 必须是非空字符串")
    numeric = (
        "stability_minutes",
        "min_fps",
        "max_real_time_factor",
        "max_first_frame_ms",
        "max_endpoint_pts_gap_ms",
        "min_frame_coverage",
        "max_pts_cadence_error_ms",
        "transaction_timeout_seconds",
        "max_cancel_ms",
        "render_started_timeout_seconds",
        "max_vram_slope_mib_min",
        "max_reserved_vram_slope_mib_min",
        "max_vram_allocated_growth_mib",
        "max_vram_reserved_growth_mib",
        "max_rss_slope_mib_min",
    )
    for field in numeric:
        value = getattr(args, field)
        if not math.isfinite(value) or value < 0:
            parser.error(f"--{field.replace('_', '-')} 必须是有限非负数")
    if not 0 < args.min_frame_coverage <= 1:
        parser.error("--min-frame-coverage 必须位于 (0, 1]")
    if args.transaction_timeout_seconds <= 0:
        parser.error("--transaction-timeout-seconds 必须大于 0")
    if args.render_started_timeout_seconds <= 0:
        parser.error("--render-started-timeout-seconds 必须大于 0")
    return args


def _decode_control(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, str):
        raise AcceptanceError("期望 control JSON，却收到二进制消息")
    try:
        message = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise AcceptanceError("sidecar control 不是合法 JSON") from exc
    if not isinstance(message, dict):
        raise AcceptanceError("sidecar control 顶层必须是 object")
    return message


def _remaining_timeout(deadline: float | None, maximum: float) -> float:
    if deadline is None:
        return maximum
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise AcceptanceError("absolute transaction/run deadline exceeded")
    return min(maximum, remaining)


async def _send(websocket: Any, payload: str | bytes, deadline: float | None) -> None:
    timeout = _remaining_timeout(deadline, MESSAGE_TIMEOUT_SECONDS)
    try:
        await asyncio.wait_for(websocket.send(payload), timeout=timeout)
    except TimeoutError as exc:
        raise AcceptanceError(f"发送 sidecar 消息超时 ({timeout:.3f}s)") from exc


async def _recv(
    websocket: Any,
    timeout: float = MESSAGE_TIMEOUT_SECONDS,
    deadline: float | None = None,
) -> Any:
    effective_timeout = _remaining_timeout(deadline, timeout)
    try:
        return await asyncio.wait_for(websocket.recv(), timeout=effective_timeout)
    except TimeoutError as exc:
        raise AcceptanceError(f"等待 sidecar 消息超时 ({effective_timeout:.3f}s)") from exc


def _strict_int(mapping: Mapping[str, Any], field: str, context: str, minimum: int = 0) -> int:
    value = mapping.get(field)
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise AcceptanceError(f"{context}.{field} 必须是 >= {minimum} 的严格 int")
    return value


def _descriptor_from_capabilities(capabilities: Mapping[str, Any], args: argparse.Namespace) -> dict[str, Any]:
    if capabilities.get("renderer_available") is not True or capabilities.get("neural_lipsync") is not True:
        raise BlockedError("sidecar 未声明可用 neural renderer")
    if capabilities.get("strict_completion") is not True or capabilities.get("supports_credit") is not True:
        raise AcceptanceError("sidecar 未声明 strict_completion/supports_credit")
    if capabilities.get("streaming_video") is not True or capabilities.get("supports_cancel_ack") is not True:
        raise AcceptanceError("sidecar 未声明 streaming_video/supports_cancel_ack")
    if capabilities.get("cancel_threadsafe") is not True or capabilities.get("cancel_quiesces") is not True:
        raise AcceptanceError("sidecar 未声明严格线程安全且静止语义 cancel")
    if capabilities.get("supports_render_started") is not True:
        raise AcceptanceError("strict neural sidecar 未声明 supports_render_started")
    backends = capabilities.get("render_backends")
    if not isinstance(backends, list):
        raise AcceptanceError("capabilities.render_backends 必须是数组")
    descriptor = next(
        (
            dict(item)
            for item in backends
            if isinstance(item, dict) and item.get("id") == args.backend_id
        ),
        None,
    )
    if descriptor is None:
        raise BlockedError("目标节点没有请求的 backend descriptor")
    for field in ("available", "neural", "warmed", "license_approved"):
        if descriptor.get(field) is not True:
            raise BlockedError(f"backend descriptor {field} 未严格为 true")
    expected = {
        "id": args.backend_id,
        "avatar_id": args.avatar_id,
        "avatar_revision": args.avatar_revision,
        "avatar_digest": args.avatar_digest,
        "license_manifest_sha256": args.license_manifest_digest,
        "weights_sha256": args.weights_sha256,
        "model_version": args.model_version,
    }
    for field, expected_value in expected.items():
        actual = descriptor.get(field)
        if not isinstance(actual, str) or not actual or actual != expected_value:
            raise AcceptanceError(f"握手 descriptor.{field} 与验收参数不一致")
    for field in ("gpu_device", "gpu_name", "gpu_compute_capability"):
        if not isinstance(descriptor.get(field), str) or not descriptor[field].strip():
            raise BlockedError(f"backend descriptor 缺少 GPU 字段 {field}")
    return descriptor


def _validate_evidence(message: Mapping[str, Any], descriptor: Mapping[str, Any], context: str) -> None:
    expected = {
        "backend_id": descriptor["id"],
        "model_version": descriptor["model_version"],
        "weights_sha256": descriptor["weights_sha256"],
        "license_manifest_sha256": descriptor["license_manifest_sha256"],
        "license_approved": True,
        "avatar_id": descriptor["avatar_id"],
        "avatar_revision": descriptor["avatar_revision"],
        "avatar_digest": descriptor["avatar_digest"],
    }
    for field, expected_value in expected.items():
        if message.get(field) != expected_value:
            raise AcceptanceError(f"{context}.{field} 与 auth descriptor 不一致")


def _extract_resource_stats(message: Mapping[str, Any]) -> dict[str, Any] | None:
    value = message.get("resource_stats")
    return dict(value) if isinstance(value, dict) else None


def _resource_complete(stats: Mapping[str, Any] | None) -> bool:
    if not stats or stats.get("cuda_available") is not True:
        return False
    return all(
        isinstance(stats.get(field), int) and not isinstance(stats.get(field), bool)
        for field in CUDA_RESOURCE_KEYS
    )


def _load_wav(path: str) -> tuple[bytes, AudioFormat]:
    wav_path = Path(path).expanduser().resolve()
    if not wav_path.is_file():
        raise AcceptanceError(f"WAV 不存在: {wav_path}")
    try:
        with wave.open(str(wav_path), "rb") as source:
            if source.getcomptype() != "NONE" or source.getsampwidth() != 2:
                raise AcceptanceError("--audio-wav 只接受未压缩 PCM16 WAV")
            channels = source.getnchannels()
            sample_rate = source.getframerate()
            frame_count = source.getnframes()
            if channels < 1 or sample_rate < 1 or frame_count < 1:
                raise AcceptanceError("WAV channels/sample_rate/frames 非法")
            pcm = source.readframes(frame_count)
    except (EOFError, wave.Error) as exc:
        raise AcceptanceError(f"WAV 解析失败: {exc}") from exc
    audio_format = AudioFormat(sample_rate=sample_rate, channels=channels, sample_width_bytes=2)
    if len(pcm) != frame_count * audio_format.bytes_per_sample_frame:
        raise AcceptanceError("WAV PCM 数据长度与 header 不一致")
    return pcm, audio_format


def _synthetic_pcm(audio_format: AudioFormat, duration_seconds: float = 1.0) -> bytes:
    sample_count = max(1, int(audio_format.sample_rate * duration_seconds))
    values = bytearray()
    for index in range(sample_count):
        sample = int(1200 * math.sin(2.0 * math.pi * 220.0 * index / audio_format.sample_rate))
        packed = struct.pack("<h", sample)
        values.extend(packed * audio_format.channels)
    return bytes(values)


def _capability_formats(capabilities: Mapping[str, Any]) -> list[AudioFormat]:
    values = capabilities.get("input_formats")
    if not isinstance(values, list):
        raise AcceptanceError("capabilities.input_formats 必须是数组")
    formats: list[AudioFormat] = []
    for value in values:
        if not isinstance(value, dict):
            continue
        try:
            sample_rate = _strict_int(value, "sample_rate", "input_format", 1)
            channels = _strict_int(value, "channels", "input_format", 1)
            sample_width = _strict_int(value, "sample_width_bytes", "input_format", 1)
            formats.append(
                AudioFormat(
                    codec=value.get("codec", ""),
                    sample_rate=sample_rate,
                    channels=channels,
                    sample_width_bytes=sample_width,
                )
            )
        except (AcceptanceError, ValueError):
            continue
    if not formats:
        raise AcceptanceError("sidecar 没有可用 PCM16 input_formats")
    return formats


def _frame_audio(pcm: bytes, audio_format: AudioFormat, fps: float, generation: int) -> tuple[AudioFrame, ...]:
    frame_duration = max(1, int(round(audio_format.sample_rate / fps)))
    bytes_per_frame = frame_duration * audio_format.bytes_per_sample_frame
    chunks = [pcm[offset : offset + bytes_per_frame] for offset in range(0, len(pcm), bytes_per_frame)]
    if not chunks or any(not chunk for chunk in chunks):
        raise AcceptanceError("PCM 标准帧化未产生有效帧")
    audio_id = uuid.uuid4().hex
    frames: list[AudioFrame] = []
    pts = 0
    for sequence, chunk in enumerate(chunks):
        frame = AudioFrame(
            data=chunk,
            format=audio_format,
            audio_id=audio_id,
            sequence=sequence,
            pts_samples=pts,
            audio_generation=generation,
            session_generation=1,
            text="acceptance transport fixture",
            is_first=sequence == 0,
            is_final=sequence == len(chunks) - 1,
        )
        frames.append(frame)
        pts += frame.duration_samples
    try:
        return validate_audio_frame_batch(frames)
    except ValueError as exc:
        raise AcceptanceError(str(exc)) from exc


def _validate_accepted(
    message: Mapping[str, Any],
    request_id: str,
    descriptor: Mapping[str, Any],
    frames: tuple[AudioFrame, ...],
) -> int:
    if message.get("event") == "error":
        raise AcceptanceError(str(message.get("message") or "render_open rejected"))
    if message.get("event") != "render_accepted" or message.get("request_id") != request_id:
        raise AcceptanceError("render_accepted 身份或事件非法")
    _validate_evidence(message, descriptor, "render_accepted")
    credit = _strict_int(message, "initial_credit", "render_accepted", 1)
    if credit > MAX_INITIAL_CREDIT:
        raise AcceptanceError("render_accepted.initial_credit 超过协议上限")
    totals = message.get("strict_totals")
    if not isinstance(totals, dict):
        raise AcceptanceError("render_accepted 缺少 strict_totals")
    expected = {
        "declared_frames": len(frames),
        "declared_samples": sum(frame.duration_samples for frame in frames),
        "frame_duration_samples": frames[0].duration_samples,
    }
    for field, value in expected.items():
        if _strict_int(totals, field, "render_accepted.strict_totals") != value:
            raise AcceptanceError(f"render_accepted.strict_totals.{field} 不一致")
    return credit


def _accept_video(raw: bytes, request_id: str, first: AudioFrame, ledger: dict[str, Any]) -> None:
    try:
        frame = decode_video_frame(raw)
    except ValueError as exc:
        raise AcceptanceError(str(exc)) from exc
    if (
        frame.request_id != request_id
        or frame.audio_id != first.audio_id
        or frame.audio_generation != first.audio_generation
        or frame.session_generation != first.session_generation
    ):
        raise AcceptanceError("JPEG 帧事务身份不一致")
    if not frame.jpeg.startswith(b"\xff\xd8") or not frame.jpeg.endswith(b"\xff\xd9"):
        raise AcceptanceError("JPEG SOI/EOI 非法")
    if frame.sequence != ledger["frames"]:
        raise AcceptanceError("视频 sequence 不连续")
    if ledger["frames"] >= MAX_VIDEO_FRAMES:
        raise AcceptanceError("视频帧数超过验收工具上限")
    if frame.pts_samples >= ledger["total_samples"]:
        raise AcceptanceError("视频 PTS 超出音频范围")
    if ledger["last_pts_samples"] is not None and frame.pts_samples < ledger["last_pts_samples"]:
        raise AcceptanceError("视频 PTS 非单调")
    ledger["frames"] += 1
    ledger["bytes"] += len(frame.jpeg)
    ledger["last_pts_samples"] = frame.pts_samples
    ledger["pts_samples"].append(frame.pts_samples)


def _validate_render_started(
    message: Mapping[str, Any],
    request_id: str,
    *,
    expected_kind: str | None = None,
    expected_sequence: int | None = None,
) -> None:
    if message.get("request_id") != request_id:
        raise AcceptanceError("render_started.request_id 不匹配")
    kind = message.get("kind")
    if kind not in {"audio", "finish"}:
        raise AcceptanceError("render_started.kind 非法")
    sequence = _strict_int(message, "sequence", "render_started")
    if expected_kind is not None and kind != expected_kind:
        raise AcceptanceError("render_started.kind 与等待的推理调用不匹配")
    if expected_sequence is not None and sequence != expected_sequence:
        raise AcceptanceError("render_started.sequence 与等待的推理调用不匹配")


def _consume_control(message: Mapping[str, Any], request_id: str, credit: int) -> int:
    if message.get("request_id") != request_id:
        raise AcceptanceError("收到其他事务的 control 消息")
    event = message.get("event")
    if event == "error":
        raise AcceptanceError(str(message.get("message") or message.get("code") or "sidecar error"))
    if event == "render_credit":
        granted = _strict_int(message, "credit", "render_credit", 1)
        if credit + granted > MAX_INITIAL_CREDIT:
            raise AcceptanceError("credit ledger 超过协议上限")
        return credit + granted
    if event == "render_started":
        _validate_render_started(message, request_id)
        return credit
    raise AcceptanceError(f"事务中收到意外 control 事件: {event!r}")


def _validate_complete(
    complete: Mapping[str, Any],
    request_id: str,
    descriptor: Mapping[str, Any],
    frames: tuple[AudioFrame, ...],
    ledger: Mapping[str, Any],
) -> None:
    first = frames[0]
    total_samples = sum(frame.duration_samples for frame in frames)
    if complete.get("request_id") != request_id or complete.get("audio_id") != first.audio_id:
        raise AcceptanceError("render_complete 事务身份不一致")
    _validate_evidence(complete, descriptor, "render_complete")
    if _strict_int(complete, "rendered_frames", "render_complete") != ledger["frames"]:
        raise AcceptanceError("render_complete.rendered_frames 与接收 JPEG 数不一致")
    if _strict_int(complete, "last_pts_samples", "render_complete") != total_samples:
        raise AcceptanceError("render_complete.last_pts_samples 与音频末端不一致")
    if ledger["frames"] < 1:
        raise AcceptanceError("strict completion 未返回任何 JPEG")
    totals = complete.get("strict_totals")
    if not isinstance(totals, dict):
        raise AcceptanceError("render_complete 缺少 strict_totals")
    expected = {
        "declared_frames": len(frames),
        "declared_samples": total_samples,
        "frame_duration_samples": first.duration_samples,
        "received_frames": len(frames),
        "received_samples": total_samples,
        "received_bytes": sum(len(frame.data) for frame in frames),
        "rendered_frames": ledger["frames"],
        "rendered_bytes": ledger["bytes"],
        "last_video_pts_samples": ledger["last_pts_samples"],
    }
    for field, value in expected.items():
        if _strict_int(totals, field, "render_complete.strict_totals") != value:
            raise AcceptanceError(f"render_complete.strict_totals.{field} 不一致")


async def _render_transaction(
    websocket: Any,
    args: argparse.Namespace,
    descriptor: Mapping[str, Any],
    frames: tuple[AudioFrame, ...],
    fps: float,
    absolute_deadline: float | None = None,
) -> dict[str, Any]:
    first = frames[0]
    request_id = uuid.uuid4().hex
    total_samples = sum(frame.duration_samples for frame in frames)
    transaction_deadline = time.monotonic() + args.transaction_timeout_seconds
    if absolute_deadline is not None:
        transaction_deadline = min(transaction_deadline, absolute_deadline)
    request = {
        "event": "render_open",
        "protocol_version": PROTOCOL_VERSION,
        "request_id": request_id,
        "audio_id": first.audio_id,
        "audio_generation": first.audio_generation,
        "session_generation": first.session_generation,
        "format": first.format.to_dict(),
        "frame_duration_samples": first.duration_samples,
        "total_frames": len(frames),
        "total_samples": total_samples,
        "backend_id": args.backend_id,
        "avatar_id": args.avatar_id,
        "avatar_revision": args.avatar_revision,
        "avatar_digest": args.avatar_digest,
        "license_manifest_digest": args.license_manifest_digest,
        "weights_sha256": args.weights_sha256,
        "model_version": args.model_version,
        "text": first.text,
    }
    started = time.monotonic()
    await _send(
        websocket,
        json.dumps(request, ensure_ascii=False, separators=(",", ":")),
        transaction_deadline,
    )
    accepted = _decode_control(await _recv(websocket, deadline=transaction_deadline))
    credit = _validate_accepted(accepted, request_id, descriptor, frames)
    ledger: dict[str, Any] = {
        "frames": 0,
        "bytes": 0,
        "last_pts_samples": None,
        "pts_samples": [],
        "total_samples": total_samples,
    }
    first_frame_at: float | None = None
    message_count = 1
    for frame in frames:
        while credit <= 0:
            if message_count >= MAX_TRANSACTION_MESSAGES:
                raise AcceptanceError("事务消息数超过验收工具上限")
            raw = await _recv(websocket, deadline=transaction_deadline)
            message_count += 1
            if isinstance(raw, bytes):
                _accept_video(raw, request_id, first, ledger)
                first_frame_at = first_frame_at or time.monotonic()
            else:
                credit = _consume_control(_decode_control(raw), request_id, credit)
        await _send(websocket, encode_audio_frame(request_id, frame), transaction_deadline)
        credit -= 1
    await _send(
        websocket,
        json.dumps(
            {
                "event": "render_finish",
                "request_id": request_id,
                "audio_id": first.audio_id,
                "final_sequence": frames[-1].sequence,
                "total_samples": total_samples,
            },
            separators=(",", ":"),
        ),
        transaction_deadline,
    )
    complete: dict[str, Any] | None = None
    while complete is None:
        if message_count >= MAX_TRANSACTION_MESSAGES:
            raise AcceptanceError("事务消息数超过验收工具上限")
        raw = await _recv(websocket, deadline=transaction_deadline)
        message_count += 1
        if isinstance(raw, bytes):
            _accept_video(raw, request_id, first, ledger)
            first_frame_at = first_frame_at or time.monotonic()
            continue
        message = _decode_control(raw)
        if message.get("request_id") != request_id:
            raise AcceptanceError("收到其他事务的 control 消息")
        if message.get("event") == "render_complete":
            complete = message
        elif message.get("event") == "render_credit":
            credit = _consume_control(message, request_id, credit)
        elif message.get("event") == "render_started":
            _validate_render_started(message, request_id)
        elif message.get("event") == "error":
            raise AcceptanceError(str(message.get("message") or message.get("code") or "sidecar error"))
        else:
            raise AcceptanceError(f"完成阶段收到意外事件: {message.get('event')!r}")
    finished = time.monotonic()
    _validate_complete(complete, request_id, descriptor, frames, ledger)
    audio_seconds = total_samples / first.format.sample_rate
    wall_seconds = finished - started
    expected_frames = max(1, math.ceil(audio_seconds * fps))
    expected_interval_samples = first.format.sample_rate / fps
    pts_values = list(ledger["pts_samples"])
    cadence_errors_ms = [
        abs((right - left) - expected_interval_samples) * 1000.0 / first.format.sample_rate
        for left, right in zip(pts_values, pts_values[1:])
    ]
    cadence_p95_ms = _percentile_95(cadence_errors_ms)
    frame_coverage = ledger["frames"] / expected_frames
    timeline_coverage = min(
        1.0,
        (
            float(pts_values[-1] - pts_values[0]) + expected_interval_samples
            if pts_values
            else 0.0
        )
        / total_samples,
    )
    endpoint_gap_ms = (
        abs(total_samples - int(ledger["last_pts_samples"])) * 1000.0 / first.format.sample_rate
    )
    return {
        "request_id": request_id,
        "first_frame_latency_ms": (
            (first_frame_at - started) * 1000.0 if first_frame_at is not None else None
        ),
        "rendered_frames": ledger["frames"],
        "expected_media_frames": expected_frames,
        "rendered_bytes": ledger["bytes"],
        "wall_seconds": wall_seconds,
        "throughput_fps": ledger["frames"] / wall_seconds if wall_seconds > 0 else None,
        "media_fps": ledger["frames"] / audio_seconds,
        "audio_seconds": audio_seconds,
        "real_time_factor": wall_seconds / audio_seconds,
        "frame_coverage": frame_coverage,
        "timeline_coverage": timeline_coverage,
        "pts_cadence_error_p95_ms": cadence_p95_ms,
        "endpoint_pts_gap_ms": endpoint_gap_ms,
        "timeline_contract": "PROTOCOL_PTS_ONLY_PHYSICAL_AV_NOT_MEASURED",
        "pts_monotonic": True,
        "credit_validated": True,
        "message_count": message_count,
        "accepted_resource_stats": _extract_resource_stats(accepted),
        "complete_resource_stats": _extract_resource_stats(complete),
    }


def _percentile_95(values: list[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, math.ceil(len(ordered) * 0.95) - 1)
    return ordered[index]


async def _cancel_transaction(
    websocket: Any,
    args: argparse.Namespace,
    descriptor: Mapping[str, Any],
    frames: tuple[AudioFrame, ...],
) -> dict[str, Any]:
    first = frames[0]
    request_id = uuid.uuid4().hex
    total_samples = sum(frame.duration_samples for frame in frames)
    transaction_deadline = time.monotonic() + args.transaction_timeout_seconds
    await _send(
        websocket,
        json.dumps(
            {
                "event": "render_open",
                "protocol_version": PROTOCOL_VERSION,
                "request_id": request_id,
                "audio_id": first.audio_id,
                "audio_generation": first.audio_generation,
                "session_generation": first.session_generation,
                "format": first.format.to_dict(),
                "frame_duration_samples": first.duration_samples,
                "total_frames": len(frames),
                "total_samples": total_samples,
                "backend_id": args.backend_id,
                "avatar_id": args.avatar_id,
                "avatar_revision": args.avatar_revision,
                "avatar_digest": args.avatar_digest,
                "license_manifest_digest": args.license_manifest_digest,
                "weights_sha256": args.weights_sha256,
                "model_version": args.model_version,
                "text": "acceptance cancellation fixture",
            },
            separators=(",", ":"),
        ),
        transaction_deadline,
    )
    accepted = _decode_control(await _recv(websocket, deadline=transaction_deadline))
    credit = _validate_accepted(accepted, request_id, descriptor, frames)
    await _send(websocket, encode_audio_frame(request_id, first), transaction_deadline)
    credit -= 1

    barrier_started = time.monotonic()
    barrier_deadline = min(
        transaction_deadline,
        barrier_started + args.render_started_timeout_seconds,
    )
    inference_active_observed = False
    barrier_video_frames = 0
    message_count = 1
    while not inference_active_observed:
        if message_count >= MAX_TRANSACTION_MESSAGES:
            raise AcceptanceError("取消 barrier 消息数超过验收工具上限")
        raw = await _recv(
            websocket,
            args.render_started_timeout_seconds,
            barrier_deadline,
        )
        message_count += 1
        if isinstance(raw, bytes):
            frame = decode_video_frame(raw)
            if (
                frame.request_id != request_id
                or frame.audio_id != first.audio_id
                or frame.audio_generation != first.audio_generation
                or frame.session_generation != first.session_generation
            ):
                raise AcceptanceError("render_started barrier 前收到其他事务视频")
            barrier_video_frames += 1
            continue
        message = _decode_control(raw)
        if message.get("request_id") != request_id:
            raise AcceptanceError("render_started barrier 收到其他事务 control")
        event = message.get("event")
        if event == "render_started":
            _validate_render_started(
                message,
                request_id,
                expected_kind="audio",
                expected_sequence=first.sequence,
            )
            inference_active_observed = True
        elif event == "render_credit":
            credit = _consume_control(message, request_id, credit)
        elif event == "error":
            raise AcceptanceError(str(message.get("message") or "render_started barrier error"))
        else:
            raise AcceptanceError(f"render_started barrier 收到意外事件: {event!r}")
    barrier_observed_at = time.monotonic()

    cancel_started = time.monotonic()
    await _send(
        websocket,
        json.dumps(
            {"event": "render_cancel", "request_id": request_id, "reason": "acceptance cancellation"},
            separators=(",", ":"),
        ),
        transaction_deadline,
    )
    acknowledgement: dict[str, Any] | None = None
    timeout_seconds = max(0.001, args.max_cancel_ms / 1000.0)
    cancel_deadline = min(transaction_deadline, cancel_started + timeout_seconds)
    while acknowledgement is None:
        if message_count >= MAX_TRANSACTION_MESSAGES:
            raise AcceptanceError("取消事务消息数超过验收工具上限")
        raw = await _recv(websocket, timeout_seconds, cancel_deadline)
        message_count += 1
        if isinstance(raw, bytes):
            frame = decode_video_frame(raw)
            if frame.request_id != request_id:
                raise AcceptanceError("取消前收到其他事务视频")
            continue
        message = _decode_control(raw)
        if message.get("request_id") != request_id:
            raise AcceptanceError("取消阶段收到其他事务 control")
        if message.get("event") == "render_cancelled":
            if message.get("cancel_ack") is not True or message.get("quiesced") is not True:
                raise AcceptanceError("render_cancelled 必须严格确认 cancel_ack/quiesced")
            acknowledgement = message
        elif message.get("event") == "error":
            raise AcceptanceError(str(message.get("message") or "cancel error"))
        elif message.get("event") != "render_credit":
            raise AcceptanceError(f"取消阶段收到意外事件: {message.get('event')!r}")
    ack_at = time.monotonic()
    stale_video_after_ack = 0
    guard_deadline = min(transaction_deadline, ack_at + POST_CANCEL_GUARD_SECONDS)
    while True:
        remaining = guard_deadline - time.monotonic()
        if remaining <= 0:
            break
        try:
            raw = await asyncio.wait_for(websocket.recv(), timeout=remaining)
        except TimeoutError:
            break
        message_count += 1
        if message_count > MAX_TRANSACTION_MESSAGES:
            raise AcceptanceError("取消事务消息数超过验收工具上限")
        if isinstance(raw, bytes):
            frame = decode_video_frame(raw)
            if frame.request_id == request_id:
                stale_video_after_ack += 1
        else:
            message = _decode_control(raw)
            if message.get("request_id") == request_id and message.get("event") == "error":
                raise AcceptanceError(str(message.get("message") or "post-cancel error"))
    if stale_video_after_ack:
        raise AcceptanceError("render_cancelled ack 后仍收到旧事务视频")
    return {
        "request_id": request_id,
        "cancel_ack_ms": (ack_at - cancel_started) * 1000.0,
        "inference_active_observed": inference_active_observed,
        "render_started_wait_ms": (barrier_observed_at - barrier_started) * 1000.0,
        "barrier_video_frames": barrier_video_frames,
        "quiesced": True,
        "stale_video_after_ack": stale_video_after_ack,
        "guard_seconds": POST_CANCEL_GUARD_SECONDS,
        "message_count": message_count,
    }


async def _post_cancel_lock_probe(
    websocket: Any,
    args: argparse.Namespace,
    descriptor: Mapping[str, Any],
    frame: AudioFrame,
) -> dict[str, Any]:
    """ACK 后立即 open 新 session，证明旧 producer 已释放 model lock。"""
    request_id = uuid.uuid4().hex
    started = time.monotonic()
    accepted_deadline = started + args.render_started_timeout_seconds
    await _send(
        websocket,
        json.dumps(
            {
                "event": "render_open",
                "protocol_version": PROTOCOL_VERSION,
                "request_id": request_id,
                "audio_id": frame.audio_id,
                "audio_generation": frame.audio_generation,
                "session_generation": frame.session_generation,
                "format": frame.format.to_dict(),
                "frame_duration_samples": frame.duration_samples,
                "total_frames": 1,
                "total_samples": frame.duration_samples,
                "backend_id": args.backend_id,
                "avatar_id": args.avatar_id,
                "avatar_revision": args.avatar_revision,
                "avatar_digest": args.avatar_digest,
                "license_manifest_digest": args.license_manifest_digest,
                "weights_sha256": args.weights_sha256,
                "model_version": args.model_version,
                "text": "post-cancel model-lock probe",
            },
            separators=(",", ":"),
        ),
        accepted_deadline,
    )
    accepted = _decode_control(await _recv(websocket, deadline=accepted_deadline))
    _validate_accepted(accepted, request_id, descriptor, (frame,))
    accepted_at = time.monotonic()

    cancel_deadline = accepted_at + max(0.001, args.max_cancel_ms / 1000.0)
    await _send(
        websocket,
        json.dumps(
            {"event": "render_cancel", "request_id": request_id, "reason": "post-cancel probe cleanup"},
            separators=(",", ":"),
        ),
        cancel_deadline,
    )
    cancelled = _decode_control(await _recv(websocket, deadline=cancel_deadline))
    if (
        cancelled.get("event") != "render_cancelled"
        or cancelled.get("request_id") != request_id
        or cancelled.get("cancel_ack") is not True
        or cancelled.get("quiesced") is not True
    ):
        raise AcceptanceError("post-cancel probe 未收到严格 quiesced cancel ACK")
    return {
        "request_id": request_id,
        "model_lock_reacquired": True,
        "render_accepted_ms": (accepted_at - started) * 1000.0,
        "cleanup_cancel_ms": (time.monotonic() - accepted_at) * 1000.0,
    }


def _slope_mib_per_minute(samples: list[ResourceSample], field: str) -> float | None:
    points = [
        (sample.elapsed_seconds / 60.0, float(sample.stats[field]) / MIB)
        for sample in samples
        if isinstance(sample.stats.get(field), int) and not isinstance(sample.stats.get(field), bool)
    ]
    if len(points) < 2:
        return None
    x_mean = sum(point[0] for point in points) / len(points)
    y_mean = sum(point[1] for point in points) / len(points)
    denominator = sum((point[0] - x_mean) ** 2 for point in points)
    if denominator <= 0:
        return 0.0
    return sum((x - x_mean) * (y - y_mean) for x, y in points) / denominator


def _resource_summary(samples: list[ResourceSample]) -> dict[str, Any]:
    summary: dict[str, Any] = {}
    for field in ("process_rss_bytes", "memory_allocated_bytes", "memory_reserved_bytes"):
        values = [
            sample.stats[field]
            for sample in samples
            if isinstance(sample.stats.get(field), int) and not isinstance(sample.stats.get(field), bool)
        ]
        summary[field] = {
            "start": values[0] if values else None,
            "end": values[-1] if values else None,
            "peak": max(values) if values else None,
            "peak_minus_baseline_mib": (
                (max(values) - values[0]) / MIB if values else None
            ),
            "slope_mib_per_minute": _slope_mib_per_minute(samples, field),
        }
    peak_allocated = [sample.stats.get("max_memory_allocated_bytes") for sample in samples]
    peak_reserved = [sample.stats.get("max_memory_reserved_bytes") for sample in samples]
    summary["torch_peak_memory_allocated_bytes"] = max(
        (value for value in peak_allocated if isinstance(value, int) and not isinstance(value, bool)),
        default=None,
    )
    summary["torch_peak_memory_reserved_bytes"] = max(
        (value for value in peak_reserved if isinstance(value, int) and not isinstance(value, bool)),
        default=None,
    )
    return summary


async def _stability_loop(
    websocket: Any,
    args: argparse.Namespace,
    descriptor: Mapping[str, Any],
    pcm: bytes,
    audio_format: AudioFormat,
    fps: float,
    starting_generation: int,
) -> dict[str, Any]:
    duration_seconds = args.stability_minutes * 60.0
    started = time.monotonic()
    deadline = started + duration_seconds
    transaction_count = 0
    errors: list[str] = []
    oom_count = 0
    fallback_count = 0
    samples: list[ResourceSample] = []
    generation = starting_generation
    deadline_reached_during_transaction = False
    while time.monotonic() < deadline:
        generation += 1
        frames = _frame_audio(pcm, audio_format, fps, generation)
        try:
            transaction = await _render_transaction(
                websocket,
                args,
                descriptor,
                frames,
                fps,
                absolute_deadline=deadline,
            )
        except Exception as exc:
            if time.monotonic() >= deadline:
                deadline_reached_during_transaction = True
                try:
                    await asyncio.wait_for(websocket.close(), timeout=CONNECT_TIMEOUT_SECONDS)
                except Exception:
                    pass
                break
            text = str(exc)
            errors.append(text)
            lower = text.lower()
            oom_count += int("oom" in lower or "out of memory" in lower)
            fallback_count += int("fallback" in lower)
            break
        transaction_count += 1
        for field in ("accepted_resource_stats", "complete_resource_stats"):
            stats = transaction.get(field)
            if isinstance(stats, dict):
                samples.append(ResourceSample(time.monotonic() - started, stats))
    elapsed = time.monotonic() - started
    resources = _resource_summary(samples)
    tolerance_seconds = min(1.0, duration_seconds * 0.01)
    return {
        "requested_minutes": args.stability_minutes,
        "elapsed_seconds": elapsed,
        "duration_tolerance_seconds": tolerance_seconds,
        "duration_complete": elapsed >= duration_seconds - tolerance_seconds,
        "deadline_reached_during_transaction": deadline_reached_during_transaction,
        "transaction_count": transaction_count,
        "resource_stats": resources,
        "error_count": len(errors),
        "errors": errors,
        "oom_count": oom_count,
        "fallback_count": fallback_count,
        "telemetry_complete": bool(samples) and all(_resource_complete(sample.stats) for sample in samples),
    }


def _validate_transport_security(args: argparse.Namespace) -> None:
    parsed = urlparse(args.url)
    hostname = (parsed.hostname or "").lower()
    loopback = hostname in {"localhost", "127.0.0.1", "::1"}
    if parsed.scheme not in {"ws", "wss"} or not hostname:
        raise AcceptanceError("sidecar URL 必须是合法 ws:// 或 wss:// 地址")
    if parsed.scheme == "ws" and not loopback:
        raise AcceptanceError("非本机 sidecar 必须使用 wss://，禁止明文传输媒体和凭据")
    if not loopback and not args.token:
        raise AcceptanceError("远程 sidecar 必须配置鉴权 token")
    if args.tls_ca and parsed.scheme != "wss":
        raise AcceptanceError("--tls-ca 仅适用于 wss:// URL")


async def _connect_and_auth(args: argparse.Namespace) -> tuple[Any, dict[str, Any], dict[str, Any]]:
    _validate_transport_security(args)
    try:
        import websockets
    except ImportError as exc:
        raise BlockedError("缺少 websockets 依赖；工具不会自动安装") from exc
    connect_options: dict[str, Any] = {"max_size": 16 * 1024 * 1024}
    if args.tls_ca:
        connect_options["ssl"] = ssl.create_default_context(cafile=args.tls_ca)
    try:
        websocket = await asyncio.wait_for(
            websockets.connect(args.url, **connect_options),
            timeout=CONNECT_TIMEOUT_SECONDS,
        )
    except Exception as exc:
        raise BlockedError(f"无法连接 sidecar: {exc}") from exc
    auth_deadline = time.monotonic() + CONNECT_TIMEOUT_SECONDS
    try:
        await _send(
            websocket,
            json.dumps(build_auth_message(args.token), separators=(",", ":")),
            auth_deadline,
        )
        raw = await _recv(websocket, CONNECT_TIMEOUT_SECONDS, auth_deadline)
        negotiated = validate_auth_reply(_decode_control(raw))
        capabilities = dict(negotiated["capabilities"])
        descriptor = _descriptor_from_capabilities(capabilities, args)
        return websocket, capabilities, descriptor
    except BlockedError:
        await websocket.close()
        raise
    except Exception as exc:
        await websocket.close()
        if isinstance(exc, AcceptanceError):
            raise
        raise AcceptanceError(f"auth/capabilities 验证失败: {exc}") from exc


async def _run(args: argparse.Namespace, report: dict[str, Any]) -> int:
    websocket: Any = None
    try:
        websocket, capabilities, descriptor = await _connect_and_auth(args)
        report["descriptor"] = descriptor
        report["criteria"]["authentication_descriptor"] = _criterion(
            "PASS", "v3 auth and exact neural descriptor validated"
        )

        formats = _capability_formats(capabilities)
        fps_value = capabilities.get("fps")
        if isinstance(fps_value, bool) or not isinstance(fps_value, (int, float)) or fps_value <= 0:
            raise AcceptanceError("capabilities.fps 必须是正数")
        fps = float(fps_value)
        if args.audio_wav:
            pcm, audio_format = _load_wav(args.audio_wav)
            report["audio_fixture"] = "pcm16_wav"
            if audio_format not in formats:
                raise AcceptanceError("WAV 格式不在 sidecar input_formats 中")
        else:
            audio_format = formats[0]
            pcm = _synthetic_pcm(audio_format)
            report["audio_fixture"] = "synthetic_transport_only"
        frames = _frame_audio(pcm, audio_format, fps, 1)

        performance = await _render_transaction(websocket, args, descriptor, frames, fps)
        report["performance"] = performance
        cadence_valid = (
            performance["expected_media_frames"] <= 1
            or (
                performance["pts_cadence_error_p95_ms"] is not None
                and performance["pts_cadence_error_p95_ms"] <= args.max_pts_cadence_error_ms
            )
        )
        performance_pass = bool(
            performance["first_frame_latency_ms"] is not None
            and performance["first_frame_latency_ms"] <= args.max_first_frame_ms
            and performance["media_fps"] >= args.min_fps
            and performance["throughput_fps"] is not None
            and performance["throughput_fps"] >= args.min_fps
            and performance["real_time_factor"] <= args.max_real_time_factor
            and performance["frame_coverage"] >= args.min_frame_coverage
            and performance["timeline_coverage"] >= args.min_frame_coverage
            and cadence_valid
            and performance["endpoint_pts_gap_ms"] <= args.max_endpoint_pts_gap_ms
            and performance["pts_monotonic"] is True
            and performance["credit_validated"] is True
        )
        report["criteria"]["performance_transaction"] = _criterion(
            "PASS" if performance_pass else "FAIL",
            "protocol timeline cadence/coverage, media FPS, throughput, credit and JPEG identity validated; physical A/V not measured"
            if performance_pass
            else "one or more protocol timeline/media cadence thresholds failed; physical A/V not measured",
            min_media_fps=args.min_fps,
            min_throughput_fps=args.min_fps,
            max_real_time_factor=args.max_real_time_factor,
            min_frame_coverage=args.min_frame_coverage,
            max_first_frame_ms=args.max_first_frame_ms,
            max_endpoint_pts_gap_ms=args.max_endpoint_pts_gap_ms,
            max_pts_cadence_error_ms=args.max_pts_cadence_error_ms,
        )

        cancel_frames = _frame_audio(pcm, audio_format, fps, 2)
        cancellation = await _cancel_transaction(websocket, args, descriptor, cancel_frames)
        probe_frame = _frame_audio(pcm, audio_format, fps, 3)[0]
        post_cancel_probe = await _post_cancel_lock_probe(websocket, args, descriptor, probe_frame)
        cancellation["post_cancel_probe"] = post_cancel_probe
        report["cancellation"] = cancellation
        cancellation_pass = bool(
            cancellation["inference_active_observed"] is True
            and cancellation["quiesced"] is True
            and cancellation["cancel_ack_ms"] <= args.max_cancel_ms
            and post_cancel_probe["model_lock_reacquired"] is True
            and post_cancel_probe["render_accepted_ms"]
            <= args.render_started_timeout_seconds * 1000.0
        )
        report["criteria"]["cancellation_transaction"] = _criterion(
            "PASS" if cancellation_pass else "FAIL",
            "inference-active barrier, bounded quiesced cancel ack, no post-ack stale video and immediate model-lock probe"
            if cancellation_pass
            else "cancel barrier/ack failed or a new session could not promptly reacquire the model lock",
            max_cancel_ms=args.max_cancel_ms,
            max_post_cancel_probe_ms=args.render_started_timeout_seconds * 1000.0,
        )

        perf_stats = [
            performance.get("accepted_resource_stats"),
            performance.get("complete_resource_stats"),
        ]
        telemetry_complete = all(_resource_complete(stats) for stats in perf_stats)
        if args.stability_minutes == 0:
            report["stability"] = {
                "requested_minutes": 0.0,
                "transaction_count": 0,
                "resource_stats": None,
                "error_count": 0,
                "oom_count": 0,
                "fallback_count": 0,
            }
            report["criteria"]["long_stability"] = _criterion(
                "SKIPPED_NOT_RUN",
                "--stability-minutes 0 explicitly disables the required long stability run",
            )
        else:
            stability = await _stability_loop(websocket, args, descriptor, pcm, audio_format, fps, 2)
            report["stability"] = stability
            telemetry_complete = telemetry_complete and stability["telemetry_complete"]
            resources = stability["resource_stats"]
            allocated = resources["memory_allocated_bytes"]
            reserved = resources["memory_reserved_bytes"]
            rss_slope = resources["process_rss_bytes"]["slope_mib_per_minute"]
            rss_within_threshold = rss_slope is None or rss_slope <= args.max_rss_slope_mib_min
            cuda_telemetry_complete = stability["telemetry_complete"]
            stability_pass = bool(
                stability["duration_complete"] is True
                and stability["transaction_count"] > 0
                and stability["error_count"] == 0
                and stability["oom_count"] == 0
                and stability["fallback_count"] == 0
                and allocated["slope_mib_per_minute"] is not None
                and allocated["slope_mib_per_minute"] <= args.max_vram_slope_mib_min
                and reserved["slope_mib_per_minute"] is not None
                and reserved["slope_mib_per_minute"] <= args.max_reserved_vram_slope_mib_min
                and allocated["peak_minus_baseline_mib"] is not None
                and allocated["peak_minus_baseline_mib"] <= args.max_vram_allocated_growth_mib
                and reserved["peak_minus_baseline_mib"] is not None
                and reserved["peak_minus_baseline_mib"] <= args.max_vram_reserved_growth_mib
                and rss_within_threshold
            )
            long_status = "BLOCKED" if not cuda_telemetry_complete else "PASS" if stability_pass else "FAIL"
            report["criteria"]["long_stability"] = _criterion(
                long_status,
                "absolute duration, errors/OOM/fallback, allocated/reserved slopes and peak growth validated"
                if long_status == "PASS"
                else "required CUDA telemetry missing"
                if long_status == "BLOCKED"
                else "duration, counters, allocated/reserved slope, or peak-growth threshold failed",
                max_vram_slope_mib_min=args.max_vram_slope_mib_min,
                max_reserved_vram_slope_mib_min=args.max_reserved_vram_slope_mib_min,
                max_vram_allocated_growth_mib=args.max_vram_allocated_growth_mib,
                max_vram_reserved_growth_mib=args.max_vram_reserved_growth_mib,
                max_rss_slope_mib_min=args.max_rss_slope_mib_min,
                rss_status="PASS" if rss_slope is not None else "UNKNOWN",
            )

        rss_available = all(
            isinstance(stats, dict)
            and isinstance(stats.get("process_rss_bytes"), int)
            and not isinstance(stats.get("process_rss_bytes"), bool)
            for stats in perf_stats
        )
        report["criteria"]["resource_telemetry"] = _criterion(
            "PASS" if telemetry_complete else "BLOCKED",
            "torch CUDA counters are numeric; optional RSS is "
            + ("available" if rss_available else "unknown")
            if telemetry_complete
            else "required torch CUDA resource counters are unknown or unavailable",
            rss_status="PASS" if rss_available else "UNKNOWN",
        )
        statuses = [item["status"] for item in report["criteria"].values()]
        if all(status == "PASS" for status in statuses):
            report["status"] = "PASS"
            return 0
        if "FAIL" in statuses:
            report["status"] = "FAIL"
            return 1
        if "BLOCKED" in statuses:
            report["status"] = "SKIPPED_BLOCKED"
            return 2
        report["status"] = "FAIL"
        return 1
    except BlockedError as exc:
        logger.error("验收被阻塞: %s", exc)
        report["errors"].append(str(exc))
        report["status"] = "SKIPPED_BLOCKED"
        for name, criterion in report["criteria"].items():
            if criterion["status"] == "FAIL" and criterion["detail"] == "not run":
                report["criteria"][name] = _criterion("BLOCKED", str(exc))
        return 2
    except Exception as exc:
        logger.error("验收失败: %s", exc)
        report["errors"].append(str(exc))
        report["status"] = "FAIL"
        return 1
    finally:
        if websocket is not None:
            try:
                await asyncio.wait_for(websocket.close(), timeout=CONNECT_TIMEOUT_SECONDS)
            except Exception:
                pass


def _write_report(path: str, report: Mapping[str, Any]) -> None:
    report_path = Path(path).expanduser().resolve()
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", stream=sys.stderr)
    args = _parse_args(argv)
    report = _base_report(args)
    try:
        exit_code = asyncio.run(_run(args, report))
    except KeyboardInterrupt:
        report["status"] = "FAIL"
        report["errors"].append("interrupted")
        exit_code = 130
    if args.report:
        try:
            _write_report(args.report, report)
        except Exception as exc:
            logger.error("写入 --report 失败: %s", exc)
            report["status"] = "FAIL"
            report["errors"].append(f"report write failed: {exc}")
            exit_code = 1
    print(json.dumps(report, ensure_ascii=True, separators=(",", ":")))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
