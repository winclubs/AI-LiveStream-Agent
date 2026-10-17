# -*- coding: utf-8 -*-
"""场次状态机：构造、锁、统计、AI 暂停/接管/恢复。

由 server/routes/live.py 的 LiveSessionController 拆出。
缝在**函数体内**从 server.routes.live 解析，见 test_live_seam_late_binding.py。
"""
import logging
from collections import deque
import asyncio
import time
import numpy as np
from server.adapters.media.media_router import global_media_router
from server.core.media.av_sync import global_av_sync
from server.core.media.frame_bus import global_frame_bus
from server.core.media.hybrid_stitcher import HybridStitcher
from server.core.media.shared_playback_clock import global_shared_playback_clock
from server.core.monitoring.vram_watchdog import VRAMWatchdog
from server.core.queue.priority_queue import BarrageAggregator, PriorityBargeInQueue
from typing import Any, Optional
logger = logging.getLogger("LiveAgent.LiveController")


class SessionStateMixin:
    """场次状态机：构造、锁、统计、AI 暂停/接管/恢复"""
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
        from server.routes.live import EdgeTTSMediaDriver
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


    async def _on_vram_alert(self, info: dict):
        from server.routes.ws_live import ws_manager
        await ws_manager.broadcast("VRAM_WARNING", info)


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
