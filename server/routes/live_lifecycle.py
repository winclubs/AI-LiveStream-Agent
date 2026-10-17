# -*- coding: utf-8 -*-
"""开播 / 停播生命周期：场次创建、生产门禁、OBS 联动、人工干预。

由 server/routes/live.py 拆分而来。**缝约定**：本模块引用的一切「缝」一律在
**函数体内**从 ``server.routes.live`` 解析 —— 顶层 import 会把缝变成早绑定，使
``patch("server.routes.live.X")`` 静默失效（测试绿而线上行为已变）。
守卫见 server/tests/test_live_seam_late_binding.py。
"""
import asyncio
import json
import logging
import os
import uuid
from pathlib import Path
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from server.database.db import get_db
from server.database.models import Product

logger = logging.getLogger("LiveAgent.LiveController")

#: 子路由用相对路径（无 prefix）；``/live`` 前缀由 façade 的 router 提供。
router = APIRouter()


# ---- 请求模型 ----

class LiveStartRequest(BaseModel):
    room_url: Optional[str] = ""
    room_id: Optional[str] = None
    role_id: Optional[str] = None
    anchor_id: Optional[str] = None
    voice_id: Optional[str] = None
    platform: Optional[str] = "bilibili"
    demo_mode: Optional[bool] = False  # 明确的演示模式 (允许 mock 弹幕)
    obs_auto_link: Optional[bool] = False  # 联动 OBS 自动开启推流
    execution_mode: Optional[str] = None  # DEMO/PREVIEW/PILOT/PRODUCTION, 缺省读 LIVE_AGENT_MODE


class LiveInterruptRequest(BaseModel):
    text: str


class LiveTakeoverRequest(BaseModel):
    action: str = "takeover"  # takeover / release
    operator: Optional[str] = "admin"
    reason: Optional[str] = "人工控制台接管"


# ---- 状态查询 ----

def _build_external_publish_status() -> dict:
    """统一构建外部推流状态对象：本地源 / OBS / 平台公开开播状态。

    platform_live 在未接入平台 API 前诚实保持 null，绝不过度宣称。
    """
    from server.routes.live import global_obs_client

    obs_summary = global_obs_client.get_summary()
    is_connected = obs_summary.get("is_connected", False)
    is_streaming = obs_summary.get("is_streaming", False)
    return {
        "mode": "obs_websocket",
        "status": "streaming" if is_streaming else ("obs_ready" if is_connected else "not_managed"),
        "transport_status": "active" if is_streaming else ("connected" if is_connected else "disconnected"),
        "platform_live": None,
        "validation": "obs_output_only" if is_streaming else ("obs_standby" if is_connected else "pending_external_acceptance"),
        "obs": obs_summary,
    }


@router.get("/status")
async def get_live_status():
    """获取本地直播源与外部推流状态"""
    from server.routes.live import _build_external_publish_status as _build_status
    from server.routes.live import global_live_controller

    running = global_live_controller.is_live
    return {
        "code": 0,
        "is_live": running,
        "session_id": global_live_controller.session_id,
        "platform": getattr(global_live_controller, "platform", "bilibili"),
        "local_source": {
            "status": "running" if running else "stopped",
            "session_id": global_live_controller.session_id,
        },
        "execution_mode": getattr(global_live_controller, "execution_mode", "PREVIEW"),
        "state_detail": getattr(global_live_controller, "state_detail", "STOPPED" if not running else "LOCAL_READY"),
        "platform_confirmed": getattr(global_live_controller, "platform_confirmed", False),
        "external_publish": _build_status(),
    }


# ---- 开播：身份解析 / 驱动挂接 / 预检（自 _start_live_unlocked 抽出的子过程） ----

async def _resolve_live_identity(req: LiveStartRequest, db: AsyncSession):
    """解析并激活主播角色，校验主播与音色存在性。返回 (role, anchor, voice_id)。"""
    from server.database.models import Anchor, AnchorRole, VoiceProfile
    from server.routes.live import global_role_manager

    if req.role_id:
        role_row = await db.get(AnchorRole, req.role_id)
        if not role_row:
            raise HTTPException(status_code=400, detail=f"指定的主播角色不存在: {req.role_id}")
        await db.execute(update(AnchorRole).values(is_active=0))
        setattr(role_row, "is_active", 1)
        global_role_manager.register_or_update_from_db(role_row, activate=True)
    active_role = global_role_manager.get_active_role()

    anchor_row = None
    if req.anchor_id:
        anchor_row = await db.get(Anchor, req.anchor_id)
        if not anchor_row:
            raise HTTPException(status_code=400, detail=f"指定的主播不存在: {req.anchor_id}")

    target_voice_id: Optional[str] = req.voice_id or (
        str(anchor_row.voice_id) if anchor_row and anchor_row.voice_id else None
    )
    if target_voice_id and not await db.get(VoiceProfile, target_voice_id):
        raise HTTPException(status_code=400, detail=f"指定的音色不存在: {target_voice_id}")

    return active_role, anchor_row, target_voice_id


def _resolve_execution_mode(req: LiveStartRequest, platform: str, target_room: str) -> tuple[str, bool]:
    """解析运行档位与演示标记；正式平台缺房间号时直接拦截。"""
    platform_lower = platform.lower()
    is_demo = bool(
        getattr(req, "demo_mode", False)
        or platform_lower in ("mock", "demo")
        or target_room.lower() in ("room_demo", "mock")
    )
    raw_mode = (getattr(req, "execution_mode", None) or os.getenv("LIVE_AGENT_MODE") or "").strip()
    if not raw_mode:
        raw_mode = "DEMO" if is_demo else "PREVIEW"

    # 正式直播平台严禁空房间静默进入仿真模式，必须拦截并校验
    if platform_lower in ("bilibili", "douyin", "kuaishou", "wechat") and not is_demo:
        if not target_room:
            raise HTTPException(
                status_code=400,
                detail=f"【{platform}】正式直播必须提供有效的房间号或直播链接；如需测试仿真互动，请选择 mock 模式或开启 demo_mode"
            )
    return raw_mode.upper(), is_demo


async def _attach_avatar_runtime(avatar_asset_dir: str, req: LiveStartRequest):
    """挂接数字人资产与双分支算力驱动（本地显卡 vs 远端租赁GPU）。

    返回 compute_plan，不可用时为 None（供唇形预检降级）。
    """
    if not avatar_asset_dir or not Path(avatar_asset_dir).exists():
        return None
    try:
        from server.core.avatar import (
            CloudSidecarDriver,
            LocalLiveTalkingDriver,
            set_active_avatar_driver,
        )
        from server.core.hardware.gpu_capability import evaluate_compute

        compute_plan = await evaluate_compute(
            feature_name="开播数字人实时驱动", required_vram_gb=2.0
        )
        if compute_plan.use_cloud and compute_plan.can_execute and compute_plan.cloud_gpu:
            cloud_cfg = compute_plan.cloud_gpu
            logger.info(
                "开播数字人启用【分支 2: 远端租赁GPU】，Sidecar: %s",
                cloud_cfg.get("provider_name", "云节点"),
            )
            sidecar_driver = CloudSidecarDriver(config={
                "sidecar_url": cloud_cfg.get("base_url", ""),
                "api_key": cloud_cfg.get("api_key", ""),
                "avatar_asset_dir": avatar_asset_dir,
                "anchor_id": req.anchor_id,
            })
            await sidecar_driver.start()
            set_active_avatar_driver(sidecar_driver)
        elif not compute_plan.use_cloud and compute_plan.can_execute:
            logger.info("开播数字人启用【分支 1: 本地高性能显卡】，资产: %s", avatar_asset_dir)
            local_driver = LocalLiveTalkingDriver(config={
                "avatar_asset_dir": avatar_asset_dir,
                "anchor_id": req.anchor_id,
            })
            await local_driver.start()
            set_active_avatar_driver(local_driver)
        return compute_plan
    except Exception as e:
        logger.warning("挂载切片数字人驱动异常，保持默认驱动: %s", e)
        return None


async def _run_avatar_preflights(
    avatar_asset_dir: str, compute_plan: Any
) -> tuple[Dict[str, Any], Dict[str, Any]]:
    """资产健康 + 神经唇形算力预检，返回 (lipsync_preflight, asset_health)。

    资产严重损坏（存在无人脸帧）**阻断开播**；算力不达标只告知不阻断
    —— 是否开播是产品决策，算力模块不越权。
    """
    lipsync_preflight: Dict[str, Any] = {}
    asset_health: Dict[str, Any] = {}
    if not avatar_asset_dir:
        return lipsync_preflight, asset_health

    # 先查资产健康：人脸切片里存在无人脸帧时，唇形必然是噪声，此时再谈算力没有意义。
    try:
        from server.core.avatar.asset_health import AssetUnusableError, check_and_enforce

        asset_health_obj = check_and_enforce(avatar_asset_dir)
        asset_health = asset_health_obj.to_dict()
        logger.info(
            "开播资产健康: %s (判定来源: %s)",
            asset_health.get("status"), asset_health.get("source"),
        )
    except AssetUnusableError as e:
        logger.error("数字人资产严重损坏，阻断开播: %s", e)
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.warning("资产健康检查异常: %s", e)
        asset_health = {"status": "unknown", "reason": str(e)}

    try:
        from server.core.avatar.lipsync_preflight import probe_async

        _probe_renderer = None
        if compute_plan is not None and not compute_plan.use_cloud:
            try:
                from server.core.avatar import get_active_avatar_driver

                _drv = get_active_avatar_driver()
                _probe_renderer = getattr(_drv, "lip_renderer", None)
            except Exception:
                _probe_renderer = None

        _pf = await probe_async(renderer=_probe_renderer)
        lipsync_preflight = _pf.to_dict()
        if not _pf.ok:
            logger.warning("神经唇形算力预检未达标: %s", _pf.summary())
            for _h in _pf.hints:
                logger.warning("  · %s", _h)
        else:
            logger.info("神经唇形算力预检: %s", _pf.summary())
    except Exception as e:
        logger.warning("神经唇形算力预检异常（不阻断开播）: %s", e)
        lipsync_preflight = {"level": "unavailable", "ok": False, "reason": str(e)}

    return lipsync_preflight, asset_health


async def _start_live_unlocked(req: LiveStartRequest, db: AsyncSession):
    """在生命周期锁内创建持久化场次并启动本地直播源。"""
    from server.database.models import LiveSessionRecord, utc_now
    from server.routes.live import (
        _build_external_publish_status as _build_status,
        _resolve_landmarks_for_avatar,
        global_live_controller,
    )
    from server.routes.settings import SETTING_KEY_LIVE_MODE, _get_setting

    if global_live_controller.is_live:
        raise HTTPException(status_code=409, detail="直播已在进行中，请勿重复开播；如需重开请先停止当前直播")

    active_role, anchor_row, target_voice_id = await _resolve_live_identity(req, db)

    rows = (await db.execute(select(Product).where(Product.is_active == 1))).scalars().all()
    from server.routes.products import product_to_context
    products = [product_to_context(product) for product in rows]

    target_room = (req.room_id or req.room_url or "").strip()
    session_id = f"sess_{uuid.uuid4().hex[:12]}"
    platform = (req.platform or "bilibili").strip()
    execution_mode, is_demo = _resolve_execution_mode(req, platform, target_room)

    live_mode = await _get_setting(db, SETTING_KEY_LIVE_MODE) or "B"
    live_theme = await _get_setting(db, "live_theme") or ""
    avatar_path: str = str(anchor_row.photo_portrait) if anchor_row and anchor_row.photo_portrait else ""
    landmarks_path = await asyncio.to_thread(_resolve_landmarks_for_avatar, avatar_path) if avatar_path else ""

    avatar_asset_dir = str(getattr(anchor_row, "avatar_asset_dir", "") or "") if anchor_row else ""
    has_video_avatar = bool(avatar_asset_dir and Path(avatar_asset_dir).exists())

    compute_plan = await _attach_avatar_runtime(avatar_asset_dir, req) if has_video_avatar else None
    lipsync_preflight, asset_health = await _run_avatar_preflights(
        avatar_asset_dir if has_video_avatar else "", compute_plan
    )
    from server.routes.live_preflight import assert_production_gates

    preflight_report = await assert_production_gates(
        execution_mode=execution_mode,
        avatar_asset_dir=avatar_asset_dir if has_video_avatar else "",
        voice_id=target_voice_id,
        platform=platform,
        anchor_id=req.anchor_id,
    )

    session_record = LiveSessionRecord(
        session_id=session_id,
        platform=platform,
        theme=live_theme,
        mode=live_mode,
        role_id=active_role.role_id,
        voice_id=target_voice_id,
        anchor_id=req.anchor_id,
        product_snapshot_json=json.dumps(products, ensure_ascii=False),
        status="starting",
        execution_mode=execution_mode,
        state_detail="LOCAL_READY",
        platform_confirmed=False,
    )
    db.add(session_record)
    await db.commit()

    obs_linked = False
    obs_error_msg = None
    try:
        is_demo = bool(getattr(req, "demo_mode", False))
        global_live_controller.anchor_id = req.anchor_id
        await global_live_controller.start(
            session_id, products, target_room, platform=platform, theme=live_theme,
            voice_id=target_voice_id, avatar_path=avatar_path, mode=live_mode,
            landmarks_path=landmarks_path, demo_mode=is_demo,
            avatar_asset_dir=avatar_asset_dir, execution_mode=execution_mode,
        )
        setattr(session_record, "status", "live")
        await db.commit()

        from server.routes.live_obs import link_obs_push_stream

        obs_linked, obs_error_msg = await link_obs_push_stream(req, session_id)
        current_detail = "OBS_STREAMING" if obs_linked else "LOCAL_READY"
        setattr(session_record, "state_detail", current_detail)
        global_live_controller.set_state_detail(current_detail)
        await db.commit()
    except Exception as exc:
        await db.rollback()
        record = await db.get(LiveSessionRecord, session_id)
        if record:
            setattr(record, "status", "failed")
            setattr(record, "state_detail", "FAILED")
            setattr(record, "end_time", utc_now())
            await db.commit()
        raise HTTPException(status_code=500, detail=f"直播资源启动失败: {exc}") from exc

    # 启动直播自动录制以留存全场视频便于复盘与 OSS 归档
    try:
        from server.core.media.recorder import get_record_manager
        rec_mgr = get_record_manager()
        if not rec_mgr.is_recording():
            rec_mgr.start_recording(
                title=live_theme or "直播实况录像",
                record_id=session_id,
                width=1280,
                height=720,
            )
    except Exception as rec_err:
        logger.warning("启动自动场次录像异常: %s", rec_err)

    return {
        "code": 0,
        "session_id": session_id,
        "is_live": True,
        "platform": platform,
        "voice_id": target_voice_id,
        "role_id": active_role.role_id,
        "mode": live_mode,
        "execution_mode": execution_mode,
        "state_detail": global_live_controller.state_detail,
        "platform_confirmed": global_live_controller.platform_confirmed,
        "obs_linked": obs_linked,
        "degraded": obs_error_msg is not None,
        "obs_error": obs_error_msg,
        "local_source": {"status": "running", "preview": "/api/v1/live/stream/preview"},
        "external_publish": _build_status(),
        "preflight_report": preflight_report.to_dict(),
        "lipsync_preflight": lipsync_preflight,
        "avatar_asset_health": asset_health,
        "message": f"直播源已成功启动 ({platform}, 模式 {live_mode}, 档位 {execution_mode}, 状态 {global_live_controller.state_detail})"
    }


@router.post("/start")
async def start_live(req: LiveStartRequest, db: AsyncSession = Depends(get_db)):
    """用同一把生命周期锁包裹完整开播状态转换。"""
    from server.routes.live import _start_live_unlocked, global_live_controller

    async with global_live_controller.lifecycle_lock:
        return await _start_live_unlocked(req, db)


# ---- 停播 ----

async def _finalize_session_record(
    db: AsyncSession, sid: str, stats: dict, cleanup_errors: list[str]
) -> None:
    """把最终场次指标落库，并联动停止录制与 OSS 异步归档。"""
    from server.database.models import LiveSessionRecord, Order, utc_now

    record = await db.get(LiveSessionRecord, sid)
    if not record:
        return
    setattr(record, "end_time", utc_now())
    setattr(record, "status", "stopped_with_errors" if cleanup_errors else "stopped")
    setattr(record, "danmaku_count", stats.get("danmaku_count", 0))
    setattr(record, "peak_viewers", stats.get("peak_viewer_count", 0))
    setattr(record, "gift_income", stats.get("gift_income_yuan", 0.0))

    # 订单汇总以数据库为权威源，取消/退款订单不会计入成交。
    aggregate = await db.execute(
        select(func.coalesce(func.sum(Order.amount), 0.0), func.count(Order.id))
        .where(Order.session_id == sid, Order.status == "completed")
    )
    total_gmv, orders_count = aggregate.one()
    setattr(record, "total_gmv", float(total_gmv or 0.0))
    setattr(record, "orders_count", int(orders_count or 0))

    try:
        from server.core.media.recorder import get_record_manager
        rec_mgr = get_record_manager()
        if rec_mgr.is_recording():
            rec_info = rec_mgr.stop_recording()
            if rec_info.get("code") == 0 and rec_info.get("data"):
                data = rec_info["data"]
                vid_p = data.get("video_path", "")
                setattr(record, "video_path", vid_p)
                setattr(record, "preview_image_url", data.get("preview_path", ""))
                setattr(record, "video_duration_sec", data.get("duration_sec", 0.0))
                setattr(record, "video_size_bytes", data.get("file_size_bytes", 0))
                from server.core.media.oss_uploader import global_oss_uploader
                asyncio.create_task(
                    global_oss_uploader.upload_session_recording_async(
                        session_id=sid,
                        video_path=vid_p,
                        preview_path=data.get("preview_path", ""),
                    )
                )
    except Exception as rec_err:
        logger.warning("结束场次录制与 OSS 归档处理异常: %s", rec_err)

    await db.commit()


async def _stop_live_unlocked(db: AsyncSession):
    """在生命周期锁内停止直播源并持久化最终场次指标。"""
    from server.routes.live import global_live_controller, global_obs_client

    sid = global_live_controller.session_id
    if not sid and not global_live_controller.is_live:
        return {"code": 0, "is_live": False, "message": "直播已停止"}
    stats = global_live_controller.stats.copy()

    # 在会话销毁前冻结所有权 token；停 OBS 前还会复核连接 epoch。
    owner_session_id = global_live_controller.obs_owner_session_id
    owner_epoch = global_live_controller.obs_owner_connection_epoch
    current_epoch = getattr(global_obs_client, "connection_epoch", None)
    if not isinstance(current_epoch, int):
        current_epoch = owner_epoch
    should_stop_obs = bool(
        owner_session_id
        and owner_session_id == sid
        and owner_epoch is not None
        and owner_epoch == current_epoch
        and global_obs_client.is_connected
        and global_obs_client.is_streaming
    )

    cleanup_errors = []
    try:
        cleanup_errors = await global_live_controller.stop()
    except Exception as exc:
        logger.error("控制器 stop() 抛出未捕获异常: %s", exc, exc_info=True)
        cleanup_errors.append(str(exc))
    finally:
        from server.routes.live_obs import stop_obs_push_and_rtmp

        cleanup_errors.extend(
            await stop_obs_push_and_rtmp(
                session_id=sid,
                should_stop_obs=should_stop_obs,
                owner_session_id=owner_session_id,
                owner_epoch=owner_epoch,
            )
        )
        if sid:
            try:
                await _finalize_session_record(db, sid, stats, cleanup_errors)
            except Exception as db_err:
                logger.error("停播持久化场次指标失败: %s", db_err, exc_info=True)
                await db.rollback()

    status_str = "stopped" if not cleanup_errors else "stopped_with_errors"
    return {
        "code": 0,
        "is_live": False,
        "status": status_str,
        "cleanup_errors": cleanup_errors,
        "message": "直播已停止" if not cleanup_errors else f"直播已停止，存在资源清理警告: {', '.join(cleanup_errors)}"
    }


@router.post("/stop")
async def stop_live(db: AsyncSession = Depends(get_db)):
    """用同一把生命周期锁包裹完整停播状态转换。"""
    from server.routes.live import _stop_live_unlocked, global_live_controller

    async with global_live_controller.lifecycle_lock:
        return await _stop_live_unlocked(db)


# ---- 人工干预：插话 / 接管 / 暂停恢复 ----

@router.post("/interrupt")
@router.post("/manual-speech")
async def interrupt_live(req: LiveInterruptRequest):
    """运营人员紧急人工插话（双路由对齐，触发毫秒级打断并播报新内容）"""
    from server.routes.live import global_live_controller

    if not global_live_controller.is_live:
        raise HTTPException(status_code=400, detail="直播尚未开播，禁止插播")
    res = await global_live_controller.ingest_event(
        event_type="chat",
        user_name="运营管理员",
        payload={"text": req.text},
        priority=0,  # P0 触发抢占打断
        source="manual_interrupt",
        is_mock=False,
    )
    return {"code": 0 if res.get("accepted") else 1, "message": "人工插话已成功抢占插播", "data": res}


@router.post("/takeover")
async def takeover_live(req: LiveTakeoverRequest):
    """人工接管直播间 (《商业化运营改造方案》§6.3)。

    takeover: 原子打断 AI/TTS 并切 HUMAN_TAKEOVER；release: 释放接管。
    """
    from server.routes.live import global_live_controller

    if not global_live_controller.is_live:
        raise HTTPException(status_code=400, detail="直播尚未开播，无法接管")
    if req.action == "takeover":
        res = await global_live_controller.takeover(
            operator=req.operator or "admin", reason=req.reason or ""
        )
        return {"code": 0, "message": "人工接管已生效，AI 与语音播报已原子抢占", "data": res}
    if req.action == "release":
        res = await global_live_controller.release_takeover(operator=req.operator or "admin")
        return {"code": 0, "message": "人工接管已释放，恢复正常直播状态", "data": res}
    raise HTTPException(status_code=400, detail=f"不支持的接管动作: {req.action}")


@router.post("/pause-ai")
async def pause_ai_live(reason: str = "运营手动暂停"):
    """手动暂停 AI 生成进入 AI_PAUSED 安全态"""
    from server.routes.live import global_live_controller

    if not global_live_controller.is_live:
        raise HTTPException(status_code=400, detail="直播未开播")
    await global_live_controller.transition_to_ai_paused(reason)
    return {"code": 0, "message": "AI 已暂停", "state_detail": global_live_controller.state_detail}


@router.post("/resume-ai")
async def resume_ai_live():
    """从暂停或降级态恢复 AI 生成"""
    from server.routes.live import global_live_controller

    if not global_live_controller.is_live:
        raise HTTPException(status_code=400, detail="直播未开播")
    result = await global_live_controller.resume_ai()
    if not result.get("ok"):
        return {"code": 409, "message": "AI 恢复被重新预检阻断", "data": result}
    return {
        "code": 0, "message": "AI 已恢复", "data": result,
        "state_detail": global_live_controller.state_detail,
    }
