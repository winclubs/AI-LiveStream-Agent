# -*- coding: utf-8 -*-
"""
电商商品切片管理 API 与直播大屏动静双轨指标单元测试
覆盖：
1. GET /products/{id}/slices
2. POST /products/{id}/slices/generate
3. POST /products/{id}/slices/single
4. DELETE /products/slices/{id}
5. GET /live/stats (stitcher_state, stitcher_badge, active_slice_info)
"""
from unittest.mock import AsyncMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from server.app import app
from server.database.db import AsyncSessionLocal
from server.database.models import Anchor, Product, SkuSlice


@pytest.mark.anyio
async def test_product_slices_api_lifecycle(tmp_path):
    """测试商品切片增删查 API 完整生命周期"""
    async with AsyncSessionLocal() as session:
        # 准备主播与商品
        anchor = Anchor(
            id="test_anchor_api_01",
            name="带货主播小陈",
            anchor_type="ecommerce",
        )
        session.add(anchor)

        prod = Product(
            id="test_prod_api_01",
            sku_code="SKU_API_01",
            title="无线降噪耳机",
            category="数码",
            live_price=299.0,
            original_price=599.0,
            current_stock=80,
            selling_points='["智能主动降噪", "40小时超长续航"]',
            coupon_script="立减 300 元，领券马上拍！",
        )
        session.add(prod)
        await session.commit()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # 1. 初始查询切片列表应为空
        res = await client.get("/api/v1/products/test_prod_api_01/slices")
        assert res.status_code == 200
        data = res.json()
        assert data["code"] == 0
        assert data["total"] == 0
        assert data["data"] == []

        # 2. 生成单切片
        mock_slice = SkuSlice(
            id="slice_api_intro_01",
            product_id="test_prod_api_01",
            slot_code="intro",
            slice_type="main_pitch",
            script_text="欢迎各位宝子们来到直播间！这款耳机音质绝了！",
            video_path="/data/slices/intro.mp4",
            audio_path="/data/slices/intro.wav",
            duration_sec=3.2,
            loudness_lufs=-16.0,
            render_engine="latentsync_cloud",
            is_active=1,
        )

        with patch("server.core.avatar.sku_slice_renderer.SkuSliceRenderer.render_single_slice", new=AsyncMock(return_value=mock_slice)):
            single_res = await client.post(
                "/api/v1/products/test_prod_api_01/slices/single",
                json={
                    "anchor_id": "test_anchor_api_01",
                    "slot_code": "intro",
                    "script_text": "欢迎各位宝子们来到直播间！这款耳机音质绝了！",
                },
            )
            assert single_res.status_code == 200
            single_data = single_res.json()
            assert single_data["code"] == 0
            assert single_data["data"]["slot_code"] == "intro"

        # 3. 批量生成切片
        mock_suite = [
            mock_slice,
            SkuSlice(
                id="slice_api_coupon_01",
                product_id="test_prod_api_01",
                slot_code="coupon",
                slice_type="coupon_burst",
                script_text="限时立减，大家拼手速！",
                video_path="/data/slices/coupon.mp4",
                audio_path="/data/slices/coupon.wav",
                duration_sec=4.0,
                loudness_lufs=-16.0,
                render_engine="latentsync_cloud",
                is_active=1,
            ),
        ]
        with patch("server.core.avatar.sku_slice_renderer.SkuSliceRenderer.render_sku_standard_suite", new=AsyncMock(return_value=mock_suite)):
            batch_res = await client.post(
                "/api/v1/products/test_prod_api_01/slices/generate",
                json={"anchor_id": "test_anchor_api_01"},
            )
            assert batch_res.status_code == 200
            batch_data = batch_res.json()
            assert batch_data["code"] == 0
            assert len(batch_data["data"]) == 2

        # 4. 再次查询切片清单
        async with AsyncSessionLocal() as session:
            # 手工落一条切片记录测试 list 与 delete
            real_slice = SkuSlice(
                id="slice_real_001",
                product_id="test_prod_api_01",
                slot_code="faq_size",
                script_text="支持七天无理由退换",
                video_path="/test/v.mp4",
                audio_path="/test/a.wav",
                duration_sec=2.5,
                loudness_lufs=-16.0,
                is_active=1,
            )
            session.add(real_slice)
            await session.commit()

        list_res = await client.get("/api/v1/products/test_prod_api_01/slices")
        assert list_res.status_code == 200
        list_data = list_res.json()
        assert list_data["total"] >= 1

        # 5. 删除切片
        del_res = await client.delete("/api/v1/products/slices/slice_real_001")
        assert del_res.status_code == 200
        assert del_res.json()["code"] == 0


@pytest.mark.anyio
async def test_live_stats_includes_stitcher_metrics():
    """测试 /live/stats 包含动静结合大屏指标"""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        res = await client.get("/api/v1/live/stats")
        assert res.status_code == 200
        body = res.json()
        assert body["code"] == 0
        data = body["data"]

        # 验证大屏动静指标字段存在且结构完备
        assert "stitcher_state" in data
        assert "stitcher_badge" in data
        assert data["stitcher_state"] in (
            "idle",
            "playing_batch_slice",
            "interrupted_holding",
            "streaming_realtime",
            "blending_transition",
        )
        assert "【" in data["stitcher_badge"]
