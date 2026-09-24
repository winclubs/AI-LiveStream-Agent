# -*- coding: utf-8 -*-
"""抖音 a_bogus 签名生成器的单元测试 (修复四)"""
import time

from server.adapters.danmaku.abogus import (
    clear_cache,
    generate_a_bogus,
    _canonicalize_params,
    _rc4,
    _CACHE_TTL_SEC,
)


def test_signature_is_deterministic_for_same_input():
    params = {"room_id": "123456", "app_name": "douyin_web"}
    ts = 1750000000000
    a = generate_a_bogus(params, "Mozilla/5.0", timestamp_ms=ts, use_cache=False)
    b = generate_a_bogus(params, "Mozilla/5.0", timestamp_ms=ts, use_cache=False)
    assert a == b
    assert len(a) > 0


def test_signature_changes_with_different_params():
    ts = 1750000000000
    a = generate_a_bogus({"room_id": "111"}, "Mozilla/5.0", timestamp_ms=ts, use_cache=False)
    b = generate_a_bogus({"room_id": "222"}, "Mozilla/5.0", timestamp_ms=ts, use_cache=False)
    assert a != b


def test_signature_changes_with_different_ua():
    ts = 1750000000000
    a = generate_a_bogus({"room_id": "111"}, "Mozilla/5.0", timestamp_ms=ts, use_cache=False)
    b = generate_a_bogus({"room_id": "111"}, "Mozilla/5.0 Chrome/125", timestamp_ms=ts, use_cache=False)
    assert a != b


def test_signature_changes_with_different_timestamp():
    params = {"room_id": "111"}
    a = generate_a_bogus(params, "Mozilla/5.0", timestamp_ms=1750000000000, use_cache=False)
    b = generate_a_bogus(params, "Mozilla/5.0", timestamp_ms=1750000006000, use_cache=False)
    assert a != b


def test_signature_output_is_url_safe_base64():
    sig = generate_a_bogus({"room_id": "123"}, "Mozilla/5.0", use_cache=False)
    # 不含标准 base64 的 + / 与填充 =
    assert "+" not in sig
    assert "/" not in sig
    assert "=" not in sig
    # 可被 base64 解回
    import base64 as b64
    decoded = b64.urlsafe_b64decode(sig + "=" * (-len(sig) % 4))
    assert len(decoded) >= 16


def test_canonicalize_params_sorted_and_quoted():
    out = _canonicalize_params({"b": "2", "a": "1", "c": None})
    assert out == "a=1&b=2"
    # None 值被丢弃
    out2 = _canonicalize_params({"z": "x y", "a": "1"})
    assert out2 == "a=1&z=x%20y"


def test_cache_reuses_within_window():
    clear_cache()
    params = {"room_id": "12345"}
    # 对齐到当前 5s 分箱起点 +600ms，保证 ts 与 ts+1000 必定同窗、ts+10000 必定跨窗，
    # 彻底消除测试启动时刻贴近窗口边界导致的偶发失败 (flaky)。
    _now_ms = int(time.time() * 1000)
    ts = _now_ms - (_now_ms % (_CACHE_TTL_SEC * 1000)) + 600
    a = generate_a_bogus(params, "Mozilla/5.0", timestamp_ms=ts, use_cache=True)
    # 同 5s 分箱内 (相差 1s) 应命中缓存
    b = generate_a_bogus(params, "Mozilla/5.0", timestamp_ms=ts + 1000, use_cache=True)
    assert a == b
    # 跨窗口 (相差 10s) 缓存键不同，且签名输入时间戳也不同 -> 签名变化
    c = generate_a_bogus(params, "Mozilla/5.0", timestamp_ms=ts + 10000, use_cache=True)
    assert a != c
    clear_cache()


def test_rc4_is_self_inverse():
    key = b"\x01\x02\x03"
    data = b"hello a_bogus"
    encrypted = _rc4(key, data)
    decrypted = _rc4(key, encrypted)
    assert decrypted == data


def test_empty_params_still_produces_signature():
    sig = generate_a_bogus({}, "Mozilla/5.0", use_cache=False)
    assert len(sig) > 0
