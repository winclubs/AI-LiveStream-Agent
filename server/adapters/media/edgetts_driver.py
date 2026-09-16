import asyncio
import logging
from typing import Optional, AsyncGenerator
from server.adapters.media.base_driver import BaseMediaDriver

logger = logging.getLogger("LiveAgent.EdgeTTSDriver")

class EdgeTTSMediaDriver(BaseMediaDriver):
    """
    基于 Microsoft Edge-TTS 的高保真轻量语音媒体驱动
    特点：零 GPU 算力依赖、极速流式返回、原生支持中文多音色、毫秒级任务取消
    """
    def __init__(self, voice: str = "zh-CN-XiaoxiaoNeural", rate: str = "+0%", pitch: str = "+0Hz", volume: str = "+0%"):
        super().__init__()
        self.voice = voice
        self.rate = rate
        self.pitch = pitch
        self.volume = volume
        self.current_task: Optional[asyncio.Task] = None
        self.is_running = False
        self.buffer_queue = asyncio.Queue()

    async def start(self):
        self.is_running = True
        logger.info(f"EdgeTTSMediaDriver 已初始化就绪，音色: {self.voice}")

    async def apply_speech_speed(self, speed: float):
        """应用口播语速倍率 (含防封杀随机微扰后的最终值)"""
        from server.core.guardrails.humanizer import speed_to_edge_rate
        self.rate = speed_to_edge_rate(speed)

    async def apply_volume_gain(self, gain: float):
        """应用音色档案音量增益倍率"""
        try:
            percent = int(round((float(gain or 1.0) - 1.0) * 100))
            self.volume = f"{percent:+d}%"
        except Exception:
            self.volume = "+0%"

    async def stop(self):
        self.is_running = False
        await self.interrupt("Driver Stop")
        logger.info("EdgeTTSMediaDriver 已停止")

    async def feed_audio_chunk(self, audio_bytes: bytes, text_snippet: str):
        """传入音频切片"""
        self.is_speaking = True
        await self.buffer_queue.put((audio_bytes, text_snippet))

    async def synthesize_stream(self, text: str) -> AsyncGenerator[bytes, None]:
        """
        流式合成文本为音频字节流，支持被 interrupt 瞬间取消
        """
        self._is_interrupted = False
        edge_stream = None
        try:
            import edge_tts
            communicate = edge_tts.Communicate(text, self.voice, rate=self.rate, pitch=self.pitch, volume=self.volume)
            edge_stream = communicate.stream()
            async for chunk in edge_stream:
                if getattr(self, "_is_interrupted", False):
                    logger.info("EdgeTTS 合成已被打断")
                    break
                if chunk["type"] == "audio":
                    yield chunk["data"]
        except ImportError:
            logger.warning("未检测到本地 edge-tts 库，使用模拟音频流回退")
            # 模拟生成 5 个切片并支持打断检查
            for i in range(5):
                if getattr(self, "_is_interrupted", False):
                    break
                await asyncio.sleep(0.06)
                yield b"\x00" * 3200
        except asyncio.CancelledError:
            logger.info("EdgeTTS 合成任务已被抢占打断(Cancelled)")
            raise
        finally:
            close_stream = getattr(edge_stream, "aclose", None)
            if close_stream is not None:
                try:
                    await close_stream()
                except Exception:
                    logger.debug("关闭 EdgeTTS 内层流失败", exc_info=True)

    async def interrupt(self, reason: str = "Barge-in"):
        """响应抢占打断信令：瞬间中断当前生成与播报"""
        logger.info(f"EdgeTTSMediaDriver 接收打断信令: {reason}，重置缓冲区与播报态")
        self.is_speaking = False
        self._is_interrupted = True
        curr = asyncio.current_task()
        if self.current_task and not self.current_task.done() and self.current_task is not curr:
            self.current_task.cancel()
            self.current_task = None

        # 清空等待缓冲队列
        while not self.buffer_queue.empty():
            try:
                self.buffer_queue.get_nowait()
                self.buffer_queue.task_done()
            except Exception:
                break
