# -*- coding: utf-8 -*-
"""播报前置：断句、预取、人性化与 lookahead 管线。

由 server/routes/live.py 的 LiveSessionController 拆出。
缝在**函数体内**从 server.routes.live 解析，见 test_live_seam_late_binding.py。
"""
import logging
import asyncio
import time
import uuid
from server.core.guardrails.aho_corasick import global_guardrail
from server.core.guardrails.humanizer import humanize_text
from server.core.media.av_sync import global_av_sync
logger = logging.getLogger("LiveAgent.LiveController")


class SpeechPrepareMixin:
    """播报前置：断句、预取、人性化与 lookahead 管线"""
    async def _prepare_sentence(self, sentence: str, active_role) -> str:
        """违禁词过滤 + 电商价格审计 + 拟人化；被熔断或空句返回 ""。"""
        complete_sentence = sentence
        speak_text, is_dropped, hits = await self._sanitize_and_humanize(
            complete_sentence, active_role
        )
        if hits:
            self._track_task(self._log_guardrail_hits(
                complete_sentence, speak_text, hits, is_dropped, self.session_id
            ))
        if is_dropped or not speak_text.strip():
            return ""
        return speak_text


    async def _speak_with_lookahead(self, stream, active_role, event) -> str:
        """批处理 Provider 前瞻播报：LLM 流与播报并发，提前渲染下一句。

        为什么必须并发：扩散模型 (LatentSync UNet3D) 单句渲染耗时通常超过音频时长。
        串行 `await _speak_sentence()` 会让 LLM 流被播报阻塞，下一句的文本在当前句
        播完之前根本不存在 —— 预取永远拿不到 ahead 时间，批处理路径必然音画失步。

        本实现把 LLM 流作为生产者协程，与播报消费者解耦：
            生产者：断句 -> 合规过滤 -> 入队 -> 立即为该句发起前瞻预渲染
            消费者：出队播报；命中预取缓存即零延迟上屏

        打断语义完全沿用既有 `event_queue.is_cancelled()`；预取与播报解耦后，
        被打断的句子由 `interrupt_and_clear()` 统一清空，绝不会播出旧画面。
        """
        from server.routes.live import _SENTENCE_DELIMITERS
        sentence_queue: asyncio.Queue = asyncio.Queue(maxsize=2)
        SENTENCE_END = object()
        full_reply = ""
        loop = asyncio.get_running_loop()

        async def producer() -> None:
            """LLM 流生产者：断句、合规、入队，并对每句发起前瞻预渲染。"""
            nonlocal full_reply
            text_buffer = ""
            is_first_sentence = True
            try:
                async for raw_chunk in stream:
                    if self.event_queue.is_cancelled():
                        logger.info("当前播报已被更高优先级抢占打断")
                        break
                    text_buffer += raw_chunk
                    parts = _SENTENCE_DELIMITERS.split(text_buffer)
                    ready_sentence = None
                    if len(parts) >= 3:
                        ready_sentence = parts[0] + parts[1]
                        text_buffer = "".join(parts[2:])
                    elif is_first_sentence and len(text_buffer) >= 6:
                        for punc in ("，", "！", "？", " ", "、", "。"):
                            if punc in text_buffer:
                                idx = text_buffer.find(punc)
                                if idx >= 3:
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
                        await sentence_queue.put(speak_text)
                        self._track_task(self._prefetch_sentence(speak_text))

                if text_buffer.strip() and not self.event_queue.is_cancelled():
                    speak_text = await self._prepare_sentence(text_buffer, active_role)
                    if speak_text:
                        full_reply += speak_text
                        await sentence_queue.put(speak_text)
                        self._track_task(self._prefetch_sentence(speak_text))
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("前瞻播报生产者异常，提前结束")
            finally:
                with_loop = loop.create_task(sentence_queue.put(SENTENCE_END))
                with_loop.add_done_callback(lambda t: t.exception() if not t.cancelled() else None)

        async def consumer() -> None:
            """播报消费者：按序出队播报，优先命中预取缓存。"""
            nonlocal full_reply
            while True:
                item = await sentence_queue.get()
                if item is SENTENCE_END:
                    return
                if self.event_queue.is_cancelled():
                    # 已被抢占：丢弃剩余句子，不再播报
                    continue
                await self._speak_sentence(item, active_role)

        producer_task = asyncio.create_task(producer())
        consumer_task = asyncio.create_task(consumer())
        try:
            await consumer_task
        finally:
            for task in (producer_task, consumer_task):
                if not task.done():
                    task.cancel()
            await asyncio.gather(producer_task, consumer_task, return_exceptions=True)
        return full_reply


    def _prefetch_sentence(self, speak_text: str):
        """把一句文本提前渲染成云端切片（TTS + 批处理），best-effort。

        走独立协程：预取是加速层，任何失败都不得阻塞或影响播报主链路。
        """
        from server.routes.live import global_media_driver

        async def _run() -> None:
            try:
                driver = self.tts_driver
                if driver is None:
                    return
                frames = await self._build_pcm_frames_for_prefetch(speak_text)
                if not frames:
                    return
                await global_media_driver.prefetch_upcoming_sentence(frames)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.debug("句级前瞻预取失败，保持本地 shadow", exc_info=True)

        return _run()


    async def _build_pcm_frames_for_prefetch(self, speak_text: str):
        """为预取合成标准 PCM 帧；不可用时返回 None（跳过预取而非报错）。"""
        try:
            driver = self.tts_driver
            if driver is None:
                return None
            synth_started = time.time()
            audio = await self._collect_tts_sentence(driver, speak_text)
            if not audio:
                return None
            from server.core.media.incremental_audio import global_incremental_audio_pipeline

            tx = global_incremental_audio_pipeline.open_transaction(
                sample_rate=int(getattr(driver, "audio_sample_rate", 24000)),
                channels=int(getattr(driver, "audio_channels", 1)),
                audio_id=f"pf_{self.session_id or 'local'}_{self._audio_generation}_{uuid.uuid4().hex[:8]}",
                audio_generation=self._audio_generation,
                session_generation=self._session_generation,
                text=speak_text,
            )
            tx.append(audio)
            tx.finish()
            frames = list(tx.drain_frames())
            if frames:
                global_av_sync.measure(synth_started)
            return frames
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.debug("预取音频合成失败，跳过该句预取", exc_info=True)
            return None


    async def _sanitize_and_humanize(self, sentence: str, active_role):
        """违禁词扫描平替 -> 电商价格防幻觉双重审计 -> 语音防机械感人类化 (规划 §14.2 / §14.3)"""
        current_platform = getattr(self, "platform", "all") or "all"
        sanitized_sentence, hits, is_dropped = global_guardrail.sanitize(
            sentence, current_role=active_role.role_type, current_platform=current_platform
        )
        if is_dropped or not sanitized_sentence.strip():
            return "", is_dropped, hits

        from server.routes.ws_live import ws_manager
        if hits:
            await ws_manager.broadcast("GUARDRAIL_TRIGGERED", {
                "original": sentence,
                "sanitized": sanitized_sentence,
                "hits": hits
            })

        # 电商带货模式：口播价格防幻觉双重审计
        if active_role.role_type == "ecommerce":
            from server.core.guardrails.price_auditor import global_price_auditor
            prods = self.live_context.get("products", [])
            sanitized_sentence, price_corrected, audit_info = global_price_auditor.audit_sentence(
                sanitized_sentence, all_products=prods
            )
            if price_corrected and audit_info:
                await ws_manager.broadcast("PRICE_AUDIT_WARNING", audit_info)

        speak_text = humanize_text(sanitized_sentence, active_role.role_type)
        return speak_text, False, hits


    async def _collect_tts_sentence(self, driver, text: str, pcm_transaction=None) -> bytes:
        """完整收集一句音频；PCM provider 同时增量帧化，但仅在成功后提交。"""
        from server.routes.live import _call_async_compat
        chunks = []
        total_bytes = 0
        completed = False
        stream = driver.synthesize_stream(text)
        try:
            async for chunk in stream:
                if self.event_queue.is_cancelled():
                    return b""
                if chunk:
                    total_bytes += len(chunk)
                    if total_bytes > self.MAX_TTS_SENTENCE_BYTES:
                        raise RuntimeError(
                            f"TTS 整句编码音频超过 {self.MAX_TTS_SENTENCE_BYTES} bytes"
                        )
                    if pcm_transaction is not None:
                        pcm_transaction.append(chunk)
                    chunks.append(chunk)
            completed = True
            return b"".join(chunks)
        finally:
            if pcm_transaction is not None and not completed:
                pcm_transaction.abort("source_incomplete")
            close_stream = getattr(stream, "aclose", None)
            if close_stream is not None:
                await _call_async_compat(close_stream)
