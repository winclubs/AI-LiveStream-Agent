import uuid
from pathlib import Path
from fastapi import APIRouter, Depends, UploadFile, File, Form, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, delete
from server.database.db import get_db
from server.database.models import Avatar
from server.config import DATA_DIR
from server.core.cpu_worker import run_cpu_bound
from server.core.resource_limits import (
    MAX_IMAGE_BYTES,
    cleanup_paths,
    publish_staged,
    stage_upload,
    validate_image_budget,
)

router = APIRouter(prefix="/avatars", tags=["数字人形象工厂"])

AVATARS_DIR = DATA_DIR / "avatars"
AVATARS_DIR.mkdir(parents=True, exist_ok=True)

@router.get("/list")
async def list_avatars(db: AsyncSession = Depends(get_db)):
    """获取所有已录入的主播数字人形象"""
    result = await db.execute(select(Avatar))
    avatars = result.scalars().all()
    return {
        "code": 0,
        "total": len(avatars),
        "data": [
            {
                "id": a.id,
                "name": a.name,
                "avatar_type": a.avatar_type,
                "source_file_path": a.source_file_path,
                "preprocessed_cache_path": a.preprocessed_cache_path,
                "created_at": a.created_at.isoformat() if a.created_at else ""
            }
            for a in avatars
        ]
    }

@router.post("/create")
async def create_avatar(
    name: str = Form(...),
    file: UploadFile = File(...),
    avatar_type: str = Form("image"),
    db: AsyncSession = Depends(get_db)
):
    """
    上传单张正脸肖像照片（PNG/JPG）或短视频（MP4）并预处理特征
    """
    if not file.filename:
        raise HTTPException(status_code=400, detail="未选择有效文件")

    clean_name = name.strip()
    if not clean_name or len(clean_name) > 128:
        raise HTTPException(status_code=422, detail="形象名称长度必须为 1 到 128 字符")
    if avatar_type not in ("image", "video"):
        raise HTTPException(status_code=422, detail="形象类型仅支持 image 或 video")

    avatar_id = f"avatar_{uuid.uuid4().hex[:8]}"
    ext = Path(file.filename).suffix.lower() or ".jpg"
    target_path = AVATARS_DIR / f"{avatar_id}{ext}"
    cache_path = AVATARS_DIR / f"{avatar_id}_landmarks.json"
    staged = None
    landmark_info = {}
    try:
        staged, _ = await stage_upload(file, AVATARS_DIR, avatar_id, MAX_IMAGE_BYTES)
        from server.core.security.file_validator import validate_file_content
        head = staged.read_bytes()[:128]
        is_valid, reason = validate_file_content(head, allowed_categories={avatar_type}, filename=file.filename or "")
        if not is_valid:
            raise HTTPException(status_code=400, detail=f"数字人形象资产文件安全核验失败: {reason}")
        if avatar_type == "image":
            await run_cpu_bound(validate_image_budget, staged)
        publish_staged(staged, target_path)
        staged = None

        if avatar_type == "image":
            from server.core.vision.face_landmarks import detect_face_landmarks
            landmark_info = await run_cpu_bound(
                detect_face_landmarks,
                target_path.as_posix(),
                cache_path.as_posix(),
            )

        record = Avatar(
            id=avatar_id,
            name=clean_name,
            avatar_type=avatar_type,
            source_file_path=target_path.as_posix(),
            preprocessed_cache_path=cache_path.as_posix() if avatar_type == "image" else "",
        )
        db.add(record)
        await db.commit()
    except BaseException:
        await db.rollback()
        cleanup_paths([staged, target_path, cache_path])
        raise

    return {
        "code": 0,
        "message": "数字人形象已成功录入并完成人脸特征预提取",
        "data": {
            "id": record.id,
            "name": record.name,
            "path": record.source_file_path,
            "landmarks": landmark_info
        }
    }

@router.delete("/{avatar_id}")
async def delete_avatar(avatar_id: str, db: AsyncSession = Depends(get_db)):
    """删除指定数字人形象"""
    res = await db.execute(select(Avatar).where(Avatar.id == avatar_id))
    record = res.scalar_one_or_none()
    if not record:
        raise HTTPException(status_code=404, detail="形象不存在")

    # 尝试删除磁盘文件
    try:
        if record.source_file_path and Path(record.source_file_path).exists():
            Path(record.source_file_path).unlink(missing_ok=True)
        if record.preprocessed_cache_path and Path(record.preprocessed_cache_path).exists():
            Path(record.preprocessed_cache_path).unlink(missing_ok=True)
    except Exception:
        pass

    await db.execute(delete(Avatar).where(Avatar.id == avatar_id))
    await db.commit()
    return {"code": 0, "message": "形象已成功删除"}
