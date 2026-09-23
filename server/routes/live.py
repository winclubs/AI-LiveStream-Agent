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
import psutil
from collections import deque
from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Form, WebSocket, WebSocketDisconnect, Response, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from typing import Optional, Dict, Any, AsyncGenerator
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

logger = logging.getLogger("LiveAgent.LiveController")
router = APIRouter(prefix="/live", tags=["直播控场与调度"])

OBS_SETTING_HOST = "obs_websocket_host"
OBS_SETTING_PORT = "obs_websocket_port"
OBS_SETTING_PASSWORD = "obs_websocket_password"
OBS_SETTING_AUTO_CONNECT = "obs_auto_connect"


def _pcm_s16le_to_wav(pcm: bytes, sample_rate: int, channels: int) -> bytes:
    """将裸 PCM16 封装为浏览器可独立播放的完整 WAV 容器。"""
    output = io.BytesIO()
    with wave.open(output, "wb") as wav_file:
        wav_file.setnchannels(int(channels))
        wav_file.setsampwidth(2)
        wav_file.setframerate(int(sample_rate))
        wav_file.writeframes(pcm)
    return output.getvalue()


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
        self.current_tts_task: Optional[asyncio.Task] = None
        self.cleanup_errors: list = []
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
                await asyncio.wait_for(previous_orchestrator.stop(), timeout=4.0)
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

    async def start(self, session_id: str, products: list, room_id: Optional[str] = None, platform: str = "bilibili", theme: str = "", voice_id: Optional[str] = None, avatar_path: str = "", mode: str = "B", landmarks_path: str = "", demo_mode: bool = False):
        """事务式启动资源；任一步失败都回滚已启动任务和设备。"""
        try:
            await self._start_resources(
                session_id, products, room_id, platform, theme, voice_id,
                avatar_path, mode, landmarks_path, demo_mode=demo_mode
            )
        except Exception:
            logger.exception("直播资源启动失败，正在回滚")
            await self.stop()
            raise

    async def _start_resources(self, session_id: str, products: list, room_id: Optional[str] = None, platform: str = "bilibili", theme: str = "", voice_id: Optional[str] = None, avatar_path: str = "", mode: str = "B", landmarks_path: str = "", demo_mode: bool = False):
        if self.is_live:
            return
        self.is_live = True
        self.demo_mode = demo_mode
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

        # 启动数字人渲染引擎与音画帧管道 (根据模式、主播底图与人脸关键点)
        global_media_router.select_driver(mode=mode, avatar_path=avatar_path, landmarks_path=landmarks_path)
        await global_media_router.start()

        # 启动 VRAM 看门狗 (无 GPU 环境自动空转)
        self.vram_watchdog.start()
        # 启动低配机 CPU/内存自适应降频看门狗 (保障音频算力优先)
        from server.core.monitoring.system_resource_watchdog import global_resource_watchdog
        global_resource_watchdog.start()

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

            # 2. 取消主消费、场观、视觉任务
            session_tasks = [
                task for task in (self.worker_task, self.viewer_task, self.vision_task)
                if task and not task.done()
            ]
            for task in session_tasks:
                task.cancel()
            if session_tasks:
                await asyncio.gather(*session_tasks, return_exceptions=True)
            self.worker_task = self.viewer_task = self.vision_task = None

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

            # 6. 停止摄像头、声卡和视觉感知设备
            await self._safe_close("vision", global_vision.close, timeout=2.0)
            self.live_context.pop("vision_image_b64", None)
            if hasattr(self, "vram_watchdog") and self.vram_watchdog:
                await self._safe_close("vram_watchdog", self.vram_watchdog.stop, timeout=2.0)
            from server.core.monitoring.system_resource_watchdog import global_resource_watchdog
            await self._safe_close("resource_watchdog", global_resource_watchdog.stop, timeout=2.0)

            # 7. 清空事件队列
            await self._safe_close("event_queue", self.event_queue.clear, timeout=1.0)

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
                finally:
                    # 无论正常结束、打断 break 还是异常，强制广播复位平息态
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
                if not self.is_live:
                    break
                logger.warning("直播消费循环收到打断或取消信号，但会话仍在进行中，自动保持消费主循环存活")
                await asyncio.sleep(0.05)
            except Exception as e:
                logger.error(f"直播主循环异常: {e}", exc_info=True)
                await asyncio.sleep(0.5)

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
        """完整合成后一次性提交音频，并在每个关键 await 后复核双代际。TTS 生成创建独立子任务，绝不污染长期 Worker。"""
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

        codec = getattr(driver, "audio_codec", "mp3")
        mime_type = getattr(driver, "audio_mime_type", "audio/mpeg")
        sample_rate = int(getattr(driver, "audio_sample_rate", 24000))
        channels = int(getattr(driver, "audio_channels", 1))
        pts_ms = int(time.monotonic() * 1000)
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
                        get_webrtc_stream_manager().push_audio(pcm_for_stream)
                    except Exception:
                        pass
                    try:
                        from server.core.avatar.task_manager import get_record_manager
                        rec_mgr = get_record_manager()
                        if rec_mgr and getattr(rec_mgr, "is_recording", lambda: False)():
                            rec_mgr.feed_audio(pcm_for_stream)
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

def _on_obs_external_state_change(active: bool, state: str, epoch: Optional[int] = None):
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
        "external_publish": _build_external_publish_status(),
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
        is_demo = bool(getattr(req, "demo_mode", False))
        await global_live_controller.start(
            session_id, products, target_room, platform=platform, theme=live_theme,
            voice_id=target_voice_id, avatar_path=avatar_path, mode=live_mode,
            landmarks_path=landmarks_path, demo_mode=is_demo
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
    except Exception as exc:
        await db.rollback()
        record = await db.get(LiveSessionRecord, session_id)
        if record:
            setattr(record, "status", "failed")
            setattr(record, "end_time", utc_now())
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
        "obs_linked": obs_linked,
        "degraded": obs_degraded,
        "obs_error": obs_error_msg,
        "local_source": {"status": "running", "preview": "/api/v1/live/stream/preview"},
        "external_publish": _build_external_publish_status(),
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


# ------------------------------------------------------------------
# OBS Studio WebSocket v5 联动控制端点
# ------------------------------------------------------------------
@router.get("/obs/status")
async def get_obs_status():
    """获取当前 OBS-WebSocket 状态；刷新失败时保留最后可信状态并显式标 stale。"""
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
    await _set_obs_auto_connect(db, False)
    await global_obs_client.disconnect()
    return {"code": 0, "message": "OBS 连接已断开"}


@router.post("/obs/stream/start")
async def start_obs_stream():
    """控制 OBS 开始推流"""
    if not global_obs_client.is_connected:
        raise HTTPException(status_code=400, detail="OBS 未连接，请先连接 OBS")
    res = await global_obs_client.start_stream()
    return {"code": 0 if res.get("result") else 1, "data": res}


@router.post("/obs/stream/stop")
async def stop_obs_stream():
    """控制 OBS 停止推流"""
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
    intent_keywords = ["多少钱", "怎么买", "发货", "优惠", "包邮", "领券", "链接", "库存", "正品", "拍了", "保修"]
    text_content = payload.text or ""
    if event_type == "gift":
        priority = 0 if (payload.total_coin or 0) >= 50000 else 1
    elif any(kw in text_content for kw in intent_keywords):
        priority = 1
    else:
        priority = 2
    res = await global_live_controller.ingest_event(
        event_type=event_type,
        user_name=payload.user_name,
        payload=danmaku_payload,
        priority=priority,
        source="webhook",
        is_mock=False,
    )
    return {"code": 0 if res.get("accepted") else 1, "message": "弹幕已接收并进入调度队列", "data": res}

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


class MockEventRequest(BaseModel):
    type: str = "chat"  # chat(促单提问 P1) / gift(大额打赏 P0 打断)


@router.post("/mock-event")
async def inject_mock_event(req: MockEventRequest):
    """向直播队列注入仿真观众事件 (演示/联调按钮的真实数据通路，强制标记 is_mock 排除真实指标统计)"""
    if not global_live_controller.is_live:
        raise HTTPException(status_code=400, detail="直播间尚未开播，请先在直播大屏一键开播")

    if req.type == "gift":
        res = await global_live_controller.ingest_event(
            event_type="gift",
            user_name="仿真大哥888",
            payload={"gift_name": "超级大火箭", "count": 1, "total_coin": 100000},
            priority=0,
            source="mock",
            is_mock=True,
        )
        return {"code": 0, "message": "已注入仿真大额打赏 (P0 强打断)，AI 主播将立刻感谢", "data": res}
    else:
        res = await global_live_controller.ingest_event(
            event_type="chat",
            user_name="仿真买家小美",
            payload={"text": "主播，这款多少钱？现在还有优惠吗？"},
            priority=1,
            source="mock",
            is_mock=True,
        )
        return {"code": 0, "message": "已注入仿真促单提问 (P1)，AI 主播将优先解答", "data": res}

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
_GPU_PROBE_CACHE: Dict[str, Any] = {
    "torch_checked": False, "torch": None,
    "wmi_checked": False, "wmi_info": None,
}


def _get_torch() -> Any:
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
    default_info = {"gpu_name": None, "vram_total_gb": 0.0}
    if _GPU_PROBE_CACHE["wmi_checked"]:
        cached = _GPU_PROBE_CACHE.get("wmi_info")
        return cached if isinstance(cached, dict) else default_info
    _GPU_PROBE_CACHE["wmi_checked"] = True
    _GPU_PROBE_CACHE["wmi_info"] = dict(default_info)
    try:
        ps_cmd = "Get-CimInstance Win32_VideoController | Select-Object Name, AdapterRAM, DriverVersion | ConvertTo-Json"
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps_cmd],
            capture_output=True, text=True, timeout=8
        )
        if out.returncode == 0 and out.stdout.strip():
            raw_data = json.loads(out.stdout)
            items = raw_data if isinstance(raw_data, list) else [raw_data]
            ignore_keywords = {"oray", "virtual", "basic display", "idddriver", "remote", "microsoft 基本显示"}
            valid_gpus = []
            for item in items:
                name = (item.get("Name") or "").strip()
                if not name or any(k in name.lower() for k in ignore_keywords):
                    continue
                ram = item.get("AdapterRAM") or 0
                valid_gpus.append((ram, name))

            if valid_gpus:
                valid_gpus.sort(key=lambda x: x[0], reverse=True)
                best_ram, best_name = valid_gpus[0]
                info = {"gpu_name": best_name, "vram_total_gb": 0.0}
                if 0 < best_ram <= 4 * (1024 ** 3):
                    info["vram_total_gb"] = round(best_ram / (1024 ** 3), 2)
                _GPU_PROBE_CACHE["wmi_info"] = info
    except Exception:
        pass
    cached = _GPU_PROBE_CACHE.get("wmi_info")
    return cached if isinstance(cached, dict) else default_info



def _probe_gpu() -> dict:
    """
    三级探测本机 GPU：
      1. PyTorch CUDA (最精确，实时显存)
      2. nvidia-smi (自动搜索常见安装路径，实时显存)
      3. WMI Win32_VideoController (兜底识别物理显卡型号)
    """
    info: Dict[str, Any] = {"gpu_name": None, "vram_total_gb": 0.0, "vram_used_gb": 0.0, "cuda_available": False}

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
_HW_PAYLOAD_CACHE: Dict[str, Any] = {"ts": 0.0, "data": None}
_HW_CACHE_TTL_SEC = 3.0


async def _hardware_payload() -> dict:
    import platform
    now = time.time()
    cached_data = _HW_PAYLOAD_CACHE.get("data")
    last_ts = float(_HW_PAYLOAD_CACHE.get("ts") or 0.0)
    if isinstance(cached_data, dict) and now - last_ts < _HW_CACHE_TTL_SEC:
        # 动态指标 (CPU/内存占用) 实时更新，静态探测 (GPU/型号) 走缓存
        cached = dict(cached_data)
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
    from server.core.hardware.gpu_capability import evaluate_compute
    compute_plan = await evaluate_compute(feature_name="全局高性能计算", required_vram_gb=2.0)
    cpu_name = platform.processor() or os.environ.get("PROCESSOR_IDENTIFIER", "") or "未知处理器"

    payload = {
        "cpu_name": cpu_name,
        "cpu_cores": psutil.cpu_count(logical=True) or 0,
        "cpu_percent": cpu_percent,
        "ram_used_gb": round(memory.used / (1024 ** 3), 2),
        "ram_total_gb": round(memory.total / (1024 ** 3), 2),
        "ram_percent": memory.percent,
        "gpu": gpu_info,
        "gpu_capability": compute_plan.to_dict(),
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


async def _pf_ping(url: str, headers: Optional[dict] = None, timeout: float = 4.0) -> Optional[int]:
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
                   "前往【LLM 配置(1)】启用大模型 (如 DeepSeek) 或本地 Ollama", "settings_llm")
    provider = (cfg.get("provider_name") or "").lower()
    base = (cfg.get("base_url") or "").rstrip("/")

    if "ollama" in provider:
        code = await _pf_ping(base + "/api/tags" if base else "http://127.0.0.1:11434/api/tags")
        if code == 200:
            return _pf("pass", "llm", "大模型 (AI 大脑)", f"本地 Ollama 服务连通正常 ({cfg.get('model_name')})")
        status = "不可达" if code is None else f"HTTP {code}"
        return _pf("fail", "llm", "大模型 (AI 大脑)",
                   f"已选择本地 Ollama，但服务检查失败 ({status})，AI 将降级为离线话术",
                   "请先启动 Ollama (ollama serve) 并拉取模型，或改用云端 API", "settings_llm")

    # 云端 OpenAI 兼容接口
    if not cfg.get("api_key"):
        return _pf("fail", "llm", "大模型 (AI 大脑)",
                   "已启用云端大模型但未填写 API Key，AI 将降级为离线话术",
                   "在【LLM 配置(1)】的大模型卡片填入 sk- 开头的密钥", "settings_llm")
    code = await _pf_ping(base + "/models", {"Authorization": f"Bearer {cfg['api_key']}"})
    if code == 200:
        return _pf("pass", "llm", "大模型 (AI 大脑)", f"大模型 API 连通正常 ({cfg.get('model_name')})")
    if code == 401:
        message = "API Key 无效或已过期 (HTTP 401)，AI 将降级为离线话术"
        hint = "请到服务商开放平台重新生成密钥并在【LLM 配置(1)】更新"
    elif code is None:
        message = "无法连通大模型服务 (超时或地址错误)，AI 将降级为离线话术"
        hint = "检查 Base URL 是否正确、本机网络/代理是否可用"
    else:
        message = f"大模型服务检查失败 (HTTP {code})，AI 将降级为离线话术"
        hint = "检查 Base URL、API 权限、账户额度和服务商状态"
    return _pf("fail", "llm", "大模型 (AI 大脑)", message, hint, "settings_llm")


async def _pf_tts_check(cfg: Optional[dict]) -> dict:
    """语音合成 (TTS) 真实可用性检查"""
    if not cfg:
        return _pf("warn", "tts", "语音合成 (TTS)",
                   "未选择语音服务，开播将自动使用微软免费语音 (Edge-TTS)",
                   "如需专属克隆音色，可在【TTS 语音(3)】启用", "settings_tts")
    provider = (cfg.get("provider_name") or "").lower()
    base = (cfg.get("base_url") or "").rstrip("/")
    api_key = cfg.get("api_key") or ""
    model_name = cfg.get("model_name") or "cosyvoice-v3.5-flash"

    if "edge" in provider:
        return _pf("pass", "tts", "语音合成 (TTS)", "微软免费语音 (Edge-TTS) 就绪，零配置零费用")

    # 阿里云百炼 CosyVoice 云端音色克隆通道 (包含 aliyuncs / dashscope / maas 或云端域名)
    is_bailian_cloud = any(k in base.lower() for k in ["aliyuncs.com", "dashscope", "maas"]) or "bailian" in provider
    if "cosyvoice" in provider or is_bailian_cloud:
        if is_bailian_cloud:
            return _pf("pass", "tts", "语音合成 (TTS)", f"阿里云百炼 CosyVoice 云端音色克隆已就绪 ({model_name})")

        # 否则属于本地私有化部署的 CosyVoice 服务 (如 127.0.0.1:50000 / 9233 等)
        if not base:
            return _pf("warn", "tts", "语音合成 (TTS)",
                       "已启用本地 CosyVoice 但未填写服务地址，开播将自动回退微软免费语音",
                       "请前往【TTS 语音(3)】填入本地 CosyVoice 服务地址", "settings_tts")
        code = await _pf_ping(base + "/")
        if code is not None and 200 <= code < 300:
            return _pf("pass", "tts", "语音合成 (TTS)", "本地 CosyVoice 声音克隆服务已就绪")
        status = "不可达" if code is None else f"HTTP {code}"
        return _pf("warn", "tts", "语音合成 (TTS)",
                   f"本地 CosyVoice 服务检查失败 ({status})，开播将自动回退微软免费语音 (克隆音色不可用)",
                   "请启动本地 CosyVoice 推理服务，或在【TTS 语音(3)】改选微软免费语音", "settings_tts")

    return _pf("pass", "tts", "语音合成 (TTS)", f"语音服务已配置 ({cfg.get('provider_name')})")


def _pf_obs_check() -> dict:
    """OBS 推流软件检测 (SSOT 统一探测)：进程运行 > 已安装未运行 > 未安装"""
    from server.routes.system import find_obs_studio
    obs_info = find_obs_studio()
    if obs_info["is_running"]:
        return _pf("pass", "obs", "OBS 本机组件", "检测到 OBS 进程正在运行；是否已捕获本地音视频源仍需人工确认")
    if obs_info["is_installed"]:
        ver_str = f" (v{obs_info['version']})" if obs_info.get("version") else ""
        return _pf("warn", "obs", "OBS 推流软件",
                   f"检测到 OBS 已安装{ver_str}但尚未运行（本机仿真试播不受影响，正式对外推流前请启动 OBS）",
                   "请启动 OBS Studio 并将视频源设为『OBS 虚拟摄像头』", None)
    return _pf("warn", "obs", "OBS 推流软件",
               "未检测到 OBS（本机仿真试播不受影响，正式对外推流需要 OBS 抓取画面）",
               "免费下载安装 OBS Studio: obsproject.com", None)


def _pf_avatar_check(configs: list) -> dict:
    """校验本地 Avatar 能力、远端配置与现有运行态；不在 GET 中启动远端连接。"""
    from server.adapters.media.avatar_provider_registry import (
        AvatarProviderAvailability,
        get_avatar_provider_descriptor,
        normalize_avatar_provider_config,
    )

    try:
        media_status = global_media_router.get_preview_status()
    except Exception as exc:
        logger.warning("Avatar preflight 状态读取失败: %s", exc)
        media_status = {}
    local_available = media_status.get("cv_available") is True

    if not configs:
        if local_available:
            return _pf(
                "pass", "avatar", "数字人画面与云渲染",
                "本地程序化 Avatar 可用；未启用远端 Provider，远端渲染为可选增强",
            )
        return _pf(
            "warn", "avatar", "数字人画面与云渲染",
            "本地 OpenCV 程序化 Avatar 不可用，当前只能使用 mock 画面；未启用远端 Provider",
            "安装 OpenCV 运行依赖，或在【开播向导】配置可用的 Avatar Provider", "wizard",
        )

    valid_configs = []
    invalid_configs = []
    for config in configs:
        try:
            extra = json.loads(config.extra_params_json or "{}")
            if not isinstance(extra, dict):
                raise ValueError("extra_params 必须是 object")
            adapter_id = str(extra.get("adapter") or "").strip().lower()
            descriptor = get_avatar_provider_descriptor(adapter_id)
            if descriptor.availability is AvatarProviderAvailability.PLANNED:
                raise ValueError(f"{descriptor.display_name} 仍为计划支持，尚未交付或验收")
            canonical = normalize_avatar_provider_config(
                extra,
                base_url=config.base_url,
                credential_present=bool(config.encrypted_api_key),
            )
            valid_configs.append((config, descriptor, canonical))
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            invalid_configs.append(f"{config.id}: {exc}")

    if invalid_configs:
        detail = "；".join(invalid_configs[:3])
        return _pf(
            "fail", "avatar", "数字人画面与云渲染",
            f"启用的远端 Avatar 配置无效：{detail}。本地程序化 shadow 仍保留，但该配置不会被冒充为可用节点",
            "在【云端模型】修复或停用无效的 Avatar Provider 实例", "settings",
        )

    provider_summary = media_status.get("avatar_provider") or {}
    provider_states = {
        state.get("provider_id"): state
        for state in (media_status.get("avatar_providers") or [])
        if isinstance(state, dict) and state.get("provider_id")
    }
    orchestrator_running = provider_summary.get("running") is True
    healthy_ids = []
    unverified_ids = []
    unavailable_ids = []

    for config, descriptor, _canonical in valid_configs:
        provider_id = f"neural_renderer:{config.id}"
        state = provider_states.get(provider_id)
        if descriptor.availability is AvatarProviderAvailability.EXPERIMENTAL:
            unverified_ids.append(config.id)
            continue
        if state is None:
            unavailable_ids.append(config.id)
            continue
        capabilities = state.get("capabilities") or {}
        adapter = state.get("adapter") or {}
        verified = (
            capabilities.get("verification") == "verified"
            and capabilities.get("eligible_for_auto") is True
        )
        healthy = bool(
            orchestrator_running
            and state.get("up") is True
            and state.get("ready") is True
            and verified
            and state.get("circuit_state") != "open"
            and adapter.get("remote_ready") is True
            and adapter.get("degraded") is not True
        )
        if healthy:
            healthy_ids.append(config.id)
        elif not verified:
            unverified_ids.append(config.id)
        else:
            unavailable_ids.append(config.id)

    local_note = "本地程序化 shadow 可用" if local_available else "本地程序化画面当前不可用"
    if healthy_ids:
        if not local_available:
            return _pf(
                "warn", "avatar", "数字人画面与云渲染",
                f"{len(healthy_ids)} 个远端 renderer-only Provider 已通过当前运行时握手，但本地程序化 shadow 不可用；远端故障时只能降级为 mock 画面",
                "安装本地 OpenCV 运行依赖以恢复热 shadow；外部平台画面仍需单独验收", "wizard",
            )
        return _pf(
            "pass", "avatar", "数字人画面与云渲染",
            f"{local_note}；{len(healthy_ids)} 个远端 renderer-only Provider 已通过当前运行时握手与能力门禁。外部平台画面仍需单独验收",
        )
    if unverified_ids:
        return _pf(
            "warn", "avatar", "数字人画面与云渲染",
            f"{local_note}；{len(unverified_ids)} 个远端 Provider 仍为 experimental/unverified，不进入自动渲染池，也不能视为已验收",
            "仅在沙箱完成音频、完成、中断、视频轨和时间戳验收后，才能升级 Provider 状态", "wizard",
        )
    if not orchestrator_running:
        return _pf(
            "warn", "avatar", "数字人画面与云渲染",
            f"{local_note}；{len(valid_configs)} 个远端 Provider 配置有效，但尚未开播或编排器未启动，需在运行时握手后确认",
            "可先使用本地程序化画面开播；远端节点状态将在运行后重新检查", "wizard",
        )
    return _pf(
        "warn", "avatar", "数字人画面与云渲染",
        f"{local_note}；远端 Provider 当前未就绪或已降级，未将其标记为通过",
        "检查 Provider 运行状态、鉴权、网络、熔断与额度；本地 shadow 可继续承接画面", "settings",
    )


@router.get("/preflight")
async def preflight_check(db: AsyncSession = Depends(get_db)):
    """
    开播前真实检查：
    模式/角色/大模型/TTS/Avatar/OBS/商品或主题/硬件/违禁词库
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
            "api_key": decrypt_secret(str(row.encrypted_api_key)) if row.encrypted_api_key else "",
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
            "api_key": decrypt_secret(str(row.encrypted_api_key)) if row.encrypted_api_key else "",
        }

    res = await db.execute(
        select(ApiProviderConfig)
        .where(
            ApiProviderConfig.config_group == "neural_renderer",
            ApiProviderConfig.is_active == 1,
        )
        .order_by(ApiProviderConfig.id.asc())
    )
    avatar_configs = list(res.scalars().all())

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
                              "在【开播向导】第四步填写今日主题", "wizard"))

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

    # 10. 高性能显卡与云端算力智能调度研判 (按用户战略规则优先云端调度)
    from server.core.hardware.gpu_capability import evaluate_compute
    cap_plan = await evaluate_compute(feature_name="数字人高保真口型渲染", required_vram_gb=2.0)
    if cap_plan.use_cloud:
        cloud_pname = cap_plan.cloud_gpu.get("provider_name", "Sidecar") if cap_plan.cloud_gpu else "Sidecar"
        checks.append(_pf(
            "pass", "gpu_cloud_dispatch", "高性能显卡调度",
            f"已优先调度已配置的云端显卡算力节点 [{cloud_pname}]，本地轻量低负载运行",
        ))
    elif cap_plan.is_low_spec_local:
        checks.append(_pf(
            "warn", "gpu_cloud_dispatch", "高性能显卡调度",
            f"本地显卡仅 {cap_plan.local_gpu.get('vram_total_gb', 0)}GB 显存且未对接云端显卡。高清深度神经渲染不可用，已自动选用轻量免显卡 CPU 模式",
            "如需高清写实数字人，请前往【GPU配置(2)】添加云端显卡 (Sidecar) 节点", "settings_gpu",
        ))
    else:
        checks.append(_pf(
            "pass", "gpu_cloud_dispatch", "高性能显卡调度",
            f"本地显卡（{cap_plan.local_gpu.get('gpu_name')}，显存 {cap_plan.local_gpu.get('vram_total_gb')}GB）显存充足，支持高性能运行",
        ))

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


class RtmpStartRequest(BaseModel):
    rtmp_url: str = Field(..., description="RTMP 服务器地址 (例如 rtmp://live-push.bilivideo.com/live-bvc/)")
    stream_key: Optional[str] = Field("", description="直播码 / 推流密钥")
    width: Optional[int] = Field(720, ge=320, le=3840)
    height: Optional[int] = Field(960, ge=240, le=3840)
    fps: Optional[int] = Field(25, ge=15, le=60)
    bitrate_kbps: Optional[int] = Field(2500, ge=500, le=12000)


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
# 弹幕凭证健康度与连通性探针端点 (P2-2)
# ------------------------------------------------------------------
class DanmakuProbeRequest(BaseModel):
    platform: str = Field("bilibili", description="直播平台 (bilibili, douyin, kuaishou, mock)")
    room_id: str = Field("", description="房间号或链接")
    ttwid: Optional[str] = Field("", description="抖音 ttwid")
    ms_token: Optional[str] = Field("", description="抖音 msToken")
    cookie: Optional[str] = Field("", description="平台 Cookie")


@router.post("/danmaku/probe", summary="开播前弹幕平台与凭证连通性探测")
async def probe_danmaku_endpoint(req: DanmakuProbeRequest):
    """
    无干扰轻量探针：探测目标平台连通性、凭证有效性与环境库完整度
    """
    from server.adapters.danmaku.probe import probe_danmaku_platform
    result = await probe_danmaku_platform(
        platform=req.platform,
        room_id=req.room_id,
        ttwid=req.ttwid or "",
        ms_token=req.ms_token or "",
        cookie=req.cookie or "",
    )
    return {"code": 0, "data": result}


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

@router.websocket("/asr/ws")
async def websocket_asr_endpoint(websocket: WebSocket):
    """
    全双工麦克风音频流 WebSocket：
    客户端流式上行 16kHz 16-bit 单声道 PCM 数据，服务端 VAD 检测人声，
    触发瞬间打断与段落转写，并将识别结果作为现场提问注入直播交互。
    """
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

    session = manager.get_or_create_session(
        session_id=session_id,
        on_interrupt=_notify_interrupt,
        on_transcribe=_notify_transcribe,
    )

    try:
        while True:
            message = await websocket.receive()
            if "bytes" in message and message["bytes"]:
                pcm_chunk = message["bytes"]
                res = session.feed_pcm(pcm_chunk)
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
async def start_clip_recording(req: StartRecordRequest = StartRecordRequest()):
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
    停止录制，自动调用 FFmpeg 封装带声画同步的标准 H.264 MP4 文件
    """
    recorder_mgr = get_record_manager()
    res = recorder_mgr.stop_recording()
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


