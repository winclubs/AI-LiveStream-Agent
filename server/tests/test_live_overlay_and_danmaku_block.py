# -*- coding: utf-8 -*-
"""
验证阶段四中控台交互端点：画中画优惠券、逼单倒计时画层与违规弹幕一键拉黑
"""
import pytest
from fastapi.testclient import TestClient

from server.core.media.scene_overlay import global_scene_overlay_state
from server.app import app


@pytest.fixture
def client():
    return TestClient(app)


def test_trigger_overlay_coupon_api(client: TestClient):
    global_scene_overlay_state.clear()

    res = client.post(
        "/api/v1/live/overlay/coupon",
        json={
            "sku": "SKU_TEST_888",
            "title": "爆款风衣专属券",
            "amount": "立减 50 元",
            "desc": "领券立享折上折",
            "seconds": 15,
        },
    )
    assert res.status_code == 200
    data = res.json()
    assert data["code"] == 0

    # 验证画层状态
    snap = global_scene_overlay_state.snapshot()
    assert snap["scene"] is not None
    assert snap["scene"]["kind"] == "pip_coupon"
    assert snap["scene"]["payload"]["sku"] == "SKU_TEST_888"
    assert snap["scene"]["payload"]["amount"] == "立减 50 元"


def test_trigger_overlay_urgency_api(client: TestClient):
    global_scene_overlay_state.clear()

    res = client.post(
        "/api/v1/live/overlay/urgency",
        json={
            "sku": "SKU_TEST_999",
            "title": "🔥 限量抢购 倒计时",
            "desc": "仅剩最后少量现货",
            "seconds": 30,
        },
    )
    assert res.status_code == 200
    data = res.json()
    assert data["code"] == 0

    snap = global_scene_overlay_state.snapshot()
    assert snap["scene"] is not None
    assert snap["scene"]["kind"] == "urgency_countdown"
    assert snap["scene"]["payload"]["seconds"] == 30


def test_block_danmaku_user_api(client: TestClient):
    res = client.post(
        "/api/v1/live/danmaku/block",
        json={
            "user_name": "黑粉张三",
            "keyword": "骗人假货",
            "reason": "恶意攻击商品声誉",
        },
    )
    assert res.status_code == 200
    data = res.json()
    assert data["code"] == 0
    assert "黑粉张三" in data["data"]["blocked_rules"]
    assert "骗人假货" in data["data"]["blocked_rules"]
