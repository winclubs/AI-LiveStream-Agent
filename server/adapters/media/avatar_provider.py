"""厂商无关的远端 Avatar 渲染 Provider 契约。

该契约只负责消费已经帧化的 PCM 音频并生成远端视频，不参与 TTS。具体厂商的
鉴权、endpoint 和 wire protocol 必须由显式注册的 adapter 实现，不能从数据库
字符串动态导入。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType
from typing import Any, Mapping, Protocol, Sequence, runtime_checkable

from server.core.media.audio_frame import AudioFrame


class ProviderMode(StrEnum):
    """Provider 在自动选择池中的运营角色。"""

    PRIMARY = "primary"
    FALLBACK = "fallback"
    SHADOW = "shadow"
    DISABLED = "disabled"


class AvatarRenderMode(StrEnum):
    """远端渲染事务模型；两种模式都必须保持整句原子完成语义。"""

    REALTIME = "realtime"
    BATCH = "batch"


class ProviderVerification(StrEnum):
    """能力声明的可信来源，未验证节点不能进入神经渲染 auto 池。"""

    VERIFIED = "verified"
    UNVERIFIED = "unverified"


class ProviderErrorCode(StrEnum):
    """跨厂商稳定错误分类，供熔断、额度保护和低基数指标使用。"""

    AUTHENTICATION = "authentication"
    CAPABILITY_MISMATCH = "capability_mismatch"
    INVALID_REQUEST = "invalid_request"
    CONNECT_TIMEOUT = "connect_timeout"
    MESSAGE_TIMEOUT = "message_timeout"
    REQUEST_TIMEOUT = "request_timeout"
    CONCURRENCY_LIMIT = "concurrency_limit"
    QUOTA_EXHAUSTED = "quota_exhausted"
    RATE_LIMITED = "rate_limited"
    REMOTE_UNAVAILABLE = "remote_unavailable"
    REMOTE_RESOURCE_EXHAUSTED = "remote_resource_exhausted"
    PROTOCOL_VIOLATION = "protocol_violation"
    CANCEL_UNCONFIRMED = "cancel_unconfirmed"
    STALE_GENERATION = "stale_generation"
    INTERNAL = "internal"


@dataclass(frozen=True, slots=True)
class AvatarProviderCapabilities:
    """可机器校验的 renderer-only 能力声明。"""

    render_modes: frozenset[AvatarRenderMode]
    input_codecs: frozenset[str] = frozenset({"pcm_s16le"})
    renderer_only: bool = True
    neural_lipsync: bool = True
    transactional_sentence: bool = True
    supports_cancel: bool = True
    supports_cancel_ack: bool = False
    supports_credit: bool = False
    supports_render_started: bool = False
    supports_sample_pts: bool = False
    strict_completion: bool = False
    protocol_name: str = ""
    protocol_version: int | None = None
    verification: ProviderVerification = ProviderVerification.UNVERIFIED
    extra: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        modes = frozenset(AvatarRenderMode(mode) for mode in self.render_modes)
        codecs = frozenset(str(codec).strip().lower() for codec in self.input_codecs if codec)
        if not modes:
            raise ValueError("render_modes 不能为空")
        if not codecs:
            raise ValueError("input_codecs 不能为空")
        if self.protocol_version is not None and self.protocol_version < 1:
            raise ValueError("protocol_version 必须大于等于 1")
        if not self.renderer_only:
            raise ValueError("RemoteAvatarProvider 必须是 renderer-only，不能耦合 TTS")
        object.__setattr__(self, "render_modes", modes)
        object.__setattr__(self, "input_codecs", codecs)
        object.__setattr__(self, "extra", MappingProxyType(dict(self.extra)))

    def is_mode_eligible(self, mode: AvatarRenderMode) -> bool:
        """校验指定事务模式是否具备自动承接直播句子的完整安全边界。"""
        mode = AvatarRenderMode(mode)
        common = bool(
            mode in self.render_modes
            and self.verification is ProviderVerification.VERIFIED
            and self.neural_lipsync
            and self.transactional_sentence
            and self.supports_cancel
            and self.supports_cancel_ack
            and self.strict_completion
            and "pcm_s16le" in self.input_codecs
        )
        if not common:
            return False
        if mode is AvatarRenderMode.REALTIME:
            return bool(
                self.supports_credit
                and self.supports_render_started
                and self.supports_sample_pts
            )
        return True

    @property
    def eligible_for_auto(self) -> bool:
        """至少一种声明模式满足严格自动选择要求。"""
        return any(self.is_mode_eligible(mode) for mode in self.render_modes)

    def to_dict(self) -> dict[str, Any]:
        return {
            "render_modes": sorted(mode.value for mode in self.render_modes),
            "input_codecs": sorted(self.input_codecs),
            "renderer_only": self.renderer_only,
            "neural_lipsync": self.neural_lipsync,
            "transactional_sentence": self.transactional_sentence,
            "supports_cancel": self.supports_cancel,
            "supports_cancel_ack": self.supports_cancel_ack,
            "supports_credit": self.supports_credit,
            "supports_render_started": self.supports_render_started,
            "supports_sample_pts": self.supports_sample_pts,
            "strict_completion": self.strict_completion,
            "protocol_name": self.protocol_name,
            "protocol_version": self.protocol_version,
            "verification": self.verification.value,
            "eligible_for_auto": self.eligible_for_auto,
            **dict(self.extra),
        }


@dataclass(frozen=True, slots=True)
class ProviderRenderResult:
    """单句远端事务的稳定结果；成功不代表音频已经在本地播放。"""

    provider_id: str
    request_id: str
    audio_id: str
    render_mode: AvatarRenderMode
    latency_ms: float
    output_frames: int = 0
    billed_units: int = 0
    cost_minor: int = 0
    evidence: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.provider_id or not self.request_id or not self.audio_id:
            raise ValueError("provider_id、request_id 和 audio_id 不能为空")
        if self.latency_ms < 0 or self.output_frames < 0:
            raise ValueError("latency_ms 和 output_frames 不能为负数")
        if self.billed_units < 0 or self.cost_minor < 0:
            raise ValueError("billed_units 和 cost_minor 不能为负数")
        object.__setattr__(self, "render_mode", AvatarRenderMode(self.render_mode))
        object.__setattr__(self, "evidence", MappingProxyType(dict(self.evidence)))


class ProviderError(RuntimeError):
    """携带稳定分类的 Provider 异常，保留 wire 细节但不暴露凭据。"""

    def __init__(
        self,
        code: ProviderErrorCode,
        message: str,
        *,
        provider_id: str = "",
        retryable: bool = False,
        restart_required: bool = False,
        request_id: str = "",
        details: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = ProviderErrorCode(code)
        self.provider_id = str(provider_id)
        self.retryable = bool(retryable)
        self.restart_required = bool(restart_required)
        self.request_id = str(request_id)
        self.details = MappingProxyType(dict(details or {}))

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code.value,
            "message": str(self),
            "provider_id": self.provider_id,
            "retryable": self.retryable,
            "restart_required": self.restart_required,
            "request_id": self.request_id,
            "details": dict(self.details),
        }


@runtime_checkable
class AvatarPreviewProvider(Protocol):
    """可选视频预览契约；通用 Provider 不必实现。"""

    @property
    def has_frames(self) -> bool: ...

    def get_latest_jpeg(self) -> bytes: ...

    def get_preview_status(self) -> Mapping[str, Any]: ...

    def get_media_capabilities(self) -> Mapping[str, Any]: ...


@runtime_checkable
class RemoteAvatarProvider(Protocol):
    """远端 Avatar adapter 的最小生命周期与整句渲染契约。"""

    @property
    def provider_id(self) -> str: ...

    @property
    def display_name(self) -> str: ...

    @property
    def capabilities(self) -> AvatarProviderCapabilities: ...

    async def start(self) -> None: ...

    async def render_sentence(
        self,
        frames: Sequence[AudioFrame],
        *,
        render_mode: AvatarRenderMode,
    ) -> ProviderRenderResult: ...

    async def interrupt(
        self,
        reason: str = "Barge-in",
        *,
        next_generation: int | None = None,
    ) -> None: ...

    async def stop(self) -> None: ...

    def get_status(self) -> Mapping[str, Any]: ...
