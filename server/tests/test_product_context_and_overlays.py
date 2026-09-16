import asyncio
import threading
from pathlib import Path

import numpy as np
import pytest
from fastapi.testclient import TestClient

from server.app import app


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as test_client:
        yield test_client


def test_product_context_fields_round_trip(client):
    payload = {
        "sku_code": "SKU_CONTEXT_001",
        "title": "轻暖羊毛外套",
        "category": "女装",
        "original_price": 699.0,
        "live_price": 399.0,
        "current_stock": 12,
        "selling_points": ["90%羊毛", "轻盈保暖"],
        "faq_data": [
            {"question": "会不会扎？", "answer": "内层亲肤，不直接摩擦皮肤"},
            {"question": "怎么清洗？", "answer": "建议专业干洗"},
        ],
        "size_chart": {
            "columns": ["尺码", "胸围", "衣长"],
            "rows": [["S", "100", "62"], ["M", "104", "64"]],
            "unit": "cm",
        },
        "coupon_script": "领券后再减30元",
        "description": "适合5至15摄氏度通勤穿着",
        "images": ["data/product_images/context-main.png"],
    }

    created = client.post("/api/v1/products/upsert", json=payload)
    assert created.status_code == 200
    product_id = created.json()["data"]["id"]

    products = client.get("/api/v1/products/list").json()["data"]
    product = next(item for item in products if item["id"] == product_id)
    for field in (
        "selling_points", "faq_data", "size_chart", "coupon_script",
        "description", "images",
    ):
        assert product[field] == payload[field]

    payload.update({"id": product_id, "current_stock": 7, "coupon_script": "库存变化后新话术"})
    assert client.post("/api/v1/products/upsert", json=payload).status_code == 200
    from server.routes.live import global_live_controller
    asyncio.run(global_live_controller._refresh_product_context())
    refreshed = next(item for item in global_live_controller.live_context["products"] if item["id"] == product_id)
    assert refreshed["current_stock"] == 7
    assert refreshed["coupon_script"] == "库存变化后新话术"


def test_llm_product_context_is_complete_and_bounded():
    from server.core.llm.client import LLMClient

    products = [{
        "sku": "SKU_CONTEXT_001",
        "title": "轻暖羊毛外套",
        "category": "女装",
        "original_price": 699.0,
        "live_price": 399.0,
        "current_stock": 12,
        "selling_points": ["90%羊毛", "轻盈保暖"],
        "faq_data": [{"question": "会不会扎？", "answer": "内层亲肤"}],
        "size_chart": {"columns": ["尺码", "胸围"], "rows": [["M", "104"]]},
        "coupon_script": "领券后再减30元",
        "description": "适合通勤穿着",
        "images": ["data/product_images/context-main.png"],
    }] + [{"sku": f"EXTRA_{i}", "title": "超长标题" * 200} for i in range(20)]

    text = LLMClient._build_product_context(products)
    for expected in (
        "SKU_CONTEXT_001", "轻暖羊毛外套", "699.0", "399.0", "库存:12",
        "90%羊毛", "会不会扎？", "内层亲肤", "尺码", "胸围",
        "领券后再减30元", "适合通勤穿着", "context-main.png",
    ):
        assert expected in text
    assert len(text) <= 6000
    assert "EXTRA_8" not in text


def test_scene_tools_broadcast_renderable_payloads(monkeypatch):
    from server.core.tools import tool_bus
    from server.routes.ws_live import ws_manager

    broadcasts = []

    async def fake_broadcast(event, payload):
        broadcasts.append((event, payload))

    monkeypatch.setattr(ws_manager, "broadcast", fake_broadcast)

    async def run():
        coupon = await tool_bus.trigger_onscreen_coupon(
            sku="SKU_CONTEXT_001", title="轻暖羊毛外套", desc="限时减30", seconds=60
        )
        closeup = await tool_bus.product_closeup(
            sku="SKU_CONTEXT_001", title="轻暖羊毛外套", image="data/product_images/context-main.png", seconds=6
        )
        chart = await tool_bus.show_size_chart(
            sku="SKU_CONTEXT_001", title="轻暖羊毛外套",
            size_chart={"columns": ["尺码", "胸围"], "rows": [["M", "104"]]},
        )
        return coupon, closeup, chart

    coupon, closeup, chart = asyncio.run(run())
    assert coupon["sku"] == "SKU_CONTEXT_001" and coupon["seconds"] == 60
    assert closeup["image"].endswith("context-main.png")
    assert chart["size_chart"]["rows"] == [["M", "104"]]
    assert [event for event, _ in broadcasts] == [
        "ONSCREEN_COUPON", "CAMERA_CLOSEUP", "SIZE_CHART"
    ]


def test_console_contains_real_scene_overlay_lifecycle():
    root = Path(__file__).parents[2]
    html = (root / "server/static/index.html").read_text(encoding="utf-8")
    js_modules = (root / "server/static/js/modules").glob("*.js")
    js = "".join(f.read_text(encoding="utf-8") for f in sorted(js_modules)) + (root / "server/static/js/console.js").read_text(encoding="utf-8")
    css = (root / "server/static/css/console.css").read_text(encoding="utf-8")

    for element_id in ("coupon-overlay", "product-scene-overlay", "scene-overlay-content"):
        assert f'id="{element_id}"' in html
    for function_name in (
        "showCouponOverlay", "showProductCloseup", "showSizeChart", "closeSceneOverlay",
    ):
        assert f"function {function_name}" in js
    assert "clearTimeout(sceneOverlayTimer)" in js
    assert "clearInterval(couponOverlayTimer)" in js
    assert ".coupon-overlay" in css
    assert ".product-scene-overlay" in css


def test_scene_overlay_state_is_thread_safe_and_expires_by_monotonic_deadline():
    from server.core.media.scene_overlay import SceneOverlayState

    now = [100.0]
    state = SceneOverlayState(clock=lambda: now[0])
    source_payload = {"desc": "限时减30", "nested": {"value": 1}}
    state.set_coupon(source_payload, seconds=5)
    source_payload["nested"]["value"] = 99

    first = state.snapshot()
    assert first["coupon"]["payload"]["nested"]["value"] == 1
    assert first["coupon"]["remaining_seconds"] == 5

    failures = []

    def writer(index):
        try:
            state.set_scene(
                "closeup" if index % 2 else "size_chart",
                {"title": f"商品-{index}", "rows": [[index]]},
                seconds=10,
            )
            snapshot = state.snapshot()
            if snapshot["scene"]["kind"] not in {"closeup", "size_chart"}:
                failures.append(snapshot)
        except Exception as exc:  # pragma: no cover - 失败时保留线程异常
            failures.append(exc)

    threads = [threading.Thread(target=writer, args=(index,)) for index in range(40)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert failures == []

    now[0] = 105.01
    expired = state.snapshot()
    assert expired["coupon"] is None
    assert expired["scene"] is not None


def test_scene_overlay_composition_changes_pixels_and_keeps_frame_bounded():
    from server.core.media.scene_overlay import compose_scene_overlays

    base = np.zeros((240, 320, 3), dtype=np.uint8)
    snapshots = (
        {"coupon": {"payload": {"title": "COUPON", "desc": "SAVE 30"}, "remaining_seconds": 9}, "scene": None},
        {"coupon": None, "scene": {"kind": "closeup", "payload": {"title": "COAT", "sku": "SKU-1"}}},
        {"coupon": None, "scene": {"kind": "size_chart", "payload": {"title": "SIZE", "size_chart": {"columns": ["Size", "Chest"], "rows": [["M", "104"]]}}}},
    )

    for snapshot in snapshots:
        composed = compose_scene_overlays(base, snapshot)
        assert composed.shape == base.shape
        assert composed.dtype == np.uint8
        assert np.count_nonzero(composed) > 0
        assert np.count_nonzero(base) == 0


def test_scene_tools_update_publish_state_and_keep_ws_broadcast(monkeypatch):
    from server.core.media.scene_overlay import global_scene_overlay_state
    from server.core.tools import tool_bus
    from server.routes.ws_live import ws_manager

    broadcasts = []

    async def fake_broadcast(event, payload):
        broadcasts.append((event, payload))

    monkeypatch.setattr(ws_manager, "broadcast", fake_broadcast)
    global_scene_overlay_state.clear()

    async def run():
        await tool_bus.trigger_onscreen_coupon(desc="限时减30", seconds=60)
        coupon = global_scene_overlay_state.snapshot()
        await tool_bus.product_closeup(title="轻暖羊毛外套", seconds=6)
        closeup = global_scene_overlay_state.snapshot()
        await tool_bus.show_size_chart(
            title="轻暖羊毛外套",
            size_chart={"columns": ["尺码", "胸围"], "rows": [["M", "104"]]},
            seconds=20,
        )
        chart = global_scene_overlay_state.snapshot()
        return coupon, closeup, chart

    coupon, closeup, chart = asyncio.run(run())
    assert coupon["coupon"]["payload"]["desc"] == "限时减30"
    assert closeup["scene"]["kind"] == "closeup"
    assert chart["scene"]["kind"] == "size_chart"
    assert [event for event, _ in broadcasts] == [
        "ONSCREEN_COUPON", "CAMERA_CLOSEUP", "SIZE_CHART"
    ]
    global_scene_overlay_state.clear()


def test_virtual_camera_letterboxes_without_stretching():
    from server.core.media.virtual_cam import VirtualCameraService, letterbox_frame

    portrait = np.full((8, 4, 3), 255, dtype=np.uint8)
    fitted = letterbox_frame(portrait, width=16, height=8)
    assert fitted.shape == (8, 16, 3)
    assert np.all(fitted[:, :6] == 0)
    assert np.all(fitted[:, 6:10] == 255)
    assert np.all(fitted[:, 10:] == 0)

    class FakeCamera:
        def __init__(self):
            self.frames = []

        def send(self, frame):
            self.frames.append(frame.copy())

    service = VirtualCameraService(width=16, height=8)
    service.cam_device = FakeCamera()
    service.is_active = True
    service.send_frame(portrait)
    assert len(service.cam_device.frames) == 1
    assert np.array_equal(service.cam_device.frames[0], fitted)


def test_overlay_text_has_deterministic_non_ascii_rendering():
    from server.core.media.scene_overlay import renderable_text

    rendered = renderable_text("限时优惠")
    assert rendered != "????"
    assert rendered == "限时优惠" or "U+9650" in rendered


def test_musetalk_composes_once_before_camera_and_jpeg_fanout(monkeypatch):
    import cv2
    from server.adapters.media import musetalk_driver as module
    from server.core.media.scene_overlay import global_scene_overlay_state

    sent_frames = []
    monkeypatch.setattr(module.global_virtual_cam, "is_active", True)
    monkeypatch.setattr(module.global_virtual_cam, "send_frame", lambda frame: sent_frames.append(frame.copy()))
    global_scene_overlay_state.clear()
    global_scene_overlay_state.set_coupon({"title": "限时优惠", "desc": "立减30"}, seconds=30)

    driver = object.__new__(module.MuseTalkMediaDriver)
    driver.latest_jpeg_frame = b""
    base = np.zeros((240, 320, 3), dtype=np.uint8)
    driver._publish_frame(base)

    assert len(sent_frames) == 1
    assert np.count_nonzero(sent_frames[0]) > 0
    decoded = cv2.imdecode(np.frombuffer(driver.latest_jpeg_frame, dtype=np.uint8), cv2.IMREAD_COLOR)
    assert decoded is not None
    assert np.count_nonzero(decoded) > 0
    assert decoded.shape[:2] == sent_frames[0].shape[:2]
    global_scene_overlay_state.clear()
