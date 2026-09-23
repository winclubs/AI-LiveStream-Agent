import uuid
import csv
from pathlib import Path
from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Query
from pydantic import BaseModel, Field
from typing import Optional
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, delete
from server.database.db import get_db
from server.database.models import ProhibitedWord, ProhibitedWordLog
from server.core.guardrails.aho_corasick import global_guardrail
from server.config import DATA_DIR
from server.core.resource_limits import (
    MAX_GUARDRAIL_BYTES,
    MAX_GUARDRAIL_CELL_CHARS,
    MAX_GUARDRAIL_COLUMNS,
    MAX_GUARDRAIL_EXPANDED_CHARS,
    MAX_GUARDRAIL_ROWS,
    check_archive_budget,
    cleanup_paths,
    stage_upload,
)

router = APIRouter(prefix="/guardrails", tags=["违禁词与合规拦截"])

class ProhibitedWordCreate(BaseModel):
    word: str = Field(min_length=1, max_length=128)
    category: str = Field(default="extreme", max_length=32)
    role_scope: str = Field(default="all", max_length=32)
    platform: str = Field(default="all", max_length=32)
    action_policy: str = Field(default="substitute", max_length=32)
    replacement_word: Optional[str] = Field(default="", max_length=128)

class TestSanitizeRequest(BaseModel):
    text: str = Field(min_length=1, max_length=20_000)
    role_scope: str = Field(default="all", max_length=32)
    platform: str = Field(default="all", max_length=32)


class BatchWordsRequest(BaseModel):
    words: str = Field(min_length=1, max_length=MAX_GUARDRAIL_EXPANDED_CHARS)
    replacement_word: str = Field(default="", max_length=128)
    category: str = Field(default="extreme", max_length=32)
    platform: str = Field(default="all", max_length=32)
    action_policy: str = Field(default="substitute", max_length=32)

@router.get("/words")
async def list_words(
    category: Optional[str] = None,
    platform: Optional[str] = None,
    db: AsyncSession = Depends(get_db)
):
    """获取违禁词与平替规则列表，支持按分类与平台筛选"""
    query = select(ProhibitedWord)
    if category and category != "all":
        query = query.where(ProhibitedWord.category == category)
    if platform and platform != "all":
        query = query.where(ProhibitedWord.platform == platform)
    result = await db.execute(query)
    words = result.scalars().all()
    return {
        "code": 0,
        "total": len(words),
        "data": [
            {
                "id": w.id,
                "word": w.word,
                "category": w.category,
                "role_scope": w.role_scope,
                "platform": getattr(w, "platform", "all") or "all",
                "action_policy": w.action_policy,
                "replacement_word": w.replacement_word,
                "is_enabled": bool(w.is_enabled)
            }
            for w in words
        ]
    }

@router.post("/words")
async def add_word(req: ProhibitedWordCreate, db: AsyncSession = Depends(get_db)):
    """新增违禁词规则并热加载"""
    word_clean = req.word.strip()
    if not word_clean:
        raise HTTPException(status_code=400, detail="违禁词内容不可为空")

    # 检查重复
    exist = await db.execute(select(ProhibitedWord).where(ProhibitedWord.word == word_clean))
    if exist.scalar_one_or_none():
        raise HTTPException(status_code=400, detail="该违禁词已存在")

    record = ProhibitedWord(
        id=f"pw_{uuid.uuid4().hex[:8]}",
        word=word_clean,
        category=req.category,
        role_scope=req.role_scope,
        platform=req.platform or "all",
        action_policy=req.action_policy,
        replacement_word=req.replacement_word or "",
        is_enabled=1
    )
    db.add(record)
    await db.commit()

    # 触发 AC 自动机热重载
    await reload_guardrails(db)
    return {"code": 0, "message": "违禁词已添加并热重载生效", "data": {"id": record.id}}


@router.post("/words/batch")
async def batch_add_words(req: BatchWordsRequest, db: AsyncSession = Depends(get_db)):
    """
    批量添加违禁词 (需求 7)：英文逗号分隔，一次添加任意数量
    支持按平台 (platform) 和分类批量录入
    """
    raw_words = [w.strip() for w in (req.words or "").replace("，", ",").split(",")]
    words = list(dict.fromkeys(w for w in raw_words if w))
    if len(words) > MAX_GUARDRAIL_ROWS:
        raise HTTPException(status_code=413, detail="单次批量添加超过 10000 条预算")
    if any(len(word) > MAX_GUARDRAIL_CELL_CHARS for word in words):
        raise HTTPException(status_code=413, detail="单个违禁词超过 256 字符预算")
    if not words:
        raise HTTPException(status_code=400, detail="请输入至少一个违禁词（多个词用英文逗号分隔）")
    if req.action_policy == "substitute" and not req.replacement_word.strip():
        raise HTTPException(status_code=400, detail="平替模式必须填写合规替换词")
    if req.action_policy not in ("substitute", "drop", "alert"):
        raise HTTPException(status_code=400, detail="拦截动作不合法")

    target_platform = (req.platform or "all").strip().lower()
    imported, skipped = 0, 0
    existing_result = await db.execute(select(ProhibitedWord.word).where(ProhibitedWord.word.in_(words)))
    existing_words = set(existing_result.scalars().all())
    for word in words:
        if word in existing_words:
            skipped += 1
            continue
        db.add(ProhibitedWord(
            id=f"pw_{uuid.uuid4().hex[:8]}",
            word=word,
            category=req.category,
            role_scope="all",  # 全直播间全角色通用
            platform=target_platform,
            action_policy=req.action_policy,
            replacement_word=req.replacement_word.strip(),
            is_enabled=1
        ))
        imported += 1

    await db.commit()
    if imported:
        await reload_guardrails(db)
    return {
        "code": 0,
        "message": f"批量添加完成：新增 {imported} 个违禁词，跳过重复 {skipped} 个，已热重载生效",
        "data": {"imported": imported, "skipped": skipped}
    }

@router.delete("/words/{word_id}")
async def delete_word(word_id: str, db: AsyncSession = Depends(get_db)):
    """删除违禁词并热重载"""
    await db.execute(delete(ProhibitedWord).where(ProhibitedWord.id == word_id))
    await db.commit()
    await reload_guardrails(db)
    return {"code": 0, "message": "违禁词已删除并热重载生效"}


VALID_CATEGORIES = {"extreme", "medical", "traffic", "competitor", "sensitive"}
VALID_ACTIONS = {"substitute", "drop", "alert"}


@router.post("/words/import")
async def import_words(file: UploadFile = File(...), db: AsyncSession = Depends(get_db)):
    """
    Excel/CSV/TXT 批量导入违禁词 (规划 §12.1)
    - TXT: 每行一词，支持 `词|类别|动作|平替词` 管道扩展格式
    - CSV: 表头 word,category,action_policy,replacement_word (Excel 另存 CSV 即可)
    - XLSX: 首列词，可选后三列 (需 openpyxl)
    """
    if not file.filename:
        raise HTTPException(status_code=400, detail="未选择文件")

    ext = Path(file.filename).suffix.lower()
    if ext not in (".txt", "", ".csv", ".xlsx"):
        raise HTTPException(status_code=400, detail="仅支持 TXT / CSV / XLSX 文件")

    staged = None
    rows: list[list[str]] = []
    expanded_chars = 0

    def add_row(values) -> None:
        nonlocal expanded_chars
        if len(rows) >= MAX_GUARDRAIL_ROWS:
            raise HTTPException(status_code=413, detail="导入文件超过 10000 行预算")
        values = list(values)
        if len(values) > MAX_GUARDRAIL_COLUMNS:
            raise HTTPException(status_code=413, detail="导入文件超过 4 列预算")
        cells = [("" if value is None else str(value).strip()) for value in values]
        if any(len(cell) > MAX_GUARDRAIL_CELL_CHARS for cell in cells):
            raise HTTPException(status_code=413, detail="导入单元格超过 256 字符预算")
        expanded_chars += sum(len(cell) for cell in cells)
        if expanded_chars > MAX_GUARDRAIL_EXPANDED_CHARS:
            raise HTTPException(status_code=413, detail="导入文本展开量超过 200 万字符预算")
        rows.append(cells)

    try:
        staged, _ = await stage_upload(file, DATA_DIR / "tmp" / "guardrails", "guardrails", MAX_GUARDRAIL_BYTES)
        if ext in (".txt", ""):
            text = staged.read_text(encoding="utf-8", errors="ignore")
            for line in text.splitlines():
                line = line.strip()
                if line and not line.startswith("#"):
                    add_row(line.split("|"))
        elif ext == ".csv":
            with staged.open("r", encoding="utf-8-sig", errors="ignore", newline="") as input_file:
                for row in csv.reader(input_file):
                    if row:
                        add_row(row)
        else:
            check_archive_budget(staged)
            try:
                import openpyxl
                workbook = openpyxl.load_workbook(staged, read_only=True, data_only=True)
                try:
                    for row in workbook.active.iter_rows(values_only=True):
                        add_row(row)
                finally:
                    workbook.close()
            except ImportError as exc:
                raise HTTPException(status_code=400, detail="解析 XLSX 需要安装 openpyxl 库 (pip install openpyxl)") from exc
    finally:
        cleanup_paths([staged])

    imported, skipped = 0, 0
    candidate_words = [row[0] for row in rows if row and row[0] and row[0].lower() not in ("word", "违禁词", "词")]
    existing_result = await db.execute(select(ProhibitedWord.word).where(ProhibitedWord.word.in_(candidate_words)))
    existing_words = set(existing_result.scalars().all())
    seen_words: set[str] = set()
    for row in rows:
        # 跳过表头行
        if row and row[0].lower() in ("word", "违禁词", "词"):
            continue
        if not row or not row[0]:
            continue

        word = row[0]
        category = row[1] if len(row) > 1 and row[1] in VALID_CATEGORIES else "extreme"
        action = row[2] if len(row) > 2 and row[2] in VALID_ACTIONS else "substitute"
        replacement = row[3] if len(row) > 3 else ""

        platform_val = row[4].strip().lower() if len(row) > 4 and row[4].strip() else "all"

        if word in existing_words or word in seen_words:
            skipped += 1
            continue
        seen_words.add(word)

        db.add(ProhibitedWord(
            id=f"pw_{uuid.uuid4().hex[:8]}",
            word=word,
            category=category,
            role_scope="all",
            platform=platform_val,
            action_policy=action,
            replacement_word=replacement or "",
            is_enabled=1
        ))
        imported += 1

    await db.commit()
    if imported:
        await reload_guardrails(db)
    return {"code": 0, "message": f"批量导入完成：新增 {imported} 条，跳过重复 {skipped} 条", "data": {"imported": imported, "skipped": skipped}}

@router.post("/test-sanitize")
async def test_sanitize(req: TestSanitizeRequest):
    """测试文本流式合规审计与平替，支持模拟主播角色与开播平台"""
    sanitized_text, hits, is_dropped = global_guardrail.sanitize(
        req.text,
        current_role=req.role_scope,
        current_platform=req.platform or "all"
    )
    return {
        "code": 0,
        "original_text": req.text,
        "sanitized_text": sanitized_text,
        "is_dropped": is_dropped,
        "hits": hits
    }

@router.get("/logs")
async def list_audit_logs(limit: int = Query(default=50, ge=1, le=500), db: AsyncSession = Depends(get_db)):
    """获取直播间触发的违规与平替审计流水日志"""
    query = select(ProhibitedWordLog).order_by(ProhibitedWordLog.created_at.desc()).limit(limit)
    res = await db.execute(query)
    logs = res.scalars().all()
    return {
        "code": 0,
        "total": len(logs),
        "data": [
            {
                "id": log.id,
                "session_id": log.session_id,
                "matched_word": log.matched_word,
                "category": log.category,
                "original_sentence": log.original_sentence,
                "processed_sentence": log.processed_sentence,
                "action_taken": log.action_taken,
                "created_at": log.created_at.isoformat() if log.created_at else ""
            }
            for log in logs
        ]
    }

async def reload_guardrails(db: AsyncSession):
    """从数据库重载违禁词到 AC 树"""
    res = await db.execute(select(ProhibitedWord).where(ProhibitedWord.is_enabled == 1))
    all_words = res.scalars().all()
    words_data = [
        {
            "word": w.word,
            "category": w.category,
            "role_scope": w.role_scope,
            "platform": getattr(w, "platform", "all") or "all",
            "action_policy": w.action_policy,
            "replacement_word": w.replacement_word,
            "is_enabled": w.is_enabled
        }
        for w in all_words
    ]
    global_guardrail.load_words(words_data)
