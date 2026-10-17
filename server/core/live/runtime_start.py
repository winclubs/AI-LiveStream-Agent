# -*- coding: utf-8 -*-
"""开播资源装载：队列/弹幕源/切片装载与帧循环。

由 server/routes/live.py 的 LiveSessionController 拆出。
缝在**函数体内**从 server.routes.live 解析，见 test_live_seam_late_binding.py。
"""
import logging
import asyncio
import json
import re
import time
from pathlib import Path
from server.adapters.danmaku.circuit_breaker import CircuitBreakerDanmakuFetcher
from server.adapters.danmaku.mock_fetcher import MockDanmakuFetcher
from server.adapters.media.media_router import global_media_router
from server.core.media.frame_bus import global_frame_bus
from server.core.media.hybrid_stitcher import HybridSliceHandle
from server.core.media.stitcher_protocol import StitcherState
from server.core.queue.priority_queue import BarrageAggregator, PriorityBargeInQueue
from sqlalchemy import select
from typing import Optional
logger = logging.getLogger("LiveAgent.LiveController")


class RuntimeStartMixin:
    """开播资源装载：队列/弹幕源/切片装载与帧循环"""
    async def _start_resources(self, session_id: str, products: list, room_id: Optional[str] = None, platform: str = "bilibili", theme: str = "", voice_id: Optional[str] = None, avatar_path: str = "", mode: str = "B", landmarks_path: str = "", demo_mode: bool = False, avatar_asset_dir: str = "", execution_mode: str = "PREVIEW"):
        from server.routes.live import _configure_media_watchdog
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
            from server.routes.live import EdgeTTSMediaDriver
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
