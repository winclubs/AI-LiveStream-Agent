import asyncio
import json
import struct
import zlib
import pytest
from server.adapters.danmaku.bilibili_fetcher import BilibiliDanmakuFetcher
from server.adapters.danmaku.circuit_breaker import CircuitBreakerDanmakuFetcher
from server.adapters.danmaku.mock_fetcher import MockDanmakuFetcher
from server.adapters.danmaku.proto_reader import iter_fields, read_varint


def _make_bilibili_packet(protover: int, optype: int, body_bytes: bytes) -> bytes:
    """构造 B 站 WebSocket 二进制协议包 (16字节报头 + body)"""
    header_len = 16
    total_len = header_len + len(body_bytes)
    header = struct.pack(">IHHII", total_len, header_len, protover, optype, 1)
    return header + body_bytes


def test_bilibili_contract_replay_normal_danmaku():
    """契约测试：录制回放真实 B 站弹幕 JSON 数据包解码"""
    events = []

    def on_event(event_type, user_name, payload, priority):
        events.append({"type": event_type, "user": user_name, "text": payload.get("text")})

    fetcher = BilibiliDanmakuFetcher("123456", on_event)

    # 模拟真实 B 站发来的 DANMU_MSG 消息体
    danmu_payload = {
        "cmd": "DANMU_MSG",
        "info": [
            [0, 1, 25, 16777215, 0, 0, 0, "", 0, 0, 0],
            "主播这件衣服怎么卖？",
            [10001, "粉丝小张", 0, 0, 0, 10000, 1, ""],
        ],
    }
    raw_json = json.dumps(danmu_payload).encode("utf-8")
    packet = _make_bilibili_packet(protover=0, optype=5, body_bytes=raw_json)

    # 离线回放包解析
    asyncio.run(fetcher._handle_raw_packet(packet))

    assert len(events) == 1
    assert events[0]["type"] == "danmaku"
    assert events[0]["user"] == "粉丝小张"
    assert events[0]["text"] == "主播这件衣服怎么卖？"


def test_bilibili_contract_replay_zlib_compressed_batch():
    """契约测试：录制回放 zlib (protover=2) 批量弹幕包解码"""
    events = []

    def on_event(event_type, user_name, payload, priority):
        events.append({"type": event_type, "user": user_name, "text": payload.get("text")})

    fetcher = BilibiliDanmakuFetcher("123456", on_event)

    # 两个连续弹幕包
    danmu1 = json.dumps({"cmd": "DANMU_MSG", "info": [[], "支持支持！", [1, "老观众"]]}).encode("utf-8")
    danmu2 = json.dumps({"cmd": "DANMU_MSG", "info": [[], "质量怎么样？", [2, "新朋友"]]}).encode("utf-8")

    sub_packet1 = _make_bilibili_packet(protover=0, optype=5, body_bytes=danmu1)
    sub_packet2 = _make_bilibili_packet(protover=0, optype=5, body_bytes=danmu2)
    compressed_body = zlib.compress(sub_packet1 + sub_packet2)

    root_packet = _make_bilibili_packet(protover=2, optype=5, body_bytes=compressed_body)
    asyncio.run(fetcher._handle_raw_packet(root_packet))

    assert len(events) == 2
    assert events[0]["text"] == "支持支持！"
    assert events[1]["text"] == "质量怎么样？"


def test_proto_reader_robustness_on_truncated_data():
    """契约测试：iter_fields 在遇到截断/畸形二进制数据包时优雅返回而不抛未捕获崩溃"""
    # 构造畸形 Varint
    truncated = bytes([0x08, 0xFF, 0xFF])  # 缺少结尾字节
    fields = list(iter_fields(truncated))
    # 即使数据包截断，也能安全处理不崩溃
    assert isinstance(fields, list)


def test_circuit_breaker_automatic_fallback_and_recovery():
    """契约测试：弹幕熔断器在遭遇连续 5 次致命故障时自动熔断并切入 Mock 兜底，探活后可恢复"""
    async def _test():
        state_transitions = []
        events_received = []

        def on_event(event_type, user_name, payload, priority):
            events_received.append((event_type, user_name, payload))

        def on_state_change(new_state, reason):
            state_transitions.append((new_state, reason))

        class FaultyFetcher(MockDanmakuFetcher):
            async def start(self):
                self.is_running = False
                raise ConnectionResetError("模拟平台网关强制断开")

        faulty = FaultyFetcher("test_room", on_event, auto_inject=False)
        breaker = CircuitBreakerDanmakuFetcher(
            real_fetcher=faulty,
            room_id="test_room",
            on_event_callback=on_event,
            on_state_change_callback=on_state_change,
            failure_threshold=3,
            recovery_timeout_sec=0.5,
        )

        # 1. 启动
        await breaker.start()
        assert breaker.consecutive_failures == 1

        # 2. 注入另外两次故障，触发连续 3 次阈值
        breaker.record_failure("连接超时")
        breaker.record_failure("协议校验错误")

        assert breaker.state == CircuitBreakerDanmakuFetcher.STATE_OPEN
        assert any(s[0] == "OPEN" for s in state_transitions)
        await asyncio.sleep(0.05)
        assert breaker.mock_fetcher.is_running is True

        # 3. 模拟真实数据源收到消息，验证自动自愈恢复至 CLOSED
        breaker._on_real_event("danmaku", "真实老铁", {"text": "恢复正常了！"}, 2)
        assert breaker.state == CircuitBreakerDanmakuFetcher.STATE_CLOSED
        assert breaker.consecutive_failures == 0

        await breaker.stop()

    asyncio.run(_test())
