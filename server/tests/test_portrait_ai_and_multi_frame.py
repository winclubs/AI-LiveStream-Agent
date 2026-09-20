# -*- coding: utf-8 -*-
"""
多画面智能人脸优选与最佳真人形象照自动提取测试套件
参考 LiveTalking 原生算法哲学：无需冗余 LLM 生图，通过多画面抽样和 OpenCV 图像质量评分，
毫秒级提取最优质真人正面人脸帧作为主播形象照。
"""
from pathlib import Path
import cv2
import numpy as np
import pytest
from httpx import ASGITransport, AsyncClient

from server.app import app
from server.core.avatar.portrait_selector import evaluate_frame_quality, smart_extract_best_face_portrait
from server.database.db import AsyncSessionLocal
from server.database.models import Anchor


@pytest.fixture
def anyio_backend():
    return "asyncio"


def _create_test_video_with_black_intro(output_path: Path, black_frames: int = 10, face_frames: int = 15) -> Path:
    """生成前段为全黑屏、后段为清晰人像的测试视频"""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    width, height = 320, 240
    fourcc = cv2.VideoWriter_fourcc(*"mp4v")
    out = cv2.VideoWriter(str(output_path), fourcc, 25.0, (width, height))

    # 1. 写入黑屏过渡 (模拟片头或黑屏无人物)
    for _ in range(black_frames):
        black_img = np.zeros((height, width, 3), dtype=np.uint8)
        out.write(black_img)

    # 2. 写入高对比清晰人脸画面 (模拟主播正面入镜)
    for _ in range(face_frames):
        frame = np.full((height, width, 3), 180, dtype=np.uint8)
        # 脸部椭圆
        cv2.ellipse(frame, (width // 2, height // 2), (50, 70), 0, 0, 360, (220, 200, 180), -1)
        # 眼睛
        cv2.circle(frame, (width // 2 - 20, height // 2 - 20), 8, (10, 10, 10), -1)
        cv2.circle(frame, (width // 2 + 20, height // 2 - 20), 8, (10, 10, 10), -1)
        # 嘴唇
        cv2.rectangle(frame, (width // 2 - 15, height // 2 + 25), (width // 2 + 15, height // 2 + 35), (20, 20, 180), -1)
        out.write(frame)

    out.release()
    return output_path


def test_evaluate_frame_quality_filters_black_frame():
    """测试单帧质量评估：纯黑屏应被严重扣分并过滤"""
    black_frame = np.zeros((240, 320, 3), dtype=np.uint8)
    score_black, face_rect = evaluate_frame_quality(black_frame)
    assert score_black < -500.0
    assert face_rect is None

    # 正常明亮帧
    bright_frame = np.full((240, 320, 3), 150, dtype=np.uint8)
    score_bright, _ = evaluate_frame_quality(bright_frame)
    assert score_bright > 0.0


def test_smart_extract_best_face_portrait_skips_black_frames(tmp_path):
    """测试多画面智能抽样：自动跳过前段黑屏，选出明亮有效帧并成功保存"""
    video_path = _create_test_video_with_black_intro(tmp_path / "black_intro.mp4", black_frames=12, face_frames=15)
    output_portrait = tmp_path / "best_portrait.jpg"

    ok, path_or_err, meta = smart_extract_best_face_portrait(
        video_path=str(video_path),
        output_image_path=str(output_portrait),
        sample_count=8
    )

    assert ok is True
    assert output_portrait.exists()
    assert output_portrait.stat().st_size > 0
    # 选中的帧必须位于后段有效画面中（索引大于黑屏段）
    assert meta["selected_frame_index"] >= 10
    assert meta["selected_score"] > 0


@pytest.mark.anyio
async def test_anchor_create_video_auto_extracts_best_portrait(tmp_path):
    """测试主播创建并上传视频时，后端自动多画面采样提取最佳帧存为形象照"""
    video_path = _create_test_video_with_black_intro(tmp_path / "anchor_video.mp4", black_frames=10, face_frames=15)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        with open(video_path, "rb") as vf:
            create_res = await client.post(
                "/api/v1/anchors/create",
                data={"name": "智能多帧主播", "anchor_type": "ecommerce"},
                files={"video": ("test_vid.mp4", vf, "video/mp4")}
            )
        assert create_res.status_code == 200
        res_data = create_res.json()["data"]
        anchor_id = res_data["id"]
        portrait_rel = res_data["photos"]["portrait"]

        # 验证形象照已被自动生成且非空
        assert portrait_rel != ""
        assert portrait_rel.endswith(".jpg")

        # 验证数据库中主播的 photo_portrait 已持久化
        async with AsyncSessionLocal() as db:
            anchor_rec = await db.get(Anchor, anchor_id)
            assert anchor_rec is not None
            assert anchor_rec.photo_portrait == portrait_rel


@pytest.mark.anyio
async def test_avatar_task_video_upload_auto_updates_anchor_portrait(tmp_path):
    """测试在数字人工场中上传视频启动任务时，自动为目标主播提取最佳正脸并回填头像"""
    video_path = _create_test_video_with_black_intro(tmp_path / "factory_video.mp4", black_frames=10, face_frames=15)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # 1. 创建一个无照片无视频的纯档案主播
        create_res = await client.post(
            "/api/v1/anchors/create",
            data={"name": "工场测试主播", "anchor_type": "ecommerce"}
        )
        assert create_res.status_code == 200
        anchor_id = create_res.json()["data"]["id"]
        assert create_res.json()["data"]["photos"]["portrait"] == ""

        # 2. 在工场提交数字人训练任务并绑定该主播
        with open(video_path, "rb") as vf:
            task_res = await client.post(
                "/api/v1/anchors/avatar/task",
                data={"name": "工场测试数字人", "anchor_id": anchor_id},
                files={"file": ("factory_vid.mp4", vf, "video/mp4")}
            )
        assert task_res.status_code == 200
        assert task_res.json()["code"] == 0

        # 3. 验证该主播的 photo_portrait 已被自动提取并写入
        async with AsyncSessionLocal() as db:
            anchor_rec = await db.get(Anchor, anchor_id)
            assert anchor_rec is not None
            assert anchor_rec.photo_portrait is not None
            assert anchor_rec.photo_portrait != ""
            assert anchor_rec.photo_portrait.endswith(".jpg")

        # 4. 测试 GET /api/v1/anchors/{anchor_id}/avatar-detail
        detail_res = await client.get(f"/api/v1/anchors/{anchor_id}/avatar-detail")
        assert detail_res.status_code == 200
        detail_data = detail_res.json()["data"]
        assert detail_data["anchor_id"] == anchor_id
        assert detail_data["anchor_name"] == "工场测试主播"
        assert "source_video_url" in detail_data

        # 5. 测试 GET /api/v1/anchors/{anchor_id}/source-video
        video_res = await client.get(f"/api/v1/anchors/{anchor_id}/source-video")
        assert video_res.status_code in (200, 206)

        # 6. 测试 GET /api/v1/anchors/list 包含 avatar_meta 字段
        list_res = await client.get("/api/v1/anchors/list")
        assert list_res.status_code == 200
        anchors_list = list_res.json()["data"]
        found = next((a for a in anchors_list if a["id"] == anchor_id), None)
        assert found is not None
        assert "avatar_meta" in found
        assert found["avatar_task_id"] != ""

