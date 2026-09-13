import uuid
import logging
from pathlib import Path
from typing import Optional
from fastapi import APIRouter, Depends, UploadFile, File, Form, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, update
from server.database.db import get_db
from server.database.models import VoiceProfile, Anchor, AnchorRole, LiveSessionRecord
from server.config import DATA_DIR
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


def _extract_and_save_features(sample_path: str, embedding_path: str) -> bool:
    from server.core.audio.features import extract_voice_features, save_embedding

    feat = extract_voice_features(sample_path)
    return bool(feat.get("vector") and save_embedding(feat.get("vector"), embedding_path))

@router.get("/list")
async def list_voices(db: AsyncSession = Depends(get_db)):
    """获取所有已克隆的声音档案列表"""
    result = await db.execute(select(VoiceProfile))
    voices = result.scalars().all()
    return {
        "code": 0,
        "total": len(voices),
        "data": [
            {
                "id": v.id,
                "name": v.name,
                "sample_wav_path": v.sample_wav_path,
                "speech_speed": v.speech_speed,
                "volume_gain": v.volume_gain,
                "status": v.status or "ready",
                "sample_status": "uploaded" if v.sample_wav_path and Path(v.sample_wav_path).exists() else "missing",
                "feature_status": "ready" if v.embedding_npy_path and Path(v.embedding_npy_path).exists() else "pending",
                "clone_engine": "local_features",
                "synthesis_status": "not_available",
                "preview_kind": "original_sample",
                "created_at": v.created_at.isoformat() if v.created_at else ""
            }
            for v in voices
        ]
    }

@router.post("/clone")
async def clone_voice(
    name: str = Form(...),
    audio_file: UploadFile = File(...),
    speed: float = Form(1.0),
    volume: float = Form(1.0),
    db: AsyncSession = Depends(get_db)
):
    """
    上传 10 秒人声音频样本，提取 Speaker Embedding 并保存档案
    """
    if not audio_file.filename:
        raise HTTPException(status_code=400, detail="未选择音频文件")

    clean_name = name.strip()
    if not clean_name or len(clean_name) > 128:
        raise HTTPException(status_code=422, detail="音色名称长度必须为 1 到 128 字符")
    if not 0.5 <= speed <= 2.0 or not 0.5 <= volume <= 2.0:
        raise HTTPException(status_code=422, detail="语速和音量必须在 0.5 到 2.0 之间")

    voice_id = f"voice_{uuid.uuid4().hex[:8]}"
    ext = Path(audio_file.filename).suffix.lower() or ".wav"
    target_path = VOICES_DIR / f"{voice_id}{ext}"
    embedding_path = VOICES_DIR / f"{voice_id}_embedding.npy"
    staged = None
    try:
        staged, _ = await stage_upload(audio_file, VOICES_DIR, voice_id, MAX_AUDIO_BYTES)
        await run_cpu_bound(probe_audio_budget, staged)
        publish_staged(staged, target_path)
        staged = None

        if not await run_cpu_bound(
            _extract_and_save_features,
            target_path.as_posix(),
            embedding_path.as_posix(),
        ):
            raise HTTPException(status_code=422, detail="声音样本无法生成有效声学特征")

        record = VoiceProfile(
            id=voice_id,
            name=clean_name,
            sample_wav_path=target_path.as_posix(),
            embedding_npy_path=embedding_path.as_posix(),
            speech_speed=speed,
            volume_gain=volume,
            status="ready",
        )
        db.add(record)
        await db.commit()
    except BaseException:
        await db.rollback()
        cleanup_paths([staged, target_path, embedding_path])
        raise

    return {
        "code": 0,
        "message": "声音样本已上传并完成本地声学特征预提取；当前未验证神经声音克隆合成",
        "data": {
            "id": record.id,
            "name": record.name,
            "path": record.sample_wav_path,
            "status": record.status,
            "engine": "local_features",
            "synthesis_status": "not_available",
            "preview_kind": "original_sample",
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
        record.name = req.name.strip()
    if req.speech_speed is not None:
        record.speech_speed = req.speech_speed
    if req.volume_gain is not None:
        record.volume_gain = req.volume_gain

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
    if not Path(record.sample_wav_path).exists():
        raise HTTPException(status_code=400, detail="声音样本文件丢失，请重新上传")

    record.status = "cloning"
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
                    "sample_wav": record.sample_wav_path, "speaker_id": record.id
                })
            if resp.status_code == 200:
                engine_used = "cosyvoice"
                message = f"已在本地 CosyVoice 服务登记音色【{record.name}】作为参考样本；首次真实合成仍需外部验收"
    except Exception as exc:
        logger.warning("CosyVoice 参考音色登记失败，保留本地声学特征: %s", exc)

    # 确保声学特征向量文件就绪 (引擎不可用时执行本地 FFT/Mel 特征提取)
    embedding_path = Path(record.embedding_npy_path or (VOICES_DIR / f"{voice_id}_embedding.npy"))
    if not embedding_path.exists():
        await run_cpu_bound(
            _extract_and_save_features,
            record.sample_wav_path,
            embedding_path.as_posix(),
        )
    record.embedding_npy_path = embedding_path.as_posix()

    record.status = "ready"
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
    """在线试听：返回原始声音样本音频流"""
    res = await db.execute(select(VoiceProfile).where(VoiceProfile.id == voice_id))
    record = res.scalar_one_or_none()
    if not record:
        raise HTTPException(status_code=404, detail="音色档案不存在")
    wav_path = Path(record.sample_wav_path)
    if not wav_path.exists():
        raise HTTPException(status_code=404, detail="声音样本文件丢失，请重新上传")
    return FileResponse(wav_path, media_type="audio/wav", filename=wav_path.name)

@router.delete("/{voice_id}")
async def delete_voice(voice_id: str, db: AsyncSession = Depends(get_db)):
    """删除指定声音档案"""
    res = await db.execute(select(VoiceProfile).where(VoiceProfile.id == voice_id))
    record = res.scalar_one_or_none()
    if not record:
        raise HTTPException(status_code=404, detail="声音档案不存在")

    try:
        if record.sample_wav_path and Path(record.sample_wav_path).exists():
            Path(record.sample_wav_path).unlink(missing_ok=True)
        if record.embedding_npy_path and Path(record.embedding_npy_path).exists():
            Path(record.embedding_npy_path).unlink(missing_ok=True)
    except Exception:
        pass

    # 兼容无外键约束的历史库：删除前显式安全解绑引用。
    await db.execute(update(Anchor).where(Anchor.voice_id == voice_id).values(voice_id=None))
    await db.execute(update(AnchorRole).where(AnchorRole.default_voice_id == voice_id).values(default_voice_id=None))
    await db.execute(update(LiveSessionRecord).where(LiveSessionRecord.voice_id == voice_id).values(voice_id=None))
    await db.delete(record)
    await db.commit()
    return {"code": 0, "message": "声音档案已成功删除"}
