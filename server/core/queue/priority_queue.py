import asyncio
import time
import itertools
from typing import Optional, Dict, Any, Callable, List
from dataclasses import dataclass, field

import heapq
import logging

logger = logging.getLogger("LiveAgent.PriorityQueue")

@dataclass
class QueuePutResult:
    """入队结果 (兼容布尔判断与等值比较)"""
    accepted: bool
    dropped_event_id: Optional[str] = None
    dropped_event_type: Optional[str] = None
    dropped_is_mock: Optional[bool] = None
    reason: Optional[str] = None

    def __bool__(self) -> bool:
        return self.accepted

    def __eq__(self, other) -> bool:
        if isinstance(other, bool):
            return self.accepted == other
        if isinstance(other, QueuePutResult):
            return (
                self.accepted == other.accepted
                and self.dropped_event_id == other.dropped_event_id
                and self.dropped_event_type == other.dropped_event_type
                and self.dropped_is_mock == other.dropped_is_mock
                and self.reason == other.reason
            )
        return False


@dataclass
class LiveEventItem:
    """四级优先级队列项 (Priority: 0最高, 3最低)"""
    priority: int
    timestamp: float = field(default_factory=time.time)
    event_id: str = field(default="")
    event_type: str = field(default="chat")  # gift / chat / follow / idle_filler
    user_name: str = field(default="")
    payload: Dict[str, Any] = field(default_factory=dict)
    seq: int = field(default=0)  # 用于同 priority 的严格 FIFO 排序 (itertools.count 生成)
    ttl_seconds: float = field(default=120.0)  # 消息生命周期，超时自动过期

    def __lt__(self, other: "LiveEventItem") -> bool:
        """优先比较 priority (升序, 0最优先); 优先级相同时严格按 seq 升序比较 (保证先进先出 FIFO)"""
        if self.priority != other.priority:
            return self.priority < other.priority
        return self.seq < other.seq

    def is_expired(self, now: Optional[float] = None) -> bool:
        """检查事件是否过期；P0(打赏)与 P1(促单咨询) 永不过期"""
        if self.priority <= 1:
            return False
        t = now or time.time()
        return (t - self.timestamp) > self.ttl_seconds

class PriorityBargeInQueue:
    """
    四级抢占式事件队列与全双工 Barge-in 打断总线
    P0 (最高): 大额礼物打赏 / 房管警告 -> 毫秒级打断正在播放的音频
    P1 (高)  : 促单咨询与特定商品追问
    P2 (中)  : 普通观众打卡与日常闲聊
    P3 (保底): 冷场自动垫场脚本

    打断语义 (协作式)：trigger_barge_in 置位打断令牌后保持粘滞，
    播报协程在句边界通过 is_cancelled() 观察并中止播报，
    由消费方在事件处理完成后调用 reset_interrupt() 复位，绝不自动清除。
    """
    def __init__(self, on_interrupt_callback: Optional[Callable] = None, maxsize: int = 200, p0_maxsize: int = 50, default_ttl: float = 120.0):
        self._queue: asyncio.PriorityQueue[LiveEventItem] = asyncio.PriorityQueue()
        self._cancel_token = asyncio.Event()
        self._on_interrupt_callback = on_interrupt_callback
        self.is_interrupted_flag = False
        self.maxsize = maxsize
        self.p0_maxsize = p0_maxsize
        self.default_ttl = default_ttl
        self._sequence = itertools.count()

        # 指标统计
        self.dropped_p2_total = 0
        self.dropped_p3_total = 0
        self.event_expired_total = 0
        self.hard_maxsize = self.maxsize + self.p0_maxsize
        self.last_put_result: Optional[QueuePutResult] = None

    def _evict_oldest_by_priority(self, target_priority: int) -> Optional[LiveEventItem]:
        """淘汰指定优先级中最旧的项，并返回完整事件供调用方正确归属指标。"""
        underlying = getattr(self._queue, "_queue", None)
        if not underlying:
            return None
        matches = [item for item in underlying if item.priority == target_priority]
        if not matches:
            return None
        # 找到 seq 最小（最旧）的项
        oldest = min(matches, key=lambda x: x.seq)
        underlying.remove(oldest)
        heapq.heapify(underlying)
        if target_priority == 3:
            self.dropped_p3_total += 1
        elif target_priority == 2:
            self.dropped_p2_total += 1
        return oldest

    async def put(
        self,
        event_id: str = "",
        event_type: str = "chat",
        user_name: str = "",
        payload: Optional[Dict[str, Any]] = None,
        priority: int = 2,
        ttl_seconds: Optional[float] = None,
        as_result: bool = False,
    ):
        """入队新事件，支持严格 FIFO、全优先级分级背压与丢弃；默认返回 bool，as_result=True 返回 QueuePutResult"""
        seq = next(self._sequence)
        ttl = ttl_seconds if ttl_seconds is not None else self.default_ttl
        item = LiveEventItem(
            priority=priority,
            timestamp=time.time(),
            event_id=event_id,
            event_type=event_type,
            user_name=user_name,
            payload=payload or {},
            seq=seq,
            ttl_seconds=ttl
        )

        current_size = self._queue.qsize()

        def _ret(res: QueuePutResult):
            self.last_put_result = res
            return res if as_result else res.accepted

        def _accepted_result(dropped_item: Optional[LiveEventItem] = None) -> QueuePutResult:
            if dropped_item is None:
                return QueuePutResult(accepted=True)
            dropped_payload = dropped_item.payload or {}
            return QueuePutResult(
                accepted=True,
                dropped_event_id=dropped_item.event_id,
                dropped_event_type=dropped_item.event_type,
                dropped_is_mock=bool(
                    dropped_payload.get("_is_mock")
                    or dropped_payload.get("_is_fallback")
                    or dropped_payload.get("_source") == "mock"
                ),
            )

        # 全局硬容量保护：防止任何异常中继导致内存无限增长
        if current_size >= self.hard_maxsize:
            logger.error("队列达到全局硬容量限制 (%d)，拒绝入队以保护进程", self.hard_maxsize)
            return _ret(QueuePutResult(accepted=False, reason="hard_capacity_exceeded"))

        # P0 独立容量保护
        if priority == 0:
            underlying = getattr(self._queue, "_queue", [])
            p0_count = sum(1 for x in underlying if x.priority == 0)
            if p0_count >= self.p0_maxsize:
                logger.error("P0 紧急事件达到上限 (%d)，拒绝入队以防内存被恶意打爆", self.p0_maxsize)
                return _ret(QueuePutResult(accepted=False, reason="p0_capacity_exceeded"))

            # 若总队列满，驱逐最低优事件 (先找 P3，再找 P2，再找 P1)
            dropped_item = None
            if current_size >= self.maxsize:
                for evict_p in (3, 2, 1):
                    dropped_item = self._evict_oldest_by_priority(evict_p)
                    if dropped_item:
                        break
            await self._queue.put(item)
            detail = (payload or {}).get("gift_name") or (payload or {}).get("text") or event_type
            await self.trigger_barge_in(f"收到用户【{user_name}】的高优先级事件: {detail}")
            return _ret(_accepted_result(dropped_item))

        # P1 (促单咨询)：满载时驱逐最旧 P3，无则驱逐最旧 P2；若全为高优则拒绝扩展入队
        if priority == 1:
            dropped_item = None
            if current_size >= self.maxsize:
                dropped_item = self._evict_oldest_by_priority(3) or self._evict_oldest_by_priority(2)
                if not dropped_item:
                    logger.warning("队列中全为高优事件且已满载，P1 事件触发背压丢弃")
                    return _ret(QueuePutResult(accepted=False, reason="queue_full_no_lower_priority"))
            await self._queue.put(item)
            return _ret(_accepted_result(dropped_item))

        # P2 (普通闲聊)：满载时优先驱逐最旧 P3，若无 P3 则背压拒绝新 P2 (防冲刷已有消息)
        if priority == 2:
            dropped_item = None
            if current_size >= self.maxsize:
                dropped_item = self._evict_oldest_by_priority(3)
                if not dropped_item:
                    self.dropped_p2_total += 1
                    return _ret(QueuePutResult(accepted=False, reason="queue_full_p2_rejected"))
            await self._queue.put(item)
            return _ret(_accepted_result(dropped_item))

        # P3 (冷场垫场)：满载直接丢弃
        if current_size >= self.maxsize:
            self.dropped_p3_total += 1
            return _ret(QueuePutResult(accepted=False, reason="queue_full_p3_rejected"))

        await self._queue.put(item)
        return _ret(QueuePutResult(accepted=True))

    async def trigger_barge_in(self, reason: str = "Barge-in Interrupt"):
        """毫秒级打断：置位粘滞打断令牌，并通知下游媒体适配器清空缓冲"""
        self.is_interrupted_flag = True
        self._cancel_token.set()

        # 调用回调通知媒体适配器清空缓冲
        if self._on_interrupt_callback:
            try:
                res = self._on_interrupt_callback(reason)
                if asyncio.iscoroutine(res):
                    await res
            except Exception:
                pass

    def reset_interrupt(self):
        """消费方在事件处理完成后复位打断状态 (令牌与标志同时清除)"""
        self.is_interrupted_flag = False
        self._cancel_token.clear()

    async def get(self) -> Optional[LiveEventItem]:
        """从队列中获取最高优先级待处理事件；自动跳过并丢弃已超时的闲聊事件"""
        while True:
            item = await self._queue.get()
            if not item.is_expired():
                return item
            self.event_expired_total += 1
            logger.info("丢弃超时积压弹幕: id=%s, user=%s, 延迟=%.1fs (累计过期=%d)", item.event_id, item.user_name, time.time() - item.timestamp, self.event_expired_total)
            if self._queue.empty():
                return None

    def is_cancelled(self) -> bool:
        """检查当前播报是否已被打断 (粘滞令牌，直到 reset_interrupt 复位)"""
        return self._cancel_token.is_set()

    def qsize(self) -> int:
        return self._queue.qsize()

    def get_stats(self) -> dict:
        return {
            "queue_depth": self._queue.qsize(),
            "dropped_p2_total": self.dropped_p2_total,
            "dropped_p3_total": self.dropped_p3_total,
            "event_expired_total": self.event_expired_total,
            "hard_maxsize": self.hard_maxsize,
        }

    def clear(self):
        """清空队列中所有待消费事件并复位打断状态"""
        while not self._queue.empty():
            try:
                self._queue.get_nowait()
            except Exception:
                break
        self.reset_interrupt()


class BarrageAggregator:
    """
    滑动时间窗口弹幕聚合器：合并短时间内刷屏的重复/相似提问
    相似判定：完全相同 / 短文本互相包含 / SequenceMatcher 相似度 >= 0.82
    聚合策略：首条立即答复；窗口内第 2 条相似提问触发一次合并集中答复；
    输出后开启新一轮聚合周期，任何观众提问都不会被静默吞掉
    """
    SIMILARITY_THRESHOLD = 0.82
    AGGREGATE_TRIGGER_COUNT = 2

    def __init__(self, window_seconds: float = 3.0):
        self.window = window_seconds
        self.buffer: List[Dict[str, Any]] = []
        self._lock = asyncio.Lock()

    @classmethod
    def is_similar(cls, a: str, b: str) -> bool:
        if a == b:
            return True
        if len(a) >= 4 and len(b) >= 4 and (a in b or b in a):
            return True
        from difflib import SequenceMatcher
        return SequenceMatcher(None, a, b).ratio() >= cls.SIMILARITY_THRESHOLD

    async def add_message(self, user: str, text: str) -> Optional[Dict[str, Any]]:
        async with self._lock:
            now = time.time()
            # 清理过期消息
            self.buffer = [m for m in self.buffer if now - m["time"] <= self.window]

            clean_text = text.strip()
            if not clean_text:
                return {"is_aggregated": False, "text": clean_text, "user": user}

            # 检查是否存在相似/重复消息
            for item in self.buffer:
                if self.is_similar(item["text"], clean_text):
                    item["count"] += 1
                    item["users"].append(user)
                    item["time"] = now  # 刷新窗口，续接聚合周期
                    if item["count"] >= self.AGGREGATE_TRIGGER_COUNT:
                        result = {
                            "is_aggregated": True,
                            "text": item["text"],
                            "count": item["count"],
                            "users": item["users"][:]
                        }
                        # 合并答复输出后移除条目开启新一轮聚合周期，避免重复刷屏或吞掉后续提问
                        self.buffer.remove(item)
                        return result
                    return None

            # 首次记录
            self.buffer.append({
                "text": clean_text,
                "count": 1,
                "users": [user],
                "time": now
            })
            return {"is_aggregated": False, "text": clean_text, "user": user}
