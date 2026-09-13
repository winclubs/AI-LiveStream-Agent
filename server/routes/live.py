import asyncio
import uuid
import base64
import json
import os
import re
import time
import logging
import inspect
import psutil
from collections import deque
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from typing import Optional
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

logger = logging.getLogger("LiveAgent.LiveController")
router = APIRouter(prefix="/live", tags=["直播控场与调度"])


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
    return await callable_obj(*args, **_supported_metadata(callable_obj, metadata))


def _call_sync_compat(callable_obj, *args, **metadata):
    return callable_obj(*args, **_supported_metadata(callable_obj, metadata))

# 全局直播运行控制器
class LiveSessionController:
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
        self.live_context = {"products": []}
        self._background_tasks = set()
        self._session_generation = 0
        self._audio_generation = 0
        self._lifecycle_lock: Optional[asyncio.Lock] = None
        self.tts_driver = EdgeTTSMediaDriver()
        self.vram_watchdog = VRAMWatchdog(on_alert=self._on_vram_alert)
        # 直播大屏运营统计 (需求 8)
        self.stats = {
            "start_ts": None,
            "viewer_count": 0,
            "peak_viewer_count": 0,
            "gift_income_yuan": 0.0,
            "gmv_yuan": 0.0,
            "orders_count": 0,
            "danmaku_count": 0,
            "gift_count": 0,
            "guardrail_hits": 0,
            "flash_sales": 0
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
            "danmaku_count": 0,
            "gift_count": 0,
            "guardrail_hits": 0,
            "flash_sales": 0
        })

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

    def hot_reload(self, section: str = "all"):
        """同步上下文里的安全热重载入口：未开播直接跳过，开播中调度到事件循环执行。"""
        if not self.is_live:
            return
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(self.reload_runtime_config(section))
        except RuntimeError:
            # 无运行中的事件循环（同步调用栈）: 尝试复用服务主循环
            from server.app import _service_loop
            if _service_loop is not None and not _service_loop.is_closed():
                asyncio.run_coroutine_threadsafe(self.reload_runtime_config(section), _service_loop)

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
        try:
            await self.tts_driver.interrupt(reason)
        except Exception:
            logger.exception("TTS 驱动打断失败")
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
            token = extra.get("auth_token") or (decrypt_secret(remote_cfg.encrypted_api_key) if remote_cfg.encrypted_api_key else "")
            driver = RemoteGPUMediaDriver(node_url=remote_cfg.base_url, auth_token=token)
            await driver.start()
            if driver.is_connected:
                logger.info("已启用端云分离远程 GPU 渲染通道 (Tier C)")
                return driver
            logger.info("远程 GPU 节点不可达，自动降级本地 TTS")

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
            key = decrypt_secret(tts_cfg.encrypted_api_key) if tts_cfg.encrypted_api_key else ""
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

    def _track_task(self, coroutine):
        """跟踪短生命周期后台任务，停播时统一收敛，避免跨场执行。"""
        task = asyncio.create_task(coroutine)
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)
        return task

    async def start(self, session_id: str, products: list, room_id: Optional[str] = None, platform: str = "bilibili", theme: str = "", voice_id: Optional[str] = None, avatar_path: str = "", mode: str = "B", landmarks_path: str = ""):
        """事务式启动资源；任一步失败都回滚已启动任务和设备。"""
        try:
            await self._start_resources(
                session_id, products, room_id, platform, theme, voice_id,
                avatar_path, mode, landmarks_path
            )
        except Exception:
            logger.exception("直播资源启动失败，正在回滚")
            await self.stop()
            raise

    async def _start_resources(self, session_id: str, products: list, room_id: Optional[str] = None, platform: str = "bilibili", theme: str = "", voice_id: Optional[str] = None, avatar_path: str = "", mode: str = "B", landmarks_path: str = ""):
        if self.is_live:
            return
        self.is_live = True
        self._session_generation += 1
        self._audio_generation += 1
        self.session_id = session_id
        self.event_queue = PriorityBargeInQueue(on_interrupt_callback=self._on_barge_in)
        self.platform = platform
        self.live_context = {"products": products, "theme": theme or ""}
        self.history.clear()
        self.aggregator = BarrageAggregator(window_seconds=3.0)
        self._stats_reset()

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

        # 启动数字人渲染引擎与音画帧管道 (根据模式、主播底图与人脸关键点)
        global_media_router.select_driver(mode=mode, avatar_path=avatar_path, landmarks_path=landmarks_path)
        await global_media_router.start()

        # 启动 VRAM 看门狗 (无 GPU 环境自动空转)
        self.vram_watchdog.start()

        # 根据平台经注册表选择真实抓取器；未注册平台回退中继/仿真
        from server.adapters.danmaku.registry import global_danmaku_registry
        platform_lower = (platform or "bilibili").lower().strip()
        is_mock_room = not room_id or not room_id.strip() or room_id.strip().lower() in ["room_demo", "mock"]

        if is_mock_room:
            logger.info("启动仿真弹幕注入器 (MockDanmakuFetcher)")
            self.fetcher = MockDanmakuFetcher("room_demo_888", self._on_danmaku_event)
        elif global_danmaku_registry.has(platform_lower):
            clean_id = re.sub(r"[^0-9]", "", room_id.split("?")[0].rstrip("/").split("/")[-1]) or room_id
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
                )
            else:
                self.fetcher = MockDanmakuFetcher(room_id=room_id, on_event_callback=self._on_danmaku_event)
        else:
            logger.info(
                f"平台【{platform}】未内置真实协议 (可用平台: {global_danmaku_registry.list_platforms()})，"
                f"已挂载被动监听器：请通过 POST /live/danmaku-webhook 或 WS /ws/danmaku-ingest 推送弹幕"
            )
            # 被动模式：不注入仿真事件，弹幕完全依赖外部中继推送
            self.fetcher = MockDanmakuFetcher(room_id=room_id, on_event_callback=self._on_danmaku_event, auto_inject=False)

        await self.fetcher.start()

        # 启动优先级队列消费工作协程
        self.worker_task = asyncio.create_task(self._consume_queue_loop())

        # 启动视觉感知采集协程 (按配置节流抓取桌面/摄像头画面)
        if self.vision_task and not self.vision_task.done():
            self.vision_task.cancel()
        self.vision_task = asyncio.create_task(self._vision_loop())

        logger.info(f"直播会话已启动: {session_id}，平台: {platform}")

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

    async def stop(self):
        """幂等停止并等待所有会话任务退出，最后清除会话身份。"""
        self.is_live = False
        self._session_generation += 1
        self._audio_generation += 1
        if self.fetcher:
            await self.fetcher.stop()
            self.fetcher = None

        session_tasks = [
            task for task in (self.worker_task, self.viewer_task, self.vision_task)
            if task and not task.done()
        ]
        for task in session_tasks:
            task.cancel()
        if session_tasks:
            await asyncio.gather(*session_tasks, return_exceptions=True)
        self.worker_task = self.viewer_task = self.vision_task = None

        # 日志/广播等受控后台任务必须在 session_id 清除前完成或取消。
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

        global_vision.close()
        self.live_context.pop("vision_image_b64", None)
        _call_sync_compat(
            global_virtual_audio.stop,
            next_generation=self._audio_generation,
        )
        await self.vram_watchdog.stop()
        await self.tts_driver.stop()
        self.event_queue.clear()
        global_media_router.detach_remote()
        await global_media_router.stop()
        self.session_id = None
        self.live_context = {"products": []}
        logger.info("直播会话已安全停止")

    def _on_danmaku_event(self, event_type: str, user_name: str, payload: dict, priority: int = 2):
        # 运营统计累加 (需求 8：打赏金额/弹幕量)
        if event_type == "gift":
            total_coin = payload.get("total_coin", 0) or 0
            self.stats["gift_income_yuan"] = round(self.stats["gift_income_yuan"] + total_coin / 1000.0, 2)
            self.stats["gift_count"] += 1
        elif event_type == "danmaku":
            self.stats["danmaku_count"] += 1

        self._track_task(
            self.event_queue.put(
                event_id=f"evt_{uuid.uuid4().hex[:8]}",
                event_type=event_type,
                user_name=user_name,
                payload=payload,
                priority=priority
            )
        )
        # 实时广播弹幕/送礼事件给前端大屏瀑布流展示
        from server.routes.ws_live import ws_manager
        text_content = payload.get("text") or (f"送出【{payload.get('gift_name')}】x{payload.get('count', 1)}" if event_type == "gift" else "进入直播间")
        self._track_task(
            ws_manager.broadcast("danmaku", {
                "user": user_name,
                "text": text_content,
                "event_type": event_type,
                "priority": priority
            })
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
        sentence_delimiters = re.compile(r'([，。！？；\n]+)')

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

                # 广播当前播报状态
                await ws_manager.broadcast("speaking_state", {"is_speaking": True, "user": event.user_name})

                # 获取主播大模型流式思考切片 (携带多轮对话历史)
                text_buffer = ""
                full_reply = ""
                stream = active_role.process_event(
                    event.event_type,
                    event.user_name,
                    event.payload,
                    self.live_context
                )

                async for raw_chunk in stream:
                    # 检查是否中途被更高优先级的 P0 打断
                    if self.event_queue.is_cancelled():
                        logger.info("当前播报已被更高优先级抢占打断")
                        break

                    text_buffer += raw_chunk
                    parts = sentence_delimiters.split(text_buffer)

                    # 如果凑齐了完整短句
                    if len(parts) >= 3:
                        # 组合出第一句
                        complete_sentence = parts[0] + parts[1]
                        text_buffer = "".join(parts[2:])

                        speak_text, is_dropped, hits = await self._sanitize_and_humanize(
                            complete_sentence, active_role
                        )
                        if hits:
                            self._track_task(self._log_guardrail_hits(
                                complete_sentence, speak_text, hits, is_dropped, self.session_id
                            ))
                        if is_dropped or not speak_text.strip():
                            continue

                        full_reply += speak_text
                        await self._speak_sentence(speak_text, active_role)

                # 处理缓冲区剩余的尾句
                if text_buffer.strip() and not self.event_queue.is_cancelled():
                    speak_text, is_dropped, hits = await self._sanitize_and_humanize(text_buffer, active_role)
                    if hits:
                        self._track_task(self._log_guardrail_hits(
                            text_buffer, speak_text, hits, is_dropped, self.session_id
                        ))
                    if not is_dropped and speak_text.strip():
                        full_reply += speak_text
                        await self._speak_sentence(speak_text, active_role)

                # 事件处理完毕，广播播报呼吸平息态
                await ws_manager.broadcast("speaking_state", {"is_speaking": False})

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
                break
            except Exception as e:
                logger.error(f"直播主循环异常: {e}", exc_info=True)
                await asyncio.sleep(0.5)

    async def _sanitize_and_humanize(self, sentence: str, active_role):
        """违禁词扫描平替 -> 电商价格防幻觉双重审计 -> 语音防机械感人类化 (规划 §14.2 / §14.3)"""
        sanitized_sentence, hits, is_dropped = global_guardrail.sanitize(
            sentence, active_role.role_type
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

    async def _collect_tts_sentence(self, driver, text: str) -> bytes:
        """完整收集一句音频；异常或提前取消时显式关闭上游异步生成器。"""
        chunks = []
        stream = driver.synthesize_stream(text)
        try:
            async for chunk in stream:
                if self.event_queue.is_cancelled():
                    return b""
                if chunk:
                    chunks.append(chunk)
            return b"".join(chunks)
        finally:
            close_stream = getattr(stream, "aclose", None)
            if close_stream is not None:
                await close_stream()

    async def _switch_to_edge_tts(self):
        """运行期 TTS 故障后原子卸载远程媒体源并切换 Edge 驱动。"""
        previous = self.tts_driver
        # 先取消远程帧优先级，避免驱动停止期间继续暴露旧画面。
        global_media_router.detach_remote()
        try:
            await previous.stop()
        except Exception:
            logger.exception("停止故障 TTS 驱动失败")
        fallback = EdgeTTSMediaDriver()
        await fallback.start()
        self.tts_driver = fallback
        return fallback

    async def _speak_sentence(self, speak_text: str, active_role):
        """完整合成后一次性提交音频，并在每个关键 await 后复核双代际。"""
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
        try:
            full_audio = await self._collect_tts_sentence(driver, speak_text)
            # 紧邻生成器耗尽读取事务 ID，中间不 await，避免其他请求覆盖兼容游标。
            video_request_id = getattr(driver, "last_completed_request_id", None)
            if not is_current():
                return
            if not full_audio and not isinstance(driver, EdgeTTSMediaDriver):
                raise RuntimeError("TTS 返回空音频")
        except asyncio.CancelledError:
            raise
        except Exception as tts_err:
            if not is_current():
                return
            if isinstance(driver, EdgeTTSMediaDriver):
                logger.exception("Edge-TTS 语音合成失败")
                return
            logger.warning("TTS 运行期合成失败，丢弃半句并切换 Edge-TTS: %s", tts_err)
            try:
                driver = await self._switch_to_edge_tts()
                if not is_current():
                    return
                full_audio = await self._collect_tts_sentence(driver, speak_text)
                video_request_id = getattr(driver, "last_completed_request_id", None)
                if not is_current():
                    return
            except Exception:
                logger.exception("Edge-TTS 运行期降级失败")
                return

        global_av_sync.measure(synth_started)
        if not full_audio or not is_current():
            return

        codec = getattr(driver, "audio_codec", "mp3")
        mime_type = getattr(driver, "audio_mime_type", "audio/mpeg")
        sample_rate = int(getattr(driver, "audio_sample_rate", 24000))
        channels = int(getattr(driver, "audio_channels", 1))
        audio_id = f"{self.session_id or 'local'}_{audio_generation}_{uuid.uuid4().hex[:8]}"
        pts_ms = int(time.monotonic() * 1000)
        metadata = {
            "codec": codec,
            "sample_rate": sample_rate,
            "channels": channels,
            "audio_generation": audio_generation,
            "session_generation": session_generation,
        }

        # 口型 driver 可能执行异步解码；恢复后必须再次拒绝已失效代际。
        await _call_async_compat(
            global_media_driver.feed_audio_chunk,
            full_audio,
            speak_text,
            **metadata,
        )
        if not is_current():
            return

        _call_sync_compat(
            global_virtual_audio.play_chunk,
            full_audio,
            fallback_sample_rate=sample_rate,
            **metadata,
        )
        if not is_current():
            return

        # 远程视频在整句音频确认成功并进入播放队列后才开始发布，禁止合成阶段提前直出。
        commit_video = getattr(driver, "commit_video_frames", None)
        if commit_video is not None:
            commit_kwargs = {
                "codec": codec,
                "sample_rate": sample_rate,
                "channels": channels,
            }
            if video_request_id:
                commit_kwargs["request_id"] = video_request_id
            await commit_video(full_audio, **commit_kwargs)
            if not is_current():
                return

        await ws_manager.broadcast("AUDIO_CHUNK", {
            "audio_base64": base64.b64encode(full_audio).decode("utf-8"),
            "text": speak_text,
            "speaker": active_role.role_name,
            "codec": codec,
            "mime_type": mime_type,
            "sample_rate": sample_rate,
            "channels": channels,
            "audio_id": audio_id,
            "audio_generation": audio_generation,
            "session_generation": session_generation,
            "delay_ms": 0,
            "pts_ms": pts_ms,
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

class LiveStartRequest(BaseModel):
    room_url: Optional[str] = ""
    room_id: Optional[str] = None
    role_id: Optional[str] = None
    anchor_id: Optional[str] = None
    voice_id: Optional[str] = None
    platform: Optional[str] = "bilibili"

class LiveInterruptRequest(BaseModel):
    text: str

class DanmakuWebhookPayload(BaseModel):
    platform: Optional[str] = "douyin"
    user_name: str
    text: Optional[str] = ""
    event_type: Optional[str] = "chat"  # chat / gift / follow
    gift_name: Optional[str] = None
    gift_count: Optional[int] = 1
    total_coin: Optional[int] = 0

@router.get("/status")
async def get_live_status():
    """获取本地直播源状态；不推断任何外部平台发布状态。"""
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
        "external_publish": {
            "status": "not_managed",
            "platform_live": None,
            "validation": "pending_external_acceptance",
        },
    }

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
        role_row.is_active = 1
        global_role_manager.register_or_update_from_db(role_row, activate=True)
    active_role = global_role_manager.get_active_role()

    anchor_row = None
    if req.anchor_id:
        anchor_row = await db.get(Anchor, req.anchor_id)
        if not anchor_row:
            raise HTTPException(status_code=400, detail=f"指定的主播不存在: {req.anchor_id}")

    target_voice_id = req.voice_id or (anchor_row.voice_id if anchor_row else None)
    if target_voice_id and not await db.get(VoiceProfile, target_voice_id):
        raise HTTPException(status_code=400, detail=f"指定的音色不存在: {target_voice_id}")

    rows = (await db.execute(select(Product).where(Product.is_active == 1))).scalars().all()
    from server.routes.products import product_to_context
    products = [product_to_context(product) for product in rows]

    target_room = req.room_id or req.room_url
    session_id = f"sess_{uuid.uuid4().hex[:12]}"
    platform = req.platform or "bilibili"
    live_mode = await _get_setting(db, SETTING_KEY_LIVE_MODE) or "B"
    live_theme = await _get_setting(db, "live_theme") or ""
    avatar_path = anchor_row.photo_portrait if anchor_row and anchor_row.photo_portrait else ""
    landmarks_path = await asyncio.to_thread(_resolve_landmarks_for_avatar, avatar_path) if avatar_path else ""

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
    )
    db.add(session_record)
    await db.commit()

    try:
        await global_live_controller.start(
            session_id, products, target_room, platform=platform, theme=live_theme,
            voice_id=target_voice_id, avatar_path=avatar_path, mode=live_mode,
            landmarks_path=landmarks_path
        )
        session_record.status = "live"
        await db.commit()
    except Exception as exc:
        await db.rollback()
        record = await db.get(LiveSessionRecord, session_id)
        if record:
            record.status = "failed"
            record.end_time = utc_now()
            await db.commit()
        raise HTTPException(status_code=500, detail=f"直播资源启动失败: {exc}") from exc

    return {
        "code": 0,
        "session_id": session_id,
        "is_live": True,
        "platform": platform,
        "voice_id": target_voice_id,
        "role_id": active_role.role_id,
        "mode": live_mode,
        "local_source": {"status": "running", "preview": "/api/v1/live/stream/preview"},
        "external_publish": {
            "status": "not_managed",
            "platform_live": None,
            "validation": "pending_external_acceptance",
        },
        "message": f"本地直播源已成功启动 ({platform}, 模式 {live_mode})；外部平台发布需人工验收"
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
    await global_live_controller.stop()

    if sid:
        record = await db.get(LiveSessionRecord, sid)
        if record:
            record.end_time = utc_now()
            record.status = "stopped"
            record.danmaku_count = stats.get("danmaku_count", 0)
            record.peak_viewers = stats.get("peak_viewer_count", 0)
            record.gift_income = stats.get("gift_income_yuan", 0.0)
            # 订单汇总以数据库为权威源，取消/退款订单不会计入成交。
            aggregate = await db.execute(
                select(func.coalesce(func.sum(Order.amount), 0.0), func.count(Order.id))
                .where(Order.session_id == sid, Order.status == "completed")
            )
            total_gmv, orders_count = aggregate.one()
            record.total_gmv = float(total_gmv or 0.0)
            record.orders_count = int(orders_count or 0)
            await db.commit()

    return {"code": 0, "is_live": False, "message": "直播已停止"}


@router.post("/stop")
async def stop_live(db: AsyncSession = Depends(get_db)):
    """用同一把生命周期锁包裹完整停播状态转换。"""
    async with global_live_controller.lifecycle_lock:
        return await _stop_live_unlocked(db)


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

@router.get("/media/status")
async def get_media_status():
    """获取当前数字人媒体驱动渲染指标、虚拟摄像头、视觉感知与音画同步状态"""
    data = global_media_router.get_preview_status()
    data["vision"] = global_vision.get_status()
    data["vision"]["active_frame"] = bool(global_live_controller.live_context.get("vision_image_b64"))
    data["av_sync"] = global_av_sync.get_status()
    return {
        "code": 0,
        "data": data
    }

@router.post("/danmaku-webhook")
async def receive_danmaku_webhook(payload: DanmakuWebhookPayload):
    """
    通用第三方弹幕 Webhook (支持抖音/快手/视频号弹幕姬、浏览器扩展、本地中继推送)
    """
    if not global_live_controller.is_live:
        return {"code": 1, "message": "直播间尚未开播，弹幕已丢弃"}

    event_type = payload.event_type or "chat"
    danmaku_payload = {
        "platform": payload.platform or "external",
        "text": payload.text or "",
        "gift_name": payload.gift_name,
        "count": payload.gift_count or 1,
        "total_coin": payload.total_coin or 0
    }
    priority = 0 if event_type == "gift" and (payload.total_coin or 0) >= 50000 else (1 if event_type == "gift" else 2)
    global_live_controller._on_danmaku_event(event_type, payload.user_name, danmaku_payload, priority=priority)
    return {"code": 0, "message": "弹幕已接收并进入调度队列"}

@router.post("/interrupt")
@router.post("/manual-speech")
async def interrupt_live(req: LiveInterruptRequest):
    """运营人员紧急人工插话（双路由对齐，触发毫秒级打断并播报新内容）"""
    if not global_live_controller.is_live:
        raise HTTPException(status_code=400, detail="直播尚未开播，禁止插播")
    await global_live_controller.event_queue.put(
        event_id=f"evt_manual_{uuid.uuid4().hex[:6]}",
        event_type="chat",
        user_name="运营管理员",
        payload={"text": req.text},
        priority=0  # P0 触发抢占打断
    )
    return {"code": 0, "message": "人工插话已成功抢占插播"}


class MockEventRequest(BaseModel):
    type: str = "chat"  # chat(促单提问 P1) / gift(大额打赏 P0 打断)


@router.post("/mock-event")
async def inject_mock_event(req: MockEventRequest):
    """向直播队列注入仿真观众事件 (演示/联调按钮的真实数据通路)"""
    if not global_live_controller.is_live:
        raise HTTPException(status_code=400, detail="直播间尚未开播，请先在直播大屏一键开播")

    if req.type == "gift":
        await global_live_controller.event_queue.put(
            event_id=f"evt_mock_{uuid.uuid4().hex[:6]}",
            event_type="gift",
            user_name="仿真大哥888",
            payload={"gift_name": "超级大火箭", "count": 1, "total_coin": 100000},
            priority=0
        )
        return {"code": 0, "message": "已注入仿真大额打赏 (P0 强打断)，AI 主播将立刻感谢"}
    else:
        await global_live_controller.event_queue.put(
            event_id=f"evt_mock_{uuid.uuid4().hex[:6]}",
            event_type="chat",
            user_name="仿真买家小美",
            payload={"text": "主播，这款多少钱？现在还有优惠吗？"},
            priority=1
        )
        return {"code": 0, "message": "已注入仿真促单提问 (P1)，AI 主播将优先解答"}

# ---------------------------------------------------------------------------
# 硬件探测与运行档位推荐 (规划 §8.1)
# ---------------------------------------------------------------------------
import shutil
import subprocess
from pathlib import Path

# nvidia-smi 常见安装位置 (Windows 驱动默认不在 PATH 中)
_NVIDIA_SMI_CANDIDATES = [
    r"C:\Windows\System32\nvidia-smi.exe",
    r"C:\Program Files\NVIDIA Corporation\NVSMI\nvidia-smi.exe",
    r"C:\Program Files\NVIDIA Corporation\Driver\nvidia-smi.exe",
    "/usr/bin/nvidia-smi",
    "/usr/local/bin/nvidia-smi",
]

# 探测结果缓存：torch 只尝试导入一次，WMI 只查询一次 (避免 5s 轮询开销)
_GPU_PROBE_CACHE = {
    "torch_checked": False, "torch": None,
    "wmi_checked": False, "wmi_info": None,
}


def _get_torch():
    """惰性导入 torch (未安装时快速失败且不重复尝试)"""
    if not _GPU_PROBE_CACHE["torch_checked"]:
        _GPU_PROBE_CACHE["torch_checked"] = True
        try:
            import importlib
            _GPU_PROBE_CACHE["torch"] = importlib.import_module("torch")
        except Exception:
            _GPU_PROBE_CACHE["torch"] = None
    return _GPU_PROBE_CACHE["torch"]


def _find_nvidia_smi() -> Optional[str]:
    """定位 nvidia-smi 可执行文件"""
    which = shutil.which("nvidia-smi")
    if which:
        return which
    for cand in _NVIDIA_SMI_CANDIDATES:
        if Path(cand).exists():
            return cand
    return None


def _wmi_gpu_probe() -> dict:
    """WMI 兜底探测显卡 (仅查询一次并缓存)：过滤虚拟显卡，取显存最大的真实物理显卡"""
    if _GPU_PROBE_CACHE["wmi_checked"]:
        return _GPU_PROBE_CACHE["wmi_info"]
    _GPU_PROBE_CACHE["wmi_checked"] = True
    _GPU_PROBE_CACHE["wmi_info"] = {"gpu_name": None, "vram_total_gb": 0.0}
    try:
        ps_script = (
            "Get-CimInstance Win32_VideoController | "
            "Where-Object { $_.Name -and $_.Name -notmatch "
            "'Oray|IddDriver|Virtual|Basic Display|Microsoft 基本显示|Remote' } | "
            "Sort-Object AdapterRAM -Descending | "
            "Select-Object -First 1 Name, AdapterRAM | ConvertTo-Json -Compress"
        )
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps_script],
            capture_output=True, text=True, timeout=8
        )
        if out.returncode == 0 and out.stdout.strip():
            data = json.loads(out.stdout)
            if isinstance(data, list):
                data = data[0] if data else {}
            name = (data.get("Name") or "").strip()
            if name:
                info = {"gpu_name": name, "vram_total_gb": 0.0}
                ram = data.get("AdapterRAM") or 0
                # AdapterRAM 为 uint32，>4GB 显存会溢出误报，仅在合理范围使用
                if 0 < ram <= 4 * 1024 ** 3:
                    info["vram_total_gb"] = round(ram / 1024 ** 3, 2)
                _GPU_PROBE_CACHE["wmi_info"] = info
    except Exception:
        pass
    return _GPU_PROBE_CACHE["wmi_info"]


def _probe_gpu() -> dict:
    """
    三级探测本机 GPU：
      1. PyTorch CUDA (最精确，实时显存)
      2. nvidia-smi (自动搜索常见安装路径，实时显存)
      3. WMI Win32_VideoController (兜底识别物理显卡型号)
    """
    info = {"gpu_name": None, "vram_total_gb": 0.0, "vram_used_gb": 0.0, "cuda_available": False}

    # 1. PyTorch CUDA
    torch = _get_torch()
    if torch is not None:
        try:
            if torch.cuda.is_available():
                props = torch.cuda.get_device_properties(0)
                used, total = torch.cuda.mem_get_info(0)
                info.update(
                    gpu_name=props.name,
                    vram_total_gb=round(total / (1024 ** 3), 2),
                    vram_used_gb=round((total - used) / (1024 ** 3), 2),
                    cuda_available=True
                )
                return info
        except Exception:
            pass

    # 2. nvidia-smi (含 Windows 驱动常见安装路径)
    smi = _find_nvidia_smi()
    if smi:
        try:
            out = subprocess.run(
                [smi, "--query-gpu=name,memory.total,memory.used", "--format=csv,noheader,nounits"],
                capture_output=True, text=True, timeout=4
            )
            if out.returncode == 0 and out.stdout.strip():
                line = out.stdout.strip().splitlines()[0]
                name, total, used = [x.strip() for x in line.split(",")]
                info.update(
                    gpu_name=name,
                    vram_total_gb=round(float(total) / 1024, 2),
                    vram_used_gb=round(float(used) / 1024, 2)
                )
                return info
        except Exception:
            pass

    # 3. WMI 兜底 (识别物理显卡型号，无实时显存)
    wmi_info = _wmi_gpu_probe()
    if wmi_info["gpu_name"]:
        info.update(gpu_name=wmi_info["gpu_name"], vram_total_gb=wmi_info["vram_total_gb"])
    return info

def _resolve_landmarks_for_avatar(avatar_path: str) -> str:
    """
    按主播底图解析人脸关键点缓存 (规划 §4.2)：
    - 已有缓存 (同目录 <图名>_landmarks.json) 直接复用，不重复检测
    - 无缓存则执行一次 MediaPipe/Haar 检测并落盘 (线程池中调用)
    - 底图缺失或检测结果无效时返回空串，渲染器回退比例定位
    """
    if not avatar_path:
        return ""
    from pathlib import Path as _Path
    import json as _json
    from server.core.vision.face_landmarks import detect_face_landmarks

    photo = _Path(avatar_path)
    if not photo.exists():
        return ""
    cache = photo.with_name(f"{photo.stem}_landmarks.json")
    if not cache.exists():
        try:
            detect_face_landmarks(str(photo), str(cache))
        except Exception as e:
            logger.debug(f"人脸关键点检测失败: {e}")
            return ""
    try:
        data = _json.loads(cache.read_text(encoding="utf-8"))
        box = data.get("face_box")
        if box and len(box) == 4 and box[2] > 0:
            return str(cache)
    except Exception:
        pass
    return ""


def _recommend_tier(gpu_info: dict) -> str:
    """按显存阶梯推荐运行档位"""
    vram = gpu_info.get("vram_total_gb", 0.0) or 0.0
    if gpu_info.get("cuda_available") and vram >= 15.5:
        return "Tier A (全本地离线模式)"
    if vram >= 5.5:
        return "Tier B (主流端云混合模式)"
    if vram > 0:
        return "Tier C (端云分离模式)"
    return "Tier D (轻量免显卡模式)"

# 硬件负载 TTL 缓存 (避免多客户端 5s 轮询反复 spawn nvidia-smi/WMI 子进程)
_HW_PAYLOAD_CACHE = {"ts": 0.0, "data": None}
_HW_CACHE_TTL_SEC = 3.0


async def _hardware_payload() -> dict:
    import platform
    now = time.time()
    if _HW_PAYLOAD_CACHE["data"] is not None and now - _HW_PAYLOAD_CACHE["ts"] < _HW_CACHE_TTL_SEC:
        # 动态指标 (CPU/内存占用) 实时更新，静态探测 (GPU/型号) 走缓存
        cached = dict(_HW_PAYLOAD_CACHE["data"])
        cpu_percent = psutil.cpu_percent(interval=None)
        memory = psutil.virtual_memory()
        cached.update(
            cpu_percent=cpu_percent,
            ram_used_gb=round(memory.used / (1024 ** 3), 2),
            ram_percent=memory.percent,
        )
        return cached

    cpu_percent = psutil.cpu_percent(interval=None)
    memory = psutil.virtual_memory()
    # 重量级 GPU 探测 (nvidia-smi/WMI 子进程) 卸载到线程池，严禁阻塞事件循环 (ADR-08)
    gpu_info = await asyncio.to_thread(_probe_gpu)
    cpu_name = platform.processor() or os.environ.get("PROCESSOR_IDENTIFIER", "") or "未知处理器"
    payload = {
        "cpu_name": cpu_name,
        "cpu_cores": psutil.cpu_count(logical=True) or 0,
        "cpu_percent": cpu_percent,
        "ram_used_gb": round(memory.used / (1024 ** 3), 2),
        "ram_total_gb": round(memory.total / (1024 ** 3), 2),
        "ram_percent": memory.percent,
        "gpu": gpu_info,
        "recommended_mode": _recommend_tier(gpu_info)
    }
    _HW_PAYLOAD_CACHE["ts"] = now
    _HW_PAYLOAD_CACHE["data"] = payload
    return payload

@router.get("/hardware")
async def get_hardware_status():
    """获取本地硬件资源负载与显存建议 (兼容路由)"""
    return {"code": 0, "data": await _hardware_payload()}


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


async def _refresh_order_stats(db: AsyncSession, session_id: str) -> tuple[float, int]:
    from server.database.models import Order
    result = await db.execute(
        select(func.coalesce(func.sum(Order.amount), 0.0), func.count(Order.id)).where(
            Order.session_id == session_id, Order.status == "completed"
        )
    )
    gmv, count = result.one()
    aggregate = (round(float(gmv or 0.0), 2), int(count or 0))
    if session_id == global_live_controller.session_id:
        global_live_controller.stats["gmv_yuan"] = aggregate[0]
        global_live_controller.stats["orders_count"] = aggregate[1]
    return aggregate


@router.post("/stats/order")
async def register_order(req: OrderRequest, db: AsyncSession = Depends(get_db)):
    """幂等登记成交，库存条件更新与订单/流水在同一事务提交。"""
    from server.database.models import InventoryMovement, Order

    if not global_live_controller.is_live or not global_live_controller.session_id:
        raise HTTPException(status_code=400, detail="直播间尚未开播")

    external_id = (req.external_id or f"manual-{uuid.uuid4().hex}").strip()
    existing = (await db.execute(select(Order).where(Order.external_id == external_id))).scalar_one_or_none()
    if existing:
        gmv_yuan, orders_count = await _refresh_order_stats(db, existing.session_id)
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
    if result.rowcount != 1:
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
    gmv_yuan, orders_count = await _refresh_order_stats(db, order.session_id)
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
    if result.rowcount != 1:
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
    await _refresh_order_stats(db, order.session_id)
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
    if result.rowcount != 1:
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
    await _refresh_order_stats(db, order.session_id)
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

    mode = None
    anchor_id = None
    res = await db.execute(select(AppSetting).where(AppSetting.key == SETTING_KEY_LIVE_MODE))
    row = res.scalar_one_or_none()
    mode = row.value if row else None

    res2 = await db.execute(select(AppSetting).where(AppSetting.key == SETTING_KEY_ANCHOR_ID))
    row2 = res2.scalar_one_or_none()
    anchor_id = row2.value if row2 else None

    anchor_name = None
    if anchor_id:
        res3 = await db.execute(select(Anchor).where(Anchor.id == anchor_id))
        anchor = res3.scalar_one_or_none()
        if anchor:
            anchor_name = anchor.name

    active_role = global_role_manager.get_active_role()
    duration_sec = int(time.time() - stats["start_ts"]) if (is_live and stats["start_ts"]) else 0

    return {
        "code": 0,
        "data": {
            "is_live": is_live,
            "session_id": global_live_controller.session_id,
            "mode": mode,
            "platform": global_live_controller.platform if is_live else None,
            "anchor_name": anchor_name or active_role.role_name,
            "role_name": active_role.role_name,
            "role_type": active_role.role_type,
            "duration_sec": duration_sec,
            "viewer_count": stats["viewer_count"] if is_live else 0,
            "peak_viewer_count": stats["peak_viewer_count"] if is_live else 0,
            "gift_income_yuan": stats["gift_income_yuan"] if is_live else 0.0,
            "gift_count": stats["gift_count"] if is_live else 0,
            "gmv_yuan": stats["gmv_yuan"] if is_live else 0.0,
            "orders_count": stats["orders_count"] if is_live else 0,
            "danmaku_count": stats["danmaku_count"] if is_live else 0,
            "guardrail_hits": stats["guardrail_hits"] if is_live else 0,
            "flash_sales": stats["flash_sales"] if is_live else 0,
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


# ---------------------------------------------------------------------------
# 开播前检查 (Preflight)：真实探测各项开播条件，杜绝盲目引导 (v1.1.3)
# ---------------------------------------------------------------------------
OBS_INSTALL_PATHS = [
    r"C:\Program Files\obs-studio\bin\64bit\obs64.exe",
    r"C:\Program Files (x86)\obs-studio\bin\64bit\obs64.exe",
    r"C:\Program Files\obs-studio\bin\32bit\obs32.exe",
]

# 模式对本地硬件的需求强度 (A 最重)
MODE_DEMAND_RANK = {"A": 3, "B": 2, "C": 1, "D": 0}


def _pf(status: str, key: str, title: str, message: str, fix_hint: str = "", action_tab: Optional[str] = None) -> dict:
    return {
        "status": status,  # pass / warn / fail
        "key": key,
        "title": title,
        "message": message,
        "fix_hint": fix_hint,
        "action_tab": action_tab,  # 前端一键跳转处理的页签
    }


async def _pf_ping(url: str, headers: dict = None, timeout: float = 4.0):
    """轻量连通性探测，返回 HTTP 状态码或 None(不可达)"""
    import httpx
    try:
        async with httpx.AsyncClient(timeout=timeout, verify=True) as client:
            resp = await client.get(url, headers=headers or {})
            return resp.status_code
    except Exception:
        return None


async def _pf_llm_check(cfg: Optional[dict]) -> dict:
    """大模型 (AI 大脑) 真实可用性检查"""
    if not cfg:
        return _pf("warn", "llm", "大模型 (AI 大脑)",
                   "尚未配置大模型，AI 将使用内置离线话术应答，互动效果有限",
                   "前往【云端模型】启用大模型 (如 DeepSeek) 或本地 Ollama", "settings")
    provider = (cfg["provider_name"] or "").lower()
    base = (cfg["base_url"] or "").rstrip("/")

    if "ollama" in provider:
        code = await _pf_ping(base + "/api/tags" if base else "http://127.0.0.1:11434/api/tags")
        if code == 200:
            return _pf("pass", "llm", "大模型 (AI 大脑)", f"本地 Ollama 服务连通正常 ({cfg['model_name']})")
        status = "不可达" if code is None else f"HTTP {code}"
        return _pf("fail", "llm", "大模型 (AI 大脑)",
                   f"已选择本地 Ollama，但服务检查失败 ({status})，AI 将降级为离线话术",
                   "请先启动 Ollama (ollama serve) 并拉取模型，或改用云端 API", "settings")

    # 云端 OpenAI 兼容接口
    if not cfg["api_key"]:
        return _pf("fail", "llm", "大模型 (AI 大脑)",
                   "已启用云端大模型但未填写 API Key，AI 将降级为离线话术",
                   "在【云端模型】的大模型卡片填入 sk- 开头的密钥", "settings")
    code = await _pf_ping(base + "/models", {"Authorization": f"Bearer {cfg['api_key']}"})
    if code == 200:
        return _pf("pass", "llm", "大模型 (AI 大脑)", f"大模型 API 连通正常 ({cfg['model_name']})")
    if code == 401:
        message = "API Key 无效或已过期 (HTTP 401)，AI 将降级为离线话术"
        hint = "请到服务商开放平台重新生成密钥并更新"
    elif code is None:
        message = "无法连通大模型服务 (超时或地址错误)，AI 将降级为离线话术"
        hint = "检查 Base URL 是否正确、本机网络/代理是否可用"
    else:
        message = f"大模型服务检查失败 (HTTP {code})，AI 将降级为离线话术"
        hint = "检查 Base URL、API 权限、账户额度和服务商状态"
    return _pf("fail", "llm", "大模型 (AI 大脑)", message, hint, "settings")


async def _pf_tts_check(cfg: Optional[dict]) -> dict:
    """语音合成 (TTS) 真实可用性检查"""
    if not cfg:
        return _pf("warn", "tts", "语音合成 (TTS)",
                   "未选择语音服务，开播将自动使用微软免费语音 (Edge-TTS)",
                   "如需专属克隆音色，可在【云端模型】启用", "settings")
    provider = (cfg["provider_name"] or "").lower()
    base = (cfg["base_url"] or "").rstrip("/")

    if "edge" in provider:
        return _pf("pass", "tts", "语音合成 (TTS)", "微软免费语音 (Edge-TTS) 就绪，零配置零费用")
    if "cosyvoice" in provider:
        if not base:
            return _pf("warn", "tts", "语音合成 (TTS)",
                       "已启用 CosyVoice 但未填写服务地址，将回退微软免费语音",
                       "在【云端模型】填入本地 CosyVoice 服务地址", "settings")
        code = await _pf_ping(base + "/")
        if code is not None and 200 <= code < 300:
            return _pf("pass", "tts", "语音合成 (TTS)", "本地 CosyVoice 声音克隆服务已就绪")
        status = "不可达" if code is None else f"HTTP {code}"
        return _pf("warn", "tts", "语音合成 (TTS)",
                   f"本地 CosyVoice 服务检查失败 ({status})，开播将自动回退微软免费语音 (克隆音色不可用)",
                   "启动本地 CosyVoice 推理服务，检查服务地址，或改选免费语音", "settings")
    return _pf("pass", "tts", "语音合成 (TTS)", "语音服务已配置")


def _pf_obs_check() -> dict:
    """OBS 推流软件检测：进程运行 > 已安装未运行 > 未安装"""
    try:
        import psutil
        for proc in psutil.process_iter(["name"]):
            try:
                name = (proc.info.get("name") or "").lower()
                if name in ("obs64.exe", "obs32.exe", "obs"):
                    return _pf("pass", "obs", "OBS 本机组件", "检测到 OBS 进程正在运行；是否已捕获本地音视频源仍需人工确认")
            except Exception:
                continue
    except Exception:
        pass
    for path in OBS_INSTALL_PATHS:
        if Path(path).exists():
            return _pf("warn", "obs", "OBS 推流软件",
                       "检测到 OBS 已安装但尚未运行（仿真试播不受影响，正式对外推流前请启动 OBS）",
                       "请启动 OBS Studio 并将视频源设为『OBS 虚拟摄像头』", None)
    return _pf("warn", "obs", "OBS 推流软件",
               "未检测到 OBS（本机仿真试播不受影响，正式对外推流需要 OBS 抓取画面）",
               "免费下载安装 OBS Studio: obsproject.com", None)


@router.get("/preflight")
async def preflight_check(db: AsyncSession = Depends(get_db)):
    """
    开播前真实检查 (v1.1.3)：
    模式/角色/大模型连通/TTS连通/OBS/带货商品/娱乐主题/硬件匹配/违禁词库
    全部基于真实状态探测，检查未通过时给出可执行的修复指引
    """
    from sqlalchemy import func
    from server.database.models import ApiProviderConfig
    from server.routes.settings import SETTING_KEY_LIVE_MODE, LIVE_MODES, _get_setting
    from server.config import decrypt_secret

    # ---- 集中读取 DB 状态 (AsyncSession 禁止并发查询) ----
    mode = await _get_setting(db, SETTING_KEY_LIVE_MODE)
    theme = await _get_setting(db, "live_theme") or ""

    llm_cfg = tts_cfg = None
    res = await db.execute(
        select(ApiProviderConfig).where(ApiProviderConfig.config_group == "llm", ApiProviderConfig.is_active == 1)
    )
    row = res.scalars().first()
    if row:
        llm_cfg = {
            "provider_name": row.provider_name,
            "base_url": row.base_url or "",
            "model_name": row.model_name or "",
            "api_key": decrypt_secret(row.encrypted_api_key) if row.encrypted_api_key else "",
        }
    res = await db.execute(
        select(ApiProviderConfig).where(ApiProviderConfig.config_group == "tts", ApiProviderConfig.is_active == 1)
    )
    row = res.scalars().first()
    if row:
        tts_cfg = {
            "provider_name": row.provider_name,
            "base_url": row.base_url or "",
            "model_name": row.model_name or "",
        }

    role = global_role_manager.get_active_role()

    product_count = 0
    if role.role_type == "ecommerce":
        res = await db.execute(select(func.count()).select_from(Product).where(Product.is_active == 1))
        product_count = res.scalar() or 0

    # ---- 网络探测并行执行 ----
    llm_check, tts_check = await asyncio.gather(_pf_llm_check(llm_cfg), _pf_tts_check(tts_cfg))

    checks = []

    # 1. 直播模式
    if mode and mode in LIVE_MODES:
        checks.append(_pf("pass", "mode", "直播模式", f"{mode}. {LIVE_MODES[mode]['name']} · {LIVE_MODES[mode]['cost']}"))
    else:
        checks.append(_pf("fail", "mode", "直播模式",
                          "尚未选择直播运行模式",
                          "在【开播向导】第一步选择模式 (系统已按硬件自动推荐)", "wizard"))

    # 2. 主播角色
    checks.append(_pf("pass", "role", "主播角色", f"当前主播：{role.role_name} ({role.role_type})"))

    # 3 & 4. 大模型与语音 (真实连通探测)
    checks.append(llm_check)
    checks.append(tts_check)

    # 5. OBS 仅探测本机组件，不代表平台已收流
    checks.append(_pf_obs_check())
    checks.append(_pf(
        "warn", "external_publish", "外部平台发布验收",
        "系统只启动本地直播源，不管理 OBS/直播伴侣的平台发布；平台是否有声有画尚未验证",
        "请在 OBS 或平台直播伴侣中选择本机视频/音频源，并人工确认平台预览与回看", None,
    ))

    # 6. 带货主播必须有商品
    if role.role_type == "ecommerce":
        if product_count > 0:
            checks.append(_pf("pass", "products", "商品库", f"{product_count} 款商品就绪，可正常带货促单"))
        else:
            checks.append(_pf("fail", "products", "商品库",
                              "带货主播还没有上架任何商品，开播后将无货可卖",
                              "前往【商品管理】添加商品 (标题/价格/库存)", "products"))

    # 7. 娱乐/闲聊主播建议配置今日直播主题
    if role.role_type in ("entertainment", "chitchat"):
        if theme:
            checks.append(_pf("pass", "theme", "今日直播主题", f"主题：{theme}"))
        else:
            checks.append(_pf("warn", "theme", "今日直播主题",
                              "尚未设置今日直播主题，冷场时 AI 缺少话题锚点",
                              "在【开播向导】第三步填写今日主题", "wizard"))

    # 8. 硬件与所选模式匹配度
    gpu = await asyncio.to_thread(_probe_gpu)
    rec_tier = _recommend_tier(gpu)
    rec_code = rec_tier.split(" ")[1] if rec_tier.startswith("Tier") else "D"
    if mode and mode in MODE_DEMAND_RANK and MODE_DEMAND_RANK[mode] > MODE_DEMAND_RANK.get(rec_code, 0):
        checks.append(_pf("warn", "hardware", "硬件与模式匹配",
                          f"您的硬件更适合 {rec_code} 档 (当前选 {mode} 档，本地渲染可能卡顿)",
                          "可回到【开播向导】更换为推荐档位", "wizard"))
    else:
        gpu_name = gpu.get("gpu_name") or "未检测到独立显卡"
        checks.append(_pf("pass", "hardware", "硬件与模式匹配",
                          f"{gpu_name} · 推荐 {rec_code} 档 / 已选 {mode or '未设置'} 档，匹配无冲突"))

    # 9. 违禁词合规护栏
    guardrail_count = len(global_guardrail.word_meta) if global_guardrail.is_ready else 0
    if guardrail_count > 0:
        checks.append(_pf("pass", "guardrails", "违禁词护栏", f"已加载 {guardrail_count} 条违禁词实时拦截"))
    else:
        checks.append(_pf("warn", "guardrails", "违禁词护栏",
                          "违禁词库为空，直播缺少合规护栏",
                          "前往【违禁词汇】添加 (如广告法极限词)", "guardrails"))

    fails = sum(1 for c in checks if c["status"] == "fail")
    warns = sum(1 for c in checks if c["status"] == "warn")
    passes = sum(1 for c in checks if c["status"] == "pass")

    return {
        "code": 0,
        "data": {
            "ready": fails == 0,
            "local_ready": fails == 0,
            "external_publish": {
                "status": "not_managed",
                "validation": "pending_external_acceptance",
            },
            "summary": f"{passes} 项通过 · {warns} 项建议/待外部验收 · {fails} 项未通过",
            "checks": checks
        }
    }
