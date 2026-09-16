"""LiveAvatar LITE 实验适配器。

只实现官方 HTTPS/WebSocket 控制面。LiveKit 视频轨尚未接入本地 Router，
因此该 Provider 必须保持 sandbox-only、UNVERIFIED，且不实现 AvatarPreviewProvider。
"""

from __future__ import annotations

import asyncio
import base64
import json
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Mapping, Sequence
from urllib.parse import urlparse

import httpx

from server.adapters.media.avatar_provider import (
    AvatarProviderCapabilities,
    AvatarRenderMode,
    ProviderError,
    ProviderErrorCode,
    ProviderRenderResult,
    ProviderVerification,
)
from server.core.media.audio_frame import AudioFrame, validate_audio_frame_batch

try:
    import websockets
except ImportError:  # pragma: no cover - 项目依赖缺失时由 start 给出稳定错误
    websockets = None

WsConnector = Callable[[str, float], Awaitable[Any]]


@dataclass(slots=True)
class _UtteranceState:
    request_id: str
    audio_id: str
    expected_appends: int
    appended_event_ids: set[str] = field(default_factory=set)
    all_appended: asyncio.Event = field(default_factory=asyncio.Event)
    committed: asyncio.Event = field(default_factory=asyncio.Event)
    started: asyncio.Event = field(default_factory=asyncio.Event)
    terminal: asyncio.Event = field(default_factory=asyncio.Event)
    interrupt_cleared: asyncio.Event = field(default_factory=asyncio.Event)
    interrupt_id: str = ""
    terminal_kind: str = ""
    error: ProviderError | None = None


class LiveAvatarLiteAvatarProvider:
    """LITE renderer-only 控制面；普通直播 factory 禁止构造该实验实例。"""

    MAX_WS_MESSAGE_BYTES = 1024 * 1024
    MAX_PCM_CHUNK_BYTES = 700_000

    def __init__(
        self,
        *,
        provider_id: str,
        display_name: str,
        api_base_url: str,
        api_key: str,
        avatar_id: str,
        sandbox_only: bool = True,
        max_session_duration: int = 60,
        keep_alive_seconds: float = 30.0,
        connect_timeout: float = 10.0,
        message_timeout: float = 20.0,
        request_timeout: float = 120.0,
        http_client: httpx.AsyncClient | None = None,
        ws_connector: WsConnector | None = None,
    ) -> None:
        provider_id = str(provider_id).strip()
        if not str(api_key).strip():
            raise ValueError("LiveAvatar API Key 不能为空")
        if not str(avatar_id).strip():
            raise ValueError("avatar_id 不能为空")
        if min(connect_timeout, message_timeout, request_timeout) <= 0:
            raise ValueError("Provider timeout 必须为正数")

        self._provider_id = provider_id
        self._display_name = str(display_name).strip() or provider_id
        self.api_base_url = str(api_base_url).rstrip("/")
        self._api_key = str(api_key)
        self.avatar_id = str(avatar_id).strip()
        self.sandbox_only = bool(sandbox_only)
        self.max_session_duration = int(max_session_duration)
        self.keep_alive_seconds = float(keep_alive_seconds)
        self.connect_timeout = float(connect_timeout)
        self.message_timeout = float(message_timeout)
        self.request_timeout = float(request_timeout)
        self._http = http_client
        self._owns_http = http_client is None
        self._ws_connector = ws_connector or self._default_ws_connector

        self._lifecycle_lock = asyncio.Lock()
        self._render_lock = asyncio.Lock()
        self._send_lock = asyncio.Lock()
        self._ws: Any = None
        self._reader_task: asyncio.Task | None = None
        self._keep_alive_task: asyncio.Task | None = None
        self._connected_event = asyncio.Event()
        self._active: _UtteranceState | None = None
        self._session_token = ""
        self._session_id = ""
        self._livekit_url = ""
        self._livekit_client_token = ""
        self._ws_url = ""
        self._state = "stopped"
        self._last_error: ProviderError | None = None
        self._warnings_total = 0
        self._transactions_completed = 0
        self._transactions_interrupted = 0
        self._connection_epoch = 0

        self._capabilities = AvatarProviderCapabilities(
            render_modes=frozenset({AvatarRenderMode.REALTIME}),
            input_codecs=frozenset({"pcm_s16le"}),
            renderer_only=True,
            neural_lipsync=True,
            transactional_sentence=True,
            supports_cancel=True,
            # 官方有 cleared/interrupted 事件，但本地尚未验证视频轨静止边界。
            supports_cancel_ack=False,
            supports_credit=False,
            supports_render_started=True,
            supports_sample_pts=False,
            strict_completion=True,
            protocol_name="liveavatar_lite_events",
            protocol_version=1,
            verification=ProviderVerification.UNVERIFIED,
            extra={
                "adapter": "liveavatar_lite",
                "sandbox_only": True,
                "experimental": True,
                "media_transport": "livekit",
                "local_video_output": False,
                "max_safe_concurrency": 1,
            },
        )

    @property
    def provider_id(self) -> str:
        return self._provider_id

    @property
    def display_name(self) -> str:
        return self._display_name

    @property
    def play_stream_addr(self) -> str:
        """获取 LiveAvatar 视频房间/流地址。"""
        return self._livekit_url or self._ws_url

    @property
    def capabilities(self) -> AvatarProviderCapabilities:
        return self._capabilities

    @staticmethod
    async def _default_ws_connector(url: str, timeout: float):
        if websockets is None:
            raise ProviderError(
                ProviderErrorCode.REMOTE_UNAVAILABLE,
                "缺少 websockets 依赖，无法连接 LiveAvatar 控制通道",
            )
        return await websockets.connect(
            url,
            open_timeout=timeout,
            close_timeout=min(timeout, 5.0),
            max_size=LiveAvatarLiteAvatarProvider.MAX_WS_MESSAGE_BYTES,
        )

    def _provider_error(
        self,
        code: ProviderErrorCode,
        message: str,
        *,
        retryable: bool = False,
        restart_required: bool = False,
        request_id: str = "",
        details: Mapping[str, Any] | None = None,
    ) -> ProviderError:
        return ProviderError(
            code,
            message,
            provider_id=self.provider_id,
            retryable=retryable,
            restart_required=restart_required,
            request_id=request_id,
            details=details,
        )

    @staticmethod
    def _http_error_code(status: int) -> tuple[ProviderErrorCode, bool]:
        if status in {401, 403}:
            return ProviderErrorCode.AUTHENTICATION, False
        if status in {400, 404, 422}:
            return ProviderErrorCode.INVALID_REQUEST, False
        if status == 409:
            return ProviderErrorCode.CONCURRENCY_LIMIT, True
        if status == 429:
            return ProviderErrorCode.RATE_LIMITED, True
        if status >= 500:
            return ProviderErrorCode.REMOTE_UNAVAILABLE, True
        return ProviderErrorCode.REMOTE_UNAVAILABLE, False

    async def _request_json(
        self,
        method: str,
        path: str,
        *,
        headers: Mapping[str, str],
        payload: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        if self._http is None:
            self._http = httpx.AsyncClient(
                timeout=httpx.Timeout(self.connect_timeout),
                follow_redirects=False,
            )
        try:
            response = await self._http.request(
                method,
                f"{self.api_base_url}{path}",
                headers=dict(headers),
                json=dict(payload) if payload is not None else None,
            )
        except httpx.TimeoutException as exc:
            raise self._provider_error(
                ProviderErrorCode.CONNECT_TIMEOUT,
                "LiveAvatar API 请求超时",
                retryable=True,
            ) from exc
        except httpx.HTTPError as exc:
            raise self._provider_error(
                ProviderErrorCode.REMOTE_UNAVAILABLE,
                f"LiveAvatar API 连接失败：{type(exc).__name__}",
                retryable=True,
            ) from exc

        if not 200 <= response.status_code < 300:
            code, retryable = self._http_error_code(response.status_code)
            raise self._provider_error(
                code,
                f"LiveAvatar API 返回 HTTP {response.status_code}",
                retryable=retryable,
                details={"http_status": response.status_code},
            )
        try:
            body = response.json()
        except ValueError as exc:
            raise self._provider_error(
                ProviderErrorCode.PROTOCOL_VIOLATION,
                "LiveAvatar API 返回了非 JSON 响应",
                restart_required=True,
            ) from exc
        if not isinstance(body, dict):
            raise self._provider_error(
                ProviderErrorCode.PROTOCOL_VIOLATION,
                "LiveAvatar API 响应必须是 object",
                restart_required=True,
            )
        code_value = body.get("code")
        if code_value is not None and code_value != 1000:
            raise self._provider_error(
                ProviderErrorCode.REMOTE_UNAVAILABLE,
                f"LiveAvatar API 业务状态异常 code={code_value!r}",
                retryable=True,
            )
        data = body.get("data", body)
        if not isinstance(data, dict):
            raise self._provider_error(
                ProviderErrorCode.PROTOCOL_VIOLATION,
                "LiveAvatar API data 必须是 object",
                restart_required=True,
            )
        return data

    async def start(self) -> None:
        async with self._lifecycle_lock:
            if self._state == "ready":
                return
            if self._state not in {"stopped", "failed"}:
                raise self._provider_error(
                    ProviderErrorCode.CONCURRENCY_LIMIT,
                    f"LiveAvatar session 当前状态不允许启动：{self._state}",
                )
            self._state = "creating"
            self._last_error = None
            self._connected_event = asyncio.Event()
            self._connection_epoch += 1
            try:
                token_data = await self._request_json(
                    "POST",
                    "/v1/sessions/token",
                    headers={
                        "X-API-KEY": self._api_key,
                        "Content-Type": "application/json",
                    },
                    payload={
                        "mode": "LITE",
                        "avatar_id": self.avatar_id,
                        "is_sandbox": True,
                        "max_session_duration": self.max_session_duration,
                    },
                )
                self._session_token = str(token_data.get("session_token") or "")
                self._session_id = str(token_data.get("session_id") or "")
                if not self._session_token or not self._session_id:
                    raise self._provider_error(
                        ProviderErrorCode.PROTOCOL_VIOLATION,
                        "LiveAvatar token 响应缺少 session_token 或 session_id",
                        restart_required=True,
                    )

                self._state = "starting"
                session_data = await self._request_json(
                    "POST",
                    "/v1/sessions/start",
                    headers={
                        "Authorization": f"Bearer {self._session_token}",
                        "Content-Type": "application/json",
                    },
                )
                response_session_id = str(session_data.get("session_id") or "")
                if response_session_id and response_session_id != self._session_id:
                    raise self._provider_error(
                        ProviderErrorCode.PROTOCOL_VIOLATION,
                        "LiveAvatar start 响应 session_id 不匹配",
                        restart_required=True,
                    )
                self._ws_url = str(session_data.get("ws_url") or "")
                self._livekit_url = str(session_data.get("livekit_url") or "")
                self._livekit_client_token = str(
                    session_data.get("livekit_client_token") or ""
                )
                parsed_ws = urlparse(self._ws_url)
                if parsed_ws.scheme != "wss" or not parsed_ws.hostname:
                    raise self._provider_error(
                        ProviderErrorCode.PROTOCOL_VIOLATION,
                        "LiveAvatar start 响应缺少合法 wss:// ws_url",
                        restart_required=True,
                    )

                self._state = "connecting_ws"
                self._ws = await self._ws_connector(
                    self._ws_url,
                    self.connect_timeout,
                )
                self._reader_task = asyncio.create_task(self._reader_loop())
                try:
                    await asyncio.wait_for(
                        self._connected_event.wait(),
                        timeout=self.connect_timeout,
                    )
                except TimeoutError as exc:
                    raise self._provider_error(
                        ProviderErrorCode.CONNECT_TIMEOUT,
                        "LiveAvatar WebSocket 未进入 connected 状态",
                        retryable=True,
                        restart_required=True,
                    ) from exc
                if self._state == "failed" or self._last_error is not None:
                    raise self._last_error or self._provider_error(
                        ProviderErrorCode.REMOTE_UNAVAILABLE,
                        "LiveAvatar WebSocket 在握手期间断开",
                        retryable=True,
                        restart_required=True,
                    )
                self._state = "ready"
                self._keep_alive_task = asyncio.create_task(self._keep_alive_loop())
            except Exception as exc:
                error = exc if isinstance(exc, ProviderError) else self._provider_error(
                    ProviderErrorCode.REMOTE_UNAVAILABLE,
                    f"LiveAvatar 启动失败：{type(exc).__name__}",
                    retryable=True,
                )
                self._last_error = error
                self._state = "failed"
                await self._cleanup_locked(send_stop=True)
                raise error

    async def _send_event(self, event_type: str, event_id: str, payload: dict | None = None) -> None:
        if self._ws is None:
            raise self._provider_error(
                ProviderErrorCode.REMOTE_UNAVAILABLE,
                "LiveAvatar WebSocket 未连接",
                retryable=True,
            )
        message = {"type": event_type, "event_id": event_id}
        if payload:
            message.update(payload)
        encoded = json.dumps(message, separators=(",", ":"))
        if len(encoded.encode("utf-8")) > self.MAX_WS_MESSAGE_BYTES:
            raise self._provider_error(
                ProviderErrorCode.INVALID_REQUEST,
                "LiveAvatar WebSocket 消息超过 1MB",
                request_id=event_id,
            )
        async with self._send_lock:
            await self._ws.send(encoded)

    async def _reader_loop(self) -> None:
        try:
            async for raw in self._ws:
                if not isinstance(raw, str):
                    raise self._provider_error(
                        ProviderErrorCode.PROTOCOL_VIOLATION,
                        "LiveAvatar 控制通道只允许 JSON text frame",
                        restart_required=True,
                    )
                self._handle_message(raw)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            error = exc if isinstance(exc, ProviderError) else self._provider_error(
                ProviderErrorCode.REMOTE_UNAVAILABLE,
                f"LiveAvatar WebSocket 已断开：{type(exc).__name__}",
                retryable=True,
                restart_required=True,
            )
            self._last_error = error
            self._state = "failed"
            self._connected_event.set()
            active = self._active
            if active is not None:
                active.error = error
                active.all_appended.set()
                active.committed.set()
                active.started.set()
                active.terminal.set()
                active.interrupt_cleared.set()

    def _handle_message(self, raw: str) -> None:
        try:
            message = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise self._provider_error(
                ProviderErrorCode.PROTOCOL_VIOLATION,
                "LiveAvatar WebSocket 返回无效 JSON",
                restart_required=True,
            ) from exc
        if not isinstance(message, dict):
            raise self._provider_error(
                ProviderErrorCode.PROTOCOL_VIOLATION,
                "LiveAvatar WebSocket 消息必须是 object",
                restart_required=True,
            )
        event_type = str(message.get("type") or "")
        source_event_id = str(message.get("source_event_id") or "")
        payload = message.get("payload")
        payload = payload if isinstance(payload, dict) else message

        if event_type == "session.state_updated":
            state = str(payload.get("state") or "")
            if state == "connected":
                self._connected_event.set()
            elif state == "disconnected":
                raise self._provider_error(
                    ProviderErrorCode.REMOTE_UNAVAILABLE,
                    "LiveAvatar session 已断开",
                    retryable=True,
                    restart_required=True,
                )
            return
        if event_type == "warning":
            self._warnings_total += 1
            return
        if event_type == "error":
            error_payload = payload.get("error")
            error_payload = error_payload if isinstance(error_payload, dict) else {}
            wire_type = str(error_payload.get("type") or "server_error")
            code = (
                ProviderErrorCode.INVALID_REQUEST
                if wire_type == "invalid_request_error"
                else ProviderErrorCode.REMOTE_UNAVAILABLE
            )
            error = self._provider_error(
                code,
                f"LiveAvatar WebSocket error: {wire_type}",
                retryable=wire_type != "invalid_request_error",
                request_id=source_event_id,
                details={"wire_type": wire_type},
            )
            active = self._active
            if active is not None and (
                source_event_id in {active.request_id, active.interrupt_id}
                or not source_event_id
            ):
                active.error = error
                active.all_appended.set()
                active.committed.set()
                active.started.set()
                active.terminal.set()
                active.interrupt_cleared.set()
            self._last_error = error
            return

        active = self._active
        if active is None:
            return
        if event_type == "agent.audio_buffer_appended" and source_event_id == active.request_id:
            response_id = str(message.get("event_id") or uuid.uuid4().hex)
            active.appended_event_ids.add(response_id)
            if len(active.appended_event_ids) >= active.expected_appends:
                active.all_appended.set()
        elif event_type == "agent.audio_buffer_committed" and source_event_id == active.request_id:
            active.committed.set()
        elif event_type == "agent.speak_started" and source_event_id == active.request_id:
            active.started.set()
        elif event_type == "agent.speak_ended" and source_event_id == active.request_id:
            active.terminal_kind = "ended"
            active.terminal.set()
        elif event_type == "agent.speak_interrupted" and source_event_id == active.request_id:
            active.terminal_kind = "interrupted"
            active.terminal.set()
        elif (
            event_type == "agent.audio_buffer_cleared"
            and active.interrupt_id
            and source_event_id == active.interrupt_id
        ):
            active.interrupt_cleared.set()

    async def _wait_event(
        self,
        event: asyncio.Event,
        active: _UtteranceState,
        deadline: float,
        label: str,
    ) -> None:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise self._provider_error(
                ProviderErrorCode.REQUEST_TIMEOUT,
                f"LiveAvatar 等待 {label} 超过整句截止时间",
                retryable=True,
                restart_required=True,
                request_id=active.request_id,
            )
        try:
            await asyncio.wait_for(event.wait(), timeout=min(remaining, self.message_timeout))
        except TimeoutError as exc:
            code = (
                ProviderErrorCode.REQUEST_TIMEOUT
                if remaining <= self.message_timeout
                else ProviderErrorCode.MESSAGE_TIMEOUT
            )
            raise self._provider_error(
                code,
                f"LiveAvatar 等待 {label} 超时",
                retryable=True,
                restart_required=True,
                request_id=active.request_id,
            ) from exc
        if active.error is not None:
            raise active.error

    async def render_sentence(
        self,
        frames: Sequence[AudioFrame],
        *,
        render_mode: AvatarRenderMode,
    ) -> ProviderRenderResult:
        if render_mode is not AvatarRenderMode.REALTIME:
            raise self._provider_error(
                ProviderErrorCode.CAPABILITY_MISMATCH,
                "LiveAvatar LITE 只支持 realtime 模式",
            )
        frame_batch = validate_audio_frame_batch(frames)
        first = frame_batch[0]
        if first.format.sample_rate != 24_000 or first.format.channels != 1:
            raise self._provider_error(
                ProviderErrorCode.CAPABILITY_MISMATCH,
                "LiveAvatar LITE 要求 PCM16 24kHz mono",
            )
        if any(len(frame.data) > self.MAX_PCM_CHUNK_BYTES for frame in frame_batch):
            raise self._provider_error(
                ProviderErrorCode.INVALID_REQUEST,
                "LiveAvatar 单个 PCM chunk 过大",
            )

        async with self._render_lock:
            if self._state != "ready" or self._ws is None:
                raise self._provider_error(
                    ProviderErrorCode.REMOTE_UNAVAILABLE,
                    "LiveAvatar sandbox session 尚未就绪",
                    retryable=True,
                )
            request_id = str(uuid.uuid4())
            active = _UtteranceState(
                request_id=request_id,
                audio_id=first.audio_id,
                expected_appends=len(frame_batch),
            )
            self._active = active
            started_at = time.monotonic()
            deadline = started_at + self.request_timeout
            try:
                for frame in frame_batch:
                    await self._send_event(
                        "agent.speak",
                        request_id,
                        {"audio": base64.b64encode(frame.data).decode("ascii")},
                    )
                await self._send_event("agent.speak_end", request_id)
                await self._wait_event(active.all_appended, active, deadline, "audio_buffer_appended")
                await self._wait_event(active.committed, active, deadline, "audio_buffer_committed")
                await self._wait_event(active.started, active, deadline, "speak_started")
                await self._wait_event(active.terminal, active, deadline, "speak_ended")
                if active.terminal_kind != "ended":
                    raise self._provider_error(
                        ProviderErrorCode.STALE_GENERATION,
                        "LiveAvatar utterance 已被中断",
                        request_id=request_id,
                    )
                self._transactions_completed += 1
                self._last_error = None
                return ProviderRenderResult(
                    provider_id=self.provider_id,
                    request_id=request_id,
                    audio_id=first.audio_id,
                    render_mode=render_mode,
                    latency_ms=(time.monotonic() - started_at) * 1000.0,
                    output_frames=0,
                    billed_units=0,
                    cost_minor=0,
                    evidence={
                        "adapter": "liveavatar_lite",
                        "sandbox_only": True,
                        "completion_scope": "remote_control_terminal",
                        "video_observed": False,
                        "sample_pts_observed": False,
                        "media_delivery": "livekit_external_unconsumed",
                        "billing_evidence": "unverified",
                    },
                )
            except ProviderError as exc:
                self._last_error = exc
                if exc.restart_required:
                    await self._close_ws()
                    self._state = "failed"
                raise
            except Exception as exc:
                error = self._provider_error(
                    ProviderErrorCode.REMOTE_UNAVAILABLE,
                    f"LiveAvatar 发送失败：{type(exc).__name__}",
                    retryable=True,
                    restart_required=True,
                    request_id=request_id,
                )
                self._last_error = error
                raise error from exc
            finally:
                if self._active is active:
                    self._active = None

    async def interrupt(
        self,
        reason: str = "Barge-in",
        *,
        next_generation: int | None = None,
    ) -> None:
        del reason, next_generation
        active = self._active
        if active is None:
            return
        interrupt_id = str(uuid.uuid4())
        active.interrupt_id = interrupt_id
        deadline = time.monotonic() + min(self.request_timeout, self.message_timeout * 2)
        try:
            await self._send_event("agent.interrupt", interrupt_id)
            await self._wait_event(
                active.interrupt_cleared,
                active,
                deadline,
                "audio_buffer_cleared",
            )
            await self._wait_event(
                active.terminal,
                active,
                deadline,
                "speak_interrupted",
            )
            if active.terminal_kind != "interrupted":
                raise self._provider_error(
                    ProviderErrorCode.CANCEL_UNCONFIRMED,
                    "LiveAvatar 未返回当前 utterance 的 speak_interrupted",
                    restart_required=True,
                    request_id=active.request_id,
                )
            self._transactions_interrupted += 1
        except Exception as exc:
            error = exc if isinstance(exc, ProviderError) else self._provider_error(
                ProviderErrorCode.CANCEL_UNCONFIRMED,
                f"LiveAvatar 中断未确认：{type(exc).__name__}",
                restart_required=True,
                request_id=active.request_id,
            )
            if error.code is not ProviderErrorCode.CANCEL_UNCONFIRMED:
                error = self._provider_error(
                    ProviderErrorCode.CANCEL_UNCONFIRMED,
                    "LiveAvatar 未同时确认 buffer cleared 与 utterance interrupted",
                    restart_required=True,
                    request_id=active.request_id,
                    details={"cause": error.code.value},
                )
            self._last_error = error
            await self._close_ws()
            self._state = "failed"
            raise error

    async def _keep_alive_loop(self) -> None:
        try:
            while self._state == "ready":
                await asyncio.sleep(self.keep_alive_seconds)
                if self._state != "ready":
                    return
                await self._send_event("session.keep_alive", str(uuid.uuid4()))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._last_error = self._provider_error(
                ProviderErrorCode.REMOTE_UNAVAILABLE,
                f"LiveAvatar keep-alive 失败：{type(exc).__name__}",
                retryable=True,
                restart_required=True,
            )
            self._state = "failed"

    async def _close_ws(self) -> None:
        ws = self._ws
        self._ws = None
        if ws is not None:
            try:
                await ws.close()
            except Exception:
                pass
        reader = self._reader_task
        self._reader_task = None
        if reader is not None and reader is not asyncio.current_task():
            reader.cancel()
            await asyncio.gather(reader, return_exceptions=True)

    async def _cleanup_locked(self, *, send_stop: bool) -> None:
        keep_alive = self._keep_alive_task
        self._keep_alive_task = None
        if keep_alive is not None:
            keep_alive.cancel()
            await asyncio.gather(keep_alive, return_exceptions=True)
        await self._close_ws()
        if send_stop and self._session_token:
            try:
                await self._request_json(
                    "POST",
                    "/v1/sessions/stop",
                    headers={
                        "Authorization": f"Bearer {self._session_token}",
                        "Content-Type": "application/json",
                    },
                )
            except Exception:
                pass
        self._active = None
        self._session_token = ""
        self._session_id = ""
        self._livekit_url = ""
        self._livekit_client_token = ""
        self._ws_url = ""
        if self._owns_http and self._http is not None:
            await self._http.aclose()
            self._http = None

    async def stop(self) -> None:
        async with self._lifecycle_lock:
            if self._state == "stopped" and not self._session_token:
                return
            self._state = "stopping"
            active = self._active
            if active is not None:
                try:
                    await self.interrupt("Provider stop")
                except Exception:
                    pass
            await self._cleanup_locked(send_stop=True)
            self._state = "stopped"

    def get_status(self) -> Mapping[str, Any]:
        error = self._last_error
        return {
            "configured": True,
            "adapter": "liveavatar_lite",
            "sandbox_only": True,
            "experimental": True,
            "verification": ProviderVerification.UNVERIFIED.value,
            "session_state": self._state,
            "control_ready": self._state == "ready",
            "speech_state": "active" if self._active is not None else "idle",
            "livekit_metadata_received": bool(
                self._livekit_url and self._livekit_client_token
            ),
            "media_transport": "livekit",
            "local_video_consumable": False,
            "local_preview": False,
            "video_track_observed": False,
            "sample_pts_observed": False,
            "media_boundary_validated": False,
            "transactions_completed": self._transactions_completed,
            "transactions_interrupted": self._transactions_interrupted,
            "warnings_total": self._warnings_total,
            "connection_epoch": self._connection_epoch,
            "last_error": error.to_dict() if error is not None else None,
        }
