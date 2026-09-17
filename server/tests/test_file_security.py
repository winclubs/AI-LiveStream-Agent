# -*- coding: utf-8 -*-
"""
文件上传二进制 Magic Bytes 安全核验与防注入测试套件
"""
import pytest
from fastapi import HTTPException

from server.core.security.file_validator import (
    validate_file_content,
    detect_file_type,
)


def test_magic_bytes_detection():
    """测试多媒体文件头识别能力"""
    # 1. 正常图片
    png_bytes = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"
    assert detect_file_type(png_bytes) == "png"

    jpg_bytes = b"\xff\xd8\xff\xe0\x00\x10JFIF"
    assert detect_file_type(jpg_bytes) == "jpeg"

    # 2. 正常音频/视频
    wav_bytes = b"RIFF\x24\x08\x00\x00WAVEfmt "
    assert detect_file_type(wav_bytes) == "wav"

    mp4_bytes = b"\x00\x00\x00\x1cftypisom\x00\x00\x02\x00"
    assert detect_file_type(mp4_bytes) == "mp4"

    # 3. 拦截高危可执行与脚本
    php_bytes = b"<?php phpinfo(); ?>"
    assert detect_file_type(php_bytes) == "malicious_executable"

    exe_bytes = b"MZ\x90\x00\x03\x00\x00\x00"
    assert detect_file_type(exe_bytes) == "malicious_executable"

    elf_bytes = b"\x7fELF\x02\x01\x01\x00"
    assert detect_file_type(elf_bytes) == "malicious_executable"


def test_validate_file_content_safeguards():
    """测试安全核验规则"""
    # 正常图片通过
    png_data = b"\x89PNG\r\n\x1a\n" + b"\x00" * 100
    ok, msg = validate_file_content(png_data, allowed_categories={"image"}, filename="test.png")
    assert ok is True

    # 恶意 php 伪装成 jpg 拦截
    fake_jpg = b"<?php system($_GET['c']); ?>"
    ok, msg = validate_file_content(fake_jpg, allowed_categories={"image"}, filename="test.jpg")
    assert ok is False
    assert "可执行代码或脚本载荷" in msg or "非法" in msg

    # exe 伪装成 mp4 拦截
    fake_mp4 = b"MZ\x90\x00" + b"\x00" * 200
    ok, msg = validate_file_content(fake_mp4, allowed_categories={"video"}, filename="test.mp4")
    assert ok is False

    # 超大体积拦截
    huge_data = b"\x89PNG\r\n\x1a\n" + b"\x00" * (2 * 1024 * 1024)
    ok, msg = validate_file_content(huge_data, allowed_categories={"image"}, max_size_mb=1.0)
    assert ok is False
    assert "超出限制" in msg
