# -*- coding: utf-8 -*-
"""
全双工实时 ASR 语音与麦克风极速打断系统 (Full-Duplex ASR & Instant Interrupt)
阶段四核心模块：
1. 接收前端浏览器麦克风流式传输的 16kHz PCM 音频数据；
2. 基于时域 RMS 能量与平滑时序实现低延迟 VAD (语音活动检测)；
3. 全双工极速打断：用户开嗓发言即刻检测，若当前数字人正处于播报状态，立即调用 flush_talk() 清空唇形与音频拖尾；
4. 静音切片分段与本地 ASR 转录 (SenseVoice / Faster-Whisper / 兜底)；
5. 将识别出的文本作为高优先级互动提问注入直播大脑交互队列。
"""
import asyncio
import io
import logging
import time
import wave
from typing import Any, Callable, Dict, List, Optional

import numpy as np

from server.core.audio.asr_engine import is_voice_active, transcribe_audio_bytes
from server.core.avatar import get_active_avatar_driver

logger = logging.getLogger("LiveAgent.FullDuplexASR")


class ASRSession:
    """单个麦克风流式会话状态机"""

    def __init__(
        self,
        session_id: str,
        energy_threshold: float = 300.0,
        silence_timeout_sec: float = 0.6,
        on_interrupt: Optional[Callable[[], None]] = None,
        on_transcribe: Optional[Callable[[str], None]] = None,
    ):
        self.session_id = session_id
        self.energy_threshold = energy_threshold
        self.silence_timeout_sec = silence_timeout_sec
        self.on_interrupt = on_interrupt
        self.on_transcribe = on_transcribe

        # 运行缓冲
        self.buffer = bytearray()
        self.is_speaking = False
        self.active_frame_count = 0
        self.last_speech_time = 0.0
        self.interrupted_triggered = False

    def feed_pcm(self, pcm_chunk: bytes) -> Dict[str, Any]:
        """
        送入一帧 16kHz 16-bit 单声道 PCM 音频 (建议每次 320 ~ 1600 字节，即 10ms ~ 50ms)
        返回当前帧的分析事件指标
        """
        now = time.time()
        has_voice = is_voice_active(pcm_chunk, energy_threshold=self.energy_threshold)

        events: List[str] = []
        transcribed_text: Optional[str] = None

        if has_voice:
            self.active_frame_count += 1
            self.last_speech_time = now
            self.buffer.extend(pcm_chunk)

            # 连续 2 帧以上检测到有效人声能量，确认进入说话态
            if not self.is_speaking and self.active_frame_count >= 2:
                self.is_speaking = True
                self.interrupted_triggered = False
                events.append("speech_start")
                logger.info(f"会话 {self.session_id} 检测到用户开嗓发言")

            # 若数字人当前正在说话播报，且尚未触发本次打断，立即触发极速打断！
            if self.is_speaking and not self.interrupted_triggered:
                driver = get_active_avatar_driver()
                if driver and driver.is_speaking():
                    try:
                        asyncio.create_task(driver.flush_talk())
                    except Exception:
                        pass
                    self.interrupted_triggered = True
                    events.append("interrupted")
                    logger.info(f"会话 {self.session_id} 触发全双工极速打断: 主播立即闭嘴并重置待机唇形")
                    if self.on_interrupt:
                        try:
                            self.on_interrupt()
                        except Exception:
                            pass
        else:
            self.active_frame_count = max(0, self.active_frame_count - 1)
            # 用户发言后出现连续静音，判定一句话结束
            if self.is_speaking:
                self.buffer.extend(pcm_chunk)  # 保留小段尾音防吞字
                if now - self.last_speech_time > self.silence_timeout_sec:
                    self.is_speaking = False
                    events.append("speech_end")
                    logger.info(f"会话 {self.session_id} 发言段落结束，音频缓冲体积: {len(self.buffer)} 字节")

                    # 将累计的 PCM 打包成 WAV 进行转录
                    wav_bytes = self._pack_wav(bytes(self.buffer))
                    self.buffer.clear()

                    # 触发 ASR 转录
                    if len(wav_bytes) > 1000:
                        text = transcribe_audio_bytes(wav_bytes)
                        if text:
                            transcribed_text = text
                            events.append("transcription")
                            logger.info(f"会话 {self.session_id} ASR 转写结果: 【{text}】")
                            if self.on_transcribe:
                                try:
                                    self.on_transcribe(text)
                                except Exception:
                                    pass

        return {
            "session_id": self.session_id,
            "has_voice": has_voice,
            "is_speaking": self.is_speaking,
            "events": events,
            "text": transcribed_text,
        }

    @staticmethod
    def _pack_wav(pcm_data: bytes, sample_rate: int = 16000, channels: int = 1, sampwidth: int = 2) -> bytes:
        """快速将裸 PCM 封装为带标准头部的 WAV 二进制流"""
        out_buf = io.BytesIO()
        with wave.open(out_buf, "wb") as wf:
            wf.setnchannels(channels)
            wf.setsampwidth(sampwidth)
            wf.setframerate(sample_rate)
            wf.writeframes(pcm_data)
        return out_buf.getvalue()


class FullDuplexASRManager:
    """全局全双工 ASR 语音接入管理器"""

    def __init__(self):
        self.sessions: Dict[str, ASRSession] = {}
        self.default_energy_threshold: float = 300.0
        self.default_silence_timeout: float = 0.6

    def get_or_create_session(
        self,
        session_id: str,
        on_interrupt: Optional[Callable[[], None]] = None,
        on_transcribe: Optional[Callable[[str], None]] = None,
    ) -> ASRSession:
        """获取或创建指定会话的 ASR 实例"""
        if session_id not in self.sessions:
            self.sessions[session_id] = ASRSession(
                session_id=session_id,
                energy_threshold=self.default_energy_threshold,
                silence_timeout_sec=self.default_silence_timeout,
                on_interrupt=on_interrupt,
                on_transcribe=on_transcribe,
            )
        return self.sessions[session_id]

    def remove_session(self, session_id: str):
        """移除会话并释放缓冲"""
        self.sessions.pop(session_id, None)


_global_asr_manager: Optional[FullDuplexASRManager] = None


def get_full_duplex_asr_manager() -> FullDuplexASRManager:
    """获取全局全双工 ASR 语音管理器单例"""
    global _global_asr_manager
    if _global_asr_manager is None:
        _global_asr_manager = FullDuplexASRManager()
    return _global_asr_manager
