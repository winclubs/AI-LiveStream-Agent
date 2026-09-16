"""腾讯云智能数智人（IVH）云渲染实验 Provider。

基于官方公开 aPaaS 接口（https://gw.tvs.qq.com，文档 product/1240）：
- 会话管理走 HTTPS：createsession / statsession / startsession / closesession；
- 音频驱动走 WSS commandchannel：SEND_AUDIO（PCM 16kHz/16bit/mono，Base64）；
- 下行消息提供 ReqId 关联的 AudioStart/AudioOver/Error 与 FinalType。

视频流 PlayStreamAddr 尚未接入本地输出（可能携带 TRTC userSig，禁止写入日志
或状态），因此本适配器只用于 sandbox 诊断，验收前保持 experimental/unverified。
会话结束必须 closesession，否则并发与计费持续消耗。
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import time
import uuid
from array import array
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Literal, Mapping, Sequence
from urllib.parse import quote, urlsplit

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

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
except ImportError:  # pragma: no cover
    websockets = None  # type: ignore[assignment]

PROTOCOL_NAME = "tencent_ivh_apaas"
PROTOCOL_VERSION = 1
DEFAULT_API_BASE_URL = "https://gw.tvs.qq.com"
_ALLOWED_API_HOSTS = frozenset({"gw.tvs.qq.com"})
_AUDIO_SAMPLE_RATE = 16_000
_AUDIO_PACKET_BYTES = 5120  # 160ms PCM16 mono
_FAST_PACKET_COUNT = 6
_PACKET_INTERVAL_SECONDS = 0.12
_STATUS_POLL_INTERVAL_SECONDS = 2.0
_SESSION_STATUS_READY = 1
_SESSION_STATUS_CLOSED = 2
_SESSION_STATUS_PREPARING = 3
_SESSION_STATUS_FAILED = 4

WsConnector = Callable[[str, float], Awaitable[Any]]


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class TencentIVHCredentialBundleV1(_StrictModel):
    """加密字段内的官方网关凭据；AppKey 半公开但与 AccessToken 成对出现，统一加密。"""

    version: Literal[1]
    app_key: str = Field(min_length=1, max_length=256)
    access_token: str = Field(min_length=1, max_length=4096)


def parse_tencent_credential_bundle(raw: str) -> TencentIVHCredentialBundleV1:
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError("腾讯云 credential bundle 必须是 JSON") from exc
    try:
        return TencentIVHCredentialBundleV1.model_validate(payload, strict=True)
    except ValidationError as exc:
        first = exc.errors(include_url=False)[0]
        path = ".".join(str(part) for part in first.get("loc", ())) or "credential"
        raise ValueError(
            f"腾讯云 credential bundle 无效：{path}: {first.get('msg')}"
        ) from exc


def build_signed_url(
    base_url: str,
    path: str,
    params: Mapping[str, str],
    access_token: str,
) -> str:
    """按官方签名规则生成带 signature 的完整请求 URL。

    规则（文档 107197）：除 signature 外的全部 query 参数按字典序拼接为
    k=v&k=v，用 AccessToken 做 HmacSha256 后 Base64，再 URL 编码追加。
    """

    signing_content = "&".join(
        f"{key}={params[key]}" for key in sorted(params)
    )
    digest = hmac.new(
        access_token.encode("utf-8"),
        signing_content.encode("utf-8"),
        hashlib.sha256,
    ).digest()
    signature = base64.b64encode(digest).decode("ascii")
    return f"{base_url.rstrip('/')}{path}?{signing_content}&signature={quote(signature, safe='')}"


def _resample_to_16k_mono(pcm: bytes, source_rate: int) -> bytes:
    """线性插值重采样到 16kHz；仅处理小端 PCM16（Windows/LE 平台假设）。"""

    if source_rate == _AUDIO_SAMPLE_RATE:
        return pcm
    sample_count = len(pcm) // 2
    if sample_count == 0:
        return b""
    source = array("h")
    source.frombytes(pcm[: sample_count * 2])
    ratio = _AUDIO_SAMPLE_RATE / float(source_rate)
    out_count = max(1, int(sample_count * ratio))
    target = array("h")
    for index in range(out_count):
        position = index / ratio
        base_index = int(position)
        next_index = min(base_index + 1, sample_count - 1)
        frac = position - base_index
        target.append(
            int(source[base_index] * (1.0 - frac) + source[next_index] * frac)
        )
    return target.tobytes()


@dataclass(slots=True)
class _ActiveDrive:
    request_id: str
    audio_id: str
    audio_start_observed: bool = False
    audio_over_observed: bool = False
    final_type: int | None = None
    observed_states: list[str] = field(default_factory=list)
    error: ProviderError | None = None
    audio_over_event: asyncio.Event = field(default_factory=asyncio.Event)


class TencentIVHAvatarProvider:
    """腾讯云智能数智人云渲染 fail-closed 实验适配器。"""

    def __init__(
        self,
        *,
        provider_id: str,
        display_name: str,
        api_base_url: str = DEFAULT_API_BASE_URL,
        credential_bundle: str,
        virtualman_project_id: str,
        user_id: str = "las_agent_ivh_experimental",
        protocol: str = "rtmp",
        stream_max_interval: int = 2000,
        heartbeat_interval_seconds: int = 40,
        sandbox_only: bool = False,
        connect_timeout: float = 10.0,
        message_timeout: float = 20.0,
        request_timeout: float = 180.0,
        http_client: httpx.AsyncClient | None = None,
        ws_connector: WsConnector | None = None,
    ) -> None:
        provider_id = str(provider_id).strip()
        if not provider_id:
            raise ValueError("provider_id 不能为空")
        self._sandbox_only = bool(sandbox_only)
        self._play_stream_addr = ""
        parsed = urlsplit(str(api_base_url))
        if (
            parsed.scheme != "https"
            or (parsed.hostname or "").lower() not in _ALLOWED_API_HOSTS
            or parsed.port not in {None, 443}
            or parsed.path.rstrip("/")
            or parsed.query
            or parsed.fragment
            or parsed.username
            or parsed.password
        ):
            raise ValueError(
                "腾讯云数智人 base_url 仅允许官方网关 https://gw.tvs.qq.com"
            )
        project_id = str(virtualman_project_id).strip()
        if not project_id or len(project_id) > 256:
            raise ValueError("virtualman_project_id 不能为空且不超过 256 字符")
        if protocol not in {"rtmp", "webrtc"}:
            raise ValueError("实验适配器仅支持 rtmp/webrtc 协议（trtc 需房间配置，未开放）")
        if not 2000 <= int(stream_max_interval) <= 6000:
            raise ValueError("stream_max_interval 必须在 2000-6000ms 范围内")
        if not 31 <= int(heartbeat_interval_seconds) <= 59:
            raise ValueError("heartbeat_interval_seconds 必须在 31-59 秒范围内")
        if min(connect_timeout, message_timeout, request_timeout) <= 0:
            raise ValueError("Provider timeout 必须为正数")

        self._provider_id = provider_id
        self._display_name = str(display_name).strip() or provider_id
        self._api_base_url = str(api_base_url).rstrip("/")
        self._credential = parse_tencent_credential_bundle(credential_bundle)
        self._project_id = project_id
        self._user_id = str(user_id).strip() or "las_agent_ivh_experimental"
        self._protocol = protocol
        self._stream_max_interval = int(stream_max_interval)
        self._heartbeat_interval = float(heartbeat_interval_seconds)
        self.connect_timeout = float(connect_timeout)
        self.message_timeout = float(message_timeout)
        self.request_timeout = float(request_timeout)
        self._ws_connector = ws_connector or self._default_ws_connector
        self._http = http_client
        self._owns_http = http_client is None
        self._ws: Any = None
        self._reader_task: asyncio.Task | None = None
        self._heartbeat_task: asyncio.Task | None = None
        self._lifecycle_lock = asyncio.Lock()
        self._render_lock = asyncio.Lock()
        self._send_lock = asyncio.Lock()
        self._state = "stopped"
        self._session_id = ""
        self._session_status: int | None = None
        self._speak_status = ""
        self._active: _ActiveDrive | None = None
        self._last_error: ProviderError | None = None
        self._connection_epoch = 0
        self._transactions_completed = 0
        self._interrupt_final_type_observed: int | None = None

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
                "adapter": "tencent_avatar",
                "experimental": True,
                "sandbox_only": True,
                "media_transport": "tencent_ivh_apaas",
                "capability_source": "vendor_public_apaas_docs",
                "audio_requirement": "pcm_s16le_16000_mono",
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
    def capabilities(self) -> AvatarProviderCapabilities:
        return self._capabilities

    @property
    def play_stream_addr(self) -> str:
        """获取腾讯云数智人云端推流/播放流地址（RTMP/WebRTC），供 OBS 媒体源直接拉流。"""
        return self._play_stream_addr

    @staticmethod
    async def _default_ws_connector(url: str, timeout: float):
        if websockets is None:
            raise ProviderError(
                ProviderErrorCode.REMOTE_UNAVAILABLE,
                "缺少 websockets 依赖，无法连接腾讯云数智人 commandchannel",
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

    def _api_url(self, path: str, extra_params: Mapping[str, str] | None = None) -> str:
        params: dict[str, str] = {
            "appkey": self._credential.app_key,
            "timestamp": str(int(time.time())),
        }
        if extra_params:
            params.update(extra_params)
        return build_signed_url(
            self._api_base_url,
            path,
            params,
            self._credential.access_token,
        )

    async def _post_api(self, path: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        if self._http is None:
            self._http = httpx.AsyncClient(
                timeout=httpx.Timeout(self.message_timeout),
            )
        url = self._api_url(path)
        try:
            response = await self._http.request(
                "POST",
                url,
                json={"Header": {}, "Payload": dict(payload)},
            )
        except httpx.TimeoutException as exc:
            raise self._error(
                ProviderErrorCode.MESSAGE_TIMEOUT,
                f"腾讯云数智人接口超时: {path}",
                retryable=True,
                restart_required=True,
            ) from exc
        except httpx.HTTPError as exc:
            raise self._error(
                ProviderErrorCode.REMOTE_UNAVAILABLE,
                f"腾讯云数智人接口网络错误: {type(exc).__name__}",
                retryable=True,
                restart_required=True,
            ) from exc
        if response.status_code in {401, 403}:
            raise self._error(
                ProviderErrorCode.AUTHENTICATION,
                f"腾讯云数智人鉴权失败 (HTTP {response.status_code})",
                details={"http_status": response.status_code},
            )
        if not 200 <= response.status_code < 300:
            raise self._error(
                ProviderErrorCode.REMOTE_UNAVAILABLE,
                f"腾讯云数智人接口 HTTP {response.status_code}",
                retryable=True,
                restart_required=True,
                details={"http_status": response.status_code},
            )
        try:
            body = response.json()
        except ValueError as exc:
            raise self._error(
                ProviderErrorCode.PROTOCOL_VIOLATION,
                "腾讯云数智人接口返回非 JSON",
                restart_required=True,
            ) from exc
        if not isinstance(body, dict):
            raise self._error(
                ProviderErrorCode.PROTOCOL_VIOLATION,
                "腾讯云数智人接口响应必须是 object",
                restart_required=True,
            )
        header = body.get("Header")
        if isinstance(header, dict) and header.get("Code") not in {0, None}:
            code = header.get("Code")
            raise self._error(
                ProviderErrorCode.REMOTE_UNAVAILABLE,
                f"腾讯云数智人接口业务错误 code={code}: {header.get('Message', '')}",
                retryable=True,
                restart_required=True,
                details={"api_code": code},
            )
        payload_out = body.get("Payload")
        if not isinstance(payload_out, dict):
            raise self._error(
                ProviderErrorCode.PROTOCOL_VIOLATION,
                "腾讯云数智人接口响应缺少 Payload",
                restart_required=True,
            )
        return payload_out

    async def _close_session_api(self) -> None:
        if not self._session_id:
            return
        session_id = self._session_id
        try:
            await asyncio.wait_for(
                self._post_api(
                    "/v2/ivh/sessionmanager/sessionmanagerservice/closesession",
                    {"ReqId": uuid.uuid4().hex, "SessionId": session_id},
                ),
                timeout=self.message_timeout,
            )
        except Exception:
            # 关闭失败也要让 stop 完成；计费释放问题由状态字段如实暴露。
            pass

    async def start(self) -> None:
        async with self._lifecycle_lock:
            if self._state == "ready":
                return
            if self._state not in {"stopped", "failed"}:
                raise self._error(
                    ProviderErrorCode.CONCURRENCY_LIMIT,
                    f"腾讯云数智人会话当前状态不允许启动：{self._state}",
                )
            self._state = "connecting"
            self._last_error = None
            self._connection_epoch += 1
            session_created = False
            try:
                create = await self._post_api(
                    "/v2/ivh/sessionmanager/sessionmanagerservice/createsession",
                    {
                        "ReqId": uuid.uuid4().hex,
                        "VirtualmanProjectId": self._project_id,
                        "UserId": self._user_id,
                        "Protocol": self._protocol,
                        "DriverType": 3,
                        "StreamMaxInterval": self._stream_max_interval,
                    },
                )
                session_id = str(create.get("SessionId") or "")
                if not session_id:
                    raise self._error(
                        ProviderErrorCode.PROTOCOL_VIOLATION,
                        "腾讯云数智人 createsession 未返回 SessionId",
                        restart_required=True,
                    )
                self._session_id = session_id
                self._play_stream_addr = str(create.get("PlayStreamAddr") or "")
                session_created = True
                status = create.get("SessionStatus")
                if isinstance(status, bool) or not isinstance(status, int):
                    raise self._error(
                        ProviderErrorCode.PROTOCOL_VIOLATION,
                        "createsession SessionStatus 必须是 int",
                        restart_required=True,
                    )
                deadline = time.monotonic() + self.request_timeout
                while status != _SESSION_STATUS_READY:
                    if status in {_SESSION_STATUS_CLOSED, _SESSION_STATUS_FAILED}:
                        raise self._error(
                            ProviderErrorCode.REMOTE_UNAVAILABLE,
                            f"腾讯云数智人会话不可用，SessionStatus={status}",
                            retryable=True,
                            restart_required=True,
                            details={"session_status": status},
                        )
                    if time.monotonic() >= deadline:
                        raise self._error(
                            ProviderErrorCode.REQUEST_TIMEOUT,
                            "等待腾讯云数智人会话就绪超时",
                            retryable=True,
                            restart_required=True,
                        )
                    await asyncio.sleep(_STATUS_POLL_INTERVAL_SECONDS)
                    state = await self._post_api(
                        "/v2/ivh/sessionmanager/sessionmanagerservice/statsession",
                        {"ReqId": uuid.uuid4().hex, "SessionId": session_id},
                    )
                    if not self._play_stream_addr and state.get("PlayStreamAddr"):
                        self._play_stream_addr = str(state.get("PlayStreamAddr") or "")
                    raw_status = state.get("SessionStatus")
                    if isinstance(raw_status, bool) or not isinstance(raw_status, int):
                        raise self._error(
                            ProviderErrorCode.PROTOCOL_VIOLATION,
                            "statsession SessionStatus 必须是 int",
                            restart_required=True,
                        )
                    status = raw_status
                    self._session_status = status
                self._session_status = status
                await self._post_api(
                    "/v2/ivh/sessionmanager/sessionmanagerservice/startsession",
                    {"ReqId": uuid.uuid4().hex, "SessionId": session_id},
                )
                ws_url = build_signed_url(
                    "wss://" + self._api_base_url.split("://", 1)[1],
                    "/v2/ws/ivh/interactdriver/interactdriverservice/commandchannel",
                    {
                        "appkey": self._credential.app_key,
                        "requestid": uuid.uuid4().hex,
                        "timestamp": str(int(time.time())),
                    },
                    self._credential.access_token,
                )
                self._ws = await self._ws_connector(ws_url, self.connect_timeout)
                self._state = "ready"
                self._reader_task = asyncio.create_task(self._reader_loop())
                self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())
            except Exception as exc:
                error = exc if isinstance(exc, ProviderError) else self._error(
                    ProviderErrorCode.PROTOCOL_VIOLATION,
                    f"腾讯云数智人会话启动失败：{exc}",
                    restart_required=True,
                )
                self._last_error = error
                self._state = "failed"
                if session_created:
                    await self._close_session_api()
                await self._close_ws()
                raise error

    async def _reader_loop(self) -> None:
        try:
            async for raw in self._ws:
                if isinstance(raw, (bytes, bytearray)):
                    raise self._error(
                        ProviderErrorCode.PROTOCOL_VIOLATION,
                        "腾讯云数智人 commandchannel 不应返回二进制消息",
                        restart_required=True,
                    )
                self._handle_message(str(raw))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            error = exc if isinstance(exc, ProviderError) else self._error(
                ProviderErrorCode.REMOTE_UNAVAILABLE,
                f"腾讯云数智人 commandchannel 已断开: {type(exc).__name__}",
                retryable=True,
                restart_required=True,
            )
            self._last_error = error
            self._state = "failed"
            active = self._active
            if active is not None:
                active.error = error
                active.audio_over_event.set()

    def _handle_message(self, raw: str) -> None:
        try:
            message = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise self._error(
                ProviderErrorCode.PROTOCOL_VIOLATION,
                "腾讯云数智人下行消息不是有效 JSON",
                restart_required=True,
            ) from exc
        if not isinstance(message, dict):
            raise self._error(
                ProviderErrorCode.PROTOCOL_VIOLATION,
                "腾讯云数智人下行消息必须是 object",
                restart_required=True,
            )
        message_type = message.get("Type")
        if isinstance(message_type, bool) or not isinstance(message_type, int):
            return
        speak_status = str(message.get("SpeakStatus") or "")
        if speak_status:
            self._speak_status = speak_status
        error_code = message.get("ErrorCode")
        if isinstance(error_code, int) and error_code != 0 and message_type == 9:
            error = self._error(
                ProviderErrorCode.REMOTE_UNAVAILABLE,
                f"腾讯云数智人驱动失败 code={error_code}: "
                f"{message.get('ErrorMessage', '')}",
                retryable=True,
                restart_required=True,
                details={"drive_error_code": error_code},
            )
            self._last_error = error
            active = self._active
            if active is not None and str(message.get("ReqId") or "") == active.request_id:
                active.error = error
                active.audio_over_event.set()
            return
        active = self._active
        if active is None or str(message.get("ReqId") or "") != active.request_id:
            return
        if speak_status:
            active.observed_states.append(speak_status)
        if speak_status == "AudioStart":
            active.audio_start_observed = True
        elif speak_status == "AudioOver":
            final_type = message.get("FinalType")
            if isinstance(final_type, int) and not isinstance(final_type, bool):
                active.final_type = final_type
            active.audio_over_observed = True
            active.audio_over_event.set()
        elif speak_status == "Error":
            active.error = self._error(
                ProviderErrorCode.REMOTE_UNAVAILABLE,
                "腾讯云数智人播报进入 Error 终态",
                retryable=True,
            )
            active.audio_over_event.set()

    async def _heartbeat_loop(self) -> None:
        try:
            while self._state == "ready":
                await asyncio.sleep(self._heartbeat_interval)
                if self._state != "ready" or self._ws is None:
                    return
                await self._send_json(
                    {
                        "Header": {},
                        "Payload": {
                            "ReqId": uuid.uuid4().hex,
                            "SessionId": self._session_id,
                            "Command": "SEND_HEARTBEAT",
                            "Data": {"Text": "PING"},
                        },
                    }
                )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            error = exc if isinstance(exc, ProviderError) else self._error(
                ProviderErrorCode.REMOTE_UNAVAILABLE,
                f"腾讯云数智人心跳发送失败: {type(exc).__name__}",
                retryable=True,
                restart_required=True,
            )
            self._last_error = error
            self._state = "failed"
            active = self._active
            if active is not None:
                active.error = error
                active.audio_over_event.set()

    async def _send_json(self, message: Mapping[str, Any]) -> None:
        if self._ws is None:
            raise self._error(
                ProviderErrorCode.REMOTE_UNAVAILABLE,
                "腾讯云数智人 commandchannel 未连接",
                retryable=True,
            )
        encoded = json.dumps(dict(message), ensure_ascii=False, separators=(",", ":"))
        async with self._send_lock:
            await self._ws.send(encoded)

    async def _send_audio_packet(
        self,
        request_id: str,
        seq: int,
        pcm: bytes,
        *,
        is_final: bool,
    ) -> None:
        await self._send_json(
            {
                "Header": {},
                "Payload": {
                    "ReqId": request_id,
                    "SessionId": self._session_id,
                    "Command": "SEND_AUDIO",
                    "Data": {
                        "Audio": base64.b64encode(pcm).decode("ascii") if pcm else "",
                        "Seq": seq,
                        "IsFinal": is_final,
                    },
                },
            }
        )

    async def render_sentence(
        self,
        frames: Sequence[AudioFrame],
        *,
        render_mode: AvatarRenderMode,
    ) -> ProviderRenderResult:
        if render_mode is not AvatarRenderMode.REALTIME:
            raise self._error(
                ProviderErrorCode.CAPABILITY_MISMATCH,
                "腾讯云数智人实验适配器只支持 realtime 诊断模式",
            )
        frame_batch = validate_audio_frame_batch(frames)
        first = frame_batch[0]
        pcm = b"".join(bytes(frame.data) for frame in frame_batch)
        input_format = first.format
        if input_format.channels != 1 or input_format.sample_width_bytes != 2:
            raise self._error(
                ProviderErrorCode.CAPABILITY_MISMATCH,
                "腾讯云数智人仅接受 mono PCM16 音频帧",
            )
        async with self._render_lock:
            if self._state != "ready" or self._ws is None:
                raise self._error(
                    ProviderErrorCode.REMOTE_UNAVAILABLE,
                    "腾讯云数智人会话尚未就绪",
                    retryable=True,
                )
            if self._active is not None:
                raise self._error(
                    ProviderErrorCode.CONCURRENCY_LIMIT,
                    "实验适配器每个会话只允许一条在途音频驱动",
                )
            request_id = uuid.uuid4().hex
            active = _ActiveDrive(request_id=request_id, audio_id=first.audio_id)
            self._active = active
            started_at = time.monotonic()
            try:
                pcm_16k = _resample_to_16k_mono(pcm, input_format.sample_rate)
                packets = [
                    pcm_16k[offset : offset + _AUDIO_PACKET_BYTES]
                    for offset in range(0, len(pcm_16k), _AUDIO_PACKET_BYTES)
                ]
                if not packets:
                    raise self._error(
                        ProviderErrorCode.INVALID_REQUEST,
                        "音频批次为空，无法驱动腾讯云数智人",
                    )
                sent = 0
                for packet in packets:
                    if sent >= _FAST_PACKET_COUNT:
                        await asyncio.sleep(_PACKET_INTERVAL_SECONDS)
                    await self._send_audio_packet(
                        request_id, sent + 1, packet, is_final=False
                    )
                    sent += 1
                if sent >= _FAST_PACKET_COUNT:
                    await asyncio.sleep(_PACKET_INTERVAL_SECONDS)
                await self._send_audio_packet(
                    request_id, sent + 1, b"", is_final=True
                )
                try:
                    await asyncio.wait_for(
                        active.audio_over_event.wait(),
                        timeout=self.request_timeout,
                    )
                except TimeoutError as exc:
                    raise self._error(
                        ProviderErrorCode.REQUEST_TIMEOUT,
                        "等待腾讯云数智人 AudioOver 终态超时",
                        retryable=True,
                        restart_required=True,
                        request_id=request_id,
                    ) from exc
                if active.error is not None:
                    raise active.error
                if not active.audio_over_observed:
                    raise self._error(
                        ProviderErrorCode.PROTOCOL_VIOLATION,
                        "腾讯云数智人下行消息缺少 AudioOver 终态",
                        restart_required=True,
                        request_id=request_id,
                    )
                self._transactions_completed += 1
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
                        "adapter": "tencent_avatar",
                        "sandbox_only": True,
                        "completion_scope": "audio_over_observed",
                        "request_correlated_completion": True,
                        "final_type": active.final_type,
                        "audio_start_observed": active.audio_start_observed,
                        "observed_states": list(active.observed_states),
                        "video_track_consumed": False,
                        "sample_pts_observed": False,
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
                error = self._error(
                    ProviderErrorCode.REMOTE_UNAVAILABLE,
                    f"腾讯云数智人音频驱动失败: {type(exc).__name__}",
                    retryable=True,
                    restart_required=True,
                    request_id=request_id,
                )
                self._last_error = error
                await self._close_ws()
                self._state = "failed"
                raise error from exc
            finally:
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
        try:
            await self._send_json(
                {
                    "Header": {},
                    "Payload": {
                        "ReqId": active.request_id,
                        "SessionId": self._session_id,
                        "Command": "SEND_AUDIO",
                        "Data": {"Interrupt": True},
                    },
                }
            )
            # FinalType=3 表示中断导致的终态；观察到也只能证明驱动结束，
            # 不能证明视频轨已静止，因此仍按未确认取消处理。
            try:
                await asyncio.wait_for(
                    active.audio_over_event.wait(),
                    timeout=self.message_timeout,
                )
            except TimeoutError:
                pass
            if active.audio_over_observed and active.final_type == 3:
                self._interrupt_final_type_observed = 3
        except ProviderError as exc:
            self._last_error = exc
        finally:
            await self._close_ws()
            self._state = "failed"
        error = self._error(
            ProviderErrorCode.CANCEL_UNCONFIRMED,
            "腾讯云数智人已发送 Interrupt，但视频轨静止无法证明",
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
        for task in (self._heartbeat_task, self._reader_task):
            if task is not None and task is not asyncio.current_task():
                task.cancel()
        tasks = [
            task
            for task in (self._heartbeat_task, self._reader_task)
            if task is not None
        ]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._heartbeat_task = None
        self._reader_task = None

    async def stop(self) -> None:
        async with self._lifecycle_lock:
            if self._state == "stopped" and self._ws is None:
                return
            self._state = "stopping"
            await self._close_session_api()
            await self._close_ws()
            self._active = None
            self._session_id = ""
            self._session_status = None
            self._speak_status = ""
            self._state = "stopped"

    def get_status(self) -> Mapping[str, Any]:
        error = self._last_error
        return {
            "configured": True,
            "adapter": "tencent_avatar",
            "experimental": True,
            "sandbox_only": True,
            "verification": ProviderVerification.UNVERIFIED.value,
            "session_state": self._state,
            "control_ready": self._state == "ready",
            "session_id": self._session_id or None,
            "session_status": self._session_status,
            "speak_status": self._speak_status or None,
            "protocol_name": PROTOCOL_NAME,
            "protocol_version": PROTOCOL_VERSION,
            "stream_protocol": self._protocol,
            "media_transport": "tencent_ivh_apaas",
            "local_video_consumable": False,
            "video_track_observed": False,
            "sample_pts_observed": False,
            "strict_completion_observed": False,
            "media_boundary_validated": False,
            "transactions_completed": self._transactions_completed,
            "interrupt_final_type_observed": self._interrupt_final_type_observed,
            "connection_epoch": self._connection_epoch,
            "last_error": error.to_dict() if error is not None else None,
        }
