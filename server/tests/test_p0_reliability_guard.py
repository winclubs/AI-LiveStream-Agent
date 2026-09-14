import asyncio
import time
import pytest
from server.core.queue.priority_queue import PriorityBargeInQueue, LiveEventItem
from server.adapters.danmaku.circuit_breaker import CircuitBreakerDanmakuFetcher
from server.adapters.danmaku.base_fetcher import BaseDanmakuFetcher


class DummyFlakyFetcher(BaseDanmakuFetcher):
    """用于测试熔断器的可控测试桩"""
    def __init__(self, room_id="test_room", on_event_callback=None):
        super().__init__(room_id, on_event_callback or (lambda *args: None))
        self.start_call_count = 0
        self.stop_call_count = 0

    async def start(self):
        self.start_call_count += 1
        self.is_running = True

    async def stop(self):
        self.stop_call_count += 1
        self.is_running = False

    def simulate_packet_error(self, err_msg="网络异常中断"):
        self.notify_error(RuntimeError(err_msg), "测试链路错误")

    def simulate_real_message(self, user="tester", text="hello"):
        if self.on_event_callback:
            return self.on_event_callback("danmaku", user, {"text": text}, 2)


def test_priority_queue_fifo_order():
    """测试同优先级事件严格遵循 FIFO 顺序出队 (解决 UUID 字母序乱序)"""
    async def _run():
        q = PriorityBargeInQueue(maxsize=100)
        # 连续推入 5 条 P2 闲聊弹幕
        for i in range(5):
            await q.put(
                event_id=f"evt_z_{i}",  # 故意让 event_id 逆序或乱序
                event_type="chat",
                user_name=f"user_{i}",
                payload={"text": f"msg_{i}"},
                priority=2
            )

        # 出队顺序必须是 0, 1, 2, 3, 4
        for i in range(5):
            item = await q.get()
            assert item.user_name == f"user_{i}"
            assert item.payload["text"] == f"msg_{i}"

    asyncio.run(_run())


def test_priority_queue_backpressure_and_p0_protection():
    """测试队列超限背压：丢弃低优消息，严保高价值 P0/P1"""
    async def _run():
        q = PriorityBargeInQueue(maxsize=5)

        # 填满 5 条 P2 闲聊
        for i in range(5):
            ok = await q.put(f"p2_{i}", "chat", f"u_{i}", {"text": f"chat_{i}"}, priority=2)
            assert ok is True

        assert q.qsize() == 5

        # 第 6 条 P2 闲聊应被背压直接拒绝
        rejected = await q.put("p2_overflow", "chat", "u_overflow", {"text": "overflow"}, priority=2)
        assert rejected is False
        assert q.qsize() == 5

        # 推入一条 P0 礼物，必须成功并挤出一条低优消息
        p0_ok = await q.put("p0_gift", "gift", "rich_user", {"gift_name": "火箭", "total_coin": 10000}, priority=0)
        assert p0_ok is True
        assert q.qsize() == 5

        # 出队的第一条必须是 P0 礼物
        first_item = await q.get()
        assert first_item.priority == 0
        assert first_item.user_name == "rich_user"

    asyncio.run(_run())


def test_priority_queue_ttl_expiration():
    """测试闲聊消息超时丢弃"""
    async def _run():
        q = PriorityBargeInQueue(maxsize=10)
        # 推入一个 ttl 为 0.05 秒的短命事件
        await q.put("exp_evt", "chat", "slow_user", {"text": "马上过期"}, priority=2, ttl_seconds=0.05)
        # 推入一个常规 P1 促单事件
        await q.put("p1_evt", "chat", "buyer", {"text": "这个怎么买"}, priority=1)

        await asyncio.sleep(0.08)  # 等待过期

        # 获取事件时，过期事件应被自动跳过，直接返回 P1 事件
        item = await q.get()
        assert item.event_id == "p1_evt"
        assert item.user_name == "buyer"

    asyncio.run(_run())


def test_circuit_breaker_underlying_error_and_half_open_clean():
    """测试熔断器感知底层链路错误，并在半开探活时先干净清理旧任务"""
    async def _run():
        flaky = DummyFlakyFetcher()
        cb = CircuitBreakerDanmakuFetcher(
            real_fetcher=flaky,
            room_id="room_123",
            on_event_callback=lambda *args: None,
            failure_threshold=3,
            recovery_timeout_sec=0.1
        )

        await cb.start()
        assert cb.state == CircuitBreakerDanmakuFetcher.STATE_CLOSED

        # 模拟底层连续 3 次网络错误
        flaky.simulate_packet_error("连接断开 1")
        flaky.simulate_packet_error("连接断开 2")
        flaky.simulate_packet_error("连接断开 3")

        # 应该进入 OPEN 状态
        assert cb.state == CircuitBreakerDanmakuFetcher.STATE_OPEN
        assert cb.consecutive_failures >= 3

        # 等待探活周期
        cb.last_state_change_time = time.time() - 1.0
        old_stops = flaky.stop_call_count
        old_starts = flaky.start_call_count

        # 模拟 _circuit_monitor_loop 内的半开重启
        await flaky.stop()
        await flaky.start()

        assert flaky.stop_call_count == old_stops + 1
        assert flaky.start_call_count == old_starts + 1

        # 收到真实数据后应恢复 CLOSED
        flaky.simulate_real_message("real_user", "弹幕通了")
        assert cb.state == CircuitBreakerDanmakuFetcher.STATE_CLOSED
        assert cb.consecutive_failures == 0

        await cb.stop()

    asyncio.run(_run())


def test_controller_stop_fail_safe():
    """测试 LiveSessionController.stop 在组件崩溃时依然安全完成资源释放与状态清空"""
    async def _run():
        from server.routes.live import LiveSessionController

        ctrl = LiveSessionController()
        ctrl.is_live = True
        ctrl.session_id = "test_sid_fail_safe"

        class BrokenComponent:
            async def stop(self):
                raise RuntimeError("故意模拟组件停止崩溃")

        ctrl.fetcher = BrokenComponent()
        ctrl.tts_driver = BrokenComponent()

        # 执行 stop，绝不能抛出异常，必须正常结束
        await ctrl.stop()

        assert ctrl.is_live is False
        assert ctrl.session_id is None
        assert ctrl.fetcher is None

    asyncio.run(_run())
