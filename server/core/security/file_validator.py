# -*- coding: utf-8 -*-
"""
文件上传二进制安全校验器 (Magic Bytes Validator)
阶段一安全加固核心：
1. 检测上传文件的真实二进制文件头 (Magic Bytes)，防止攻击者伪造文件后缀 (例如上传 .php/.exe 伪装为 .jpg/.mp4)；
2. 支持严格模式白名单校验 (图片、视频、音频)；
3. 拦截常见 WebShell、可执行文件 (PE/ELF)、脚本注入等恶意载荷。
"""
import logging
from typing import Optional, Set, Tuple

from fastapi import HTTPException, UploadFile

logger = logging.getLogger("LiveAgent.FileValidator")

# 常见合法多媒体类型的魔数特征
MAGIC_SIGNATURES = {
    # 图片
    "jpeg": [b"\xff\xd8\xff"],
    "jpg": [b"\xff\xd8\xff"],
    "png": [b"\x89PNG\r\n\x1a\n"],
    "gif": [b"GIF87a", b"GIF89a"],
    "webp": [b"RIFF"],  # 后续需校验 +8 字节处的 WEBP
    # 视频
    "mp4": [b"ftyp"],   # 偏离 4 字节处
    "mov": [b"ftyp", b"moov"],
    "avi": [b"RIFF"],   # +8 字节处的 AVI
    "webm": [b"\x1a\x45\xdf\xa3"],
    "mkv": [b"\x1a\x45\xdf\xa3"],
    # 音频
    "wav": [b"RIFF"],   # +8 字节处的 WAVE
    "mp3": [b"ID3", b"\xff\xfb", b"\xff\xf3", b"\xff\xf2"],
}

# 明确禁止的高危文件头标志
MALICIOUS_PREFIXES = [
    b"<?php",
    b"<?=",
    b"<script",
    b"MZ",       # Windows PE 可执行文件 / DLL
    b"\x7fELF",  # Linux ELF 可执行文件
]


def detect_file_type(header_bytes: bytes) -> Optional[str]:
    """根据文件二进制头部探测真实多媒体类型"""
    if not header_bytes or len(header_bytes) < 4:
        return None

    # 1. 检查已知恶意前缀
    for bad in MALICIOUS_PREFIXES:
        if header_bytes.startswith(bad):
            return "malicious_executable"

    # 2. 检查特定格式
    if header_bytes.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if header_bytes.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    if header_bytes.startswith(b"GIF87a") or header_bytes.startswith(b"GIF89a"):
        return "gif"
    if header_bytes.startswith(b"\x1a\x45\xdf\xa3"):
        return "mkv"
    if header_bytes.startswith(b"ID3") or header_bytes[:2] in (b"\xff\xfb", b"\xff\xf3", b"\xff\xf2"):
        return "mp3"

    # 3. RIFF 容器检测 (WAV / WEBP / AVI)
    if header_bytes.startswith(b"RIFF") and len(header_bytes) >= 12:
        tag = header_bytes[8:12]
        if tag == b"WAVE":
            return "wav"
        if tag == b"WEBP":
            return "webp"
        if tag == b"AVI ":
            return "avi"

    # 4. ISO Base Media File Format (MP4 / MOV) - 通常在第 4 到 8 字节为 ftyp
    if len(header_bytes) >= 12 and header_bytes[4:8] == b"ftyp":
        return "mp4"

    return None


def validate_file_content(
    file_bytes: bytes,
    allowed_categories: Set[str] = {"image", "video"},
    max_size_mb: float = 200.0,
    filename: str = "",
) -> Tuple[bool, str]:
    """
    对上传的二进制文件内容进行全面安全核验：
    :param file_bytes: 完整文件或前 1024 字节数据
    :param allowed_categories: 允许的大类 ('image', 'video', 'audio')
    :param max_size_mb: 最大文件体积 (MB)
    :param filename: 上传原始文件名 (用于校验扩展名匹配)
    :return: (is_valid, reason)
    """
    if not file_bytes:
        return False, "上传文件为空"

    # 体积上限检查
    size_mb = len(file_bytes) / (1024 * 1024)
    if size_mb > max_size_mb:
        return False, f"文件体积超出限制 (当前 {size_mb:.1f}MB, 最大允许 {max_size_mb:.1f}MB)"

    header = file_bytes[:64]

    # 检测恶意代码特征
    for bad in MALICIOUS_PREFIXES:
        if header.startswith(bad):
            logger.warning(f"检测到高危上传载荷拦截: {filename}, header={header[:8]}")
            return False, "非法的文件内容：检测到潜在的可执行代码或脚本载荷"

    detected_type = detect_file_type(header)
    if not detected_type:
        # 如果后缀是常见的 jpg/png/mp4/wav 但魔数不符，坚决拦截
        ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
        if ext in ("jpg", "jpeg", "png", "gif", "webp", "mp4", "wav", "mp3"):
            return False, f"文件内容特征与声明的扩展名 .{ext} 不匹配，疑似伪造文件"
        return False, "无法识别的文件格式或不受支持的二进制文件"

    # 归类检查
    type_category_map = {
        "jpeg": "image",
        "png": "image",
        "gif": "image",
        "webp": "image",
        "mp4": "video",
        "mov": "video",
        "avi": "video",
        "mkv": "video",
        "wav": "audio",
        "mp3": "audio",
    }
    cat = type_category_map.get(detected_type, "unknown")
    if cat not in allowed_categories:
        return False, f"不允许上传此类文件 (检测为 {detected_type}，只允许 {list(allowed_categories)})"

    return True, "OK"


async def validate_upload_file(
    file: UploadFile,
    allowed_categories: Set[str] = {"image", "video"},
    max_size_mb: float = 200.0,
) -> bytes:
    """
    FastAPI 路由依赖函数：读取 UploadFile 并进行 Magic Bytes 安全验证
    校验成功返回文件二进制，失败直接抛出 HTTPException 400
    """
    contents = await file.read()
    await file.seek(0)  # 还原指针

    is_valid, msg = validate_file_content(
        contents,
        allowed_categories=allowed_categories,
        max_size_mb=max_size_mb,
        filename=file.filename or "",
    )
    if not is_valid:
        logger.warning(f"文件安全校验拦截: {file.filename} - {msg}")
        raise HTTPException(status_code=400, detail=f"文件安全校验失败: {msg}")

    return contents
