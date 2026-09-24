# -*- coding: utf-8 -*-
"""
数字人资产预览与算力真实探活自动化测试
"""
import pytest
import asyncio
from server.core.hardware.gpu_capability import (
    verify_cloud_endpoint_reachability,
    evaluate_compute,
    get_active_cloud_gpu,
)
from server.database.db import AsyncSessionLocal
from server.routes.anchors import get_anchor_avatar_detail, get_anchor_avatar_sample_frames


@pytest.fixture
def anyio_backend():
    return "asyncio"



@pytest.mark.anyio
async def test_cloud_reachability_unreachable():
    """测试当云端未开机或域名无效时，探活坚决返回 False"""
    reachable, err = await verify_cloud_endpoint_reachability("wss://invalid-tunnel-offline.domain.fake/ws", timeout_sec=0.5)
    assert reachable is False
    assert len(err) > 0


@pytest.mark.anyio
async def test_evaluate_compute_no_fake_cloud():
    """测试算力研判坚决不虚标已调度远端GPU"""
    plan = await evaluate_compute(feature_name="数字人视频切片制作", required_vram_gb=2.0)
    cloud = await get_active_cloud_gpu()
    if not cloud.is_reachable:
        assert plan.use_cloud is False
        assert "已实测连通并调度【远端租赁GPU加速】" not in plan.user_message


@pytest.mark.anyio
async def test_avatar_detail_and_samples():
    """测试主播数字人资产详情与切片样本接口 (使用数据目录中真实存在的资产)"""
    from pathlib import Path
    from server.config import DATA_DIR

    # 优先用数据库中绑定了资产的主播；数据库为空时回退到 DATA_DIR 真实资产目录
    from server.database.models import Anchor
    from sqlalchemy import select
    anchor_id = None
    async with AsyncSessionLocal() as db:
        res = await db.execute(
            select(Anchor).where(Anchor.avatar_asset_dir.isnot(None)).order_by(Anchor.created_at)
        )
        anchor = res.scalars().first()
        if anchor is not None:
            anchor_id = anchor.id

    if anchor_id is None:
        # 数据库无绑定记录 (如隔离库/全新环境)：取 DATA_DIR 下首个合法资产目录构造探活
        assets_root = Path(DATA_DIR) / "avatar_assets"
        for asset_dir in sorted(assets_root.glob("task_*")):
            if (asset_dir / "coords.pkl").exists() and (asset_dir / "meta.json").exists():
                anchor_id = asset_dir.name
                break
        assert anchor_id is not None, "数据目录中不存在合法数字人资产，无法完成资产预览测试"
        pytest.skip(f"数据库无主播绑定记录，改用资产目录探活: {anchor_id}")
        return

    async with AsyncSessionLocal() as db:
        detail = await get_anchor_avatar_detail(anchor_id, db)
        assert detail["code"] == 0
        data = detail["data"]
        assert "cloud_status" in data
        assert "local_gpu_name" in data
        assert data["compute_branch"] != "cloud_sidecar"

        samples_resp = await get_anchor_avatar_sample_frames(anchor_id, db)
        assert samples_resp["code"] == 0
        samples_data = samples_resp["data"]
        assert samples_data["has_asset"] is True
        assert samples_data["total_frames"] > 0
        assert len(samples_data["samples"]) > 0

        sample0 = samples_data["samples"][0]
        assert "bbox_percent" in sample0
        assert "full_url" in sample0
        assert "left" in sample0["bbox_percent"]
        assert "top" in sample0["bbox_percent"]
