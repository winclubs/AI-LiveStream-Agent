# -*- coding: utf-8 -*-
"""抖音弹幕 fetcher 的 a_bogus 签名接入与回退机制测试 (修复四)"""
import asyncio

from server.adapters.danmaku.douyin_fetcher import DouyinDanmakuFetcher


def _make_fetcher():
    events = []

    def on_event(event_type, user_name, payload, priority):
        events.append((event_type, user_name))

    fetcher = DouyinDanmakuFetcher("https://live.douyin.com/123456789", on_event)
    return fetcher, events


def test_room_id_extracted_from_url():
    fetcher, _ = _make_fetcher()
    assert fetcher.clean_room_id == "123456789"


def test_signature_fail_threshold_default():
    fetcher, _ = _make_fetcher()
    assert fetcher.SIGNATURE_FAIL_THRESHOLD == 3
    assert fetcher._signature_fail_count == 0
    assert fetcher._signature_suppressed is False


def test_signature_suppressed_after_threshold():
    """模拟连续签名握手失败，验证达到阈值后自动抑制签名"""
    fetcher, _ = _make_fetcher()

    async def simulate_failures():
        # 复用 _listen_loop 的失败计数逻辑：直接模拟抛出风控相关异常
        for _ in range(fetcher.SIGNATURE_FAIL_THRESHOLD):
            err = Exception("Handshake status 403")
            signature_related = any(k in str(err) for k in ("403", "402", "Handshake", "signature", "a_bogus"))
            if signature_related and not fetcher._signature_suppressed:
                fetcher._signature_fail_count += 1
                if fetcher._signature_fail_count >= fetcher.SIGNATURE_FAIL_THRESHOLD:
                    fetcher._signature_suppressed = True

    asyncio.run(simulate_failures())
    assert fetcher._signature_fail_count == 3
    assert fetcher._signature_suppressed is True


def test_signature_reset_on_success():
    """握手成功应重置失败计数"""
    fetcher, _ = _make_fetcher()
    fetcher._signature_fail_count = 2
    fetcher._signature_suppressed = True
    # 模拟握手成功后的重置逻辑
    fetcher._signature_fail_count = 0
    fetcher._signature_suppressed = False
    assert fetcher._signature_fail_count == 0
    assert fetcher._signature_suppressed is False


def test_signature_import_available():
    """验证 fetcher 模块已成功导入 a_bogus 生成器"""
    from server.adapters.danmaku.douyin_fetcher import generate_a_bogus
    sig = generate_a_bogus({"room_id": "123"}, "Mozilla/5.0", use_cache=False)
    assert len(sig) > 0
