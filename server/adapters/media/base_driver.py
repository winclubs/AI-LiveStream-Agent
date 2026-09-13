from abc import ABC, abstractmethod
from typing import Optional

class BaseMediaDriver(ABC):
    """数字人实时媒体驱动抽象基类。TTS 字节默认采用 MP3/24kHz/单声道契约。"""
    audio_codec = "mp3"
    audio_mime_type = "audio/mpeg"
    audio_sample_rate = 24000
    audio_channels = 1

    def __init__(self):
        self.is_speaking = False

    async def apply_speech_speed(self, speed: float):
        """应用口播语速倍率 (含防封杀随机微扰后的最终值)，默认空实现"""
        return None

    async def apply_volume_gain(self, gain: float):
        """应用音量增益倍率 (来自音色档案 volume_gain)，默认空实现"""
        return None

    @abstractmethod
    async def feed_audio_chunk(self, audio_bytes: bytes, text_snippet: str):
        """传入一段 80ms PCM 音频切片进行驱动渲染"""
        pass

    @abstractmethod
    async def interrupt(self, reason: str = "Barge-in"):
        """响应抢占打断信令，瞬间清空待播音频缓冲区并复位唇形为呼吸态"""
        pass

    @abstractmethod
    async def start(self):
        """启动媒体管道"""
        pass

    @abstractmethod
    async def stop(self):
        """停止媒体管道"""
        pass
