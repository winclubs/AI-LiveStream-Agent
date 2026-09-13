"""
主播管理路由 (需求 4)：主播增删改查
支持上传 形象照(正面)/全身照/半身照/侧面照，绑定音色与备注
"""
import uuid
from pathlib import Path
from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession
from server.database.db import get_db
from server.database.models import Anchor, AppSetting, LiveSessionRecord
from server.config import DATA_DIR
from server.core.resource_limits import (
    MAX_IMAGE_BYTES,
    cleanup_paths,
    publish_staged,
    stage_upload,
    validate_image_budget,
)

router = APIRouter(prefix="/anchors", tags=["主播管理"])

ANCHORS_DIR = DATA_DIR / "anchors"
ANCHORS_DIR.mkdir(parents=True, exist_ok=True)

PHOTO_FIELDS = {
    "portrait": "photo_portrait",      # 形象照（正面）
    "full_body": "photo_full_body",    # 全身照
    "half_body": "photo_half_body",    # 半身照
    "side": "photo_side",              # 侧面照
}


def _anchor_payload(a: Anchor) -> dict:
    return {
        "id": a.id,
        "name": a.name,
        "voice_id": a.voice_id,
        "remark": a.remark or "",
        "photos": {
            "portrait": a.photo_portrait or "",
            "full_body": a.photo_full_body or "",
            "half_body": a.photo_half_body or "",
            "side": a.photo_side or ""
        },
        "created_at": a.created_at.isoformat() if a.created_at else ""
    }


async def _stage_photo(anchor_id: str, slot_key: str, file: UploadFile) -> tuple[Path, Path] | None:
    """暂存并验证单张照片，返回（暂存路径，最终路径）。"""
    if not file or not file.filename:
        return None
    ext = Path(file.filename).suffix.lower() or ".jpg"
    if ext not in (".jpg", ".jpeg", ".png", ".webp", ".bmp"):
        raise HTTPException(status_code=400, detail="主播照片仅支持 jpg/png/webp/bmp")
    staged, _ = await stage_upload(file, ANCHORS_DIR, f"{anchor_id}_{slot_key}", MAX_IMAGE_BYTES)
    try:
        validate_image_budget(staged)
    except BaseException:
        cleanup_paths([staged])
        raise
    target = ANCHORS_DIR / f"{anchor_id}_{slot_key}_{uuid.uuid4().hex[:8]}{ext}"
    return staged, target


@router.get("/list")
async def list_anchors(db: AsyncSession = Depends(get_db)):
    """获取全部主播档案"""
    res = await db.execute(select(Anchor))
    anchors = res.scalars().all()
    return {"code": 0, "total": len(anchors), "data": [_anchor_payload(a) for a in anchors]}


@router.post("/create")
async def create_anchor(
    name: str = Form(...),
    voice_id: str = Form(""),
    remark: str = Form(""),
    portrait: UploadFile = File(None),
    full_body: UploadFile = File(None),
    half_body: UploadFile = File(None),
    side: UploadFile = File(None),
    db: AsyncSession = Depends(get_db)
):
    """新增主播：姓名、音色、备注与最多四类照片"""
    if not name.strip():
        raise HTTPException(status_code=400, detail="主播姓名不可为空")
    if len(name.strip()) > 128 or len(voice_id) > 64 or len(remark) > 2000:
        raise HTTPException(status_code=422, detail="主播姓名、音色 ID 或备注超过资源预算")

    anchor_id = f"anchor_{uuid.uuid4().hex[:8]}"
    uploads = {"portrait": portrait, "full_body": full_body, "half_body": half_body, "side": side}
    staged_pairs: dict[str, tuple[Path, Path]] = {}
    published: list[Path] = []
    try:
        for slot_key, uploaded in uploads.items():
            pair = await _stage_photo(anchor_id, slot_key, uploaded)
            if pair:
                staged_pairs[slot_key] = pair
        record = Anchor(
            id=anchor_id,
            name=name.strip(),
            voice_id=voice_id or None,
            remark=remark or "",
        )
        for slot_key, pair in staged_pairs.items():
            staged, target = pair
            publish_staged(staged, target)
            published.append(target)
            setattr(record, PHOTO_FIELDS[slot_key], target.as_posix())
        db.add(record)
        await db.commit()
    except BaseException:
        await db.rollback()
        cleanup_paths([pair[0] for pair in staged_pairs.values()] + published)
        raise
    return {"code": 0, "message": f"主播【{record.name}】已创建", "data": _anchor_payload(record)}


@router.post("/update")
async def update_anchor(
    request: Request,
    id: str = Form(...),
    name: str = Form(""),
    voice_id: str | None = Form(None),
    remark: str | None = Form(None),
    portrait: UploadFile = File(None),
    full_body: UploadFile = File(None),
    half_body: UploadFile = File(None),
    side: UploadFile = File(None),
    db: AsyncSession = Depends(get_db)
):
    """编辑主播资料与照片（照片留空则保持不变）。"""
    raw_form = await request.form()
    voice_provided = "voice_id" in raw_form
    remark_provided = "remark" in raw_form
    voice_value = str(raw_form.get("voice_id") or "") if voice_provided else None
    remark_value = str(raw_form.get("remark") or "") if remark_provided else None
    if (
        len(id) > 64
        or len(name.strip()) > 128
        or (voice_value is not None and len(voice_value) > 64)
        or (remark_value is not None and len(remark_value) > 2000)
    ):
        raise HTTPException(status_code=422, detail="主播资料字段超过资源预算")
    res = await db.execute(select(Anchor).where(Anchor.id == id))
    record = res.scalar_one_or_none()
    if not record:
        raise HTTPException(status_code=404, detail="主播不存在")

    if name.strip():
        record.name = name.strip()
    # 原始表单键缺省表示保持；键存在且值为空才表示解除绑定或清空。
    if voice_provided:
        record.voice_id = voice_value or None
    if remark_provided:
        record.remark = remark_value or ""

    uploads = {"portrait": portrait, "full_body": full_body, "half_body": half_body, "side": side}
    staged_pairs: dict[str, tuple[Path, Path]] = {}
    published: list[Path] = []
    old_paths: list[Path] = []
    try:
        for slot_key, uploaded in uploads.items():
            pair = await _stage_photo(record.id, slot_key, uploaded)
            if pair:
                staged_pairs[slot_key] = pair
        for slot_key, pair in staged_pairs.items():
            current = getattr(record, PHOTO_FIELDS[slot_key], "")
            if current:
                old_paths.append(Path(current))
            staged, target = pair
            publish_staged(staged, target)
            published.append(target)
            setattr(record, PHOTO_FIELDS[slot_key], target.as_posix())
        await db.commit()
    except BaseException:
        await db.rollback()
        cleanup_paths([pair[0] for pair in staged_pairs.values()] + published)
        raise
    cleanup_paths(old_paths)
    return {"code": 0, "message": f"主播【{record.name}】资料已更新", "data": _anchor_payload(record)}


@router.delete("/{anchor_id}")
async def delete_anchor(anchor_id: str, db: AsyncSession = Depends(get_db)):
    """删除主播档案并清理照片文件"""
    res = await db.execute(select(Anchor).where(Anchor.id == anchor_id))
    record = res.scalar_one_or_none()
    if not record:
        raise HTTPException(status_code=404, detail="主播不存在")

    for col in PHOTO_FIELDS.values():
        path = getattr(record, col, "")
        if path and Path(path).exists():
            try:
                Path(path).unlink()
            except Exception:
                pass

    # 历史场次保留但解绑主播；若当前设置指向该主播则同步清空。
    await db.execute(update(LiveSessionRecord).where(LiveSessionRecord.anchor_id == anchor_id).values(anchor_id=None))
    selected = await db.get(AppSetting, "selected_anchor_id")
    if selected and selected.value == anchor_id:
        selected.value = ""
    await db.delete(record)
    await db.commit()
    return {"code": 0, "message": f"主播【{record.name}】已删除"}
