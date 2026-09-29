# -*- coding: utf-8 -*-
"""
阿里云 OSS 存储配置、直播录像复盘与运营工作导航自动化测试套件
"""
import io
import time
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from server.app import app
from server.database.db import AsyncSessionLocal
from server.database.models import LiveSessionRecord, utc_now


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


def test_01_oss_config_lifecycle(client):
    """测试 OSS 配置读取、保存与密钥脱敏"""
    # 1. 初始读取
    res_get = client.get("/api/v1/oss/config")
    assert res_get.status_code == 200
    d_get = res_get.json()
    assert d_get["code"] == 0
    assert "data" in d_get
    assert "endpoint" in d_get["data"]

    # 2. 保存配置
    save_payload = {
        "endpoint": "oss-cn-hangzhou.aliyuncs.com",
        "bucket": "test-live-bucket",
        "access_key_id": "LTAI_TEST_KEY_ID_12345",
        "access_key_secret": "my_super_secret_key_8888",
        "custom_domain": "https://cdn.example.com",
        "prefix": "recordings/2026/",
        "auto_upload": True,
        "is_active": True,
    }
    res_save = client.post("/api/v1/oss/config", json=save_payload)
    assert res_save.status_code == 200
    d_save = res_save.json()
    assert d_save["code"] == 0
    assert d_save["data"]["configured"] is True
    assert d_save["data"]["bucket"] == "test-live-bucket"
    assert "https://oss-cn-hangzhou.aliyuncs.com" in d_save["data"]["endpoint"]
    # 验证 Secret 被脱敏掩码
    assert ("***" in d_save["data"]["access_key_secret"] or "•" in d_save["data"]["access_key_secret"])

    # 3. 再次读取，验证持久化并保持掩码
    res_verify = client.get("/api/v1/oss/config")
    assert res_verify.status_code == 200
    d_verify = res_verify.json()["data"]
    assert d_verify["bucket"] == "test-live-bucket"
    assert ("***" in d_verify["access_key_secret"] or "•" in d_verify["access_key_secret"])


def test_02_oss_connectivity_test(client):
    """测试 OSS 连通性测试接口"""
    # 传参不全时报错
    res_bad = client.post(
        "/api/v1/oss/test",
        json={"endpoint": "", "bucket": "", "access_key_id": "", "access_key_secret": ""},
    )
    assert res_bad.status_code == 200
    assert res_bad.json()["code"] == 1

    # 完整参数时调用（测试环境下因无真实阿里云外网秘钥，预期优雅返回失败信息而非 500 崩溃）
    res_test = client.post(
        "/api/v1/oss/test",
        json={
            "endpoint": "https://oss-cn-hangzhou.aliyuncs.com",
            "bucket": "non-existent-bucket-9999",
            "access_key_id": "LTAI_MOCK_TEST",
            "access_key_secret": "MOCK_SECRET",
        },
    )
    assert res_test.status_code == 200
    d_test = res_test.json()
    assert "data" in d_test
    assert "connected" in d_test["data"]


@pytest.mark.anyio
async def test_03_live_recordings_and_replay(client, tmp_path):
    """测试直播场次录制、列表检索与复盘播放直链获取"""
    # 1. 模拟生成一段本地临时测试 mp4 视频
    dummy_video = tmp_path / "live_session_test.mp4"
    dummy_video.write_bytes(b"\x00\x00\x00\x20ftypisom" + b"\x00" * 2048)

    session_id = f"test_rec_sess_{int(time.time())}"

    # 2. 插入数据库记录
    async with AsyncSessionLocal() as db:
        rec = LiveSessionRecord(
            session_id=session_id,
            platform="douyin",
            theme="首发专场带货",
            status="stopped",
            total_gmv=12888.5,
            orders_count=66,
            danmaku_count=320,
            peak_viewers=1580,
            video_path=str(dummy_video),
            oss_url="https://test-live-bucket.oss-cn-hangzhou.aliyuncs.com/recordings/test.mp4",
            video_duration_sec=3600.0,
            video_size_bytes=len(dummy_video.read_bytes()),
            oss_upload_status="uploaded",
        )
        db.add(rec)
        await db.commit()

    # 3. 分页查询录像列表
    res_list = client.get("/api/v1/oss/records?page=1&page_size=10")
    assert res_list.status_code == 200
    d_list = res_list.json()
    assert d_list["code"] == 0
    items = d_list["data"]["items"]
    target = next((item for item in items if item["session_id"] == session_id), None)
    assert target is not None
    assert target["theme"] == "首发专场带货"
    assert target["total_gmv"] == 12888.5
    assert target["playable"] is True
    assert target["oss_upload_status"] == "uploaded"

    # 4. 获取在线复盘播放 URL
    res_play = client.get(f"/api/v1/oss/records/{session_id}/play-url")
    assert res_play.status_code == 200
    d_play = res_play.json()
    assert d_play["code"] == 0
    assert "play_url" in d_play["data"]
    assert len(d_play["data"]["play_url"]) > 0

    # 5. 测试手动重试上传接口
    res_retry = client.post(f"/api/v1/oss/records/{session_id}/retry-upload")
    assert res_retry.status_code == 200
    assert res_retry.json()["code"] == 0


def test_04_operations_tools_presence():
    """验证前端已成功装配运营工作导航卡片与指定的所有目标外链"""
    index_html = Path("server/static/index.html").read_text(encoding="utf-8")

    # 1. 验证左侧菜单导航项
    assert 'data-tab="operations"' in index_html
    assert '运营工作' in index_html
    assert 'data-tab="oss"' in index_html
    assert 'OSS配置(4)' in index_html

    # 2. 验证用户指定的 7 个实用工具网址全部就绪
    required_urls = [
        "https://www.clipto.com/zh-TW/features",  # 视频下载
        "https://shipindashi.cn/",               # 视频处理
        "https://www.aconvert.com/",             # 文字/格式处理
        "https://tinypng.com/",                  # 图片压缩
        "https://www.linshihaoma.com/",          # 临时短信
        "https://haoweichi.com/",                # 地址生成
        "https://tinywow.com/",                  # 综合工具
    ]

    for url in required_urls:
        assert url in index_html, f"前端页面中缺失工具导航链接: {url}"
