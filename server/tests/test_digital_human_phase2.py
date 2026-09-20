# -*- coding: utf-8 -*-
"""
数字人核心演进阶段二全功能验证测试套件 (Phase 2 Test Suite)
覆盖：
1. 异步任务管理器与端到端切片提取管线 (视频帧全切片 full_imgs、人脸特征 coords.pkl、音频分离 audio.wav、元数据 meta.json)；
2. POST /api/v1/anchors/avatar/task 提交视频任务与资源预算校验；
3. GET /api/v1/anchors/avatar/tasks/{task_id} 进度轮询与状态机流转 (pending -> processing -> completed)；
4. GET /api/v1/anchors/avatar/tasks 列表多维度查询；
5. POST /api/v1/anchors/avatar/tasks/{task_id}/apply 一键绑定资产到目标主播；
6. 取消任务与清理任务接口；
7. 异常防御性测试 (无效格式拦截、非法视频路径拦截、缺失主播 ID 等)。
"""
import asyncio
import os
import pickle
import wave
from pathlib import Path
import cv2
import numpy as np
import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from server.app import app
from server.database.db import AsyncSessionLocal
from server.database.models import Anchor, Avatar, AvatarTask
from server.core.avatar.task_manager import get_avatar_task_manager


@pytest.fixture
def anyio_backend():
    return "asyncio"


def _create_dummy_mp4_video(output_path: Path, frame_count: int = 15, width: int = 320, height: int = 240) -> Path:
    """生成用于测试的微型合成 MP4 视频，包含模拟人脸区域"""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    fourcc = cv2.VideoWriter_fourcc(*"mp4v")
    out = cv2.VideoWriter(str(output_path), fourcc, 25.0, (width, height))

    for i in range(frame_count):
        # 纯色背景 + 模拟人脸矩形与五官
        frame = np.full((height, width, 3), 40, dtype=np.uint8)
        # 模拟人脸椭圆
        cv2.ellipse(frame, (width // 2, height // 2), (50, 70), 0, 0, 360, (200, 180, 150), -1)
        # 模拟眼睛
        cv2.circle(frame, (width // 2 - 20, height // 2 - 20), 8, (30, 30, 30), -1)
        cv2.circle(frame, (width // 2 + 20, height // 2 - 20), 8, (30, 30, 30), -1)
        # 模拟嘴巴
        cv2.rectangle(frame, (width // 2 - 15, height // 2 + 25), (width // 2 + 15, height // 2 + 35), (20, 20, 180), -1)
        out.write(frame)

    out.release()
    return output_path


@pytest.mark.anyio
async def test_avatar_task_pipeline_end_to_end(tmp_path):
    """验证切片处理流水线完整端到端：帧切片、coords.pkl、audio.wav、meta.json 产物完整性"""
    video_file = _create_dummy_mp4_video(tmp_path / "test_avatar_sample.mp4", frame_count=12)
    output_dir = tmp_path / "avatar_out_001"

    # 创建主播用于关联
    anchor_id = "test_anchor_phase2_01"
    async with AsyncSessionLocal() as db:
        anchor = Anchor(id=anchor_id, name="阶段二测试主播", anchor_type="ecommerce")
        db.add(anchor)
        await db.commit()

    manager = get_avatar_task_manager()
    task_id = "task_test_pipeline_001"

    # 先在数据库中记录任务
    async with AsyncSessionLocal() as db:
        t_rec = AvatarTask(
            id=task_id,
            anchor_id=anchor_id,
            name="端到端测试任务",
            status="pending",
            progress=0,
            video_path=video_file.as_posix(),
            output_dir=output_dir.as_posix(),
        )
        db.add(t_rec)
        await db.commit()

    # 提交执行
    res = await manager.submit_task(
        task_id=task_id,
        name="端到端测试任务",
        video_path=video_file.as_posix(),
        anchor_id=anchor_id,
        output_dir=output_dir.as_posix(),
    )
    assert res["task_id"] == task_id
    assert res["status"] == "pending"

    # 等待异步任务执行完成 (最长等待 8s，平时 <1s)
    status_info = None
    for _ in range(80):
        status_info = await manager.get_task_status(task_id)
        if status_info and status_info["status"] in ("completed", "failed", "cancelled"):
            break
        await asyncio.sleep(0.1)

    assert status_info is not None
    assert status_info["status"] == "completed", f"任务失败原因: {status_info.get('error_message')}"
    assert status_info["progress"] == 100

    # 验证磁盘产物
    assert (output_dir / "full_imgs").exists()
    extracted_imgs = list((output_dir / "full_imgs").glob("*.jpg"))
    assert len(extracted_imgs) == 12

    # 验证 face_imgs (标准数字人裁剪序列，Wav2Lip推理必备)
    assert (output_dir / "face_imgs").exists()
    face_imgs = list((output_dir / "face_imgs").glob("*.jpg"))
    assert len(face_imgs) == 12

    # 验证 coords.pkl
    coords_file = output_dir / "coords.pkl"
    assert coords_file.exists()
    with open(coords_file, "rb") as f:
        coords = pickle.load(f)
    assert isinstance(coords, list)
    assert len(coords) == 12
    for c in coords:
        assert len(c) == 4
        # (ymin, ymax, xmin, xmax)
        assert c[0] <= c[1] and c[2] <= c[3]

    # 验证 audio.wav
    audio_file = output_dir / "audio.wav"
    assert audio_file.exists()
    assert audio_file.stat().st_size > 44

    # 验证 preview.jpg 与 meta.json
    assert (output_dir / "preview.jpg").exists()
    meta_file = output_dir / "meta.json"
    assert meta_file.exists()
    import json as _json
    meta_data = _json.loads(meta_file.read_text(encoding="utf-8"))
    assert meta_data.get("face_imgs_count") == 12
    assert meta_data.get("face_img_size") == 256

    # 验证数据库中主播信息自动更新
    async with AsyncSessionLocal() as db:
        updated_anchor = await db.get(Anchor, anchor_id)
        assert updated_anchor.avatar_asset_dir == output_dir.as_posix()
        assert updated_anchor.source_video == video_file.as_posix()



@pytest.mark.anyio
async def test_avatar_task_api_submit_and_poll(tmp_path):
    """测试通过 REST API 上传视频并轮询进度接口"""
    video_file = _create_dummy_mp4_video(tmp_path / "api_test_clip.mp4", frame_count=8)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # 1. 提交切片任务
        with open(video_file, "rb") as vf:
            resp = await client.post(
                "/api/v1/anchors/avatar/task",
                data={"name": "API测试主播数字人"},
                files={"file": ("api_test_clip.mp4", vf, "video/mp4")},
            )
        assert resp.status_code == 200
        res_json = resp.json()
        assert res_json["code"] == 0
        task_id = res_json["data"]["task_id"]

        # 2. 轮询任务进度直至完成
        completed = False
        p_data = {}
        for _ in range(80):
            p_resp = await client.get(f"/api/v1/anchors/avatar/tasks/{task_id}")
            assert p_resp.status_code == 200
            p_data = p_resp.json()["data"]
            if p_data["status"] == "completed":
                completed = True
                assert p_data["progress"] == 100
                break
            await asyncio.sleep(0.1)

        assert completed is True, f"任务状态未按期完成: {p_data.get('status')}"

        # 3. 验证任务列表端点
        list_resp = await client.get("/api/v1/anchors/avatar/tasks")
        assert list_resp.status_code == 200
        tasks = list_resp.json()["data"]
        assert any(t["id"] == task_id for t in tasks)


@pytest.mark.anyio
async def test_avatar_task_apply_to_anchor(tmp_path):
    """测试将切片资产一键应用至指定主播"""
    video_file = _create_dummy_mp4_video(tmp_path / "apply_clip.mp4", frame_count=5)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # 创建待绑定的主播
        create_res = await client.post(
            "/api/v1/anchors/create",
            data={"name": "待绑定资产主播", "anchor_type": "entertainment"}
        )
        assert create_res.status_code == 200
        target_anchor_id = create_res.json()["data"]["id"]

        # 提交切片任务 (未指定主播)
        with open(video_file, "rb") as vf:
            sub_res = await client.post(
                "/api/v1/anchors/avatar/task",
                data={"name": "通用资产模型"},
                files={"file": ("apply_clip.mp4", vf, "video/mp4")},
            )
        task_id = sub_res.json()["data"]["task_id"]

        # 等待完成
        task_done = False
        for _ in range(80):
            p_resp = await client.get(f"/api/v1/anchors/avatar/tasks/{task_id}")
            if p_resp.json().get("data", {}).get("status") == "completed":
                task_done = True
                break
            await asyncio.sleep(0.1)
        assert task_done is True, "切片任务未在规定时间内完成处理"

        # 一键应用绑定
        apply_res = await client.post(
            f"/api/v1/anchors/avatar/tasks/{task_id}/apply",
            json={"anchor_id": target_anchor_id}
        )
        assert apply_res.status_code == 200
        applied_data = apply_res.json()["data"]
        assert applied_data["id"] == target_anchor_id
        assert applied_data["avatar_asset_dir"] != ""


@pytest.mark.anyio
async def test_avatar_task_cancellation_and_deletion(tmp_path):
    """测试取消任务与清理任务记录及磁盘产物"""
    video_file = _create_dummy_mp4_video(tmp_path / "cancel_clip.mp4", frame_count=20)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        with open(video_file, "rb") as vf:
            sub_res = await client.post(
                "/api/v1/anchors/avatar/task",
                data={"name": "待取消任务"},
                files={"file": ("cancel_clip.mp4", vf, "video/mp4")},
            )
        task_id = sub_res.json()["data"]["task_id"]

        # 调用取消
        cancel_res = await client.post(f"/api/v1/anchors/avatar/tasks/{task_id}/cancel")
        assert cancel_res.status_code == 200
        assert cancel_res.json()["code"] == 0

        # 查询状态已变更
        poll_res = await client.get(f"/api/v1/anchors/avatar/tasks/{task_id}")
        assert poll_res.json()["data"]["status"] in ("cancelled", "failed", "completed")

        # 删除任务
        del_res = await client.delete(f"/api/v1/anchors/avatar/tasks/{task_id}")
        assert del_res.status_code == 200
        assert del_res.json()["code"] == 0

        # 再次查询应返回 404
        after_res = await client.get(f"/api/v1/anchors/avatar/tasks/{task_id}")
        assert after_res.status_code == 404


@pytest.mark.anyio
async def test_avatar_task_validation_errors():
    """测试非法请求格式拦截"""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # 1. 缺失文件与路径
        res1 = await client.post("/api/v1/anchors/avatar/task", data={"name": "空任务"})
        assert res1.status_code == 400

        # 2. 上传不支持的文件格式 (如 txt)
        res2 = await client.post(
            "/api/v1/anchors/avatar/task",
            data={"name": "错误格式"},
            files={"file": ("test.txt", b"hello world", "text/plain")}
        )
        assert res2.status_code == 400

        # 3. 关联不存在的主播
        res3 = await client.post(
            "/api/v1/anchors/avatar/task",
            data={"name": "不存在主播", "anchor_id": "non_existent_anchor_123", "video_path": "fake.mp4"}
        )
        assert res3.status_code in (400, 404)

        # 4. 查询不存在的任务
        res4 = await client.get("/api/v1/anchors/avatar/tasks/task_not_exist_999")
        assert res4.status_code == 404


@pytest.mark.anyio
async def test_create_anchor_with_direct_video_upload(tmp_path):
    """验证主播管理优化：创建主播时直接上传视频切片，自动关联并生成 video_task_id"""
    video_file = _create_dummy_mp4_video(tmp_path / "anchor_video_sample.mp4", frame_count=6)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        with open(video_file, "rb") as vf:
            res = await client.post(
                "/api/v1/anchors/create",
                data={
                    "name": "直接带视频创建的主播",
                    "anchor_type": "ecommerce",
                    "remark": "一键上传视频自动化测试",
                },
                files={"video": ("sample.mp4", vf, "video/mp4")},
            )
        assert res.status_code == 200
        data = res.json()["data"]
        assert data["name"] == "直接带视频创建的主播"
        assert "video_task_id" in data
        assert data["video_task_id"].startswith("task_")

        # 验证该切片任务已经在调度管理中
        task_id = data["video_task_id"]
        poll_res = await client.get(f"/api/v1/anchors/avatar/tasks/{task_id}")
        assert poll_res.status_code == 200
        assert poll_res.json()["data"]["anchor_id"] == data["id"]


@pytest.mark.anyio
async def test_gpu_target_api_and_branches():
    """验证算力双分支设置接口与分支切换"""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # 1. 查询当前默认偏好
        get_res = await client.get("/api/v1/settings/gpu-target")
        assert get_res.status_code == 200
        assert "target" in get_res.json()["data"]

        # 2. 切换为 local 偏好
        post_res1 = await client.post("/api/v1/settings/gpu-target", json={"target": "local"})
        assert post_res1.status_code == 200
        assert post_res1.json()["data"]["target"] == "local"

        # 3. 切换为 cloud 偏好
        post_res2 = await client.post("/api/v1/settings/gpu-target", json={"target": "cloud"})
        assert post_res2.status_code == 200
        assert post_res2.json()["data"]["target"] == "cloud"

        # 4. 恢复 auto
        post_res3 = await client.post("/api/v1/settings/gpu-target", json={"target": "auto"})
        assert post_res3.status_code == 200
        assert post_res3.json()["data"]["target"] == "auto"

        # 5. 非法参数校验拦截
        err_res = await client.post("/api/v1/settings/gpu-target", json={"target": "invalid_mode"})
        assert err_res.status_code == 400


@pytest.mark.anyio
async def test_update_anchor_with_video_upload(tmp_path):
    """验证主播编辑时更新上传视频，自动启动新切片流水线"""
    video_file = _create_dummy_mp4_video(tmp_path / "update_video_sample.mp4", frame_count=5)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # 1. 先创建一个基础主播
        init_res = await client.post(
            "/api/v1/anchors/create",
            data={"name": "待编辑的主播", "anchor_type": "ecommerce"}
        )
        assert init_res.status_code == 200
        anchor_id = init_res.json()["data"]["id"]

        # 2. 编辑该主播并上传视频
        with open(video_file, "rb") as vf:
            update_res = await client.post(
                "/api/v1/anchors/update",
                data={
                    "id": anchor_id,
                    "name": "待编辑的主播(已更新视频)",
                    "remark": "编辑时上传视频测试",
                },
                files={"video": ("new_sample.mp4", vf, "video/mp4")},
            )
        assert update_res.status_code == 200
        update_data = update_res.json()["data"]
        assert update_data["name"] == "待编辑的主播(已更新视频)"
        assert "video_task_id" in update_data
        assert update_data["video_task_id"].startswith("task_")


