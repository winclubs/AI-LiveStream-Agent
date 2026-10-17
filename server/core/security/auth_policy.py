# -*- coding: utf-8 -*-
"""
鉴权策略单一事实来源 (server.core.security.auth_policy)

存在理由（上线诊断 BLOCKER-1）：
历史上 HTTP 中间件 (`server/app.py`) 与 WebSocket 守卫
(`server/core/security/ws_auth.py`) 各自实现了一份鉴权判定逻辑，
且 **HTTP 侧缺少 `LIVE_AGENT_MODE` 商业档位判断**，导致：

    服务绑定 0.0.0.0 + 未配置 API_AUTH_TOKEN + 未开 ENFORCE_REMOTE_AUTH
        -> WebSocket 端点被 1008 拒绝（安全）
        -> 整个 REST API 完全裸奔（不安全，含凭据配置与开播控制）

HTTP 反而成了比 WebSocket 更弱的入口。本次把判定收敛到本模块，
两个入口共用同一函数，从结构上消除再次漂移的可能。

判定顺序（与 ws_auth 历史行为保持一致，勿轻易调整）：
    1. 回环免密仅在「非商业档位」成立；
    2. 配置了令牌则一律校验；
    3. 未配置令牌时，商业档位或强制拦截开启 -> 拒绝，否则放行并告警。
"""
from __future__ import annotations

import os
from typing import Any

#: 商业档位后必须校验令牌的运行模式
COMMERCIAL_MODES = frozenset({"PILOT", "PRODUCTION"})

#: 视为本机零摩擦回环的客户端标识（TestClient 以 "testclient" 身份出现）
LOCAL_CLIENT_HOSTS = frozenset({"127.0.0.1", "::1", "localhost", "testclient"})

_TRUTHY = ("1", "true", "yes")

#: 参与鉴权裁决的请求头键位（显式收敛：鉴权只应读取这几项）
_HEADER_KEYS = ("Authorization", "X-API-Key", "access_token")

#: header/cookie 访问器类型别名。
#:
#: 这里刻意用 ``Any`` 而非 Protocol：Starlette 的 ``Headers.get`` 是**重载
#: 签名**，结构化子类型无法匹配单一 Protocol 方法签名，会产生无意义的
#: 类型报错噪音。真实约束是鸭子类型的 ``get(key, default) -> str``，
#: 并由下方 ``normalize_headers`` 在运行时显式收敛与防御。
HeaderLike = Any


def normalize_headers(source: HeaderLike | None) -> dict[str, str]:
    """把任意 header/cookie 访问器收敛为只含鉴权相关键位的普通字典。

    刻意在此归一化而非让类型系统承担：既让「鉴权只看这几个头」这一事实
    显式化，也顺带挡住非字符串值（畸形请求头）与访问器抛异常两种情况。
    """
    if source is None:
        return {}
    out: dict[str, str] = {}
    for key in _HEADER_KEYS:
        try:
            value = source.get(key, "")
        except Exception:
            continue
        if isinstance(value, str) and value:
            out[key] = value
    return out


def _env(name: str) -> str:
    return os.getenv(name, "").strip()


def resolve_auth_token() -> str:
    """返回系统统一鉴权令牌；`API_AUTH_TOKEN` 优先，`LIVE_AGENT_TOKEN` 为兼容别名。"""
    return _env("API_AUTH_TOKEN") or _env("LIVE_AGENT_TOKEN")


def is_commercial_mode() -> bool:
    """当前是否处于 PILOT/PRODUCTION 商业档位。

    大小写与前后空白均被容忍：运维手写 ``LIVE_AGENT_MODE=production ``
    不应导致门禁静默失效。
    """
    return _env("LIVE_AGENT_MODE").upper() in COMMERCIAL_MODES


def is_local_client(host: str | None) -> bool:
    """判断请求来源是否为本机回环。"""
    return (host or "") in LOCAL_CLIENT_HOSTS


def enforce_remote_auth() -> bool:
    """运维是否显式要求拦截「未配置令牌」的外部访问。"""
    return _env("ENFORCE_REMOTE_AUTH").lower() in _TRUTHY


def extract_http_token(headers: dict[str, str], cookies: dict[str, str]) -> str:
    """按 Bearer > X-API-Key > Cookie 优先级提取 HTTP 请求携带的令牌。"""
    header_auth = headers.get("Authorization", "")
    if header_auth.startswith("Bearer "):
        return header_auth[7:].strip()
    api_key = headers.get("X-API-Key", "")
    if api_key:
        return api_key.strip()
    return cookies.get("access_token", "").strip()


def evaluate_access(
    client_host: str | None,
    headers: HeaderLike | None,
    cookies: HeaderLike | None,
) -> tuple[bool, str]:
    """统一鉴权裁决。

    返回 ``(是否放行, 拒绝原因)``；放行时原因为空串。

    ``headers`` / ``cookies`` 接受任意具备 ``get(key, default)`` 的访问器
    （Starlette Headers/Cookers 或普通 dict）。HTTP 中间件与 WebSocket 守卫
    共用本函数，确保二者强度永远一致。
    """
    auth_token = resolve_auth_token()
    commercial = is_commercial_mode()
    local = is_local_client(client_host)
    token_provided = extract_http_token(normalize_headers(headers), normalize_headers(cookies))

    if auth_token:
        # 本地零摩擦免密仅保留给未进入商业档的开发态；
        # 商业档位或任何非回环来源都必须校验令牌。
        if commercial or not local or token_provided:
            if token_provided != auth_token:
                return False, "invalid_token"
        return True, ""

    # 未配置系统令牌
    if commercial:
        return False, "commercial_without_token"
    if not local and enforce_remote_auth():
        return False, "remote_without_token"
    return True, ""
