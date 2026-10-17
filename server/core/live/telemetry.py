# -*- coding: utf-8 -*-
"""违规词与完成弹幕的落库流水。

由 server/routes/live.py 的 LiveSessionController 拆出。
缝在**函数体内**从 server.routes.live 解析，见 test_live_seam_late_binding.py。
"""
import logging
import uuid
from server.database.models import BarrageLog, ProhibitedWordLog
from typing import Optional
logger = logging.getLogger("LiveAgent.LiveController")


class TelemetryMixin:
    """违规词与完成弹幕的落库流水"""
    async def _log_guardrail_hits(self, original: str, sanitized: str, hits: list, is_dropped: bool, session_id: Optional[str]):
        """后台异步写入违禁词拦截日志"""
        self.stats["guardrail_hits"] += len(hits)
        from server.database.db import AsyncSessionLocal
        try:
            async with AsyncSessionLocal() as session:
                for hit in hits:
                    log_item = ProhibitedWordLog(
                        id=f"log_{uuid.uuid4().hex[:8]}",
                        session_id=session_id or "session_default",
                        matched_word=hit["matched_word"],
                        category=hit["category"],
                        original_sentence=original,
                        processed_sentence=sanitized,
                        action_taken="drop" if is_dropped else hit["action"]
                    )
                    session.add(log_item)
                    # alert 命中按策略语义属于高危放行不可播报：整句已被拦截，
                    # 与 drop 的区别是它面向人工复核的严重等级，单独告警留痕
                    if is_dropped and hit["action"] == "alert":
                        logger.warning(
                            "⚠️ 高危告警词命中，整句已拦截禁止播报 (人工复核): session=%s word=%s category=%s sentence=%s",
                            session_id or "session_default", hit["matched_word"], hit["category"], original[:200]
                        )
                await session.commit()
        except Exception:
            logger.exception("写入违禁词审计日志失败")


    async def _log_barrage_complete(self, event, ai_reply: str = "", latency_ms: int = 0, session_id: Optional[str] = None):
        """后台异步记录弹幕交互完成历史 (含 AI 回复与响应延迟)"""
        from server.database.db import AsyncSessionLocal
        try:
            async with AsyncSessionLocal() as session:
                barrage_log = BarrageLog(
                    id=f"b_{uuid.uuid4().hex[:8]}",
                    session_id=session_id or "session_default",
                    platform=event.payload.get("platform") or getattr(self, "platform", "bilibili"),
                    user_id=event.user_name,
                    user_nickname=event.user_name,
                    raw_message=event.payload.get("text") or event.payload.get("gift_name") or event.event_type,
                    event_type=event.event_type,
                    priority_level=event.priority,
                    ai_reply_text=(ai_reply or "")[:500],
                    response_latency_ms=latency_ms,
                    is_interrupted=1 if self.event_queue.is_interrupted_flag else 0
                )
                session.add(barrage_log)
                await session.commit()
        except Exception:
            logger.exception("写入弹幕审计日志失败")
