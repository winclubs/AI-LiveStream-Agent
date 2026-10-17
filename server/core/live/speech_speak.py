# -*- coding: utf-8 -*-
"""播报执行：PCM 流式增量路径与整句合成路径。

由 server/routes/live.py 的 LiveSessionController 拆出。
缝在**函数体内**从 server.routes.live 解析，见 test_live_seam_late_binding.py。

分工：`_speak_sentence` 只做编排（选路 → 合成 → 提交 → 旁路 → 下行），
合成阶段的驱动事务与失败降级留在本模块，媒体提交与下行在 speech_commit.py。
"""
import logging
import asyncio
import base64
import inspect
import time
import uuid
from server.core.media.av_sync import global_av_sync
logger = logging.getLogger("LiveAgent.LiveController")


class SpeechSpeakMixin:
    """播报执行：PCM 流式与整句合成"""
    async def _speak_pcm_streaming_sentence(self, speak_text: str, active_role, driver) -> None:
        """CosyVoice PCM 流的边生成边播放路径。"""
        from server.routes.live import _call_async_compat, _call_sync_compat, _pcm_s16le_to_wav, global_media_driver, global_virtual_audio
        from server.routes.ws_live import ws_manager
        from server.core.media.incremental_audio import global_incremental_audio_pipeline
        from server.core.media.shared_playback_clock import global_shared_playback_clock

        audio_generation = self._audio_generation
        session_generation = self._session_generation
        audio_id = f"{self.session_id or 'local'}_{audio_generation}_{uuid.uuid4().hex[:8]}"
        wall_started = time.time()
        realtime_started = time.monotonic()
        sample_rate = int(getattr(driver, "audio_sample_rate", 24000))
        channels = int(getattr(driver, "audio_channels", 1))
        tx = global_incremental_audio_pipeline.open_transaction(
            sample_rate=sample_rate,
            channels=channels,
            audio_id=audio_id,
            audio_generation=audio_generation,
            session_generation=session_generation,
            text=speak_text,
        )
        prepare = getattr(global_virtual_audio, "prepare_playback", None)
        cursor_prepared = False
        if prepare is not None:
            cursor_prepared = _call_sync_compat(
                prepare,
                audio_id,
                fallback_sample_rate=sample_rate,
                audio_generation=audio_generation,
                session_generation=session_generation,
            ) is not False
            if not cursor_prepared:
                tx.abort("playback_cursor_rejected")
                return

        first_chunk = False
        first_frame = False
        first_pts_ms = int(global_shared_playback_clock.now_ms())
        action_applied = False

        def current() -> bool:
            return self._speech_is_current(audio_generation, session_generation)

        async def emit_frames(frame_batch, *, is_final: bool) -> None:
            nonlocal first_frame, action_applied
            if not frame_batch or not current():
                return
            if not first_frame:
                global_av_sync.record_realtime_latency("audio_first_frame_ms", realtime_started)
                first_frame = True

            await _call_async_compat(
                global_media_driver.feed_audio_frames,
                frame_batch,
                is_final=is_final,
            )
            play_progressive = getattr(global_virtual_audio, "play_frames_progressive", None)
            if play_progressive is not None:
                _call_sync_compat(play_progressive, frame_batch, is_final=is_final)

            raw_pcm = b"".join(frame.data for frame in frame_batch)
            if not action_applied:
                try:
                    from server.core.avatar import get_active_avatar_driver, get_action_state_machine
                    avatar_driver = get_active_avatar_driver()
                    action_code = get_action_state_machine().evaluate_text(speak_text)
                    if avatar_driver and avatar_driver.is_active and action_code is not None:
                        await avatar_driver.set_custom_state(action_code, source="speech_text")
                    action_applied = True
                except Exception:
                    logger.debug("增量音频动作联动失败", exc_info=True)

            try:
                from server.core.avatar import get_active_avatar_driver
                avatar_driver = get_active_avatar_driver()
                if avatar_driver and avatar_driver.is_active:
                    await avatar_driver.push_audio_chunk(
                        raw_pcm,
                        {"sample_rate": sample_rate, "text": speak_text if not action_applied else ""},
                    )
            except Exception:
                logger.debug("增量音频数字人驱动联动失败", exc_info=True)

            try:
                from server.core.media.webrtc_streamer import get_webrtc_stream_manager
                get_webrtc_stream_manager().push_audio(raw_pcm, sample_rate=sample_rate)
            except Exception:
                pass
            try:
                from server.core.media.recorder import get_record_manager
                rec_mgr = get_record_manager()
                if rec_mgr and getattr(rec_mgr, "is_recording", lambda: False)():
                    rec_mgr.feed_audio(raw_pcm, sample_rate=sample_rate)
            except Exception:
                pass

            first = frame_batch[0]
            browser_audio = _pcm_s16le_to_wav(raw_pcm, sample_rate, channels)
            await ws_manager.broadcast("AUDIO_CHUNK", {
                "audio_base64": base64.b64encode(browser_audio).decode("utf-8"),
                "text": speak_text if first.sequence == 0 else "",
                "speaker": active_role.role_name,
                "codec": "wav",
                "source_codec": "pcm_s16le",
                "mime_type": "audio/wav",
                "sample_rate": sample_rate,
                "channels": channels,
                "audio_id": audio_id,
                "sequence": first.sequence,
                "is_first": first.is_first,
                "is_final": bool(is_final and frame_batch[-1].is_final),
                "audio_generation": audio_generation,
                "session_generation": session_generation,
                "delay_ms": int(max(0, min(300, global_av_sync.recommended_delay_ms))),
                "pts_ms": first_pts_ms + int(first.pts_ms),
                "pts_samples": int(first.pts_samples),
                "wallclock_ms": int(time.time() * 1000),
            })

        try:
            stream = driver.synthesize_stream(speak_text)
            async for chunk in stream:
                if not current():
                    tx.abort("generation_changed")
                    return
                if chunk:
                    if not first_chunk:
                        global_av_sync.record_realtime_latency("tts_first_chunk_ms", realtime_started)
                        first_chunk = True
                    tx.append(chunk)
                    await emit_frames(tx.drain_frames(), is_final=False)

            tx.finish()
            await emit_frames(tx.drain_frames(), is_final=True)
            if not first_chunk or not first_frame:
                raise RuntimeError("增量 PCM TTS 未产生有效音频帧")
            global_av_sync.measure(wall_started)
        except asyncio.CancelledError:
            if tx.state == "open":
                tx.abort("task_cancelled")
            raise
        except Exception:
            if tx.state == "open":
                tx.abort("stream_error")
            if cursor_prepared:
                reject = getattr(global_virtual_audio, "reject_playback", None)
                if reject is not None:
                    _call_sync_compat(reject, audio_id, reason="stream_error")
            logger.exception("增量 PCM TTS 提交失败，已丢弃当前句")


    def _speech_is_current(self, audio_generation: int, session_generation: int) -> bool:
        """播报代际栅栏：队列未取消且会话/音频代际均未漂移才允许继续提交。"""
        return (
            not self.event_queue.is_cancelled()
            and audio_generation == self._audio_generation
            and session_generation == self._session_generation
        )


    async def _speak_progressive_pcm(self, speak_text: str, active_role, driver) -> None:
        """增量 PCM 路径的独立 Task 包装（current_tts_task 只挂短生命周期 Task）。"""
        stream_task = asyncio.create_task(
            self._speak_pcm_streaming_sentence(speak_text, active_role, driver)
        )
        self.current_tts_task = stream_task
        try:
            await stream_task
        finally:
            if self.current_tts_task is stream_task:
                self.current_tts_task = None


    def _open_pcm_transaction(self, speak_text, target_driver, *, audio_id, audio_generation, session_generation):
        """为 PCM 驱动开启帧事务；驱动/收集器不具备 PCM 能力时返回 None。"""
        from server.core.media.incremental_audio import global_incremental_audio_pipeline

        try:
            parameters = inspect.signature(self._collect_tts_sentence).parameters
        except (TypeError, ValueError):
            return None
        if "pcm_transaction" not in parameters:
            return None
        target_codec = str(getattr(target_driver, "audio_codec", "mp3") or "").lower()
        if target_codec not in {"pcm_s16le", "pcm16", "s16le"}:
            return None
        return global_incremental_audio_pipeline.open_transaction(
            sample_rate=int(getattr(target_driver, "audio_sample_rate", 24000)),
            channels=int(getattr(target_driver, "audio_channels", 1)),
            audio_id=audio_id,
            audio_generation=audio_generation,
            session_generation=session_generation,
            text=speak_text,
        )


    def _create_collect_task(self, target_driver, speak_text, pcm_transaction):
        """收集整句音频的独立 Task（绝不拿主循环自身 Worker 充当 current_tts_task）。"""
        kwargs = {"pcm_transaction": pcm_transaction} if pcm_transaction is not None else {}
        return asyncio.create_task(self._collect_tts_sentence(target_driver, speak_text, **kwargs))


    async def _synthesize_sentence_audio(self, speak_text, synth_started, audio_id, audio_generation, session_generation):
        """整句合成阶段：驱动采集、事务收尾、失败降级 Edge。

        返回 ``(full_audio, driver, video_request_id, staged_pcm_frames)``；
        抢占打断 / 空音频 / 商业档位熔断 / 降级失败等一切放弃路径返回 ``None``。
        """
        from server.routes.live import EdgeTTSMediaDriver

        def is_current() -> bool:
            return self._speech_is_current(audio_generation, session_generation)

        def open_pcm_transaction(target_driver):
            return self._open_pcm_transaction(
                speak_text, target_driver,
                audio_id=audio_id,
                audio_generation=audio_generation,
                session_generation=session_generation,
            )

        driver = self.tts_driver
        video_request_id = None
        staged_pcm_frames = None

        pcm_transaction = open_pcm_transaction(driver)
        synth_task = self._create_collect_task(driver, speak_text, pcm_transaction)
        self.current_tts_task = synth_task
        fallback_task = None
        try:
            full_audio = await synth_task
            # 紧邻生成器耗尽读取事务 ID，中间不 await，避免其他请求覆盖兼容游标。
            video_request_id = getattr(driver, "last_completed_request_id", None)
            if not is_current():
                if pcm_transaction is not None:
                    pcm_transaction.abort("generation_changed")
                return None
            if not full_audio or len(full_audio) == 0:
                raise RuntimeError("TTS 返回空音频数据 (0 bytes)")
            if pcm_transaction is not None:
                staged_pcm_frames = pcm_transaction.finish()
        except asyncio.CancelledError:
            logger.info("单句 TTS 生成任务已被 P0 抢占打断取消")
            return None
        except Exception as tts_err:
            if not is_current():
                return None
            if self.execution_mode in ("PRODUCTION", "PILOT"):
                logger.error("【商业上线硬门禁】商业生产档位严禁静默切换未经授权的异音色！TTS 故障: %s，已进入 AI_PAUSED", tts_err)
                await self.transition_to_ai_paused(f"TTS 合成失败: {tts_err}")
                return None
            if isinstance(driver, EdgeTTSMediaDriver):
                logger.warning("Edge-TTS 语音合成失败或返回零字节音频，丢弃该句以防爆音: %s", tts_err)
                return None
            logger.warning("TTS 运行期合成失败，丢弃半句并切换 Edge-TTS: %s", tts_err)
            try:
                driver = await self._switch_to_edge_tts()
                if not is_current():
                    return None
                staged_pcm_frames = None
                pcm_transaction = open_pcm_transaction(driver)
                fallback_task = self._create_collect_task(driver, speak_text, pcm_transaction)
                self.current_tts_task = fallback_task
                try:
                    full_audio = await fallback_task
                except asyncio.CancelledError:
                    logger.info("降级 Edge-TTS 单句生成被抢占打断取消")
                    return None
                video_request_id = getattr(driver, "last_completed_request_id", None)
                if not is_current() or not full_audio or len(full_audio) == 0:
                    return None
                if pcm_transaction is not None:
                    staged_pcm_frames = pcm_transaction.finish()
            except Exception:
                logger.exception("Edge-TTS 运行期降级失败")
                return None
        finally:
            if self.current_tts_task in (synth_task, fallback_task):
                self.current_tts_task = None
            if pcm_transaction is not None and pcm_transaction.state == "open":
                pcm_transaction.abort("controller_exit")

        global_av_sync.measure(synth_started)
        if not full_audio or len(full_audio) == 0 or not is_current():
            return None
        return full_audio, driver, video_request_id, staged_pcm_frames


    async def _speak_sentence(self, speak_text: str, active_role):
        """完整合成后一次性提交音频；PCM 流式驱动在能力满足时走增量路径。"""
        driver = self.tts_driver
        if self._supports_progressive_pcm_tts(driver):
            await self._speak_progressive_pcm(speak_text, active_role, driver)
            return

        synth_started = time.time()
        audio_generation = self._audio_generation
        session_generation = self._session_generation
        audio_id = f"{self.session_id or 'local'}_{audio_generation}_{uuid.uuid4().hex[:8]}"

        outcome = await self._synthesize_sentence_audio(
            speak_text, synth_started, audio_id, audio_generation, session_generation,
        )
        if outcome is None:
            return
        full_audio, driver, video_request_id, staged_pcm_frames = outcome

        commit = await self._commit_sentence_audio(
            speak_text,
            full_audio,
            driver,
            audio_id,
            audio_generation,
            session_generation,
            staged_pcm_frames,
            video_request_id,
        )
        if commit is None:
            return

        sample_rate = await self._push_sentence_to_side_channels(speak_text, full_audio, commit)
        await self._broadcast_sentence_audio(
            speak_text, active_role, full_audio, commit, audio_id,
            audio_generation, session_generation, sample_rate,
        )