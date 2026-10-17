# -*- coding: utf-8 -*-
"""
HTTP 鉴权商业档位强制门禁回归测试。

背景（上线诊断 BLOCKER-1）：
`server/app.py` 的 HTTP 安全中间件只在「已配置 API_AUTH_TOKEN」或
「ENFORCE_REMOTE_AUTH=1」时拦截，**缺少** `LIVE_AGENT_MODE` 判断；而
`server/core/security/ws_auth.py` 早已具备该判断（生产/Pilot 档即使绑定
回环也强制校验令牌）。

后果：运维将服务绑定到 0.0.0.0 且未设置令牌时，WebSocket 端点被锁死，
但整个 REST API（含 /api/v1/settings 凭据配置、/api/v1/live 开播控制）
完全裸奔。HTTP 反而成了比 WebSocket 更弱的入口，属于明确的安全倒挂。

本文件锁定「HTTP 与 WebSocket 鉴权强度一致」这一不变量。
"""
import pytest
from fastapi.testclient import TestClient

from server.app import app


API_PATH = "/api/v1/system/version"


@pytest.fixture
def local_client():
    return TestClient(app, client=("127.0.0.1", 54321))


@pytest.fixture
def ext_client():
    return TestClient(app, client=("198.51.100.1", 54321))


def _clear_auth_env(monkeypatch):
    monkeypatch.delenv("API_AUTH_TOKEN", raising=False)
    monkeypatch.delenv("LIVE_AGENT_TOKEN", raising=False)
    monkeypatch.delenv("ENFORCE_REMOTE_AUTH", raising=False)


def _clear_mode_env(monkeypatch):
    monkeypatch.delenv("LIVE_AGENT_MODE", raising=False)


# ---------------------------------------------------------------------------
# RED-1: 商业档位下本地回环不得再享受零摩擦免密
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("mode", ["PRODUCTION", "PILOT"])
def test_commercial_mode_blocks_local_request_without_token(local_client, monkeypatch, mode):
    """PRODUCTION/PILOT 档位下，本地回环且未配置令牌时必须 403 拦截。

    这是本文件的核心回归：修复前 HTTP 层放行，WebSocket 层拦截，
    安全强度倒挂。修复后两者行为一致。
    """
    _clear_auth_env(monkeypatch)
    monkeypatch.setenv("LIVE_AGENT_MODE", mode)

    resp = local_client.get(API_PATH)

    assert resp.status_code == 403, (
        f"{mode} 档位下未配置令牌时，本地回环 HTTP 请求必须被拦截，"
        f"实际返回 {resp.status_code}（若为 200 说明 HTTP 鉴权弱于 WebSocket 鉴权）"
    )
    assert "令牌" in resp.json().get("message", "") or "拦截" in resp.json().get("message", "")


@pytest.mark.parametrize("mode", ["PRODUCTION", "PILOT"])
def test_commercial_mode_requires_valid_token_on_localhost(local_client, monkeypatch, mode):
    """PRODUCTION/PILOT 档位下，本地回环必须携带有效令牌才能访问业务 API。"""
    monkeypatch.setenv("LIVE_AGENT_MODE", mode)
    monkeypatch.setenv("API_AUTH_TOKEN", "prod-secret-token-xyz")

    # 无令牌 -> 401
    assert local_client.get(API_PATH).status_code == 401
    # 错误令牌 -> 401
    bad = local_client.get(API_PATH, headers={"Authorization": "Bearer wrong"})
    assert bad.status_code == 401
    # 正确 Bearer -> 200
    ok = local_client.get(API_PATH, headers={"Authorization": "Bearer prod-secret-token-xyz"})
    assert ok.status_code == 200
    assert ok.json().get("code") == 0
    # 正确 X-API-Key -> 200
    ok_key = local_client.get(API_PATH, headers={"X-API-Key": "prod-secret-token-xyz"})
    assert ok_key.status_code == 200


# ---------------------------------------------------------------------------
# RED-2: 非商业档位必须保持原有零摩擦本地体验（防止修复过度收紧）
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("mode", ["DEMO", "PREVIEW"])
def test_non_commercial_mode_keeps_local_frictionless(local_client, monkeypatch, mode):
    """DEMO/PREVIEW 档位下本地回环仍然免密，避免开发/演示体验被修复破坏。"""
    _clear_auth_env(monkeypatch)
    monkeypatch.setenv("LIVE_AGENT_MODE", mode)

    assert local_client.get(API_PATH).status_code == 200


def test_absent_mode_env_keeps_local_frictionless(local_client, monkeypatch):
    """未设置 LIVE_AGENT_MODE 时维持既有行为：本地回环免密。"""
    _clear_auth_env(monkeypatch)
    _clear_mode_env(monkeypatch)

    assert local_client.get(API_PATH).status_code == 200


# ---------------------------------------------------------------------------
# RED-3: 健康探针与静态资源在商业档位下仍须放行（否则负载均衡会判定实例不健康）
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("probe", ["/health", "/livez", "/readyz"])
def test_health_probes_still_open_in_commercial_mode(local_client, monkeypatch, probe):
    """商业档位不得阻断健康探针，否则 K8s/守护进程会误杀实例。"""
    _clear_auth_env(monkeypatch)
    monkeypatch.setenv("LIVE_AGENT_MODE", "PRODUCTION")

    resp = local_client.get(probe)

    assert resp.status_code != 401
    assert resp.status_code != 403


# ---------------------------------------------------------------------------
# RED-4: 外部网络在商业档位下未配令牌必须硬拦截（不再仅告警）
# ---------------------------------------------------------------------------

def test_commercial_mode_blocks_external_without_token(ext_client, monkeypatch):
    """商业档位 + 外部网络 + 无令牌 -> 403 硬拦截，而不是仅记录告警。"""
    _clear_auth_env(monkeypatch)
    monkeypatch.setenv("LIVE_AGENT_MODE", "PRODUCTION")

    resp = ext_client.get(API_PATH)

    assert resp.status_code == 403
    # 不应再出现「仅告警」的 X-Security-Warning 头
    assert resp.headers.get("X-Security-Warning") is None


# ---------------------------------------------------------------------------
# RED-5: 档位值大小写与空白容错
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("mode", ["production", "Production", " PRODUCTION ", "production "])
def test_commercial_mode_detection_is_case_and_space_insensitive(local_client, monkeypatch, mode):
    """档位解析必须容忍大小写与前后空白，避免运维手误导致门禁失效。"""
    _clear_auth_env(monkeypatch)
    monkeypatch.setenv("LIVE_AGENT_MODE", mode)

    assert local_client.get(API_PATH).status_code == 403


# ---------------------------------------------------------------------------
# RED-6: CORS 默认不得为 "*"
# ---------------------------------------------------------------------------

def test_cors_default_is_not_wildcard():
    """未配置 CORS_ALLOW_ORIGINS 时，默认白名单必须是本机来源而非 "*"。

    历史实现默认为 ``["*"]``。配合本地回环免密，任意网页都能对本机控制台
    发起跨域控制类请求（开播/改配置/读密钥）。默认收紧为白名单后，
    ``*`` 只能通过显式配置开启（并会打印安全告警）。
    """
    from starlette.middleware.cors import CORSMiddleware

    import server.app as app_mod

    cors_layers = [
        mw.kwargs.get("allow_origins")
        for mw in app_mod.app.user_middleware
        if mw.cls is CORSMiddleware
    ]

    assert cors_layers, "未找到 CORS 中间件配置"
    for allow_origins in cors_layers:
        assert "*" not in allow_origins, "CORS 默认不得放行 '*'，必须显式配置才生效"
        assert allow_origins, "CORS 白名单不应为空"
