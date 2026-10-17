import asyncio
import hashlib
import hmac
import time
from unittest.mock import patch
from fastapi.testclient import TestClient

from server.app import app
from server.core.compliance.governance import global_compliance_service
from server.core.operations.preflight import PreflightService
from server.routes.live import global_live_controller
from server.routes.orders_webhook import verify_webhook_signature


def test_compliance_ai_disclosure_in_preflight():
    """验证开播门禁中 AI 披露声明规则"""
    svc = PreflightService(cache_ttl_seconds=10)
    async def _run():
        # 默认有披露文案，检查项通过
        report = await svc.run_preflight(mode="PREVIEW", force=True)
        compliance_check = [c for c in report.checks if c.code == "COMPLIANCE_AI_DISCLOSURE"]
        assert len(compliance_check) == 1
        assert compliance_check[0].passed is True

        # 若清空披露文案，在 PRODUCTION 档位下必须被阻断
        with patch.object(global_compliance_service, "get_disclosure_text", return_value=""):
            report_blocked = await svc.run_preflight(mode="PRODUCTION", force=True)
            blocked_check = [c for c in report_blocked.checks if c.code == "COMPLIANCE_AI_DISCLOSURE"][0]
            assert blocked_check.passed is False
            assert any("COMPLIANCE_AI_DISCLOSURE" in r for r in report_blocked.blocking_reasons)

    asyncio.run(_run())


def test_demo_mode_blocks_real_order_registration():
    """DEMO 模式下严禁登记真实订单与扣减库存 (403 Fail-Closed)"""
    client = TestClient(app)
    ctrl = global_live_controller
    ctrl.is_live = True
    ctrl.session_id = "sess_demo_123"
    ctrl.execution_mode = "DEMO"

    try:
        resp = client.post("/api/v1/live/stats/order", json={
            "sku": "SKU_TEST_001",
            "amount": 99.0,
            "quantity": 1,
            "external_id": "ext_demo_test",
        })
        assert resp.status_code == 403
        assert "DEMO 演示模式严禁登记真实交易订单" in resp.json()["detail"]
    finally:
        ctrl.is_live = False


def test_order_webhook_signature_verification():
    """验证 Webhook HMAC-SHA256 签名与时间戳防重放"""
    secret = "test_super_secret_key_888"
    raw_body = b'{"event_id":"evt_1","platform_order_id":"p_1001"}'
    now = time.time()
    ts_str = str(now)

    # 构造合法签名
    msg = f"{ts_str}.".encode("utf-8") + raw_body
    valid_sig = hmac.new(secret.encode("utf-8"), msg, hashlib.sha256).hexdigest()

    ok, reason = verify_webhook_signature(raw_body, valid_sig, ts_str, secret=secret)
    assert ok is True
    assert reason == "VERIFIED"

    # 错误签名拦截
    ok_bad_sig, reason_bad = verify_webhook_signature(raw_body, "wrong_sig_value", ts_str, secret=secret)
    assert ok_bad_sig is False
    assert reason_bad == "SIGNATURE_MISMATCH"

    # 超时防重放拦截 (> 300s)
    expired_ts = str(now - 360.0)
    ok_exp, reason_exp = verify_webhook_signature(raw_body, valid_sig, expired_ts, secret=secret)
    assert ok_exp is False
    assert reason_exp == "TIMESTAMP_EXPIRED"


def test_order_reconcile_endpoint():
    """测试平台订单对账与差异分析报告接口"""
    client = TestClient(app)

    # 构造外部平台订单清单核对
    req_body = {
        "session_id": "sess_non_existent",
        "platform_orders": [
            {"platform_order_id": "P_ORDER_99999", "amount": 199.0, "status": "paid"}
        ]
    }
    resp = client.post("/api/v1/orders/reconcile", json=req_body)
    assert resp.status_code == 200
    res = resp.json()
    assert res["code"] == 0
    # 本地不存在此订单，报告 missing_in_local
    assert "P_ORDER_99999" in res["missing_in_local"]
    assert res["summary"]["missing_in_local_count"] == 1
