# -*- coding: utf-8 -*-
"""
阿里云 OSS 存储与直播录像复盘 API 路由 (OSS & Live Recordings Router)
"""
import asyncio
import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import desc, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from server.core.media.oss_uploader import global_oss_uploader
from server.database.db import get_db
from server.database.models import LiveSessionRecord

logger = logging.getLogger("LiveAgent.OSSRoutes")
router = APIRouter(prefix="/oss", tags=["阿里云OSS与直播录像复盘"])


# ---------------------------------------------------------------------------
# 请求与响应模型
# ---------------------------------------------------------------------------
class OSSConfigReq(BaseModel):
    endpoint: str = Field(..., description="OSS 地域节点 Endpoint")
    bucket: str = Field(..., description="Bucket 存储空间名称")
    access_key_id: str = Field(..., description="AccessKey ID")
    access_key_secret: str = Field("", description="AccessKey Secret (传空或包含***则保留旧秘钥)")
    custom_domain: Optional[str] = Field("", description="自定义绑定域名或 CDN 加速域名")
    prefix: Optional[str] = Field("recordings/", description="云端存储目录前缀")
    auto_upload: Optional[bool] = Field(True, description="直播结束后是否自动上传")
    is_active: Optional[bool] = Field(True, description="是否启用 OSS 存储")


class OSSTestReq(BaseModel):
    endpoint: str
    bucket: str
    access_key_id: str
    access_key_secret: str


# ---------------------------------------------------------------------------
# 1. OSS 配置管理端点
# ---------------------------------------------------------------------------
@router.get("/config")
async def get_oss_config():
    """获取阿里云 OSS 当前配置 (密钥脱敏)"""
    cfg = await global_oss_uploader.get_config(mask_secret=True)
    return {"code": 0, "data": cfg, "message": "获取成功"}


@router.post("/config")
async def save_oss_config(req: OSSConfigReq):
    """保存或更新阿里云 OSS 配置"""
    res = await global_oss_uploader.save_config(
        endpoint=req.endpoint,
        bucket=req.bucket,
        access_key_id=req.access_key_id,
        access_key_secret=req.access_key_secret,
        custom_domain=req.custom_domain or "",
        prefix=req.prefix or "recordings/",
        auto_upload=bool(req.auto_upload),
        is_active=bool(req.is_active),
    )
    return {"code": 0, "data": res, "message": "OSS 配置保存成功"}


@router.post("/test")
async def test_oss_connection(req: OSSTestReq):
    """测试 OSS 存储桶连通性与权限"""
    success, msg = await global_oss_uploader.test_connectivity(
        endpoint=req.endpoint,
        bucket=req.bucket,
        access_key_id=req.access_key_id,
        access_key_secret=req.access_key_secret,
    )
    return {
        "code": 0 if success else 1,
        "data": {"connected": success, "message": msg},
        "message": msg,
    }


# ---------------------------------------------------------------------------
# 2. 直播场次录像与复盘列表查询
# ---------------------------------------------------------------------------
@router.get("/records")
async def list_live_recordings(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
):
    """
    分页获取直播场次历史记录与录像信息
    支持在线复盘播放、大小统计与状态查询
    """
    offset = (page - 1) * page_size
    stmt = (
        select(LiveSessionRecord)
        .order_by(desc(LiveSessionRecord.start_time))
        .offset(offset)
        .limit(page_size)
    )
    res = await db.execute(stmt)
    records = res.scalars().all()

    # 查询总数
    from sqlalchemy import func
    total_res = await db.execute(select(func.count(LiveSessionRecord.session_id)))
    total = total_res.scalar() or 0

    items = []
    for r in records:
        # 计算时长
        dur_sec = r.video_duration_sec or 0.0
        if not dur_sec and r.end_time and r.start_time:
            dur_sec = round((r.end_time - r.start_time).total_seconds(), 1)

        has_local = bool(r.video_path and Path(r.video_path).exists())
        has_oss = bool(r.oss_url)

        items.append({
            "session_id": r.session_id,
            "platform": r.platform or "bilibili",
            "theme": r.theme or "日常带货直播",
            "status": r.status,
            "start_time": r.start_time.strftime("%Y-%m-%d %H:%M:%S") if r.start_time else "",
            "end_time": r.end_time.strftime("%Y-%m-%d %H:%M:%S") if r.end_time else "",
            "duration_sec": dur_sec,
            "duration_formatted": f"{int(dur_sec // 60)}分{int(dur_sec % 60)}秒" if dur_sec > 0 else "0秒",
            "total_gmv": round(float(r.total_gmv or 0.0), 2),
            "orders_count": r.orders_count or 0,
            "danmaku_count": r.danmaku_count or 0,
            "peak_viewers": r.peak_viewers or 0,
            "gift_income": round(float(r.gift_income or 0.0), 2),
            "video_path": r.video_path or "",
            "oss_url": r.oss_url or "",
            "preview_image_url": r.preview_image_url or "",
            "video_size_bytes": r.video_size_bytes or 0,
            "video_size_mb": round((r.video_size_bytes or 0) / (1024 * 1024), 2),
            "oss_upload_status": r.oss_upload_status or "none",
            "oss_error_msg": r.oss_error_msg or "",
            "has_local_video": has_local,
            "has_oss_video": has_oss,
            "playable": bool(has_local or has_oss),
        })

    return {
        "code": 0,
        "data": {
            "total": total,
            "page": page,
            "page_size": page_size,
            "items": items,
        },
        "message": "获取成功",
    }


# ---------------------------------------------------------------------------
# 3. 视频播放与防盗链直链生成
# ---------------------------------------------------------------------------
@router.get("/records/{session_id}/play-url")
async def get_record_play_url(session_id: str, db: AsyncSession = Depends(get_db)):
    """获取可直接用于网页 `<video>` 播放的 URL (优先 OSS 带签名防盗链直链，本地备选)"""
    stmt = select(LiveSessionRecord).where(LiveSessionRecord.session_id == session_id).limit(1)
    res = await db.execute(stmt)
    record = res.scalar_one_or_none()

    if not record:
        raise HTTPException(status_code=404, detail="未找到该场次记录")

    # 1. 优先获取 OSS 播放直链 (若为私有 Bucket 则自动签名防盗链)
    if record.oss_url:
        signed_play_url = await global_oss_uploader.generate_play_url(record.oss_url, expires_sec=86400)
        return {
            "code": 0,
            "data": {
                "session_id": session_id,
                "play_url": signed_play_url,
                "source": "oss",
                "theme": record.theme,
            },
            "message": "获取云端播放地址成功",
        }

    # 2. 本地回退：提供本地流媒体分片播放 URL
    if record.video_path and Path(record.video_path).exists():
        local_stream_url = f"/api/v1/oss/stream/{session_id}"
        return {
            "code": 0,
            "data": {
                "session_id": session_id,
                "play_url": local_stream_url,
                "source": "local",
                "theme": record.theme,
            },
            "message": "获取本地录像播放地址成功",
        }

    raise HTTPException(status_code=404, detail="该场次暂无可播放的录像视频")


@router.get("/stream/{session_id}")
async def stream_local_video(
    session_id: str,
    range: Optional[str] = Header(None),
    db: AsyncSession = Depends(get_db),
):
    """
    支持 HTTP 206 Partial Content 分片断点续传的本地录像流式传输
    允许用户在浏览器视频播放器中随意快进快退拖动时间轴
    """
    stmt = select(LiveSessionRecord).where(LiveSessionRecord.session_id == session_id).limit(1)
    res = await db.execute(stmt)
    record = res.scalar_one_or_none()

    if not record or not record.video_path or not Path(record.video_path).exists():
        raise HTTPException(status_code=404, detail="录像文件未找到")

    video_file = Path(record.video_path)
    file_size = video_file.stat().st_size

    # 处理 Range 头
    if range:
        range_val = range.replace("bytes=", "").strip()
        parts = range_val.split("-")
        start = int(parts[0]) if parts[0] else 0
        end = int(parts[1]) if len(parts) > 1 and parts[1] else file_size - 1
        end = min(end, file_size - 1)
        chunk_size = end - start + 1

        def _file_chunk_generator():
            with open(video_file, "rb") as f:
                f.seek(start)
                bytes_left = chunk_size
                while bytes_left > 0:
                    read_size = min(bytes_left, 64 * 1024)
                    data = f.read(read_size)
                    if not data:
                        break
                    bytes_left -= len(data)
                    yield data

        headers = {
            "Content-Range": f"bytes {start}-{end}/{file_size}",
            "Accept-Ranges": "bytes",
            "Content-Length": str(chunk_size),
            "Content-Type": "video/mp4",
        }
        return StreamingResponse(_file_chunk_generator(), status_code=206, headers=headers)

    # 完整文件返回
    return FileResponse(
        video_file,
        media_type="video/mp4",
        filename=f"live_{session_id}.mp4",
        headers={"Accept-Ranges": "bytes"},
    )


# ---------------------------------------------------------------------------
# 4. 手动重新上传与重试
# ---------------------------------------------------------------------------
@router.post("/records/{session_id}/retry-upload")
async def retry_upload_record(session_id: str, db: AsyncSession = Depends(get_db)):
    """手动触发失败录像重新上传到阿里云 OSS"""
    stmt = select(LiveSessionRecord).where(LiveSessionRecord.session_id == session_id).limit(1)
    res = await db.execute(stmt)
    record = res.scalar_one_or_none()

    if not record:
        raise HTTPException(status_code=404, detail="未找到该场次记录")

    if not record.video_path or not Path(record.video_path).exists():
        raise HTTPException(status_code=400, detail="本地录像文件已不存在，无法重试上传")

    # 启动后台异步上传协程
    asyncio.create_task(
        global_oss_uploader.upload_session_recording_async(
            session_id=session_id,
            video_path=record.video_path,
            preview_path=record.preview_image_url or "",
        )
    )

    return {"code": 0, "message": "重新上传任务已派发，正在后台上传至阿里云 OSS"}
