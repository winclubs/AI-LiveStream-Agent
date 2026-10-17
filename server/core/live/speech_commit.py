# -*- coding: utf-8 -*-
"""播报媒体提交：帧事务解码、播放游标栅栏、旁路分发与浏览器下行。

由 server/routes/live.py 的 LiveSessionController 拆出。
缝在**函数体内**从 server.routes.live 解析，见 test_live_seam_late_binding.py。
"""
import logging
import base64
import time
from typing import Any, NamedTuple
from server.core.media.av_sync import global_av_sync
logger = logging.getLogger("LiveAgent.LiveController")


class _SentenceCommit(NamedTuple):
    """整句音频提交后下游需要的全部静态上下文。"""

    codec: str
    mime_type: str
    sample_rate: int
    channels: int
    pts_ms: int
    framed_audio: Any


class SpeechCommitMixin:
    """播报媒体提交：游标栅栏、旁路联动与浏览器下行"""
    async def _commit_sentence_audio(
        self,
        speak_text,
        full_audio,
        driver,
        audio_id,
        audio_generation,
        session_generation,
        staged_pcm_frames,
        video_request_id,
    ):
        """把整句音频解码为有界 PCM 帧并原子提交到媒体与本地音频链路。

        提交顺序为「先注册不播放的 cursor → 建立口型 → generation fence 后提交音频」；
        任一栅栏失败即放弃本句并回滚游标。放弃路径返回 ``None``。
        """
        from server.routes.live import _call_async_compat, _call_sync_compat, global_media_driver, global_virtual_audio
        from server.core.media.shared_playback_clock import global_shared_playback_clock

        def is_current() -> bool:
            return self._speech_is_current(audio_generation, session_generation)

        codec = str(getattr(driver, "audio_codec", "mp3") or "mp3")
        mime_type = str(getattr(driver, "audio_mime_type", "audio/mpeg") or "audio/mpeg")
        sample_rate = int(getattr(driver, "audio_sample_rate", 24000))
        channels = int(getattr(driver, "audio_channels", 1))
        pts_ms = int(global_shared_playback_clock.now_ms())
        metadata = {
            "codec": codec,
            "sample_rate": sample_rate,
            "channels": channels,
            "audio_generation": audio_generation,
            "session_generation": session_generation,
            "audio_id": audio_id,
        }

        # 第一阶段保留整句事务屏障，成功后统一解码为有界 PCM 帧。解码不可用、
        # 测试替身或旧驱动仍走原字节路径，不改变 partial 丢弃和浏览器兼容契约。
        from server.core.media.audio_pipeline import FramedAudio, global_audio_frame_pipeline
        if staged_pcm_frames is not None:
            framed_audio = FramedAudio(
                frames=staged_pcm_frames,
                format=staged_pcm_frames[0].format,
                source_codec=codec,
                decode_ms=0.0,
            )
        else:
            framed_audio = await global_audio_frame_pipeline.frame_transaction(
                full_audio,
                source_codec=codec,
                sample_rate=sample_rate,
                channels=channels,
                audio_id=audio_id,
                audio_generation=audio_generation,
                session_generation=session_generation,
                text=speak_text,
            )
        if not is_current():
            return None
        playback_sample_rate = framed_audio.format.sample_rate if framed_audio else sample_rate

        # 原子事务：先注册不播放的 cursor，再建立口型，最后在 generation fence 后提交音频。
        # 旧服务/测试替身没有 prepare/frame API 时保持原有兼容路径。
        prepare_playback = getattr(global_virtual_audio, "prepare_playback", None)
        if prepare_playback is None:
            prepare_playback = getattr(global_virtual_audio, "register_playback", None)
        cursor_prepared = False
        audio_commit_resolved = False
        if prepare_playback is not None:
            prepare_metadata = {key: value for key, value in metadata.items() if key != "audio_id"}
            prepare_result = _call_sync_compat(
                prepare_playback,
                audio_id,
                fallback_sample_rate=playback_sample_rate,
                **prepare_metadata,
            )
            # 旧 API 返回 None 继续视为成功；新 API 的 False 表示 cursor 已明确拒绝。
            cursor_prepared = prepare_result is not False
            if prepare_result is False:
                logger.info("播放 cursor 拒绝事务 audio_id=%s，停止媒体提交", audio_id)
                return None
        if not is_current():
            reject_playback = getattr(global_virtual_audio, "reject_playback", None)
            if cursor_prepared and reject_playback is not None:
                _call_sync_compat(reject_playback, audio_id, reason="generation_changed_before_feed")
            return None

        try:
            feed_frames = getattr(global_media_driver, "feed_audio_frames", None)
            if framed_audio is not None and feed_frames is not None:
                await _call_async_compat(feed_frames, framed_audio.frames)
            else:
                # 兼容驱动可能执行异步完整容器解码；它看到 audio_id 时 cursor 已存在且尚未开始。
                await _call_async_compat(
                    global_media_driver.feed_audio_chunk,
                    full_audio,
                    speak_text,
                    **metadata,
                )
            if not is_current():
                return None

            play_frames = getattr(global_virtual_audio, "play_frames", None)
            if framed_audio is not None and play_frames is not None:
                play_result = _call_sync_compat(play_frames, framed_audio.frames)
            else:
                play_result = _call_sync_compat(
                    global_virtual_audio.play_chunk,
                    full_audio,
                    fallback_sample_rate=sample_rate,
                    **metadata,
                )
            # 返回 False 的新 API 已自行把 cursor 标为 rejected；浏览器通道仍作为显式软降级。
            # 旧 API 返回 None 继续视为已处理，保持替身兼容。
            audio_commit_resolved = True
            if play_result is False:
                logger.info("本地音频提交被拒绝，保留浏览器降级 audio_id=%s", audio_id)
            if not is_current():
                return None
        finally:
            if cursor_prepared and not audio_commit_resolved:
                reject_playback = getattr(global_virtual_audio, "reject_playback", None)
                if reject_playback is not None:
                    _call_sync_compat(reject_playback, audio_id, reason="commit_aborted")

        # 远程视频绑定同一 audio_id；commit 本身只启动异步时间线，不阻塞事件循环。
        commit_video = getattr(driver, "commit_video_frames", None)
        if commit_video is not None:
            commit_kwargs = {
                "codec": codec,
                "sample_rate": sample_rate,
                "channels": channels,
                "audio_id": audio_id,
            }
            if video_request_id:
                commit_kwargs["request_id"] = video_request_id
            await _call_async_compat(commit_video, full_audio, **commit_kwargs)
            if not is_current():
                return None

        return _SentenceCommit(
            codec=codec,
            mime_type=mime_type,
            sample_rate=sample_rate,
            channels=channels,
            pts_ms=pts_ms,
            framed_audio=framed_audio,
        )


    def _resolve_sentence_raw_pcm(self, full_audio, commit):
        """尽力把容器音频解为纯净 PCM16，返回 ``(raw_pcm, sample_rate)``。

        解码不可用时返回空 PCM 且采样率保持不变 —— 杜绝容器头或压缩编码污染数字人链路。
        """
        from server.core.media.audio_decode import decode_audio_to_float32
        import numpy as np

        samples, decoded_sr = decode_audio_to_float32(
            full_audio, fallback_sample_rate=commit.sample_rate, codec=commit.codec,
        )
        if samples is None or len(samples) == 0:
            return b"", commit.sample_rate
        pcm = (np.clip(samples, -1.0, 1.0) * 32767.0).astype(np.int16).tobytes()
        return pcm, (decoded_sr or commit.sample_rate)


    async def _push_sentence_to_side_channels(self, speak_text, full_audio, commit) -> int:
        """联动数字人驱动音频推送与电商带货场景动作切片，返回最终采样率。

        容器音频经兜底解码后采样率可能变化，调用方必须用返回值做浏览器下行。
        """
        sample_rate = commit.sample_rate
        try:
            from server.core.avatar import get_active_avatar_driver, get_action_state_machine
            avatar_driver = get_active_avatar_driver()
            action_sm = get_action_state_machine()
            if avatar_driver and avatar_driver.is_active:
                action_code = action_sm.evaluate_text(speak_text)
                if action_code is None:
                    # 规则兜底研判
                    if any(kw in speak_text for kw in ("购物车", "下单", "左下角", "抢购", "手慢无", "买一送")):
                        action_code = 3  # 促单指引购物车
                    elif any(kw in speak_text for kw in ("欢迎", "来了", "刚进", "哈喽", "晚上好")):
                        action_code = 1  # 挥手欢迎
                    elif any(kw in speak_text for kw in ("点赞", "关注", "粉丝团", "灯牌")):
                        action_code = 2  # 求关注点赞
                    elif any(kw in speak_text for kw in ("感谢", "礼物", "破费", "大气")):
                        action_code = 4  # 致谢大礼
                    if action_code:
                        await avatar_driver.set_custom_state(action_code, source="speech_text")
                else:
                    await avatar_driver.set_custom_state(action_code, source="speech_text")

                raw_pcm = b""
                if commit.codec in ("pcm_s16le", "pcm16", "s16le"):
                    raw_pcm = full_audio
                elif commit.framed_audio and getattr(commit.framed_audio, "frames", None):
                    raw_pcm = b"".join(getattr(f, "pcm_bytes", b"") for f in commit.framed_audio.frames)
                elif full_audio:
                    # 容错保障：从容器音频 (如 WAV/MP3) 尝试解码为纯净标准 PCM16
                    try:
                        raw_pcm, sample_rate = self._resolve_sentence_raw_pcm(full_audio, commit)
                    except Exception:
                        pass

                if raw_pcm:
                    await avatar_driver.push_audio_chunk(raw_pcm, {"sample_rate": sample_rate, "text": speak_text})

                pcm_for_stream = raw_pcm if raw_pcm else (full_audio if commit.codec in ("pcm_s16le", "pcm16", "s16le") else b"")
                if pcm_for_stream:
                    self._feed_audio_side_channels(pcm_for_stream, sample_rate)
        except Exception:
            logger.debug("同步数字人驱动音频与动作异常", exc_info=True)
        return sample_rate


    def _feed_audio_side_channels(self, pcm: bytes, sample_rate: int) -> None:
        """把 PCM 同步分发给 WebRTC 观看端与切片录制（均失败可忽略）。"""
        try:
            from server.core.media.webrtc_streamer import get_webrtc_stream_manager
            get_webrtc_stream_manager().push_audio(pcm, sample_rate=sample_rate)
        except Exception:
            pass
        try:
            from server.core.media.recorder import get_record_manager
            rec_mgr = get_record_manager()
            if rec_mgr and getattr(rec_mgr, "is_recording", lambda: False)():
                rec_mgr.feed_audio(pcm, sample_rate=sample_rate)
        except Exception:
            pass


    async def _broadcast_sentence_audio(
        self,
        speak_text,
        active_role,
        full_audio,
        commit,
        audio_id,
        audio_generation,
        session_generation,
        sample_rate,
    ) -> None:
        """向控制台浏览器下发可独立播放的音频块（PCM 自动封装为 WAV）。"""
        from server.routes.live import _pcm_s16le_to_wav
        from server.routes.ws_live import ws_manager

        browser_audio = full_audio
        browser_codec = commit.codec
        browser_mime_type = commit.mime_type
        if str(commit.codec).lower() in {"pcm_s16le", "pcm16", "s16le"}:
            browser_audio = _pcm_s16le_to_wav(full_audio, sample_rate, commit.channels)
            browser_codec = "wav"
            browser_mime_type = "audio/wav"

        await ws_manager.broadcast("AUDIO_CHUNK", {
            "audio_base64": base64.b64encode(browser_audio).decode("utf-8"),
            "text": speak_text,
            "speaker": active_role.role_name,
            "codec": browser_codec,
            "source_codec": commit.codec,
            "mime_type": browser_mime_type,
            "sample_rate": sample_rate,
            "channels": commit.channels,
            "audio_id": audio_id,
            "audio_generation": audio_generation,
            "session_generation": session_generation,
            # 音画同步补偿：将渲染耗时采样换算的音频前置延迟补偿量真实下发给前端播放器，
            # 使唇形画面与声波对齐（闭环 av_sync.AVSyncController 的规划 §15.1 补偿回路）
            "delay_ms": int(max(0, min(300, global_av_sync.recommended_delay_ms))),
            "pts_ms": commit.pts_ms,
            # 采样时钟换算的时间戳：与云端 sidecar 的 pts_samples 同语义，供跨端对齐
            "pts_samples": int((commit.pts_ms / 1000.0) * sample_rate),
            "wallclock_ms": int(time.time() * 1000),
        })