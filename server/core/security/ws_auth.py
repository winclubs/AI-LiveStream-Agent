# -*- coding: utf-8 -*-
"""
WebSocket 安全鉴权与速率限制守卫 (server.core.security.ws_auth)
依据《商业化运营改造方案》v3.0.0 第八章制定。

解决风险：
1. HTTP middleware 无法拦截 WebSocket 握手，导致端点直接 accept() 裸奔；
2. 弹幕中继和 ASR 端点缺失 Token 校验与频控，易遭受越权调用和高频刷爆。
"""
from __future__ import annotations

import logging
import time
from collections import deque
from fastapi import WebSocket, status

from server.core.security.auth_policy import HeaderLike, evaluate_access

logger = logging.getLogger("LiveAgent.Security.WS")


async def verify_websocket_auth(websocket: WebSocket) -> bool:
    """
    在 websocket.accept() 之前执行严格的 Token 与网络来源鉴权：
    - 支持 Query 参数 ?token=xxx
    - 支持 Headers: Authorization: Bearer xxx 或 X-API-Key
    - 支持 Cookies: access_token
    - 本地单机回环 (127.0.0.1/::1/localhost/testclient) 允许零摩擦免密放行，但若设置了强制外部鉴权则必须校验

    裁决逻辑复用 `auth_policy.evaluate_access`，与 HTTP 中间件同源，
    杜绝二者强度漂移（历史上 HTTP 侧缺少商业档位判断，见 BLOCKER-1）。
    WebSocket 侧特有的 query 参数令牌提取在此补齐后再委托共享函数。
    """
    client_host = websocket.client.host if websocket.client else ""

    # query 参数令牌优先；命中后构造等价的 header 视图交给共享裁决函数
    token_provided = (
        websocket.query_params.get("token", "").strip()
        or websocket.query_params.get("api_key", "").strip()
    )
    if token_provided:
        headers: HeaderLike | None = {"Authorization": f"Bearer {token_provided}"}
        cookies: HeaderLike | None = None
    else:
        headers = websocket.headers
        cookies = websocket.cookies

    allowed, reason = evaluate_access(client_host, headers, cookies)
    if allowed:
        return True

    if reason == "invalid_token":
        logger.warning(
            "WebSocket 鉴权失败：客户端 %s 访问 %s 提供的令牌无效",
            client_host, websocket.url.path,
        )
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION, reason="Unauthorized: Invalid Token")
    else:
        logger.warning(
            "WebSocket 访问拦截：客户端 %s 访问 %s (%s)，系统未设置有效令牌",
            client_host, websocket.url.path, reason,
        )
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION, reason="Unauthorized: Remote Access Forbidden")
    return False


class WSRateLimiter:
    """简单的滑动窗口速率限制器，防止弹幕中继等通道恶意高频灌水"""

    def __init__(self, max_requests: int = 50, window_seconds: float = 1.0):
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self._timestamps: deque = deque()

    def allow_request(self) -> bool:
        now = time.time()
        # 清除窗口外的过时记录
        while self._timestamps and (now - self._timestamps[0]) > self.window_seconds:
            self._timestamps.popleft()

        if len(self._timestamps) >= self.max_requests:
            return False

        self._timestamps.append(now)
        return True
