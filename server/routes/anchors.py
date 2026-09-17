"""
主播管理路由 (需求 4)：主播增删改查
支持上传 形象照(正面)/全身照/半身照/侧面照，绑定音色与备注
"""
import uuid
import cv2
from pathlib import Path
from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession
from server.database.db import get_db
from server.database.models import Anchor, AppSetting, AvatarAction, AvatarTask, LiveSessionRecord, VoiceProfile
from server.config import DATA_DIR
from server.core.avatar import get_avatar_task_manager, get_action_state_machine
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
AVATAR_TASKS_DIR = DATA_DIR / "avatar_tasks"
AVATAR_TASKS_DIR.mkdir(parents=True, exist_ok=True)
AVATAR_ACTIONS_DIR = DATA_DIR / "avatar_actions"
AVATAR_ACTIONS_DIR.mkdir(parents=True, exist_ok=True)

MAX_VIDEO_BYTES = 200 * 1024 * 1024  # 200MB 视频切片训练预算
VALID_VIDEO_EXTS = {".mp4", ".mov", ".avi", ".webm", ".mkv"}

PHOTO_FIELDS = {
    "portrait": "photo_portrait",      # 形象照（正面）
    "full_body": "photo_full_body",    # 全身照
    "half_body": "photo_half_body",    # 半身照
    "side": "photo_side",              # 侧面照
}


VALID_ANCHOR_TYPES = {"ecommerce", "entertainment", "expert", "chat"}


def _anchor_payload(a: Anchor, voice_name: str | None = None) -> dict:
    return {
        "id": a.id,
        "name": a.name,
        "anchor_type": a.anchor_type or "ecommerce",
        "voice_id": a.voice_id,
        "voice_name": voice_name if voice_name is not None else (a.voice_id if a.voice_id else "未绑定"),
        "remark": a.remark or "",
        "photos": {
            "portrait": a.photo_portrait or "",
            "full_body": a.photo_full_body or "",
            "half_body": a.photo_half_body or "",
            "side": a.photo_side or ""
        },
        "avatar_asset_dir": a.avatar_asset_dir or "",
        "source_video": a.source_video or "",
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
        from server.core.security.file_validator import validate_file_content
        head = staged.read_bytes()[:128]
        is_valid, reason = validate_file_content(head, allowed_categories={"image"}, filename=file.filename or "")
        if not is_valid:
            raise HTTPException(status_code=400, detail=f"主播照片安全核验未通过: {reason}")
        validate_image_budget(staged)
    except BaseException:
        cleanup_paths([staged])
        raise
    target = ANCHORS_DIR / f"{anchor_id}_{slot_key}_{uuid.uuid4().hex[:8]}{ext}"
    return staged, target


@router.get("/list")
async def list_anchors(db: AsyncSession = Depends(get_db)):
    """获取全部主播档案（本地数据库单次查询联表直出音色名称、引擎与开播状态，零远程依赖）"""
    selected_setting = await db.get(AppSetting, "selected_anchor_id")
    selected_anchor_id = selected_setting.value if selected_setting else ""

    stmt = (
        select(Anchor, VoiceProfile.name.label("voice_name"), VoiceProfile.provider_name.label("voice_provider"))
        .outerjoin(VoiceProfile, Anchor.voice_id == VoiceProfile.id)
        .order_by(Anchor.created_at.desc())
    )
    res = await db.execute(stmt)
    rows = res.all()

    data = []
    for anchor, v_name, v_provider in rows:
        payload = _anchor_payload(anchor, voice_name=v_name)
        payload["voice_provider"] = v_provider or ""
        payload["is_current_live"] = bool(anchor.id and anchor.id == selected_anchor_id)
        data.append(payload)

    return {
        "code": 0,
        "total": len(data),
        "selected_anchor_id": selected_anchor_id,
        "data": data
    }


@router.post("/create")
async def create_anchor(
    name: str = Form(...),
    anchor_type: str = Form("ecommerce"),
    voice_id: str = Form(""),
    remark: str = Form(""),
    portrait: UploadFile = File(None),
    full_body: UploadFile = File(None),
    half_body: UploadFile = File(None),
    side: UploadFile = File(None),
    db: AsyncSession = Depends(get_db)
):
    """新增主播：姓名、主播类型、音色、备注与最多四类照片"""
    if not name.strip():
        raise HTTPException(status_code=400, detail="主播姓名不可为空")
    if len(name.strip()) > 128 or len(voice_id) > 64 or len(remark) > 2000 or len(anchor_type) > 32:
        raise HTTPException(status_code=422, detail="主播姓名、音色 ID、类型或备注超过资源预算")

    clean_type = anchor_type.strip() if anchor_type.strip() in VALID_ANCHOR_TYPES else "ecommerce"
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
            anchor_type=clean_type,
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
    anchor_type: str | None = Form(None),
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
    type_provided = "anchor_type" in raw_form
    voice_value = str(raw_form.get("voice_id") or "") if voice_provided else None
    remark_value = str(raw_form.get("remark") or "") if remark_provided else None
    type_value = str(raw_form.get("anchor_type") or "").strip() if type_provided else None
    if (
        len(id) > 64
        or len(name.strip()) > 128
        or (voice_value is not None and len(voice_value) > 64)
        or (remark_value is not None and len(remark_value) > 2000)
        or (type_value is not None and len(type_value) > 32)
    ):
        raise HTTPException(status_code=422, detail="主播资料字段超过资源预算")
    res = await db.execute(select(Anchor).where(Anchor.id == id))
    record = res.scalar_one_or_none()
    if not record:
        raise HTTPException(status_code=404, detail="主播不存在")

    # 确定 anchor_type 的值：优先从形参中读取，其次从 raw_form 兜底
    clean_type = (anchor_type or "").strip()
    if not clean_type and type_provided and type_value:
        clean_type = type_value
    valid_type = (clean_type if clean_type in VALID_ANCHOR_TYPES else "ecommerce") if clean_type else None

    update_fields = {}
    if name.strip():
        update_fields["name"] = name.strip()
        record.name = name.strip()
    if valid_type:
        update_fields["anchor_type"] = valid_type
        record.anchor_type = valid_type
    if voice_provided:
        update_fields["voice_id"] = voice_value or None
        record.voice_id = voice_value or None
    if remark_provided:
        update_fields["remark"] = remark_value or ""
        record.remark = remark_value or ""

    if update_fields:
        await db.execute(
            update(Anchor).where(Anchor.id == id).values(**update_fields)
        )

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
        db.add(record)
        await db.commit()
        await db.refresh(record)
    except BaseException:
        await db.rollback()
        cleanup_paths([pair[0] for pair in staged_pairs.values()] + published)
        raise
    cleanup_paths(old_paths)
    return {"code": 0, "message": f"主播【{record.name}】资料已更新", "data": _anchor_payload(record)}
# ============================================================================
# 数字人视频切片与训练任务工场 (Phase 2 Avatar Task Management)
# ============================================================================

@router.post("/avatar/task")
async def create_avatar_task(
    name: str = Form("真人视频数字人"),
    anchor_id: str = Form(""),
    video_path: str = Form(""),
    file: UploadFile = File(None),
    db: AsyncSession = Depends(get_db)
):
    """
    提交一段 1~2 分钟真人说话视频，启动后台切片、面部关键点提取与数字人资产训练任务
    """
    task_id = f"task_{uuid.uuid4().hex[:10]}"
    final_video_path = ""

    if file and file.filename:
        ext = Path(file.filename).suffix.lower() or ".mp4"
        if ext not in VALID_VIDEO_EXTS:
            raise HTTPException(status_code=400, detail="数字人训练视频仅支持 MP4/MOV/AVI/WebM/MKV 格式")
        task_dir = AVATAR_TASKS_DIR / task_id
        task_dir.mkdir(parents=True, exist_ok=True)
        staged, _ = await stage_upload(file, task_dir, "input_video", MAX_VIDEO_BYTES)
        target_video = task_dir / f"input{ext}"
        publish_staged(staged, target_video)
        final_video_path = target_video.as_posix()
    elif video_path.strip():
        vp = Path(video_path.strip())
        if not vp.exists() or not vp.is_file():
            raise HTTPException(status_code=400, detail="指定的视频路径不存在或不是有效文件")
        if vp.suffix.lower() not in VALID_VIDEO_EXTS:
            raise HTTPException(status_code=400, detail="指定的视频格式不受支持")
        final_video_path = vp.as_posix()
    else:
        raise HTTPException(status_code=400, detail="请上传真人说话视频文件或指定已有视频路径")

    target_anchor_id = anchor_id.strip() or None
    if target_anchor_id:
        anchor_rec = await db.get(Anchor, target_anchor_id)
        if not anchor_rec:
            raise HTTPException(status_code=404, detail="绑定的目标主播不存在")

    clean_name = name.strip() or "真人视频数字人"
    from server.core.avatar.task_manager import AVATAR_ASSETS_DIR
    output_dir = (AVATAR_ASSETS_DIR / task_id).as_posix()

    task_record = AvatarTask(
        id=task_id,
        anchor_id=target_anchor_id,
        name=clean_name,
        status="pending",
        progress=0,
        stage_message="任务已提交，进入调度队列...",
        video_path=final_video_path,
        output_dir=output_dir,
    )
    db.add(task_record)
    await db.commit()

    manager = get_avatar_task_manager()
    await manager.submit_task(
        task_id=task_id,
        name=clean_name,
        video_path=final_video_path,
        anchor_id=target_anchor_id,
        output_dir=output_dir,
    )

    return {
        "code": 0,
        "message": "数字人视频切片任务已提交并启动后台训练",
        "data": {
            "task_id": task_id,
            "name": clean_name,
            "anchor_id": target_anchor_id or "",
            "status": "pending",
            "progress": 0,
            "output_dir": output_dir,
        }
    }


@router.get("/avatar/tasks")
async def list_avatar_tasks(
    anchor_id: str | None = None,
    limit: int = 50,
):
    """查询数字人视频切片任务列表"""
    manager = get_avatar_task_manager()
    tasks = await manager.list_tasks(anchor_id=anchor_id, limit=limit)
    return {
        "code": 0,
        "total": len(tasks),
        "data": tasks
    }


@router.get("/avatar/tasks/{task_id}")
async def get_avatar_task(task_id: str):
    """轮询单个切片训练任务的实时进度与日志"""
    manager = get_avatar_task_manager()
    task_info = await manager.get_task_status(task_id)
    if not task_info:
        raise HTTPException(status_code=404, detail="任务不存在")
    return {
        "code": 0,
        "data": task_info
    }


@router.post("/avatar/tasks/{task_id}/apply")
async def apply_avatar_task(
    task_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db)
):
    """将已完成的切片资产一键应用绑定至指定主播"""
    data = {}
    if request.headers.get("content-type", "").startswith("application/json"):
        data = await request.json()
    else:
        form = await request.form()
        data = dict(form)

    target_anchor_id = str(data.get("anchor_id", "")).strip()
    if not target_anchor_id:
        raise HTTPException(status_code=400, detail="必须指定目标主播 ID")

    anchor = await db.get(Anchor, target_anchor_id)
    if not anchor:
        raise HTTPException(status_code=404, detail="目标主播不存在")

    task = await db.get(AvatarTask, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="切片任务不存在")
    if task.status != "completed":
        raise HTTPException(status_code=400, detail=f"该任务尚未完成训练 (当前状态: {task.status})，无法应用")

    anchor.avatar_asset_dir = task.output_dir or ""
    anchor.source_video = task.video_path or ""
    task.anchor_id = anchor.id

    if task.output_dir:
        cand_preview = Path(task.output_dir) / "preview.jpg"
        if cand_preview.exists() and not anchor.photo_portrait:
            anchor.photo_portrait = cand_preview.as_posix()

    await db.commit()
    await db.refresh(anchor)

    return {
        "code": 0,
        "message": f"数字人切片资产已成功绑定至主播【{anchor.name}】",
        "data": _anchor_payload(anchor)
    }


@router.post("/avatar/tasks/{task_id}/cancel")
async def cancel_avatar_task(task_id: str):
    """取消进行中或等待中的切片训练任务"""
    manager = get_avatar_task_manager()
    success = await manager.cancel_task(task_id)
    if not success:
        raise HTTPException(status_code=400, detail="任务无法取消或不存在")
    return {"code": 0, "message": "数字人切片任务已取消"}


@router.delete("/avatar/tasks/{task_id}")
async def delete_avatar_task(task_id: str, db: AsyncSession = Depends(get_db)):
    """删除切片训练任务记录并清理磁盘缓存"""
    import shutil
    manager = get_avatar_task_manager()
    await manager.cancel_task(task_id)

    task = await db.get(AvatarTask, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")

    # 清理磁盘产物
    if task.output_dir and Path(task.output_dir).exists():
        try:
            shutil.rmtree(task.output_dir, ignore_errors=True)
        except Exception:
            pass

    if task.video_path and Path(task.video_path).exists():
        try:
            vp = Path(task.video_path)
            if "avatar_tasks" in vp.parts:
                shutil.rmtree(vp.parent, ignore_errors=True)
        except Exception:
            pass

    await db.delete(task)
    await db.commit()
    return {"code": 0, "message": "切片任务已删除"}


# ==========================================
# 🎭 数字人动作切片状态机与电商带货场景绑定 API (阶段三)
# ==========================================

@router.get("/avatar/actions")
async def list_avatar_actions(
    anchor_id: str | None = None,
    db: AsyncSession = Depends(get_db)
):
    """
    获取动作切片状态机配置列表。
    支持按主播 ID 过滤，并附带切片帧数与当前状态机运行态指标。
    """
    stmt = select(AvatarAction)
    if anchor_id:
        stmt = stmt.where((AvatarAction.anchor_id == anchor_id) | (AvatarAction.anchor_id.is_(None)))
    else:
        stmt = stmt.where(AvatarAction.anchor_id.is_(None))
    stmt = stmt.order_by(AvatarAction.action_code.asc())
    res = await db.execute(stmt)
    records = res.scalars().all()

    action_sm = get_action_state_machine()
    data = []
    for r in records:
        clip = action_sm.clips.get(r.action_code)
        frames_count = clip.total_frames if clip else 0
        if frames_count == 0 and r.frames_dir and Path(r.frames_dir).exists():
            frames_count = len(list(Path(r.frames_dir).glob("*.jpg")))

        data.append({
            "id": r.id,
            "anchor_id": r.anchor_id,
            "action_code": r.action_code,
            "action_name": r.action_name,
            "video_path": r.video_path or "",
            "frames_dir": r.frames_dir or "",
            "frames_count": frames_count,
            "trigger_type": r.trigger_type or "both",
            "trigger_keywords": r.trigger_keywords or "",
            "trigger_events": r.trigger_events or "",
            "duration_sec": float(r.duration_sec or 3.5),
            "priority": int(r.priority or 1),
            "mirror_loop": bool(r.mirror_loop),
            "is_active": bool(r.is_active),
            "created_at": r.created_at.isoformat() if r.created_at else "",
        })

    return {
        "code": 0,
        "total": len(data),
        "current_status": action_sm.get_status(),
        "data": data
    }


@router.post("/avatar/actions")
async def save_avatar_action(
    request: Request,
    db: AsyncSession = Depends(get_db)
):
    """
    创建或更新数字人动作状态机配置
    """
    body = await request.json()
    action_id = str(body.get("id", "")).strip()
    action_code = body.get("action_code")
    if action_code is None:
        raise HTTPException(status_code=400, detail="必须指定 action_code")
    try:
        action_code = int(action_code)
    except (ValueError, TypeError):
        raise HTTPException(status_code=400, detail="action_code 必须为整数")

    action_name = str(body.get("action_name", "")).strip()
    if not action_name:
        raise HTTPException(status_code=400, detail="动作名称不可为空")

    anchor_id = str(body.get("anchor_id", "")).strip() or None
    trigger_type = str(body.get("trigger_type", "both")).strip()
    trigger_keywords = str(body.get("trigger_keywords", "")).strip()
    trigger_events = str(body.get("trigger_events", "")).strip()
    duration_sec = float(body.get("duration_sec", 3.5))
    priority = int(body.get("priority", 1))
    mirror_loop = 1 if body.get("mirror_loop", True) else 0
    is_active = 1 if body.get("is_active", True) else 0

    record = None
    if action_id:
        record = await db.get(AvatarAction, action_id)
    if not record:
        stmt = select(AvatarAction).where(
            AvatarAction.action_code == action_code,
            AvatarAction.anchor_id == anchor_id
        )
        res = await db.execute(stmt)
        record = res.scalar_one_or_none()

    if record:
        record.action_name = action_name
        record.trigger_type = trigger_type
        record.trigger_keywords = trigger_keywords
        record.trigger_events = trigger_events
        record.duration_sec = duration_sec
        record.priority = priority
        record.mirror_loop = mirror_loop
        record.is_active = is_active
    else:
        record = AvatarAction(
            id=action_id or f"act_{uuid.uuid4().hex[:8]}",
            anchor_id=anchor_id,
            action_code=action_code,
            action_name=action_name,
            trigger_type=trigger_type,
            trigger_keywords=trigger_keywords,
            trigger_events=trigger_events,
            duration_sec=duration_sec,
            priority=priority,
            mirror_loop=mirror_loop,
            is_active=is_active,
        )
        db.add(record)

    await db.commit()
    await db.refresh(record)

    action_sm = get_action_state_machine()
    await action_sm.load_configs_from_db(anchor_id)

    return {
        "code": 0,
        "message": f"动作【{record.action_name}】配置保存成功并已生效",
        "data": {
            "id": record.id,
            "action_code": record.action_code,
            "action_name": record.action_name,
            "priority": record.priority,
            "duration_sec": record.duration_sec,
        }
    }


@router.post("/avatar/actions/{action_id}/upload-clip")
async def upload_action_clip(
    action_id: str,
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_db)
):
    """
    上传动作切片短视频 (MP4/WebM)，自动解码抽帧至切片目录并热重载
    """
    record = await db.get(AvatarAction, action_id)
    if not record:
        raise HTTPException(status_code=404, detail="动作不存在")

    ext = Path(file.filename or "").suffix.lower()
    if ext not in VALID_VIDEO_EXTS:
        raise HTTPException(status_code=400, detail=f"不支持的视频格式: {ext}，仅支持 {VALID_VIDEO_EXTS}")

    clip_dir = AVATAR_ACTIONS_DIR / record.id
    clip_dir.mkdir(parents=True, exist_ok=True)
    video_dest = clip_dir / f"clip{ext}"

    content = await file.read()
    if len(content) > MAX_VIDEO_BYTES:
        raise HTTPException(status_code=400, detail=f"视频切片不能超过 {MAX_VIDEO_BYTES // 1024 // 1024}MB")

    from server.core.security.file_validator import validate_file_content
    is_valid, reason = validate_file_content(content, allowed_categories={"video"}, filename=file.filename or "")
    if not is_valid:
        raise HTTPException(status_code=400, detail=f"动作切片视频安全校验未通过: {reason}")

    video_dest.write_bytes(content)

    frames_dir = clip_dir / "frames"
    frames_dir.mkdir(parents=True, exist_ok=True)

    for old_f in frames_dir.glob("*.jpg"):
        try:
            old_f.unlink()
        except Exception:
            pass

    cap = cv2.VideoCapture(str(video_dest))
    frame_idx = 0
    MAX_CLIP_FRAMES = 3000
    if cap.isOpened():
        while frame_idx < MAX_CLIP_FRAMES:
            ret, frame = cap.read()
            if not ret or frame is None:
                break
            cv2.imwrite(str(frames_dir / f"{frame_idx:06d}.jpg"), frame, [int(cv2.IMWRITE_JPEG_QUALITY), 90])
            frame_idx += 1
        cap.release()

    record.video_path = str(video_dest.as_posix())
    record.frames_dir = str(frames_dir.as_posix())
    await db.commit()
    await db.refresh(record)

    action_sm = get_action_state_machine()
    await action_sm.load_configs_from_db(record.anchor_id)

    return {
        "code": 0,
        "message": f"动作【{record.action_name}】切片视频已解码加载完成，共 {frame_idx} 帧",
        "data": {
            "id": record.id,
            "action_code": record.action_code,
            "video_path": record.video_path,
            "frames_dir": record.frames_dir,
            "total_frames": frame_idx
        }
    }


@router.post("/avatar/actions/test-trigger")
async def test_trigger_action(
    request: Request
):
    """
    手动调试触发动作状态机
    """
    body = await request.json()
    action_code = int(body.get("action_code", 0))
    duration = float(body.get("duration", 3.5)) if body.get("duration") else None
    priority = int(body.get("priority", 1)) if body.get("priority") else None

    action_sm = get_action_state_machine()
    switched = action_sm.trigger_action(
        action_code=action_code,
        source="manual_test",
        duration=duration,
        priority=priority
    )
    return {
        "code": 0 if switched else 1,
        "message": f"动作切换成功 (code={action_code})" if switched else "动作抢占被忽略 (当前运行中动作优先级更高)",
        "current_status": action_sm.get_status()
    }


@router.delete("/avatar/actions/{action_id}")
async def delete_avatar_action(
    action_id: str,
    db: AsyncSession = Depends(get_db)
):
    """
    删除自定义动作配置及对应的切片视频
    """
    import shutil
    record = await db.get(AvatarAction, action_id)
    if not record:
        raise HTTPException(status_code=404, detail="动作不存在")

    if record.action_code == 0:
        raise HTTPException(status_code=400, detail="默认待机呼吸动作不可删除")

    clip_dir = AVATAR_ACTIONS_DIR / record.id
    if clip_dir.exists():
        try:
            shutil.rmtree(clip_dir, ignore_errors=True)
        except Exception:
            pass

    anchor_id = record.anchor_id
    await db.delete(record)
    await db.commit()

    action_sm = get_action_state_machine()
    await action_sm.load_configs_from_db(anchor_id)

    return {"code": 0, "message": f"动作【{record.action_name}】已删除"}


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
