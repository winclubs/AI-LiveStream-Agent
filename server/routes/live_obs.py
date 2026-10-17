# -*- coding: utf-8 -*-
"""OBS 协同与虚拟摄像头输出。

由 server/routes/live.py 拆分而来。
本模块实现侧调用「缝」时一律写在函数体内，
以保住 server.routes.live 上的晚绑定语义（见设计文档 §5.2）。
"""
import asyncio
import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from server.adapters.media.media_router import global_media_router
from server.database.db import get_db
from server.database.models import AppSetting

router = APIRouter()
logger = logging.getLogger("LiveAgent.LiveController")

OBS_SETTING_HOST = "obs_websocket_host"
OBS_SETTING_PORT = "obs_websocket_port"
OBS_SETTING_PASSWORD = "obs_websocket_password"
OBS_SETTING_AUTO_CONNECT = "obs_auto_connect"


async def _save_obs_settings(
    db: AsyncSession,
    *,
    host: str,
    port: int,
    password: str,
    auto_connect: bool,
) -> None:
    """使用现有 AppSetting 表原子保存 OBS 配置；密码始终加密。"""
    from server.config import encrypt_secret

    values = {
        OBS_SETTING_HOST: host,
        OBS_SETTING_PORT: str(port),
        OBS_SETTING_PASSWORD: encrypt_secret(password) if password else "",
        OBS_SETTING_AUTO_CONNECT: "1" if auto_connect else "0",
    }
    result = await db.execute(select(AppSetting).where(AppSetting.key.in_(values)))
    existing: dict[str, AppSetting] = {str(row.key): row for row in result.scalars().all()}
    for key, value in values.items():
        row = existing.get(key)
        if row:
            setattr(row, "value", value)
        else:
            db.add(AppSetting(key=key, value=value))
    await db.commit()


async def _set_obs_auto_connect(db: AsyncSession, enabled: bool) -> None:
    """仅更新用户自动连接意图，不改写已保存的连接参数。"""
    result = await db.execute(select(AppSetting).where(AppSetting.key == OBS_SETTING_AUTO_CONNECT))
    row = result.scalar_one_or_none()
    value: str = "1" if enabled else "0"
    if row:
        setattr(row, "value", value)
    else:
        db.add(AppSetting(key=OBS_SETTING_AUTO_CONNECT, value=value))
    await db.commit()


async def restore_obs_connection(db: AsyncSession) -> bool:
    """数据库初始化后恢复 OBS 配置；仅持久化 intent=true 时尝试连接。"""
    from server.config import decrypt_secret

    # 测试缝（late-binding seam）：必须函数体内解析，
    # patch('server.routes.live.<name>') 才生效（见 test_live_seam_late_binding.py）。
    from server.routes.live import global_obs_client

    keys = (
        OBS_SETTING_HOST,
        OBS_SETTING_PORT,
        OBS_SETTING_PASSWORD,
        OBS_SETTING_AUTO_CONNECT,
    )
    result = await db.execute(select(AppSetting).where(AppSetting.key.in_(keys)))
    values: dict[str, str] = {str(row.key): str(row.value or "") for row in result.scalars().all()}
    if values.get(OBS_SETTING_AUTO_CONNECT) != "1":
        return False

    host = values.get(OBS_SETTING_HOST) or "127.0.0.1"
    try:
        port = int(values.get(OBS_SETTING_PORT) or 4455)
    except (TypeError, ValueError):
        port = 4455
    encrypted_password = values.get(OBS_SETTING_PASSWORD, "")
    password = decrypt_secret(encrypted_password) if encrypted_password else ""
    global_obs_client.host = host
    global_obs_client.port = port
    global_obs_client.password = password
    connected = await global_obs_client.connect()
    if not connected:
        logger.warning("启动时恢复 OBS 自动连接失败: %s:%s", host, port)
    return connected


def on_obs_external_state_change(active: bool, state: str, epoch: Optional[int] = None):
    """OBS 外部推流状态回调：连接中断时释放 Agent 的推流所有权。"""
    # 测试缝（late-binding seam）：必须函数体内解析，
    # patch('server.routes.live.<name>') 才生效（见 test_live_seam_late_binding.py）。
    from server.routes.live import global_live_controller, global_obs_client

    if not active:
        current_epoch = global_obs_client.connection_epoch
        owner_epoch = getattr(global_live_controller, "obs_owner_connection_epoch", None)
        # 仅当前连接代际可以释放同代际所有权；旧连接事件不能触碰新场次。
        if epoch is not None and epoch != current_epoch:
            logger.debug("忽略来自过时 OBS 连接的停流事件 (epoch=%d, current=%d)", epoch, current_epoch)
            return
        event_epoch = current_epoch if epoch is None else epoch
        if (
            getattr(global_live_controller, "obs_owner_session_id", None)
            and owner_epoch == current_epoch == event_epoch
        ):
            logger.info(
                "OBS 外部推流已停止或连接中断 (%s, epoch=%s)，释放 Agent 推流所有权 (session=%s)",
                state,
                epoch,
                global_live_controller.obs_owner_session_id,
            )
            global_live_controller.obs_owner_session_id = None
            global_live_controller.obs_owner_connection_epoch = None


# ------------------------------------------------------------------
# OBS Studio WebSocket v5 联动控制端点
# ------------------------------------------------------------------
@router.get("/obs/status")
async def get_obs_status():
    """获取当前 OBS-WebSocket 状态；刷新失败时保留最后可信状态并显式标 stale。"""

    # 测试缝（late-binding seam）：必须函数体内解析，
    # patch('server.routes.live.<name>') 才生效（见 test_live_seam_late_binding.py）。
    from server.routes.live import global_obs_client
    refresh_error = None
    if global_obs_client.is_connected:
        try:
            refreshed = await global_obs_client.refresh_stream_status()
            refresh_error = refreshed.get("error") if refreshed else "OBS 状态刷新返回为空"
        except Exception as exc:
            refresh_error = str(exc)
        global_obs_client.is_stale = bool(refresh_error)
        global_obs_client.last_error = refresh_error
    return {
        "code": 1 if refresh_error else 0,
        "message": f"刷新 OBS 推流状态失败: {refresh_error}" if refresh_error else None,
        "data": global_obs_client.get_summary(),
    }


class ObsConnectRequest(BaseModel):
    host: Optional[str] = "127.0.0.1"
    port: Optional[int] = 4455
    password: Optional[str] = ""


@router.post("/obs/connect")
async def connect_obs(req: ObsConnectRequest, db: AsyncSession = Depends(get_db)):
    """保存用户 OBS 自动连接意图后立即尝试连接；失败时后台继续退避恢复。"""

    # 测试缝（late-binding seam）：必须函数体内解析，
    # patch('server.routes.live.<name>') 才生效（见 test_live_seam_late_binding.py）。
    from server.routes.live import global_obs_client
    host = req.host or "127.0.0.1"
    port = int(req.port or 4455)
    password = req.password or ""
    await _save_obs_settings(
        db,
        host=host,
        port=port,
        password=password,
        auto_connect=True,
    )
    global_obs_client.host = host
    global_obs_client.port = port
    global_obs_client.password = password
    success = await global_obs_client.connect()
    return {
        "code": 0 if success else 1,
        "connected": success,
        "message": (
            "OBS 连接成功"
            if success
            else "当前连接 OBS 失败，配置与自动连接意图已保存，后台将继续退避重试"
        ),
        "data": global_obs_client.get_summary()
    }


@router.post("/obs/disconnect")
async def disconnect_obs(db: AsyncSession = Depends(get_db)):
    """先可靠持久化用户禁用意图，再改变当前进程 OBS 运行态。"""

    # 测试缝（late-binding seam）：必须函数体内解析，
    # patch('server.routes.live.<name>') 才生效（见 test_live_seam_late_binding.py）。
    from server.routes.live import global_obs_client
    await _set_obs_auto_connect(db, False)
    await global_obs_client.disconnect()
    return {"code": 0, "message": "OBS 连接已断开"}


@router.post("/obs/stream/start")
async def start_obs_stream():
    """控制 OBS 开始推流"""

    # 测试缝（late-binding seam）：必须函数体内解析，
    # patch('server.routes.live.<name>') 才生效（见 test_live_seam_late_binding.py）。
    from server.routes.live import global_obs_client
    if not global_obs_client.is_connected:
        raise HTTPException(status_code=400, detail="OBS 未连接，请先连接 OBS")
    res = await global_obs_client.start_stream()
    return {"code": 0 if res.get("result") else 1, "data": res}


@router.post("/obs/stream/stop")
async def stop_obs_stream():
    """控制 OBS 停止推流"""

    # 测试缝（late-binding seam）：必须函数体内解析，
    # patch('server.routes.live.<name>') 才生效（见 test_live_seam_late_binding.py）。
    from server.routes.live import global_obs_client
    if not global_obs_client.is_connected:
        raise HTTPException(status_code=400, detail="OBS 未连接，请先连接 OBS")
    res = await global_obs_client.stop_stream()
    return {"code": 0 if res.get("result") else 1, "data": res}


@router.get("/stream/preview")
async def live_stream_preview():
    """
    前端数字人实时视频流预览 (标准流式 MJPEG)
    浏览器原生支持使用 <img src="/api/v1/live/stream/preview"> 实时播放 25fps 数字人画面
    """
    async def frame_generator():
        while True:
            jpeg_bytes = global_media_router.get_latest_jpeg()
            if not jpeg_bytes:
                await asyncio.sleep(0.08)
                continue
            yield (
                b"--frame\r\n"
                b"Content-Type: image/jpeg\r\n\r\n" + jpeg_bytes + b"\r\n"
            )
            await asyncio.sleep(0.04)

    return StreamingResponse(
        frame_generator(),
        media_type="multipart/x-mixed-replace; boundary=frame"
    )


# ------------------------------------------------------------------
# OBS 虚拟摄像头 (Virtual Camera) 控制与状态端点
# ------------------------------------------------------------------
class VirtualCamStartRequest(BaseModel):
    width: Optional[int] = Field(1280, ge=320, le=3840)
    height: Optional[int] = Field(720, ge=240, le=3840)
    fps: Optional[int] = Field(25, ge=10, le=60)


@router.post("/virtual-cam/start", summary="启动 OBS 虚拟摄像头输出")
async def start_virtual_camera(req: Optional[VirtualCamStartRequest] = None):
    """启动本地虚拟摄像头，向 DirectShow / OBS Virtual Camera 输出实时画面"""
    from server.core.media.virtual_cam import global_virtual_cam
    if req:
        global_virtual_cam.width = req.width or 1280
        global_virtual_cam.height = req.height or 720
        global_virtual_cam.fps = req.fps or 25
    success = global_virtual_cam.start()
    status = global_virtual_cam.get_status()
    if not success and not status.get("is_active"):
        return {"code": 1, "message": f"虚拟摄像头启动受限: {status.get('last_error', '未知错误')}", "data": status}
    return {"code": 0, "message": "虚拟摄像头已成功启动", "data": status}


@router.post("/virtual-cam/stop", summary="停止 OBS 虚拟摄像头输出")
async def stop_virtual_camera():
    """安全关闭本地虚拟摄像头设备并释放句柄"""
    from server.core.media.virtual_cam import global_virtual_cam
    global_virtual_cam.stop()
    return {"code": 0, "message": "虚拟摄像头已停止", "data": global_virtual_cam.get_status()}


@router.get("/virtual-cam/status", summary="查询 OBS 虚拟摄像头状态与设备指标")
async def get_virtual_camera_status():
    """获取本地虚拟摄像头设备状态、输出帧率与写入统计"""
    from server.core.media.virtual_cam import global_virtual_cam
    return {"code": 0, "message": "success", "data": global_virtual_cam.get_status()}


# ============================================================================
# 推流生命周期联动 (由 live_lifecycle.py 迁入：这两个子过程只做 OBS/RTMP 协同)
# ============================================================================

async def link_obs_push_stream(req, session_id: str) -> tuple[bool, Optional[str]]:
    """按需联动 OBS 自动推流，返回 (是否联动成功, 降级原因)。

    推流所有权在 ``session_id`` 归属本 Agent 且此前非推流中时才登记，
    避免停播时误切用户手工开的流。
    """
    from server.routes.live import global_live_controller, global_obs_client

    if not getattr(req, "obs_auto_link", False):
        return False, None
    if not global_obs_client.is_connected:
        logger.warning("OBS 未连接，无法自动联动推流")
        return False, "OBS 未连接，无法自动联动推流"
    try:
        res = await global_obs_client.start_stream()
        if res.get("result"):
            if not res.get("already_streaming"):
                global_live_controller.obs_owner_session_id = session_id
                global_live_controller.obs_owner_connection_epoch = global_obs_client.connection_epoch
            else:
                logger.info("OBS 此前已在推流中，保持现有推流所有权不变，停播时不主动切断")
            return True, None
        msg = res.get("comment") or res.get("error") or "OBS 拒绝推流"
        logger.warning("联动 OBS 开始推流失败: %s", msg)
        return False, msg
    except Exception as e:
        logger.warning("联动 OBS 开始推流异常: %s", e)
        return False, str(e)


async def stop_obs_push_and_rtmp(
    *, session_id: Optional[str], should_stop_obs: bool,
    owner_session_id: Optional[str], owner_epoch: Optional[int],
) -> list[str]:
    """按所有权 token 停止 OBS 推流并联动停止内置 RTMP 直推，随后清空所有权。

    返回本次清理产生的错误列表（供停播流程汇总为 stopped_with_errors）。
    ``session_id`` 由停播方在会话销毁前冻结传入 —— 不能在 stop() 之后再读
    ``global_live_controller.session_id``，否则所有权复核会拿到已清空的值。
    """
    from server.routes.live import global_live_controller, global_obs_client

    cleanup_errors: list[str] = []
    try:
        stop_epoch = getattr(global_obs_client, "connection_epoch", None)
        if not isinstance(stop_epoch, int):
            stop_epoch = owner_epoch
        owner_still_current = bool(
            should_stop_obs
            and owner_session_id == session_id
            and owner_epoch == stop_epoch
            and global_obs_client.is_connected
            and global_obs_client.is_streaming
        )
        if owner_still_current:
            try:
                res = await global_obs_client.stop_stream()
                if not res.get("result") and not res.get("already_stopped"):
                    err_msg = res.get("error") or res.get("comment") or "OBS 拒绝停止推流"
                    logger.warning("联动 OBS 停止推流失败: %s", err_msg)
                    cleanup_errors.append(f"obs_stop_rejected: {err_msg}")
            except Exception as e:
                logger.warning("联动 OBS 停止推流异常: %s", e)
                cleanup_errors.append(f"obs_stop_failed: {e}")
    finally:
        # 场次结束无条件清空完整所有权 token，防止跨场残留。
        global_live_controller.obs_stream_started_by_agent = False
        global_live_controller.obs_owner_session_id = None
        global_live_controller.obs_owner_connection_epoch = None

        try:
            from server.core.media.rtmp_streamer import global_rtmp_streamer
            if global_rtmp_streamer.is_streaming:
                global_rtmp_streamer.stop()
        except Exception as e:
            logger.warning("联动停止 RTMP 直推异常: %s", e)
    return cleanup_errors
