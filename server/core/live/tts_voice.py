# -*- coding: utf-8 -*-
"""TTS 驱动选择、神经 sidecar 挂接与配置读取。

由 server/routes/live.py 的 LiveSessionController 拆出。
缝在**函数体内**从 server.routes.live 解析，见 test_live_seam_late_binding.py。
"""
import logging
import asyncio
import json
from server.adapters.media.media_router import global_media_router
from sqlalchemy import select
from typing import Optional
logger = logging.getLogger("LiveAgent.LiveController")


class TtsVoiceMixin:
    """TTS 驱动选择、神经 sidecar 挂接与配置读取"""
    async def _select_tts_driver(self, target_voice_id: Optional[str] = None):
        """
        根据主播专属音色与激活配置选择媒体驱动 (规划 §8/§9.2/§9.4)：
        1. 端云分离远程 GPU 节点 (Tier C，连通优先)
        2. 本地 CosyVoice 声音克隆 (优先匹配主播绑定的 voice_id 样本)
        3. Edge-TTS 云端兜底 (支持根据音色档案/配置选择匹配发音人)
        """
        from server.routes.live import global_role_manager
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
        from server.routes.live import EdgeTTSMediaDriver
        driver = EdgeTTSMediaDriver(voice=voice_name)
        if active_voice:
            await driver.apply_volume_gain(getattr(active_voice, "volume_gain", 1.0) or 1.0)
        return driver


    async def _configure_neural_sidecar(self) -> None:
        """加载全部 enabled renderer-only v3 Provider；不可用不影响开播。"""
        from server.routes.live import _call_async_compat
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


    async def _switch_to_edge_tts(self):
        """运行期 TTS 故障后原子卸载远程媒体源并切换 Edge 驱动。"""
        from server.routes.live import _call_async_compat, EdgeTTSMediaDriver
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
        from server.routes.live import global_media_driver
        codec = str(getattr(driver, "audio_codec", "") or "").lower()
        supports_pcm = codec in {"pcm_s16le", "pcm16", "s16le"}
        supports_renderer = bool(
            getattr(global_media_driver, "supports_progressive_audio", lambda: False)()
        )
        return supports_pcm and supports_renderer
