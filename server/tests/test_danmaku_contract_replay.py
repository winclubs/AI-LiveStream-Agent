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


def test_kuaishou_contract_replay_dispatch_and_intent():
    """契约测试：快手弹幕适配器消息解析、高意图促单提权与大额礼物 P0 打断"""
    from server.adapters.danmaku.kuaishou_fetcher import KuaishouDanmakuFetcher

    events = []

    def on_event(event_type, user_name, payload, priority):
        events.append({"type": event_type, "user": user_name, "payload": payload, "priority": priority})

    fetcher = KuaishouDanmakuFetcher("https://live.kuaishou.com/u/kuaishou_streamer_888", on_event)
    assert fetcher.clean_room_id == "kuaishou_streamer_888"

    # 1. 普通聊天发言 -> P2
    fetcher._dispatch_event({
        "type": "chat",
        "userName": "老铁666",
        "content": "主播好呀！今天天气不错",
        "userId": "1001",
    })
    assert len(events) == 1
    assert events[0]["priority"] == 2
    assert events[0]["user"] == "老铁666"

    # 2. 促单高意图关键词（多少钱）-> 自动提权 P1
    fetcher._dispatch_event({
        "type": "comment",
        "userName": "想买的粉丝",
        "content": "请问一号链接多少钱？包邮吗？",
        "userId": "1002",
    })
    assert len(events) == 2
    assert events[1]["priority"] == 1
    assert events[1]["payload"]["text"] == "请问一号链接多少钱？包邮吗？"

    # 3. 普通礼物 -> P1
    fetcher._dispatch_event({
        "type": "gift",
        "userName": "路人甲",
        "giftName": "荧光棒",
        "count": 10,
        "totalCoin": 100,
        "userId": "1003",
    })
    assert len(events) == 3
    assert events[2]["priority"] == 1

    # 4. 超级大礼 (穿云箭 50000 币) -> P0 强打断
    fetcher._dispatch_event({
        "type": "gift",
        "userName": "榜一大哥",
        "giftName": "穿云箭",
        "count": 1,
        "totalCoin": 66666,
        "userId": "1004",
    })
    assert len(events) == 4
    assert events[3]["priority"] == 0
    assert events[3]["payload"]["gift_name"] == "穿云箭"


def test_wechat_contract_replay_dispatch_and_intent():
    """契约测试：微信视频号弹幕适配器解析、专业咨询提权与高额打赏 P0 打断"""
    from server.adapters.danmaku.wechat_fetcher import WechatDanmakuFetcher

    events = []

    def on_event(event_type, user_name, payload, priority):
        events.append({"type": event_type, "user": user_name, "payload": payload, "priority": priority})

    fetcher = WechatDanmakuFetcher("liveId=export_wx_channel_live_99", on_event)
    assert fetcher.clean_room_id == "export_wx_channel_live_99"

    # 1. 普通留言 -> P2
    fetcher._dispatch_event({
        "type": "chat",
        "nickname": "视频号观众",
        "content": "打卡签到",
        "fromUsername": "wx_user_1",
    })
    assert len(events) == 1
    assert events[0]["priority"] == 2

    # 2. 专业咨询高意图（老师/咨询）-> P1 提权
    fetcher._dispatch_event({
        "type": 1,
        "nickname": "咨询客户",
        "content": "老师您好，请问这类案件怎么联系咨询？",
        "fromUsername": "wx_user_2",
    })
    assert len(events) == 2
    assert events[1]["priority"] == 1

    # 3. 微信大额礼物 -> P0 强打断
    fetcher._dispatch_event({
        "type": 2,
        "nickname": "热情老铁",
        "giftName": "璀璨爱心",
        "count": 1,
        "totalCoin": 58888,
        "fromUsername": "wx_user_3",
    })
    assert len(events) == 3
    assert events[2]["priority"] == 0


def test_danmaku_registry_multi_platform_support():
    """契约测试：验证 DanmakuFetcherRegistry 成功注册并支持四大核心直播平台"""
    from server.adapters.danmaku.registry import global_danmaku_registry

    platforms = global_danmaku_registry.list_platforms()
    assert "bilibili" in platforms
    assert "douyin" in platforms
    assert "kuaishou" in platforms
    assert "wechat" in platforms

    # 验证能成功创建快手和视频号适配器
    def dummy_cb(*args, **kwargs):
        pass

    ks_fetcher = global_danmaku_registry.create("kuaishou", "ks_room_101", dummy_cb)
    assert ks_fetcher is not None
    assert ks_fetcher.platform_name == "kuaishou"

    wx_fetcher = global_danmaku_registry.create("wechat", "wx_live_202", dummy_cb)
    assert wx_fetcher is not None
    assert wx_fetcher.platform_name == "wechat"
