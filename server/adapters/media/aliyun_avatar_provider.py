"""阿里云万相数字人实验 Provider（本地 WebSDK bridge 客户端）。

bridge wire 是本项目自有 IPC，不是阿里云官方协议。公开 WebSDK 没有提供
request-correlated 完成、取消静止或视频 sample PTS，因此本适配器只能用于 sandbox。
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Literal, Mapping, Sequence
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from server.adapters.media.avatar_provider import (
    AvatarProviderCapabilities,
    AvatarRenderMode,
    ProviderError,
    ProviderErrorCode,
    ProviderRenderResult,
    ProviderVerification,
)
from server.core.media.aliyun_websdk_bridge_protocol import (
    MAX_CONTROL_BYTES,
    PROTOCOL_NAME,
    PROTOCOL_VERSION,
    build_hello_message,
    encode_audio_frame,
    validate_hello_reply,
    validate_session_ready,
)
from server.core.media.audio_frame import AudioFormat, AudioFrame, validate_audio_frame_batch

try:
    import websockets
except ImportError:  # pragma: no cover
    websockets = None  # type: ignore[assignment]

WsConnector = Callable[[str, float], Awaitable[Any]]


class _StrictCredential(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class _RtcInitConfig(_StrictCredential):
    app_id: str = Field(min_length=1, max_length=256)
    channel: str = Field(min_length=1, max_length=256)
    timestamp: int = Field(gt=0)
    token: str = Field(min_length=1, max_length=8192)
    nonce: str = Field(default="", max_length=1024)
    client_user_id: str = Field(min_length=1, max_length=256)
    client_user_name: str = Field(default="", max_length=256)
    server_user_id: str = Field(min_length=1, max_length=256)
    avatar_user_id: str = Field(min_length=1, max_length=256)
    session_id: str = Field(min_length=1, max_length=256)


class AliyunCredentialBundleV1(_StrictCredential):
    """加密字段内的短期 RTC 初始化材料；禁止写入 extra_params。"""

    version: Literal[1]
    bridge_auth_token: str = Field(min_length=16, max_length=4096)
    rtc: _RtcInitConfig


def parse_aliyun_credential_bundle(raw: str) -> AliyunCredentialBundleV1:
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError("阿里云 credential bundle 必须是 JSON") from exc
    try:
        return AliyunCredentialBundleV1.model_validate(payload, strict=True)
    except ValidationError as exc:
        first = exc.errors(include_url=False)[0]
        path = ".".join(str(part) for part in first.get("loc", ())) or "credential"
        raise ValueError(f"阿里云 credential bundle 无效：{path}: {first.get('msg')}") from exc


@dataclass(slots=True)
class _ActiveDiagnostic:
    request_id: str
    audio_id: str
    input_accepted: asyncio.Event = field(default_factory=asyncio.Event)
    interrupt_invoked: asyncio.Event = field(default_factory=asyncio.Event)
    first_frame_callback_observed: bool = False
    observed_states: list[str] = field(default_factory=list)
    interrupt_id: str = ""
    error: ProviderError | None = None


class AliyunAvatarProvider:
    """调用本地 lm-avatar-chat-sdk bridge 的 fail-closed 实验适配器。"""

    def __init__(
        self,
        *,
        provider_id: str,
        display_name: str,
        bridge_url: str,
        credential_bundle: str,
        expected_sdk_sha256: str,
        sandbox_only: bool = True,
        connect_timeout: float = 10.0,
        message_timeout: float = 20.0,
        request_timeout: float = 120.0,
        ws_connector: WsConnector | None = None,
    ) -> None:
        provider_id = str(provider_id).strip()
        if not provider_id:
            raise ValueError("provider_id 不能为空")
        self._sandbox_only = bool(sandbox_only)
        self._play_stream_addr = ""
        parsed = urlparse(str(bridge_url))
        if (
            parsed.scheme != "wss"
            or (parsed.hostname or "").lower() not in {"localhost", "127.0.0.1", "::1"}
            or parsed.username
            or parsed.password
        ):
            raise ValueError("阿里云 WebSDK bridge 仅允许 loopback wss:// 地址")
        sdk_digest = str(expected_sdk_sha256).strip().lower()
        if len(sdk_digest) != 64 or any(char not in "0123456789abcdef" for char in sdk_digest):
            raise ValueError("expected_sdk_sha256 必须是 64 位十六进制摘要")
        if min(connect_timeout, message_timeout, request_timeout) <= 0:
            raise ValueError("Provider timeout 必须为正数")

        self._provider_id = provider_id
        self._display_name = str(display_name).strip() or provider_id
        self.bridge_url = str(bridge_url)
        self._credential = parse_aliyun_credential_bundle(credential_bundle)
        self.expected_sdk_sha256 = sdk_digest
        self.connect_timeout = float(connect_timeout)
        self.message_timeout = float(message_timeout)
        self.request_timeout = float(request_timeout)
        self._ws_connector = ws_connector or self._default_ws_connector
        self._ws: Any = None
        self._reader_task: asyncio.Task | None = None
        self._lifecycle_lock = asyncio.Lock()
        self._render_lock = asyncio.Lock()
        self._send_lock = asyncio.Lock()
        self._state = "stopped"
        self._active: _ActiveDiagnostic | None = None
        self._input_format: AudioFormat | None = None
        self._bridge_evidence: dict[str, Any] = {}
        self._last_error: ProviderError | None = None
        self._connection_epoch = 0
        self._transactions_input_accepted = 0

        self._capabilities = AvatarProviderCapabilities(
            render_modes=frozenset({AvatarRenderMode.REALTIME}),
            input_codecs=frozenset({"pcm_s16le"}),
            renderer_only=True,
            neural_lipsync=True,
            transactional_sentence=False,
            supports_cancel=True,
            supports_cancel_ack=False,
            supports_credit=False,
            supports_render_started=False,
            supports_sample_pts=False,
            strict_completion=False,
            protocol_name=PROTOCOL_NAME,
            protocol_version=PROTOCOL_VERSION,
            verification=ProviderVerification.UNVERIFIED,
            extra={
                "adapter": "aliyun_avatar",
                "experimental": True,
                "sandbox_only": True,
                "media_transport": "aliyun_rtc_via_websdk",
                "capability_source": "vendor_public_websdk",
                "local_video_output": False,
                "media_boundary_validated": False,
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
        """获取阿里云万相 RTC/流通道地址。"""
        return self._play_stream_addr

    @property
    def capabilities(self) -> AvatarProviderCapabilities:
        return self._capabilities

    @staticmethod
    async def _default_ws_connector(url: str, timeout: float):
        if websockets is None:
            raise ProviderError(
                ProviderErrorCode.REMOTE_UNAVAILABLE,
                "缺少 websockets 依赖，无法连接阿里云 WebSDK bridge",
            )
        return await websockets.connect(
            url,
            open_timeout=timeout,
            close_timeout=min(timeout, 5.0),
            max_size=2 * 1024 * 1024,
        )

    def _error(
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

    async def _send_control(self, message: Mapping[str, Any]) -> None:
        if self._ws is None:
            raise self._error(
                ProviderErrorCode.REMOTE_UNAVAILABLE,
                "阿里云 WebSDK bridge 未连接",
                retryable=True,
            )
        encoded = json.dumps(dict(message), ensure_ascii=False, separators=(",", ":"))
        if len(encoded.encode("utf-8")) > MAX_CONTROL_BYTES:
            raise self._error(
                ProviderErrorCode.INVALID_REQUEST,
                "bridge 控制消息超过容量上限",
            )
        async with self._send_lock:
            await self._ws.send(encoded)

    async def _recv_control(self, label: str) -> dict[str, Any]:
        try:
            raw = await asyncio.wait_for(self._ws.recv(), timeout=self.message_timeout)
        except TimeoutError as exc:
            raise self._error(
                ProviderErrorCode.MESSAGE_TIMEOUT,
                f"等待 bridge {label} 超时",
                retryable=True,
                restart_required=True,
            ) from exc
        if not isinstance(raw, str):
            raise self._error(
                ProviderErrorCode.PROTOCOL_VIOLATION,
                f"等待 bridge {label} 时收到二进制消息",
                restart_required=True,
            )
        try:
            message = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise self._error(
                ProviderErrorCode.PROTOCOL_VIOLATION,
                "bridge 返回无效 JSON",
                restart_required=True,
            ) from exc
        if not isinstance(message, dict):
            raise self._error(
                ProviderErrorCode.PROTOCOL_VIOLATION,
                "bridge 控制消息必须是 object",
                restart_required=True,
            )
        return message

    async def start(self) -> None:
        async with self._lifecycle_lock:
            if self._state == "ready":
                return
            if self._state not in {"stopped", "failed"}:
                raise self._error(
                    ProviderErrorCode.CONCURRENCY_LIMIT,
                    f"bridge 当前状态不允许启动：{self._state}",
                )
            self._state = "connecting"
            self._last_error = None
            self._connection_epoch += 1
            try:
                self._ws = await self._ws_connector(self.bridge_url, self.connect_timeout)
                await self._send_control(
                    build_hello_message(
                        self._credential.bridge_auth_token,
                        self.expected_sdk_sha256,
                    )
                )
                hello = await self._recv_control("bridge_hello_ok")
                self._bridge_evidence = validate_hello_reply(
                    hello,
                    expected_sdk_sha256=self.expected_sdk_sha256,
                )
                rtc = self._credential.rtc
                await self._send_control(
                    {
                        "event": "session_open",
                        "protocol_version": PROTOCOL_VERSION,
                        "sandbox_only": True,
                        "sdk_mode": "tap2talk",
                        "ignore_audio_input": True,
                        "rtc_init": {
                            "appId": rtc.app_id,
                            "channel": rtc.channel,
                            "timestamp": rtc.timestamp,
                            "token": rtc.token,
                            "nonce": rtc.nonce,
                            "clientUserId": rtc.client_user_id,
                            "clientUserName": rtc.client_user_name,
                            "serverUserId": rtc.server_user_id,
                            "avatarUserId": rtc.avatar_user_id,
                            "sessionId": rtc.session_id,
                        },
                    }
                )
                ready = await self._recv_control("session_ready")
                self._input_format = validate_session_ready(ready)
                self._state = "ready"
                self._reader_task = asyncio.create_task(self._reader_loop())
            except Exception as exc:
                error = exc if isinstance(exc, ProviderError) else self._error(
                    ProviderErrorCode.PROTOCOL_VIOLATION,
                    f"阿里云 WebSDK bridge 握手失败：{exc}",
                    restart_required=True,
                )
                self._last_error = error
                self._state = "failed"
                await self._close_ws()
                raise error

    async def _reader_loop(self) -> None:
        try:
            async for raw in self._ws:
                if not isinstance(raw, str):
                    raise self._error(
                        ProviderErrorCode.PROTOCOL_VIOLATION,
                        "bridge 不得向当前实验 Provider 返回二进制视频",
                        restart_required=True,
                    )
                self._handle_message(raw)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            error = exc if isinstance(exc, ProviderError) else self._error(
                ProviderErrorCode.REMOTE_UNAVAILABLE,
                f"阿里云 WebSDK bridge 已断开：{type(exc).__name__}",
                retryable=True,
                restart_required=True,
            )
            self._last_error = error
            self._state = "failed"
            active = self._active
            if active is not None:
                active.error = error
                active.input_accepted.set()
                active.interrupt_invoked.set()

    def _handle_message(self, raw: str) -> None:
        try:
            message = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise self._error(
                ProviderErrorCode.PROTOCOL_VIOLATION,
                "bridge 返回无效 JSON",
                restart_required=True,
            ) from exc
        if not isinstance(message, dict):
            raise self._error(
                ProviderErrorCode.PROTOCOL_VIOLATION,
                "bridge 控制消息必须是 object",
                restart_required=True,
            )
        event = str(message.get("event") or "")
        active = self._active
        if event == "sdk_error":
            code = str(message.get("code") or "unknown")
            error = self._error(
                ProviderErrorCode.REMOTE_UNAVAILABLE,
                f"阿里云 WebSDK 报错 code={code}",
                restart_required=message.get("terminate") is True,
                details={"sdk_error_code": code},
            )
            self._last_error = error
            if active is not None:
                active.error = error
                active.input_accepted.set()
                active.interrupt_invoked.set()
            return
        if active is None:
            return
        request_id = str(message.get("request_id") or "")
        if event == "audio_push_accepted" and request_id == active.request_id:
            active.input_accepted.set()
        elif event == "interrupt_invoked" and request_id == active.request_id:
            if str(message.get("interrupt_id") or "") == active.interrupt_id:
                active.interrupt_invoked.set()
        elif event == "sdk_callback_observed":
            callback = str(message.get("callback") or "")
            if callback == "onFirstFrameReceived":
                active.first_frame_callback_observed = True
            elif callback == "onStateChanged":
                state = str(message.get("state") or "")
                if state:
                    active.observed_states.append(state)

    async def _wait(
        self,
        event: asyncio.Event,
        active: _ActiveDiagnostic,
        label: str,
    ) -> None:
        try:
            await asyncio.wait_for(event.wait(), timeout=self.message_timeout)
        except TimeoutError as exc:
            raise self._error(
                ProviderErrorCode.MESSAGE_TIMEOUT,
                f"等待 bridge {label} 超时",
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
            raise self._error(
                ProviderErrorCode.CAPABILITY_MISMATCH,
                "阿里云音频驱动 WebSDK 只支持 realtime 诊断模式",
            )
        frame_batch = validate_audio_frame_batch(frames)
        first = frame_batch[0]
        async with self._render_lock:
            if self._state != "ready" or self._ws is None or self._input_format is None:
                raise self._error(
                    ProviderErrorCode.REMOTE_UNAVAILABLE,
                    "阿里云 WebSDK bridge 尚未就绪",
                    retryable=True,
                )
            if self._active is not None:
                raise self._error(
                    ProviderErrorCode.CONCURRENCY_LIMIT,
                    "实验 bridge 每个 session 只允许一条诊断音频",
                )
            if first.format != self._input_format:
                raise self._error(
                    ProviderErrorCode.CAPABILITY_MISMATCH,
                    f"AudioFrame format 与 bridge 协商格式不一致：{first.format.to_dict()} != {self._input_format.to_dict()}",
                )
            request_id = uuid.uuid4().hex
            active = _ActiveDiagnostic(request_id=request_id, audio_id=first.audio_id)
            self._active = active
            started_at = time.monotonic()
            try:
                await self._send_control(
                    {
                        "event": "render_open",
                        "protocol_version": PROTOCOL_VERSION,
                        "request_id": request_id,
                        "audio_id": first.audio_id,
                        "audio_generation": first.audio_generation,
                        "session_generation": first.session_generation,
                        "format": first.format.to_dict(),
                        "total_frames": len(frame_batch),
                        "total_samples": sum(frame.duration_samples for frame in frame_batch),
                    }
                )
                for frame in frame_batch:
                    async with self._send_lock:
                        await self._ws.send(encode_audio_frame(request_id, frame))
                await self._wait(active.input_accepted, active, "audio_push_accepted")
                self._transactions_input_accepted += 1
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
                        "adapter": "aliyun_avatar",
                        "sandbox_only": True,
                        "completion_scope": "websdk_input_accepted_only",
                        "strict_completion_observed": False,
                        "first_frame_callback_observed": active.first_frame_callback_observed,
                        "first_frame_request_correlation": "unverified",
                        "video_track_consumed": False,
                        "sample_pts_observed": False,
                        "billing_evidence": "unverified",
                        **self._bridge_evidence,
                    },
                )
            except ProviderError as exc:
                self._last_error = exc
                if exc.restart_required:
                    await self._close_ws()
                    self._state = "failed"
                raise
            except Exception as exc:
                error = self._error(
                    ProviderErrorCode.REMOTE_UNAVAILABLE,
                    f"bridge 音频发送失败：{type(exc).__name__}",
                    retryable=True,
                    restart_required=True,
                    request_id=request_id,
                )
                self._last_error = error
                await self._close_ws()
                self._state = "failed"
                raise error from exc

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
        active.interrupt_id = uuid.uuid4().hex
        try:
            await self._send_control(
                {
                    "event": "interrupt_invoke",
                    "request_id": active.request_id,
                    "interrupt_id": active.interrupt_id,
                }
            )
            await self._wait(active.interrupt_invoked, active, "interrupt_invoked")
        finally:
            # WebSDK 只证明本地 interrupt() 被调用，无法证明 RTC 视频已静止。
            await self._close_ws()
            self._state = "failed"
        error = self._error(
            ProviderErrorCode.CANCEL_UNCONFIRMED,
            "阿里云 WebSDK 已调用 interrupt，但没有 request-correlated quiesced ACK",
            restart_required=True,
            request_id=active.request_id,
        )
        self._last_error = error
        raise error

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

    async def stop(self) -> None:
        async with self._lifecycle_lock:
            if self._state == "stopped" and self._ws is None:
                return
            self._state = "stopping"
            if self._ws is not None:
                try:
                    await self._send_control(
                        {
                            "event": "exit_invoke",
                            "protocol_version": PROTOCOL_VERSION,
                        }
                    )
                except Exception:
                    pass
            await self._close_ws()
            self._active = None
            self._input_format = None
            self._state = "stopped"

    def get_status(self) -> Mapping[str, Any]:
        error = self._last_error
        return {
            "configured": True,
            "adapter": "aliyun_avatar",
            "experimental": True,
            "sandbox_only": True,
            "verification": ProviderVerification.UNVERIFIED.value,
            "session_state": self._state,
            "control_ready": self._state == "ready",
            "speech_state": "diagnostic_active" if self._active is not None else "idle",
            "protocol_name": PROTOCOL_NAME,
            "protocol_version": PROTOCOL_VERSION,
            "bridge_evidence": dict(self._bridge_evidence),
            "input_format": self._input_format.to_dict() if self._input_format else None,
            "media_transport": "aliyun_rtc_via_websdk",
            "local_video_consumable": False,
            "first_frame_callback_observed": bool(
                self._active and self._active.first_frame_callback_observed
            ),
            "video_track_observed": False,
            "sample_pts_observed": False,
            "strict_completion_observed": False,
            "media_boundary_validated": False,
            "transactions_input_accepted": self._transactions_input_accepted,
            "connection_epoch": self._connection_epoch,
            "last_error": error.to_dict() if error is not None else None,
        }
