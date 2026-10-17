import asyncio
import uuid
import base64
import io
import json
import os
import re
import time
import logging
import inspect
import wave
import numpy as np
from collections import deque
from pathlib import Path
from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Form, WebSocket, WebSocketDisconnect, Response, Request, Body
from pydantic import BaseModel, Field
from typing import Optional, Dict, Any
from server.core.audio.full_duplex_asr import get_full_duplex_asr_manager
from server.core.media.webrtc_streamer import get_webrtc_stream_manager
from server.core.media.recorder import get_record_manager
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, update, func
from server.database.db import get_db
from server.database.models import Product, ProhibitedWordLog, BarrageLog, AppSetting
from server.core.roles.role_manager import global_role_manager
from server.core.guardrails.aho_corasick import global_guardrail
from server.core.guardrails.humanizer import humanize_text, jitter_speed
from server.core.queue.priority_queue import PriorityBargeInQueue, BarrageAggregator
from server.core.monitoring.vram_watchdog import VRAMWatchdog
from server.adapters.danmaku.mock_fetcher import MockDanmakuFetcher
from server.adapters.danmaku.circuit_breaker import CircuitBreakerDanmakuFetcher
from server.adapters.media.media_router import global_media_router
global_media_driver = global_media_router  # 兼容现有单例引用
from server.adapters.media.edgetts_driver import EdgeTTSMediaDriver
from server.core.vision.capture import global_vision
from server.core.media.av_sync import global_av_sync
from server.core.media.virtual_audio import global_virtual_audio
from server.adapters.obs.obs_client import global_obs_client
from server.core.media.hybrid_stitcher import (
    HybridStitcher,
    HybridSliceHandle,
    apply_audio_crossfade,
    CROSSFADE_SAMPLES_16K,
)
from server.core.media.stitcher_protocol import StitcherState, ResumePolicy
from server.core.media.shared_playback_clock import global_shared_playback_clock
from server.core.media.frame_bus import global_frame_bus

logger = logging.getLogger("LiveAgent.LiveController")
router = APIRouter(prefix="/live", tags=["直播控场与调度"])

# 句级切分标点 (前瞻播报与串行播报共用同一口径)
_SENTENCE_DELIMITERS = re.compile(r'([，。！？；\n]+)')

OBS_SETTING_HOST = "obs_websocket_host"
OBS_SETTING_PORT = "obs_websocket_port"
OBS_SETTING_PASSWORD = "obs_websocket_password"
OBS_SETTING_AUTO_CONNECT = "obs_auto_connect"


def _stream_output_ready_probe() -> bool:
    """推流输出就绪探针：任一真实输出通道在工作才允许帧停滞判定。

    RTMP 直推 / 虚拟摄像头(OBS) / WebRTC 观看端 / 切片录制任一活动即视为就绪。
    全部未就绪时没有外部观众消费画面，判定 MEDIA_DEGRADED 只会制造误降级。
    """
    try:
        from server.core.media.rtmp_streamer import global_rtmp_streamer
        if bool(getattr(global_rtmp_streamer, "is_streaming", False)):
            return True
    except Exception:
        pass
    try:
        from server.core.media.virtual_cam import global_virtual_cam
        if getattr(global_virtual_cam, "is_active", False):
            return True
    except Exception:
        pass
    try:
        from server.core.media.webrtc_streamer import get_webrtc_stream_manager
        manager = get_webrtc_stream_manager()
        if manager is not None and int(manager.get_status().get("active_connections", 0) or 0) > 0:
            return True
    except Exception:
        pass
    try:
        from server.core.media.recorder import get_record_manager
        recorder = get_record_manager()
        if recorder is not None and bool(getattr(recorder, "is_recording", lambda: False)()):
            return True
    except Exception:
        pass
    return False


def _configure_media_watchdog(execution_mode: str) -> None:
    """按运行档位配置媒体质量看门狗：停滞门限策略 + 推流输出就绪探针。"""
    from server.core.monitoring.media_quality_watchdog import global_media_watchdog
    global_media_watchdog.set_execution_mode(execution_mode)
    global_media_watchdog.set_output_readiness_probe(_stream_output_ready_probe)


def _pcm_s16le_to_wav(pcm: bytes, sample_rate: int, channels: int) -> bytes:
    """将裸 PCM16 封装为浏览器可独立播放的完整 WAV 容器。"""
    output = io.BytesIO()
    with wave.open(output, "wb") as wav_file:
        wav_file.setnchannels(int(channels))
        wav_file.setsampwidth(2)
        wav_file.setframerate(int(sample_rate))
        wav_file.writeframes(pcm)
    return output.getvalue()


# ---------------------------------------------------------------------------
# 兼容再导出：OBS 协同与虚拟摄像头已迁至 server/routes/live_obs.py。
# 端点经子路由挂载；`restore_obs_connection` 被 server/app.py 直接导入，必须保留同名缝。
# OBS_SETTING_* 四个常量仍留在本模块（_start_live_unlocked 依赖它们）。
# ---------------------------------------------------------------------------
from server.routes.live_obs import router as _obs_router  # noqa: E402


def restore_obs_connection(db: AsyncSession) -> bool:
    """缝：OBS 自动连接恢复（实现见 server/routes/live_obs.py）。"""
    from server.routes.live_obs import restore_obs_connection as _impl

    return _impl(db)


router.include_router(_obs_router)


def _supported_metadata(callable_obj, metadata: dict) -> dict:
    """仅向声明支持的 callable 传元数据，兼容旧 driver 与测试替身。"""
    try:
        parameters = inspect.signature(callable_obj).parameters
    except (TypeError, ValueError):
        return {}
    if any(param.kind == inspect.Parameter.VAR_KEYWORD for param in parameters.values()):
        return metadata
    return {key: value for key, value in metadata.items() if key in parameters}


async def _call_async_compat(callable_obj, *args, **metadata):
    """兼容异步驱动与同步测试替身，统一收敛可等待结果。"""
    result = callable_obj(*args, **_supported_metadata(callable_obj, metadata))
    if inspect.isawaitable(result):
        return await result
    return result


def _call_sync_compat(callable_obj, *args, **metadata):
    return callable_obj(*args, **_supported_metadata(callable_obj, metadata))


def _safe_media_status(component: str, getter, default: Optional[dict] = None) -> dict:
    """单个媒体组件故障不能让诊断端点整体不可用。"""
    try:
        value = getter()
        if not isinstance(value, dict):
            raise TypeError("status getter 未返回 dict")
        return value
    except Exception as exc:
        logger.warning("读取 %s 状态失败: %s", component, exc)
        return {
            **(default or {}),
            "available": False,
            "component_up": False,
            "stale": True,
            "error": str(exc),
        }

# 全局直播运行控制器
class LiveSessionController:
    MAX_TTS_SENTENCE_BYTES = 32 * 1024 * 1024

    def __init__(self):
        self.is_live = False
        self.session_id: Optional[str] = None
        self.fetcher = None
        self.platform = "bilibili"
        self.event_queue = PriorityBargeInQueue(on_interrupt_callback=self._on_barge_in)
        self.aggregator = BarrageAggregator(window_seconds=3.0)
        self.history = deque(maxlen=8)  # 多轮对话历史 (user/assistant 交替)
        self.worker_task: Optional[asyncio.Task] = None
        self.viewer_task: Optional[asyncio.Task] = None
        self.vision_task: Optional[asyncio.Task] = None
        self.live_context: dict[str, Any] = {"products": []}
        self._background_tasks = set()
        self._session_generation = 0
        self._audio_generation = 0
        self._lifecycle_lock: Optional[asyncio.Lock] = None
        self.tts_driver = EdgeTTSMediaDriver()
        self.vram_watchdog = VRAMWatchdog(on_alert=self._on_vram_alert)
        self._obs_owner_session_id: Optional[str] = None
        self.obs_owner_connection_epoch: Optional[int] = None
        self.demo_mode: bool = False
        self.execution_mode: str = "PREVIEW"
        self.state_detail: str = "STOPPED"
        self.platform_confirmed: bool = False
        self.anchor_id: Optional[str] = None
        self.voice_id: Optional[str] = None
        self.avatar_asset_dir: str = ""
        self.current_tts_task: Optional[asyncio.Task] = None
        self.cleanup_errors: list = []
        # 动静分离/批播结合：无缝流缝合调度器 (持有共享时钟与帧发布总线)
        self.stitcher = HybridStitcher(
            clock=global_shared_playback_clock,
            frame_bus=global_frame_bus,
        )
        self.stitcher.on_transition_hook = self._on_slice_transition_filler
        self.stitcher.on_slice_audio_playback = self._on_slice_audio_playback
        self._last_slice_audio_tail: Optional[np.ndarray] = None
        self.stitcher_task: Optional[asyncio.Task] = None
        # 直播大屏运营统计 (需求 8)
        self.stats: dict[str, Any] = {
            "start_ts": None,
            "viewer_count": 0,
            "peak_viewer_count": 0,
            "gift_income_yuan": 0.0,
            "gmv_yuan": 0.0,
            "orders_count": 0,
            # 兼容字段：仅统计成功进入调度队列的真实业务事件。
            "danmaku_count": 0,
            "gift_count": 0,
            "events_received_total": 0,
            "events_accepted_total": 0,
            "events_dropped_total": 0,
            "danmaku_received_total": 0,
            "danmaku_accepted_total": 0,
            "danmaku_dropped_total": 0,
            "gift_received_total": 0,
            "gift_accepted_total": 0,
            "gift_dropped_total": 0,
            "guardrail_hits": 0,
            "flash_sales": 0
        }
        # 联动 P1 媒体质量看门狗与预算超限熔断中枢
        from server.core.monitoring.media_quality_watchdog import global_media_watchdog
        from server.core.compliance.cost_privacy_manager import global_cost_privacy_manager
        global_media_watchdog._on_degraded_callback = lambda reason: asyncio.create_task(self._on_media_degraded(reason))
        global_cost_privacy_manager._on_budget_exceeded = lambda sess, cost, lim: asyncio.create_task(self.transition_to_ai_paused("BUDGET_CAP_EXCEEDED"))

    async def _on_media_degraded(self, reason: str):
        """当底层神经媒体链路失败且处于生产/试点档位时触发硬阻断安全态"""
        if self.execution_mode in ("PRODUCTION", "PILOT"):
            self.state_detail = "MEDIA_DEGRADED"
            logger.critical(
                "🚨【生产安全硬阻断】数字人媒体渲染链路发生降级: %s (档位=%s)！状态已流转至 MEDIA_DEGRADED",
                reason,
                self.execution_mode,
            )
            from server.routes.ws_live import ws_manager
            await ws_manager.broadcast("MEDIA_DEGRADED", {
                "session_id": self.session_id,
                "reason": reason,
                "execution_mode": self.execution_mode,
                "state_detail": "MEDIA_DEGRADED",
            })

    async def transition_to_ai_paused(self, reason: str):
        """流转至 AI_PAUSED 安全待机态 (依据《商业化运营改造方案》§3.2 与 §6.1)"""
        self.state_detail = "AI_PAUSED"
        logger.warning(
            "⚠️【AI 安全停顿】直播进入 AI_PAUSED 状态: %s (会话=%s, 档位=%s)",
            reason,
            self.session_id,
            self.execution_mode,
        )
        from server.routes.ws_live import ws_manager
        await ws_manager.broadcast("AI_PAUSED", {
            "session_id": self.session_id,
            "reason": reason,
            "execution_mode": self.execution_mode,
            "state_detail": "AI_PAUSED",
        })

    async def resume_ai(self) -> dict:
        """恢复前重新执行当前档位门禁，禁止从故障态直接改状态绕过检查。"""
        if self.state_detail not in ("AI_PAUSED", "MEDIA_DEGRADED"):
            return {"ok": False, "state_detail": self.state_detail, "reason": "当前不在可恢复暂停态"}

        # AI_PAUSED 可能只是上游 LLM/TTS 短暂故障；恢复仍需 Kill Switch 校验。
        # MEDIA_DEGRADED 必须执行完整 Production/Pilot 预检，禁止绕过媒体门禁。
        if self.state_detail == "AI_PAUSED":
            from server.core.compliance.kill_switch import global_kill_switch
            blocked = [
                feature for feature in ("stream_push", "avatar_rendering", "voice_synthesis")
                if not global_kill_switch.is_feature_enabled(feature, platform=self.platform, anchor_id=self.anchor_id)
            ]
            if blocked:
                return {"ok": False, "state_detail": self.state_detail, "reason": "Kill Switch 仍处于熔断状态", "blocking_reasons": blocked}
            self.state_detail = "LOCAL_READY"
            from server.routes.ws_live import ws_manager
            await ws_manager.broadcast("AI_RESUMED", {"session_id": self.session_id, "state_detail": "LOCAL_READY"})
            return {"ok": True, "state_detail": "LOCAL_READY"}

        from server.core.operations.preflight import global_preflight_service
        report = await global_preflight_service.run_preflight(
            mode=self.execution_mode,
            avatar_asset_dir=self.avatar_asset_dir or None,
            voice_id=self.voice_id,
            platform=self.platform,
            force=True,
        )
        from server.core.compliance.kill_switch import global_kill_switch
        kill_features = ("stream_push", "avatar_rendering", "voice_synthesis")
        kill_blocked = [
            feature for feature in kill_features
            if not global_kill_switch.is_feature_enabled(feature, platform=self.platform, anchor_id=self.anchor_id)
        ]
        if not report.passed or kill_blocked:
            return {
                "ok": False,
                "state_detail": self.state_detail,
                "reason": "恢复前生产门禁未通过",
                "blocking_reasons": list(report.blocking_reasons) + [f"KILL_SWITCH:{f}" for f in kill_blocked],
                "report": report.to_dict(),
            }

        self.state_detail = "LOCAL_READY"
        logger.info("AI 直播已通过重新预检，从暂停态恢复正常")
        from server.routes.ws_live import ws_manager
        await ws_manager.broadcast("AI_RESUMED", {
            "session_id": self.session_id,
            "state_detail": "LOCAL_READY",
            "preflight_generated_at": report.generated_at,
        })
        return {"ok": True, "state_detail": "LOCAL_READY", "report": report.to_dict()}


    async def takeover(self, operator: str = "admin", reason: str = "") -> dict:
        """人工紧急接管：原子抢占打断 AI/TTS 并将状态机流转为 HUMAN_TAKEOVER (方案 §6.3)"""
        self.state_detail = "HUMAN_TAKEOVER"
        logger.critical(
            "🛑【人工接管已触发】操作人=%s, 原因=%s, 立即原子打断 AI/TTS 并抢占播报权！",
            operator,
            reason or "人工控制台接管",
        )
        # 1. 原子抢占打断队列和当前 TTS 任务
        await self.event_queue.trigger_barge_in("human_takeover")
        current_tts = self.current_tts_task
        if current_tts and not current_tts.done():
            current_tts.cancel()
        # 2. 通知媒体路由与底层驱动停止数字人当前语音
        await global_media_router.interrupt("human_takeover")
        # 3. 广播接管事件至大屏和控制台
        from server.routes.ws_live import ws_manager
        await ws_manager.broadcast("HUMAN_TAKEOVER", {
            "session_id": self.session_id,
            "operator": operator,
            "reason": reason,
            "state_detail": "HUMAN_TAKEOVER",
            "timestamp": time.time(),
        })
        return {
            "ok": True,
            "session_id": self.session_id,
            "state_detail": "HUMAN_TAKEOVER",
            "operator": operator,
        }

    async def release_takeover(self, operator: str = "admin") -> dict:
        """释放人工接管：状态恢复至 LOCAL_READY"""
        if self.state_detail == "HUMAN_TAKEOVER":
            self.state_detail = "LOCAL_READY"
            logger.info("人工接管已释放 (操作人=%s)，恢复正常直播状态", operator)
            from server.routes.ws_live import ws_manager
            await ws_manager.broadcast("TAKEOVER_RELEASED", {
                "session_id": self.session_id,
                "operator": operator,
                "state_detail": "LOCAL_READY",
                "timestamp": time.time(),
            })
        return {
            "ok": True,
            "session_id": self.session_id,
            "state_detail": self.state_detail,
        }

    @property
    def obs_owner_session_id(self) -> Optional[str]:
        return self._obs_owner_session_id

    @obs_owner_session_id.setter
    def obs_owner_session_id(self, session_id: Optional[str]) -> None:
        """兼容旧调用：直接认领 owner 时同步记录当前 OBS epoch。"""
        self._obs_owner_session_id = session_id
        if session_id is None:
            self.obs_owner_connection_epoch = None
        elif self.obs_owner_connection_epoch is None:
            current_epoch = getattr(global_obs_client, "connection_epoch", None)
            self.obs_owner_connection_epoch = current_epoch if isinstance(current_epoch, int) else 0

    @property
    def obs_stream_started_by_agent(self) -> bool:
        """推流所有权严格绑定当前会话和 OBS 连接 epoch。"""
        current_epoch = getattr(global_obs_client, "connection_epoch", None)
        if not isinstance(current_epoch, int):
            current_epoch = self.obs_owner_connection_epoch
        return bool(
            self.obs_owner_session_id
            and self.session_id
            and self.obs_owner_session_id == self.session_id
            and self.obs_owner_connection_epoch is not None
            and self.obs_owner_connection_epoch == current_epoch
        )

    @obs_stream_started_by_agent.setter
    def obs_stream_started_by_agent(self, val: bool):
        if val and self.session_id:
            self.obs_owner_session_id = self.session_id
            current_epoch = getattr(global_obs_client, "connection_epoch", None)
            self.obs_owner_connection_epoch = current_epoch if isinstance(current_epoch, int) else 0
        else:
            self.obs_owner_session_id = None
            self.obs_owner_connection_epoch = None

    @property
    def lifecycle_lock(self) -> asyncio.Lock:
        """返回绑定当前服务 loop 的生命周期锁，兼容 TestClient lifespan 重建。"""
        running_loop = asyncio.get_running_loop()
        lock = self._lifecycle_lock
        lock_loop = getattr(lock, "_loop", None) if lock is not None else None
        if lock is None or (lock_loop is not None and lock_loop is not running_loop):
            if lock is not None and lock.locked():
                raise RuntimeError("直播生命周期锁仍被其他事件循环占用")
            lock = asyncio.Lock()
            self._lifecycle_lock = lock
        return lock

    def _stats_reset(self):
        self.stats.update({
            "start_ts": time.time(),
            "viewer_count": 0,
            "peak_viewer_count": 0,
            "gift_income_yuan": 0.0,
            "gmv_yuan": 0.0,
            "orders_count": 0,
            # 兼容字段：仅统计成功进入调度队列的真实业务事件。
            "danmaku_count": 0,
            "gift_count": 0,
            "events_received_total": 0,
            "events_accepted_total": 0,
            "events_dropped_total": 0,
            "danmaku_received_total": 0,
            "danmaku_accepted_total": 0,
            "danmaku_dropped_total": 0,
            "gift_received_total": 0,
            "gift_accepted_total": 0,
            "gift_dropped_total": 0,
            "guardrail_hits": 0,
            "flash_sales": 0
        })
        # 每场直播独立统计首 chunk/首帧/播放启动延迟，避免跨场次污染分位数。
        global_av_sync.reset()

    def _refresh_viewer_count(self):
        """单步场观采集：仅采信真实人气数据源，无数据源时保持 0，绝不伪造数据"""
        watched = getattr(self.fetcher, "watched_count", 0) if self.fetcher else 0
        if watched and watched > 0:
            self.stats["viewer_count"] = int(watched)
            self.stats["peak_viewer_count"] = max(self.stats["peak_viewer_count"], self.stats["viewer_count"])

    async def _viewer_count_loop(self):
        """场观人数巡检：真实 B 站心跳回包人气值 (无数据源时不上报，杜绝假数据)"""
        while self.is_live:
            try:
                self._refresh_viewer_count()
                await asyncio.sleep(5.0)
            except asyncio.CancelledError:
                break
            except Exception:
                await asyncio.sleep(5.0)

    def _on_danmaku_circuit_state_change(self, new_state: str, reason: str):
        """向前端 WebSocket 广播弹幕抓取熔断状态更新"""
        from server.routes.ws_live import ws_manager
        logger.warning("【弹幕熔断器】广播状态: %s - %s", new_state, reason)
        self._track_task(
            ws_manager.broadcast("danmaku_circuit_status", {
                "state": new_state,
                "reason": reason,
                "timestamp": time.time(),
            })
        )

    async def reload_runtime_config(self, section: str = "all"):
        """直播运行中配置热重载：不关播无感刷新商品、违禁词、主播人设与配置上下文"""
        logger.info("执行运行时配置热重载, section=%s", section)
        if section in ("all", "products"):
            try:
                await self._refresh_product_context()
            except Exception:
                logger.exception("热重载商品上下文失败")
        if section in ("all", "guardrails"):
            try:
                from server.database.db import AsyncSessionLocal
                from server.routes.guardrails import reload_guardrails
                async with AsyncSessionLocal() as session:
                    await reload_guardrails(session)
            except Exception:
                logger.exception("热重载违禁词引擎失败")
        if section in ("all", "roles", "anchors"):
            try:
                from server.database.db import AsyncSessionLocal
                from server.database.models import AnchorRole
                async with AsyncSessionLocal() as session:
                    active_role = global_role_manager.get_active_role()
                    if active_role:
                        res = await session.execute(select(AnchorRole).where(AnchorRole.id == active_role.role_id))
                        role_db = res.scalar_one_or_none()
                        if role_db:
                            global_role_manager.register_or_update_from_db(role_db, activate=True)
            except Exception:
                logger.exception("热重载当前角色配置失败")
        if section in ("all", "settings"):
            try:
                await self._configure_neural_sidecar()
            except Exception:
                logger.exception("热重载远端 Avatar Provider 失败，继续使用程序化 shadow")

    async def _on_barge_in(self, reason: str):
        self._audio_generation += 1
        generation = self._audio_generation
        # 先推进本地播放 fence，保证任何迟到 producer 都不能在异步中断期间复活旧音频。
        try:
            _call_sync_compat(
                global_virtual_audio.stop,
                next_generation=generation,
            )
        except Exception:
            logger.exception("推进 VirtualAudio 打断代际失败")
        # 再通知异步口型媒体与 TTS 取消其内部工作；各 sink 故障互不阻断。
        try:
            await _call_async_compat(
                global_media_driver.interrupt,
                reason,
                next_generation=generation,
            )
        except Exception:
            logger.exception("媒体驱动打断失败")
        # 物理取消当前正在进行的底层网络/生成协程
        if self.current_tts_task and not self.current_tts_task.done():
            self.current_tts_task.cancel()
        try:
            await self.tts_driver.interrupt(reason)
        except Exception:
            logger.exception("TTS 驱动打断失败")
        # 联动数字人驱动瞬间打断 (清空口型与音频队列，恢复待机)
        try:
            from server.core.avatar import get_active_avatar_driver
            await get_active_avatar_driver().flush_talk()
        except Exception:
            logger.exception("数字人驱动 flush_talk 打断失败")
        # 联动动静分离缝合器打断 (Smoothstep 缓动停顿并挂起切片断点)
        try:
            self.stitcher.trigger_interrupt(ResumePolicy.REWIND_SENTENCE)
        except Exception:
            logger.exception("切片缝合器打断失败")
        # 向前端 WebSocket 广播打断信令
        from server.routes.ws_live import ws_manager
        await ws_manager.broadcast("TRIGGER_BARGE_IN", {
            "reason": reason,
            "audio_generation": self._audio_generation,
        })

    async def _on_vram_alert(self, info: dict):
        from server.routes.ws_live import ws_manager
        await ws_manager.broadcast("VRAM_WARNING", info)

    async def _select_tts_driver(self, target_voice_id: Optional[str] = None):
        """
        根据主播专属音色与激活配置选择媒体驱动 (规划 §8/§9.2/§9.4)：
        1. 端云分离远程 GPU 节点 (Tier C，连通优先)
        2. 本地 CosyVoice 声音克隆 (优先匹配主播绑定的 voice_id 样本)
        3. Edge-TTS 云端兜底 (支持根据音色档案/配置选择匹配发音人)
        """
        from server.database.db import AsyncSessionLocal
        from server.database.models import ApiProviderConfig, VoiceProfile, Anchor
        from server.config import decrypt_secret
        from server.adapters.media.cosyvoice_driver import CosyVoiceMediaDriver
        from server.adapters.media.remote_gpu_driver import RemoteGPUMediaDriver

        tts_cfg = remote_cfg = active_voice = None
        async with AsyncSessionLocal() as session:
            res = await session.execute(
                select(ApiProviderConfig).where(
                    ApiProviderConfig.config_group == "tts", ApiProviderConfig.is_active == 1
                )
            )
            tts_cfg = res.scalars().first()

            res2 = await session.execute(
                select(ApiProviderConfig).where(
                    ApiProviderConfig.config_group == "remote_gpu", ApiProviderConfig.is_active == 1
                )
            )
            remote_cfg = res2.scalars().first()

            # 优先根据指定的主播专属 voice_id 查询音色档案
            if target_voice_id:
                res_v = await session.execute(select(VoiceProfile).where(VoiceProfile.id == target_voice_id))
                active_voice = res_v.scalars().first()

            # 若未指定或未查到，尝试从当前激活主播角色中获取关联音色
            if not active_voice:
                active_role = global_role_manager.get_active_role()
                if getattr(active_role, "default_voice_id", None):
                    res_r = await session.execute(select(VoiceProfile).where(VoiceProfile.id == active_role.default_voice_id))
                    active_voice = res_r.scalars().first()

            # 若仍未找到，尝试从主播管理表 Anchor 中获取第一个配置了 voice_id 的主播音色
            if not active_voice:
                res_a = await session.execute(select(Anchor).where(Anchor.voice_id.isnot(None)).limit(1))
                first_anchor = res_a.scalars().first()
                if first_anchor and first_anchor.voice_id:
                    res_va = await session.execute(select(VoiceProfile).where(VoiceProfile.id == first_anchor.voice_id))
                    active_voice = res_va.scalars().first()

            # 最终降级：取数据库中任意一条就绪的音色档案
            if not active_voice:
                res3 = await session.execute(select(VoiceProfile).limit(1))
                active_voice = res3.scalars().first()

        # 1) 端云分离远程 GPU 节点
        if remote_cfg and remote_cfg.base_url:
            extra = {}
            try:
                extra = json.loads(remote_cfg.extra_params_json or "{}")
            except Exception:
                pass
            token = extra.get("auth_token") or (decrypt_secret(str(remote_cfg.encrypted_api_key)) if remote_cfg.encrypted_api_key else "")
            driver = RemoteGPUMediaDriver(node_url=remote_cfg.base_url, auth_token=token)
            await driver.start()
            if driver.is_connected:
                logger.info("已启用端云分离远程 GPU 渲染通道 (Tier C)")
                return driver
            logger.info("远程 GPU 节点不可达，自动降级本地 TTS")

        # 1.5) MOSS-TTS-Nano 端侧超轻量高保真声音驱动 (优先匹配本地或远端 GPU 算力端点)
        if tts_cfg and ("moss" in (tts_cfg.provider_name or "").lower() or "nano" in (tts_cfg.provider_name or "").lower()):
            from server.adapters.media.moss_driver import MossTTSMediaDriver
            driver = MossTTSMediaDriver(
                api_base=tts_cfg.base_url or "http://127.0.0.1:9880",
                prompt_wav_path=active_voice.sample_wav_path if active_voice else None
            )
            if await driver.health_check():
                await driver.start()
                if active_voice:
                    await driver.apply_volume_gain(getattr(active_voice, "volume_gain", 1.0) or 1.0)
                    await driver.apply_speech_speed(getattr(active_voice, "speech_speed", 1.0) or 1.0)
                logger.info(f"已启用 MOSS-TTS-Nano 端侧高保真声音驱动 (音色/参考: {active_voice.name if active_voice else '默认'})")
                return driver
            logger.warning("MOSS-TTS-Nano 端点连通性检查未通过 (端口 9880 未启动)，自动降级 Edge-TTS 兜底开播")

        # 2) 本地 CosyVoice 声音克隆 (前置健康检查：不可达自动降级 Edge-TTS，绝不静音开播)
        if tts_cfg and "cosyvoice" in (tts_cfg.provider_name or "").lower():
            driver = CosyVoiceMediaDriver(
                api_base=tts_cfg.base_url or "http://127.0.0.1:9233",
                prompt_wav_path=active_voice.sample_wav_path if active_voice else None
            )
            if await driver.health_check():
                await driver.start()
                logger.info(f"已启用本地 CosyVoice 声音克隆驱动 (音色: {active_voice.name if active_voice else '默认'})")
                return driver
            logger.warning("本地 CosyVoice 服务健康检查未通过，自动降级 Edge-TTS 云端语音 (ADR-10)")

        # 2.5) MiniMax 云端 TTS (规划 §9.2)：有凭据即启用，合成失败自动回退 Edge (ADR-10)
        if tts_cfg and "minimax" in (tts_cfg.provider_name or "").lower():
            from server.adapters.media.minimax_driver import MinimaxTTSMediaDriver
            extra = {}
            try:
                extra = json.loads(tts_cfg.extra_params_json or "{}")
            except Exception:
                pass
            key = decrypt_secret(str(tts_cfg.encrypted_api_key)) if tts_cfg.encrypted_api_key else ""
            driver = MinimaxTTSMediaDriver(
                api_base=tts_cfg.base_url or "https://api.minimax.chat/v1",
                api_key=key,
                group_id=extra.get("group_id", ""),
                voice_id=extra.get("voice_id", ""),
            )
            if await driver.health_check():
                await driver.start()
                if active_voice:
                    await driver.apply_volume_gain(getattr(active_voice, "volume_gain", 1.0) or 1.0)
                logger.info("已启用 MiniMax 云端 TTS 驱动 (T2A v2)")
                return driver
            logger.warning("MiniMax TTS 缺少有效 API Key，自动降级 Edge-TTS (ADR-10)")

        # 2.6) ElevenLabs 云端 TTS (规划 §9.2)：有凭据即启用，合成失败自动回退 Edge (ADR-10)
        # voice_id 优先取配置 extra_params，其次取克隆档案登记的 ElevenLabs voice_id
        # (voices.py 克隆成功后把官方 voice_id 直接写入 VoiceProfile.id)
        if tts_cfg and "eleven" in (tts_cfg.provider_name or "").lower():
            from server.adapters.media.elevenlabs_driver import ElevenLabsTTSMediaDriver
            extra = {}
            try:
                extra = json.loads(tts_cfg.extra_params_json or "{}")
            except Exception:
                pass
            key = decrypt_secret(str(tts_cfg.encrypted_api_key)) if tts_cfg.encrypted_api_key else ""
            el_voice = extra.get("voice_id", "")
            if not el_voice and active_voice and str(getattr(active_voice, "id", "") or ""):
                # 克隆档案的 id 即 ElevenLabs 官方 voice_id (voices.py IVC 落库约定)
                vid = str(active_voice.id)
                if not vid.startswith("clone_"):
                    el_voice = vid
            driver = ElevenLabsTTSMediaDriver(
                api_base=tts_cfg.base_url or "https://api.elevenlabs.io/v1",
                api_key=key,
                voice_id=el_voice,
                model_id=extra.get("model_id", "eleven_multilingual_v2"),
            )
            if await driver.health_check():
                await driver.start()
                if active_voice:
                    await driver.apply_volume_gain(getattr(active_voice, "volume_gain", 1.0) or 1.0)
                    await driver.apply_speech_speed(getattr(active_voice, "speech_speed", 1.0) or 1.0)
                logger.info(f"已启用 ElevenLabs 云端 TTS 驱动 (音色: {el_voice or '默认'})")
                return driver
            logger.warning("ElevenLabs TTS 缺少有效 API Key 或音色，自动降级 Edge-TTS (ADR-10)")

        # 3) Edge-TTS 云端兜底
        voice_name = (tts_cfg.model_name if tts_cfg and tts_cfg.model_name else "")
        if not voice_name and active_voice and ("Neural" in active_voice.name or "zh-" in active_voice.name):
            voice_name = active_voice.name
        voice_name = voice_name or "zh-CN-XiaoxiaoNeural"
        logger.info(f"已启用 Edge-TTS 云端驱动，音色: {voice_name}")
        driver = EdgeTTSMediaDriver(voice=voice_name)
        if active_voice:
            await driver.apply_volume_gain(getattr(active_voice, "volume_gain", 1.0) or 1.0)
        return driver

    async def _configure_neural_sidecar(self) -> None:
        """加载全部 enabled renderer-only v3 Provider；不可用不影响开播。"""
        from server.adapters.media.avatar_orchestrator import (
            AvatarProviderEntry,
            AvatarProviderOrchestrator,
        )
        from server.adapters.media.avatar_provider_registry import create_avatar_provider
        from server.config import decrypt_secret
        from server.database.db import AsyncSessionLocal
        from server.database.models import ApiProviderConfig

        previous_orchestrator = global_media_router.avatar_orchestrator
        if previous_orchestrator is not None:
            try:
                await asyncio.wait_for(_call_async_compat(previous_orchestrator.stop), timeout=4.0)
            except Exception:
                logger.exception(
                    "旧 Avatar Provider 编排器未确认停止；中止热替换并保持程序化 shadow"
                )
                return
            global_media_router.detach_avatar_orchestrator()

        async with AsyncSessionLocal() as session:
            result = await session.execute(
                select(ApiProviderConfig)
                .where(
                    ApiProviderConfig.config_group == "neural_renderer",
                    ApiProviderConfig.is_active == 1,
                )
                .order_by(ApiProviderConfig.id.asc())
            )
            configs = list(result.scalars().all())

        entries = []
        preview_driver = None
        for config in configs:
            if not config.base_url:
                logger.warning("跳过无 base_url 的 Avatar Provider 配置 id=%s", config.id)
                continue
            try:
                extra = json.loads(config.extra_params_json or "{}")
                if not isinstance(extra, dict):
                    raise ValueError("extra_params 必须是 object")
                token = (
                    decrypt_secret(str(config.encrypted_api_key))
                    if config.encrypted_api_key
                    else ""
                )
                provider, policy = create_avatar_provider(
                    provider_instance_id=config.id,
                    display_name=config.provider_name or f"Avatar Provider {config.id}",
                    base_url=config.base_url,
                    credential=token,
                    model_name=config.model_name,
                    extra_params=extra,
                )
                entries.append(AvatarProviderEntry(provider=provider, policy=policy))
                if preview_driver is None:
                    preview_driver = provider
            except Exception as exc:
                logger.warning("跳过无效 Avatar Provider 配置 id=%s: %s", config.id, exc)

        if not entries:
            global_media_router.detach_avatar_orchestrator()
            return
        orchestrator = AvatarProviderOrchestrator(entries)
        global_media_router.attach_avatar_orchestrator(
            orchestrator,
            preview_driver=preview_driver,
        )
        # 热加载发生在本地 renderer 已运行之后，新编排器需立即启动；首次开播由 Router.start 负责。
        if global_media_router.is_running:
            await orchestrator.start()

    def _track_task(self, coroutine):
        """跟踪短生命周期后台任务，停播时统一收敛，避免跨场执行。"""
        task = asyncio.create_task(coroutine)
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)
        return task

    def set_state_detail(self, state: str) -> None:
        """更新当前直播生命周期状态机 (PRECHECKING / LOCAL_READY / OBS_STREAMING / PLATFORM_CONFIRMED / AI_RUNNING 等)"""
        logger.info(f"直播会话状态流转: {self.state_detail} -> {state}")
        self.state_detail = state

    async def start(self, session_id: str, products: list, room_id: Optional[str] = None, platform: str = "bilibili", theme: str = "", voice_id: Optional[str] = None, avatar_path: str = "", mode: str = "B", landmarks_path: str = "", demo_mode: bool = False, avatar_asset_dir: str = "", execution_mode: str = "PREVIEW"):
        """事务式启动资源；任一步失败都回滚已启动任务和设备。"""
        try:
            await self._start_resources(
                session_id, products, room_id, platform, theme, voice_id,
                avatar_path, mode, landmarks_path, demo_mode=demo_mode,
                avatar_asset_dir=avatar_asset_dir,
                execution_mode=execution_mode,
            )
        except Exception:
            logger.exception("直播资源启动失败，正在回滚")
            await self.stop()
            raise

    async def _start_resources(self, session_id: str, products: list, room_id: Optional[str] = None, platform: str = "bilibili", theme: str = "", voice_id: Optional[str] = None, avatar_path: str = "", mode: str = "B", landmarks_path: str = "", demo_mode: bool = False, avatar_asset_dir: str = "", execution_mode: str = "PREVIEW"):
        if self.is_live:
            return
        self.is_live = True
        self.demo_mode = demo_mode
        self.execution_mode = execution_mode
        self.anchor_id = None
        self.voice_id = voice_id
        self.avatar_asset_dir = avatar_asset_dir or ""
        self.state_detail = "LOCAL_READY"
        self.platform_confirmed = False
        self._session_generation += 1
        self._audio_generation += 1
        self.session_id = session_id
        self.event_queue = PriorityBargeInQueue(on_interrupt_callback=self._on_barge_in)
        self.platform = platform
        self.live_context = {"products": products, "theme": theme or ""}
        self.history.clear()
        self.aggregator = BarrageAggregator(window_seconds=3.0)
        self._stats_reset()
        from server.core.llm.budget_manager import get_llm_budget_manager
        get_llm_budget_manager().reset_session(session_id)

        # 启动场观统计巡检协程
        if self.viewer_task and not self.viewer_task.done():
            self.viewer_task.cancel()
        self.viewer_task = asyncio.create_task(self._viewer_count_loop())

        # 选择并启动真实语音驱动 (与主播专属音色绑定)
        try:
            self.tts_driver = await self._select_tts_driver(target_voice_id=voice_id)
        except Exception as e:
            logger.warning(f"TTS 驱动选择异常，回退 Edge-TTS: {e}")
            self.tts_driver = EdgeTTSMediaDriver()
        await self.tts_driver.start()

        # 端云分离 (Tier C)：若启用远程 GPU 节点，则将其挂载为媒体帧来源优先显示
        from server.adapters.media.remote_gpu_driver import RemoteGPUMediaDriver
        if isinstance(self.tts_driver, RemoteGPUMediaDriver):
            global_media_router.attach_remote(self.tts_driver)
        else:
            global_media_router.detach_remote()

        # renderer-only sidecar 使用独立 neural_renderer 配置组；配置或握手失败不阻断本地开播。
        try:
            await self._configure_neural_sidecar()
        except Exception:
            logger.exception("加载神经渲染 sidecar 配置失败，继续使用本地 renderer")
            global_media_router.detach_sidecar()

        # 启动数字人渲染引擎与音画帧管道 (根据模式、主播底图与人脸关键点，商业档位硬阻断)
        global_media_router.set_execution_mode(execution_mode)
        global_media_router.set_on_degraded(self._on_media_degraded)
        global_media_router.select_driver(
            mode=mode,
            avatar_path=avatar_path,
            landmarks_path=landmarks_path,
            avatar_asset_dir=avatar_asset_dir,
            execution_mode=execution_mode,
        )
        await global_media_router.start()

        # 启动 VRAM 看门狗 (无 GPU 环境自动空转)
        self.vram_watchdog.start()
        # 启动低配机 CPU/内存自适应降频看门狗 (保障音频算力优先)
        from server.core.monitoring.system_resource_watchdog import global_resource_watchdog
        global_resource_watchdog.start()
        # 启动 P1 媒体质量看门狗与 SLI 监控 (按档位套用停滞门限 + 输出就绪探针)
        from server.core.monitoring.media_quality_watchdog import global_media_watchdog
        _configure_media_watchdog(execution_mode)
        global_media_watchdog.reset()
        global_media_watchdog.start()
        # 启动场次成本计量中枢
        from server.core.compliance.cost_privacy_manager import global_cost_privacy_manager
        global_cost_privacy_manager.start_session(session_id=session_id)

        # 根据平台经注册表选择真实抓取器；正式平台绝不静默注入假弹幕
        from server.adapters.danmaku.registry import global_danmaku_registry
        platform_lower = (platform or "bilibili").lower().strip()
        raw_room = (room_id or "").strip()
        is_explicit_demo = self.demo_mode or platform_lower in ("mock", "demo") or raw_room.lower() in ["room_demo", "mock"]

        if is_explicit_demo:
            logger.info("启动仿真弹幕注入器 (MockDanmakuFetcher, auto_inject=True)")
            self.fetcher = MockDanmakuFetcher("room_demo_888", self._on_danmaku_event, auto_inject=True)
        elif global_danmaku_registry.has(platform_lower):
            if platform_lower == "bilibili":
                clean_id = re.sub(r"[^0-9]", "", raw_room.split("?")[0].rstrip("/").split("/")[-1]) or raw_room
            else:
                clean_id = raw_room.strip()
            if not clean_id:
                logger.warning("正式平台【%s】未提供有效房间号，挂载无自动注入的被动中继器", platform_lower)
                self.fetcher = MockDanmakuFetcher(room_id="passive", on_event_callback=self._on_danmaku_event, auto_inject=False)
            else:
                logger.info(f"启动【{platform_lower}】真实弹幕监听，房间: {clean_id}")
                fetcher_kwargs = {}
                if platform_lower == "douyin":
                    fetcher_kwargs = await self._load_douyin_cookie_config()
                real_fetcher = global_danmaku_registry.create(platform_lower, clean_id, self._on_danmaku_event, **fetcher_kwargs)
                if real_fetcher:
                    self.fetcher = CircuitBreakerDanmakuFetcher(
                        real_fetcher=real_fetcher,
                        room_id=clean_id,
                        on_event_callback=self._on_danmaku_event,
                        on_state_change_callback=self._on_danmaku_circuit_state_change,
                        failure_threshold=5,
                        recovery_timeout_sec=30.0,
                        enable_mock_fallback=False,  # 正式直播严禁自动注入假弹幕
                    )
                else:
                    logger.error("无法为正式平台【%s】创建弹幕监听器，挂载无自动注入的被动中继器", platform_lower)
                    self.fetcher = MockDanmakuFetcher(room_id=clean_id, on_event_callback=self._on_danmaku_event, auto_inject=False)
        else:
            logger.info(
                f"平台【{platform}】未内置真实协议 (可用平台: {global_danmaku_registry.list_platforms()})，"
                f"已挂载被动监听器：请通过 POST /live/danmaku-webhook 或 WS /ws/danmaku-ingest 推送弹幕"
            )
            # 被动模式：不注入仿真事件，弹幕完全依赖外部中继推送
            self.fetcher = MockDanmakuFetcher(room_id=raw_room or "passive", on_event_callback=self._on_danmaku_event, auto_inject=False)

        await self.fetcher.start()

        # 启动优先级队列消费工作协程
        self.worker_task = asyncio.create_task(self._consume_queue_loop())

        # 启动视觉感知采集协程 (按配置节流抓取桌面/摄像头画面)
        if self.vision_task and not self.vision_task.done():
            self.vision_task.cancel()
        self.vision_task = asyncio.create_task(self._vision_loop())

        # 动静分离核心：从 ecommerce_sku_slices 表真实加载商品就绪切片并装载入队
        await self._load_product_slices_to_stitcher(products)

        # 启动缝合器 25 FPS 连续帧循环调度协程
        if self.stitcher_task and not self.stitcher_task.done():
            self.stitcher_task.cancel()
        self.stitcher_task = asyncio.create_task(self._stitcher_frame_loop())

        logger.info(f"直播会话已启动: {session_id}，平台: {platform}")

    async def _load_product_slices_to_stitcher(self, products: list):
        """开播时从 ecommerce_sku_slices 数据库表装载商品切片资产至待播队列"""
        self.stitcher.clear_playlist()
        prod_ids = []
        if isinstance(products, list):
            for p in products:
                if isinstance(p, dict) and p.get("id"):
                    prod_ids.append(str(p["id"]))
                elif hasattr(p, "id"):
                    prod_ids.append(str(p.id))

        from server.database.db import AsyncSessionLocal
        from server.database.models import SkuSlice
        async with AsyncSessionLocal() as session:
            stmt = select(SkuSlice).where(SkuSlice.is_active == 1)
            if prod_ids:
                stmt = stmt.where(SkuSlice.product_id.in_(prod_ids))
            stmt = stmt.order_by(SkuSlice.product_id, SkuSlice.created_at)
            res = await session.execute(stmt)
            slices = res.scalars().all()

        loaded_count = 0
        for s in slices:
            v_p = Path(s.video_path)
            if v_p.exists():
                noise_profile = None
                if s.bg_noise_profile:
                    try:
                        noise_profile = json.loads(s.bg_noise_profile)
                    except Exception:
                        pass
                handle = HybridSliceHandle(
                    slice_id=s.id,
                    product_id=s.product_id,
                    slot_code=s.slot_code,
                    video_path=s.video_path,
                    audio_path=s.audio_path,
                    duration_sec=float(s.duration_sec or 0.0),
                    loudness_lufs=float(s.loudness_lufs or -16.0),
                    noise_profile=noise_profile,
                )
                self.stitcher.enqueue_slice(handle)
                loaded_count += 1

        logger.info(
            f"[LiveSessionController] 动静分离初始化：已为 {len(prod_ids)} 个商品装载 {loaded_count} 个高精切片"
        )

    async def _stitcher_frame_loop(self):
        """25 FPS 驱动流缝合器：统一向 global_frame_bus 扇出最高优先级(100)视频帧"""
        frame_interval = 1.0 / 25.0
        while self.is_live:
            t0 = time.monotonic()
            try:
                # 若当前正处于实时动轨交互状态，自动从活动渲染驱动获取最新帧喂入动轨供帧缓冲区
                if self.stitcher.state == StitcherState.STREAMING_REALTIME:
                    rt_frame = None
                    if hasattr(global_media_router, "get_latest_frame_bgr"):
                        rt_frame = global_media_router.get_latest_frame_bgr()
                    if rt_frame is not None:
                        self.stitcher.feed_realtime_frame(rt_frame)

                # 获取待机底图作为无切片或过渡停顿时的底托
                base_idle = None
                if hasattr(global_media_router, "get_latest_frame_bgr"):
                    base_idle = global_media_router.get_latest_frame_bgr()

                frame = self.stitcher.tick_frame(base_idle_frame=base_idle)
                if frame is not None:
                    # 向 global_frame_bus 发布帧 (优先级 100 压制本地 shadow)
                    global_frame_bus.publish_frame(
                        frame,
                        owner="hybrid_stitcher",
                        priority=100,
                    )
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.debug(f"[HybridStitcher] 帧循环异常: {e}")

            elapsed = time.monotonic() - t0
            sleep_time = max(0.002, frame_interval - elapsed)
            await asyncio.sleep(sleep_time)

    def _on_slice_transition_filler(
        self,
        old_slice: Optional[HybridSliceHandle],
        next_slice: Optional[HybridSliceHandle],
        filler_text: str,
    ) -> None:
        """
        切片转场过场词事件钩子：
        1. 登记当前直播上下文，并通过 WebSocket 广播给前端中控大屏；
        2. 启动异步任务调用 synthesize_preview_audio 合成该语气过场词音频；
        3. 向 global_virtual_audio 投递播放 (带 audio_generation 栅栏，支持打断)。
        """
        try:
            from server.routes.ws_live import ws_manager
            old_slot = old_slice.slot_code if old_slice else ""
            next_slot = next_slice.slot_code if next_slice else ""
            logger.info(
                f"[LiveSessionController] 切片转场过场词触发: '{filler_text}' "
                f"({old_slot} -> {next_slot})"
            )
            self.live_context["last_transition_filler"] = filler_text

            async def _synthesize_and_play():
                try:
                    # 1. 广播信令给中控大屏
                    await ws_manager.broadcast(
                        "TRANSITION_FILLER",
                        {
                            "filler_text": filler_text,
                            "old_slice_id": old_slice.slice_id if old_slice else None,
                            "old_slot_code": old_slot,
                            "next_slice_id": next_slice.slice_id if next_slice else None,
                            "next_slot_code": next_slot,
                            "timestamp": time.time(),
                        },
                    )

                    # 2. TTS 合成过场词语音
                    from server.core.audio.tts_preview_service import (
                        PreviewSpeechParams,
                        synthesize_preview_audio,
                    )
                    gen_before = self._audio_generation
                    tts_params = PreviewSpeechParams(
                        provider_name="edge_tts",
                        voice_name="zh-CN-XiaoxiaoNeural",
                        text=filler_text,
                    )
                    audio_bytes, _media_type = await synthesize_preview_audio(tts_params)

                    # 3. 栅栏检查：若在合成期间发生了打断或已停播，则丢弃
                    if (
                        not self.is_live
                        or self._audio_generation != gen_before
                        or not audio_bytes
                    ):
                        return

                    # 4. 投递到 global_virtual_audio 播放
                    global_virtual_audio.play_chunk(
                        audio_bytes,
                        fallback_sample_rate=24000,
                        codec="wav",
                        audio_generation=self._audio_generation,
                        audio_id=f"filler_{uuid.uuid4().hex[:8]}_{self._audio_generation}",
                    )
                    logger.info(
                        f"[LiveSessionController] 过场词音频已送入虚拟声卡: '{filler_text}', gen={self._audio_generation}"
                    )
                except Exception as ex:
                    logger.debug(f"[LiveSessionController] 过场词合成/播放异常: {ex}")

            try:
                loop = asyncio.get_running_loop()
                loop.create_task(_synthesize_and_play())
            except RuntimeError:
                pass
        except Exception as e:
            logger.debug(f"[LiveSessionController] 切片转场回调处理异常: {e}")

    def _on_slice_audio_playback(
        self,
        slice_handle: HybridSliceHandle,
        start_frame: int = 0,
        use_crossfade: bool = True,
    ) -> None:
        """
        向 virtual_audio 投递切片音频，实现动静分离中静轨的声音真实播出。
        支持每轮播动态微变速、WAV头自动剥离、断点重入截取与 50ms 平滑 Cross-fade。
        """
        if not self.is_live or slice_handle is None:
            return

        try:
            pcm_bytes = slice_handle.get_audio_pcm(dynamic_varispeed=True, start_frame=start_frame)
            if not pcm_bytes:
                return

            pcm_float = np.frombuffer(pcm_bytes, dtype=np.int16).astype(np.float32) / 32768.0

            if use_crossfade and len(pcm_float) > CROSSFADE_SAMPLES_16K:
                prev_tail = getattr(self, "_last_slice_audio_tail", None)
                if prev_tail is None:
                    prev_tail = np.zeros(CROSSFADE_SAMPLES_16K, dtype=np.float32)
                faded_pcm = apply_audio_crossfade(
                    prev_tail,
                    pcm_float,
                    crossfade_samples=CROSSFADE_SAMPLES_16K,
                )
                self._last_slice_audio_tail = pcm_float[-CROSSFADE_SAMPLES_16K:].copy()
                pcm_bytes = (np.clip(faded_pcm, -1.0, 1.0) * 32767.0).astype(np.int16).tobytes()
            else:
                self._last_slice_audio_tail = (
                    pcm_float[-CROSSFADE_SAMPLES_16K:].copy()
                    if len(pcm_float) >= CROSSFADE_SAMPLES_16K
                    else None
                )

            audio_id = f"slice_{slice_handle.slice_id}_{self._audio_generation}_{start_frame}"
            global_virtual_audio.play_chunk(
                pcm_bytes,
                fallback_sample_rate=16000,
                codec="pcm_s16le",
                allow_raw_pcm=True,
                audio_generation=self._audio_generation,
                audio_id=audio_id,
            )
            logger.info(
                f"[LiveSessionController] 投递切片音频至虚拟声卡: {slice_handle.slice_id} "
                f"(帧偏移={start_frame}/{slice_handle.total_frames}, 字节数={len(pcm_bytes)}, "
                f"比率={slice_handle.current_loop_speed_ratio:.4f}, gen={self._audio_generation})"
            )
        except Exception as e:
            logger.warning(f"[LiveSessionController] 投递切片音频失败: {e}")

    async def _load_douyin_cookie_config(self) -> dict:
        """读取用户在 app_settings 配置的抖音鉴权 Cookie (douyin_ttwid / douyin_ms_token，选填)"""
        from server.database.db import AsyncSessionLocal
        from server.database.models import AppSetting
        from server.config import decrypt_secret
        try:
            async with AsyncSessionLocal() as session:
                res = await session.execute(
                    select(AppSetting).where(AppSetting.key.in_(["douyin_ttwid", "douyin_ms_token"]))
                )
                kv = {r.key: (r.value or "") for r in res.scalars().all()}
            kwargs = {}
            if kv.get("douyin_ttwid"):
                val = decrypt_secret(kv["douyin_ttwid"]) or kv["douyin_ttwid"]
                kwargs["ttwid"] = val
            if kv.get("douyin_ms_token"):
                val = decrypt_secret(kv["douyin_ms_token"]) or kv["douyin_ms_token"]
                kwargs["ms_token"] = val
            return kwargs
        except Exception:
            return {}

    async def _vision_loop(self):
        """视觉感知采集：按配置频次抓取画面并写入 live_context 供多模态大脑感知"""
        from server.database.db import AsyncSessionLocal
        from server.routes.settings import _load_vision_config
        while self.is_live:
            try:
                async with AsyncSessionLocal() as session:
                    cfg = await _load_vision_config(session)
                if cfg.get("enabled"):
                    b64 = await asyncio.to_thread(global_vision.capture_b64)
                    if b64:
                        self.live_context["vision_image_b64"] = b64
                await asyncio.sleep(max(0.5, float(cfg.get("interval_sec", 2.5))))
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.debug(f"视觉采集巡检异常: {e}")
                await asyncio.sleep(2.0)

    async def _safe_close(
        self,
        name: str,
        closer,
        timeout: float = 3.0,
    ) -> None:
        """Fail-Safe 清理闭包：接收清理函数，严格超时控制与异常隔离，记录清理错误"""
        try:
            result = closer()
            if inspect.isawaitable(result):
                await asyncio.wait_for(result, timeout=timeout)
        except asyncio.TimeoutError:
            err_msg = f"清理资源超时: {name}"
            logger.error(err_msg)
            self.cleanup_errors.append(err_msg)
        except BaseException as e:
            err_msg = f"清理资源失败: {name} ({e})"
            logger.exception(err_msg)
            self.cleanup_errors.append(err_msg)

    async def stop(self) -> list:
        """Fail-Safe 幂等停止并隔离等待所有会话资源退出，严格按 8 步安全顺序清理，并返回清理错误列表。"""
        self.is_live = False
        self._session_generation += 1
        self._audio_generation += 1
        self.cleanup_errors = []

        try:
            # 1. 立即停止接收新弹幕
            if self.fetcher:
                await self._safe_close("danmaku_fetcher", self.fetcher.stop, timeout=3.0)
                self.fetcher = None

            # 2. 取消主消费、场观、视觉与缝合器任务
            session_tasks = [
                task for task in (self.worker_task, self.viewer_task, self.vision_task, self.stitcher_task)
                if task and not task.done()
            ]
            for task in session_tasks:
                task.cancel()
            if session_tasks:
                await asyncio.gather(*session_tasks, return_exceptions=True)
            self.worker_task = self.viewer_task = self.vision_task = self.stitcher_task = None
            try:
                self.stitcher.clear_playlist()
                self._last_slice_audio_tail = None
            except Exception:
                pass

            # 3. 收敛后台广播/数据库任务
            pending = [task for task in self._background_tasks if not task.done()]
            if pending:
                try:
                    await asyncio.wait_for(asyncio.gather(*pending, return_exceptions=True), timeout=2.0)
                except asyncio.TimeoutError:
                    for task in pending:
                        if not task.done():
                            task.cancel()
                    await asyncio.gather(*pending, return_exceptions=True)
            self._background_tasks.clear()

            # 4. 停止音频和 TTS
            current_tts_task = self.current_tts_task
            self.current_tts_task = None
            if current_tts_task and not current_tts_task.done():
                current_tts_task.cancel()
                await asyncio.gather(current_tts_task, return_exceptions=True)
            if hasattr(self, "tts_driver") and self.tts_driver:
                await self._safe_close("tts_driver", self.tts_driver.stop, timeout=3.0)
            await self._safe_close(
                "virtual_audio",
                lambda: _call_sync_compat(global_virtual_audio.stop, next_generation=self._audio_generation),
                timeout=2.0,
            )

            # 5. 停止远程 GPU 和本地媒体
            await self._safe_close("detach_remote", global_media_router.detach_remote, timeout=2.0)
            await self._safe_close("media_router", global_media_router.stop, timeout=3.0)
            await self._safe_close("detach_sidecar", global_media_router.detach_sidecar, timeout=2.0)
            global_media_router.reset_media_degraded()
            global_media_router.set_on_degraded(None)

            # 6. 停止摄像头、声卡和视觉感知设备
            await self._safe_close("vision", global_vision.close, timeout=2.0)
            self.live_context.pop("vision_image_b64", None)
            if hasattr(self, "vram_watchdog") and self.vram_watchdog:
                await self._safe_close("vram_watchdog", self.vram_watchdog.stop, timeout=2.0)
            from server.core.monitoring.system_resource_watchdog import global_resource_watchdog
            await self._safe_close("resource_watchdog", global_resource_watchdog.stop, timeout=2.0)
            from server.core.monitoring.media_quality_watchdog import global_media_watchdog
            await self._safe_close("media_watchdog", global_media_watchdog.stop, timeout=2.0)

            # 7. 清空事件队列
            await self._safe_close("event_queue", self.event_queue.clear, timeout=1.0)

            # 7.1 关闭残留的 WebRTC/WHEP 会话，避免跨场次泄漏 ICE/DTLS 连接
            try:
                from server.core.media.webrtc_streamer import get_webrtc_stream_manager
                webrtc_manager = get_webrtc_stream_manager()
                if webrtc_manager is not None:
                    await self._safe_close("webrtc_sessions", webrtc_manager.close_all, timeout=3.0)
            except Exception as webrtc_close_exc:
                logger.debug(f"WebRTC 会话收敛跳过（可选依赖缺失）: {webrtc_close_exc}")

        finally:
            # 8. 最终复位所有身份和引用
            self.is_live = False
            self.session_id = None
            self.obs_owner_session_id = None
            self.obs_owner_connection_epoch = None
            self.live_context = {"products": []}
            self.fetcher = None
            self.worker_task = self.viewer_task = self.vision_task = None
            logger.info("直播会话已安全释放所有硬件与内存资源 (清理错误数: %d)", len(self.cleanup_errors))

        return list(self.cleanup_errors)

    async def ingest_event(
        self,
        event_type: str,
        user_name: str,
        payload: dict,
        priority: int = 2,
        source: str = "real",
        is_mock: bool = False,
    ) -> dict:
        """
        统一事件入口：Webhook、WebSocket、真实平台与仿真测试均由此收敛。
        参数标准化、正式指标统计隔离（杜绝 Mock 污染）、实时大屏广播、队列背压与审计。
        """
        if not self.is_live:
            return {"accepted": False, "reason": "not_live"}

        is_mock_event = bool(is_mock or payload.get("_is_mock") or payload.get("_is_fallback") or source == "mock")
        payload["_source"] = source
        payload["_is_mock"] = is_mock_event
        metric_kind = "danmaku" if event_type in ("danmaku", "chat") else event_type

        # 正式入口指标只统计真实事件；瀑布流仍展示 received（包括 Mock）。
        if not is_mock_event:
            self.stats["events_received_total"] = int(self.stats.get("events_received_total") or 0) + 1
            if metric_kind in ("danmaku", "gift"):
                key_recv = f"{metric_kind}_received_total"
                self.stats[key_recv] = int(self.stats.get(key_recv) or 0) + 1

        # 实时广播大屏瀑布流
        from server.routes.ws_live import ws_manager
        text_content = payload.get("text") or (
            f"送出【{payload.get('gift_name')}】x{payload.get('count', 1)}"
            if event_type == "gift"
            else "进入直播间"
        )
        self._track_task(
            ws_manager.broadcast("danmaku", {
                "user": user_name,
                "text": text_content,
                "event_type": event_type,
                "priority": priority,
                "is_mock": is_mock_event,
            })
        )

        event_id = payload.get("event_id") or f"evt_{uuid.uuid4().hex[:8]}"
        res = await self.event_queue.put(
            event_id=event_id,
            event_type=event_type,
            user_name=user_name,
            payload=payload,
            priority=priority,
            as_result=True,
        )
        if not is_mock_event:
            if res.accepted:
                self.stats["events_accepted_total"] = int(self.stats.get("events_accepted_total") or 0) + 1
                if metric_kind in ("danmaku", "gift"):
                    key_acc = f"{metric_kind}_accepted_total"
                    self.stats[key_acc] = int(self.stats.get(key_acc) or 0) + 1
                # 运营兼容指标与收入只累计成功受理的真实事件。
                if metric_kind == "gift":
                    total_coin = payload.get("total_coin", 0) or 0
                    self.stats["gift_income_yuan"] = round(
                        float(self.stats.get("gift_income_yuan") or 0.0) + total_coin / 1000.0,
                        2,
                    )
                    self.stats["gift_count"] = int(self.stats.get("gift_count") or 0) + 1
                elif metric_kind == "danmaku":
                    self.stats["danmaku_count"] = int(self.stats.get("danmaku_count") or 0) + 1
            else:
                self.stats["events_dropped_total"] = int(self.stats.get("events_dropped_total") or 0) + 1
                if metric_kind in ("danmaku", "gift"):
                    key_drop = f"{metric_kind}_dropped_total"
                    self.stats[key_drop] = int(self.stats.get(key_drop) or 0) + 1

        # accepted 新事件可能驱逐旧事件；dropped 必须按被驱逐项自身的来源和类型归属。
        if res.accepted and res.dropped_event_id and res.dropped_is_mock is False:
            dropped_kind = "danmaku" if res.dropped_event_type in ("danmaku", "chat") else res.dropped_event_type
            self.stats["events_dropped_total"] = int(self.stats.get("events_dropped_total") or 0) + 1
            if dropped_kind in ("danmaku", "gift"):
                key_evict = f"{dropped_kind}_dropped_total"
                self.stats[key_evict] = int(self.stats.get(key_evict) or 0) + 1

        return {
            "accepted": bool(res),
            "event_id": event_id,
            "dropped_event_id": getattr(res, "dropped_event_id", None),
            "reason": getattr(res, "reason", None),
        }

    def _on_danmaku_event(self, event_type: str, user_name: str, payload: dict, priority: int = 2):
        is_mock = bool(payload.get("_is_mock") or payload.get("_is_fallback"))
        self._track_task(
            self.ingest_event(
                event_type=event_type,
                user_name=user_name,
                payload=payload,
                priority=priority,
                source="danmaku_fetcher",
                is_mock=is_mock,
            )
        )

    async def _refresh_product_context(self):
        """每个业务事件前刷新活跃商品，场次审计快照保持开播时状态。"""
        from server.database.db import AsyncSessionLocal
        from server.database.models import Product
        from server.routes.products import product_to_context

        async with AsyncSessionLocal() as session:
            rows = (
                await session.execute(select(Product).where(Product.is_active == 1))
            ).scalars().all()
        self.live_context["products"] = [product_to_context(product) for product in rows]

    async def _consume_queue_loop(self):
        """核心业务循环：取事件 -> 弹幕聚合 -> 角色大脑(LLM+RAG+MCP) -> 违禁词过滤 -> 人类化 -> TTS -> 广播"""
        from server.routes.ws_live import ws_manager
        from server.core.queue.priority_queue import LiveEventItem
        sentence_delimiters = _SENTENCE_DELIMITERS

        while self.is_live:
            try:
                # 若被打断，先复位
                self.event_queue.reset_interrupt()

                # 取出最高优先级事件：若 8 秒内无观众弹幕，自动触发 P3 冷场垫场
                try:
                    event = await asyncio.wait_for(self.event_queue.get(), timeout=8.0)
                except asyncio.TimeoutError:
                    if not self.is_live:
                        break
                    event = LiveEventItem(
                        priority=3,
                        timestamp=time.time(),
                        event_id=f"evt_idle_{uuid.uuid4().hex[:6]}",
                        event_type="idle_filler",
                        user_name="系统调度",
                        payload={"text": "冷场轮播讲解"}
                    )

                if not event:
                    continue

                # 事件出队即复位打断令牌：P0 事件自身触发的 barge-in 信号到此消费完毕，
                # 后续若再次被打断，将由新的 P0 触发重新置位 (粘滞令牌语义，见 priority_queue)
                self.event_queue.reset_interrupt()

                event_started_at = time.time()
                await self._refresh_product_context()
                active_role = global_role_manager.get_active_role()
                logger.info(f"开始处理直播事件 [P{event.priority}]: {event.event_type} 来自 {event.user_name}")

                # 动作状态机事件联动 (如礼物打赏自动触发动作4致谢，新进观众触发动作1欢迎)
                try:
                    from server.core.avatar import get_action_state_machine
                    get_action_state_machine().evaluate_event(event.event_type, event.payload)
                except Exception:
                    pass

                # 注入多轮对话历史 (转 list 快照，供角色 LLM 上下文引用)
                self.live_context["history"] = list(self.history)

                # 应用角色语速到 TTS 驱动 (含防封杀 ±3% 随机微扰，规划 §14.2)
                try:
                    speed = jitter_speed(getattr(active_role, "speech_speed", 1.0))
                    await self.tts_driver.apply_speech_speed(speed)
                except Exception:
                    pass

                # ---- 滑动窗口弹幕聚合 (规划 §6.2)：相似提问合并后集中答复 ----
                # 注意: Mock 注入器产生 "chat"，B站真实协议产生 "danmaku"，两者都需聚合
                if event.event_type in ("chat", "danmaku") and event.user_name != "运营管理员":
                    agg = await self.aggregator.add_message(event.user_name, event.payload.get("text", ""))
                    if agg is None:
                        # 窗口内已存在相似提问，静默合并等待集中答复
                        logger.debug(f"弹幕已聚合合并: {event.payload.get('text', '')}")
                        continue
                    if agg.get("is_aggregated"):
                        event.payload["text"] = agg["text"]
                        event.payload["aggregated_users"] = agg["users"]
                        event.payload["aggregated_count"] = agg["count"]
                        # 聚合答复时提示角色：这是多位观众的相同提问，需一次向全场集中答复
                        event.user_name = f"{agg['users'][0]}等{agg['count']}位观众"

                # 广播当前播报状态并在退出时严格 finally 复位
                await ws_manager.broadcast("speaking_state", {"is_speaking": True, "user": event.user_name})
                text_buffer = ""
                full_reply = ""
                is_first_sentence = True
                try:
                    stream_or_coro = active_role.process_event(
                        event.event_type,
                        event.user_name,
                        event.payload,
                        self.live_context
                    )
                    stream: Any = (
                        await stream_or_coro
                        if inspect.iscoroutine(stream_or_coro)
                        else stream_or_coro
                    )

                    # P0-1 前瞻预取：仅当已挂载批处理 Provider 时启用并发前瞻路径。
                    # 扩散模型单句渲染耗时通常超过音频时长，必须在「当前句仍在播报时」
                    # 就把下一句提前渲染好；串行 await 会让 LLM 流被播报阻塞，
                    # 根本不存在提前量。批处理路径不存在时保持原串行逻辑不变。
                    prefetcher = getattr(global_media_driver, "_batch_prefetcher", None)
                    if prefetcher is not None:
                        full_reply = await self._speak_with_lookahead(
                            stream, active_role, event
                        )
                    else:
                        async for raw_chunk in stream:
                            # 检查是否中途被更高优先级的 P0 打断
                            if self.event_queue.is_cancelled():
                                logger.info("当前播报已被更高优先级抢占打断")
                                break

                            text_buffer += raw_chunk
                            parts = sentence_delimiters.split(text_buffer)

                            # 优先研判是否满足断句输出条件 (标准标点断句 或 首句微块极速开嗓)
                            ready_sentence = None
                            if len(parts) >= 3:
                                ready_sentence = parts[0] + parts[1]
                                text_buffer = "".join(parts[2:])
                            elif is_first_sentence and len(text_buffer) >= 6:
                                # 首句微块切片机制：前 6~12 字快速寻找断句点，将首字延迟压缩至 300ms 内
                                for punc in ("，", "！", "？", " ", "、", "。"):
                                    if punc in text_buffer:
                                        idx = text_buffer.find(punc)
                                        if idx >= 3:  # 确保微块至少 4 字，避免单个字断句过碎
                                            ready_sentence = text_buffer[: idx + 1]
                                            text_buffer = text_buffer[idx + 1 :]
                                            break
                                if not ready_sentence and len(text_buffer) >= 12:
                                    ready_sentence = text_buffer[:10]
                                    text_buffer = text_buffer[10:]

                            if ready_sentence:
                                is_first_sentence = False
                                speak_text = await self._prepare_sentence(
                                    ready_sentence, active_role
                                )
                                if not speak_text:
                                    continue

                                full_reply += speak_text
                                await self._speak_sentence(speak_text, active_role)

                        # 处理缓冲区剩余的尾句
                        if text_buffer.strip() and not self.event_queue.is_cancelled():
                            speak_text = await self._prepare_sentence(
                                text_buffer, active_role
                            )
                            if speak_text:
                                full_reply += speak_text
                                await self._speak_sentence(speak_text, active_role)
                except Exception as llm_err:
                    logger.error("LLM 话术生成或流迭代异常: %s", llm_err, exc_info=True)
                    if self.execution_mode in ("PRODUCTION", "PILOT"):
                        await self.transition_to_ai_paused(f"LLM 话术生成失败: {llm_err}")
                finally:
                    # 无论正常结束、打断 break 还是异常，强制广播复位平息态
                    await ws_manager.broadcast("speaking_state", {"is_speaking": False})
                    # 动静分离核心：实时答复结束，驱动缝合器 Smoothstep 4 步缓动回切切片断点并重锚定时钟
                    if self.stitcher.state == StitcherState.STREAMING_REALTIME:
                        self.stitcher.finish_realtime_reply()

                # 维护多轮对话历史供 LLM 上下文引用
                if full_reply.strip():
                    q = event.payload.get("text") or event.payload.get("gift_name") or event.event_type
                    self.history.append({"role": "user", "content": f"{event.user_name}:{q}"})
                    self.history.append({"role": "assistant", "content": full_reply[:300]})

                # 记录该弹幕事件完成日志 (含 AI 回复与端到端响应延迟)
                if event and event.event_type != "idle_filler":
                    latency_ms = int((time.time() - event_started_at) * 1000)
                    self._track_task(self._log_barrage_complete(
                        event, full_reply, latency_ms, self.session_id
                    ))

                # 重置打断标记
                self.event_queue.reset_interrupt()

            except asyncio.CancelledError:
                if not self.is_live:
                    break
                logger.warning("直播消费循环收到打断或取消信号，但会话仍在进行中，自动保持消费主循环存活")
                await asyncio.sleep(0.05)
            except Exception as e:
                logger.error(f"直播主循环异常: {e}", exc_info=True)
                await asyncio.sleep(0.5)

    async def _prepare_sentence(self, sentence: str, active_role) -> str:
        """违禁词过滤 + 电商价格审计 + 拟人化；被熔断或空句返回 ""。"""
        complete_sentence = sentence
        speak_text, is_dropped, hits = await self._sanitize_and_humanize(
            complete_sentence, active_role
        )
        if hits:
            self._track_task(self._log_guardrail_hits(
                complete_sentence, speak_text, hits, is_dropped, self.session_id
            ))
        if is_dropped or not speak_text.strip():
            return ""
        return speak_text

    async def _speak_with_lookahead(self, stream, active_role, event) -> str:
        """批处理 Provider 前瞻播报：LLM 流与播报并发，提前渲染下一句。

        为什么必须并发：扩散模型 (LatentSync UNet3D) 单句渲染耗时通常超过音频时长。
        串行 `await _speak_sentence()` 会让 LLM 流被播报阻塞，下一句的文本在当前句
        播完之前根本不存在 —— 预取永远拿不到 ahead 时间，批处理路径必然音画失步。

        本实现把 LLM 流作为生产者协程，与播报消费者解耦：
            生产者：断句 -> 合规过滤 -> 入队 -> 立即为该句发起前瞻预渲染
            消费者：出队播报；命中预取缓存即零延迟上屏

        打断语义完全沿用既有 `event_queue.is_cancelled()`；预取与播报解耦后，
        被打断的句子由 `interrupt_and_clear()` 统一清空，绝不会播出旧画面。
        """
        sentence_queue: asyncio.Queue = asyncio.Queue(maxsize=2)
        SENTENCE_END = object()
        full_reply = ""
        loop = asyncio.get_running_loop()

        async def producer() -> None:
            """LLM 流生产者：断句、合规、入队，并对每句发起前瞻预渲染。"""
            nonlocal full_reply
            text_buffer = ""
            is_first_sentence = True
            try:
                async for raw_chunk in stream:
                    if self.event_queue.is_cancelled():
                        logger.info("当前播报已被更高优先级抢占打断")
                        break
                    text_buffer += raw_chunk
                    parts = _SENTENCE_DELIMITERS.split(text_buffer)
                    ready_sentence = None
                    if len(parts) >= 3:
                        ready_sentence = parts[0] + parts[1]
                        text_buffer = "".join(parts[2:])
                    elif is_first_sentence and len(text_buffer) >= 6:
                        for punc in ("，", "！", "？", " ", "、", "。"):
                            if punc in text_buffer:
                                idx = text_buffer.find(punc)
                                if idx >= 3:
                                    ready_sentence = text_buffer[: idx + 1]
                                    text_buffer = text_buffer[idx + 1 :]
                                    break
                        if not ready_sentence and len(text_buffer) >= 12:
                            ready_sentence = text_buffer[:10]
                            text_buffer = text_buffer[10:]

                    if ready_sentence:
                        is_first_sentence = False
                        speak_text = await self._prepare_sentence(
                            ready_sentence, active_role
                        )
                        if not speak_text:
                            continue
                        full_reply += speak_text
                        await sentence_queue.put(speak_text)
                        self._track_task(self._prefetch_sentence(speak_text))

                if text_buffer.strip() and not self.event_queue.is_cancelled():
                    speak_text = await self._prepare_sentence(text_buffer, active_role)
                    if speak_text:
                        full_reply += speak_text
                        await sentence_queue.put(speak_text)
                        self._track_task(self._prefetch_sentence(speak_text))
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("前瞻播报生产者异常，提前结束")
            finally:
                with_loop = loop.create_task(sentence_queue.put(SENTENCE_END))
                with_loop.add_done_callback(lambda t: t.exception() if not t.cancelled() else None)

        async def consumer() -> None:
            """播报消费者：按序出队播报，优先命中预取缓存。"""
            nonlocal full_reply
            while True:
                item = await sentence_queue.get()
                if item is SENTENCE_END:
                    return
                if self.event_queue.is_cancelled():
                    # 已被抢占：丢弃剩余句子，不再播报
                    continue
                await self._speak_sentence(item, active_role)

        producer_task = asyncio.create_task(producer())
        consumer_task = asyncio.create_task(consumer())
        try:
            await consumer_task
        finally:
            for task in (producer_task, consumer_task):
                if not task.done():
                    task.cancel()
            await asyncio.gather(producer_task, consumer_task, return_exceptions=True)
        return full_reply

    def _prefetch_sentence(self, speak_text: str):
        """把一句文本提前渲染成云端切片（TTS + 批处理），best-effort。

        走独立协程：预取是加速层，任何失败都不得阻塞或影响播报主链路。
        """

        async def _run() -> None:
            try:
                driver = self.tts_driver
                if driver is None:
                    return
                frames = await self._build_pcm_frames_for_prefetch(speak_text)
                if not frames:
                    return
                await global_media_driver.prefetch_upcoming_sentence(frames)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.debug("句级前瞻预取失败，保持本地 shadow", exc_info=True)

        return _run()

    async def _build_pcm_frames_for_prefetch(self, speak_text: str):
        """为预取合成标准 PCM 帧；不可用时返回 None（跳过预取而非报错）。"""
        try:
            driver = self.tts_driver
            if driver is None:
                return None
            synth_started = time.time()
            audio = await self._collect_tts_sentence(driver, speak_text)
            if not audio:
                return None
            from server.core.media.incremental_audio import global_incremental_audio_pipeline

            tx = global_incremental_audio_pipeline.open_transaction(
                sample_rate=int(getattr(driver, "audio_sample_rate", 24000)),
                channels=int(getattr(driver, "audio_channels", 1)),
                audio_id=f"pf_{self.session_id or 'local'}_{self._audio_generation}_{uuid.uuid4().hex[:8]}",
                audio_generation=self._audio_generation,
                session_generation=self._session_generation,
                text=speak_text,
            )
            tx.append(audio)
            tx.finish()
            frames = list(tx.drain_frames())
            if frames:
                global_av_sync.measure(synth_started)
            return frames
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.debug("预取音频合成失败，跳过该句预取", exc_info=True)
            return None

    async def _sanitize_and_humanize(self, sentence: str, active_role):
        """违禁词扫描平替 -> 电商价格防幻觉双重审计 -> 语音防机械感人类化 (规划 §14.2 / §14.3)"""
        current_platform = getattr(self, "platform", "all") or "all"
        sanitized_sentence, hits, is_dropped = global_guardrail.sanitize(
            sentence, current_role=active_role.role_type, current_platform=current_platform
        )
        if is_dropped or not sanitized_sentence.strip():
            return "", is_dropped, hits

        from server.routes.ws_live import ws_manager
        if hits:
            await ws_manager.broadcast("GUARDRAIL_TRIGGERED", {
                "original": sentence,
                "sanitized": sanitized_sentence,
                "hits": hits
            })

        # 电商带货模式：口播价格防幻觉双重审计
        if active_role.role_type == "ecommerce":
            from server.core.guardrails.price_auditor import global_price_auditor
            prods = self.live_context.get("products", [])
            sanitized_sentence, price_corrected, audit_info = global_price_auditor.audit_sentence(
                sanitized_sentence, all_products=prods
            )
            if price_corrected and audit_info:
                await ws_manager.broadcast("PRICE_AUDIT_WARNING", audit_info)

        speak_text = humanize_text(sanitized_sentence, active_role.role_type)
        return speak_text, False, hits

    async def _collect_tts_sentence(self, driver, text: str, pcm_transaction=None) -> bytes:
        """完整收集一句音频；PCM provider 同时增量帧化，但仅在成功后提交。"""
        chunks = []
        total_bytes = 0
        completed = False
        stream = driver.synthesize_stream(text)
        try:
            async for chunk in stream:
                if self.event_queue.is_cancelled():
                    return b""
                if chunk:
                    total_bytes += len(chunk)
                    if total_bytes > self.MAX_TTS_SENTENCE_BYTES:
                        raise RuntimeError(
                            f"TTS 整句编码音频超过 {self.MAX_TTS_SENTENCE_BYTES} bytes"
                        )
                    if pcm_transaction is not None:
                        pcm_transaction.append(chunk)
                    chunks.append(chunk)
            completed = True
            return b"".join(chunks)
        finally:
            if pcm_transaction is not None and not completed:
                pcm_transaction.abort("source_incomplete")
            close_stream = getattr(stream, "aclose", None)
            if close_stream is not None:
                await _call_async_compat(close_stream)

    async def _switch_to_edge_tts(self):
        """运行期 TTS 故障后原子卸载远程媒体源并切换 Edge 驱动。"""
        previous = self.tts_driver
        # 先取消远程帧优先级，避免驱动停止期间继续暴露旧画面。
        global_media_router.detach_remote()
        try:
            await _call_async_compat(previous.stop)
        except Exception:
            logger.exception("停止故障 TTS 驱动失败")
        fallback = EdgeTTSMediaDriver()
        await fallback.start()
        self.tts_driver = fallback
        return fallback

    def _supports_progressive_pcm_tts(self, driver) -> bool:
        codec = str(getattr(driver, "audio_codec", "") or "").lower()
        supports_pcm = codec in {"pcm_s16le", "pcm16", "s16le"}
        supports_renderer = bool(
            getattr(global_media_driver, "supports_progressive_audio", lambda: False)()
        )
        return supports_pcm and supports_renderer

    async def _speak_pcm_streaming_sentence(self, speak_text: str, active_role, driver) -> None:
        """CosyVoice PCM 流的边生成边播放路径。"""
        from server.routes.ws_live import ws_manager
        from server.core.media.incremental_audio import global_incremental_audio_pipeline
        from server.core.media.shared_playback_clock import global_shared_playback_clock

        audio_generation = self._audio_generation
        session_generation = self._session_generation
        audio_id = f"{self.session_id or 'local'}_{audio_generation}_{uuid.uuid4().hex[:8]}"
        wall_started = time.time()
        realtime_started = time.monotonic()
        sample_rate = int(getattr(driver, "audio_sample_rate", 24000))
        channels = int(getattr(driver, "audio_channels", 1))
        tx = global_incremental_audio_pipeline.open_transaction(
            sample_rate=sample_rate,
            channels=channels,
            audio_id=audio_id,
            audio_generation=audio_generation,
            session_generation=session_generation,
            text=speak_text,
        )
        prepare = getattr(global_virtual_audio, "prepare_playback", None)
        cursor_prepared = False
        if prepare is not None:
            cursor_prepared = _call_sync_compat(
                prepare,
                audio_id,
                fallback_sample_rate=sample_rate,
                audio_generation=audio_generation,
                session_generation=session_generation,
            ) is not False
            if not cursor_prepared:
                tx.abort("playback_cursor_rejected")
                return

        first_chunk = False
        first_frame = False
        first_pts_ms = int(global_shared_playback_clock.now_ms())
        action_applied = False

        def current() -> bool:
            return (
                not self.event_queue.is_cancelled()
                and audio_generation == self._audio_generation
                and session_generation == self._session_generation
            )

        async def emit_frames(frame_batch, *, is_final: bool) -> None:
            nonlocal first_frame, action_applied
            if not frame_batch or not current():
                return
            if not first_frame:
                global_av_sync.record_realtime_latency("audio_first_frame_ms", realtime_started)
                first_frame = True

            await _call_async_compat(
                global_media_driver.feed_audio_frames,
                frame_batch,
                is_final=is_final,
            )
            play_progressive = getattr(global_virtual_audio, "play_frames_progressive", None)
            if play_progressive is not None:
                _call_sync_compat(play_progressive, frame_batch, is_final=is_final)

            raw_pcm = b"".join(frame.data for frame in frame_batch)
            if not action_applied:
                try:
                    from server.core.avatar import get_active_avatar_driver, get_action_state_machine
                    avatar_driver = get_active_avatar_driver()
                    action_code = get_action_state_machine().evaluate_text(speak_text)
                    if avatar_driver and avatar_driver.is_active and action_code is not None:
                        await avatar_driver.set_custom_state(action_code, source="speech_text")
                    action_applied = True
                except Exception:
                    logger.debug("增量音频动作联动失败", exc_info=True)

            try:
                from server.core.avatar import get_active_avatar_driver
                avatar_driver = get_active_avatar_driver()
                if avatar_driver and avatar_driver.is_active:
                    await avatar_driver.push_audio_chunk(
                        raw_pcm,
                        {"sample_rate": sample_rate, "text": speak_text if not action_applied else ""},
                    )
            except Exception:
                logger.debug("增量音频数字人驱动联动失败", exc_info=True)

            try:
                from server.core.media.webrtc_streamer import get_webrtc_stream_manager
                get_webrtc_stream_manager().push_audio(raw_pcm, sample_rate=sample_rate)
            except Exception:
                pass
            try:
                from server.core.media.recorder import get_record_manager
                rec_mgr = get_record_manager()
                if rec_mgr and getattr(rec_mgr, "is_recording", lambda: False)():
                    rec_mgr.feed_audio(raw_pcm, sample_rate=sample_rate)
            except Exception:
                pass

            first = frame_batch[0]
            browser_audio = _pcm_s16le_to_wav(raw_pcm, sample_rate, channels)
            await ws_manager.broadcast("AUDIO_CHUNK", {
                "audio_base64": base64.b64encode(browser_audio).decode("utf-8"),
                "text": speak_text if first.sequence == 0 else "",
                "speaker": active_role.role_name,
                "codec": "wav",
                "source_codec": "pcm_s16le",
                "mime_type": "audio/wav",
                "sample_rate": sample_rate,
                "channels": channels,
                "audio_id": audio_id,
                "sequence": first.sequence,
                "is_first": first.is_first,
                "is_final": bool(is_final and frame_batch[-1].is_final),
                "audio_generation": audio_generation,
                "session_generation": session_generation,
                "delay_ms": int(max(0, min(300, global_av_sync.recommended_delay_ms))),
                "pts_ms": first_pts_ms + int(first.pts_ms),
                "pts_samples": int(first.pts_samples),
                "wallclock_ms": int(time.time() * 1000),
            })

        try:
            stream = driver.synthesize_stream(speak_text)
            async for chunk in stream:
                if not current():
                    tx.abort("generation_changed")
                    return
                if chunk:
                    if not first_chunk:
                        global_av_sync.record_realtime_latency("tts_first_chunk_ms", realtime_started)
                        first_chunk = True
                    tx.append(chunk)
                    await emit_frames(tx.drain_frames(), is_final=False)

            tx.finish()
            await emit_frames(tx.drain_frames(), is_final=True)
            if not first_chunk or not first_frame:
                raise RuntimeError("增量 PCM TTS 未产生有效音频帧")
            global_av_sync.measure(wall_started)
        except asyncio.CancelledError:
            if tx.state == "open":
                tx.abort("task_cancelled")
            raise
        except Exception:
            if tx.state == "open":
                tx.abort("stream_error")
            if cursor_prepared:
                reject = getattr(global_virtual_audio, "reject_playback", None)
                if reject is not None:
                    _call_sync_compat(reject, audio_id, reason="stream_error")
            logger.exception("增量 PCM TTS 提交失败，已丢弃当前句")

    async def _speak_sentence(self, speak_text: str, active_role):
        """完整合成后一次性提交音频；PCM 流式驱动在能力满足时走增量路径。"""
        driver = self.tts_driver
        if self._supports_progressive_pcm_tts(driver):
            stream_task = asyncio.create_task(
                self._speak_pcm_streaming_sentence(speak_text, active_role, driver)
            )
            self.current_tts_task = stream_task
            try:
                await stream_task
            finally:
                if self.current_tts_task is stream_task:
                    self.current_tts_task = None
            return

        from server.routes.ws_live import ws_manager

        synth_started = time.time()
        audio_generation = self._audio_generation
        session_generation = self._session_generation

        def is_current() -> bool:
            return (
                not self.event_queue.is_cancelled()
                and audio_generation == self._audio_generation
                and session_generation == self._session_generation
            )

        driver = self.tts_driver
        video_request_id = None
        audio_id = f"{self.session_id or 'local'}_{audio_generation}_{uuid.uuid4().hex[:8]}"
        staged_pcm_frames = None
        from server.core.media.incremental_audio import global_incremental_audio_pipeline

        def open_pcm_transaction(target_driver):
            try:
                parameters = inspect.signature(self._collect_tts_sentence).parameters
            except (TypeError, ValueError):
                return None
            if "pcm_transaction" not in parameters:
                return None
            target_codec = str(getattr(target_driver, "audio_codec", "mp3") or "").lower()
            if target_codec not in {"pcm_s16le", "pcm16", "s16le"}:
                return None
            return global_incremental_audio_pipeline.open_transaction(
                sample_rate=int(getattr(target_driver, "audio_sample_rate", 24000)),
                channels=int(getattr(target_driver, "audio_channels", 1)),
                audio_id=audio_id,
                audio_generation=audio_generation,
                session_generation=session_generation,
                text=speak_text,
            )

        def create_collect_task(target_driver, pcm_transaction):
            kwargs = {"pcm_transaction": pcm_transaction} if pcm_transaction is not None else {}
            return asyncio.create_task(self._collect_tts_sentence(target_driver, speak_text, **kwargs))

        # 创建独立的短生命周期 Task，绝不拿主循环自身 Worker 充当 current_tts_task
        pcm_transaction = open_pcm_transaction(driver)
        synth_task = create_collect_task(driver, pcm_transaction)
        self.current_tts_task = synth_task
        fallback_task = None
        try:
            full_audio = await synth_task
            # 紧邻生成器耗尽读取事务 ID，中间不 await，避免其他请求覆盖兼容游标。
            video_request_id = getattr(driver, "last_completed_request_id", None)
            if not is_current():
                if pcm_transaction is not None:
                    pcm_transaction.abort("generation_changed")
                return
            if not full_audio or len(full_audio) == 0:
                raise RuntimeError("TTS 返回空音频数据 (0 bytes)")
            if pcm_transaction is not None:
                staged_pcm_frames = pcm_transaction.finish()
        except asyncio.CancelledError:
            logger.info("单句 TTS 生成任务已被 P0 抢占打断取消")
            return
        except Exception as tts_err:
            if not is_current():
                return
            if self.execution_mode in ("PRODUCTION", "PILOT"):
                logger.error("【商业上线硬门禁】商业生产档位严禁静默切换未经授权的异音色！TTS 故障: %s，已进入 AI_PAUSED", tts_err)
                await self.transition_to_ai_paused(f"TTS 合成失败: {tts_err}")
                return
            if isinstance(driver, EdgeTTSMediaDriver):
                logger.warning("Edge-TTS 语音合成失败或返回零字节音频，丢弃该句以防爆音: %s", tts_err)
                return
            logger.warning("TTS 运行期合成失败，丢弃半句并切换 Edge-TTS: %s", tts_err)
            try:
                driver = await self._switch_to_edge_tts()
                if not is_current():
                    return
                staged_pcm_frames = None
                pcm_transaction = open_pcm_transaction(driver)
                fallback_task = create_collect_task(driver, pcm_transaction)
                self.current_tts_task = fallback_task
                try:
                    full_audio = await fallback_task
                except asyncio.CancelledError:
                    logger.info("降级 Edge-TTS 单句生成被抢占打断取消")
                    return
                video_request_id = getattr(driver, "last_completed_request_id", None)
                if not is_current() or not full_audio or len(full_audio) == 0:
                    return
                if pcm_transaction is not None:
                    staged_pcm_frames = pcm_transaction.finish()
            except Exception:
                logger.exception("Edge-TTS 运行期降级失败")
                return
        finally:
            if self.current_tts_task in (synth_task, fallback_task):
                self.current_tts_task = None
            if pcm_transaction is not None and pcm_transaction.state == "open":
                pcm_transaction.abort("controller_exit")

        global_av_sync.measure(synth_started)
        if not full_audio or len(full_audio) == 0 or not is_current():
            return

        codec = str(getattr(driver, "audio_codec", "mp3") or "mp3")
        mime_type = str(getattr(driver, "audio_mime_type", "audio/mpeg") or "audio/mpeg")
        sample_rate = int(getattr(driver, "audio_sample_rate", 24000))
        channels = int(getattr(driver, "audio_channels", 1))
        from server.core.media.shared_playback_clock import global_shared_playback_clock
        pts_ms = int(global_shared_playback_clock.now_ms())
        metadata = {
            "codec": codec,
            "sample_rate": sample_rate,
            "channels": channels,
            "audio_generation": audio_generation,
            "session_generation": session_generation,
            "audio_id": audio_id,
        }

        # 第一阶段保留整句事务屏障，成功后统一解码为有界 PCM 帧。解码不可用、
        # 测试替身或旧驱动仍走原字节路径，不改变 partial 丢弃和浏览器兼容契约。
        from server.core.media.audio_pipeline import FramedAudio, global_audio_frame_pipeline
        if staged_pcm_frames is not None:
            framed_audio = FramedAudio(
                frames=staged_pcm_frames,
                format=staged_pcm_frames[0].format,
                source_codec=codec,
                decode_ms=0.0,
            )
        else:
            framed_audio = await global_audio_frame_pipeline.frame_transaction(
                full_audio,
                source_codec=codec,
                sample_rate=sample_rate,
                channels=channels,
                audio_id=audio_id,
                audio_generation=audio_generation,
                session_generation=session_generation,
                text=speak_text,
            )
        if not is_current():
            return
        playback_sample_rate = framed_audio.format.sample_rate if framed_audio else sample_rate

        # 原子事务：先注册不播放的 cursor，再建立口型，最后在 generation fence 后提交音频。
        # 旧服务/测试替身没有 prepare/frame API 时保持原有兼容路径。
        prepare_playback = getattr(global_virtual_audio, "prepare_playback", None)
        if prepare_playback is None:
            prepare_playback = getattr(global_virtual_audio, "register_playback", None)
        cursor_prepared = False
        audio_commit_resolved = False
        if prepare_playback is not None:
            prepare_metadata = {key: value for key, value in metadata.items() if key != "audio_id"}
            prepare_result = _call_sync_compat(
                prepare_playback,
                audio_id,
                fallback_sample_rate=playback_sample_rate,
                **prepare_metadata,
            )
            # 旧 API 返回 None 继续视为成功；新 API 的 False 表示 cursor 已明确拒绝。
            cursor_prepared = prepare_result is not False
            if prepare_result is False:
                logger.info("播放 cursor 拒绝事务 audio_id=%s，停止媒体提交", audio_id)
                return
        if not is_current():
            reject_playback = getattr(global_virtual_audio, "reject_playback", None)
            if cursor_prepared and reject_playback is not None:
                _call_sync_compat(reject_playback, audio_id, reason="generation_changed_before_feed")
            return

        try:
            feed_frames = getattr(global_media_driver, "feed_audio_frames", None)
            if framed_audio is not None and feed_frames is not None:
                await _call_async_compat(feed_frames, framed_audio.frames)
            else:
                # 兼容驱动可能执行异步完整容器解码；它看到 audio_id 时 cursor 已存在且尚未开始。
                await _call_async_compat(
                    global_media_driver.feed_audio_chunk,
                    full_audio,
                    speak_text,
                    **metadata,
                )
            if not is_current():
                return

            play_frames = getattr(global_virtual_audio, "play_frames", None)
            if framed_audio is not None and play_frames is not None:
                play_result = _call_sync_compat(play_frames, framed_audio.frames)
            else:
                play_result = _call_sync_compat(
                    global_virtual_audio.play_chunk,
                    full_audio,
                    fallback_sample_rate=sample_rate,
                    **metadata,
                )
            # 返回 False 的新 API 已自行把 cursor 标为 rejected；浏览器通道仍作为显式软降级。
            # 旧 API 返回 None 继续视为已处理，保持替身兼容。
            audio_commit_resolved = True
            if play_result is False:
                logger.info("本地音频提交被拒绝，保留浏览器降级 audio_id=%s", audio_id)
            if not is_current():
                return
        finally:
            if cursor_prepared and not audio_commit_resolved:
                reject_playback = getattr(global_virtual_audio, "reject_playback", None)
                if reject_playback is not None:
                    _call_sync_compat(reject_playback, audio_id, reason="commit_aborted")

        # 远程视频绑定同一 audio_id；commit 本身只启动异步时间线，不阻塞事件循环。
        commit_video = getattr(driver, "commit_video_frames", None)
        if commit_video is not None:
            commit_kwargs = {
                "codec": codec,
                "sample_rate": sample_rate,
                "channels": channels,
                "audio_id": audio_id,
            }
            if video_request_id:
                commit_kwargs["request_id"] = video_request_id
            await _call_async_compat(commit_video, full_audio, **commit_kwargs)
            if not is_current():
                return

        # 联动数字人驱动音频推送与电商带货场景动作切片智能切换
        try:
            from server.core.avatar import get_active_avatar_driver, get_action_state_machine
            avatar_driver = get_active_avatar_driver()
            action_sm = get_action_state_machine()
            if avatar_driver and avatar_driver.is_active:
                action_code = action_sm.evaluate_text(speak_text)
                if action_code is None:
                    # 规则兜底研判
                    if any(kw in speak_text for kw in ("购物车", "下单", "左下角", "抢购", "手慢无", "买一送")):
                        action_code = 3  # 促单指引购物车
                    elif any(kw in speak_text for kw in ("欢迎", "来了", "刚进", "哈喽", "晚上好")):
                        action_code = 1  # 挥手欢迎
                    elif any(kw in speak_text for kw in ("点赞", "关注", "粉丝团", "灯牌")):
                        action_code = 2  # 求关注点赞
                    elif any(kw in speak_text for kw in ("感谢", "礼物", "破费", "大气")):
                        action_code = 4  # 致谢大礼
                    if action_code:
                        await avatar_driver.set_custom_state(action_code, source="speech_text")
                else:
                    await avatar_driver.set_custom_state(action_code, source="speech_text")

                raw_pcm = b""
                if codec in ("pcm_s16le", "pcm16", "s16le"):
                    raw_pcm = full_audio
                elif framed_audio and getattr(framed_audio, "frames", None):
                    raw_pcm = b"".join(getattr(f, "pcm_bytes", b"") for f in framed_audio.frames)
                elif full_audio:
                    # 容错保障：从容器音频 (如 WAV/MP3) 尝试解码为纯净标准 PCM16，杜绝容器头或压缩编码污染
                    try:
                        from server.core.media.audio_decode import decode_audio_to_float32
                        import numpy as np
                        samples, decoded_sr = decode_audio_to_float32(full_audio, fallback_sample_rate=sample_rate, codec=codec)
                        if samples is not None and len(samples) > 0:
                            raw_pcm = (np.clip(samples, -1.0, 1.0) * 32767.0).astype(np.int16).tobytes()
                            sample_rate = decoded_sr or sample_rate
                    except Exception:
                        pass

                if raw_pcm:
                    await avatar_driver.push_audio_chunk(raw_pcm, {"sample_rate": sample_rate, "text": speak_text})

                pcm_for_stream = raw_pcm if raw_pcm else (full_audio if codec in ("pcm_s16le", "pcm16", "s16le") else b"")
                if pcm_for_stream:
                    try:
                        from server.core.media.webrtc_streamer import get_webrtc_stream_manager
                        get_webrtc_stream_manager().push_audio(pcm_for_stream, sample_rate=sample_rate)
                    except Exception:
                        pass
                    try:
                        from server.core.media.recorder import get_record_manager
                        rec_mgr = get_record_manager()
                        if rec_mgr and getattr(rec_mgr, "is_recording", lambda: False)():
                            rec_mgr.feed_audio(pcm_for_stream, sample_rate=sample_rate)
                    except Exception:
                        pass
        except Exception:
            logger.debug("同步数字人驱动音频与动作异常", exc_info=True)

        browser_audio = full_audio
        browser_codec = codec
        browser_mime_type = mime_type
        if str(codec).lower() in {"pcm_s16le", "pcm16", "s16le"}:
            browser_audio = _pcm_s16le_to_wav(full_audio, sample_rate, channels)
            browser_codec = "wav"
            browser_mime_type = "audio/wav"

        await ws_manager.broadcast("AUDIO_CHUNK", {
            "audio_base64": base64.b64encode(browser_audio).decode("utf-8"),
            "text": speak_text,
            "speaker": active_role.role_name,
            "codec": browser_codec,
            "source_codec": codec,
            "mime_type": browser_mime_type,
            "sample_rate": sample_rate,
            "channels": channels,
            "audio_id": audio_id,
            "audio_generation": audio_generation,
            "session_generation": session_generation,
            # 音画同步补偿：将渲染耗时采样换算的音频前置延迟补偿量真实下发给前端播放器，
            # 使唇形画面与声波对齐（闭环 av_sync.AVSyncController 的规划 §15.1 补偿回路）
            "delay_ms": int(max(0, min(300, global_av_sync.recommended_delay_ms))),
            "pts_ms": pts_ms,
            # 采样时钟换算的时间戳：与云端 sidecar 的 pts_samples 同语义，供跨端对齐
            "pts_samples": int((pts_ms / 1000.0) * sample_rate),
            "wallclock_ms": int(time.time() * 1000),
        })

    async def _log_guardrail_hits(self, original: str, sanitized: str, hits: list, is_dropped: bool, session_id: Optional[str]):
        """后台异步写入违禁词拦截日志"""
        self.stats["guardrail_hits"] += len(hits)
        from server.database.db import AsyncSessionLocal
        try:
            async with AsyncSessionLocal() as session:
                for hit in hits:
                    log_item = ProhibitedWordLog(
                        id=f"log_{uuid.uuid4().hex[:8]}",
                        session_id=session_id or "session_default",
                        matched_word=hit["matched_word"],
                        category=hit["category"],
                        original_sentence=original,
                        processed_sentence=sanitized,
                        action_taken="drop" if is_dropped else hit["action"]
                    )
                    session.add(log_item)
                    # alert 命中按策略语义属于高危放行不可播报：整句已被拦截，
                    # 与 drop 的区别是它面向人工复核的严重等级，单独告警留痕
                    if is_dropped and hit["action"] == "alert":
                        logger.warning(
                            "⚠️ 高危告警词命中，整句已拦截禁止播报 (人工复核): session=%s word=%s category=%s sentence=%s",
                            session_id or "session_default", hit["matched_word"], hit["category"], original[:200]
                        )
                await session.commit()
        except Exception:
            logger.exception("写入违禁词审计日志失败")

    async def _log_barrage_complete(self, event, ai_reply: str = "", latency_ms: int = 0, session_id: Optional[str] = None):
        """后台异步记录弹幕交互完成历史 (含 AI 回复与响应延迟)"""
        from server.database.db import AsyncSessionLocal
        try:
            async with AsyncSessionLocal() as session:
                barrage_log = BarrageLog(
                    id=f"b_{uuid.uuid4().hex[:8]}",
                    session_id=session_id or "session_default",
                    platform=event.payload.get("platform") or getattr(self, "platform", "bilibili"),
                    user_id=event.user_name,
                    user_nickname=event.user_name,
                    raw_message=event.payload.get("text") or event.payload.get("gift_name") or event.event_type,
                    event_type=event.event_type,
                    priority_level=event.priority,
                    ai_reply_text=(ai_reply or "")[:500],
                    response_latency_ms=latency_ms,
                    is_interrupted=1 if self.event_queue.is_interrupted_flag else 0
                )
                session.add(barrage_log)
                await session.commit()
        except Exception:
            logger.exception("写入弹幕审计日志失败")

global_live_controller = LiveSessionController()

def _on_obs_external_state_change(active: bool, state: str, epoch: Optional[int] = None):
    """缝：OBS 外部推流状态回调（实现见 server/routes/live_obs.py）。"""
    from server.routes.live_obs import on_obs_external_state_change

    return on_obs_external_state_change(active, state, epoch)


global_obs_client.add_stream_state_listener(_on_obs_external_state_change)

class LiveStartRequest(BaseModel):
    room_url: Optional[str] = ""
    room_id: Optional[str] = None
    role_id: Optional[str] = None
    anchor_id: Optional[str] = None
    voice_id: Optional[str] = None
    platform: Optional[str] = "bilibili"
    demo_mode: Optional[bool] = False  # 是否为明确的演示模式 (允许使用 mock 弹幕)
    obs_auto_link: Optional[bool] = False  # 是否联动 OBS 同步开启推流
    execution_mode: Optional[str] = None  # 运行档位: DEMO / PREVIEW / PILOT / PRODUCTION (未指定时读取 LIVE_AGENT_MODE 或默认为 PREVIEW)

class LiveInterruptRequest(BaseModel):
    text: str


def _build_external_publish_status() -> dict:
    """
    统一构建外部推流状态对象 (三层解耦)：
    local_source: 本地直播源/虚拟设备
    obs: OBS-WebSocket 状态与推流指标
    platform: 平台公开开播状态 (未接入平台 API 前诚实保持 null，绝不过度宣称)
    """
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
        "external_publish": _build_external_publish_status(),
    }


# ---------------------------------------------------------------------------
# 兼容再导出：开播门禁体检已迁至 server/routes/live_preflight.py。
# 端点经子路由挂载，`server.routes.live.get_preflight_status` 仍可被直接导入。
# ---------------------------------------------------------------------------
from server.routes.live_preflight import (  # noqa: E402
    get_preflight_status,  # noqa: F401  兼容再导出：外部仍可 from server.routes.live import get_preflight_status
    router as _preflight_router,
)

router.include_router(_preflight_router)


async def _start_live_unlocked(req: LiveStartRequest, db: AsyncSession):
    """在生命周期锁内创建持久化场次并启动本地直播源。"""
    from server.database.models import Anchor, AnchorRole, LiveSessionRecord, VoiceProfile, utc_now
    from server.routes.settings import _get_setting, SETTING_KEY_LIVE_MODE

    if global_live_controller.is_live:
        raise HTTPException(status_code=409, detail="直播已在进行中，请勿重复开播；如需重开请先停止当前直播")

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

    target_voice_id: Optional[str] = req.voice_id or (str(anchor_row.voice_id) if anchor_row and anchor_row.voice_id else None)
    if target_voice_id and not await db.get(VoiceProfile, target_voice_id):
        raise HTTPException(status_code=400, detail=f"指定的音色不存在: {target_voice_id}")

    rows = (await db.execute(select(Product).where(Product.is_active == 1))).scalars().all()
    from server.routes.products import product_to_context
    products = [product_to_context(product) for product in rows]

    target_room = (req.room_id or req.room_url or "").strip()
    session_id = f"sess_{uuid.uuid4().hex[:12]}"
    platform = (req.platform or "bilibili").strip()
    platform_lower = platform.lower()
    is_demo = bool(getattr(req, "demo_mode", False) or platform_lower in ("mock", "demo") or target_room.lower() in ("room_demo", "mock"))
    raw_mode = (getattr(req, "execution_mode", None) or os.getenv("LIVE_AGENT_MODE") or "").strip()
    if not raw_mode:
        raw_mode = "DEMO" if is_demo else "PREVIEW"
    execution_mode = raw_mode.upper()

    # 正式直播平台严禁空房间静默进入仿真模式，必须拦截并校验
    if platform_lower in ("bilibili", "douyin", "kuaishou", "wechat") and not is_demo:
        if not target_room:
            raise HTTPException(
                status_code=400,
                detail=f"【{platform}】正式直播必须提供有效的房间号或直播链接；如需测试仿真互动，请选择 mock 模式或开启 demo_mode"
            )

    live_mode = await _get_setting(db, SETTING_KEY_LIVE_MODE) or "B"
    live_theme = await _get_setting(db, "live_theme") or ""
    avatar_path: str = str(anchor_row.photo_portrait) if anchor_row and anchor_row.photo_portrait else ""
    landmarks_path = await asyncio.to_thread(_resolve_landmarks_for_avatar, avatar_path) if avatar_path else ""

    # 挂接数字人资产与双分支算力驱动器 (本地高性能显卡 vs 远端租赁GPU)
    avatar_asset_dir = str(getattr(anchor_row, "avatar_asset_dir", "") or "") if anchor_row else ""
    has_video_avatar = bool(avatar_asset_dir and Path(avatar_asset_dir).exists())
    if has_video_avatar:
        try:
            from server.core.hardware.gpu_capability import evaluate_compute
            from server.core.avatar import set_active_avatar_driver, LocalLiveTalkingDriver, CloudSidecarDriver
            compute_plan = await evaluate_compute(feature_name="开播数字人实时驱动", required_vram_gb=2.0)
            if compute_plan.use_cloud and compute_plan.can_execute and compute_plan.cloud_gpu:
                cloud_cfg = compute_plan.cloud_gpu
                logger.info(f"开播数字人启用【分支 2: 远端租赁GPU】，Sidecar: {cloud_cfg.get('provider_name', '云节点')}")
                sidecar_driver = CloudSidecarDriver(config={
                    "sidecar_url": cloud_cfg.get("base_url", ""),
                    "api_key": cloud_cfg.get("api_key", ""),
                    "avatar_asset_dir": avatar_asset_dir,
                    "anchor_id": req.anchor_id,
                })
                await sidecar_driver.start()
                set_active_avatar_driver(sidecar_driver)
            elif not compute_plan.use_cloud and compute_plan.can_execute:
                logger.info(f"开播数字人启用【分支 1: 本地高性能显卡】，资产: {avatar_asset_dir}")
                local_driver = LocalLiveTalkingDriver(config={
                    "avatar_asset_dir": avatar_asset_dir,
                    "anchor_id": req.anchor_id,
                })
                await local_driver.start()
                set_active_avatar_driver(local_driver)
        except Exception as e:
            logger.warning(f"挂载切片数字人驱动异常，保持默认驱动: {e}")

    # 神经唇形算力预检：把「本地 CPU 跑不到 25fps」这个结构性缺陷前移到开播前告知，
    # 而不是让用户开播后自己撞上持续掉帧。实测优先（跑真实推理测延迟），不估算。
    # 只告知不阻断 —— 是否开播是产品决策，算力模块不越权。
    lipsync_preflight: Dict[str, Any] = {}
    asset_health: Dict[str, Any] = {}
    if has_video_avatar:
        # 先查资产健康：人脸切片里存在无人脸帧时，唇形必然是噪声，
        # 此时再谈算力没有意义。
        try:
            from server.core.avatar.asset_health import check_and_enforce, AssetUnusableError

            asset_health_obj = check_and_enforce(avatar_asset_dir)
            asset_health = asset_health_obj.to_dict()
            logger.info(
                "开播资产健康: %s (判定来源: %s)",
                asset_health.get("status"), asset_health.get("source"),
            )
        except AssetUnusableError as e:
            logger.error("数字人资产严重损坏，阻断开播: %s", e)
            raise HTTPException(
                status_code=400,
                detail=str(e),
            )
        except Exception as e:
            logger.warning("资产健康检查异常: %s", e)
            asset_health = {"status": "unknown", "reason": str(e)}

        try:
            from server.core.avatar.lipsync_preflight import probe_async

            _probe_renderer = None
            if not compute_plan.use_cloud:
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

    # 统一全栈生产开播门禁检查 (依据《商业化运营改造方案》v3.0.0 第四章)
    from server.core.operations.preflight import global_preflight_service
    preflight_report = await global_preflight_service.run_preflight(
        mode=execution_mode,
        avatar_asset_dir=avatar_asset_dir if has_video_avatar else None,
        voice_id=target_voice_id,
        force=True,
    )
    if not preflight_report.passed:
        logger.error(
            "统一生产开播门禁未通过，已强制阻断: mode=%s, reasons=%s",
            execution_mode, preflight_report.blocking_reasons,
        )
        raise HTTPException(
            status_code=400,
            detail={
                "code": "PREFLIGHT_BLOCKED",
                "message": f"当前环境未达到 {execution_mode} 档位生产准入标准，已强制阻断开播",
                "blocking_reasons": preflight_report.blocking_reasons,
                "report": preflight_report.to_dict(),
            },
        )

    # 灰度与 Kill Switch 开关校验 (依据《商业化运营改造方案》§10 与 §12 P1)
    from server.core.compliance.kill_switch import global_kill_switch
    if not global_kill_switch.is_feature_enabled("stream_push", platform=platform, anchor_id=req.anchor_id):
        logger.critical(
            "🚨【Kill Switch 熔断阻断开播】主播 %s 在平台 %s 的推流权限已被熔断切断！",
            req.anchor_id, platform,
        )
        raise HTTPException(
            status_code=403,
            detail={
                "code": "KILL_SWITCH_BLOCKED",
                "message": f"当前主播或平台 ({platform}) 已被 Kill Switch 熔断禁用开播与推流",
            },
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


        # 若配置了 OBS 联动自动开播，且 OBS 已连接，触发 OBS 推流并记录所有权
        obs_linked = False
        obs_degraded = False
        obs_error_msg = None
        if getattr(req, "obs_auto_link", False):
            if not global_obs_client.is_connected:
                obs_degraded = True
                obs_error_msg = "OBS 未连接，无法自动联动推流"
                logger.warning(obs_error_msg)
            else:
                try:
                    res = await global_obs_client.start_stream()
                    if res.get("result"):
                        obs_linked = True
                        if not res.get("already_streaming"):
                            global_live_controller.obs_owner_session_id = session_id
                            global_live_controller.obs_owner_connection_epoch = global_obs_client.connection_epoch
                        else:
                            logger.info("OBS 此前已在推流中，保持现有推流所有权不变，停播时不主动切断")
                    else:
                        obs_degraded = True
                        obs_error_msg = res.get("comment") or res.get("error") or "OBS 拒绝推流"
                        logger.warning("联动 OBS 开始推流失败: %s", obs_error_msg)
                except Exception as e:
                    obs_degraded = True
                    obs_error_msg = str(e)
                    logger.warning("联动 OBS 开始推流异常: %s", e)

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
        logger.warning(f"启动自动场次录像异常: {rec_err}")

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
        "degraded": obs_degraded,
        "obs_error": obs_error_msg,
        "local_source": {"status": "running", "preview": "/api/v1/live/stream/preview"},
        "external_publish": _build_external_publish_status(),
        "preflight_report": preflight_report.to_dict(),
        "lipsync_preflight": lipsync_preflight,
        "avatar_asset_health": asset_health,
        "message": f"直播源已成功启动 ({platform}, 模式 {live_mode}, 档位 {execution_mode}, 状态 {global_live_controller.state_detail})"
    }



@router.post("/start")
async def start_live(req: LiveStartRequest, db: AsyncSession = Depends(get_db)):
    """用同一把生命周期锁包裹完整开播状态转换。"""
    async with global_live_controller.lifecycle_lock:
        return await _start_live_unlocked(req, db)


async def _stop_live_unlocked(db: AsyncSession):
    """在生命周期锁内停止直播源并持久化最终场次指标。"""
    from server.database.models import LiveSessionRecord, Order, utc_now

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
        # 联动 OBS：仅当 session 和 connection epoch 所有权仍匹配时停止 OBS。
        try:
            stop_epoch = getattr(global_obs_client, "connection_epoch", None)
            if not isinstance(stop_epoch, int):
                stop_epoch = owner_epoch
            owner_still_current = bool(
                should_stop_obs
                and owner_session_id == sid
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

            # 联动停止内置 RTMP 直推引擎 (如果处于推流中)
            try:
                from server.core.media.rtmp_streamer import global_rtmp_streamer
                if global_rtmp_streamer.is_streaming:
                    global_rtmp_streamer.stop()
            except Exception as e:
                logger.warning("联动停止 RTMP 直推异常: %s", e)

        if sid:
            try:
                record = await db.get(LiveSessionRecord, sid)
                if record:
                    setattr(record, "end_time", utc_now())
                    setattr(record, "status", "stopped" if not cleanup_errors else "stopped_with_errors")
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

                    # 停止自动场次录像并联动阿里云 OSS 异步归档
                    try:
                        from server.core.media.recorder import get_record_manager
                        rec_mgr = get_record_manager()
                        if rec_mgr.is_recording():
                            rec_info = rec_mgr.stop_recording()
                            if rec_info.get("code") == 0 and rec_info.get("data"):
                                data = rec_info["data"]
                                vid_p = data.get("video_path", "")
                                prev_p = data.get("preview_path", "")
                                dur_sec = data.get("duration_sec", 0.0)
                                f_size = data.get("file_size_bytes", 0)
                                setattr(record, "video_path", vid_p)
                                setattr(record, "preview_image_url", prev_p)
                                setattr(record, "video_duration_sec", dur_sec)
                                setattr(record, "video_size_bytes", f_size)
                                # 异步启动 OSS 上传任务
                                from server.core.media.oss_uploader import global_oss_uploader
                                asyncio.create_task(
                                    global_oss_uploader.upload_session_recording_async(
                                        session_id=sid,
                                        video_path=vid_p,
                                        preview_path=prev_p,
                                    )
                                )
                    except Exception as rec_err:
                        logger.warning(f"结束场次录制与 OSS 归档处理异常: {rec_err}")

                    await db.commit()
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
    async with global_live_controller.lifecycle_lock:
        return await _stop_live_unlocked(db)


@router.get("/media/status")
async def get_media_status():
    """获取媒体状态；任一子组件故障时保留其余诊断信息。"""
    from server.core.media.audio_pipeline import global_audio_frame_pipeline
    from server.core.media.incremental_audio import global_incremental_audio_pipeline

    data = _safe_media_status(
        "media_router",
        global_media_router.get_preview_status,
        {"driver_type": "unknown", "is_running": False},
    )
    data["audio_pipeline"] = _safe_media_status(
        "audio_pipeline", global_audio_frame_pipeline.get_status
    )
    data["incremental_audio"] = _safe_media_status(
        "incremental_audio", global_incremental_audio_pipeline.get_status
    )
    data["virtual_audio"] = _safe_media_status(
        "virtual_audio", global_virtual_audio.get_status
    )
    vision = _safe_media_status("vision", global_vision.get_status)
    vision["active_frame"] = bool(global_live_controller.live_context.get("vision_image_b64"))
    data["vision"] = vision
    data["av_sync"] = _safe_media_status("av_sync", global_av_sync.get_status)
    from server.core.monitoring.system_resource_watchdog import global_resource_watchdog
    data["resource_watchdog"] = global_resource_watchdog.get_status()
    return {
        "code": 0,
        "data": data,
    }

# ---------------------------------------------------------------------------
# 兼容再导出：弹幕接入与场控已迁至 server/routes/live_danmaku.py，端点经子路由挂载。
# ---------------------------------------------------------------------------
from server.routes.live_danmaku import router as _danmaku_router  # noqa: E402

router.include_router(_danmaku_router)



@router.post("/interrupt")
@router.post("/manual-speech")
async def interrupt_live(req: LiveInterruptRequest):
    """运营人员紧急人工插话（双路由对齐，触发毫秒级打断并播报新内容）"""
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


class LiveTakeoverRequest(BaseModel):
    action: str = "takeover"  # takeover / release
    operator: Optional[str] = "admin"
    reason: Optional[str] = "人工控制台接管"


@router.post("/takeover")
async def takeover_live(req: LiveTakeoverRequest):
    """
    人工接管直播间 (依据《商业化运营改造方案》§6.3):
    - action="takeover": 立即原子打断 AI/TTS，状态切换为 HUMAN_TAKEOVER
    - action="release": 释放接管，恢复正常直播
    """
    if not global_live_controller.is_live:
        raise HTTPException(status_code=400, detail="直播尚未开播，无法接管")
    if req.action == "takeover":
        res = await global_live_controller.takeover(operator=req.operator or "admin", reason=req.reason or "")
        return {"code": 0, "message": "人工接管已生效，AI 与语音播报已原子抢占", "data": res}
    elif req.action == "release":
        res = await global_live_controller.release_takeover(operator=req.operator or "admin")
        return {"code": 0, "message": "人工接管已释放，恢复正常直播状态", "data": res}
    else:
        raise HTTPException(status_code=400, detail=f"不支持的接管动作: {req.action}")


@router.post("/pause-ai")
async def pause_ai_live(reason: str = "运营手动暂停"):
    """手动暂停 AI 生成进入 AI_PAUSED 安全态"""
    if not global_live_controller.is_live:
        raise HTTPException(status_code=400, detail="直播未开播")
    await global_live_controller.transition_to_ai_paused(reason)
    return {"code": 0, "message": "AI 已暂停", "state_detail": global_live_controller.state_detail}


@router.post("/resume-ai")
async def resume_ai_live():
    """从暂停或降级态恢复 AI 生成"""
    if not global_live_controller.is_live:
        raise HTTPException(status_code=400, detail="直播未开播")
    result = await global_live_controller.resume_ai()
    if not result.get("ok"):
        return {"code": 409, "message": "AI 恢复被重新预检阻断", "data": result}
    return {"code": 0, "message": "AI 已恢复", "data": result, "state_detail": global_live_controller.state_detail}


@router.post("/voice-interrupt")
async def voice_interrupt_live(
    file: UploadFile = File(...),
    auto_reply: bool = Form(True)
):
    """
    麦克风实时语音打断与人工插话 (解决 7.4-7 无 ASR/VAD 语音输入通道短板)
    上传麦克风录音切片，自动调用 ASR 引擎识别文本；
    若识别出有效内容，立即触发 P0 级打断当前主播，并作为高优先级事件抢占播报。
    """
    if not global_live_controller.is_live:
        raise HTTPException(status_code=400, detail="直播尚未开播，禁止语音插播")

    content = await file.read()
    if not content:
        raise HTTPException(status_code=400, detail="上传的语音内容为空")

    from server.core.audio.asr_engine import transcribe_audio_bytes
    text = transcribe_audio_bytes(content)
    if not text.strip():
        text = "收到现场麦克风紧急语音插话"

    res = await global_live_controller.ingest_event(
        event_type="chat",
        user_name="现场麦克风",
        payload={"text": text, "from_voice": True},
        priority=0,  # P0 触发毫秒级强打断
        source="voice_interrupt",
        is_mock=False,
    )
    return {
        "code": 0,
        "message": f"麦克风语音识别成功: '{text}'，已触发 P0 强打断抢占播报",
        "transcribed_text": text,
        "data": res
    }


class OverlayCouponRequest(BaseModel):
    sku: str = ""
    title: str = "直播间专属优惠券"
    amount: str = "立减 30 元"
    desc: str = "点击左下角领券立减"
    seconds: int = 20


@router.post("/overlay/coupon")
async def trigger_overlay_coupon(req: OverlayCouponRequest):
    """一键触发画中画优惠券画层 (方案 4.2)"""
    from server.core.tools.tool_bus import trigger_pip_coupon
    res = await trigger_pip_coupon(
        sku=req.sku,
        title=req.title,
        amount=req.amount,
        desc=req.desc,
        seconds=req.seconds,
    )
    return {"code": 0, "message": "画中画优惠券已触发", "data": res}


class OverlayUrgencyRequest(BaseModel):
    sku: str = ""
    title: str = "🔥 限量抢购 倒计时"
    desc: str = "库存告急，先到先得！"
    seconds: int = 60


@router.post("/overlay/urgency")
async def trigger_overlay_urgency(req: OverlayUrgencyRequest):
    """一键触发逼单倒计时画层 (方案 4.2)"""
    from server.core.tools.tool_bus import trigger_urgency_countdown
    res = await trigger_urgency_countdown(
        sku=req.sku,
        title=req.title,
        seconds=req.seconds,
        desc=req.desc,
    )
    return {"code": 0, "message": "逼单倒计时已触发", "data": res}


# ---------------------------------------------------------------------------
# 兼容再导出：以下符号被其他生产模块与测试直接导入 / patch（缝），不可移除。
# 转发体必须薄，且在**函数体内**解析目标 —— 顶层 import 会把缝变成早绑定，
# 使 patch("server.routes.live.<X>") 静默失效（测试绿而线上行为已变）。
# 守卫见 server/tests/test_live_seam_late_binding.py。
# ---------------------------------------------------------------------------
from server.routes.live_hardware import (  # noqa: E402
    _resolve_landmarks_for_avatar,
    router as _hardware_router,
)


def _probe_gpu() -> dict:
    """缝：本机 GPU 委托探测（单一事实源 gpu_capability）。"""
    from server.routes.live_hardware import probe_gpu

    return probe_gpu()


def _recommend_tier(gpu_info: dict) -> str:
    """缝：按硬件推荐运行模式。"""
    from server.routes.live_hardware import recommend_tier

    return recommend_tier(gpu_info)


async def _hardware_payload() -> dict:
    """缝：硬件负载载荷（含 TTL 缓存）。"""
    from server.routes.live_hardware import hardware_payload

    return await hardware_payload()


router.include_router(_hardware_router)


class OrderRequest(BaseModel):
    amount: float = Field(gt=0, le=100_000_000)
    sku: str = Field(min_length=1, max_length=64)
    quantity: int = Field(default=1, gt=0, le=10_000)
    external_id: Optional[str] = Field(default=None, max_length=128)
    note: Optional[str] = Field(default="", max_length=2000)


class RefundRequest(BaseModel):
    amount: Optional[float] = Field(default=None, gt=0, le=100_000_000)


def _order_payload(order) -> dict:
    return {
        "id": order.id,
        "external_id": order.external_id,
        "session_id": order.session_id,
        "product_id": order.product_id,
        "sku": order.sku,
        "amount": order.amount,
        "quantity": order.quantity,
        "status": order.status,
        "refund_amount": order.refund_amount,
        "note": order.note or "",
        "created_at": order.created_at.isoformat() if order.created_at else None,
    }


async def _refresh_order_stats(db: AsyncSession, session_id: Any) -> tuple[float, int]:
    from server.database.models import Order
    sid = str(session_id or "")
    result = await db.execute(
        select(func.coalesce(func.sum(Order.amount), 0.0), func.count(Order.id)).where(
            Order.session_id == sid, Order.status == "completed"
        )
    )
    gmv, count = result.one()
    aggregate = (round(float(gmv or 0.0), 2), int(count or 0))
    if sid == global_live_controller.session_id:
        global_live_controller.stats["gmv_yuan"] = aggregate[0]
        global_live_controller.stats["orders_count"] = aggregate[1]
    return aggregate


@router.post("/stats/order")
async def register_order(req: OrderRequest, db: AsyncSession = Depends(get_db)):
    """幂等登记成交，库存条件更新与订单/流水在同一事务提交。"""
    from server.database.models import InventoryMovement, Order

    if not global_live_controller.is_live or not global_live_controller.session_id:
        raise HTTPException(status_code=400, detail="直播间尚未开播")

    if global_live_controller.execution_mode == "DEMO":
        raise HTTPException(status_code=403, detail="【商业准入安全拦截】DEMO 演示模式严禁登记真实交易订单与扣减库存")

    external_id = (req.external_id or f"manual-{uuid.uuid4().hex}").strip()
    existing = (await db.execute(select(Order).where(Order.external_id == external_id))).scalar_one_or_none()
    if existing:
        gmv_yuan, orders_count = await _refresh_order_stats(db, str(existing.session_id))
        product = await db.get(Product, existing.product_id) if existing.product_id else None
        return {
            "code": 0,
            "message": "重复请求已按幂等订单返回",
            "data": {
                "order_id": existing.id,
                "gmv_yuan": gmv_yuan,
                "orders_count": orders_count,
                "remaining_stock": product.current_stock if product else None,
                "idempotent_replay": True,
            },
        }

    sku = req.sku.strip()
    product = (await db.execute(select(Product).where(Product.sku_code == sku, Product.is_active == 1))).scalar_one_or_none()
    if not product:
        raise HTTPException(status_code=404, detail=f"商品 SKU 不存在或已下架: {sku}")

    result = await db.execute(
        update(Product)
        .where(Product.id == product.id, Product.current_stock >= req.quantity)
        .values(current_stock=Product.current_stock - req.quantity)
    )
    if getattr(result, "rowcount", 0) != 1:
        await db.rollback()
        raise HTTPException(status_code=409, detail="商品库存不足")

    snapshot = {
        "id": product.id,
        "sku": product.sku_code,
        "title": product.title,
        "unit_live_price": product.live_price,
    }
    order = Order(
        id=f"ord_{uuid.uuid4().hex[:12]}",
        external_id=external_id,
        session_id=global_live_controller.session_id,
        product_id=product.id,
        sku=sku,
        amount=req.amount,
        quantity=req.quantity,
        status="completed",
        note=req.note or "",
        product_snapshot_json=json.dumps(snapshot, ensure_ascii=False),
    )
    db.add(order)
    await db.flush()
    db.add(InventoryMovement(
        id=f"mov_{uuid.uuid4().hex[:12]}", order_id=order.id,
        product_id=product.id, quantity_delta=-req.quantity, reason="sale"
    ))
    await db.commit()
    await db.refresh(product)
    gmv_yuan, orders_count = await _refresh_order_stats(db, str(order.session_id))
    return {
        "code": 0,
        "message": f"成交已登记并持久化：+{req.amount} 元",
        "data": {
            "order_id": order.id,
            "gmv_yuan": gmv_yuan,
            "orders_count": orders_count,
            "remaining_stock": product.current_stock,
            "idempotent_replay": False,
        },
    }


@router.get("/orders")
async def list_orders(session_id: Optional[str] = None, db: AsyncSession = Depends(get_db)):
    from server.database.models import Order
    query = select(Order).order_by(Order.created_at.desc())
    if session_id:
        query = query.where(Order.session_id == session_id)
    rows = (await db.execute(query.limit(500))).scalars().all()
    return {"code": 0, "total": len(rows), "data": [_order_payload(row) for row in rows]}


@router.post("/orders/{order_id}/cancel")
async def cancel_order(order_id: str, db: AsyncSession = Depends(get_db)):
    from server.database.models import InventoryMovement, Order
    order = await db.get(Order, order_id)
    if not order:
        raise HTTPException(status_code=404, detail="订单不存在")
    if order.status != "completed":
        return {"code": 0, "message": "订单已处理", "data": _order_payload(order)}
    result = await db.execute(
        update(Order).where(Order.id == order_id, Order.status == "completed").values(status="cancelled")
    )
    if getattr(result, "rowcount", 0) != 1:
        await db.rollback()
        raise HTTPException(status_code=409, detail="订单状态已变化，请刷新后重试")
    await db.execute(
        update(Product).where(Product.id == order.product_id).values(current_stock=Product.current_stock + order.quantity)
    )
    db.add(InventoryMovement(
        id=f"mov_{uuid.uuid4().hex[:12]}", order_id=order.id,
        product_id=order.product_id, quantity_delta=order.quantity, reason="cancel"
    ))
    await db.commit()
    await db.refresh(order)
    await _refresh_order_stats(db, str(order.session_id))
    return {"code": 0, "message": "订单已取消并回补库存", "data": _order_payload(order)}


@router.post("/orders/{order_id}/refund")
async def refund_order(order_id: str, req: RefundRequest, db: AsyncSession = Depends(get_db)):
    from server.database.models import InventoryMovement, Order
    order = await db.get(Order, order_id)
    if not order:
        raise HTTPException(status_code=404, detail="订单不存在")
    if order.status != "completed":
        raise HTTPException(status_code=409, detail="当前订单状态不可退款")
    refund_amount = req.amount if req.amount is not None else order.amount
    if abs(refund_amount - order.amount) > 0.005:
        raise HTTPException(status_code=400, detail="当前版本仅支持整单退款")
    result = await db.execute(
        update(Order)
        .where(Order.id == order_id, Order.status == "completed")
        .values(status="refunded", refund_amount=refund_amount)
        .execution_options(synchronize_session=False)
    )
    if getattr(result, "rowcount", 0) != 1:
        await db.rollback()
        raise HTTPException(status_code=409, detail="订单状态已变化，请刷新后重试")
    await db.execute(
        update(Product).where(Product.id == order.product_id).values(current_stock=Product.current_stock + order.quantity)
    )
    db.add(InventoryMovement(
        id=f"mov_{uuid.uuid4().hex[:12]}", order_id=order.id,
        product_id=order.product_id, quantity_delta=order.quantity, reason="refund"
    ))
    await db.commit()
    await db.refresh(order)
    await _refresh_order_stats(db, str(order.session_id))
    return {"code": 0, "message": "订单已退款并回补库存", "data": _order_payload(order)}


@router.get("/sessions")
async def list_sessions(db: AsyncSession = Depends(get_db)):
    from server.database.models import LiveSessionRecord
    rows = (await db.execute(select(LiveSessionRecord).order_by(LiveSessionRecord.start_time.desc()).limit(200))).scalars().all()
    data = [{
        "session_id": row.session_id,
        "platform": row.platform,
        "theme": row.theme,
        "mode": row.mode,
        "role_id": row.role_id,
        "voice_id": row.voice_id,
        "anchor_id": row.anchor_id,
        "status": row.status,
        "start_time": row.start_time.isoformat() if row.start_time else None,
        "end_time": row.end_time.isoformat() if row.end_time else None,
        "total_gmv": row.total_gmv,
        "orders_count": row.orders_count,
        "danmaku_count": row.danmaku_count,
        "peak_viewers": row.peak_viewers,
        "gift_income": row.gift_income,
    } for row in rows]
    return {"code": 0, "total": len(data), "data": data}


@router.get("/stats")
async def get_live_stats(db: AsyncSession = Depends(get_db)):
    """
    直播大屏运营参数 (需求 8)：
    直播类型(模式)/主播是谁/场观/打赏/成交额/弹幕量/违规拦截/网络状态/直播时长
    """
    from server.database.models import Anchor
    from server.routes.settings import SETTING_KEY_LIVE_MODE, SETTING_KEY_ANCHOR_ID

    stats = global_live_controller.stats
    is_live = global_live_controller.is_live

    anchor_id = None
    res = await db.execute(select(AppSetting).where(AppSetting.key == SETTING_KEY_LIVE_MODE))
    row = res.scalar_one_or_none()
    raw_mode: Optional[str] = str(row.value) if (row and row.value is not None) else None

    from server.routes.settings import normalize_live_mode, LIVE_MODES
    norm_mode = normalize_live_mode(raw_mode)
    mode_name = LIVE_MODES.get(norm_mode, {}).get("name", "本地云端混合")

    res2 = await db.execute(select(AppSetting).where(AppSetting.key == SETTING_KEY_ANCHOR_ID))
    row2 = res2.scalar_one_or_none()
    anchor_id: Optional[str] = str(row2.value) if (row2 and row2.value is not None) else None

    anchor_name: Optional[str] = None
    if anchor_id:
        res3 = await db.execute(select(Anchor).where(Anchor.id == anchor_id))
        anchor = res3.scalar_one_or_none()
        if anchor and anchor.name is not None:
            anchor_name = str(anchor.name)

    active_role = global_role_manager.get_active_role()
    duration_sec = int(time.time() - stats["start_ts"]) if (is_live and stats["start_ts"]) else 0

    return {
        "code": 0,
        "data": {
            "is_live": is_live,
            "session_id": global_live_controller.session_id,
            "mode": mode_name,
            "mode_code": norm_mode,
            "platform": global_live_controller.platform if is_live else None,
            "anchor_name": anchor_name or active_role.role_name,
            "role_name": active_role.role_name,
            "role_type": active_role.role_type,
            "duration_sec": duration_sec,
            "viewer_count": stats["viewer_count"] if is_live else 0,
            "peak_viewer_count": stats["peak_viewer_count"] if is_live else 0,
            "gift_income_yuan": stats["gift_income_yuan"] if is_live else 0.0,
            # 兼容字段：均表示被调度队列 accepted 的真实事件。
            "gift_count": stats["gift_count"] if is_live else 0,
            "gmv_yuan": stats["gmv_yuan"] if is_live else 0.0,
            "orders_count": stats["orders_count"] if is_live else 0,
            "danmaku_count": stats["danmaku_count"] if is_live else 0,
            "events_received_total": stats["events_received_total"] if is_live else 0,
            "events_accepted_total": stats["events_accepted_total"] if is_live else 0,
            "events_dropped_total": stats["events_dropped_total"] if is_live else 0,
            "danmaku_received_total": stats["danmaku_received_total"] if is_live else 0,
            "danmaku_accepted_total": stats["danmaku_accepted_total"] if is_live else 0,
            "danmaku_dropped_total": stats["danmaku_dropped_total"] if is_live else 0,
            "gift_received_total": stats["gift_received_total"] if is_live else 0,
            "gift_accepted_total": stats["gift_accepted_total"] if is_live else 0,
            "gift_dropped_total": stats["gift_dropped_total"] if is_live else 0,
            "queue_metrics": global_live_controller.event_queue.get_stats(),
            "guardrail_hits": stats["guardrail_hits"] if is_live else 0,
            "flash_sales": stats["flash_sales"] if is_live else 0,
            "stitcher_state": (
                global_live_controller.stitcher.state.value if (is_live and getattr(global_live_controller, "stitcher", None)) else "idle"
            ),
            "stitcher_badge": (
                (
                    "【静轨·高精切片】播发中" if global_live_controller.stitcher.state.value == "playing_batch_slice"
                    else "【打断中·平滑停顿】4-step 缓动停顿" if global_live_controller.stitcher.state.value == "interrupted_holding"
                    else "【动轨·实时驱动】正在互动答复观众" if global_live_controller.stitcher.state.value == "streaming_realtime"
                    else "【过渡中·断点回切】4-step 句首回退重入" if global_live_controller.stitcher.state.value == "blending_transition"
                    else "【动静融合中】"
                ) if (is_live and getattr(global_live_controller, "stitcher", None)) else "【待命中】"
            ),
            "active_slice_info": (
                {
                    "slice_id": global_live_controller.stitcher.current_slice.slice_id,
                    "product_id": global_live_controller.stitcher.current_slice.product_id,
                    "slot_code": global_live_controller.stitcher.current_slice.slot_code,
                    "duration_sec": global_live_controller.stitcher.current_slice.duration_sec,
                    "current_frame_idx": global_live_controller.stitcher.current_frame_idx,
                    "loudness_target": f"{global_live_controller.stitcher.current_slice.loudness_lufs:.1f} LUFS",
                    "architecture": "batch_stream_hybrid",
                    "diffusion_model": "ByteDance LatentSync UNet3D",
                } if (is_live and getattr(global_live_controller, "stitcher", None) and global_live_controller.stitcher.current_slice)
                else ({
                    "architecture": "batch_stream_hybrid",
                    "diffusion_model": "ByteDance LatentSync UNet3D",
                    "loudness_target": "-16.0 LUFS",
                    "status": "standby_or_idle",
                } if is_live else None)
            ),
            "network": {
                "ws_clients": len(ws_clients()),
                "status": "connected" if is_live else "idle"
            }
        }
    }


def ws_clients():
    """当前 WS 信令连接数 (网络状态指标)"""
    from server.routes.ws_live import ws_manager
    return ws_manager.active_connections


class RtmpStartRequest(BaseModel):
    rtmp_url: str = Field(..., description="RTMP 服务器地址 (例如 rtmp://live-push.bilivideo.com/live-bvc/)")
    stream_key: Optional[str] = Field("", description="直播码 / 推流密钥")
    width: Optional[int] = Field(720, ge=320, le=3840)
    height: Optional[int] = Field(960, ge=240, le=3840)
    fps: Optional[int] = Field(25, ge=15, le=60)
    bitrate_kbps: Optional[int] = Field(2500, ge=500, le=12000)
    audio_sample_rate: Optional[int] = Field(24000, ge=8000, le=96000)


@router.post("/rtmp/start", summary="启动内置 RTMP 直推引擎")
async def start_rtmp_streaming(req: RtmpStartRequest):
    """启动内置 RTMP 直推引擎，直接将数字人音画推向直播平台"""
    from server.core.media.rtmp_streamer import global_rtmp_streamer
    success, msg = global_rtmp_streamer.start(
        rtmp_url=req.rtmp_url,
        stream_key=req.stream_key or "",
        width=req.width or 720,
        height=req.height or 960,
        fps=req.fps or 25,
        bitrate_kbps=req.bitrate_kbps or 2500,
        audio_sample_rate=req.audio_sample_rate or 24000,
    )
    if not success:
        return {"code": 400, "message": msg, "data": global_rtmp_streamer.get_status()}
    return {"code": 0, "message": msg, "data": global_rtmp_streamer.get_status()}


@router.post("/rtmp/stop", summary="停止内置 RTMP 直推引擎")
async def stop_rtmp_streaming():
    """停止内置 RTMP 直推引擎并释放资源"""
    from server.core.media.rtmp_streamer import global_rtmp_streamer
    global_rtmp_streamer.stop()
    return {"code": 0, "message": "推流已安全停止", "data": global_rtmp_streamer.get_status()}


@router.get("/rtmp/status", summary="查询内置 RTMP 直推状态与运行指标")
async def get_rtmp_status():
    """获取内置 RTMP 直推状态、时长与码率统计"""
    from server.core.media.rtmp_streamer import global_rtmp_streamer
    return {"code": 0, "message": "success", "data": global_rtmp_streamer.get_status()}


# ------------------------------------------------------------------
# LLM 会话预算与 Token 熔断监控端点 (P2-1)
# ------------------------------------------------------------------
@router.get("/llm/budget", summary="查询 LLM 会话预算与熔断状态")
async def get_llm_budget_status():
    """获取当前直播会话的 LLM Token 累计消耗与熔断状态"""
    from server.core.llm.budget_manager import get_llm_budget_manager
    return {"code": 0, "data": get_llm_budget_manager().get_status()}


class LLMBudgetRequest(BaseModel):
    max_tokens: Optional[int] = Field(None, ge=1000, le=50000000, description="单场 Token 上限")
    eco_mode: Optional[bool] = Field(None, description="是否开启节能冷场模式 (使用本地商品话术模板)")


@router.post("/llm/budget", summary="配置 LLM 会话预算与节能模式")
async def update_llm_budget(req: LLMBudgetRequest):
    """动态调整当前直播场次的 LLM Token 预算线与冷场策略"""
    from server.core.llm.budget_manager import get_llm_budget_manager
    mgr = get_llm_budget_manager()
    if req.max_tokens is not None:
        mgr.max_tokens = req.max_tokens
        mgr.is_tripped = mgr.used_tokens >= mgr.max_tokens
    if req.eco_mode is not None:
        mgr.eco_mode = req.eco_mode
    return {"code": 0, "message": "预算参数已更新", "data": mgr.get_status()}


# ------------------------------------------------------------------
# 数字人核心状态查询、极速打断 (flush_talk) 与电商动作切换端点
# ------------------------------------------------------------------
class AvatarActionRequest(BaseModel):
    action_code: int = Field(..., ge=0, le=10, description="动作切片代码: 0=待机, 1=欢迎, 2=点赞关注, 3=促单逼单指购物车, 4=致谢")
    duration: Optional[float] = Field(None, ge=0.0, le=60.0, description="动作持续时间(秒)，超时自动平滑衰减回待机态0")
    priority: Optional[int] = Field(None, ge=0, le=10, description="动作优先级(0-10)")


@router.get("/is-speaking", summary="查询数字人毫秒级实时发声状态")
async def check_is_speaking():
    """供前端呼吸灯与动态波形毫秒级轮询：查询当前主播是否正在发声或播报"""
    from server.core.avatar import get_active_avatar_driver
    driver = get_active_avatar_driver()
    is_speaking = driver.is_speaking()
    status = driver.get_status()
    status["is_speaking"] = is_speaking
    return {
        "code": 0,
        "message": "success",
        "data": status
    }


@router.post("/avatar/flush-talk", summary="瞬间清空数字人口型与音频队列 (极速打断)")
async def flush_avatar_talk():
    """调用数字人驱动瞬间打断接口，清空待播音频队列并恢复待机帧"""
    from server.core.avatar import get_active_avatar_driver
    driver = get_active_avatar_driver()
    await driver.flush_talk()
    return {
        "code": 0,
        "message": "数字人音频与口型队列已瞬间重置",
        "data": driver.get_status()
    }


@router.post("/avatar/action", summary="切换数字人动作切片状态")
async def set_avatar_action(req: AvatarActionRequest):
    """指令式驱动数字人肢体动作切片 (0:呼吸待机, 1:欢迎, 2:求关注, 3:促单指购物车, 4:致谢)"""
    from server.core.avatar import get_active_avatar_driver
    driver = get_active_avatar_driver()
    success = await driver.set_custom_state(
        req.action_code,
        duration=req.duration,
        priority=req.priority,
        source="api"
    )
    return {
        "code": 0 if success else 1,
        "message": f"数字人动作已切换至代码: {req.action_code}",
        "data": driver.get_status()
    }


# ============================================================================
# 🎙️ 全双工 ASR 语音识别与麦克风极速打断系统 (阶段四)
# ============================================================================

@router.post("/asr/install-dependency", summary="触发 ASR 转写引擎依赖后台自动安装 (faster-whisper)")
async def install_asr_dependency():
    """
    体检检测到 faster-whisper 缺失时触发：后台通过国内镜像源 pip 安装轻量转写引擎，
    安装完成后首次语音输入自动懒加载。同一时刻仅允许一个安装任务。
    """
    from server.core.audio.asr_dependency_installer import get_asr_dependency_installer
    result = get_asr_dependency_installer().start_install(force=True)
    return {"code": 0 if result["ok"] else 1, **result}


@router.get("/asr/install-status", summary="查询 ASR 依赖自动安装进度与状态")
async def get_asr_install_status():
    """供控制台轮询安装进度 (pending / installing / installed / failed)"""
    from server.core.audio.asr_dependency_installer import get_asr_dependency_installer
    status = get_asr_dependency_installer().get_status()
    return {"code": 0, "data": status}


@router.websocket("/asr/ws")
async def websocket_asr_endpoint(websocket: WebSocket):
    """
    全双工麦克风音频流 WebSocket：
    客户端流式上行 16kHz 16-bit 单声道 PCM 数据，服务端 VAD 检测人声，
    触发瞬间打断与段落转写，并将识别结果作为现场提问注入直播交互。
    """
    from server.core.security.ws_auth import verify_websocket_auth
    if not await verify_websocket_auth(websocket):
        return

    await websocket.accept()
    session_id = f"asr_{uuid.uuid4().hex[:8]}"
    manager = get_full_duplex_asr_manager()

    loop = asyncio.get_running_loop()

    def _notify_interrupt():
        try:
            asyncio.run_coroutine_threadsafe(
                websocket.send_json({"type": "interrupted", "message": "已触发数字人实时闭嘴打断"}),
                loop
            )
        except Exception:
            pass

    def _notify_transcribe(text: str):
        try:
            asyncio.run_coroutine_threadsafe(
                websocket.send_json({"type": "transcription", "text": text}),
                loop
            )
            logger.info(f"全双工 ASR 捕获现场提问: 【{text}】")
            if getattr(global_live_controller, "is_live", False) and text.strip():
                asyncio.run_coroutine_threadsafe(
                    global_live_controller.ingest_event(
                        event_type="chat",
                        user_name="现场语音提问",
                        payload={"content": text.strip(), "text": text.strip()},
                        priority=1,
                        source="asr_mic",
                    ),
                    loop
                )
        except Exception:
            pass

    # P1-5: VAD 打断接入控制器级代际栅栏，与运营手动打断走同一条 _on_barge_in 通路，
    # 推进 _audio_generation 并清空 virtual_audio/媒体驱动/TTS，杜绝打断拖尾竞态。
    def _trigger_barge_in():
        try:
            if getattr(global_live_controller, "is_live", False):
                return global_live_controller._on_barge_in("现场语音打断")
        except Exception:
            logger.exception("VAD 打断控制器栅栏回调失败")
        return None

    session = manager.get_or_create_session(
        session_id=session_id,
        on_interrupt=_notify_interrupt,
        on_transcribe=_notify_transcribe,
        on_barge_in=_trigger_barge_in,
    )

    try:
        while True:
            message = await websocket.receive()
            if "bytes" in message and message["bytes"]:
                pcm_chunk = message["bytes"]
                res = await asyncio.to_thread(session.feed_pcm, pcm_chunk)
                if "speech_start" in res.get("events", []):
                    await websocket.send_json({"type": "speech_start"})
                if "speech_end" in res.get("events", []):
                    await websocket.send_json({"type": "speech_end"})
            elif "text" in message and message["text"]:
                data = json.loads(message["text"])
                if data.get("type") == "ping":
                    await websocket.send_json({"type": "pong"})
    except WebSocketDisconnect:
        logger.info(f"ASR WebSocket 客户端断开: {session_id}")
    except Exception as e:
        logger.warning(f"ASR WebSocket 异常: {e}")
    finally:
        manager.remove_session(session_id)


@router.post("/asr/transcribe", summary="单段语音离线识别转录")
async def transcribe_audio_file(
    file: UploadFile = File(...),
):
    """上传 wav/mp3/pcm 音频文件直接识别为中文文本"""
    from server.core.audio.asr_engine import transcribe_audio_bytes
    content = await file.read()
    if not content:
        raise HTTPException(status_code=400, detail="音频内容为空")
    text = transcribe_audio_bytes(content)
    return {
        "code": 0,
        "message": "转写完成",
        "data": {
            "text": text,
            "file_size": len(content)
        }
    }


# ============================================================================
# ⚡ WebRTC 原生流媒体大屏 (WHEP 标准协商) (阶段四)
# ============================================================================

@router.post("/webrtc/whep", summary="WHEP 协议创建低延迟 WebRTC 媒体流会话")
async def whep_endpoint(request: Request):
    """
    符合 IETF WHEP (WebRTC HTTP Egress Protocol) 标准规范：
    客户端 POST 发送 SDP Offer，服务端返回 201 Created 与 SDP Answer。
    同时兼容 application/json 格式。
    """
    content_type = request.headers.get("content-type", "")
    sdp_offer = ""
    if "application/json" in content_type:
        body = await request.json()
        sdp_offer = body.get("sdp", "")
    else:
        raw_body = await request.body()
        sdp_offer = raw_body.decode("utf-8", errors="ignore")

    if not sdp_offer.strip():
        raise HTTPException(status_code=400, detail="SDP Offer 不能为空")

    from server.core.media.webrtc_streamer import get_webrtc_stream_manager, WEBRTC_AVAILABLE
    if not WEBRTC_AVAILABLE:
        raise HTTPException(
            status_code=503,
            detail="WebRTC 媒体服务暂不可用：服务器未安装 aiortc/av 多媒体依赖，请降级使用 MJPEG 协议或安装依赖 (pip install aiortc av)",
        )

    stream_mgr = get_webrtc_stream_manager()
    session_id, sdp_answer = await stream_mgr.handle_whep_offer(sdp_offer)

    headers = {
        "Location": f"/api/v1/live/webrtc/whep/{session_id}",
        "Content-Type": "application/sdp",
    }
    accept = request.headers.get("accept", "")
    if "application/json" in accept or "application/json" in content_type:
        return {
            "code": 0,
            "session_id": session_id,
            "sdp": sdp_answer,
            "type": "answer",
        }

    return Response(
        content=sdp_answer,
        status_code=201,
        headers=headers,
        media_type="application/sdp",
    )


@router.delete("/webrtc/whep/{session_id}", summary="断开释放 WebRTC 会话")
async def close_whep_session(session_id: str):
    """释放指定的 WHEP WebRTC PeerConnection 连接"""
    stream_mgr = get_webrtc_stream_manager()
    await stream_mgr.close_session(session_id)
    return {"code": 0, "message": f"会话 {session_id} 已释放"}


@router.get("/webrtc/status", summary="查询 WebRTC 流媒体服务状态")
async def get_webrtc_status():
    """获取当前 WebRTC 在线拉流客户端数与帧率"""
    stream_mgr = get_webrtc_stream_manager()
    return {
        "code": 0,
        "data": stream_mgr.get_status()
    }


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


# ============================================================================
# 🛡️ P1 商业化质量看门狗、Kill Switch 熔断与成本隐私治理接口 (依据《商业化方案》§12)
# ============================================================================

class KillSwitchUpdateRequest(BaseModel):
    feature: str
    enabled: bool
    platform: Optional[str] = None
    anchor_id: Optional[str] = None
    operator: Optional[str] = "admin"
    reason: Optional[str] = ""

class EmergencyCutRequest(BaseModel):
    platform: Optional[str] = None
    anchor_id: Optional[str] = None
    operator: Optional[str] = "admin"
    reason: Optional[str] = "紧急风控熔断"

class BudgetSetRequest(BaseModel):
    budget_limit_cny: float

class PrivacyEraseRequest(BaseModel):
    user_id: str
    operator: Optional[str] = "dpo_admin"
    reason: Optional[str] = "用户行使被遗忘权"


@router.get("/quality/metrics", summary="获取媒体质量 SLI 指标与 SLO 评估快照")
async def get_media_quality_metrics():
    """返回有效 FPS、丢帧率、音画漂移 p95/p99、TTS 首包延时与违规诊断清单"""
    from server.core.monitoring.media_quality_watchdog import global_media_watchdog
    return {
        "code": 0,
        "data": global_media_watchdog.get_sli_snapshot(),
    }


@router.get("/quality/runbook", summary="获取媒体质量排障 Runbook 手册")
async def get_media_quality_runbook():
    """获取所有媒体质量指标对应的排障 Runbook 标准操作步骤"""
    from server.core.monitoring.media_quality_watchdog import MediaQualityWatchdog
    return {
        "code": 0,
        "data": MediaQualityWatchdog.RUNBOOKS,
    }


@router.get("/governance/kill-switch", summary="获取灰度 Feature Flag 与 Kill Switch 状态")
async def get_kill_switch_status():
    """获取全局、各平台与各主播的开关矩阵及最近熔断审计记录"""
    from server.core.compliance.kill_switch import global_kill_switch
    return {
        "code": 0,
        "data": global_kill_switch.get_switches_status(),
    }


@router.post("/governance/kill-switch", summary="动态更新 Feature Flag 开关")
async def update_kill_switch(req: KillSwitchUpdateRequest):
    """更新指定主播、平台或全局功能开关 (如禁用带货促单或推流)"""
    from server.core.compliance.kill_switch import global_kill_switch
    try:
        record = global_kill_switch.set_switch(
            feature=req.feature,
            enabled=req.enabled,
            platform=req.platform,
            anchor_id=req.anchor_id,
            operator=req.operator or "admin",
            reason=req.reason or "",
        )
        return {"code": 0, "message": "开关更新成功", "data": record}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/governance/kill-switch/emergency-cut", summary="一键紧急熔断切断 (Kill Switch)")
async def emergency_kill_switch(req: EmergencyCutRequest):
    """在发生平台风控、舆情或严重违规时秒级熔断指定范围或全局所有功能"""
    from server.core.compliance.kill_switch import global_kill_switch
    record = global_kill_switch.emergency_kill_all(
        platform=req.platform,
        anchor_id=req.anchor_id,
        operator=req.operator or "admin",
        reason=req.reason or "紧急风控熔断",
    )
    return {"code": 0, "message": "紧急熔断已执行，已切断全部功能", "data": record}


@router.get("/cost/summary", summary="获取当前场次成本核算与用量快照")
async def get_session_cost_summary():
    """获取 LLM Token、TTS 字符数、GPU 时长产生费用与每单履约成本"""
    from server.core.compliance.cost_privacy_manager import global_cost_privacy_manager
    return {
        "code": 0,
        "data": global_cost_privacy_manager.calculate_current_cost(),
    }


@router.post("/cost/budget", summary="设置当前场次预算硬上限")
async def set_session_budget(req: BudgetSetRequest):
    """设置场次预算硬上限，达到上限后将自动触发 AI 暂停熔断，防止失控计费"""
    from server.core.compliance.cost_privacy_manager import global_cost_privacy_manager
    global_cost_privacy_manager.budget_limit_cny = max(0.0, float(req.budget_limit_cny))
    global_cost_privacy_manager.warning_triggered = False
    global_cost_privacy_manager.cap_exceeded = False
    return {
        "code": 0,
        "message": f"场次预算硬上限已更新为 ¥{global_cost_privacy_manager.budget_limit_cny:.2f} CNY",
        "data": global_cost_privacy_manager.calculate_current_cost(),
    }


@router.post("/privacy/erase-user", summary="响应用户被遗忘权并抹除用户隐私数据")
async def erase_user_privacy_data(req: PrivacyEraseRequest):
    """彻底抹除用户行为日志与标识，并留存不可篡改的数据抹除审计记录"""
    from server.core.compliance.cost_privacy_manager import global_cost_privacy_manager
    try:
        record = global_cost_privacy_manager.erase_user_data(
            user_id=req.user_id,
            operator=req.operator or "dpo_admin",
            reason=req.reason or "用户行使被遗忘权",
        )
        return {"code": 0, "message": "用户隐私数据已成功抹除", "data": record}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


