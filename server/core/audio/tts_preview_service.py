# -*- coding: utf-8 -*-
"""
TTS 试听音频共享合成服务。
供 /settings/tts/preview (音色试听) 与 /anchors/{id}/avatar/preview-speech-drive
(试播台词驱动) 复用，保证两处音色解析与合成行为 100% 一致。
"""
import logging
from pathlib import Path
from typing import Optional

import httpx
from fastapi import HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select

from server.config import DATA_DIR, decrypt_secret
from server.database.db import AsyncSessionLocal
from server.database.models import ApiProviderConfig, VoiceProfile

logger = logging.getLogger("LiveAgent.TtsPreview")


class PreviewSpeechParams(BaseModel):
    provider_name: Optional[str] = Field(default="edge_tts", max_length=64)
    model_name: Optional[str] = Field(default=None, max_length=128)
    voice_name: Optional[str] = Field(default=None, max_length=128)
    base_url: Optional[str] = Field(default=None, max_length=512)
    api_key: Optional[str] = Field(default=None, max_length=16_384)
    text: Optional[str] = Field(default="", max_length=2000)


# 与原 settings.py 完全一致的预置音色台词对照表 (key -> (edge_voice, profile_text))
# NOTE: 从 server/routes/settings.py 原样搬迁，保持两处行为统一。
VOICE_PREVIEW_PROFILES = {
    # MOSS-TTS-Nano 复旦开源大模型原生端点专属音色 (广播级 48kHz)
    "moss_female_host_01": ("zh-CN-XiaoxiaoNeural", "大家好！我是 MOSS-TTS 官方清亮女主播，广播级 48kHz 超高保真音质，为您呈现自然真人发音！"),
    "moss_male_host_02": ("zh-CN-YunxiNeural", "老铁们好！我是 MOSS-TTS 阳光男主播，端侧 GPU 毫秒级极速推理，开播流畅不卡顿！"),
    "moss_female_warm_03": ("zh-CN-XiaoyiNeural", "哈喽大家好！我是 MOSS-TTS 温柔知性女主播，适合美妆服饰与生活好物带货！"),
    "moss_female_lively_04": ("zh-CN-XiaoxuanNeural", "家人们看过来！我是 MOSS-TTS 活力带货女主播，超强感染力，爆单不停！"),

    # Edge-TTS 微软原生云音色
    "zh-cn-xiaoxiaoneural": ("zh-CN-XiaoxiaoNeural", "你好！我是晓晓，超自然知性女主播，很高兴为您带来高品质直播发音！"),
    "zh-cn-yunxineural": ("zh-CN-YunxiNeural", "老铁们好！我是云希，阳光活力青年男主播，祝您开播大吉，人气爆棚！"),
    "zh-cn-yunjianneural": ("zh-CN-YunjianNeural", "大家好，我是云健，沉稳质感男声，为您带来专业深度的产品解说。"),
    "zh-cn-xiaoyineural": ("zh-CN-XiaoyiNeural", "哈喽大家好！我是晓伊，活泼亲和的邻家少女声线，欢迎来到我们的直播间！"),
    "zh-cn-liaoning-xiaobeineural": ("zh-CN-liaoning-XiaobeiNeural", "哎呀老铁们好啊！我是辽宁晓北，幽默地道的东北老铁声线，点个关注不迷路！"),
    "zh-cn-shaanxi-xiaonineural": ("zh-CN-shaanxi-XiaoniNeural", "大家好！我是陕西晓妮，热情地道的特色方言，给直播间增添别样风采！"),
    "zh-cn-xiaoxuanneural": ("zh-CN-XiaoxuanNeural", "家人们！我是晓萱，激情燃播促单声线，今天的全场福利马上开抢！"),
    "zh-cn-yunxianeural": ("zh-CN-YunxiaNeural", "小朋友和大朋友们好呀！我是云夏，活泼可爱的童声主播，今天带大家玩好玩的！"),
    "zh-cn-yunyangneural": ("zh-CN-YunyangNeural", "您好，我是云扬，专业新闻播音级质感男声，呈现高端严谨的品牌形象。"),

    # 官方通用预设声线
    "voice_default_female": ("zh-CN-XiaoxiaoNeural", "大家好，欢迎来到我的直播间！我是通用亲和女主播，很高兴为您带来精选好物！"),
    "voice_default_male": ("zh-CN-YunxiNeural", "大家好，欢迎来到我的直播间！我是通用阳光男主播，祝大家购物愉快！"),

    # CosyVoice 阿里通义音色声线特征矩阵
    "longxiaochun": ("zh-CN-XiaoxiaoNeural", "你好！我是 小琴琴，知性温和的电商带货推荐声线，卖货很牛逼的那种哦，很高兴为您发声。"),
    "longlaotie": ("zh-CN-liaoning-XiaobeiNeural", "老铁们好！我是 CosyVoice 龙老铁，幽默互动带货唠嗑全拿捏，关注主播不迷路！"),
    "loongstella": ("zh-CN-XiaoxiaoNeural", "您好，我是 CosyVoice Stella，品质优雅的解说主播声线，祝您直播顺利！"),
    "loongbella": ("zh-CN-XiaoyiNeural", "哈喽大家好！我是 CosyVoice Bella，温柔知性的美妆服饰带货声线，期待陪伴您的每一场直播。"),
    "longanran": ("zh-CN-XiaoxuanNeural", "家人们！我是 CosyVoice 龙安然，激情促单燃播声线，今天的爆款福利全场炸裂！"),
    "longanxuan": ("zh-CN-XiaoyiNeural", "哈喽大家好！我是 CosyVoice 龙安萱，亲和甜美的带货声线，今天为你精选了超多好物！"),
    "longanchong": ("zh-CN-YunxiNeural", "哈喽大家！我是 CosyVoice 龙安冲，活力满满的阳光带货声线，吃喝玩乐零食专场走起！"),
    "longanping": ("zh-CN-YunjianNeural", "大家好，我是 CosyVoice 龙安平，沉稳严谨的数码家电科技声线，为您提供专业解析。"),
    "longshuo": ("zh-CN-YunyangNeural", "您好，我是 CosyVoice 龙硕，质感商务播音男声，助力高端品牌树立专业形象。"),
    "longjielidou": ("zh-CN-YunxiaNeural", "小朋友和大朋友们好呀！我是 CosyVoice 杰力豆，活泼可爱的童声主播，今天带大家玩好玩的！"),
    "longwan": ("zh-CN-XiaoxiaoNeural", "你好呀，我是 CosyVoice 龙婉，温和亲切的邻家声线，很高兴在直播间与您相遇。"),
    "longcheng": ("zh-CN-YunxiNeural", "嗨大家好！我是 CosyVoice 龙橙，朝气蓬勃的青春男声，带给您元气满满的直播间！"),
    "longhua": ("zh-CN-YunjianNeural", "各位好，我是 CosyVoice 龙华，成熟稳重的商务解说声线，让每一次沟通更具分量。"),
    "longshu": ("zh-CN-YunyangNeural", "大家好，我是 CosyVoice 龙书，磁性深情的叙事声线，为您缓缓讲述动人故事。"),
    "longxiaobai": ("zh-CN-XiaoyiNeural", "大家好！我是 CosyVoice 龙小白，清澈治愈的少女声线，愿每一句话都温暖如初。"),
    "longxiaoxia": ("zh-CN-XiaoyiNeural", "哈喽大家好！我是 CosyVoice 龙小夏，热情活泼的元气少女声线，欢迎来到直播间！"),
    "longxiaocheng": ("zh-CN-YunxiNeural", "大家好！我是 CosyVoice 龙小诚，沉稳亲切的阳光男声，为您提供贴心细致的讲解！"),
    "longyue": ("zh-CN-XiaoxiaoNeural", "您好，我是 CosyVoice 龙悦，温婉舒缓的知性女声，愿为您带来一段舒心惬意的时光。"),
    "longjing": ("zh-CN-XiaoxiaoNeural", "您好，我是 CosyVoice 龙静，文雅舒缓的品质解说声线，为您带来宁静与专注。"),

    # ChatTTS 种子音色
    "seed_2222": ("zh-CN-XiaoxiaoNeural", "你好呀，我是 ChatTTS 2222 号自然女声，带有真实的呼吸与说话停顿呢！"),
    "seed_6666": ("zh-CN-XiaoyiNeural", "哈哈大家好！我是 ChatTTS 6666 号亲切解说声线，说话就像朋友聊天一样自然！"),
    "seed_7869": ("zh-CN-liaoning-XiaobeiNeural", "咳咳，我是 ChatTTS 7869 号微醺笑意声线，这语气够真实够有味道吧！"),
    "seed_8888": ("zh-CN-YunxiNeural", "哈喽！我是 ChatTTS 8888 号阳光男声，对话节奏超逼真，开播超轻松！"),
}


async def synthesize_preview_audio(params: PreviewSpeechParams) -> tuple[bytes, str]:
    """
    根据当前选型与参数，实时合成一段简短的问候语音 (MP3/WAV)
    精准匹配每个音色的独特声线与角色台词，让用户在试听切换时清晰感知音色变化

    返回 (audio_bytes, media_type)。
    """
    provider = (params.provider_name or "").lower().strip()
    raw_voice = (params.voice_name or "").strip()
    voice_key = raw_voice.lower()

    # 0. 智能检索声音档案是否为专属克隆音色（优先按唯一 ID，其次按名称倒序取已绑定的有效记录）
    v_record = None
    try:
        from sqlalchemy import case
        async with AsyncSessionLocal() as db_session:
            # 优先精确匹配 ID
            res = await db_session.execute(select(VoiceProfile).where(VoiceProfile.id == raw_voice))
            v_record = res.scalars().first()
            if not v_record:
                # 其次精确匹配名称，优先匹配已绑定云端真实 Voice-ID 的有效记录
                res_name = await db_session.execute(
                    select(VoiceProfile)
                    .where(VoiceProfile.name == raw_voice)
                    .order_by(
                        case((VoiceProfile.id.notlike("clone_%"), 1), else_=0).desc(),
                        VoiceProfile.created_at.desc()
                    )
                )
                v_record = res_name.scalars().first()
    except Exception as e:
        logger.warning(f"检索克隆声音档案异常: {e}")

    actual_voice_id = v_record.id if v_record else raw_voice
    cloned_name = v_record.name if v_record else raw_voice

    # 明确当前生效的 TTS 引擎：前端显式传递的引擎具有第一优先级（如正在配置 MOSS-TTS-Nano）
    req_provider = (params.provider_name or "").lower().strip()
    if req_provider:
        provider = req_provider
    elif (not provider or provider == "edge_tts") and v_record and getattr(v_record, "provider_name", None):
        provider = (v_record.provider_name or "").lower().strip()

    # 精确判断是否真正为克隆音色：避免将官方预设（如 voice_default_female）误送进百炼克隆复刻通道
    is_cloned_voice = bool(
        (v_record and getattr(v_record, "voice_type", "preset") == "cloned")
        or raw_voice.startswith("clone_")
        or "voice-custom-" in raw_voice
        or "cosyvoice-" in raw_voice
        or "qwen-audio-" in raw_voice
    )

    # 外部云端商用服务参数与凭证读取
    base_url = (params.base_url or "").strip().rstrip("/")
    api_key = (params.api_key or "").strip()

    # 专属克隆音色特权通道：100% 保证用克隆声线合成全新台词，绝对禁止播放原版上传录音！
    if is_cloned_voice:
        try:
            from server.core.audio.clone_preview import generate_cloned_voice_preview, VOICES_DIR
            sample_path = v_record.sample_wav_path if v_record else None
            if not sample_path or not Path(sample_path).exists():
                for ext in [".mp3", ".wav"]:
                    cand = VOICES_DIR / f"{actual_voice_id}{ext}"
                    if cand.exists():
                        sample_path = str(cand)
                        break
            preview_file = await generate_cloned_voice_preview(
                voice_id=actual_voice_id,
                voice_name=cloned_name,
                sample_audio_path=sample_path,
                custom_text=params.text,
                base_url=base_url,
                api_key=api_key,
                target_model=params.model_name,
                force_regenerate=True,
                provider=provider,
            )
            if preview_file and preview_file.exists():
                m_type = "audio/mpeg" if preview_file.suffix.lower() == ".mp3" else "audio/wav"
                return preview_file.read_bytes(), m_type
        except Exception as e:
            logger.error(f"克隆音色合成全新台词失败: {e}", exc_info=True)
            err_str = str(e)
            # 清理多层嵌套的前缀包裹
            clean_err = err_str.replace(f"克隆音色【{cloned_name}】合成新台词失败：", "").strip()
            raise HTTPException(
                status_code=500,
                detail=clean_err
            )

    # 1. 智能匹配官方预置音色发音台词
    default_placeholders = [
        "你好！这是当前语音合成引擎的实时试听效果，音色自然流畅，祝您直播顺利！",
        "你好，欢迎来到直播间！这是当前语音引擎的实时试听效果，祝您开播顺利！",
        "你好！这是当前语音合成引擎的实时试听效果"
    ]
    req_text = (params.text or "").strip()
    is_generic_text = not req_text or any(p in req_text for p in default_placeholders)

    profile_voice, profile_text = VOICE_PREVIEW_PROFILES.get(
        voice_key,
        (raw_voice if "neural" in voice_key else "zh-CN-XiaoxiaoNeural", f"你好！我是当前语音引擎的 {raw_voice or '推荐'} 发音音色，很高兴为您发声！")
    )
    text_to_speak = profile_text if is_generic_text else req_text

    # 2. 若是 MOSS-TTS-Nano 引擎，直接调用复旦官方 MOSS-TTS 原生神经引擎真实发声！
    if "moss" in provider or "nano" in provider or provider == "moss_tts_nano":
        moss_voice_mapping = {
            "moss_female_host_01": "Xiaoyu",
            "moss_male_host_02": "Junhao",
            "moss_female_warm_03": "Yuewen",
            "moss_female_lively_04": "Weiguo",
        }
        target_voice_name = moss_voice_mapping.get(voice_key, "Junhao")
        prompt_sample_path = None

        # 检查是否为用户克隆音色
        if str(voice_key).startswith("clone_") or "clone" in str(voice_key):
            try:
                async with AsyncSessionLocal() as db:
                    stmt = select(VoiceProfile).where(VoiceProfile.id == voice_key)
                    res = await db.execute(stmt)
                    vp = res.scalar_one_or_none()
                    if vp and vp.sample_wav_path and Path(vp.sample_wav_path).exists():
                        prompt_sample_path = vp.sample_wav_path
            except Exception as e:
                logger.warning(f"获取克隆音频样本路径异常: {e}")
            if not prompt_sample_path:
                for ext in [".mp3", ".wav"]:
                    cand = DATA_DIR / "voices" / f"{voice_key}{ext}"
                    if cand.exists():
                        prompt_sample_path = str(cand)
                        break

        try:
            from server.core.audio.moss_nano.moss_cloner import moss_cloner
            gen_file = await moss_cloner.clone_and_synthesize(
                text=text_to_speak,
                prompt_audio_path=prompt_sample_path,
                voice=target_voice_name if not prompt_sample_path else None,
                speed=1.0,
                volume=1.0
            )
            if gen_file.exists() and gen_file.stat().st_size > 512:
                return gen_file.read_bytes(), "audio/wav"
        except Exception as e:
            logger.error(f"原生 MOSS-TTS 试听合成异常: {e}", exc_info=True)
            raise HTTPException(status_code=502, detail=f"MOSS-TTS 官方引擎试听合成失败: {str(e)}")

    # 2.5 若是 Edge-TTS 引擎，直接使用微软官方声线
    if "edge" in provider or not provider or provider == "edge_tts":
        actual_voice = profile_voice if profile_voice else "zh-CN-XiaoxiaoNeural"
        try:
            import edge_tts
            communicate = edge_tts.Communicate(text_to_speak, actual_voice)
            edge_stream = communicate.stream()
            chunks = []
            try:
                async for chunk in edge_stream:
                    if chunk["type"] == "audio":
                        chunks.append(chunk["data"])
            finally:
                try:
                    await edge_stream.aclose()
                except Exception:
                    logger.debug("关闭 Edge-TTS 试听流失败", exc_info=True)
            audio_bytes = b"".join(chunks)
            if audio_bytes:
                return audio_bytes, "audio/mpeg"
        except Exception as e:
            raise HTTPException(status_code=502, detail=f"Edge-TTS 合成试听失败: {str(e)}")

    # 3. 外部云端商用服务探测与尝试 (百炼 DashScope / 硅基流动 / OpenAI 兼容网关)
    base_url = (params.base_url or "").strip().rstrip("/")
    api_key = (params.api_key or "").strip()

    # 自动解密回填数据库中存储的真实 API 密钥与端点
    if not api_key or "*" in api_key or not base_url:
        try:
            async with AsyncSessionLocal() as db_session:
                q = select(ApiProviderConfig).where(
                    (ApiProviderConfig.config_group == "tts") & (ApiProviderConfig.is_active == 1)
                )
                res = await db_session.execute(q)
                active_cfg = res.scalars().first()
                if not active_cfg:
                    q_any = select(ApiProviderConfig).where(
                        (ApiProviderConfig.config_group == "tts") & (ApiProviderConfig.provider_name.like("%cosy%"))
                    )
                    res_any = await db_session.execute(q_any)
                    active_cfg = res_any.scalars().first()
                if active_cfg:
                    if not base_url:
                        base_url = (active_cfg.base_url or "").strip().rstrip("/")
                    if not api_key or "*" in api_key:
                        api_key = decrypt_secret(active_cfg.encrypted_api_key) if active_cfg.encrypted_api_key else ""
        except Exception as e:
            logger.warning(f"读取数据库 TTS 配置异常: {e}")

    if base_url:
        headers = {}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"

        # 3.1 阿里云百炼 DashScope 原生语音合成通道（官方 SpeechSynthesizer 契约，复用 clone_preview 共享实现）
        if api_key and any(k in base_url.lower() for k in ["aliyuncs.com", "dashscope", "maas"]):
            try:
                from server.core.audio.clone_preview import (
                    DashscopeCloneError as _DCError,
                    synthesize_dashscope_cosyvoice as _synth_cloud,
                )
                cloud_bytes = await _synth_cloud(
                    base_url, api_key, raw_voice or "loongbella",
                    text_to_speak, params.model_name,
                )
                if cloud_bytes:
                    return cloud_bytes, "audio/mpeg"
            except _DCError as ce:
                if ce.status == 401:
                    raise HTTPException(status_code=401, detail="阿里云百炼 API Key 鉴权失败，请检查密钥是否正确")
                if ce.status == 400 and ("voice" in (ce.detail or "").lower() or "418" in (ce.detail or "")):
                    if is_cloned_voice:
                        raise HTTPException(
                            status_code=400,
                            detail=f"阿里云百炼未识别该专属克隆 Voice-ID ({raw_voice})。请确认该音色已在百炼控制台完成复刻，或在右侧重新登记正确的 Voice-ID。",
                        )
                    logger.warning(
                        f"百炼官方预置音色 [{raw_voice}] 云端合成受限 (detail={ce.detail})，自动平滑启用高保真声线试听"
                    )
                else:
                    logger.warning(f"百炼云端合成通道提示: {ce.detail}")
            except HTTPException:
                raise
            except Exception as e:
                logger.warning(f"百炼原生合成通道尝试: {e}")

        # 3.2 硅基流动 / OpenAI 兼容 /audio/speech 通道
        speech_url = f"{base_url}/audio/speech" if not base_url.endswith("/audio/speech") else base_url
        model_name = "tts-1"
        if "siliconflow" in base_url:
            model_name = "FunAudioLLM/CosyVoice2-0.5B" if "cosy" in provider else "2noise/ChatTTS"
        elif "cosy" in provider:
            model_name = "cosyvoice-v1"

        payload = {
            "model": model_name,
            "input": text_to_speak,
            "voice": raw_voice or "alloy"
        }

        try:
            async with httpx.AsyncClient(timeout=6.0, verify=True) as client:
                resp = await client.post(speech_url, headers=headers, json=payload)
                if resp.status_code == 200 and resp.content:
                    media_type = resp.headers.get("content-type", "audio/mpeg")
                    return resp.content, media_type
                elif resp.status_code == 401:
                    raise HTTPException(status_code=401, detail="云端 API Key 鉴权失败，请检查密钥是否正确")
        except HTTPException:
            raise
        except Exception as e:
            logger.debug(f"OpenAI 规范试听通道尝试: {e}")

    try:
        import edge_tts
        actual_voice = profile_voice if profile_voice else "zh-CN-XiaoxiaoNeural"
        communicate = edge_tts.Communicate(text_to_speak, actual_voice)
        edge_stream = communicate.stream()
        chunks = []
        try:
            async for chunk in edge_stream:
                if chunk["type"] == "audio":
                    chunks.append(chunk["data"])
        finally:
            try:
                await edge_stream.aclose()
            except Exception:
                logger.debug("关闭试听兜底流失败", exc_info=True)
        audio_bytes = b"".join(chunks)
        if audio_bytes:
            return audio_bytes, "audio/mpeg"
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"试听音频生成异常: {str(e)}")

    raise HTTPException(status_code=400, detail="未能生成有效的试听音频数据")
