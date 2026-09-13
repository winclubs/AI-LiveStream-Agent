"""
直播必备软件生态检测端点自动化测试
验证 /api/v1/system/prerequisites 的数据结构、探测鲁棒性及返回项完整性
"""
import pytest
from fastapi.testclient import TestClient

from server.app import app
from server.routes.system import _detect_prerequisites


@pytest.fixture
def client():
    return TestClient(app)


def test_detect_prerequisites_structure():
    """测试底层探测函数的结构正确性与平滑降级能力"""
    data = _detect_prerequisites()
    assert isinstance(data, dict)
    assert "summary" in data
    assert "items" in data

    summary = data["summary"]
    assert "total" in summary
    assert "ready_count" in summary
    assert "missing_count" in summary
    assert "critical_missing" in summary
    assert "overall_level" in summary
    assert summary["overall_level"] in ["success", "warning", "alert"]

    items = data["items"]
    assert len(items) >= 5
    keys = [it["key"] for it in items]
    assert "obs" in keys
    assert "vcam_driver" in keys
    assert "pyvirtualcam" in keys
    assert "live_partner" in keys
    assert "media_core" in keys

    for item in items:
        assert "name" in item
        assert "category" in item
        assert "status" in item
        assert item["status"] in ["running", "installed", "missing"]
        assert "badge" in item
        assert "desc" in item
        assert "tip" in item


def test_system_prerequisites_api(client):
    """测试 HTTP 端点 GET /api/v1/system/prerequisites"""
    resp = client.get("/api/v1/system/prerequisites")
    assert resp.status_code == 200
    json_data = resp.json()
    assert json_data["code"] == 0
    assert "data" in json_data
    assert json_data["data"]["summary"]["total"] >= 5
