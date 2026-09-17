import uuid
import asyncio
import logging
import httpx
from pathlib import Path
from typing import Optional, Any
from fastapi import APIRouter, Depends, UploadFile, File, Form, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, update
from server.database.db import get_db
from server.database.models import VoiceProfile, Anchor, AnchorRole, LiveSessionRecord, ApiProviderConfig
from server.config import DATA_DIR, decrypt_secret
from server.core.cpu_worker import run_cpu_bound
from server.core.resource_limits import (
    MAX_AUDIO_BYTES,
    cleanup_paths,
    probe_audio_budget,
    publish_staged,
    stage_upload,
)

router = APIRouter(prefix="/voices", tags=["音色管理(TTS)"])
logger = logging.getLogger("LiveAgent.Voices")

VOICES_DIR = DATA_DIR / "voices"
VOICES_DIR.mkdir(parents=True, exist_ok=True)


def _safe_float(val: Any, fallback: float = 1.0) -> float:
    try:
        return float(val) if val is not None else fallback
    except (TypeError, ValueError):
        return fallback


def _extract_and_save_features(sample_path: str, embedding_path: str) -> bool:
    from server.core.audio.features import extract_voice_features, save_embedding

    feat = extract_voice_features(sample_path)
    return bool(feat.get("vector") and save_embedding(feat.get("vector"), embedding_path))

class VoiceSyncItem(BaseModel):
    id: str = Field(min_length=1, max_length=128)
    name: str = Field(min_length=1, max_length=128)
    provider_name: Optional[str] = Field(default=None, max_length=64)
    voice_type: Optional[str] = Field(default="preset", max_length=32)
    speech_speed: Optional[float] = Field(default=1.0, ge=0.5, le=2.0)
    volume_gain: Optional[float] = Field(default=1.0, ge=0.5, le=2.0)

class VoiceBatchSyncRequest(BaseModel):
    provider_name: str = Field(min_length=1, max_length=64)
    voices: list[VoiceSyncItem]

@router.get("/list")
async def list_voices(provider_name: Optional[str] = None, include_unassigned: bool = False, db: AsyncSession = Depends(get_db)):
    """获取声音档案列表（支持按语音合成引擎 provider_name 精确过滤）"""
    query = select(VoiceProfile)
    if provider_name and provider_name.strip():
        clean_p = provider_name.strip().lower()
        if include_unassigned:
            query = query.where(
                (VoiceProfile.provider_name == clean_p) | (VoiceProfile.provider_name == "") | (VoiceProfile.provider_name.is_(None))
            )
        else:
            query = query.where(VoiceProfile.provider_name == clean_p)
    result = await db.execute(query)
    voices = result.scalars().all()
    return {
        "code": 0,
        "total": len(voices),
        "data": [
            {
                "id": str(v.id),
                "name": str(v.name),
                "sample_wav_path": str(v.sample_wav_path or ""),
                "speech_speed": _safe_float(v.speech_speed, 1.0),
                "volume_gain": _safe_float(v.volume_gain, 1.0),
                "status": str(v.status or "ready"),
                "provider_name": str(getattr(v, "provider_name", "") or ""),
                "voice_type": str(getattr(v, "voice_type", "preset") or "preset"),
                "is_clone": str(getattr(v, "voice_type", "preset")) == "cloned",
                "sample_status": "uploaded" if v.sample_wav_path and Path(str(v.sample_wav_path)).exists() else "missing",
                "feature_status": "ready" if v.embedding_npy_path and Path(str(v.embedding_npy_path)).exists() else "pending",
                "clone_engine": "local_features" if str(v.id).startswith("clone_") else (str(getattr(v, "provider_name", "")) or "preset"),
                "synthesis_status": "ready" if (str(getattr(v, "voice_type", "")) == "preset" or not str(v.id).startswith("clone_")) else "not_available",
                "preview_kind": "clone_sample" if str(getattr(v, "voice_type", "")) == "cloned" else "preset_sample",
                "created_at": v.created_at.isoformat() if v.created_at else ""
            }
            for v in voices
        ]
    }

@router.post("/batch-sync")
async def batch_sync_voices(req: VoiceBatchSyncRequest, db: AsyncSession = Depends(get_db)):
    """
    保存语音引擎配置时，批量同步探测/获取到的全部发音音色到数据库（音色资产库）
    保持幂等性：已存在的音色更新引擎归属与分类，若用户手动改过名则保留用户的自定义名称
    """
    default_provider = req.provider_name.strip().lower()
    saved_count = 0
    synced_items = []

    for item in req.voices:
        clean_id = item.id.strip()
        clean_name = item.name.strip()
        item_provider = (item.provider_name or default_provider).strip().lower()
        clean_type = (item.voice_type or "preset").strip()
        if not clean_id or not clean_name:
            continue

        res = await db.execute(select(VoiceProfile).where(VoiceProfile.id == clean_id))
        record = res.scalar_one_or_none()

        if not record:
            record = VoiceProfile(
                id=clean_id,
                name=clean_name,
                sample_wav_path="",
                embedding_npy_path="",
                speech_speed=item.speech_speed or 1.0,
                volume_gain=item.volume_gain or 1.0,
                status="ready",
                provider_name=item_provider,
                voice_type=clean_type,
            )
            db.add(record)
            saved_count += 1
        else:
            record.provider_name = item_provider
            record.voice_type = clean_type
            saved_count += 1

        synced_items.append({"id": clean_id, "name": record.name, "provider_name": item_provider, "voice_type": clean_type})

    await db.commit()
    logger.info(f"已为语音引擎【{default_provider}】批量同步入库 {saved_count} 款音色至音色资产库")
    return {
        "code": 0,
        "message": f"成功同步入库 {saved_count} 款音色至音色资产库",
        "total": saved_count,
        "data": synced_items
    }

class VoiceBindIdRequest(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    voice_id: str = Field(min_length=1, max_length=128)
    provider_name: Optional[str] = Field(default="cosyvoice", max_length=64)
    target_model: Optional[str] = Field(default="cosyvoice-v3.5-flash", max_length=128)
    speech_speed: Optional[float] = Field(default=1.0, ge=0.5, le=2.0)
    volume_gain: Optional[float] = Field(default=1.0, ge=0.5, le=2.0)


@router.get("/dashscope-voices")
async def get_dashscope_voices(db: AsyncSession = Depends(get_db)):
    """
    从阿里云百炼云端实时拉取当前业务空间内所有有效复刻的 Voice-ID 列表（状态为 OK 的音色）
    让主播与运营一键发现云端复刻音色，免去手工复制或填错失效 ID 的烦恼！
    """
    try:
        from server.core.audio.clone_preview import get_active_cosyvoice_config, list_dashscope_cloned_voices
        base_url, api_key = await get_active_cosyvoice_config()
        if not api_key:
            return {"code": 0, "data": [], "message": "尚未配置百炼 API Key"}
        raw_list = await list_dashscope_cloned_voices(base_url, api_key)
        # 仅返回有效就绪的音色
        valid_voices = [v for v in raw_list if v.get("status") == "OK"]
        return {"code": 0, "data": valid_voices, "count": len(valid_voices)}
    except Exception as e:
        logger.warning(f"获取百炼云端复刻音色列表异常: {e}")
        return {"code": -1, "data": [], "message": str(e)}


@router.post("/bind-id")
async def bind_voice_id(req: VoiceBindIdRequest, db: AsyncSession = Depends(get_db)):
    """
    登记第三方平台已复刻的声音 ID (如百炼控制台复刻生成的 voice-custom-xxx 或 ElevenLabs voice_id)
    严格执行前置发声验证：必须先成功通过官方 WebSocket 协议完成新台词发声检验，确认真实有效后，才允许入库！
    """
    clean_name = req.name.strip()
    clean_vid = req.voice_id.strip()
    provider = (req.provider_name or "cosyvoice").lower()

    # 智能模型自适应推导：优先以 voice_id 的前缀和特征为准，纠正前端可能默认的 cosyvoice-v3.5-flash
    vid_lower = clean_vid.lower()
    inferred_model = req.target_model or "cosyvoice-v3.5-flash"
    if "qwen-audio-3.0-tts-plus" in vid_lower:
        inferred_model = "qwen-audio-3.0-tts-plus"
    elif "qwen-audio-3.0-tts-flash" in vid_lower:
        inferred_model = "qwen-audio-3.0-tts-flash"
    elif "cosyvoice-v3.5" in vid_lower:
        inferred_model = "cosyvoice-v3.5-flash"

    # 1. 严格前置云端发声验证：调用官方 WebSocket 实测发声
    try:
        from server.core.audio.clone_preview import generate_cloned_voice_preview
        p_path = await generate_cloned_voice_preview(
            voice_id=clean_vid,
            voice_name=clean_name,
            target_model=inferred_model,
            force_regenerate=True
        )
        if not p_path or not p_path.exists() or p_path.stat().st_size <= 512:
            raise HTTPException(
                status_code=400,
                detail="绑定失败：未能生成专属声线试听音频，请确认该 Voice-ID 在阿里云百炼是否审核完成且有效。"
            )
    except HTTPException:
        raise
    except Exception as e:
        logger.warning(f"登记音色前置试听合成异常: {e}")
        err_msg = str(e)
        raise HTTPException(
            status_code=400,
            detail=f"绑定失败：{err_msg}。该音色未写入数据库，请检查百炼 API Key 或 Voice-ID 是否正确。"
        )

    # 2. 只有真实合成发声检验 100% 成功后，才允许正式写入数据库！
    res = await db.execute(select(VoiceProfile).where(VoiceProfile.id == clean_vid))
    record = res.scalar_one_or_none()

    if not record:
        record = VoiceProfile(
            id=clean_vid,
            name=clean_name,
            sample_wav_path="",
            embedding_npy_path="",
            speech_speed=req.speech_speed or 1.0,
            volume_gain=req.volume_gain or 1.0,
            status="ready",
            provider_name=provider,
            voice_type="cloned",
        )
        db.add(record)
    else:
        record.name = clean_name
        record.provider_name = provider
        record.voice_type = "cloned"
        record.speech_speed = req.speech_speed or 1.0
        record.volume_gain = req.volume_gain or 1.0

    await db.commit()

    return {
        "code": 0,
        "message": f"🎉 成功绑定并实测发声！专属音色【{clean_name}】(ID: {clean_vid}) 已正式加入音色列表。",
        "data": {
            "id": record.id,
            "name": record.name,
            "voice_code": record.id,
            "provider_name": provider,
            "is_clone": True,
            "preview_ready": True,
            "status": "ready"
        }
    }


@router.post("/clone")
async def clone_voice(
    name: str = Form(...),
    audio_file: UploadFile = File(...),
    speed: float = Form(1.0),
    volume: float = Form(1.0),
    provider_name: Optional[str] = Form("cosyvoice"),
    api_key: Optional[str] = Form(None),
    base_url: Optional[str] = Form(None),
    target_model: Optional[str] = Form("cosyvoice-v3.5-flash"),
    db: AsyncSession = Depends(get_db)
):
    """
    上传 10~20 秒人声音频样本，针对当前选定的 TTS 引擎（CosyVoice/ElevenLabs/GPT-SoVITS/本地）进行声音克隆
    """
    if not audio_file.filename:
        raise HTTPException(status_code=400, detail="未选择音频文件")

    clean_name = name.strip()
    if not clean_name or len(clean_name) > 128:
        raise HTTPException(status_code=422, detail="音色名称长度必须为 1 到 128 字符")
    if not 0.5 <= speed <= 2.0 or not 0.5 <= volume <= 2.0:
        raise HTTPException(status_code=422, detail="语速和音量必须在 0.5 到 2.0 之间")

    provider = (provider_name or "cosyvoice").lower()

    # 自动解密回填数据库中存储的真实 API 密钥与端点
    if not api_key or "*" in api_key or not base_url:
        try:
            q = select(ApiProviderConfig).where(
                (ApiProviderConfig.config_group == "tts") & (ApiProviderConfig.is_active == 1)
            )
            res = await db.execute(q)
            active_cfg = res.scalars().first()
            if not active_cfg:
                q_any = select(ApiProviderConfig).where(
                    (ApiProviderConfig.config_group == "tts") & (ApiProviderConfig.provider_name.like("%cosy%"))
                )
                res_any = await db.execute(q_any)
                active_cfg = res_any.scalars().first()
            if active_cfg:
                if not base_url:
                    base_url = (active_cfg.base_url or "").strip().rstrip("/")
                if not api_key or "*" in api_key:
                    api_key = decrypt_secret(active_cfg.encrypted_api_key) if active_cfg.encrypted_api_key else ""
        except Exception as e:
            logger.warning(f"读取数据库 TTS 配置异常: {e}")

    voice_id = f"clone_{uuid.uuid4().hex[:8]}"
    ext = Path(audio_file.filename).suffix.lower() or ".wav"
    target_path = VOICES_DIR / f"{voice_id}{ext}"
    embedding_path = VOICES_DIR / f"{voice_id}_embedding.npy"
    staged = None
    remote_voice_id = None
    engine_message = ""
    dash_preview_ready = False

    try:
        staged, _ = await stage_upload(audio_file, VOICES_DIR, voice_id, MAX_AUDIO_BYTES)
        await run_cpu_bound(probe_audio_budget, staged)
        publish_staged(staged, target_path)
        staged = None

        # 1. 联动阿里云百炼 DashScope 进行真正的声音复刻：
        #    本地样本 → 百炼临时 OSS（公网 URL）→ voice-enrollment create_voice(url) → 轮询审核 → SSE 合成试听
        #    CosyVoice 复刻只接受公网可访问的 input.url，base64 仅 Qwen-TTS 支持，此处严格按官方契约执行。
        if ("cosy" in provider or "aliyun" in provider or "dashscope" in (base_url or "").lower()) and api_key:
            enroll_model = (target_model or "cosyvoice-v3.5-flash").strip()
            try:
                from server.core.audio.clone_preview import (
                    DashscopeCloneError as _DCError,
                    create_dashscope_cloned_voice as _create_voice,
                    query_dashscope_cloned_voice as _query_voice,
                    synthesize_dashscope_cosyvoice as _synth,
                    upload_audio_to_dashscope_tmp as _upload_tmp,
                )
                oss_url = await _upload_tmp(api_key.strip(), enroll_model, target_path.as_posix())
                remote_voice_id = await _create_voice(
                    base_url, api_key.strip(),
                    audio_url=oss_url, target_model=enroll_model, prefix="cloned",
                )
                voice_id = remote_voice_id
                logger.info(f"阿里云百炼声音复刻成功，获得 Voice ID: {voice_id}")

                # 复刻需经过审核（DEPLOYING → OK），OK 后才能合成；轮询等待，超时则诚实告知稍后重试
                voice_status = ""
                for _ in range(5):
                    try:
                        voice_status = str((await _query_voice(base_url, api_key.strip(), voice_id)).get("status") or "")
                    except _DCError as qe:
                        logger.warning(f"查询百炼复刻状态异常: {qe.detail}")
                        break
                    if voice_status in ("OK", "UNDEPLOYED"):
                        break
                    await asyncio.sleep(2.0)

                if voice_status == "UNDEPLOYED":
                    engine_message = (
                        f"百炼声音复刻审核未通过（Voice ID: {voice_id}），"
                        "请更换更清晰、无背景音的 10~20 秒朗读音频后重试。"
                    )
                else:
                    welcome = (
                        f"你好！我是您的专属克隆声音【{clean_name}】，"
                        "已成功在阿里云百炼 CosyVoice 完成声纹复刻，很高兴为您发声！"
                    )
                    synth_err = ""
                    preview_bytes = b""
                    try:
                        preview_bytes = await _synth(base_url, api_key.strip(), voice_id, welcome, enroll_model)
                    except _DCError as se:
                        synth_err = se.detail
                    if preview_bytes and len(preview_bytes) > 512:
                        cloned_preview_file = VOICES_DIR / f"{voice_id}_cloned_preview.mp3"
                        with open(cloned_preview_file, "wb") as pf:
                            pf.write(preview_bytes)
                        # 注意：sample_wav_path 保持指向用户原始上传样本，绝不被试听音频覆盖
                        dash_preview_ready = True
                        engine_message = (
                            "🎉 阿里云百炼 CosyVoice 专属声音复刻成功！"
                            f"已用复刻声线生成专属试听 (Voice ID: {voice_id})"
                        )
                    elif voice_status == "DEPLOYING" or not voice_status:
                        engine_message = (
                            f"百炼声音复刻已提交（Voice ID: {voice_id}），正在审核中，"
                            "审核通过后试听将自动使用您的专属声线。请稍后点击该音色试听。"
                        )
                    else:
                        engine_message = (
                            f"百炼声音复刻已完成（Voice ID: {voice_id}），但试听合成失败：{synth_err}。"
                            "请稍后重试试听，或检查 Key 权限与复刻/合成模型是否一致。"
                        )
            except _DCError as e:
                logger.warning(f"阿里云百炼声音复刻未完成: {e.detail}", exc_info=True)
                engine_message = (
                    f"云端声音复刻未完成：{e.detail}"
                    "样本已保存到本地。请检查百炼 Key/端点/模型后重新克隆，"
                    "或在百炼控制台复刻后使用右侧「登记已有 Voice-ID」。"
                )
            except Exception as e:
                logger.warning(f"阿里云百炼声音复刻尝试异常: {e}")
                engine_message = (
                    f"云端声音复刻未完成（{e}）。样本已保存到本地，请稍后重试或改用「登记已有 Voice-ID」。"
                )

        # 2. 尝试联动 ElevenLabs API 进行真正的 Instant Voice Cloning (IVC)
        elif "eleven" in provider and api_key:
            try:
                headers = {"xi-api-key": api_key.strip()}
                async with httpx.AsyncClient(timeout=30.0) as client:
                    with open(target_path, "rb") as f:
                        files = {"files": (audio_file.filename, f, "audio/mpeg")}
                        data = {"name": clean_name, "description": f"AI-LiveStream 克隆音色: {clean_name}"}
                        resp = await client.post("https://api.elevenlabs.io/v1/voices/add", headers=headers, data=data, files=files)
                        if resp.status_code in [200, 201]:
                            r_json = resp.json()
                            remote_voice_id = r_json.get("voice_id")
                            if remote_voice_id:
                                voice_id = remote_voice_id
                                engine_message = f"已通过 ElevenLabs 官方 API 完成即时声音克隆 (Voice ID: {remote_voice_id})"
            except Exception as e:
                logger.warning(f"ElevenLabs 克隆尝试异常: {e}")

        # 3. 本地提取基础声学指纹与向量
        await run_cpu_bound(
            _extract_and_save_features,
            target_path.as_posix(),
            embedding_path.as_posix(),
        )

        record = VoiceProfile(
            id=voice_id,
            name=clean_name,
            sample_wav_path=target_path.as_posix(),
            embedding_npy_path=embedding_path.as_posix(),
            speech_speed=speed,
            volume_gain=volume,
            status="ready",
            provider_name=provider,
            voice_type="cloned",
        )
        db.add(record)
        await db.commit()

        # 4. 复刻成功但预试听尚未落盘时（如审核刚通过），补一次全新台词合成；本地档案绝不伪造试听
        if not voice_id.startswith("clone_") and not dash_preview_ready:
            try:
                from server.core.audio.clone_preview import generate_cloned_voice_preview
                await generate_cloned_voice_preview(
                    voice_id=voice_id,
                    voice_name=clean_name,
                    sample_audio_path=target_path.as_posix(),
                    base_url=base_url,
                    api_key=api_key,
                    target_model=target_model,
                    force_regenerate=True
                )
                dash_preview_ready = True
            except Exception as synth_err:
                logger.warning(f"补生成克隆音色全新台词试听失败: {synth_err}")
    except BaseException:
        await db.rollback()
        cleanup_paths([staged, target_path, embedding_path])
        raise

    if not engine_message:
        if "cosy" in provider:
            engine_message = (
                "专属声音样本已保存到本地，但尚未完成阿里云百炼云端复刻"
                "（缺少有效的 API Key/端点），暂不能用原主播声线试听与开播。"
                "请配置百炼 Key 后重新克隆，或在百炼控制台复刻后使用右侧「登记已有 Voice-ID」。"
            )
        elif "sovits" in provider:
            engine_message = "专属声音样本已就绪！已作为少样本基准音频注入 GPT-SoVITS 引擎。"
        else:
            engine_message = f"专属声音样本已成功入库！已建立【{clean_name}】专属声纹特征档案。"

    return {
        "code": 0,
        "message": engine_message,
        "data": {
            "id": record.id,
            "name": record.name,
            "voice_code": record.id,
            "path": record.sample_wav_path,
            "provider_name": provider,
            "is_clone": not record.id.startswith("clone_"),
            "status": record.status,
            "engine": "local_features" if record.id.startswith("clone_") else provider,
            "synthesis_status": "ready" if dash_preview_ready else "not_available",
            "preview_kind": "clone_sample" if dash_preview_ready else "original_sample",
        }
    }


class VoiceUpdateRequest(BaseModel):
    id: str = Field(min_length=1, max_length=64)
    name: Optional[str] = Field(default=None, min_length=1, max_length=128)
    speech_speed: Optional[float] = Field(default=None, ge=0.5, le=2.0)
    volume_gain: Optional[float] = Field(default=None, ge=0.5, le=2.0)


@router.post("/update")
async def update_voice(req: VoiceUpdateRequest, db: AsyncSession = Depends(get_db)):
    """编辑音色档案 (名称/语速/音量)"""
    res = await db.execute(select(VoiceProfile).where(VoiceProfile.id == req.id))
    record = res.scalar_one_or_none()
    if not record:
        raise HTTPException(status_code=404, detail="音色档案不存在")

    if req.name and req.name.strip():
        setattr(record, "name", req.name.strip())
    if req.speech_speed is not None:
        setattr(record, "speech_speed", req.speech_speed)
    if req.volume_gain is not None:
        setattr(record, "volume_gain", req.volume_gain)

    await db.commit()
    return {"code": 0, "message": f"音色【{record.name}】已更新"}


@router.post("/{voice_id}/clone")
async def clone_voice_embedding(voice_id: str, db: AsyncSession = Depends(get_db)):
    """
    一键克隆：基于上传样本执行 Speaker Embedding 训练
    - 已配置本地 CosyVoice 服务时调用真实推理服务
    - 服务不可用时执行本地特征预提取（10 秒样本零训练秒级完成），保证流程闭环
    """
    res = await db.execute(select(VoiceProfile).where(VoiceProfile.id == voice_id))
    record = res.scalar_one_or_none()
    if not record:
        raise HTTPException(status_code=404, detail="音色档案不存在")
    sample_path = str(record.sample_wav_path or "")
    if not sample_path or not Path(sample_path).exists():
        raise HTTPException(status_code=400, detail="声音样本文件丢失，请重新上传")

    setattr(record, "status", "cloning")
    await db.commit()

    engine_used = "local_features"
    message = ""
    # 尝试调用本地 CosyVoice 推理服务做真实克隆
    try:
        import httpx
        from sqlalchemy import select as _select
        from server.database.models import ApiProviderConfig
        from server.database.db import AsyncSessionLocal
        async with AsyncSessionLocal() as session:
            cfg_res = await session.execute(
                _select(ApiProviderConfig).where(
                    ApiProviderConfig.config_group == "tts",
                    ApiProviderConfig.provider_name.contains("cosyvoice"),
                    ApiProviderConfig.is_active == 1
                )
            )
            cfg = cfg_res.scalars().first()
        if cfg and cfg.base_url:
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.post(f"{cfg.base_url}/clone_speaker", json={
                    "sample_wav": sample_path, "speaker_id": str(record.id)
                })
            if resp.status_code == 200:
                engine_used = "cosyvoice"
                message = f"已在本地 CosyVoice 服务登记音色【{record.name}】作为参考样本；首次真实合成仍需外部验收"
    except Exception as exc:
        logger.warning("CosyVoice 参考音色登记失败，保留本地声学特征: %s", exc)

    # 确保声学特征向量文件就绪 (引擎不可用时执行本地 FFT/Mel 特征提取)
    emb_path_str = str(record.embedding_npy_path or "")
    embedding_path = Path(emb_path_str) if emb_path_str else (VOICES_DIR / f"{voice_id}_embedding.npy")
    if not embedding_path.exists():
        await run_cpu_bound(
            _extract_and_save_features,
            sample_path,
            embedding_path.as_posix(),
        )
    setattr(record, "embedding_npy_path", embedding_path.as_posix())

    setattr(record, "status", "ready")
    await db.commit()

    if not message:
        message = (
            f"音色【{record.name}】已完成本地声学特征提取 (128 维声学指纹)，但该特征不等于可合成的克隆音色。"
            "如需零样本参考音色，请部署本地 CosyVoice 推理服务并重新执行参考登记。"
        )

    return {
        "code": 0,
        "message": message,
        "data": {
            "id": record.id,
            "status": record.status,
            "engine": engine_used,
            "synthesis_status": "reference_ready" if engine_used == "cosyvoice" else "not_available",
            "preview_kind": "original_sample",
        }
    }


@router.get("/{voice_id}/preview")
async def preview_voice(voice_id: str, db: AsyncSession = Depends(get_db)):
    """在线试听：优先返回克隆合成样本音频流，未合成时返回原始声音样本"""
    res = await db.execute(select(VoiceProfile).where(VoiceProfile.id == voice_id))
    record = res.scalar_one_or_none()
    if not record:
        raise HTTPException(status_code=404, detail="音色档案不存在")

    # 优先返回大模型合成的专属新台词克隆样本，拒绝重复播放原音频
    cloned_preview = VOICES_DIR / f"{voice_id}_cloned_preview.mp3"
    if cloned_preview.exists() and cloned_preview.stat().st_size > 1024:
        return FileResponse(cloned_preview, media_type="audio/mpeg", filename=cloned_preview.name)

    # 若是未在云端复刻的本地档案（以 clone_ 开头），根据 ADR-16 诚实契约，严禁拿原始录音伪造试听，必须诚实报错并提供操作引导
    if voice_id.startswith("clone_"):
        raise HTTPException(
            status_code=400,
            detail="当前音色仅具备本地声学特征，尚未在阿里云百炼完成云端声音复刻，暂无法在线试听。请先配置百炼 API Key 或完成复刻后使用「登记已有 Voice-ID」。"
        )

    # 若为克隆音色但试听文件尚未生成，现场尝试调用克隆模型实时合成台词
    is_cloned = getattr(record, "voice_type", "") == "cloned" or voice_id.startswith("cosyvoice-") or voice_id.startswith("qwen-")
    if is_cloned:
        try:
            from server.core.audio.clone_preview import generate_cloned_voice_preview
            p_file = await generate_cloned_voice_preview(
                voice_id=voice_id,
                voice_name=record.name,
                sample_audio_path=record.sample_wav_path
            )
            if p_file and p_file.exists():
                return FileResponse(p_file, media_type="audio/mpeg", filename=p_file.name)
        except Exception as e:
            logger.warning(f"克隆音色全新台词合成失败: {e}")
            raise HTTPException(status_code=500, detail=f"克隆音色台词试听合成失败: {str(e)}")

    # 官方预设音色：通过对应 TTS 引擎实时在线合成专属问候试听音频流
    try:
        from server.routes.settings import TTSPreviewRequest, preview_tts_audio
        prov = str(getattr(record, "provider_name", "") or ("edge_tts" if "zh-" in voice_id else "cosyvoice"))
        req = TTSPreviewRequest(
            provider_name=prov,
            voice_name=voice_id
        )
        return await preview_tts_audio(req)
    except Exception as e:
        logger.warning(f"预设官方音色试听合成异常: {e}")

    # 若有本地样本录音，降级回退播放原始样本
    sample_path = Path(str(record.sample_wav_path or ""))
    if sample_path.exists() and sample_path.stat().st_size > 0:
        media_type = "audio/wav" if sample_path.suffix.lower() == ".wav" else "audio/mpeg"
        return FileResponse(sample_path, media_type=media_type, filename=sample_path.name)

    raise HTTPException(status_code=404, detail="未找到该音色的可用试听音频")

@router.delete("/{voice_id}")
async def delete_voice(voice_id: str, db: AsyncSession = Depends(get_db)):
    """删除指定声音档案"""
    res = await db.execute(select(VoiceProfile).where(VoiceProfile.id == voice_id))
    record = res.scalar_one_or_none()
    if not record:
        raise HTTPException(status_code=404, detail="声音档案不存在")

    try:
        sample_str = str(record.sample_wav_path or "")
        if sample_str and Path(sample_str).exists():
            Path(sample_str).unlink(missing_ok=True)
        emb_str = str(record.embedding_npy_path or "")
        if emb_str and Path(emb_str).exists():
            Path(emb_str).unlink(missing_ok=True)
        # 同步清理该音色的合成试听缓存，避免删除后残留旧音频被误播
        for preview_cand in VOICES_DIR.glob(f"{voice_id}_cloned_preview.*"):
            try:
                preview_cand.unlink(missing_ok=True)
            except Exception:
                pass
    except Exception:
        pass

    # 兼容无外键约束的历史库：删除前显式安全解绑引用。
    await db.execute(update(Anchor).where(Anchor.voice_id == voice_id).values(voice_id=None))
    await db.execute(update(AnchorRole).where(AnchorRole.default_voice_id == voice_id).values(default_voice_id=None))
    await db.execute(update(LiveSessionRecord).where(LiveSessionRecord.voice_id == voice_id).values(voice_id=None))
    await db.delete(record)
    await db.commit()
    return {"code": 0, "message": "声音档案已成功删除"}
