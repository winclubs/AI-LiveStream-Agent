"""
本地知识库管理路由 (规划 §5.4 / §12.1 /knowledge/upload)
支持上传 TXT/Markdown/CSV 直接解析，PDF/DOCX 依赖可选库自动探测
"""
import uuid
from pathlib import Path
from fastapi import APIRouter, Depends, UploadFile, File, Form, HTTPException
from pydantic import BaseModel, Field
from typing import Optional
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from server.database.db import get_db
from server.database.models import KnowledgeChunk
from server.core.rag.engine import global_rag
from server.core.cpu_worker import run_cpu_bound
from server.config import DATA_DIR
from server.core.resource_limits import (
    MAX_KNOWLEDGE_BYTES,
    MAX_KNOWLEDGE_PAGES,
    MAX_KNOWLEDGE_TEXT_CHARS,
    append_bounded,
    check_archive_budget,
    cleanup_paths,
    stage_upload,
)

router = APIRouter(prefix="/knowledge", tags=["知识库管理(RAG)"])

SUPPORTED_TEXT_EXTS = {".txt", ".md", ".markdown", ".csv"}


def _extract_text(filename: str, path: Path) -> str:
    """按页/段累计提取文本，任何格式都受统一字符预算约束。"""
    ext = Path(filename).suffix.lower()

    if ext in SUPPORTED_TEXT_EXTS:
        content = path.read_bytes()
        for encoding in ("utf-8", "gb18030"):
            try:
                text = content.decode(encoding)
                break
            except UnicodeDecodeError:
                continue
        else:
            text = content.decode("utf-8", errors="ignore")
        if len(text) > MAX_KNOWLEDGE_TEXT_CHARS:
            raise HTTPException(status_code=413, detail="文档文本超过 100 万字符预算")
        return text

    if ext == ".pdf":
        try:
            from pypdf import PdfReader
            reader = PdfReader(str(path))
            if len(reader.pages) > MAX_KNOWLEDGE_PAGES:
                raise HTTPException(status_code=413, detail="PDF 超过 200 页预算")
            parts: list[str] = []
            used = 0
            for page in reader.pages:
                used = append_bounded(parts, page.extract_text() or "", used, MAX_KNOWLEDGE_TEXT_CHARS, "PDF 文本超过 100 万字符预算")
            return "\n".join(parts)
        except ImportError as exc:
            raise HTTPException(status_code=400, detail="解析 PDF 需要安装 pypdf 库 (pip install pypdf)") from exc

    if ext == ".docx":
        check_archive_budget(path)
        try:
            import docx
            document = docx.Document(str(path))
            docx_parts: list[str] = []
            used = 0
            for paragraph in document.paragraphs:
                if paragraph.text.strip():
                    used = append_bounded(docx_parts, paragraph.text, used, MAX_KNOWLEDGE_TEXT_CHARS, "Word 文本超过 100 万字符预算")
            return "\n".join(docx_parts)
        except ImportError as exc:
            raise HTTPException(status_code=400, detail="解析 DOCX 需要安装 python-docx 库 (pip install python-docx)") from exc

    raise HTTPException(status_code=400, detail=f"暂不支持的文件格式: {ext} (支持 txt/md/csv/pdf/docx)")


class KnowledgeSearchRequest(BaseModel):
    query: str = Field(min_length=1, max_length=2000)
    top_k: int = Field(default=3, ge=1, le=50)
    min_score: Optional[float] = Field(default=None, ge=0.0, le=1.0)


MAX_KNOWLEDGE_FILE_SIZE = MAX_KNOWLEDGE_BYTES
ALL_ALLOWED_EXTS = {".txt", ".md", ".markdown", ".csv", ".pdf", ".docx"}
KNOWLEDGE_TMP_DIR = DATA_DIR / "tmp" / "knowledge"


@router.post("/upload")
async def upload_knowledge(
    file: UploadFile = File(...),
    doc_name: Optional[str] = Form(None),
    db: AsyncSession = Depends(get_db)
):
    """上传本地 PDF/Word/TXT 等专家文献并执行分块向量化索引 (上限 25MB)"""
    if not file.filename:
        raise HTTPException(status_code=400, detail="未选择文件")

    ext = Path(file.filename).suffix.lower()
    if ext not in ALL_ALLOWED_EXTS:
        raise HTTPException(status_code=400, detail=f"暂不支持的文件格式: {ext} (支持 txt/md/csv/pdf/docx)")

    if doc_name is not None and len(doc_name) > 256:
        raise HTTPException(status_code=422, detail="文档名称最多 256 字符")
    name = (doc_name or Path(file.filename).stem).strip() or "未命名文档"
    staged = None
    try:
        staged, _ = await stage_upload(file, KNOWLEDGE_TMP_DIR, "knowledge", MAX_KNOWLEDGE_FILE_SIZE)
        text = await run_cpu_bound(_extract_text, file.filename, staged)
        if not text.strip():
            raise HTTPException(status_code=400, detail="文档内容为空或无法提取文本")
        try:
            result = await global_rag.ingest_document(doc_name=name, text=text, source_path=file.filename)
        except ValueError as exc:
            raise HTTPException(status_code=413, detail=str(exc)) from exc
    finally:
        cleanup_paths([staged])
    return {
        "code": 0,
        "message": f"文档【{name}】已成功入库并建立双路混合索引",
        "data": {"doc_name": name, **result}
    }


@router.get("/status")
async def knowledge_status():
    """RAG 引擎运行态：向量后端 (onnx 真实语义 / hash 降级)、维度、分块数与阈值"""
    return {"code": 0, "data": global_rag.get_status()}


@router.get("/list")
async def list_knowledge(db: AsyncSession = Depends(get_db)):
    """按文档聚合查询已入库知识"""
    res = await db.execute(select(KnowledgeChunk))
    rows = res.scalars().all()
    docs: dict = {}
    for r in rows:
        doc = docs.setdefault(r.doc_id, {
            "doc_id": r.doc_id,
            "doc_name": r.doc_name,
            "chunk_count": 0,
            "preview": r.content[:80],
            "created_at": r.created_at.isoformat() if r.created_at else ""
        })
        doc["chunk_count"] += 1
    return {"code": 0, "total": len(docs), "data": list(docs.values())}


@router.post("/search")
async def search_knowledge(req: KnowledgeSearchRequest):
    """知识库双路混合检索测试接口"""
    result = await global_rag.search_async(req.query, top_k=req.top_k, min_score=req.min_score)
    return {"code": 0, "data": result}


@router.delete("/{doc_id}")
async def delete_knowledge(doc_id: str):
    """删除指定知识文档全部分块"""
    removed = await global_rag.delete_document(doc_id)
    if not removed:
        raise HTTPException(status_code=404, detail="知识文档不存在")
    return {"code": 0, "message": f"已删除 {removed} 个知识分块"}
