import asyncio
import time
from typing import Optional, Dict, Any, Callable, List
from dataclasses import dataclass, field

@dataclass(order=True)
class LiveEventItem:
    """四级优先级队列项 (Priority: 0最高, 3最低)"""
    priority: int
    timestamp: float = field(compare=False)
    event_id: str = field(compare=False)
    event_type: str = field(compare=False)  # gift / chat / follow / idle_filler
    user_name: str = field(compare=False)
    payload: Dict[str, Any] = field(compare=False)

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
    def __init__(self, on_interrupt_callback: Optional[Callable] = None):
        self._queue: asyncio.PriorityQueue[LiveEventItem] = asyncio.PriorityQueue()
        self._cancel_token = asyncio.Event()
        self._on_interrupt_callback = on_interrupt_callback
        self.is_interrupted_flag = False

    async def put(self, event_id: str, event_type: str, user_name: str, payload: Dict[str, Any], priority: int = 2):
        """入队新事件，如果为 P0 则触发抢占打断"""
        item = LiveEventItem(
            priority=priority,
            timestamp=time.time(),
            event_id=event_id,
            event_type=event_type,
            user_name=user_name,
            payload=payload
        )

        if priority == 0:
            detail = payload.get("gift_name") or payload.get("text") or event_type
            # 立即触发抢占打断
            await self.trigger_barge_in(f"收到用户【{user_name}】的高优先级事件: {detail}")

        await self._queue.put(item)

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

    async def get(self) -> LiveEventItem:
        """从队列中获取最高优先级待处理事件"""
        return await self._queue.get()

    def is_cancelled(self) -> bool:
        """检查当前播报是否已被打断 (粘滞令牌，直到 reset_interrupt 复位)"""
        return self._cancel_token.is_set()

    def qsize(self) -> int:
        return self._queue.qsize()

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
