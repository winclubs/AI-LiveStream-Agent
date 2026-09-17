import os
import sys
import base64
import ctypes
from pathlib import Path
from typing import Any
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

# 项目根路径与数据目录 (支持通过 LIVE_AGENT_DATA_DIR 重定向，用于测试隔离/便携部署)
BASE_DIR = Path(__file__).resolve().parent.parent
ROOT_DIR = BASE_DIR
DATA_DIR = Path(os.getenv("LIVE_AGENT_DATA_DIR") or (BASE_DIR / "data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)

# 数据库路径 (SQLite WAL 模式)
DB_PATH = DATA_DIR / "live_agent.db"
DATABASE_URL = f"sqlite+aiosqlite:///{DB_PATH.as_posix()}"

# 服务端口与主机配置
SERVER_HOST = os.getenv("LIVE_AGENT_HOST", "127.0.0.1")
SERVER_PORT = int(os.getenv("LIVE_AGENT_PORT", "18080"))

# 读取单一版本源 version.json (SSOT)
import json
VERSION_FILE = BASE_DIR / "version.json"

def _load_version_meta() -> dict:
    if VERSION_FILE.exists():
        try:
            return json.loads(VERSION_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"version": "1.7.0", "service": "AI-LiveStream-Agent", "protocol_version": "v1"}

VERSION_META = _load_version_meta()
APP_VERSION = VERSION_META.get("version", "1.7.0")
SERVICE_NAME = VERSION_META.get("service", "AI-LiveStream-Agent")


# 本地加密主密钥 (AES-256-GCM) —— 规划 §9.6 "系统级秘钥保护"落地：
# 密钥本体经 Windows DPAPI (CryptProtectData，由当前用户/机器凭据派生) 加密后落盘，
# 文件形态为 "DPAPI1:" + 保护二进制；非 Windows 或 DPAPI 不可用时回退原始字节。
# 旧版明文 32 字节密钥文件在首次加载时零损迁移 (密钥不变，存量 AES 密文可继续解密)。
KEY_FILE = DATA_DIR / ".master.key"
DPAPI_KEY_PREFIX = b"DPAPI1:"
_DPAPI_AVAILABLE = (sys.platform == "win32")


class _DPAPI_BLOB(ctypes.Structure):
    _fields_ = [("cbData", ctypes.c_ulong), ("pbData", ctypes.POINTER(ctypes.c_char))]


def _dpapi_blob(data: bytes) -> "_DPAPI_BLOB":
    buf = ctypes.create_string_buffer(bytes(data), len(data))
    return _DPAPI_BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))


def _dpapi_free(blob_ptr) -> None:
    try:
        kernel32 = ctypes.WinDLL("kernel32")
        kernel32.LocalFree.restype = ctypes.c_void_p
        kernel32.LocalFree.argtypes = [ctypes.c_void_p]
        addr = ctypes.cast(blob_ptr, ctypes.c_void_p).value
        if addr:
            kernel32.LocalFree(ctypes.c_void_p(addr))
    except Exception:
        pass


def _dpapi_protect(data: bytes):
    """DPAPI 加密字节，失败返回 None"""
    if not _DPAPI_AVAILABLE:
        return None
    try:
        crypt32 = ctypes.WinDLL("crypt32")
        crypt32.CryptProtectData.restype = ctypes.c_int
        crypt32.CryptProtectData.argtypes = [
            ctypes.POINTER(_DPAPI_BLOB),   # pDataIn
            ctypes.c_wchar_p,              # szDataDescr
            ctypes.POINTER(_DPAPI_BLOB),   # pOptionalEntropy
            ctypes.c_void_p,               # pvReserved
            ctypes.c_void_p,               # pPromptStruct
            ctypes.c_ulong,                # dwFlags
            ctypes.POINTER(_DPAPI_BLOB),   # pDataOut
        ]
        inp = _dpapi_blob(data)
        out = _DPAPI_BLOB()
        ok = crypt32.CryptProtectData(
            ctypes.byref(inp), None, None, None, None, 0, ctypes.byref(out)
        )
        if not ok or not out.pbData:
            return None
        result = ctypes.string_at(out.pbData, out.cbData)
        _dpapi_free(out.pbData)
        return result
    except Exception:
        return None


def _dpapi_unprotect(blob: bytes):
    """DPAPI 解密字节，失败返回 None"""
    if not _DPAPI_AVAILABLE:
        return None
    try:
        crypt32 = ctypes.WinDLL("crypt32")
        crypt32.CryptUnprotectData.restype = ctypes.c_int
        crypt32.CryptUnprotectData.argtypes = [
            ctypes.POINTER(_DPAPI_BLOB),   # pDataIn
            ctypes.POINTER(ctypes.c_wchar_p),  # ppszDataDescr
            ctypes.POINTER(_DPAPI_BLOB),   # pOptionalEntropy
            ctypes.c_void_p,               # pvReserved
            ctypes.c_void_p,               # pPromptStruct
            ctypes.c_ulong,                # dwFlags
            ctypes.POINTER(_DPAPI_BLOB),   # pDataOut
        ]
        inp = _dpapi_blob(blob)
        out = _DPAPI_BLOB()
        descr = ctypes.c_wchar_p()
        ok = crypt32.CryptUnprotectData(
            ctypes.byref(inp), ctypes.byref(descr), None, None, None, 0, ctypes.byref(out)
        )
        if not ok or not out.pbData:
            return None
        result = ctypes.string_at(out.pbData, out.cbData)
        _dpapi_free(out.pbData)
        return result
    except Exception:
        return None


def load_or_create_master_key(key_file: Path) -> bytes:
    """
    加载或创建 AES-256 主密钥：
    - DPAPI 形态文件 -> 解密还原 32 字节 (解密失败显式报错，严禁静默换钥)
    - 旧版明文 32 字节 -> 原密钥零损迁移为 DPAPI 形态
    - 损坏/空文件 -> 重新生成
    - DPAPI 不可用 (非 Windows) -> 回退原始 32 字节形态
    """
    if key_file.exists():
        raw = key_file.read_bytes()
        if raw.startswith(DPAPI_KEY_PREFIX):
            key = _dpapi_unprotect(raw[len(DPAPI_KEY_PREFIX):])
            if key and len(key) == 32:
                return key
            raise RuntimeError(
                "主密钥文件无法通过系统凭据解密 (可能由其他 Windows 用户创建)。"
                "请以创建该数据的同一用户身份运行，或明确知悉存量 API Key 将不可恢复时手动删除该密钥文件。"
            )
        if len(raw) == 32:
            protected = _dpapi_protect(raw)
            if protected:
                try:
                    key_file.write_bytes(DPAPI_KEY_PREFIX + protected)
                except Exception:
                    pass
            return raw
        # 损坏文件：重新生成
    key = AESGCM.generate_key(bit_length=256)
    protected = _dpapi_protect(key)
    try:
        key_file.write_bytes(DPAPI_KEY_PREFIX + protected if protected else key)
    except Exception:
        pass
    return key


MASTER_KEY = load_or_create_master_key(KEY_FILE)

def encrypt_secret(plain_text: str) -> str:
    """使用 AES-256-GCM 加密明文机密（如 API Key）"""
    if not plain_text:
        return ""
    aesgcm = AESGCM(MASTER_KEY)
    # 96-bit 随机 Nonce
    nonce = os.urandom(12)
    cipher_bytes = aesgcm.encrypt(nonce, plain_text.encode("utf-8"), None)
    # 拼接 Nonce + CipherText 并进行 Base64 编码
    combined = nonce + cipher_bytes
    return base64.b64encode(combined).decode("utf-8")

def decrypt_secret(cipher_b64: Any) -> str:
    """使用 AES-256-GCM 解密密文字符串"""
    if not cipher_b64:
        return ""
    try:
        cipher_str = str(cipher_b64)
        combined = base64.b64decode(cipher_str.encode("utf-8"))
        if len(combined) < 12:
            return ""
        nonce = combined[:12]
        cipher_bytes = combined[12:]
        aesgcm = AESGCM(MASTER_KEY)
        decrypted_bytes = aesgcm.decrypt(nonce, cipher_bytes, None)
        return decrypted_bytes.decode("utf-8")
    except Exception as e:
        # 若解密失败返回空字符串
        return ""

def mask_api_key(raw_key: str) -> str:
    """脱敏展示 API Key，如 sk-proj-••••••••abcd"""
    if not raw_key:
        return ""
    if len(raw_key) <= 8:
        return "••••••••"
    return f"{raw_key[:4]}••••••••{raw_key[-4:]}"
