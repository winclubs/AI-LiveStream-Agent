# -*- coding: utf-8 -*-
"""
WebSocket 端点鉴权与限流安全性测试 (P0 任务验收测试)
依据《商业化运营改造方案》v3.0.0 第八章与第十三节要求制定。
"""
import json
import os
import time
from unittest.mock import patch
import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from server.app import app
from server.core.security.ws_auth import WSRateLimiter


def test_ws_ratelimiter_sliding_window():
    """测试滑动窗口限流器精准拦截高频突发"""
    limiter = WSRateLimiter(max_requests=5, window_seconds=0.2)
    # 前 5 次允许
    for _ in range(5):
        assert limiter.allow_request() is True
    # 第 6 次在同一窗口内应被拦截
    assert limiter.allow_request() is False

    # 等待窗口滑过
    time.sleep(0.25)
    assert limiter.allow_request() is True


def test_ws_auth_rejection_when_token_required():
    """测试当系统配置了 API_AUTH_TOKEN 时，未携带令牌的外部请求被拦截"""
    client = TestClient(app)

    with patch.dict(os.environ, {"API_AUTH_TOKEN": "secret_token_12345", "ENFORCE_REMOTE_AUTH": "1"}):
        # 1. 模拟错误 token
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect("/api/v1/live/asr/ws?token=wrong_token"):
                pass

        # 2. 携带正确 token 允许连接并正常收发数据
        with client.websocket_connect("/api/v1/live/asr/ws?token=secret_token_12345") as ws:
            ws.send_text(json.dumps({"type": "ping"}))
            resp = ws.receive_json()
            assert resp.get("type") == "pong"


def test_danmaku_ingest_auth_rejection():
    """测试弹幕中继端点 /ws/danmaku-ingest 的令牌拦截"""
    client = TestClient(app)

    with patch.dict(os.environ, {"API_AUTH_TOKEN": "secret_token_99999"}):
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect("/ws/danmaku-ingest?token=invalid"):
                pass

        with client.websocket_connect("/ws/danmaku-ingest?token=secret_token_99999") as ws:
            # 发送一条测试弹幕
            ws.send_text(json.dumps({"type": "chat", "user_name": "测试用户", "text": "你好"}))
