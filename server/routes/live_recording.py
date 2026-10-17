# -*- coding: utf-8 -*-
"""直播切片录制：启动/停止/状态/列表/删除。

由 server/routes/live.py 拆分而来。
本模块不引用任何「缝」，因此无需函数体内 import。
"""
import asyncio

from fastapi import APIRouter, Body, HTTPException
from pydantic import BaseModel, Field

from server.core.media.recorder import get_record_manager

router = APIRouter()


# ============================================================================
# 🎥 短视频与带货切片一键录制导出系统 (/record) (阶段四)
# ============================================================================

class StartRecordRequest(BaseModel):
    title: str = Field("带货讲解切片", description="切片标题")
    sku: str = Field("", description="绑定的商品 SKU")
    width: int = Field(1280, description="视频宽度")
    height: int = Field(720, description="视频高度")


@router.post("/record/start", summary="启动直播短视频讲解切片录制")
async def start_clip_recording(req: StartRecordRequest = Body(default_factory=lambda: StartRecordRequest())):
    """
    一键开始录制当前数字人直播画面的 1080P/720P 高清切片
    """
    recorder_mgr = get_record_manager()
    res = recorder_mgr.start_recording(
        title=req.title,
        sku=req.sku,
        width=req.width,
        height=req.height
    )
    if res.get("code") != 0:
        raise HTTPException(status_code=400, detail=res.get("message", "启动录制失败"))
    return res


@router.post("/record/stop", summary="停止录制并封装导出 MP4 切片")
async def stop_clip_recording():
    """
    停止录制，自动调用 FFmpeg 封装带声画同步的标准 H.264 MP4 文件 (线程池异步封装，不阻塞事件循环)
    """
    recorder_mgr = get_record_manager()
    res = await asyncio.to_thread(recorder_mgr.stop_recording)
    if res.get("code") != 0:
        raise HTTPException(status_code=400, detail=res.get("message", "停止录制失败"))
    return res


@router.get("/record/status", summary="查询当前录制状态与计时")
async def get_recording_status():
    """查询当前是否正在录制、录制时长与累计帧数"""
    recorder_mgr = get_record_manager()
    return {
        "code": 0,
        "data": recorder_mgr.get_status()
    }


@router.get("/record/list", summary="查询已录制带货切片列表")
async def list_recordings():
    """查询历史生成的带货讲解 MP4 切片资产列表"""
    recorder_mgr = get_record_manager()
    items = recorder_mgr.list_recordings()
    return {
        "code": 0,
        "total": len(items),
        "data": items
    }


@router.delete("/record/{record_id}", summary="删除切片视频文件")
async def delete_recording(record_id: str):
    """删除指定的带货切片视频文件及目录"""
    recorder_mgr = get_record_manager()
    success = recorder_mgr.delete_recording(record_id)
    return {
        "code": 0 if success else 1,
        "message": "切片文件已删除"
    }
