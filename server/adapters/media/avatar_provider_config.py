"""Avatar Provider 策略解析、校验与敏感字段清理。"""

from __future__ import annotations

from typing import Any, Mapping

from server.adapters.media.avatar_orchestrator import (
    AvatarProviderPolicy,
    BillingUnit,
    CircuitBreakerPolicy,
    ProviderTimeouts,
    QuotaPolicy,
)
from server.adapters.media.avatar_provider import AvatarRenderMode, ProviderMode

_SENSITIVE_EXACT = {
    "api_key",
    "apikey",
    "token",
    "auth_token",
    "access_token",
    "secret",
    "secret_key",
    "client_secret",
    "password",
    "credential",
    "credentials",
    "authorization",
    "private_key",
}
_SENSITIVE_SUFFIXES = (
    "_api_key",
    "_token",
    "_secret",
    "_password",
    "_credential",
    "_credentials",
    "_authorization",
    "_private_key",
)


def is_sensitive_key(key: object) -> bool:
    normalized = str(key).strip().lower().replace("-", "_")
    return bool(
        normalized in _SENSITIVE_EXACT
        or normalized.endswith(_SENSITIVE_SUFFIXES)
        or normalized.startswith(("secret_", "password_"))
    )


def find_sensitive_paths(value: object, prefix: str = "extra_params") -> list[str]:
    paths: list[str] = []
    if isinstance(value, Mapping):
        for key, nested in value.items():
            path = f"{prefix}.{key}"
            if is_sensitive_key(key):
                if nested not in (None, ""):
                    paths.append(path)
            else:
                paths.extend(find_sensitive_paths(nested, path))
    elif isinstance(value, (list, tuple)):
        for index, nested in enumerate(value):
            paths.extend(find_sensitive_paths(nested, f"{prefix}[{index}]"))
    return paths


def redact_sensitive(value: object) -> object:
    if isinstance(value, Mapping):
        # 直接省略历史敏感键，避免占位符被旧客户端回传后再次持久化。
        return {
            str(key): redact_sensitive(nested)
            for key, nested in value.items()
            if not is_sensitive_key(key)
        }
    if isinstance(value, list):
        return [redact_sensitive(item) for item in value]
    if isinstance(value, tuple):
        return [redact_sensitive(item) for item in value]
    return value


def _mapping(value: object, field: str) -> Mapping[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ValueError(f"{field} 必须是 object")
    return value


def _int(value: object, default: int, field: str) -> int:
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise ValueError(f"{field} 必须是整数")
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} 必须是整数") from exc


def _float(value: object, default: float, field: str) -> float:
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise ValueError(f"{field} 必须是数字")
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} 必须是数字") from exc


def parse_avatar_provider_policy(
    extra_params: Mapping[str, Any],
    *,
    max_safe_concurrency: int = 1,
) -> AvatarProviderPolicy:
    raw = _mapping(extra_params.get("avatar_provider"), "avatar_provider")
    timeouts = _mapping(raw.get("timeouts"), "avatar_provider.timeouts")
    circuit = _mapping(raw.get("circuit_breaker"), "avatar_provider.circuit_breaker")
    quota = _mapping(raw.get("quota"), "avatar_provider.quota")
    max_concurrency = _int(
        raw.get("max_concurrency"), 1, "avatar_provider.max_concurrency"
    )
    if max_concurrency > max_safe_concurrency:
        raise ValueError(
            f"当前 adapter 最大安全并发为 {max_safe_concurrency}，收到 {max_concurrency}"
        )
    render_mode_raw = raw.get("render_mode")
    render_mode = AvatarRenderMode(render_mode_raw) if render_mode_raw else None
    budget_raw = quota.get("budget_minor")
    budget_minor = (
        None
        if budget_raw is None
        else _int(budget_raw, 0, "avatar_provider.quota.budget_minor")
    )
    return AvatarProviderPolicy(
        mode=ProviderMode(raw.get("mode", ProviderMode.PRIMARY.value)),
        priority=_int(raw.get("priority"), 100, "avatar_provider.priority"),
        max_concurrency=max_concurrency,
        render_mode=render_mode,
        timeouts=ProviderTimeouts(
            connect_seconds=_float(
                timeouts.get("connect_ms"), 1000.0, "avatar_provider.timeouts.connect_ms"
            )
            / 1000.0,
            message_seconds=_float(
                timeouts.get("message_ms"), 20000.0, "avatar_provider.timeouts.message_ms"
            )
            / 1000.0,
            request_seconds=_float(
                timeouts.get("request_ms"), 120000.0, "avatar_provider.timeouts.request_ms"
            )
            / 1000.0,
        ),
        circuit_breaker=CircuitBreakerPolicy(
            failure_threshold=_int(
                circuit.get("failure_threshold"),
                3,
                "avatar_provider.circuit_breaker.failure_threshold",
            ),
            open_seconds=_float(
                circuit.get("open_ms"),
                30000.0,
                "avatar_provider.circuit_breaker.open_ms",
            )
            / 1000.0,
            half_open_max_calls=_int(
                circuit.get("half_open_max_calls"),
                1,
                "avatar_provider.circuit_breaker.half_open_max_calls",
            ),
        ),
        quota=QuotaPolicy(
            currency=str(quota.get("currency") or "CNY"),
            budget_minor=budget_minor,
            warning_ratio=_float(
                quota.get("warning_ratio"),
                0.8,
                "avatar_provider.quota.warning_ratio",
            ),
            hard_limit=quota.get("hard_limit") is not False,
            billing_unit=BillingUnit(quota.get("billing_unit", BillingUnit.REQUEST.value)),
            unit_cost_minor=_int(
                quota.get("unit_cost_minor"),
                0,
                "avatar_provider.quota.unit_cost_minor",
            ),
            charge_failed_attempts=quota.get("charge_failed_attempts") is not False,
        ),
    )


def avatar_provider_policy_to_dict(policy: AvatarProviderPolicy) -> dict[str, Any]:
    return {
        "mode": policy.mode.value,
        "priority": policy.priority,
        "max_concurrency": policy.max_concurrency,
        "render_mode": policy.render_mode.value if policy.render_mode else None,
        "timeouts": {
            "connect_ms": round(policy.timeouts.connect_seconds * 1000),
            "message_ms": round(policy.timeouts.message_seconds * 1000),
            "request_ms": round(policy.timeouts.request_seconds * 1000),
        },
        "circuit_breaker": {
            "failure_threshold": policy.circuit_breaker.failure_threshold,
            "open_ms": round(policy.circuit_breaker.open_seconds * 1000),
            "half_open_max_calls": policy.circuit_breaker.half_open_max_calls,
        },
        "quota": {
            "currency": policy.quota.currency,
            "budget_minor": policy.quota.budget_minor,
            "warning_ratio": policy.quota.warning_ratio,
            "hard_limit": policy.quota.hard_limit,
            "billing_unit": policy.quota.billing_unit.value,
            "unit_cost_minor": policy.quota.unit_cost_minor,
            "charge_failed_attempts": policy.quota.charge_failed_attempts,
        },
    }
