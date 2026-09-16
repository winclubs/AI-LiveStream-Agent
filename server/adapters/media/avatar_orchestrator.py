"""远端 Avatar Provider 的句级自动选择与运营保护。

本模块不拥有程序化渲染器。调用方必须先把同一批 AudioFrame 投递给本地 shadow，
再调用 :meth:`AvatarProviderOrchestrator.dispatch_sentence`。远端失败不会在句中迁移，
下一句才会重新选择 Provider。
"""

from __future__ import annotations

import asyncio
import math
import threading
import time
from collections import Counter
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Iterable, Mapping, Sequence

from server.adapters.media.avatar_provider import (
    AvatarProviderCapabilities,
    AvatarRenderMode,
    ProviderError,
    ProviderErrorCode,
    ProviderMode,
    ProviderRenderResult,
    RemoteAvatarProvider,
)
from server.core.media.audio_frame import AudioFrame, validate_audio_frame_batch


class CircuitState(StrEnum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class BillingUnit(StrEnum):
    REQUEST = "request"
    AUDIO_SECOND = "audio_second"
    AUDIO_MINUTE = "audio_minute"
    CHARACTER = "character"


@dataclass(frozen=True, slots=True)
class ProviderTimeouts:
    connect_seconds: float = 1.0
    message_seconds: float = 20.0
    request_seconds: float = 120.0

    def __post_init__(self) -> None:
        for name, value in (
            ("connect_seconds", self.connect_seconds),
            ("message_seconds", self.message_seconds),
            ("request_seconds", self.request_seconds),
        ):
            if not math.isfinite(value) or value <= 0:
                raise ValueError(f"{name} 必须是正有限数")
        if self.connect_seconds > self.request_seconds:
            raise ValueError("connect_seconds 不能大于 request_seconds")
        if self.message_seconds > self.request_seconds:
            raise ValueError("message_seconds 不能大于 request_seconds")


@dataclass(frozen=True, slots=True)
class CircuitBreakerPolicy:
    failure_threshold: int = 3
    open_seconds: float = 30.0
    half_open_max_calls: int = 1

    def __post_init__(self) -> None:
        if self.failure_threshold < 1:
            raise ValueError("failure_threshold 必须大于等于 1")
        if not math.isfinite(self.open_seconds) or self.open_seconds <= 0:
            raise ValueError("open_seconds 必须是正有限数")
        if self.half_open_max_calls < 1:
            raise ValueError("half_open_max_calls 必须大于等于 1")


@dataclass(frozen=True, slots=True)
class QuotaPolicy:
    """单进程运行期预算；不替代厂商账单，重启后需由配置重新提供预算。"""

    currency: str = "CNY"
    budget_minor: int | None = None
    warning_ratio: float = 0.8
    hard_limit: bool = True
    billing_unit: BillingUnit = BillingUnit.REQUEST
    unit_cost_minor: int = 0
    charge_failed_attempts: bool = True

    def __post_init__(self) -> None:
        currency = str(self.currency).strip().upper()
        if not currency or len(currency) > 8:
            raise ValueError("currency 必须是 1~8 个字符的币种代码")
        if self.budget_minor is not None and self.budget_minor < 0:
            raise ValueError("budget_minor 不能为负数")
        if not math.isfinite(self.warning_ratio) or not 0 <= self.warning_ratio <= 1:
            raise ValueError("warning_ratio 必须位于 0~1")
        if self.unit_cost_minor < 0:
            raise ValueError("unit_cost_minor 不能为负数")
        object.__setattr__(self, "currency", currency)
        object.__setattr__(self, "billing_unit", BillingUnit(self.billing_unit))

    def estimate(self, frames: Sequence[AudioFrame]) -> tuple[int, int]:
        first = frames[0]
        duration_samples = sum(frame.duration_samples for frame in frames)
        duration_seconds = duration_samples / first.format.sample_rate
        if self.billing_unit is BillingUnit.REQUEST:
            units = 1
        elif self.billing_unit is BillingUnit.AUDIO_SECOND:
            units = max(1, math.ceil(duration_seconds))
        elif self.billing_unit is BillingUnit.AUDIO_MINUTE:
            units = max(1, math.ceil(duration_seconds / 60.0))
        else:
            units = max(1, len(first.text))
        return units, units * self.unit_cost_minor


@dataclass(frozen=True, slots=True)
class AvatarProviderPolicy:
    mode: ProviderMode = ProviderMode.PRIMARY
    priority: int = 100
    max_concurrency: int = 1
    render_mode: AvatarRenderMode | None = None
    timeouts: ProviderTimeouts = field(default_factory=ProviderTimeouts)
    circuit_breaker: CircuitBreakerPolicy = field(default_factory=CircuitBreakerPolicy)
    quota: QuotaPolicy = field(default_factory=QuotaPolicy)

    def __post_init__(self) -> None:
        if not -1_000_000 <= self.priority <= 1_000_000:
            raise ValueError("priority 超出允许范围")
        if not 1 <= self.max_concurrency <= 64:
            raise ValueError("max_concurrency 必须位于 1~64")
        object.__setattr__(self, "mode", ProviderMode(self.mode))
        if self.render_mode is not None:
            object.__setattr__(self, "render_mode", AvatarRenderMode(self.render_mode))


@dataclass(frozen=True, slots=True)
class AvatarProviderEntry:
    provider: RemoteAvatarProvider
    policy: AvatarProviderPolicy = field(default_factory=AvatarProviderPolicy)

    def __post_init__(self) -> None:
        if not self.provider.provider_id.strip():
            raise ValueError("provider_id 不能为空")


@dataclass(frozen=True, slots=True)
class AvatarDispatchOutcome:
    """编排结果；fallback_reason 非空表示调用方继续使用已准备好的 shadow。"""

    audio_id: str
    provider_id: str = ""
    result: ProviderRenderResult | None = None
    error: ProviderError | None = None
    fallback_reason: str = ""

    @property
    def used_remote(self) -> bool:
        return self.result is not None and not self.fallback_reason


@dataclass(slots=True)
class _ProviderRuntime:
    entry: AvatarProviderEntry
    circuit_state: CircuitState = CircuitState.CLOSED
    consecutive_failures: int = 0
    opened_at: float = 0.0
    in_flight: int = 0
    half_open_in_flight: int = 0
    spent_minor: int = 0
    reserved_minor: int = 0
    lifecycle_started: bool = False
    last_selected_at: float = 0.0
    last_success_at: float = 0.0
    last_failure_at: float = 0.0
    last_latency_ms: float = 0.0
    last_error: ProviderError | None = None
    requests_total: int = 0
    outcomes: Counter[str] = field(default_factory=Counter)


@dataclass(frozen=True, slots=True)
class _Reservation:
    runtime: _ProviderRuntime
    estimated_units: int
    estimated_cost_minor: int
    render_mode: AvatarRenderMode
    was_half_open: bool


class AvatarProviderOrchestrator:
    """无排队的句级 Provider 编排器。"""

    def __init__(self, entries: Iterable[AvatarProviderEntry] = ()) -> None:
        runtimes: dict[str, _ProviderRuntime] = {}
        for entry in entries:
            provider_id = entry.provider.provider_id.strip()
            if provider_id in runtimes:
                raise ValueError(f"重复 provider_id={provider_id!r}")
            runtimes[provider_id] = _ProviderRuntime(entry=entry)
        self._runtimes = runtimes
        self._lock = threading.RLock()
        self._running = False
        self._active_provider_ids: set[str] = set()
        self._last_selected_provider_id = ""
        self._requests_total = 0
        self._outcomes: Counter[str] = Counter()
        self._lifecycle_tasks: set[asyncio.Task] = set()

    @property
    def provider_count(self) -> int:
        return len(self._runtimes)

    @staticmethod
    def _mode_rank(mode: ProviderMode) -> int:
        return 0 if mode is ProviderMode.PRIMARY else 1

    @staticmethod
    def _choose_render_mode(
        capabilities: AvatarProviderCapabilities,
        policy: AvatarProviderPolicy,
    ) -> AvatarRenderMode | None:
        if policy.render_mode is not None:
            return (
                policy.render_mode
                if capabilities.is_mode_eligible(policy.render_mode)
                else None
            )
        if capabilities.is_mode_eligible(AvatarRenderMode.REALTIME):
            return AvatarRenderMode.REALTIME
        if capabilities.is_mode_eligible(AvatarRenderMode.BATCH):
            return AvatarRenderMode.BATCH
        return None

    @staticmethod
    def _is_circuit_failure(error: ProviderError) -> bool:
        return error.code not in {
            ProviderErrorCode.CONCURRENCY_LIMIT,
            ProviderErrorCode.QUOTA_EXHAUSTED,
            ProviderErrorCode.STALE_GENERATION,
            ProviderErrorCode.INVALID_REQUEST,
        }

    def _refresh_circuit_locked(self, runtime: _ProviderRuntime, now: float) -> None:
        policy = runtime.entry.policy.circuit_breaker
        if (
            runtime.circuit_state is CircuitState.OPEN
            and now - runtime.opened_at >= policy.open_seconds
        ):
            runtime.circuit_state = CircuitState.HALF_OPEN
            runtime.half_open_in_flight = 0

    def _quota_allows_locked(self, runtime: _ProviderRuntime, estimated_cost: int) -> bool:
        quota = runtime.entry.policy.quota
        if not quota.hard_limit or quota.budget_minor is None:
            return True
        committed = runtime.spent_minor + runtime.reserved_minor
        return committed + estimated_cost <= quota.budget_minor

    def _reserve_locked(self, frames: Sequence[AudioFrame]) -> _Reservation | None:
        # 视频输出共享同一时间线和虚拟摄像头，跨 Provider 也只允许一个整句在途。
        if self._active_provider_ids:
            return None
        now = time.monotonic()
        candidates: list[tuple[int, int, str, _ProviderRuntime, AvatarRenderMode, int, int]] = []
        for provider_id, runtime in self._runtimes.items():
            policy = runtime.entry.policy
            capabilities = runtime.entry.provider.capabilities
            if policy.mode in {ProviderMode.DISABLED, ProviderMode.SHADOW}:
                continue
            if not runtime.lifecycle_started:
                continue
            if not capabilities.eligible_for_auto:
                continue
            render_mode = self._choose_render_mode(capabilities, policy)
            if render_mode is None:
                continue
            self._refresh_circuit_locked(runtime, now)
            if runtime.circuit_state is CircuitState.OPEN:
                continue
            if runtime.in_flight >= policy.max_concurrency:
                continue
            if (
                runtime.circuit_state is CircuitState.HALF_OPEN
                and runtime.half_open_in_flight
                >= policy.circuit_breaker.half_open_max_calls
            ):
                continue
            units, estimated_cost = policy.quota.estimate(frames)
            if not self._quota_allows_locked(runtime, estimated_cost):
                continue
            candidates.append(
                (
                    self._mode_rank(policy.mode),
                    policy.priority,
                    provider_id,
                    runtime,
                    render_mode,
                    units,
                    estimated_cost,
                )
            )
        if not candidates:
            return None

        _, _, provider_id, runtime, render_mode, units, estimated_cost = min(
            candidates, key=lambda item: item[:3]
        )
        was_half_open = runtime.circuit_state is CircuitState.HALF_OPEN
        runtime.in_flight += 1
        runtime.requests_total += 1
        runtime.reserved_minor += estimated_cost
        runtime.last_selected_at = now
        if was_half_open:
            runtime.half_open_in_flight += 1
        self._active_provider_ids.add(provider_id)
        self._last_selected_provider_id = provider_id
        self._requests_total += 1
        return _Reservation(runtime, units, estimated_cost, render_mode, was_half_open)

    def _complete_locked(
        self,
        reservation: _Reservation,
        *,
        outcome: str,
        latency_ms: float,
        result: ProviderRenderResult | None = None,
        error: ProviderError | None = None,
        cancelled: bool = False,
    ) -> None:
        runtime = reservation.runtime
        provider_id = runtime.entry.provider.provider_id
        runtime.in_flight = max(0, runtime.in_flight - 1)
        runtime.reserved_minor = max(
            0, runtime.reserved_minor - reservation.estimated_cost_minor
        )
        if reservation.was_half_open:
            runtime.half_open_in_flight = max(0, runtime.half_open_in_flight - 1)
        if runtime.in_flight == 0:
            self._active_provider_ids.discard(provider_id)
        runtime.last_latency_ms = max(0.0, latency_ms)
        runtime.outcomes[outcome] += 1
        self._outcomes[outcome] += 1

        quota = runtime.entry.policy.quota
        if result is not None:
            actual_cost = result.cost_minor
            if actual_cost == 0 and result.billed_units == 0:
                actual_cost = reservation.estimated_cost_minor
            runtime.spent_minor += max(0, actual_cost)
        elif (error is not None or cancelled) and quota.charge_failed_attempts:
            runtime.spent_minor += reservation.estimated_cost_minor

        if cancelled:
            return
        now = time.monotonic()
        if error is None:
            runtime.circuit_state = CircuitState.CLOSED
            runtime.consecutive_failures = 0
            runtime.last_success_at = now
            runtime.last_error = None
            return

        runtime.last_failure_at = now
        runtime.last_error = error
        if not self._is_circuit_failure(error):
            return
        runtime.consecutive_failures += 1
        threshold = runtime.entry.policy.circuit_breaker.failure_threshold
        if runtime.circuit_state is CircuitState.HALF_OPEN or runtime.consecutive_failures >= threshold:
            runtime.circuit_state = CircuitState.OPEN
            runtime.opened_at = now
            runtime.half_open_in_flight = 0

    async def _start_runtime(self, runtime: _ProviderRuntime) -> bool:
        try:
            async with asyncio.timeout(runtime.entry.policy.timeouts.connect_seconds):
                await runtime.entry.provider.start()
        except asyncio.CancelledError:
            raise
        except TimeoutError as exc:
            error = ProviderError(
                ProviderErrorCode.CONNECT_TIMEOUT,
                "Avatar Provider 启动/握手超时",
                provider_id=runtime.entry.provider.provider_id,
                retryable=True,
            )
            with self._lock:
                runtime.last_error = error
                runtime.last_failure_at = time.monotonic()
                runtime.outcomes["start_error"] += 1
                self._outcomes["start_error"] += 1
            return False
        except Exception as exc:
            error = self._normalize_error(runtime, exc)
            with self._lock:
                runtime.last_error = error
                runtime.last_failure_at = time.monotonic()
                runtime.outcomes["start_error"] += 1
                self._outcomes["start_error"] += 1
            return False
        with self._lock:
            if self._running:
                runtime.lifecycle_started = True
                runtime.last_error = None
                return True
        # stop 与启动完成竞态：不能在编排器停止后提交 late lifecycle。
        await runtime.entry.provider.stop()
        return False

    async def _retry_runtime_start(self, runtime: _ProviderRuntime) -> None:
        delay = runtime.entry.policy.circuit_breaker.open_seconds
        while True:
            await asyncio.sleep(delay)
            with self._lock:
                if not self._running or runtime.lifecycle_started:
                    return
            if await self._start_runtime(runtime):
                return

    async def start(self) -> None:
        """独立启动各 Provider；失败节点按熔断冷却时间后台重试。"""
        with self._lock:
            if self._running:
                return
            self._running = True
            runtimes = [
                runtime
                for runtime in self._runtimes.values()
                if runtime.entry.policy.mode not in {ProviderMode.DISABLED, ProviderMode.SHADOW}
            ]

        results = await asyncio.gather(
            *(self._start_runtime(runtime) for runtime in runtimes)
        )
        with self._lock:
            if not self._running:
                return
            for runtime, started in zip(runtimes, results):
                if started:
                    continue
                task = asyncio.create_task(self._retry_runtime_start(runtime))
                self._lifecycle_tasks.add(task)
                task.add_done_callback(self._lifecycle_tasks.discard)

    def _normalize_error(
        self, runtime: _ProviderRuntime, exc: BaseException
    ) -> ProviderError:
        provider_id = runtime.entry.provider.provider_id
        if isinstance(exc, ProviderError):
            if exc.provider_id:
                return exc
            return ProviderError(
                exc.code,
                str(exc),
                provider_id=provider_id,
                retryable=exc.retryable,
                restart_required=exc.restart_required,
                request_id=exc.request_id,
                details=exc.details,
            )
        return ProviderError(
            ProviderErrorCode.INTERNAL,
            f"{type(exc).__name__}: {exc}",
            provider_id=provider_id,
            retryable=True,
        )

    async def dispatch_sentence(
        self, frames: Sequence[AudioFrame]
    ) -> AvatarDispatchOutcome:
        """最多调用一个 Provider；任何失败都立即保留 procedural shadow。"""
        frame_batch = validate_audio_frame_batch(frames)
        first = frame_batch[0]
        with self._lock:
            if not self._running:
                self._outcomes["not_running"] += 1
                return AvatarDispatchOutcome(
                    audio_id=first.audio_id,
                    fallback_reason="orchestrator_not_running",
                )
            reservation = self._reserve_locked(frame_batch)
            if reservation is None:
                self._outcomes["no_candidate"] += 1
                return AvatarDispatchOutcome(
                    audio_id=first.audio_id,
                    fallback_reason="no_eligible_provider",
                )

        runtime = reservation.runtime
        provider = runtime.entry.provider
        started_at = time.monotonic()
        try:
            async with asyncio.timeout(runtime.entry.policy.timeouts.request_seconds):
                result = await provider.render_sentence(
                    frame_batch, render_mode=reservation.render_mode
                )
            if result.provider_id != provider.provider_id or result.audio_id != first.audio_id:
                raise ProviderError(
                    ProviderErrorCode.PROTOCOL_VIOLATION,
                    "Provider 返回的 provider_id 或 audio_id 与当前事务不一致",
                    provider_id=provider.provider_id,
                    request_id=result.request_id,
                    restart_required=True,
                )
        except asyncio.CancelledError:
            latency_ms = (time.monotonic() - started_at) * 1000.0
            with self._lock:
                self._complete_locked(
                    reservation,
                    outcome="cancelled",
                    latency_ms=latency_ms,
                    cancelled=True,
                )
            raise
        except TimeoutError:
            try:
                async with asyncio.timeout(
                    min(2.0, runtime.entry.policy.timeouts.message_seconds)
                ):
                    await provider.interrupt("Provider request timeout")
            except Exception:
                pass
            error = ProviderError(
                ProviderErrorCode.REQUEST_TIMEOUT,
                "远端 Avatar Provider 整句请求超时",
                provider_id=provider.provider_id,
                retryable=True,
            )
            latency_ms = (time.monotonic() - started_at) * 1000.0
            with self._lock:
                self._complete_locked(
                    reservation,
                    outcome="timeout",
                    latency_ms=latency_ms,
                    error=error,
                )
            return AvatarDispatchOutcome(
                audio_id=first.audio_id,
                provider_id=provider.provider_id,
                error=error,
                fallback_reason=error.code.value,
            )
        except Exception as exc:
            error = self._normalize_error(runtime, exc)
            latency_ms = (time.monotonic() - started_at) * 1000.0
            with self._lock:
                self._complete_locked(
                    reservation,
                    outcome="provider_error",
                    latency_ms=latency_ms,
                    error=error,
                )
            return AvatarDispatchOutcome(
                audio_id=first.audio_id,
                provider_id=provider.provider_id,
                error=error,
                fallback_reason=error.code.value,
            )

        latency_ms = (time.monotonic() - started_at) * 1000.0
        with self._lock:
            self._complete_locked(
                reservation,
                outcome="success",
                latency_ms=latency_ms,
                result=result,
            )
        return AvatarDispatchOutcome(
            audio_id=first.audio_id,
            provider_id=provider.provider_id,
            result=result,
        )

    async def interrupt(
        self, reason: str = "Barge-in", *, next_generation: int | None = None
    ) -> None:
        with self._lock:
            selected = [
                (self._runtimes[provider_id], self._runtimes[provider_id].entry.provider)
                for provider_id in sorted(self._active_provider_ids)
            ]
        results = await asyncio.gather(
            *(
                provider.interrupt(reason, next_generation=next_generation)
                for _, provider in selected
            ),
            return_exceptions=True,
        )
        now = time.monotonic()
        with self._lock:
            for (runtime, _), result in zip(selected, results):
                if not isinstance(result, BaseException):
                    continue
                error = self._normalize_error(runtime, result)
                runtime.last_error = error
                runtime.last_failure_at = now
                runtime.outcomes["interrupt_error"] += 1
                self._outcomes["interrupt_error"] += 1
                # 未证明远端静止时禁止复用该节点，直接打开熔断器。
                if error.code is ProviderErrorCode.CANCEL_UNCONFIRMED:
                    runtime.circuit_state = CircuitState.OPEN
                    runtime.opened_at = now
                    runtime.consecutive_failures = max(
                        runtime.consecutive_failures,
                        runtime.entry.policy.circuit_breaker.failure_threshold,
                    )

    async def stop(self) -> None:
        with self._lock:
            if not self._running:
                return
            self._running = False
            lifecycle_tasks = list(self._lifecycle_tasks)
            providers = [
                runtime.entry.provider
                for runtime in self._runtimes.values()
                if runtime.entry.policy.mode
                not in {ProviderMode.DISABLED, ProviderMode.SHADOW}
            ]
        for task in lifecycle_tasks:
            task.cancel()
        if lifecycle_tasks:
            await asyncio.gather(*lifecycle_tasks, return_exceptions=True)
        await self.interrupt("Orchestrator stop")
        stop_results = await asyncio.gather(
            *(provider.stop() for provider in providers), return_exceptions=True
        )
        with self._lock:
            self._lifecycle_tasks.clear()
            self._active_provider_ids.clear()
            for runtime in self._runtimes.values():
                runtime.lifecycle_started = False
                runtime.in_flight = 0
                runtime.half_open_in_flight = 0
                runtime.reserved_minor = 0
        stop_errors = [result for result in stop_results if isinstance(result, BaseException)]
        if stop_errors:
            raise RuntimeError(
                f"{len(stop_errors)} 个 Avatar Provider 未确认停止"
            ) from stop_errors[0]

    def get_last_selected_provider(self) -> RemoteAvatarProvider | None:
        with self._lock:
            runtime = self._runtimes.get(self._last_selected_provider_id)
            return runtime.entry.provider if runtime is not None else None

    def get_snapshot(self) -> dict[str, Any]:
        """返回无密钥、低基数字段的同步状态快照。"""
        now = time.monotonic()
        with self._lock:
            providers: list[dict[str, Any]] = []
            for provider_id, runtime in sorted(self._runtimes.items()):
                self._refresh_circuit_locked(runtime, now)
                entry = runtime.entry
                policy = entry.policy
                quota = policy.quota
                budget = quota.budget_minor
                committed = runtime.spent_minor + runtime.reserved_minor
                remaining = None if budget is None else max(0, budget - committed)
                warning = bool(
                    budget is not None
                    and budget > 0
                    and committed / budget >= quota.warning_ratio
                )
                render_mode = self._choose_render_mode(entry.provider.capabilities, policy)
                ready = bool(
                    self._running
                    and runtime.lifecycle_started
                    and policy.mode not in {ProviderMode.DISABLED, ProviderMode.SHADOW}
                    and entry.provider.capabilities.eligible_for_auto
                    and render_mode is not None
                    and runtime.circuit_state is not CircuitState.OPEN
                    and runtime.in_flight < policy.max_concurrency
                    and (
                        not quota.hard_limit
                        or budget is None
                        or quota.unit_cost_minor == 0
                        or (remaining is not None and remaining > 0)
                    )
                )
                try:
                    adapter_status: Mapping[str, Any] = entry.provider.get_status()
                except Exception as exc:
                    adapter_status = {"status_error": type(exc).__name__}
                providers.append(
                    {
                        "provider_id": provider_id,
                        "display_name": entry.provider.display_name,
                        "mode": policy.mode.value,
                        "priority": policy.priority,
                        "render_mode": render_mode.value if render_mode else None,
                        "up": runtime.lifecycle_started,
                        "ready": ready,
                        "selected": provider_id in self._active_provider_ids,
                        "in_flight": runtime.in_flight,
                        "max_concurrency": policy.max_concurrency,
                        "circuit_state": runtime.circuit_state.value,
                        "consecutive_failures": runtime.consecutive_failures,
                        "requests_total": runtime.requests_total,
                        "outcomes": dict(runtime.outcomes),
                        "last_latency_ms": round(runtime.last_latency_ms, 3),
                        "last_error": (
                            runtime.last_error.to_dict() if runtime.last_error else None
                        ),
                        "quota": {
                            "currency": quota.currency,
                            "budget_minor": budget,
                            "spent_minor": runtime.spent_minor,
                            "reserved_minor": runtime.reserved_minor,
                            "remaining_minor": remaining,
                            "warning": warning,
                            "hard_limit": quota.hard_limit,
                            "billing_unit": quota.billing_unit.value,
                            "unit_cost_minor": quota.unit_cost_minor,
                        },
                        "capabilities": entry.provider.capabilities.to_dict(),
                        "adapter": dict(adapter_status),
                        "play_stream_addr": getattr(entry.provider, "play_stream_addr", ""),
                    }
                )
            return {
                "running": self._running,
                "strategy": "sentence_pinned_auto",
                "procedural_shadow": True,
                "same_sentence_failover": False,
                "provider_count": len(providers),
                "last_selected_provider_id": self._last_selected_provider_id,
                "requests_total": self._requests_total,
                "outcomes": dict(self._outcomes),
                "providers": providers,
            }
