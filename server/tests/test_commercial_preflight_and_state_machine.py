# -*- coding: utf-8 -*-
"""
商业化生产预检与运行状态机测试 (P0 任务验收测试)
依据《商业化运营改造方案》v3.0.0 第四章与第十三节要求制定。
"""
import asyncio
from unittest.mock import MagicMock, patch
from fastapi.testclient import TestClient

from server.core.operations.preflight import (
    CheckSeverity,
    PreflightService,
)


def test_preflight_service_report_structure():
    """验证预检服务生成的报告结构完整性与有效期校验"""
    svc = PreflightService(cache_ttl_seconds=10)
    report = asyncio.run(svc.run_preflight(mode="DEMO", force=True))

    assert report.mode == "DEMO"
    assert isinstance(report.passed, bool)
    assert report.is_valid() is True
    assert len(report.checks) > 0

    # 验证检查项必须包含规范字段
    first_check = report.checks[0]
    check_dict = first_check.to_dict()
    assert "code" in check_dict
    assert "title" in check_dict
    assert "severity" in check_dict
    assert "passed" in check_dict
    assert "evidence" in check_dict


def test_preflight_blocks_on_production_mode():
    """验证 PRODUCTION 档位下硬件/模型缺失时必须 Fail-Closed 阻断"""
    svc = PreflightService(cache_ttl_seconds=10)
    # 模拟环境：无 CUDA、无云端 GPU、无模型文件
    with patch("server.core.operations.preflight.DATA_DIR") as mock_data_dir:
        fake_path = MagicMock()
        fake_path.exists.return_value = False
        mock_data_dir.__truediv__.return_value = fake_path

        report = asyncio.run(svc.run_preflight(mode="PRODUCTION", avatar_asset_dir=None, force=True))

        assert report.mode == "PRODUCTION"
        assert report.passed is False
        assert len(report.blocking_reasons) > 0

        # 必须命中阻断代码
        block_codes = [c.code for c in report.checks if c.severity == CheckSeverity.BLOCK and not c.passed]
        assert "RENDER_ACCELERATION_HARDWARE" in block_codes or "ASSET_ONNX_LIPSYNC_MODEL" in block_codes


def test_preflight_preview_mode_tolerance():
    """验证 PREVIEW / DEMO 档位允许用于本地无独显调试，不作强制硬件阻断"""
    svc = PreflightService(cache_ttl_seconds=10)
    report = asyncio.run(svc.run_preflight(mode="PREVIEW", avatar_asset_dir=None, force=True))

    assert report.mode == "PREVIEW"
    # PREVIEW 模式下硬件算力项应为 WARN 而非 BLOCK
    hardware_check = next((c for c in report.checks if c.code == "RENDER_ACCELERATION_HARDWARE"), None)
    assert hardware_check is not None
    assert hardware_check.severity == CheckSeverity.WARN


def test_start_live_blocked_on_unmet_production_preflight():
    """测试在 PRODUCTION 档位下，开播接口必须硬阻断未达标请求并返回 400"""
    from server.app import app

    client = TestClient(app)

    # 传入 execution_mode="PRODUCTION"，同时未配置模型与独显
    with patch("server.core.operations.preflight.DATA_DIR") as mock_data_dir:
        fake_path = MagicMock()
        fake_path.exists.return_value = False
        mock_data_dir.__truediv__.return_value = fake_path

        response = client.post(
            "/api/v1/live/start",
            json={
                "room_url": "https://live.bilibili.com/123456",
                "platform": "bilibili",
                "execution_mode": "PRODUCTION",
            },
        )

        assert response.status_code == 400
        data = response.json()
        assert "PREFLIGHT_BLOCKED" in str(data) or "生产准入标准" in str(data)


def test_get_preflight_endpoint_available():
    """测试 GET /api/v1/live/preflight 端点正常可用"""
    from server.app import app

    client = TestClient(app)
    response = client.get("/api/v1/live/preflight?mode=PREVIEW")
    assert response.status_code in (200, 400)
    data = response.json()
    assert "data" in data
    assert "checks" in data["data"]
    assert "mode" in data["data"]
