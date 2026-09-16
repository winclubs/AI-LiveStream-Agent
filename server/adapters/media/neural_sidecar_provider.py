"""将现有 renderer-only sidecar v3 驱动适配为厂商无关 Provider。"""

from __future__ import annotations

import time
import uuid
from typing import Any, Mapping, Sequence

from server.adapters.media.avatar_provider import (
    AvatarProviderCapabilities,
    AvatarRenderMode,
    ProviderError,
    ProviderErrorCode,
    ProviderRenderResult,
    ProviderVerification,
)
from server.adapters.media.neural_sidecar_driver import NeuralSidecarMediaDriver
from server.core.media.audio_frame import AudioFrame


class NeuralSidecarAvatarProvider:
    """显式 allowlist adapter；不接受动态模块名或任意厂商协议。"""

    def __init__(
        self,
        provider_id: str,
        display_name: str,
        driver: NeuralSidecarMediaDriver,
    ) -> None:
        provider_id = str(provider_id).strip()
        if not provider_id:
            raise ValueError("provider_id 不能为空")
        self._provider_id = provider_id
        self._display_name = str(display_name).strip() or provider_id
        self.driver = driver
        self._capabilities = AvatarProviderCapabilities(
            render_modes=frozenset({AvatarRenderMode.REALTIME}),
            input_codecs=frozenset({"pcm_s16le"}),
            renderer_only=True,
            neural_lipsync=driver.require_neural_lipsync,
            transactional_sentence=True,
            supports_cancel=True,
            supports_cancel_ack=True,
            supports_credit=True,
            supports_render_started=True,
            supports_sample_pts=True,
            strict_completion=True,
            protocol_name="neural_sidecar_v3",
            protocol_version=3,
            verification=(
                ProviderVerification.VERIFIED
                if driver.require_neural_lipsync
                else ProviderVerification.UNVERIFIED
            ),
            extra={"adapter": "sidecar_v3", "max_safe_concurrency": 1},
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
    def has_frames(self) -> bool:
        return self.driver.has_frames

    def get_latest_jpeg(self) -> bytes:
        return self.driver.get_latest_jpeg()

    def get_preview_status(self) -> dict[str, Any]:
        return self.driver.get_preview_status()

    def get_media_capabilities(self) -> dict[str, Any]:
        return self.driver.get_media_capabilities()

    async def start(self) -> None:
        await self.driver.start()
        if not self.driver.is_ready:
            raise ProviderError(
                ProviderErrorCode.REMOTE_UNAVAILABLE,
                self.driver.last_error or "sidecar v3 握手未就绪",
                provider_id=self.provider_id,
                retryable=True,
            )

    async def render_sentence(
        self,
        frames: Sequence[AudioFrame],
        *,
        render_mode: AvatarRenderMode,
    ) -> ProviderRenderResult:
        if render_mode is not AvatarRenderMode.REALTIME:
            raise ProviderError(
                ProviderErrorCode.CAPABILITY_MISMATCH,
                "sidecar v3 adapter 只支持 realtime 模式",
                provider_id=self.provider_id,
            )
        started_at = time.monotonic()
        before_frames = self.driver.frames_received
        request_id = uuid.uuid4().hex
        try:
            await self.driver.feed_audio_frames(frames)
        except ProviderError as exc:
            if exc.provider_id:
                raise
            raise ProviderError(
                exc.code,
                str(exc),
                provider_id=self.provider_id,
                retryable=exc.retryable,
                restart_required=exc.restart_required,
                request_id=exc.request_id or request_id,
                details=exc.details,
            ) from exc
        except (TimeoutError, OSError) as exc:
            raise ProviderError(
                ProviderErrorCode.REMOTE_UNAVAILABLE,
                str(exc),
                provider_id=self.provider_id,
                retryable=True,
                request_id=request_id,
            ) from exc
        except Exception as exc:
            raise ProviderError(
                ProviderErrorCode.PROTOCOL_VIOLATION,
                str(exc),
                provider_id=self.provider_id,
                restart_required=True,
                request_id=request_id,
            ) from exc

        first = frames[0]
        media = self.driver.get_media_capabilities()
        evidence = {
            key: media.get(key)
            for key in (
                "backend_id",
                "model_version",
                "weights_sha256",
                "license_manifest_sha256",
                "avatar_revision",
                "avatar_digest",
                "node_version",
            )
            if media.get(key) is not None
        }
        return ProviderRenderResult(
            provider_id=self.provider_id,
            request_id=request_id,
            audio_id=first.audio_id,
            render_mode=render_mode,
            latency_ms=(time.monotonic() - started_at) * 1000.0,
            output_frames=max(0, self.driver.frames_received - before_frames),
            evidence=evidence,
        )

    async def interrupt(
        self,
        reason: str = "Barge-in",
        *,
        next_generation: int | None = None,
    ) -> None:
        had_request = self.driver.has_active_request
        await self.driver.interrupt(reason, next_generation=next_generation)
        if had_request and self.driver.last_cancel_acknowledged is not True:
            raise ProviderError(
                ProviderErrorCode.CANCEL_UNCONFIRMED,
                "sidecar 未返回 cancel_ack=true 且 quiesced=true",
                provider_id=self.provider_id,
                restart_required=True,
            )

    async def stop(self) -> None:
        await self.driver.stop()

    def get_status(self) -> Mapping[str, Any]:
        return self.driver.get_preview_status()
