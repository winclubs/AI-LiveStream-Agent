import pytest
from pathlib import Path
from fastapi.testclient import TestClient
from server.app import app


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


def test_console_html_contains_all_tabs_and_components(client):
    """E2E冒烟测试：验证中控台 /console 模板聚合完整，包含全部关键标签页与组件锚点"""
    resp = client.get("/console")
    assert resp.status_code == 200
    html = resp.text

    # 验证关键组件包含在渲染后的页面中
    expected_tabs = [
        "tab-live",
        "tab-settings",
        "tab-products",
        "tab-guardrails",
        "tab-roles",
        "tab-anchors",
        "tab-knowledge",
        "tab-voices",
    ]
    for tab in expected_tabs:
        assert tab in html, f"控制台页面缺失重要标签页组件标记: {tab}"

    # 验证核心 JS 控制器与样式文件已正确引用
    assert "console.js" in html


def test_metrics_and_health_summary_endpoints(client):
    """E2E冒烟测试：验证可观测性指标端点 /metrics 与 /api/v1/system/health-summary 正常返回"""
    # 1. Prometheus 纯文本指标
    metrics_resp = client.get("/metrics")
    assert metrics_resp.status_code == 200
    assert "text/plain" in metrics_resp.headers.get("content-type", "")
    content = metrics_resp.text
    assert "live_agent_status" in content
    assert "live_agent_danmaku_total" in content
    assert "live_agent_render_fps" in content
    assert "live_agent_memory_rss_bytes" in content

    # 2. JSON 健康看板
    summary_resp = client.get("/api/v1/system/health-summary")
    assert summary_resp.status_code == 200
    data = summary_resp.json()
    assert data["code"] == 0
    assert "render_fps" in data["data"]
    assert "process_mem_rss_mb" in data["data"]
    assert "circuit_state" in data["data"]


def test_avatar_viewport_endpoint(client):
    """验证专供伴侣捕获的独立绿幕视窗 /avatar-viewport 端点正常工作"""
    resp = client.get("/avatar-viewport")
    assert resp.status_code == 200
    html = resp.text
    assert "viewport-feed" in html
    assert "#00FF00" in html
    assert "直播伴侣" in html


def test_all_tab_html_components_exist_on_disk():
    """E2E组件测试：验证 static/components/ 目录下所有 HTML 片段文件均存在且非空"""
    comp_dir = Path(__file__).resolve().parents[1] / "static" / "components"
    assert comp_dir.exists() and comp_dir.is_dir()

    component_files = list(comp_dir.glob("*.html"))
    assert len(component_files) >= 10, f"HTML 组件数不足，当前仅有 {len(component_files)}"

    for comp in component_files:
        assert comp.stat().st_size > 0, f"HTML 组件文件为空: {comp.name}"
