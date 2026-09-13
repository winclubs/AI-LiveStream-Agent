"""上传、文档解析与配置入口共用的稳定性资源预算。"""
from __future__ import annotations

import os
import uuid
import wave
import zipfile
from pathlib import Path
from typing import Iterable

from fastapi import HTTPException, UploadFile

UPLOAD_CHUNK_BYTES = 64 * 1024
MAX_IMAGE_BYTES = 10 * 1024 * 1024
MAX_IMAGE_PIXELS = 40_000_000
MAX_IMAGE_FRAMES = 100
MAX_IMAGE_TOTAL_PIXELS = 80_000_000
MAX_AUDIO_BYTES = 20 * 1024 * 1024
MAX_AUDIO_DURATION_SEC = 30.0
MAX_KNOWLEDGE_BYTES = 25 * 1024 * 1024
MAX_KNOWLEDGE_TEXT_CHARS = 1_000_000
MAX_KNOWLEDGE_PAGES = 200
MAX_KNOWLEDGE_CHUNKS = 3_000
MAX_ARCHIVE_ENTRIES = 2_000
MAX_ARCHIVE_EXPANDED_BYTES = 100 * 1024 * 1024
MAX_GUARDRAIL_BYTES = 2 * 1024 * 1024
MAX_GUARDRAIL_ROWS = 10_000
MAX_GUARDRAIL_COLUMNS = 4
MAX_GUARDRAIL_CELL_CHARS = 256
MAX_GUARDRAIL_EXPANDED_CHARS = 2_000_000


async def stage_upload(upload: UploadFile, directory: Path, prefix: str, max_bytes: int) -> tuple[Path, int]:
    """以固定大小分块暂存上传；超限或读取失败时删除半成品。"""
    directory.mkdir(parents=True, exist_ok=True)
    staged = directory / f".{prefix}_{uuid.uuid4().hex}.part"
    total = 0
    try:
        with staged.open("wb") as output:
            while True:
                chunk = await upload.read(UPLOAD_CHUNK_BYTES)
                if not chunk:
                    break
                total += len(chunk)
                if total > max_bytes:
                    raise HTTPException(status_code=413, detail=f"上传文件超过 {max_bytes // (1024 * 1024)}MB 资源预算")
                output.write(chunk)
        if total == 0:
            raise HTTPException(status_code=400, detail="上传文件为空")
        return staged, total
    except BaseException:
        staged.unlink(missing_ok=True)
        raise


def publish_staged(staged: Path, target: Path) -> Path:
    """在同一文件系统内原子发布已验证的暂存文件。"""
    target.parent.mkdir(parents=True, exist_ok=True)
    os.replace(staged, target)
    return target


def cleanup_paths(paths: Iterable[Path | str | None]) -> None:
    for raw in paths:
        if not raw:
            continue
        try:
            Path(raw).unlink(missing_ok=True)
        except OSError:
            pass


def validate_image_budget(path: Path) -> dict:
    """完整解码前先检查图片尺寸、帧数及累计像素预算。"""
    try:
        from PIL import Image
        with Image.open(path) as image:
            width, height = image.size
            frames = int(getattr(image, "n_frames", 1) or 1)
            pixels = width * height
            if width <= 0 or height <= 0:
                raise ValueError("图片尺寸无效")
            if pixels > MAX_IMAGE_PIXELS:
                raise HTTPException(status_code=413, detail="图片像素超过 4000 万预算")
            if frames > MAX_IMAGE_FRAMES or pixels * frames > MAX_IMAGE_TOTAL_PIXELS:
                raise HTTPException(status_code=413, detail="多帧图片展开量超过资源预算")
            image.verify()
            return {"width": width, "height": height, "frames": frames}
    except HTTPException:
        raise
    except ImportError as exc:
        raise HTTPException(status_code=503, detail="图片预算检查需要安装 Pillow") from exc
    except Exception as exc:
        raise HTTPException(status_code=400, detail="图片无法解析") from exc


def probe_audio_budget(path: Path) -> dict:
    """仅读取音频元数据并限制时长，避免超长样本进入完整特征解码。"""
    frames = rate = channels = 0
    try:
        with wave.open(str(path), "rb") as audio:
            frames = audio.getnframes()
            rate = audio.getframerate()
            channels = audio.getnchannels()
    except (wave.Error, EOFError):
        try:
            import soundfile as sf
            info = sf.info(str(path))
            frames, rate, channels = int(info.frames), int(info.samplerate), int(info.channels)
        except Exception as exc:
            raise HTTPException(status_code=400, detail="音频无法解析或不受当前环境支持") from exc
    duration = frames / rate if rate else 0.0
    if duration > MAX_AUDIO_DURATION_SEC:
        raise HTTPException(status_code=413, detail="声音样本时长超过 30 秒预算")
    return {"duration_sec": duration, "sample_rate": rate, "channels": channels}


def check_archive_budget(path: Path) -> None:
    """限制 ZIP 容器（DOCX/XLSX）的条目数和声明展开总量。"""
    try:
        with zipfile.ZipFile(path) as archive:
            infos = archive.infolist()
            if len(infos) > MAX_ARCHIVE_ENTRIES:
                raise HTTPException(status_code=413, detail="压缩文档条目数超过资源预算")
            if sum(item.file_size for item in infos) > MAX_ARCHIVE_EXPANDED_BYTES:
                raise HTTPException(status_code=413, detail="压缩文档展开量超过 100MB 预算")
    except HTTPException:
        raise
    except zipfile.BadZipFile as exc:
        raise HTTPException(status_code=400, detail="压缩文档无法解析") from exc


def append_bounded(parts: list[str], value: str, used: int, limit: int, detail: str) -> int:
    value = value or ""
    new_used = used + len(value)
    if new_used > limit:
        raise HTTPException(status_code=413, detail=detail)
    parts.append(value)
    return new_used
