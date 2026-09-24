"""独立神经渲染 sidecar v3 客户端。

该驱动只消费标准 PCM AudioFrame 并接收带 sample PTS 的视频帧，不承担 TTS。
sidecar 不可用时由 MediaRouter 保持本地程序化 shadow renderer，不阻塞直播控制平面。
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from typing import Optional
from urllib.parse import urlparse

from server.adapters.media.avatar_provider import ProviderError, ProviderErrorCode
from server.adapters.media.base_driver import BaseMediaDriver
from server.core.media.av_sync import global_av_sync
from server.core.media.audio_frame import validate_audio_frame_batch
from server.core.media.shared_playback_clock import global_shared_playback_clock
from server.core.media.sidecar_protocol import (
    PROTOCOL_VERSION,
    SidecarVideoFrame,
    build_auth_message,
    decode_video_frame,
    encode_audio_frame,
    validate_auth_reply,
)

try:
    import websockets
except ImportError:  # pragma: no cover - 可选远程媒体依赖
    websockets = None

try:
    import paramiko
    PARAMIKO_AVAILABLE = True
except ImportError:
    PARAMIKO_AVAILABLE = False

logger = logging.getLogger("LiveAgent.NeuralSidecar")
_TIMELINE_END = object()


class NeuralSidecarMediaDriver(BaseMediaDriver):
    """版本化 renderer-only sidecar；失败时可由路由透明保留程序化画面。"""

    CONNECT_TIMEOUT = 1.0
    MESSAGE_TIMEOUT = 20.0
    REQUEST_TIMEOUT = 120.0
    RETRY_INTERVAL = 5.0
    TIMELINE_GRACE_SECONDS = 5.0
    MAX_VIDEO_FRAMES = 3000
    MAX_VIDEO_BYTES = 128 * 1024 * 1024
    MAX_PENDING_TIMELINES = 2
    MAX_PENDING_VIDEO_BYTES = 256 * 1024 * 1024
    MAX_INITIAL_CREDIT = 256
    EVIDENCE_FIELDS = (
        "model_version",
        "weights_sha256",
        "license_manifest_sha256",
        "license_approved",
        "avatar_id",
        "avatar_revision",
        "avatar_digest",
    )

    def __init__(
        self,
        node_url: str = "ws://127.0.0.1:8890/ws/render-v3",
        auth_token: str = "",
        backend_id: str = "auto",
        avatar_id: str = "default",
        avatar_revision: str = "",
        avatar_digest: str = "",
        license_manifest_digest: str = "",
        weights_sha256: str = "",
        model_version: str = "",
        require_neural_lipsync: bool = True,
        connect_timeout: float | None = None,
        message_timeout: float | None = None,
        request_timeout: float | None = None,
        ssh_config: dict | None = None,
    ) -> None:
        super().__init__()
        self.node_url = node_url
        self.auth_token = auth_token
        # SSH 隧道配置
        self.ssh_config = ssh_config or {}
        self.ssh_enabled = bool(self.ssh_config.get("enabled"))
        self.ssh_host = self.ssh_config.get("host")
        self.ssh_port = self.ssh_config.get("port", 22)
        self.ssh_username = self.ssh_config.get("username", "root")
        self.ssh_password = self.ssh_config.get("password")
        self.ssh_private_key = self.ssh_config.get("private_key")
        self.ssh_public_key_host = self.ssh_config.get("public_key_host", True)
        self._ssh_client = None
        self._local_tunnel_port = self.ssh_config.get("local_port", 0)  # 0 表示随机

        self.backend_id = backend_id or "auto"
        self.avatar_id = avatar_id or "default"
        self.avatar_revision = avatar_revision or ""
        self.avatar_digest = avatar_digest or ""
        self.license_manifest_digest = license_manifest_digest or ""
        self.weights_sha256 = weights_sha256 or ""
        self.model_version = model_version or ""
        self.require_neural_lipsync = require_neural_lipsync is not False
        for field, configured, default in (
            ("connect_timeout", connect_timeout, self.CONNECT_TIMEOUT),
            ("message_timeout", message_timeout, self.MESSAGE_TIMEOUT),
            ("request_timeout", request_timeout, self.REQUEST_TIMEOUT),
        ):
            value = float(default if configured is None else configured)
            if value <= 0:
                raise ValueError(f"{field} 必须大于 0")
            setattr(self, field.upper(), value)
        if self.CONNECT_TIMEOUT > self.REQUEST_TIMEOUT:
            raise ValueError("connect_timeout 不能大于 request_timeout")
        if self.MESSAGE_TIMEOUT > self.REQUEST_TIMEOUT:
            raise ValueError("message_timeout 不能大于 request_timeout")
        self.is_running = False
        self.is_connected = False
        self.is_ready = False
        self.is_degraded = False
        self.latest_jpeg = b""
        self.frames_received = 0
        self.frames_dropped = 0
        # 发布帧携带的采样时钟换算 PTS (由时间线循环在 report_audio_head 后写入)
        self._pending_video_pts_ms: Optional[float] = None
        self.transactions_completed = 0
        self.transactions_failed = 0
        self.last_error = ""
        self.last_cancel_acknowledged: bool | None = None
        self._ws = None
        self._lock = asyncio.Lock()
        self._current_request_id: Optional[str] = None
        self._current_audio_id: Optional[str] = None
        self._request_owner_task: Optional[asyncio.Task] = None
        self._selected_version: Optional[int] = None
        self._node_version = "unknown"
        self._capabilities: dict = {}
        self._selected_descriptor: dict = {}
        self._strict_completion = False
        self._streaming_video = False
        self._next_retry_at = 0.0
        self._timeline_task: Optional[asyncio.Task] = None
        self._timeline_tasks: set[asyncio.Task] = set()
        self._timeline_consumers: dict[asyncio.Queue, asyncio.Task] = {}
        self._latest_frame_owner: Optional[asyncio.Task] = None
        self._pending_timeline_bytes = 0
        self._timeline_lock = asyncio.Lock()

    async def start(self) -> None:
        self.is_running = True
        await self._ensure_connection()

    async def stop(self) -> None:
        self.is_running = False
        self.is_speaking = False
        if self._request_owner_task is not None and not self._request_owner_task.done():
            try:
                await self.interrupt("Driver stop")
            except ProviderError:
                logger.warning("sidecar 停止时未确认远端静止，已关闭 connection epoch")
        await self._cancel_timelines(clear_frame=True)
        await self._close_connection()

    async def feed_audio_chunk(self, audio_bytes: bytes, text_snippet: str) -> None:
        raise RuntimeError("神经 sidecar 只接受标准 PCM AudioFrame，不接受容器音频")

    async def _close_connection(self) -> None:
        ws = self._ws
        self._ws = None
        self.is_connected = False
        self.is_ready = False
        self._selected_version = None
        self._selected_descriptor = {}
        self._strict_completion = False
        self._streaming_video = False
        self._current_request_id = None
        self._current_audio_id = None
        if ws is not None:
            try:
                await asyncio.wait_for(ws.close(), timeout=self.CONNECT_TIMEOUT)
            except Exception:
                pass
        # 关闭 SSH 隧道
        if self._ssh_client:
            try:
                self._ssh_client.close()
            except Exception:
                pass
            self._ssh_client = None
            self._local_tunnel_port = 0

    async def _send_raw(self, payload, deadline: float) -> None:
        """将所有发送纳入同一个绝对事务截止时间。"""
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ProviderError(
                ProviderErrorCode.REQUEST_TIMEOUT,
                "sidecar 整句请求截止时间已到",
                retryable=True,
            )
        timeout = min(self.MESSAGE_TIMEOUT, remaining)
        try:
            await asyncio.wait_for(self._ws.send(payload), timeout=timeout)
        except TimeoutError as exc:
            code = (
                ProviderErrorCode.REQUEST_TIMEOUT
                if remaining <= self.MESSAGE_TIMEOUT
                else ProviderErrorCode.MESSAGE_TIMEOUT
            )
            raise ProviderError(code, "sidecar 发送超时", retryable=True) from exc

    @staticmethod
    def _non_empty_string(value) -> bool:
        return isinstance(value, str) and bool(value.strip())

    def _neural_descriptor_error(self, descriptor: dict) -> str:
        for field in ("available", "neural", "warmed", "license_approved"):
            if descriptor.get(field) is not True:
                return f"backend descriptor {field} 必须严格为 true"
        for field in (
            "id",
            "model_version",
            "weights_sha256",
            "license_manifest_sha256",
            "avatar_id",
            "avatar_revision",
            "avatar_digest",
        ):
            if not self._non_empty_string(descriptor.get(field)):
                return f"backend descriptor 缺少 {field}"
        if descriptor["avatar_id"] != self.avatar_id:
            return "backend descriptor avatar_id 与配置不匹配"
        expected = {
            "model_version": self.model_version,
            "weights_sha256": self.weights_sha256,
            "license_manifest_sha256": self.license_manifest_digest,
            "avatar_revision": self.avatar_revision,
            "avatar_digest": self.avatar_digest,
        }
        for field, value in expected.items():
            if value and descriptor.get(field) != value:
                return f"backend descriptor {field} 与配置不匹配"
        return ""

    def _select_renderer_descriptor(self, capabilities: dict) -> dict:
        if capabilities.get("renderer_available") is not True:
            raise RuntimeError("sidecar renderer_available 必须严格为 true")
        backends = capabilities.get("render_backends")
        if not isinstance(backends, list) or not backends:
            raise RuntimeError("sidecar 缺少 render_backends descriptor")
        candidates = [
            item
            for item in backends
            if isinstance(item, dict)
            and self._non_empty_string(item.get("id"))
            and (self.backend_id == "auto" or item.get("id") == self.backend_id)
        ]
        if not candidates:
            raise RuntimeError("sidecar 未返回请求的具体 backend descriptor")

        if self.require_neural_lipsync:
            if capabilities.get("neural_lipsync") is not True:
                raise RuntimeError("sidecar neural_lipsync 必须严格为 true")
            if capabilities.get("strict_completion") is not True:
                raise RuntimeError("神经 sidecar 必须启用 strict_completion")
            if capabilities.get("streaming_video") is not True:
                raise RuntimeError("神经 sidecar 必须启用 streaming_video")
            if capabilities.get("supports_cancel_ack") is not True:
                raise RuntimeError("神经 sidecar 必须启用 supports_cancel_ack")
            if capabilities.get("supports_credit") is not True:
                raise RuntimeError("神经 sidecar 必须启用 supports_credit")
            if capabilities.get("supports_render_started") is not True:
                raise RuntimeError("神经 sidecar 必须启用 supports_render_started")
            if capabilities.get("supports_sample_pts") is not True:
                raise RuntimeError("神经 sidecar 必须启用 supports_sample_pts")
            input_formats = capabilities.get("input_formats")
            if not isinstance(input_formats, list) or not any(
                isinstance(item, dict) and item.get("codec") == "pcm_s16le"
                for item in input_formats
            ):
                raise RuntimeError("神经 sidecar 必须声明 PCM16 input_formats")
            if capabilities.get("cancel_threadsafe") is not True or capabilities.get("cancel_quiesces") is not True:
                raise RuntimeError("神经 sidecar 必须声明线程安全且静止语义 cancel")
            errors = []
            for descriptor in candidates:
                error = self._neural_descriptor_error(descriptor)
                if not error:
                    return dict(descriptor)
                errors.append(error)
            raise RuntimeError(errors[0] if errors else "sidecar 没有合格的 neural backend")

        # 仅显式关闭神经要求时允许旧 v3 procedural fixture；它必须诚实声明非 neural。
        if capabilities.get("neural_lipsync") is not False:
            raise RuntimeError("procedural sidecar 必须严格声明 neural_lipsync=false")
        for descriptor in candidates:
            if descriptor.get("available") is True and descriptor.get("neural") is not True:
                return dict(descriptor)
        raise RuntimeError("sidecar 无可用的 procedural backend")

    async def _ensure_connection(self) -> bool:
        if not self.is_running or websockets is None:
            self.is_degraded = True
            self.last_error = "websockets 库不可用" if websockets is None else "driver 未启动"
            return False
        if self._ws is not None and self.is_connected:
            return self.is_ready
        if time.monotonic() < self._next_retry_at:
            return False
        try:
            # 处理 SSH 隧道
            effective_node_url = await self._establish_ssh_tunnel() if self.ssh_enabled else self.node_url

            self._validate_transport_security(effective_node_url)

            handshake_deadline = time.monotonic() + self.CONNECT_TIMEOUT
            self._ws = await asyncio.wait_for(
                websockets.connect(effective_node_url, max_size=16 * 1024 * 1024),
                timeout=max(0.001, handshake_deadline - time.monotonic()),
            )
            await self._send_raw(
                json.dumps(build_auth_message(self.auth_token)), handshake_deadline
            )
            raw = await asyncio.wait_for(
                self._ws.recv(),
                timeout=max(0.001, handshake_deadline - time.monotonic()),
            )
            if isinstance(raw, bytes):
                raise RuntimeError("sidecar 握手返回了非法二进制消息")
            negotiated = validate_auth_reply(json.loads(raw))
            capabilities = dict(negotiated["capabilities"])
            descriptor = self._select_renderer_descriptor(capabilities)
            self._selected_version = negotiated["selected_version"]
            self._node_version = negotiated["node_version"]
            self._capabilities = capabilities
            self._selected_descriptor = descriptor
            self._strict_completion = capabilities.get("strict_completion") is True
            self._streaming_video = capabilities.get("streaming_video") is True
            self.is_connected = True
            self.is_ready = True
            self.is_degraded = False
            self.last_error = ""
            return True
        except asyncio.CancelledError:
            await self._close_connection()
            raise
        except Exception as exc:
            self.last_error = str(exc)
            self.is_degraded = True
            self._next_retry_at = time.monotonic() + self.RETRY_INTERVAL
            logger.warning("神经渲染 sidecar 不可用，保留程序化降级: %s", exc)
            await self._close_connection()
            return False

    async def _establish_ssh_tunnel(self) -> str:
        """建立 SSH 隧道并返回本地转发地址。"""
        if not PARAMIKO_AVAILABLE:
            raise RuntimeError("SSH 隧道需要 paramiko 库")
        if self._ssh_client:
            # 已有关联，直接返回本地地址
            if self._local_tunnel_port == 0:
                return f"ws://127.0.0.1:{self._local_tunnel_port}/ws/render-v3"
            return f"ws://127.0.0.1:{self._local_tunnel_port}/ws/render-v3"

        try:
            self._ssh_client = paramiko.SSHClient()
            self._ssh_client.set_missing_host_key_policy(
                paramiko.AutoAddPolicy() if self.ssh_public_key_host else paramiko.RejectPolicy()
            )

            # 认证 - 优先尝试私钥，失败则用密码
            key = None
            if self.ssh_private_key:
                import io
                try:
                    from paramiko import RSAKey
                    key = RSAKey.from_private_key(io.StringIO(self.ssh_private_key))
                except Exception:
                    try:
                        from paramiko import ECDSAKey
                        key = ECDSAKey.from_private_key(io.StringIO(self.ssh_private_key))
                    except Exception:
                        try:
                            from paramiko import Ed25519Key
                            key = Ed25519Key.from_private_key(io.StringIO(self.ssh_private_key))
                        except Exception:
                            raise RuntimeError("SSH 私钥格式无效或无法解密")

            self._ssh_client.connect(
                hostname=self.ssh_host,
                port=self.ssh_port,
                username=self.ssh_username,
                password=self.ssh_password,
                pkey=key,
                timeout=self.CONNECT_TIMEOUT,
            )

            # 创建本地端口转发
            transport = self._ssh_client.get_transport()
            if transport is None:
                raise RuntimeError("SSH 传输层未就绪")

            # 请求本地端口转发：本机 -> 远端渲染服务节点
            # 注意：这里假设渲染服务在本地 (localhost) 上运行
            local_port = await asyncio.get_event_loop().run_in_executor(
                None,
                lambda: transport.request_port_forward('', self._local_tunnel_port)
            )
            self._local_tunnel_port = local_port
            return f"ws://127.0.0.1:{local_port}/ws/render-v3"

        except Exception as exc:
            logger.error("SSH 隧道建立失败: %s", exc)
            raise ProviderError(
                ProviderErrorCode.CONNECT_TIMEOUT,
                f"SSH 隧道连接失败：{self.ssh_host}:{self.ssh_port}",
                restart_required=True,
            ) from exc

    def _validate_transport_security(self, effective_node_url: str) -> None:
        parsed = urlparse(effective_node_url)
        hostname = (parsed.hostname or "").lower()
        loopback = hostname in {"localhost", "127.0.0.1", "::1"}
        if parsed.scheme not in {"ws", "wss"} or not hostname:
            raise RuntimeError("sidecar URL 必须是合法 ws:// 或 wss:// 地址")
        if parsed.scheme == "ws" and not loopback:
            raise RuntimeError("非本机 sidecar 必须使用 wss//，禁止明文传输媒体和凭据")
        if not loopback and not self.auth_token:
            raise RuntimeError("远程 sidecar 必须配置鉴权 token")

    @staticmethod
    def _strict_int(mapping: dict, field: str, context: str) -> int:
        value = mapping.get(field)
        if isinstance(value, bool) or not isinstance(value, int):
            raise RuntimeError(f"sidecar {context}.{field} 必须是 int")
        return value

    def _descriptor_evidence(self, message: dict, context: str) -> dict:
        nested = message.get("evidence")
        evidence = nested if isinstance(nested, dict) else message
        backend_id = evidence.get(
            "backend_id",
            evidence.get("id", message.get("backend_id", message.get("id"))),
        )
        if backend_id != self._selected_descriptor.get("id"):
            raise RuntimeError(f"sidecar {context} backend 与握手 descriptor 不一致")
        normalized = {"backend_id": backend_id}
        for field in self.EVIDENCE_FIELDS:
            if field not in evidence or evidence[field] != self._selected_descriptor.get(field):
                raise RuntimeError(f"sidecar {context} evidence.{field} 与握手 descriptor 不一致")
            normalized[field] = evidence[field]
        # 部分实现会重复握手状态；一旦出现就必须一致，不能用矛盾字段降级冒充。
        for field in ("available", "neural", "warmed"):
            if field in evidence and evidence[field] != self._selected_descriptor.get(field):
                raise RuntimeError(f"sidecar {context} evidence.{field} 与握手 descriptor 不一致")
        return normalized

    def _validate_render_accepted(self, accepted: dict, request_id: str) -> tuple[int, dict]:
        if accepted.get("event") != "render_accepted" or accepted.get("request_id") != request_id:
            raise RuntimeError(accepted.get("message") or "sidecar 未接受 render_open")
        evidence = {}
        if self._strict_completion or self.require_neural_lipsync:
            evidence = self._descriptor_evidence(accepted, "render_accepted")
        credit = self._strict_int(accepted, "initial_credit", "render_accepted")
        if credit <= 0 or credit > self.MAX_INITIAL_CREDIT:
            raise RuntimeError("sidecar initial_credit 非法")
        return credit, evidence

    def _validate_render_complete(
        self,
        complete: dict,
        request_id: str,
        first,
        frame_count: int,
        total_samples: int,
        audio_bytes: int,
        video_ledger: dict,
        accepted_evidence: dict,
    ) -> None:
        if complete.get("request_id") != request_id or complete.get("audio_id") != first.audio_id:
            raise RuntimeError("sidecar render_complete 事务身份不匹配")
        complete_evidence = self._descriptor_evidence(complete, "render_complete")
        if complete_evidence != accepted_evidence:
            raise RuntimeError("sidecar render_complete evidence 与 render_accepted 不一致")
        if self._strict_int(complete, "rendered_frames", "render_complete") != video_ledger["frames"]:
            raise RuntimeError("sidecar render_complete.rendered_frames 与客户端不一致")
        if self._strict_int(complete, "last_pts_samples", "render_complete") != total_samples:
            raise RuntimeError("sidecar render_complete.last_pts_samples 与音频总采样不一致")
        if video_ledger["frames"] < 1:
            raise RuntimeError("strict sidecar 必须至少返回 1 个视频帧")

        totals = complete.get("strict_totals")
        if not isinstance(totals, dict):
            raise RuntimeError("sidecar render_complete 缺少 strict_totals")
        expected = {
            "declared_frames": frame_count,
            "declared_samples": total_samples,
            "frame_duration_samples": first.duration_samples,
            "received_frames": frame_count,
            "received_samples": total_samples,
            "received_bytes": audio_bytes,
            "rendered_frames": video_ledger["frames"],
            "rendered_bytes": video_ledger["bytes"],
            "last_video_pts_samples": video_ledger["last_pts"],
        }
        for field, expected_value in expected.items():
            if self._strict_int(totals, field, "strict_totals") != expected_value:
                raise RuntimeError(f"sidecar strict_totals.{field} 与客户端不一致")

    async def feed_audio_frames(self, frames) -> None:
        """发送完整 PCM 事务，并在 accepted 后将视频逐帧送入共享时钟 timeline。"""
        frame_list = validate_audio_frame_batch(frames)
        first = frame_list[0]
        total_samples = sum(frame.duration_samples for frame in frame_list)
        audio_bytes = sum(len(frame.data) for frame in frame_list)
        async with self._lock:
            if not await self._ensure_connection():
                raise ProviderError(
                    ProviderErrorCode.REMOTE_UNAVAILABLE,
                    self.last_error or "神经渲染 sidecar 未就绪",
                    retryable=True,
                )
            advertised_formats = self._capabilities.get("input_formats")
            actual_format = first.format.to_dict()
            if not isinstance(advertised_formats, list) or not any(
                isinstance(item, dict)
                and all(item.get(key) == value for key, value in actual_format.items())
                for item in advertised_formats
            ):
                raise ProviderError(
                    ProviderErrorCode.CAPABILITY_MISMATCH,
                    "sidecar 不支持当前 AudioFrame format",
                )
            request_id = uuid.uuid4().hex
            self._current_request_id = request_id
            self._current_audio_id = first.audio_id
            self._request_owner_task = asyncio.current_task()
            self.last_cancel_acknowledged = None
            self.is_speaking = True
            deadline = time.monotonic() + self.REQUEST_TIMEOUT
            timeline_queue: Optional[asyncio.Queue] = None
            timeline_task: Optional[asyncio.Task] = None
            video_ledger = {"frames": 0, "bytes": 0, "last_sequence": None, "last_pts": None}
            try:
                await self._send_raw(json.dumps({
                    "event": "render_open",
                    "protocol_version": PROTOCOL_VERSION,
                    "request_id": request_id,
                    "audio_id": first.audio_id,
                    "audio_generation": first.audio_generation,
                    "session_generation": first.session_generation,
                    "format": first.format.to_dict(),
                    "frame_duration_samples": frame_list[0].duration_samples,
                    "total_frames": len(frame_list),
                    "total_samples": total_samples,
                    "backend_id": self._selected_descriptor.get("id"),
                    "avatar_id": self.avatar_id,
                    "avatar_revision": self.avatar_revision,
                    "avatar_digest": self.avatar_digest,
                    "license_manifest_digest": self.license_manifest_digest,
                    "weights_sha256": self.weights_sha256,
                    "model_version": self.model_version,
                    "text": first.text,
                }), deadline)
                accepted = await self._recv_json(deadline)
                credit, accepted_evidence = self._validate_render_accepted(accepted, request_id)
                timeline_queue, timeline_task = self._start_timeline(
                    first.audio_id,
                    first.format.sample_rate,
                    total_samples,
                )

                for frame in frame_list:
                    while credit <= 0:
                        credit = await self._consume_message(
                            request_id,
                            first,
                            deadline,
                            credit,
                            timeline_queue,
                            video_ledger,
                            total_samples,
                        )
                    await self._send_raw(
                        encode_audio_frame(request_id, frame), deadline
                    )
                    credit -= 1

                await self._send_raw(json.dumps({
                    "event": "render_finish",
                    "request_id": request_id,
                    "audio_id": first.audio_id,
                    "final_sequence": frame_list[-1].sequence,
                    "total_samples": total_samples,
                }), deadline)
                while True:
                    raw = await self._recv_raw(deadline)
                    if isinstance(raw, bytes):
                        self._accept_video(
                            raw,
                            request_id,
                            first,
                            timeline_queue,
                            video_ledger,
                            total_samples,
                        )
                        continue
                    message = self._decode_control(raw)
                    if message.get("request_id") != request_id:
                        raise ProviderError(
                            ProviderErrorCode.PROTOCOL_VIOLATION,
                            "sidecar 在单事务连接返回了其他 request_id",
                            request_id=str(message.get("request_id") or ""),
                            restart_required=True,
                        )
                    event = message.get("event")
                    if event == "render_complete":
                        if self._strict_completion:
                            self._validate_render_complete(
                                message,
                                request_id,
                                first,
                                len(frame_list),
                                total_samples,
                                audio_bytes,
                                video_ledger,
                                accepted_evidence,
                            )
                        break
                    if event == "render_credit":
                        continue
                    if event == "error":
                        raise self._wire_error(message)
                    if event == "render_cancelled":
                        raise ProviderError(
                            ProviderErrorCode.CANCEL_UNCONFIRMED,
                            str(message.get("message") or event),
                            request_id=request_id,
                        )

                if timeline_task.done():
                    raise RuntimeError("sidecar 视频时间线 consumer 已提前终止")
                try:
                    timeline_queue.put_nowait(_TIMELINE_END)
                except asyncio.QueueFull as exc:
                    raise RuntimeError("sidecar 视频时间线队列已满") from exc
                self.transactions_completed += 1
                self.is_degraded = False
                self.last_error = ""
            except asyncio.CancelledError:
                if timeline_task is not None:
                    await self._cancel_timeline(timeline_task)
                await self._abort_request(request_id, "local task cancelled")
                raise
            except Exception as exc:
                self.transactions_failed += 1
                self.is_degraded = True
                self.last_error = str(exc)
                if timeline_task is not None:
                    await self._cancel_timeline(timeline_task)
                await self._abort_request(request_id, "render error")
                raise
            finally:
                self.is_speaking = False
                if self._current_request_id == request_id:
                    self._current_request_id = None
                    self._current_audio_id = None
                if self._request_owner_task is asyncio.current_task():
                    self._request_owner_task = None

    async def _recv_raw(self, deadline: float):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ProviderError(
                ProviderErrorCode.REQUEST_TIMEOUT,
                "sidecar 整句请求截止时间已到",
                retryable=True,
            )
        timeout = min(self.MESSAGE_TIMEOUT, remaining)
        try:
            return await asyncio.wait_for(self._ws.recv(), timeout=timeout)
        except TimeoutError as exc:
            code = (
                ProviderErrorCode.REQUEST_TIMEOUT
                if remaining <= self.MESSAGE_TIMEOUT
                else ProviderErrorCode.MESSAGE_TIMEOUT
            )
            raise ProviderError(code, "sidecar 接收超时", retryable=True) from exc

    @staticmethod
    def _decode_control(raw: str) -> dict:
        message = json.loads(raw)
        if not isinstance(message, dict):
            raise RuntimeError("sidecar 控制消息必须是 object")
        return message

    async def _recv_json(self, deadline: float) -> dict:
        raw = await self._recv_raw(deadline)
        if isinstance(raw, bytes):
            raise RuntimeError("期望 sidecar 控制消息却收到二进制帧")
        return self._decode_control(raw)

    @staticmethod
    def _wire_error(message: dict) -> ProviderError:
        raw_code = str(message.get("code") or "remote_unavailable").strip().lower()
        code_map = {
            "authentication": ProviderErrorCode.AUTHENTICATION,
            "unauthorized": ProviderErrorCode.AUTHENTICATION,
            "capability_mismatch": ProviderErrorCode.CAPABILITY_MISMATCH,
            "invalid_request": ProviderErrorCode.INVALID_REQUEST,
            "rate_limited": ProviderErrorCode.RATE_LIMITED,
            "resource_exhausted": ProviderErrorCode.REMOTE_RESOURCE_EXHAUSTED,
            "capacity_exceeded": ProviderErrorCode.REMOTE_RESOURCE_EXHAUSTED,
            "request_timeout": ProviderErrorCode.REQUEST_TIMEOUT,
            "protocol_violation": ProviderErrorCode.PROTOCOL_VIOLATION,
        }
        details = {"wire_code": raw_code}
        resource_stats = message.get("resource_stats")
        if isinstance(resource_stats, dict):
            details["resource_stats"] = dict(resource_stats)
        return ProviderError(
            code_map.get(raw_code, ProviderErrorCode.REMOTE_UNAVAILABLE),
            str(message.get("message") or raw_code),
            retryable=message.get("retryable") is True,
            restart_required=message.get("restart_required") is True,
            request_id=str(message.get("request_id") or ""),
            details=details,
        )

    async def _consume_message(
        self,
        request_id: str,
        first,
        deadline: float,
        credit: int,
        timeline_queue: asyncio.Queue,
        video_ledger: dict,
        total_samples: int,
    ) -> int:
        raw = await self._recv_raw(deadline)
        if isinstance(raw, bytes):
            self._accept_video(
                raw,
                request_id,
                first,
                timeline_queue,
                video_ledger,
                total_samples,
            )
            return credit
        message = self._decode_control(raw)
        if message.get("request_id") != request_id:
            raise ProviderError(
                ProviderErrorCode.PROTOCOL_VIOLATION,
                "sidecar 在单事务连接返回了其他 request_id",
                request_id=str(message.get("request_id") or ""),
                restart_required=True,
            )
        event = message.get("event")
        if event == "render_credit":
            granted = self._strict_int(message, "credit", "render_credit")
            if granted <= 0 or credit + granted > self.MAX_INITIAL_CREDIT:
                raise RuntimeError("sidecar render_credit 非法")
            return credit + granted
        if event == "error":
            raise self._wire_error(message)
        if event == "render_cancelled":
            raise ProviderError(
                ProviderErrorCode.CANCEL_UNCONFIRMED,
                str(message.get("message") or event),
                request_id=request_id,
            )
        return credit

    def _accept_video(
        self,
        raw: bytes,
        request_id: str,
        first,
        timeline_queue: asyncio.Queue,
        video_ledger: dict,
        total_samples: int,
    ) -> None:
        frame = decode_video_frame(raw)
        if (
            frame.request_id != request_id
            or frame.audio_id != first.audio_id
            or frame.audio_generation != first.audio_generation
            or frame.session_generation != first.session_generation
        ):
            self.frames_dropped += 1
            return
        if frame.pts_samples >= total_samples:
            raise RuntimeError("sidecar 视频 PTS 超出音频事务范围")
        if video_ledger["last_sequence"] is not None and (
            frame.sequence <= video_ledger["last_sequence"]
            or frame.pts_samples < video_ledger["last_pts"]
        ):
            raise RuntimeError("sidecar 视频 sequence/PTS 非单调")
        if video_ledger["frames"] >= self.MAX_VIDEO_FRAMES:
            raise RuntimeError("sidecar 视频帧数超过上限")
        next_bytes = video_ledger["bytes"] + len(frame.jpeg)
        if next_bytes > self.MAX_VIDEO_BYTES:
            raise RuntimeError("sidecar 视频字节超过上限")
        consumer = self._timeline_consumers.get(timeline_queue)
        if consumer is None or consumer.done():
            raise RuntimeError("sidecar 视频时间线 consumer 已提前终止")
        if timeline_queue.full():
            raise RuntimeError("sidecar 视频时间线队列已满")
        if self._pending_timeline_bytes + len(frame.jpeg) > self.MAX_PENDING_VIDEO_BYTES:
            raise RuntimeError("sidecar 全局视频时间线容量已满")
        try:
            timeline_queue.put_nowait(frame)
        except asyncio.QueueFull as exc:
            raise RuntimeError("sidecar 视频时间线队列已满") from exc
        self._pending_timeline_bytes += len(frame.jpeg)
        video_ledger["frames"] += 1
        video_ledger["bytes"] = next_bytes
        video_ledger["last_sequence"] = frame.sequence
        video_ledger["last_pts"] = frame.pts_samples

    def _start_timeline(
        self,
        audio_id: str,
        sample_rate: int,
        total_samples: int,
    ) -> tuple[asyncio.Queue, asyncio.Task]:
        active_timelines = [task for task in self._timeline_tasks if not task.done()]
        if len(active_timelines) >= self.MAX_PENDING_TIMELINES:
            raise RuntimeError("sidecar 视频时间线数量已满")
        # 额外一个槽位专供 sentinel，视频本身仍受 3000 帧事务上限约束。
        queue: asyncio.Queue = asyncio.Queue(maxsize=self.MAX_VIDEO_FRAMES + 1)
        task = asyncio.create_task(
            self._play_video_timeline(audio_id, sample_rate, total_samples, queue)
        )
        self._timeline_task = task
        self._timeline_tasks.add(task)
        self._timeline_consumers[queue] = task
        task.add_done_callback(lambda done, timeline_queue=queue: self._timeline_done(done, timeline_queue))
        return queue, task

    async def _abort_request(self, request_id: str, reason: str) -> bool:
        """请求取消并等待可证明静止的 ACK，随后关闭 connection epoch。"""
        acknowledged = False
        ws = self._ws
        if ws is not None:
            deadline = time.monotonic() + min(2.0, self.MESSAGE_TIMEOUT)
            try:
                await self._send_raw(
                    json.dumps({
                        "event": "render_cancel",
                        "request_id": request_id,
                        "reason": reason,
                    }),
                    deadline,
                )
                while True:
                    raw = await self._recv_raw(deadline)
                    if isinstance(raw, bytes):
                        continue
                    message = self._decode_control(raw)
                    if message.get("request_id") != request_id:
                        raise ProviderError(
                            ProviderErrorCode.PROTOCOL_VIOLATION,
                            "sidecar cancel 返回了其他 request_id",
                            request_id=str(message.get("request_id") or ""),
                            restart_required=True,
                        )
                    if message.get("event") == "error":
                        raise self._wire_error(message)
                    if message.get("event") != "render_cancelled":
                        continue
                    acknowledged = bool(
                        message.get("cancel_ack") is True
                        and message.get("quiesced") is True
                    )
                    break
            except Exception as exc:
                self.last_error = f"cancel_unconfirmed: {exc}"
        self.last_cancel_acknowledged = acknowledged
        # 无论 ACK 是否成功都关闭 connection epoch，隔离迟到帧与下一事务。
        await self._close_connection()
        return acknowledged

    async def interrupt(self, reason: str = "Barge-in", next_generation: int | None = None) -> None:
        """取消 owner task；只有 render owner 可以读取 cancel ACK，杜绝 concurrent recv。"""
        self.is_speaking = False
        owner = self._request_owner_task
        current = asyncio.current_task()
        if owner is not None and owner is not current and not owner.done():
            owner.cancel()
            try:
                await owner
            except asyncio.CancelledError:
                pass
            except Exception:
                pass
            if self.last_cancel_acknowledged is not True:
                raise ProviderError(
                    ProviderErrorCode.CANCEL_UNCONFIRMED,
                    self.last_error or "sidecar 未证明取消后已静止",
                    restart_required=True,
                )
            return

        await self._cancel_timelines(clear_frame=True)
        request_id = self._current_request_id
        if request_id:
            acknowledged = await self._abort_request(request_id, reason)
            if not acknowledged:
                raise ProviderError(
                    ProviderErrorCode.CANCEL_UNCONFIRMED,
                    self.last_error or "sidecar 未证明取消后已静止",
                    restart_required=True,
                )
        else:
            await self._close_connection()

    def _timeline_done(self, task: asyncio.Task, queue: asyncio.Queue) -> None:
        self._timeline_tasks.discard(task)
        self._timeline_consumers.pop(queue, None)
        if self._timeline_task is task:
            self._timeline_task = next(
                (candidate for candidate in self._timeline_tasks if not candidate.done()),
                None,
            )
        try:
            task.result()
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.debug("sidecar 视频时间线异常", exc_info=True)

    def _release_timeline_frame(self, frame: SidecarVideoFrame) -> None:
        self._pending_timeline_bytes = max(0, self._pending_timeline_bytes - len(frame.jpeg))

    def _drain_timeline_queue(self, queue: asyncio.Queue) -> None:
        while True:
            try:
                item = queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            if item is not _TIMELINE_END:
                self._release_timeline_frame(item)
            queue.task_done()

    async def _play_video_timeline(
        self,
        audio_id: str,
        sample_rate: int,
        total_samples: int,
        queue: asyncio.Queue,
    ) -> None:
        current: Optional[SidecarVideoFrame] = None
        buffered: Optional[SidecarVideoFrame] = None
        current_task = asyncio.current_task()
        completed_normally = False
        try:
            # 清理层必须包住锁获取：等待上一句期间被取消也要归还本事务已排队字节。
            async with self._timeline_lock:
                started = time.monotonic()
                deadline = started + (total_samples / float(sample_rate)) + self.TIMELINE_GRACE_SECONDS
                using_fallback = False
                while True:
                    if current is None:
                        if buffered is not None:
                            current, buffered = buffered, None
                        else:
                            item = await queue.get()
                            if item is _TIMELINE_END:
                                queue.task_done()
                                completed_normally = True
                                return
                            current = item

                    while True:
                        if time.monotonic() > deadline:
                            self.frames_dropped += 1
                            self.is_degraded = True
                            self.last_error = "sidecar 视频时间线超时"
                            return
                        from server.core.media.virtual_audio import global_virtual_audio

                        clock = global_virtual_audio.get_playback_clock(audio_id)
                        reject_reason = clock.get("reject_reason") if clock else None
                        if clock and (clock.get("is_interrupted") or clock.get("is_rejected")):
                            if reject_reason not in {
                                "service_disabled",
                                "audio_unavailable",
                                "queue_full",
                                "audio_capacity_exceeded",
                                "stream_unavailable",
                                "decode_empty",
                                "playback_error",
                            }:
                                return
                            using_fallback = True
                        if clock and not using_fallback and not clock.get("has_started"):
                            await asyncio.sleep(0.01)
                            continue
                        elapsed_samples = (
                            int(clock.get("samples_played", 0))
                            if clock and not using_fallback
                            else int((time.monotonic() - started) * sample_rate)
                        )
                        if elapsed_samples >= current.pts_samples:
                            break
                        await asyncio.sleep(
                            min(0.01, (current.pts_samples - elapsed_samples) / sample_rate)
                        )

                    stream_done = False
                    # 若播放头已越过多帧，只展示当前时刻应显示的最新帧，避免高速追帧。
                    while True:
                        try:
                            item = queue.get_nowait()
                        except asyncio.QueueEmpty:
                            break
                        if item is _TIMELINE_END:
                            queue.task_done()
                            stream_done = True
                            break
                        if item.pts_samples <= elapsed_samples:
                            self.frames_dropped += 1
                            self._release_timeline_frame(current)
                            queue.task_done()
                            current = item
                            continue
                        buffered = item
                        break

                    try:
                        if clock and not using_fallback and clock.get("has_started"):
                            _audio_head_ms = (float(clock.get("elapsed_sec", 0.0) or 0.0)) * 1000.0
                            if _audio_head_ms <= 0.0 and sample_rate > 0:
                                _audio_head_ms = (int(clock.get("samples_played", 0)) / float(sample_rate)) * 1000.0
                            global_shared_playback_clock.report_audio_head(audio_id, _audio_head_ms)
                            _drift = global_shared_playback_clock.compute_drift()
                            global_av_sync.apply_drift(_drift, anchored=True)
                            # 发布前把采样时钟锚点传给视频侧，使两侧同源 (消除跨时钟域误差)
                            self._pending_video_pts_ms = _audio_head_ms
                        else:
                            global_av_sync.apply_drift(None, anchored=False)
                            self._pending_video_pts_ms = None
                    except Exception:
                        pass

                    await self._publish_video_frame(current.jpeg, current_task)
                    self._release_timeline_frame(current)
                    queue.task_done()
                    current = None
                    if stream_done and buffered is None:
                        completed_normally = True
                        return
        except asyncio.CancelledError:
            raise
        finally:
            if current is not None:
                self._release_timeline_frame(current)
                queue.task_done()
            if buffered is not None:
                self._release_timeline_frame(buffered)
                queue.task_done()
            self._drain_timeline_queue(queue)
            if self._timeline_task is current_task and (
                completed_normally or self._latest_frame_owner is current_task
            ):
                self.latest_jpeg = b""
                self._latest_frame_owner = None

    def _clear_frame_if_owned(self, owner: Optional[asyncio.Task]) -> None:
        if self._latest_frame_owner is owner:
            self.latest_jpeg = b""
            self._latest_frame_owner = None

    async def _publish_video_frame(self, jpeg: bytes, owner: Optional[asyncio.Task]) -> None:
        if not jpeg or not jpeg.startswith(b"\xff\xd8") or not jpeg.endswith(b"\xff\xd9"):
            self.frames_dropped += 1
            self._clear_frame_if_owned(owner)
            return
        image_rgb = None
        try:
            import cv2
            import numpy as np

            image = cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_COLOR)
            if image is None:
                self.frames_dropped += 1
                self._clear_frame_if_owned(owner)
                return
            image_rgb = cv2.cvtColor(image, cv2.COLOR_BGR2RGB)
        except ImportError:
            # 无 OpenCV 环境仍可把结构完整的 JPEG 交给 MJPEG 浏览器预览。
            pass
        except Exception:
            self.frames_dropped += 1
            self._clear_frame_if_owned(owner)
            return

        if image_rgb is not None:
            try:
                from server.core.media.scene_overlay import compose_scene_overlays, global_scene_overlay_state
                snapshot = global_scene_overlay_state.snapshot()
                composed_rgb = compose_scene_overlays(
                    image_rgb,
                    snapshot,
                    enable_anti_recording=True,
                    timestamp=time.time(),
                )
                if composed_rgb is not None:
                    image_rgb = composed_rgb
                    bgr = cv2.cvtColor(composed_rgb, cv2.COLOR_RGB2BGR)
                    ret, enc = cv2.imencode(".jpg", bgr, [int(cv2.IMWRITE_JPEG_QUALITY), 90])
                    if ret:
                        jpeg = enc.tobytes()
            except Exception:
                pass

        self.latest_jpeg = jpeg
        self._latest_frame_owner = owner
        self.frames_received += 1
        try:
            # 视频帧发布打 PTS 锚点；携带采样时钟换算值时与音频侧同源
            global_shared_playback_clock.stamp_video(self.frames_received, self._pending_video_pts_ms)
        except Exception:
            pass
        self._pending_video_pts_ms = None
        if image_rgb is not None:
            try:
                from server.core.media.virtual_cam import global_virtual_cam

                if global_virtual_cam.is_active:
                    global_virtual_cam.send_frame(
                        image_rgb, owner="neural_sidecar", priority=100
                    )
            except Exception:
                pass

    async def _cancel_timeline(self, task: asyncio.Task) -> None:
        queues = [queue for queue, consumer in self._timeline_consumers.items() if consumer is task]
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        # Task 可能在首次调度前即被取消，此时 coroutine finally 尚未运行。
        for queue in queues:
            self._drain_timeline_queue(queue)

    async def _cancel_timelines(self, *, clear_frame: bool) -> None:
        tasks = list(self._timeline_tasks)
        queues = list(self._timeline_consumers)
        if self._timeline_task and self._timeline_task not in tasks:
            tasks.append(self._timeline_task)
        self._timeline_task = None
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        for queue in queues:
            self._drain_timeline_queue(queue)
        self._timeline_tasks.clear()
        self._timeline_consumers.clear()
        self._pending_timeline_bytes = 0
        if clear_frame:
            self.latest_jpeg = b""
            self._latest_frame_owner = None
        try:
            global_shared_playback_clock.reset()
            global_av_sync.apply_drift(None, anchored=False)
        except Exception:
            pass

    @property
    def has_active_request(self) -> bool:
        return bool(self._current_request_id)

    @property
    def has_frames(self) -> bool:
        return bool(self.latest_jpeg)

    def get_latest_jpeg(self) -> bytes:
        return self.latest_jpeg

    def _neural_state(self) -> tuple[bool, bool, bool]:
        descriptor = self._selected_descriptor
        supported = bool(
            self._capabilities.get("neural_lipsync") is True
            and descriptor.get("neural") is True
        )
        descriptor_ready = supported and not self._neural_descriptor_error(descriptor)
        ready = bool(self.is_ready and descriptor_ready)
        active = bool(
            self.is_connected
            and ready
            and self._selected_version == PROTOCOL_VERSION
        )
        return supported, ready, active

    def get_media_capabilities(self) -> dict:
        renderer_active = bool(
            self.is_connected and self.is_ready and self._selected_version == PROTOCOL_VERSION
        )
        neural_supported, neural_ready, neural_active = self._neural_state()
        descriptor = self._selected_descriptor
        return {
            "accepts_audio_frames": renderer_active,
            "audio_frame_codec": "pcm_s16le",
            "tts_output_codec": None,
            "chunk_semantics": "pcm_frame",
            "transactional_sentence": True,
            "supports_cancel": True,
            "supports_shared_clock": True,
            "remote_protocol_version": self._selected_version,
            "renderer_only": True,
            "neural_lipsync": neural_active,
            "neural_lipsync_supported": neural_supported,
            "neural_lipsync_ready": neural_ready,
            "neural_lipsync_active": neural_active,
            "renderer_active": renderer_active,
            "requested_backend_id": self.backend_id,
            "selected_backend_id": descriptor.get("id"),
            "backend_id": descriptor.get("id"),
            "requested_model_version": self.model_version,
            "selected_model_version": descriptor.get("model_version"),
            "model_version": descriptor.get("model_version"),
            "requested_weights_sha256": self.weights_sha256,
            "selected_weights_sha256": descriptor.get("weights_sha256"),
            "weights_sha256": descriptor.get("weights_sha256"),
            "requested_license_manifest_sha256": self.license_manifest_digest,
            "selected_license_manifest_sha256": descriptor.get("license_manifest_sha256"),
            "license_manifest_sha256": descriptor.get("license_manifest_sha256"),
            "requested_avatar_id": self.avatar_id,
            "selected_avatar_id": descriptor.get("avatar_id"),
            "requested_avatar_revision": self.avatar_revision,
            "selected_avatar_revision": descriptor.get("avatar_revision"),
            "avatar_revision": descriptor.get("avatar_revision"),
            "requested_avatar_digest": self.avatar_digest,
            "selected_avatar_digest": descriptor.get("avatar_digest"),
            "avatar_digest": descriptor.get("avatar_digest"),
            "strict_completion": self._strict_completion,
            "streaming_video": self._streaming_video,
            "require_neural_lipsync": self.require_neural_lipsync,
            "node_version": self._node_version,
            "credit_backpressure": True,
            "supports_render_started": self._capabilities.get("supports_render_started") is True,
            "supports_sample_pts": self._capabilities.get("supports_sample_pts") is True,
            "supports_cancel_ack": self._capabilities.get("supports_cancel_ack") is True,
        }

    def get_preview_status(self) -> dict:
        media_capabilities = self.get_media_capabilities()
        active = media_capabilities["renderer_active"]
        return {
            "configured": True,
            "fps": int(self._capabilities.get("fps") or 25),
            "is_running": self.is_running,
            "is_speaking": self.is_speaking,
            "render_backend": "neural_sidecar" if media_capabilities["neural_lipsync_active"] else "sidecar",
            "remote_connected": self.is_connected,
            "remote_ready": self.is_ready,
            "degraded": self.is_degraded,
            "remote_protocol": self._selected_version,
            "node_version": self._node_version,
            "requested_backend_id": self.backend_id,
            "selected_backend_id": self._selected_descriptor.get("id"),
            "backend_id": self._selected_descriptor.get("id"),
            "requested_model_version": self.model_version,
            "selected_model_version": self._selected_descriptor.get("model_version"),
            "model_version": self._selected_descriptor.get("model_version"),
            "requested_weights_sha256": self.weights_sha256,
            "selected_weights_sha256": self._selected_descriptor.get("weights_sha256"),
            "weights_sha256": self._selected_descriptor.get("weights_sha256"),
            "requested_license_manifest_sha256": self.license_manifest_digest,
            "selected_license_manifest_sha256": self._selected_descriptor.get("license_manifest_sha256"),
            "license_manifest_sha256": self._selected_descriptor.get("license_manifest_sha256"),
            "requested_avatar_revision": self.avatar_revision,
            "selected_avatar_revision": self._selected_descriptor.get("avatar_revision"),
            "avatar_revision": self._selected_descriptor.get("avatar_revision"),
            "requested_avatar_digest": self.avatar_digest,
            "selected_avatar_digest": self._selected_descriptor.get("avatar_digest"),
            "avatar_digest": self._selected_descriptor.get("avatar_digest"),
            "neural_lipsync_supported": media_capabilities["neural_lipsync_supported"],
            "neural_lipsync_ready": media_capabilities["neural_lipsync_ready"],
            "neural_lipsync_active": media_capabilities["neural_lipsync_active"],
            "remote_frames": self.frames_received,
            "frames_dropped": self.frames_dropped,
            "transactions_completed": self.transactions_completed,
            "transactions_failed": self.transactions_failed,
            "pending_timelines": len(self._timeline_tasks),
            "pending_video_bytes": self._pending_timeline_bytes,
            "has_frame": self.has_frames,
            "request_id": self._current_request_id,
            "last_error": self.last_error,
            "last_cancel_acknowledged": self.last_cancel_acknowledged,
            "timeouts": {
                "connect_seconds": self.CONNECT_TIMEOUT,
                "message_seconds": self.MESSAGE_TIMEOUT,
                "request_seconds": self.REQUEST_TIMEOUT,
            },
            "capabilities": {
                "neural_lipsync": media_capabilities["neural_lipsync_active"],
                "neural_lipsync_supported": media_capabilities["neural_lipsync_supported"],
                "neural_lipsync_ready": media_capabilities["neural_lipsync_ready"],
                "remote_rendering": active,
                "remote_rendering_supported": True,
                "shared_playback_clock": active,
                "credit_backpressure": active,
                "strict_completion": media_capabilities["strict_completion"],
                "streaming_video": media_capabilities["streaming_video"],
            },
            "media_contract": media_capabilities,
        }
