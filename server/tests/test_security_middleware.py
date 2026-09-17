# -*- coding: utf-8 -*-
"""
安全鉴权中间件单元测试
验证本地回环免密、静态与探针免密、外部网络强制 Token 校验与拦截。
"""
import pytest
from fastapi.testclient import TestClient
from server.app import app


@pytest.fixture
def client():
    return TestClient(app)


def test_local_request_bypasses_auth(client):
    """测试本地请求默认免密放行"""
    # 模拟本地访问 /api/v1/system/version
    resp = client.get("/api/v1/system/version")
    assert resp.status_code == 200
    assert resp.json().get("code") == 0


def test_public_probes_bypass_auth(client):
    """测试健康探针等接口免密放行 (不会被安全中间件 401 拦截)"""
    assert client.get("/health").status_code != 401
    assert client.get("/livez").status_code != 401
    assert client.get("/readyz").status_code != 401


def test_external_request_with_token_protection(monkeypatch):
    """测试模拟外部公网 IP 访问且配置了 Token 时正常拦截与放行"""
    # 设置 API_AUTH_TOKEN
    monkeypatch.setenv("API_AUTH_TOKEN", "test-secret-token-123")

    # 构造外部 IP 客户端
    ext_client = TestClient(app, client=("198.51.100.1", 54321))

    # 未带 Token -> 401 拦截
    resp_unauth = ext_client.get("/api/v1/system/version")
    assert resp_unauth.status_code == 401
    assert "未授权" in resp_unauth.json().get("message", "")

    # 带错误 Token -> 401 拦截
    resp_bad = ext_client.get("/api/v1/system/version", headers={"Authorization": "Bearer bad-token"})
    assert resp_bad.status_code == 401

    # 带正确 Bearer Token -> 200 放行
    resp_good = ext_client.get("/api/v1/system/version", headers={"Authorization": "Bearer test-secret-token-123"})
    assert resp_good.status_code == 200
    assert resp_good.json().get("code") == 0

    # 带正确 X-API-Key -> 200 放行
    resp_good_key = ext_client.get("/api/v1/system/version", headers={"X-API-Key": "test-secret-token-123"})
    assert resp_good_key.status_code == 200
    assert resp_good_key.json().get("code") == 0
