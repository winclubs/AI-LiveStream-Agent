"""严格 v3 WebSocket 服务：receiver、唯一 sender 与有界模型 worker 相互分离。"""

from __future__ import annotations

import asyncio
import hmac
import ipaddress
import json
import logging
import ssl
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Mapping

from gpu_sidecar.backend import (
    BackendOOMError,
    BackendTimeoutError,
    BackendUnavailableError,
    Wav2LipBackend,
)
from gpu_sidecar.config import SidecarConfig
from gpu_sidecar.contracts import InferenceActiveMarker, PluginFrameResult, PluginSession
from server.core.media.audio_frame import AudioFormat
from server.core.media.sidecar_protocol import (
    KIND_AUDIO,
    PROTOCOL_VERSION,
    SidecarVideoFrame,
    decode_envelope,
    encode_video_frame,
)

logger = logging.getLogger("LiveAgent.GPUSidecar")
_STOP = object()
_OUTBOUND_PUT_TIMEOUT_SECONDS = 1.0
_WEBSOCKET_SEND_TIMEOUT_SECONDS = 5.0
_CONNECTION_SHUTDOWN_TIMEOUT_SECONDS = 2.0


@dataclass(frozen=True, slots=True)
class WorkItem:
    kind: str
    metadata: Mapping[str, Any] = field(default_factory=dict)
    payload: bytes = b""


@dataclass(frozen=True, slots=True)
class OutboundMessage:
    payload: str | bytes
    transaction_epoch: int | None = None
    is_video: bool = False


@dataclass(slots=True)
class RenderTransaction:
    request: dict[str, Any]
    epoch: int
    request_id: str
    audio_id: str
    audio_generation: int
    session_generation: int
    audio_format: AudioFormat
    frame_duration_samples: int
    total_frames: int
    total_samples: int
    available_credit: int = 0
    session: PluginSession | None = None
    accepted: bool = False
    render_slot_held: bool = False
    cancel_task: asyncio.Task[None] | None = None
    next_audio_sequence: int = 0
    next_audio_pts: int = 0
    received_bytes: int = 0
    final_audio_seen: bool = False
    finish_requested: bool = False
    cancel_requested: bool = False
    cancel_ack_requested: bool = False
    cancel_reason: str = ""
    cancel_signal: threading.Event = field(default_factory=threading.Event)
    next_video_sequence: int = 0
    last_video_pts: int = -1
    rendered_frames: int = 0
    rendered_bytes: int = 0

    def declared_totals(self) -> dict[str, int]:
        return {
            "declared_frames": self.total_frames,
            "declared_samples": self.total_samples,
            "frame_duration_samples": self.frame_duration_samples,
        }

    def completion_totals(self) -> dict[str, int]:
        return {
            **self.declared_totals(),
            "received_frames": self.next_audio_sequence,
            "received_samples": self.next_audio_pts,
            "received_bytes": self.received_bytes,
            "rendered_frames": self.rendered_frames,
            "rendered_bytes": self.rendered_bytes,
            "last_video_pts_samples": self.last_video_pts,
        }


@dataclass(slots=True)
class ConnectionState:
    work_queue: asyncio.Queue[WorkItem]
    outbound: asyncio.Queue[OutboundMessage | object]
    wake_worker: asyncio.Event
    authenticated: bool = False
    current: RenderTransaction | None = None
    next_transaction_epoch: int = 1
    invalidated_epochs: set[int] = field(default_factory=set)
    shutdown: bool = False


class Wav2LipSidecarServer:
    """不改变 v3 wire，只在服务端加强严格事务与 GPU 调度语义。"""

    def __init__(self, *, backend: Wav2LipBackend, config: SidecarConfig, token: str = "") -> None:
        self.backend = backend
        self.config = config
        self.token = token
        self._connection_slots = asyncio.Semaphore(config.max_connections)
        self._render_slots = asyncio.Semaphore(config.max_active_renders)
        self._connections: dict[int, ConnectionState] = {}

    def capabilities(self) -> dict[str, Any]:
        descriptor = self.backend.descriptor
        ready = bool(
            descriptor.available
            and descriptor.neural
            and descriptor.warmed
            and descriptor.license_approved
        )
        formats = [audio_format.to_dict() for audio_format in self.config.input_formats]
        sample_rates = sorted({item.sample_rate for item in self.config.input_formats})
        channels = sorted({item.channels for item in self.config.input_formats})
        backend_item = {
            "id": descriptor.backend_id,
            "model_version": descriptor.model_version,
            "available": ready,
            "neural": ready,
            "warmed": descriptor.warmed,
            "license_approved": descriptor.license_approved,
            "implementation_sha256": descriptor.implementation_sha256,
            "weights_sha256": descriptor.weights_sha256,
            "license_manifest_sha256": descriptor.license_manifest_sha256,
            "avatar_id": descriptor.avatar_id,
            "avatar_revision": descriptor.avatar_revision,
            "avatar_digest": descriptor.avatar_digest,
            "gpu_device": descriptor.gpu_device,
            "gpu_name": descriptor.gpu_name,
            "gpu_compute_capability": descriptor.gpu_compute_capability,
            "unavailable_reason": descriptor.unavailable_reason,
        }
        return {
            "renderer_available": ready,
            "neural_lipsync": ready,
            "strict_completion": True,
            "streaming_video": True,
            "supports_cancel_ack": True,
            "cancel_threadsafe": True,
            "cancel_quiesces": True,
            "supports_render_started": True,
            "supports_credit": True,
            "supports_sample_pts": True,
            "input_codecs": ["pcm_s16le"],
            "input_formats": formats,
            "sample_rates": sample_rates,
            "channels": channels,
            "fps": self.config.fps,
            "gpu_required": True,
            "gpu_device": descriptor.gpu_device,
            "gpu_name": descriptor.gpu_name,
            "gpu_compute_capability": descriptor.gpu_compute_capability,
            "model_version": descriptor.model_version,
            "weights_sha256": descriptor.weights_sha256,
            "resource_stats": self.backend.startup_resource_stats,
            "render_backends": [backend_item],
        }

    async def handle(self, websocket: Any) -> None:
        if self._connection_slots.locked():
            await websocket.close(code=1013, reason="sidecar connection capacity reached")
            return
        await self._connection_slots.acquire()
        try:
            await self._handle_admitted(websocket)
        finally:
            self._connection_slots.release()

    async def _handle_admitted(self, websocket: Any) -> None:
        state = ConnectionState(
            work_queue=asyncio.Queue(maxsize=self.config.worker_queue_size),
            outbound=asyncio.Queue(maxsize=self.config.sender_queue_size),
            wake_worker=asyncio.Event(),
        )
        connection_key = id(state)
        self._connections[connection_key] = state
        sender = asyncio.create_task(self._sender(websocket, state), name="gpu-sidecar-sender")
        worker = asyncio.create_task(self._worker(state), name="gpu-sidecar-worker")
        receiver = asyncio.create_task(self._receiver(websocket, state), name="gpu-sidecar-receiver")
        tasks = {sender, worker, receiver}
        try:
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                if task.cancelled():
                    continue
                error = task.exception()
                if error is not None:
                    logger.debug("sidecar connection task 结束", exc_info=error)
        finally:
            state.shutdown = True
            transaction = state.current
            if transaction is not None:
                transaction.cancel_requested = True
                transaction.cancel_reason = "connection closed"
                transaction.cancel_signal.set()
                state.invalidated_epochs.add(transaction.epoch)
            state.wake_worker.set()
            for task in (receiver, worker, sender):
                if not task.done():
                    task.cancel()
            try:
                await asyncio.wait_for(
                    asyncio.gather(*tasks, return_exceptions=True),
                    timeout=_CONNECTION_SHUTDOWN_TIMEOUT_SECONDS,
                )
            except TimeoutError:
                logger.warning("sidecar connection tasks 未在关闭期限内收敛")
            if transaction is not None and transaction.cancel_task is not None:
                try:
                    await asyncio.wait_for(
                        asyncio.shield(transaction.cancel_task),
                        timeout=self.config.cancel_timeout_seconds + 0.1,
                    )
                except Exception:
                    logger.debug("existing session.cancel task 未完成", exc_info=True)
            if (
                state.current is transaction
                and transaction is not None
                and transaction.session is not None
                and transaction.cancel_task is None
            ):
                await self._best_effort_cancel_session(transaction.session)
            close = getattr(websocket, "close", None)
            if callable(close):
                try:
                    await asyncio.wait_for(close(), timeout=_CONNECTION_SHUTDOWN_TIMEOUT_SECONDS)
                except Exception:
                    logger.debug("WebSocket bounded close 失败", exc_info=True)
            self._drain_work_queue(state)
            self._drain_outbound_queue(state)
            if transaction is not None:
                self._release_render_slot(transaction)
            state.current = None
            self._connections.pop(connection_key, None)

    async def _sender(self, websocket: Any, state: ConnectionState) -> None:
        """连接内只有此任务调用 websocket.send，确保控制帧与视频帧顺序唯一。"""
        while True:
            outgoing = await state.outbound.get()
            try:
                if outgoing is _STOP:
                    return
                if not isinstance(outgoing, OutboundMessage):
                    raise RuntimeError("内部 outbound 消息类型非法")
                if outgoing.is_video and outgoing.transaction_epoch in state.invalidated_epochs:
                    continue
                await asyncio.wait_for(
                    websocket.send(outgoing.payload),
                    timeout=_WEBSOCKET_SEND_TIMEOUT_SECONDS,
                )
            finally:
                state.outbound.task_done()

    async def _receiver(self, websocket: Any, state: ConnectionState) -> None:
        auth_deadline = time.monotonic() + self.config.auth_timeout_seconds
        idle_deadline = auth_deadline
        while True:
            deadline = idle_deadline if state.authenticated else auth_deadline
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("sidecar authentication/idle absolute deadline exceeded")
            try:
                raw = await asyncio.wait_for(websocket.recv(), timeout=remaining)
            except TimeoutError as exc:
                reason = "idle timeout" if state.authenticated else "authentication timeout"
                raise TimeoutError(reason) from exc
            if state.authenticated:
                idle_deadline = time.monotonic() + self.config.idle_timeout_seconds
            if isinstance(raw, bytes):
                await self._receive_audio(raw, state)
                continue
            try:
                message = json.loads(raw)
            except (TypeError, json.JSONDecodeError):
                await self._send_error(state, "控制消息不是合法 JSON object")
                continue
            if not isinstance(message, dict):
                await self._send_error(state, "控制消息顶层必须是 object")
                continue
            await self._receive_control(message, state)
            if state.authenticated:
                idle_deadline = time.monotonic() + self.config.idle_timeout_seconds

    async def _receive_control(self, message: dict[str, Any], state: ConnectionState) -> None:
        event = message.get("event")
        if event == "auth":
            await self._authenticate(message, state)
            return
        if not state.authenticated:
            await self._send_error(state, "not authenticated")
            return
        if event == "render_open":
            await self._open_transaction(message, state)
        elif event == "render_finish":
            await self._finish_transaction(message, state)
        elif event == "render_cancel":
            await self._cancel_transaction(message, state)
        else:
            await self._send_error(state, f"未知控制事件: {event!r}")

    async def _authenticate(self, message: dict[str, Any], state: ConnectionState) -> None:
        if state.authenticated:
            await self._send_error(state, "连接已完成鉴权")
            return
        supplied_token = message.get("token")
        if not isinstance(supplied_token, str) or not hmac.compare_digest(supplied_token, self.token):
            await self._send_error(state, "auth failed")
            return
        versions = message.get("supported_versions")
        supports_v3 = isinstance(versions, list) and any(
            type(version) is int and version == PROTOCOL_VERSION for version in versions
        )
        if not supports_v3:
            await self._send_error(state, "v3 required")
            return
        try:
            protocol_version = _strict_int(message.get("protocol_version"), "protocol_version", minimum=0)
        except ValueError as exc:
            await self._send_error(state, str(exc))
            return
        if protocol_version != PROTOCOL_VERSION:
            await self._send_error(state, "protocol_version 必须为 v3")
            return
        state.authenticated = True
        await self._send_json(
            state,
            {
                "event": "auth_ok",
                "selected_version": PROTOCOL_VERSION,
                "node_version": "authorized-wav2lip-plugin-v1",
                "capabilities": self.capabilities(),
            },
        )

    async def _open_transaction(self, message: dict[str, Any], state: ConnectionState) -> None:
        request_id = message.get("request_id") if isinstance(message.get("request_id"), str) else None
        if state.current is not None:
            await self._send_error(state, "一次连接只允许一个活动事务", request_id=request_id)
            return
        descriptor = self.backend.descriptor
        if not descriptor.available:
            await self._send_error(
                state,
                descriptor.unavailable_reason or "backend unavailable",
                request_id=request_id,
                code="backend_unavailable",
            )
            return
        if self._render_slots.locked():
            await self._send_error(
                state,
                "GPU render capacity reached; retry on a new transaction",
                request_id=request_id,
                code="render_capacity",
            )
            return
        await self._render_slots.acquire()
        transaction: RenderTransaction | None = None
        try:
            transaction = self._parse_render_open(message, state.next_transaction_epoch)
            transaction.render_slot_held = True
            state.work_queue.put_nowait(WorkItem(kind="open"))
        except (ValueError, asyncio.QueueFull) as exc:
            self._render_slots.release()
            await self._send_error(state, str(exc), request_id=request_id)
            return
        state.current = transaction
        state.next_transaction_epoch += 1
        state.wake_worker.set()

    def _parse_render_open(self, message: dict[str, Any], epoch: int) -> RenderTransaction:
        if _strict_int(message.get("protocol_version"), "protocol_version", minimum=0) != PROTOCOL_VERSION:
            raise ValueError("render_open.protocol_version 必须为 v3")
        request_id = _bounded_text(message.get("request_id"), "request_id")
        audio_id = _bounded_text(message.get("audio_id"), "audio_id")
        descriptor = self.backend.descriptor
        backend_id = _bounded_text(message.get("backend_id"), "backend_id")
        if backend_id != descriptor.backend_id:
            raise ValueError(f"backend_id 与已加载 descriptor 不匹配: {backend_id}")
        expected_descriptor = {
            "avatar_id": descriptor.avatar_id,
            "avatar_revision": descriptor.avatar_revision,
            "avatar_digest": descriptor.avatar_digest,
            "license_manifest_digest": descriptor.license_manifest_sha256,
            "weights_sha256": descriptor.weights_sha256,
            "model_version": descriptor.model_version,
        }
        for field_name, expected in expected_descriptor.items():
            supplied = _bounded_text(message.get(field_name), field_name)
            if supplied != expected:
                raise ValueError(f"{field_name} 与已加载 descriptor 不匹配")
        audio_generation = _strict_int(message.get("audio_generation"), "audio_generation", minimum=0)
        session_generation = _strict_int(message.get("session_generation"), "session_generation", minimum=0)
        audio_format = _parse_audio_format(message.get("format"))
        if not self.config.supports_format(audio_format):
            raise ValueError(f"输入 format 未在 config 授权: {audio_format.to_dict()}")
        frame_duration = _strict_int(message.get("frame_duration_samples"), "frame_duration_samples", minimum=1)
        total_frames = _strict_int(message.get("total_frames"), "total_frames", minimum=1)
        total_samples = _strict_int(message.get("total_samples"), "total_samples", minimum=1)
        if total_frames > self.config.max_transaction_frames:
            raise ValueError("total_frames 超过 config 上限")
        if total_samples > audio_format.sample_rate * self.config.max_transaction_seconds:
            raise ValueError("total_samples 超过 config 时长上限")
        minimum_samples = frame_duration * (total_frames - 1) + 1
        maximum_samples = frame_duration * total_frames
        if not minimum_samples <= total_samples <= maximum_samples:
            raise ValueError("total_frames/frame_duration_samples/total_samples 不一致")
        return RenderTransaction(
            request=dict(message),
            epoch=epoch,
            request_id=request_id,
            audio_id=audio_id,
            audio_generation=audio_generation,
            session_generation=session_generation,
            audio_format=audio_format,
            frame_duration_samples=frame_duration,
            total_frames=total_frames,
            total_samples=total_samples,
        )

    async def _receive_audio(self, raw: bytes, state: ConnectionState) -> None:
        transaction = state.current
        if not state.authenticated or transaction is None:
            await self._send_error(state, "没有可接收音频的活动事务")
            return
        if not transaction.accepted:
            await self._protocol_failure(state, transaction, "render_open 尚未被 backend 接受")
            return
        if transaction.cancel_requested or transaction.finish_requested:
            await self._protocol_failure(state, transaction, "事务已 cancel 或 finish，拒绝迟到音频")
            return
        try:
            if transaction.available_credit <= 0:
                raise ValueError("客户端发送音频超过已授予 credit")
            envelope = decode_envelope(raw)
            if envelope.kind != KIND_AUDIO:
                raise ValueError("只接受 KIND_AUDIO envelope")
            metadata = dict(envelope.metadata)
            duration_samples = self._validate_audio(transaction, metadata, envelope.payload)
            state.work_queue.put_nowait(WorkItem(kind="audio", metadata=metadata, payload=envelope.payload))
            transaction.available_credit -= 1
            transaction.next_audio_sequence += 1
            transaction.next_audio_pts += duration_samples
            transaction.received_bytes += len(envelope.payload)
            transaction.final_audio_seen = metadata["is_final"]
            state.wake_worker.set()
        except (ValueError, asyncio.QueueFull) as exc:
            await self._protocol_failure(state, transaction, str(exc))

    def _validate_audio(
        self,
        transaction: RenderTransaction,
        metadata: dict[str, Any],
        payload: bytes,
    ) -> int:
        if metadata.get("request_id") != transaction.request_id or metadata.get("audio_id") != transaction.audio_id:
            raise ValueError("音频帧 request_id/audio_id 不匹配")
        sequence = _strict_int(metadata.get("sequence"), "audio.sequence", minimum=0)
        pts_samples = _strict_int(metadata.get("pts_samples"), "audio.pts_samples", minimum=0)
        if sequence != transaction.next_audio_sequence or pts_samples != transaction.next_audio_pts:
            raise ValueError("音频帧 sequence/PTS 不连续")
        if _strict_int(metadata.get("audio_generation"), "audio_generation", minimum=0) != transaction.audio_generation:
            raise ValueError("audio_generation 不匹配")
        if (
            _strict_int(metadata.get("session_generation"), "session_generation", minimum=0)
            != transaction.session_generation
        ):
            raise ValueError("session_generation 不匹配")
        if _parse_audio_format(metadata.get("format")) != transaction.audio_format:
            raise ValueError("音频帧 format 与 render_open 不匹配")
        is_first = _strict_bool(metadata.get("is_first"), "is_first")
        is_final = _strict_bool(metadata.get("is_final"), "is_final")
        if is_first != (sequence == 0):
            raise ValueError("is_first 与 sequence 不一致")
        if is_final != (sequence == transaction.total_frames - 1):
            raise ValueError("is_final 与声明 total_frames 不一致")
        bytes_per_sample = transaction.audio_format.bytes_per_sample_frame
        if not payload or len(payload) % bytes_per_sample:
            raise ValueError("PCM payload 为空或未按样本边界对齐")
        duration_samples = len(payload) // bytes_per_sample
        expected_duration = (
            transaction.total_samples - pts_samples if is_final else transaction.frame_duration_samples
        )
        if duration_samples != expected_duration:
            raise ValueError("PCM duration 与 frame_duration_samples/total_samples 不一致")
        if transaction.received_bytes + len(payload) > self.config.max_transaction_bytes:
            raise ValueError("PCM 事务字节数超过 config 上限")
        return duration_samples

    async def _finish_transaction(self, message: dict[str, Any], state: ConnectionState) -> None:
        transaction = state.current
        request_id = message.get("request_id") if isinstance(message.get("request_id"), str) else None
        if transaction is None:
            await self._send_error(state, "没有活动事务可 finish", request_id=request_id)
            return
        try:
            if transaction.cancel_requested or transaction.finish_requested:
                raise ValueError("事务已 cancel 或 finish")
            if message.get("request_id") != transaction.request_id or message.get("audio_id") != transaction.audio_id:
                raise ValueError("render_finish request_id/audio_id 不匹配")
            final_sequence = _strict_int(message.get("final_sequence"), "final_sequence", minimum=0)
            total_samples = _strict_int(message.get("total_samples"), "total_samples", minimum=1)
            if final_sequence != transaction.total_frames - 1:
                raise ValueError("render_finish.final_sequence 与声明 totals 不一致")
            if total_samples != transaction.total_samples:
                raise ValueError("render_finish.total_samples 与 render_open 不一致")
            if transaction.next_audio_sequence != transaction.total_frames:
                raise ValueError("收到的音频帧数与声明 totals 不一致")
            if transaction.next_audio_pts != transaction.total_samples or not transaction.final_audio_seen:
                raise ValueError("收到的音频 sample totals 或 is_final 不完整")
        except ValueError as exc:
            await self._protocol_failure(state, transaction, str(exc))
            return
        transaction.finish_requested = True
        state.wake_worker.set()

    async def _cancel_transaction(self, message: dict[str, Any], state: ConnectionState) -> None:
        transaction = state.current
        request_id = message.get("request_id")
        if transaction is None:
            await self._send_error(state, "没有活动 session 可执行 cancel", request_id=request_id)
            return
        if not isinstance(request_id, str) or request_id != transaction.request_id:
            await self._send_error(state, "render_cancel.request_id 不匹配", request_id=request_id)
            return
        transaction.cancel_requested = True
        transaction.cancel_ack_requested = True
        transaction.cancel_signal.set()
        state.invalidated_epochs.add(transaction.epoch)
        reason = message.get("reason")
        transaction.cancel_reason = reason if isinstance(reason, str) and reason else "client cancel"
        state.wake_worker.set()
        if transaction.session is not None:
            await asyncio.shield(self._schedule_cancel(state, transaction))

    def _schedule_cancel(
        self,
        state: ConnectionState,
        transaction: RenderTransaction,
    ) -> asyncio.Task[None]:
        if transaction.cancel_task is None:
            transaction.cancel_task = asyncio.create_task(
                self._execute_cancel(state, transaction),
                name=f"gpu-sidecar-cancel-{transaction.epoch}",
            )
        return transaction.cancel_task

    async def _worker(self, state: ConnectionState) -> None:
        while True:
            await state.wake_worker.wait()
            if state.shutdown:
                await self._shutdown_transaction(state)
                return
            transaction = state.current
            if transaction is not None and transaction.cancel_requested and transaction.session is not None:
                await self._schedule_cancel(state, transaction)
                continue
            try:
                item = state.work_queue.get_nowait()
            except asyncio.QueueEmpty:
                if transaction is not None and transaction.finish_requested and transaction.session is not None:
                    await self._execute_finish(state, transaction)
                    continue
                state.wake_worker.clear()
                continue
            try:
                if item.kind == "open":
                    await self._execute_open(state)
                elif item.kind == "audio":
                    await self._execute_audio(state, item)
            finally:
                state.work_queue.task_done()

    async def _execute_open(self, state: ConnectionState) -> None:
        transaction = state.current
        if transaction is None:
            return
        try:
            transaction.session = await self.backend.open_session(transaction.request)
            if transaction.cancel_requested:
                return
            await self._send_json(
                state,
                {
                    "event": "render_accepted",
                    "request_id": transaction.request_id,
                    "initial_credit": self.config.initial_credit,
                    **self.backend.descriptor.evidence(),
                    "strict_totals": transaction.declared_totals(),
                    "resource_stats": await self._resource_stats(),
                },
            )
            transaction.available_credit = self.config.initial_credit
            transaction.accepted = True
        except Exception as exc:
            await self._model_failure(state, transaction, exc)

    async def _execute_audio(self, state: ConnectionState, item: WorkItem) -> None:
        transaction = state.current
        if transaction is None or transaction.session is None or transaction.cancel_requested:
            return
        try:
            results = self.backend.push_audio(
                transaction.session,
                pcm=item.payload,
                metadata=item.metadata,
                max_results=self.config.max_transaction_frames - transaction.rendered_frames,
                max_bytes=self.config.max_transaction_bytes - transaction.rendered_bytes,
                cancel_check=transaction.cancel_signal.is_set,
            )
            if transaction.cancel_requested or state.current is not transaction:
                return
            await self._emit_results(
                state,
                transaction,
                results,
                started_kind="audio",
                started_sequence=_strict_int(item.metadata.get("sequence"), "audio.sequence", minimum=0),
            )
            if transaction.cancel_requested or state.current is not transaction:
                return
            await self._send_json(
                state,
                {
                    "event": "render_credit",
                    "request_id": transaction.request_id,
                    "credit": 1,
                },
            )
            transaction.available_credit += 1
            if transaction.available_credit > self.config.initial_credit:
                raise RuntimeError("内部 credit ledger 越界")
        except Exception as exc:
            await self._model_failure(state, transaction, exc)

    async def _execute_finish(self, state: ConnectionState, transaction: RenderTransaction) -> None:
        try:
            session = transaction.session
            if session is None:
                raise RuntimeError("finish transaction 缺少 plugin session")
            results = self.backend.finish_session(
                session,
                max_results=self.config.max_transaction_frames - transaction.rendered_frames,
                max_bytes=self.config.max_transaction_bytes - transaction.rendered_bytes,
                cancel_check=transaction.cancel_signal.is_set,
            )
            if transaction.cancel_requested or state.current is not transaction:
                return
            await self._emit_results(
                state,
                transaction,
                results,
                started_kind="finish",
                started_sequence=transaction.next_audio_sequence,
            )
            if transaction.cancel_requested or state.current is not transaction:
                return
            await self._send_json(
                state,
                {
                    "event": "render_complete",
                    "request_id": transaction.request_id,
                    "audio_id": transaction.audio_id,
                    "rendered_frames": transaction.rendered_frames,
                    "last_pts_samples": transaction.total_samples,
                    **self.backend.descriptor.evidence(),
                    "strict_totals": transaction.completion_totals(),
                    "resource_stats": await self._resource_stats(),
                },
            )
            self._release_render_slot(transaction)
            state.current = None
        except Exception as exc:
            await self._model_failure(state, transaction, exc)

    async def _emit_results(
        self,
        state: ConnectionState,
        transaction: RenderTransaction,
        results: Any,
        *,
        started_kind: str,
        started_sequence: int,
    ) -> None:
        started_sent = False
        primary_error: BaseException | None = None
        try:
            async for result in results:
                if transaction.cancel_requested or state.current is not transaction:
                    return
                if isinstance(result, InferenceActiveMarker):
                    if started_sent:
                        raise RuntimeError("backend 重复产生 inference active marker")
                    await self._send_json(
                        state,
                        {
                            "event": "render_started",
                            "request_id": transaction.request_id,
                            "sequence": started_sequence,
                            "kind": started_kind,
                        },
                    )
                    started_sent = True
                    continue
                jpeg, pts_samples = _parse_plugin_result(result)
                if not jpeg.startswith(b"\xff\xd8") or not jpeg.endswith(b"\xff\xd9"):
                    raise ValueError("插件结果不是结构完整的 JPEG")
                if pts_samples < 0 or pts_samples >= transaction.total_samples:
                    raise ValueError("插件视频 PTS 超出音频事务范围")
                if pts_samples < transaction.last_video_pts:
                    raise ValueError("插件视频 PTS 非单调")
                if transaction.rendered_frames >= self.config.max_transaction_frames:
                    raise ValueError("插件视频帧数超过 config 上限")
                transaction.rendered_bytes += len(jpeg)
                if transaction.rendered_bytes > self.config.max_transaction_bytes:
                    raise ValueError("插件视频总字节数超过 config 上限")
                frame = SidecarVideoFrame(
                    request_id=transaction.request_id,
                    audio_id=transaction.audio_id,
                    sequence=transaction.next_video_sequence,
                    pts_samples=pts_samples,
                    audio_generation=transaction.audio_generation,
                    session_generation=transaction.session_generation,
                    jpeg=jpeg,
                )
                await self._queue_outbound(
                    state,
                    OutboundMessage(
                        payload=encode_video_frame(frame),
                        transaction_epoch=transaction.epoch,
                        is_video=True,
                    ),
                )
                transaction.next_video_sequence += 1
                transaction.rendered_frames += 1
                transaction.last_video_pts = pts_samples
        except BaseException as exc:
            primary_error = exc
            raise
        finally:
            close = getattr(results, "aclose", None)
            if callable(close):
                try:
                    await close()
                except BaseException as close_error:
                    if primary_error is None or isinstance(
                        close_error,
                        (BackendTimeoutError, BackendOOMError),
                    ):
                        raise
                    primary_error.add_note(f"backend iterator close also failed: {close_error!r}")

    async def _execute_cancel(self, state: ConnectionState, transaction: RenderTransaction) -> None:
        try:
            session = transaction.session
            if session is None:
                raise RuntimeError("cancel transaction 缺少 plugin session")
            await self.backend.cancel_session(session)
            if transaction.cancel_ack_requested:
                await self._send_json(
                    state,
                    {
                        "event": "render_cancelled",
                        "request_id": transaction.request_id,
                        "cancel_ack": True,
                        "quiesced": True,
                    },
                )
        except Exception as exc:
            timed_out = isinstance(exc, BackendTimeoutError)
            oom = isinstance(exc, BackendOOMError)
            await self._send_error(
                state,
                str(exc),
                request_id=transaction.request_id,
                code=(
                    "backend_timeout"
                    if timed_out
                    else "backend_oom"
                    if oom
                    else "cancel_failed"
                ),
                extra={
                    "supervisor_restart_required": timed_out or oom,
                    "supervisor_force_restart_required": timed_out or oom,
                },
            )
            if timed_out or oom:
                self._enter_fatal_unavailable()
        finally:
            self._drain_work_queue(state)
            self._release_render_slot(transaction)
            if state.current is transaction:
                state.current = None

    async def _shutdown_transaction(self, state: ConnectionState) -> None:
        transaction = state.current
        if transaction is not None and transaction.session is not None:
            try:
                await self.backend.cancel_session(transaction.session)
            except Exception:
                logger.debug("连接关闭时 session.cancel 失败", exc_info=True)
        self._drain_work_queue(state)
        if transaction is not None:
            self._release_render_slot(transaction)
        state.current = None

    async def _protocol_failure(
        self,
        state: ConnectionState,
        transaction: RenderTransaction,
        message: str,
    ) -> None:
        await self._send_error(state, message, request_id=transaction.request_id, code="protocol_error")
        transaction.cancel_requested = True
        transaction.cancel_reason = message
        transaction.cancel_signal.set()
        state.invalidated_epochs.add(transaction.epoch)
        state.wake_worker.set()
        if transaction.session is not None:
            await asyncio.shield(self._schedule_cancel(state, transaction))

    async def _model_failure(
        self,
        state: ConnectionState,
        transaction: RenderTransaction,
        exc: Exception,
    ) -> None:
        transaction.cancel_signal.set()
        state.invalidated_epochs.add(transaction.epoch)
        oom = isinstance(exc, BackendOOMError)
        timed_out = isinstance(exc, BackendTimeoutError)
        await self._send_error(
            state,
            str(exc),
            request_id=transaction.request_id,
            code="backend_timeout" if timed_out else "backend_oom" if oom else "backend_error",
            extra={
                "backend_available": self.backend.descriptor.available,
                "supervisor_restart_required": timed_out or oom,
                "supervisor_force_restart_required": timed_out or oom,
            },
        )
        if timed_out or oom:
            self._enter_fatal_unavailable()
        if transaction.session is not None:
            try:
                await self.backend.cancel_session(transaction.session)
            except Exception:
                logger.debug("模型失败后的 session.cancel 失败", exc_info=True)
        self._drain_work_queue(state)
        self._release_render_slot(transaction)
        if state.current is transaction:
            state.current = None

    def _enter_fatal_unavailable(self) -> None:
        """Quiesce every admitted connection after an unsafe timeout/OOM latch."""
        for active_state in tuple(self._connections.values()):
            active_state.shutdown = True
            active = active_state.current
            if active is not None:
                active.cancel_requested = True
                active.cancel_reason = "backend fatal unavailable; supervisor force restart required"
                active.cancel_signal.set()
                active_state.invalidated_epochs.add(active.epoch)
            active_state.wake_worker.set()

    def _release_render_slot(self, transaction: RenderTransaction) -> None:
        if transaction.render_slot_held:
            transaction.render_slot_held = False
            self._render_slots.release()

    @staticmethod
    def _drain_work_queue(state: ConnectionState) -> None:
        while True:
            try:
                state.work_queue.get_nowait()
            except asyncio.QueueEmpty:
                return
            else:
                state.work_queue.task_done()

    @staticmethod
    def _drain_outbound_queue(state: ConnectionState) -> None:
        while True:
            try:
                state.outbound.get_nowait()
            except asyncio.QueueEmpty:
                return
            else:
                state.outbound.task_done()

    async def _best_effort_cancel_session(self, session: PluginSession) -> None:
        try:
            await asyncio.wait_for(
                self.backend.cancel_session(session),
                timeout=self.config.cancel_timeout_seconds + 0.1,
            )
        except Exception:
            logger.debug("bounded session.cancel 未完成", exc_info=True)

    async def _resource_stats(self) -> dict[str, Any]:
        try:
            return await asyncio.to_thread(self.backend.resource_snapshot)
        except Exception as exc:
            return {"unavailable": {"resource_stats": f"snapshot unavailable: {exc}"}}

    async def _queue_outbound(
        self,
        state: ConnectionState,
        message: OutboundMessage,
    ) -> None:
        if state.shutdown:
            raise ConnectionError("connection is shutting down")
        try:
            await asyncio.wait_for(
                state.outbound.put(message),
                timeout=_OUTBOUND_PUT_TIMEOUT_SECONDS,
            )
        except TimeoutError as exc:
            state.shutdown = True
            state.wake_worker.set()
            raise RuntimeError("outbound queue blocked past bounded timeout") from exc

    async def _send_json(self, state: ConnectionState, message: Mapping[str, Any]) -> None:
        payload = json.dumps(dict(message), ensure_ascii=False, separators=(",", ":"))
        await self._queue_outbound(state, OutboundMessage(payload=payload))

    async def _send_error(
        self,
        state: ConnectionState,
        message: str,
        *,
        request_id: Any = None,
        code: str = "invalid_request",
        extra: Mapping[str, Any] | None = None,
    ) -> None:
        response: dict[str, Any] = {
            "event": "error",
            "code": code,
            "message": message,
            "resource_stats": await self._resource_stats(),
        }
        if isinstance(request_id, str) and request_id:
            response["request_id"] = request_id
        if extra:
            response.update(extra)
        await self._send_json(state, response)


async def serve(
    *,
    backend: Wav2LipBackend,
    config: SidecarConfig,
    host: str,
    port: int,
    token: str,
    tls_cert: str = "",
    tls_key: str = "",
    trusted_proxy: bool = False,
) -> None:
    """启动已通过门禁的 backend；远程监听必须原生 TLS 且使用 token。"""
    descriptor = backend.descriptor
    loopback = _is_loopback_host(host)
    if bool(tls_cert) != bool(tls_key):
        raise ValueError("--tls-cert/--tls-key 必须成对提供")
    if trusted_proxy and not loopback:
        raise ValueError("--trusted-proxy 仅允许 loopback bind，禁止绕过远程 TLS")
    if not loopback and not tls_cert:
        raise ValueError("非 loopback bind 必须配置 --tls-cert/--tls-key")
    if not loopback and not token:
        raise ValueError("非 loopback host 必须提供非空 --token")
    ssl_context: ssl.SSLContext | None = None
    if tls_cert:
        ssl_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ssl_context.minimum_version = ssl.TLSVersion.TLSv1_2
        ssl_context.load_cert_chain(certfile=tls_cert, keyfile=tls_key)
    if not (
        descriptor.available
        and descriptor.neural
        and descriptor.warmed
        and descriptor.license_approved
    ):
        raise BackendUnavailableError(descriptor.unavailable_reason or "backend 尚未通过启动门禁")
    try:
        import websockets
    except ImportError as exc:
        raise RuntimeError("缺少 websockets 依赖；sidecar 不会自动安装依赖") from exc
    server = Wav2LipSidecarServer(backend=backend, config=config, token=token)
    async with websockets.serve(
        server.handle,
        host,
        port,
        max_size=16 * 1024 * 1024,
        ssl=ssl_context,
    ):
        scheme = "wss" if ssl_context is not None else "ws"
        proxy_marker = " trusted-proxy TLS termination" if trusted_proxy else ""
        logger.info(
            "授权 Wav2Lip 插件 sidecar: %s://%s:%s/ws/render-v3%s",
            scheme,
            host,
            port,
            proxy_marker,
        )
        await asyncio.Future()


def _is_loopback_host(host: str) -> bool:
    normalized = host.strip().lower()
    if normalized == "localhost":
        return True
    try:
        return ipaddress.ip_address(normalized).is_loopback
    except ValueError:
        return False


def _parse_audio_format(value: Any) -> AudioFormat:
    if not isinstance(value, dict):
        raise ValueError("format 必须是 object")
    codec = value.get("codec")
    if codec != "pcm_s16le":
        raise ValueError("format.codec 必须为 pcm_s16le")
    return AudioFormat(
        codec=codec,
        sample_rate=_strict_int(value.get("sample_rate"), "format.sample_rate", minimum=1),
        channels=_strict_int(value.get("channels"), "format.channels", minimum=1),
        sample_width_bytes=_strict_int(value.get("sample_width_bytes"), "format.sample_width_bytes", minimum=1),
    )


def _parse_plugin_result(result: Any) -> tuple[bytes, int]:
    jpeg: Any
    pts_samples: Any
    if isinstance(result, PluginFrameResult):
        jpeg = result.jpeg
        pts_samples = result.pts_samples
    elif isinstance(result, Mapping):
        jpeg = result.get("jpeg")
        pts_samples = result.get("pts_samples")
    else:
        jpeg = getattr(result, "jpeg", None)
        pts_samples = getattr(result, "pts_samples", None)
    if not isinstance(jpeg, bytes) or not jpeg:
        raise ValueError("插件结果 jpeg 必须是非空 bytes")
    return jpeg, _strict_int(pts_samples, "plugin.pts_samples", minimum=0)


def _strict_int(value: Any, field: str, *, minimum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValueError(f"{field} 必须是大于等于 {minimum} 的整数")
    return value


def _strict_bool(value: Any, field: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"{field} 必须是 boolean")
    return value


def _bounded_text(value: Any, field: str, *, maximum_bytes: int = 128) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} 必须是非空字符串")
    text = value.strip()
    if len(text.encode("utf-8")) > maximum_bytes:
        raise ValueError(f"{field} 超过 {maximum_bytes} UTF-8 bytes")
    return text
