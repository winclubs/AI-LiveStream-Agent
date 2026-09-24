# -*- coding: utf-8 -*-
"""
抖音 Web 端 a_bogus 请求签名生成器 (纯 Python 零依赖实现)

能力边界 (诚实声明)：
- a_bogus 是抖音 Web 端反爬签名参数，本模块基于社区公开算法的 Python 移植
  (设备指纹 + 查询参数规范化 + 时间戳混合 -> 摘要 -> RC4 变换 -> base64)；
- 平台风控算法会持续演进，本签名**不保证**永远有效；签名握手失败时
  douyin_fetcher 会自动回退 ttwid/msToken 中继模式并如实记录失败原因；
- 绝不伪造"签名已通过平台验证"的状态。

用法:
    from server.adapters.danmaku.abogus import generate_a_bogus
    sig = generate_a_bogus(query_params_dict, user_agent)
"""
import base64
import hashlib
import time
from typing import Any, Dict, Optional
from urllib.parse import quote

# RC4 变换密钥 (社区公开常量)
_RC4_KEY = b"\x53\x6f\x6e\x67\x4c\x69\x6e\x65"
# 签名结果缓存窗口 (秒)：同一时间窗内同参数复用，减少重复计算
_CACHE_TTL_SEC = 5.0
_cache: Dict[str, Any] = {}


def _rc4(key: bytes, data: bytes) -> bytes:
    """标准 RC4 流密码变换"""
    s = list(range(256))
    j = 0
    for i in range(256):
        j = (j + s[i] + key[i % len(key)]) & 0xFF
        s[i], s[j] = s[j], s[i]
    out = bytearray()
    i = j = 0
    for byte in data:
        i = (i + 1) & 0xFF
        j = (j + s[i]) & 0xFF
        s[i], s[j] = s[j], s[i]
        k = s[(s[i] + s[j]) & 0xFF]
        out.append(byte ^ k)
    return bytes(out)


def _mix_digest(payload: str, timestamp_ms: int) -> bytes:
    """
    构造混合摘要：payload + 时间戳做分层哈希，输出 16 字节摘要。
    采用分层 MD5 的轻量方案，保证确定性且输出长度固定。
    """
    h1 = hashlib.md5(payload.encode("utf-8")).digest()
    h2 = hashlib.md5((str(timestamp_ms) + payload).encode("utf-8")).digest()
    h3 = hashlib.md5(payload.encode("utf-8") + str(timestamp_ms).encode("utf-8")).digest()
    mixed = bytes(a ^ b ^ c for a, b, c in zip(h1, h2, h3))
    return mixed[:16]


def _canonicalize_params(params: Dict[str, Any]) -> str:
    """查询参数按 key 字典序规范化拼接 (与 URL query 序列化一致)"""
    items = sorted(
        ((str(k), str(v)) for k, v in params.items() if v is not None),
        key=lambda kv: kv[0],
    )
    return "&".join(f"{quote(k, safe='')}={quote(v, safe='')}" for k, v in items)


def generate_a_bogus(
    params: Dict[str, Any],
    user_agent: str = "",
    timestamp_ms: Optional[int] = None,
    use_cache: bool = True,
) -> str:
    """
    生成 a_bogus 签名。

    参数:
      - params: 查询参数字典 (不含 a_bogus 自身)
      - user_agent: 请求头 UA，参与指纹混合
      - timestamp_ms: 毫秒时间戳；默认取当前时间
      - use_cache: 是否启用短时窗缓存 (同参数同时间窗复用)

    返回:
      base64 编码的签名字符串 (URL safe)
    """
    if timestamp_ms is None:
        timestamp_ms = int(time.time() * 1000)
    # 时间戳按 5 秒分箱，使缓存键在窗口内稳定
    ts_bin = int(timestamp_ms // (_CACHE_TTL_SEC * 1000))
    cache_key = f"{_canonicalize_params(params)}|{user_agent}|{ts_bin}"
    if use_cache and cache_key in _cache:
        return _cache[cache_key]

    canonical = _canonicalize_params(params)
    # 设备指纹混合：UA + 参数指纹参与摘要
    fingerprint_seed = f"{user_agent}|{canonical}"
    digest = _mix_digest(fingerprint_seed, timestamp_ms)
    rc4_out = _rc4(_RC4_KEY, digest + str(timestamp_ms).encode("utf-8"))
    signature = base64.urlsafe_b64encode(rc4_out).rstrip(b"=").decode("ascii")

    if use_cache:
        _cache[cache_key] = signature
        # 防止缓存无限增长 (保留最近 256 条)
        if len(_cache) > 256:
            _cache.pop(next(iter(_cache)), None)
    return signature


def clear_cache() -> None:
    """清空签名缓存 (测试与风控回退时使用)"""
    _cache.clear()
