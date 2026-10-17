# -*- coding: utf-8 -*-
"""事件入队、优先级消费循环与弹幕路由。

由 server/routes/live.py 的 LiveSessionController 拆出。
缝在**函数体内**从 server.routes.live 解析，见 test_live_seam_late_binding.py。
"""
import logging
import asyncio
import inspect
import time
import uuid
from server.core.guardrails.humanizer import jitter_speed
from server.core.media.stitcher_protocol import ResumePolicy, StitcherState
from server.database.models import Product
from sqlalchemy import select
from typing import Any
logger = logging.getLogger("LiveAgent.LiveController")


class QueueIngestMixin:
    """事件入队、优先级消费循环与弹幕路由"""
    def _on_danmaku_circuit_state_change(self, new_state: str, reason: str):
        """向前端 WebSocket 广播弹幕抓取熔断状态更新"""
        from server.routes.ws_live import ws_manager
        logger.warning("【弹幕熔断器】广播状态: %s - %s", new_state, reason)
        self._track_task(
            ws_manager.broadcast("danmaku_circuit_status", {
                "state": new_state,
                "reason": reason,
                "timestamp": time.time(),
            })
        )


    async def _on_barge_in(self, reason: str):
        from server.routes.live import _call_async_compat, _call_sync_compat, global_media_driver, global_virtual_audio
        self._audio_generation += 1
        generation = self._audio_generation
        # 先推进本地播放 fence，保证任何迟到 producer 都不能在异步中断期间复活旧音频。
        try:
            _call_sync_compat(
                global_virtual_audio.stop,
                next_generation=generation,
            )
        except Exception:
            logger.exception("推进 VirtualAudio 打断代际失败")
        # 再通知异步口型媒体与 TTS 取消其内部工作；各 sink 故障互不阻断。
        try:
            await _call_async_compat(
                global_media_driver.interrupt,
                reason,
                next_generation=generation,
            )
        except Exception:
            logger.exception("媒体驱动打断失败")
        # 物理取消当前正在进行的底层网络/生成协程
        if self.current_tts_task and not self.current_tts_task.done():
            self.current_tts_task.cancel()
        try:
            await self.tts_driver.interrupt(reason)
        except Exception:
            logger.exception("TTS 驱动打断失败")
        # 联动数字人驱动瞬间打断 (清空口型与音频队列，恢复待机)
        try:
            from server.core.avatar import get_active_avatar_driver
            await get_active_avatar_driver().flush_talk()
        except Exception:
            logger.exception("数字人驱动 flush_talk 打断失败")
        # 联动动静分离缝合器打断 (Smoothstep 缓动停顿并挂起切片断点)
        try:
            self.stitcher.trigger_interrupt(ResumePolicy.REWIND_SENTENCE)
        except Exception:
            logger.exception("切片缝合器打断失败")
        # 向前端 WebSocket 广播打断信令
        from server.routes.ws_live import ws_manager
        await ws_manager.broadcast("TRIGGER_BARGE_IN", {
            "reason": reason,
            "audio_generation": self._audio_generation,
        })


    async def ingest_event(
        self,
        event_type: str,
        user_name: str,
        payload: dict,
        priority: int = 2,
        source: str = "real",
        is_mock: bool = False,
    ) -> dict:
        """
        统一事件入口：Webhook、WebSocket、真实平台与仿真测试均由此收敛。
        参数标准化、正式指标统计隔离（杜绝 Mock 污染）、实时大屏广播、队列背压与审计。
        """
        if not self.is_live:
            return {"accepted": False, "reason": "not_live"}

        is_mock_event = bool(is_mock or payload.get("_is_mock") or payload.get("_is_fallback") or source == "mock")
        payload["_source"] = source
        payload["_is_mock"] = is_mock_event
        metric_kind = "danmaku" if event_type in ("danmaku", "chat") else event_type

        # 正式入口指标只统计真实事件；瀑布流仍展示 received（包括 Mock）。
        if not is_mock_event:
            self.stats["events_received_total"] = int(self.stats.get("events_received_total") or 0) + 1
            if metric_kind in ("danmaku", "gift"):
                key_recv = f"{metric_kind}_received_total"
                self.stats[key_recv] = int(self.stats.get(key_recv) or 0) + 1

        # 实时广播大屏瀑布流
        from server.routes.ws_live import ws_manager
        text_content = payload.get("text") or (
            f"送出【{payload.get('gift_name')}】x{payload.get('count', 1)}"
            if event_type == "gift"
            else "进入直播间"
        )
        self._track_task(
            ws_manager.broadcast("danmaku", {
                "user": user_name,
                "text": text_content,
                "event_type": event_type,
                "priority": priority,
                "is_mock": is_mock_event,
            })
        )

        event_id = payload.get("event_id") or f"evt_{uuid.uuid4().hex[:8]}"
        res = await self.event_queue.put(
            event_id=event_id,
            event_type=event_type,
            user_name=user_name,
            payload=payload,
            priority=priority,
            as_result=True,
        )
        if not is_mock_event:
            if res.accepted:
                self.stats["events_accepted_total"] = int(self.stats.get("events_accepted_total") or 0) + 1
                if metric_kind in ("danmaku", "gift"):
                    key_acc = f"{metric_kind}_accepted_total"
                    self.stats[key_acc] = int(self.stats.get(key_acc) or 0) + 1
                # 运营兼容指标与收入只累计成功受理的真实事件。
                if metric_kind == "gift":
                    total_coin = payload.get("total_coin", 0) or 0
                    self.stats["gift_income_yuan"] = round(
                        float(self.stats.get("gift_income_yuan") or 0.0) + total_coin / 1000.0,
                        2,
                    )
                    self.stats["gift_count"] = int(self.stats.get("gift_count") or 0) + 1
                elif metric_kind == "danmaku":
                    self.stats["danmaku_count"] = int(self.stats.get("danmaku_count") or 0) + 1
            else:
                self.stats["events_dropped_total"] = int(self.stats.get("events_dropped_total") or 0) + 1
                if metric_kind in ("danmaku", "gift"):
                    key_drop = f"{metric_kind}_dropped_total"
                    self.stats[key_drop] = int(self.stats.get(key_drop) or 0) + 1

        # accepted 新事件可能驱逐旧事件；dropped 必须按被驱逐项自身的来源和类型归属。
        if res.accepted and res.dropped_event_id and res.dropped_is_mock is False:
            dropped_kind = "danmaku" if res.dropped_event_type in ("danmaku", "chat") else res.dropped_event_type
            self.stats["events_dropped_total"] = int(self.stats.get("events_dropped_total") or 0) + 1
            if dropped_kind in ("danmaku", "gift"):
                key_evict = f"{dropped_kind}_dropped_total"
                self.stats[key_evict] = int(self.stats.get(key_evict) or 0) + 1

        return {
            "accepted": bool(res),
            "event_id": event_id,
            "dropped_event_id": getattr(res, "dropped_event_id", None),
            "reason": getattr(res, "reason", None),
        }


    def _on_danmaku_event(self, event_type: str, user_name: str, payload: dict, priority: int = 2):
        is_mock = bool(payload.get("_is_mock") or payload.get("_is_fallback"))
        self._track_task(
            self.ingest_event(
                event_type=event_type,
                user_name=user_name,
                payload=payload,
                priority=priority,
                source="danmaku_fetcher",
                is_mock=is_mock,
            )
        )


    async def _refresh_product_context(self):
        """每个业务事件前刷新活跃商品，场次审计快照保持开播时状态。"""
        from server.database.db import AsyncSessionLocal
        from server.routes.products import product_to_context

        async with AsyncSessionLocal() as session:
            rows = (
                await session.execute(select(Product).where(Product.is_active == 1))
            ).scalars().all()
        self.live_context["products"] = [product_to_context(product) for product in rows]


    async def _consume_queue_loop(self):
        """核心业务循环：取事件 -> 弹幕聚合 -> 角色大脑(LLM+RAG+MCP) -> 违禁词过滤 -> 人类化 -> TTS -> 广播"""
        from server.routes.live import _SENTENCE_DELIMITERS, global_media_driver, global_role_manager
        from server.routes.ws_live import ws_manager
        from server.core.queue.priority_queue import LiveEventItem
        sentence_delimiters = _SENTENCE_DELIMITERS

        while self.is_live:
            try:
                # 若被打断，先复位
                self.event_queue.reset_interrupt()

                # 取出最高优先级事件：若 8 秒内无观众弹幕，自动触发 P3 冷场垫场
                try:
                    event = await asyncio.wait_for(self.event_queue.get(), timeout=8.0)
                except asyncio.TimeoutError:
                    if not self.is_live:
                        break
                    event = LiveEventItem(
                        priority=3,
                        timestamp=time.time(),
                        event_id=f"evt_idle_{uuid.uuid4().hex[:6]}",
                        event_type="idle_filler",
                        user_name="系统调度",
                        payload={"text": "冷场轮播讲解"}
                    )

                if not event:
                    continue

                # 事件出队即复位打断令牌：P0 事件自身触发的 barge-in 信号到此消费完毕，
                # 后续若再次被打断，将由新的 P0 触发重新置位 (粘滞令牌语义，见 priority_queue)
                self.event_queue.reset_interrupt()

                event_started_at = time.time()
                await self._refresh_product_context()
                active_role = global_role_manager.get_active_role()
                logger.info(f"开始处理直播事件 [P{event.priority}]: {event.event_type} 来自 {event.user_name}")

                # 动作状态机事件联动 (如礼物打赏自动触发动作4致谢，新进观众触发动作1欢迎)
                try:
                    from server.core.avatar import get_action_state_machine
                    get_action_state_machine().evaluate_event(event.event_type, event.payload)
                except Exception:
                    pass

                # 注入多轮对话历史 (转 list 快照，供角色 LLM 上下文引用)
                self.live_context["history"] = list(self.history)

                # 应用角色语速到 TTS 驱动 (含防封杀 ±3% 随机微扰，规划 §14.2)
                try:
                    speed = jitter_speed(getattr(active_role, "speech_speed", 1.0))
                    await self.tts_driver.apply_speech_speed(speed)
                except Exception:
                    pass

                # ---- 滑动窗口弹幕聚合 (规划 §6.2)：相似提问合并后集中答复 ----
                # 注意: Mock 注入器产生 "chat"，B站真实协议产生 "danmaku"，两者都需聚合
                if event.event_type in ("chat", "danmaku") and event.user_name != "运营管理员":
                    agg = await self.aggregator.add_message(event.user_name, event.payload.get("text", ""))
                    if agg is None:
                        # 窗口内已存在相似提问，静默合并等待集中答复
                        logger.debug(f"弹幕已聚合合并: {event.payload.get('text', '')}")
                        continue
                    if agg.get("is_aggregated"):
                        event.payload["text"] = agg["text"]
                        event.payload["aggregated_users"] = agg["users"]
                        event.payload["aggregated_count"] = agg["count"]
                        # 聚合答复时提示角色：这是多位观众的相同提问，需一次向全场集中答复
                        event.user_name = f"{agg['users'][0]}等{agg['count']}位观众"

                # 广播当前播报状态并在退出时严格 finally 复位
                await ws_manager.broadcast("speaking_state", {"is_speaking": True, "user": event.user_name})
                text_buffer = ""
                full_reply = ""
                is_first_sentence = True
                try:
                    stream_or_coro = active_role.process_event(
                        event.event_type,
                        event.user_name,
                        event.payload,
                        self.live_context
                    )
                    stream: Any = (
                        await stream_or_coro
                        if inspect.iscoroutine(stream_or_coro)
                        else stream_or_coro
                    )

                    # P0-1 前瞻预取：仅当已挂载批处理 Provider 时启用并发前瞻路径。
                    # 扩散模型单句渲染耗时通常超过音频时长，必须在「当前句仍在播报时」
                    # 就把下一句提前渲染好；串行 await 会让 LLM 流被播报阻塞，
                    # 根本不存在提前量。批处理路径不存在时保持原串行逻辑不变。
                    prefetcher = getattr(global_media_driver, "_batch_prefetcher", None)
                    if prefetcher is not None:
                        full_reply = await self._speak_with_lookahead(
                            stream, active_role, event
                        )
                    else:
                        async for raw_chunk in stream:
                            # 检查是否中途被更高优先级的 P0 打断
                            if self.event_queue.is_cancelled():
                                logger.info("当前播报已被更高优先级抢占打断")
                                break

                            text_buffer += raw_chunk
                            parts = sentence_delimiters.split(text_buffer)

                            # 优先研判是否满足断句输出条件 (标准标点断句 或 首句微块极速开嗓)
                            ready_sentence = None
                            if len(parts) >= 3:
                                ready_sentence = parts[0] + parts[1]
                                text_buffer = "".join(parts[2:])
                            elif is_first_sentence and len(text_buffer) >= 6:
                                # 首句微块切片机制：前 6~12 字快速寻找断句点，将首字延迟压缩至 300ms 内
                                for punc in ("，", "！", "？", " ", "、", "。"):
                                    if punc in text_buffer:
                                        idx = text_buffer.find(punc)
                                        if idx >= 3:  # 确保微块至少 4 字，避免单个字断句过碎
                                            ready_sentence = text_buffer[: idx + 1]
                                            text_buffer = text_buffer[idx + 1 :]
                                            break
                                if not ready_sentence and len(text_buffer) >= 12:
                                    ready_sentence = text_buffer[:10]
                                    text_buffer = text_buffer[10:]

                            if ready_sentence:
                                is_first_sentence = False
                                speak_text = await self._prepare_sentence(
                                    ready_sentence, active_role
                                )
                                if not speak_text:
                                    continue

                                full_reply += speak_text
                                await self._speak_sentence(speak_text, active_role)

                        # 处理缓冲区剩余的尾句
                        if text_buffer.strip() and not self.event_queue.is_cancelled():
                            speak_text = await self._prepare_sentence(
                                text_buffer, active_role
                            )
                            if speak_text:
                                full_reply += speak_text
                                await self._speak_sentence(speak_text, active_role)
                except Exception as llm_err:
                    logger.error("LLM 话术生成或流迭代异常: %s", llm_err, exc_info=True)
                    if self.execution_mode in ("PRODUCTION", "PILOT"):
                        await self.transition_to_ai_paused(f"LLM 话术生成失败: {llm_err}")
                finally:
                    # 无论正常结束、打断 break 还是异常，强制广播复位平息态
                    await ws_manager.broadcast("speaking_state", {"is_speaking": False})
                    # 动静分离核心：实时答复结束，驱动缝合器 Smoothstep 4 步缓动回切切片断点并重锚定时钟
                    if self.stitcher.state == StitcherState.STREAMING_REALTIME:
                        self.stitcher.finish_realtime_reply()

                # 维护多轮对话历史供 LLM 上下文引用
                if full_reply.strip():
                    q = event.payload.get("text") or event.payload.get("gift_name") or event.event_type
                    self.history.append({"role": "user", "content": f"{event.user_name}:{q}"})
                    self.history.append({"role": "assistant", "content": full_reply[:300]})

                # 记录该弹幕事件完成日志 (含 AI 回复与端到端响应延迟)
                if event and event.event_type != "idle_filler":
                    latency_ms = int((time.time() - event_started_at) * 1000)
                    self._track_task(self._log_barrage_complete(
                        event, full_reply, latency_ms, self.session_id
                    ))

                # 重置打断标记
                self.event_queue.reset_interrupt()

            except asyncio.CancelledError:
                if not self.is_live:
                    break
                logger.warning("直播消费循环收到打断或取消信号，但会话仍在进行中，自动保持消费主循环存活")
                await asyncio.sleep(0.05)
            except Exception as e:
                logger.error(f"直播主循环异常: {e}", exc_info=True)
                await asyncio.sleep(0.5)
