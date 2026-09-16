from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Optional

from server.core.media.audio_frame import MediaCapabilities

if TYPE_CHECKING:
    from server.core.media.audio_frame import AudioFrame


class BaseMediaDriver(ABC):
    """数字人实时媒体驱动抽象基类。

    ``synthesize_stream`` 的原始字节默认采用 MP3/24kHz/单声道，但 chunk 仅是
    传输分片。标准 PCM 帧通过 ``feed_audio_frame`` 进入媒体平面。
    """

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

    def get_media_capabilities(self) -> dict:
        """返回新增帧接口能力；旧驱动无需修改即可继续工作。"""
        return MediaCapabilities(
            accepts_audio_frames=False,
            tts_output_codec=str(getattr(self, "audio_codec", "mp3")),
        ).to_dict()

    async def feed_audio_frame(self, frame: "AudioFrame") -> None:
        """标准帧的兼容入口；旧驱动默认退化为原字节接口。

        具体驱动应覆盖此方法以消费 PTS 和 generation。默认适配故意只传递旧接口
        已声明的两个位置参数，避免破坏已有第三方驱动和测试替身。
        """
        await self.feed_audio_chunk(frame.data, frame.text)

    @abstractmethod
    async def feed_audio_chunk(self, audio_bytes: bytes, text_snippet: str):
        """兼容入口：传入完整容器、PCM 数据或旧协议定义的字节块。"""
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
