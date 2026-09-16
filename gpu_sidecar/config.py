"""GPU sidecar 的无模型假设运行配置。"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from server.core.media.audio_frame import AudioFormat


@dataclass(frozen=True, slots=True)
class SidecarConfig:
    backend_id: str
    model_version: str
    fps: float
    device: str
    initial_credit: int
    worker_queue_size: int
    sender_queue_size: int
    max_transaction_frames: int
    max_transaction_bytes: int
    max_transaction_seconds: int
    max_connections: int
    max_active_renders: int
    auth_timeout_seconds: float
    idle_timeout_seconds: float
    model_call_timeout_seconds: float
    cancel_timeout_seconds: float
    bridge_queue_size: int
    input_formats: tuple[AudioFormat, ...]
    plugin_config: Mapping[str, Any]

    @classmethod
    def load(cls, path: str | Path) -> SidecarConfig:
        config_path = Path(path).expanduser().resolve()
        if not config_path.is_file() or config_path.is_symlink():
            raise ValueError(f"config 必须是存在的普通 JSON 文件: {config_path}")
        try:
            data = json.loads(config_path.read_text(encoding="utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError(f"config 不是合法 UTF-8 JSON: {config_path}") from exc
        if not isinstance(data, dict):
            raise ValueError("config 顶层必须是 object")

        formats_data = data.get("input_formats")
        if not isinstance(formats_data, list) or not formats_data:
            raise ValueError("input_formats 必须是非空数组")
        formats: list[AudioFormat] = []
        for index, item in enumerate(formats_data):
            if not isinstance(item, dict):
                raise ValueError(f"input_formats[{index}] 必须是 object")
            formats.append(
                AudioFormat(
                    codec=item.get("codec", "pcm_s16le"),
                    sample_rate=_positive_int(item.get("sample_rate"), f"input_formats[{index}].sample_rate"),
                    channels=_positive_int(item.get("channels"), f"input_formats[{index}].channels"),
                    sample_width_bytes=_positive_int(
                        item.get("sample_width_bytes", 2),
                        f"input_formats[{index}].sample_width_bytes",
                    ),
                )
            )
        plugin_config = data.get("plugin_config", {})
        if not isinstance(plugin_config, dict):
            raise ValueError("plugin_config 必须是 object")
        initial_credit = _bounded_int(data.get("initial_credit", 8), "initial_credit", 1, 256)
        worker_queue_size = _bounded_int(data.get("worker_queue_size", initial_credit), "worker_queue_size", 1, 256)
        sender_queue_size = _bounded_int(data.get("sender_queue_size", 32), "sender_queue_size", 1, 256)
        if initial_credit > worker_queue_size:
            raise ValueError("initial_credit 不能大于 worker_queue_size")
        fps = data.get("fps")
        if isinstance(fps, bool) or not isinstance(fps, (int, float)) or fps <= 0:
            raise ValueError("fps 必须是正数")
        device = _text(data.get("device", "cuda"), "device")
        if not device.lower().startswith("cuda"):
            raise ValueError("device 必须是 CUDA device")
        return cls(
            backend_id=_text(data.get("backend_id"), "backend_id"),
            model_version=_text(data.get("model_version"), "model_version"),
            fps=float(fps),
            device=device,
            initial_credit=initial_credit,
            worker_queue_size=worker_queue_size,
            sender_queue_size=sender_queue_size,
            max_transaction_frames=_bounded_int(
                data.get("max_transaction_frames", 3000),
                "max_transaction_frames",
                1,
                100_000,
            ),
            max_transaction_bytes=_bounded_int(
                data.get("max_transaction_bytes", 64 * 1024 * 1024),
                "max_transaction_bytes",
                1,
                1024 * 1024 * 1024,
            ),
            max_transaction_seconds=_bounded_int(
                data.get("max_transaction_seconds", 30),
                "max_transaction_seconds",
                1,
                3600,
            ),
            max_connections=_bounded_int(data.get("max_connections", 32), "max_connections", 1, 4096),
            max_active_renders=_bounded_int(
                data.get("max_active_renders", 1),
                "max_active_renders",
                1,
                64,
            ),
            auth_timeout_seconds=_bounded_number(
                data.get("auth_timeout_seconds", 5.0),
                "auth_timeout_seconds",
                0.1,
                300.0,
            ),
            idle_timeout_seconds=_bounded_number(
                data.get("idle_timeout_seconds", 60.0),
                "idle_timeout_seconds",
                1.0,
                3600.0,
            ),
            model_call_timeout_seconds=_bounded_number(
                data.get("model_call_timeout_seconds", 30.0),
                "model_call_timeout_seconds",
                0.1,
                3600.0,
            ),
            cancel_timeout_seconds=_bounded_number(
                data.get("cancel_timeout_seconds", 2.0),
                "cancel_timeout_seconds",
                0.1,
                60.0,
            ),
            bridge_queue_size=_bounded_int(
                data.get("bridge_queue_size", 8),
                "bridge_queue_size",
                1,
                256,
            ),
            input_formats=tuple(formats),
            plugin_config=plugin_config,
        )

    def supports_format(self, audio_format: AudioFormat) -> bool:
        return audio_format in self.input_formats


def _text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} 必须是非空字符串")
    return value.strip()


def _positive_int(value: Any, field: str) -> int:
    return _bounded_int(value, field, 1, 2**31 - 1)


def _bounded_int(value: Any, field: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ValueError(f"{field} 必须位于 {minimum}~{maximum}")
    return value


def _bounded_number(value: Any, field: str, minimum: float, maximum: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field} 必须是数字")
    number = float(value)
    if not minimum <= number <= maximum:
        raise ValueError(f"{field} 必须位于 {minimum}~{maximum}")
    return number
