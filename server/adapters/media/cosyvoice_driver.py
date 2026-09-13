import asyncio
import logging
from pathlib import Path
from typing import Optional, AsyncGenerator
from server.adapters.media.base_driver import BaseMediaDriver

logger = logging.getLogger("LiveAgent.CosyVoiceDriver")

class CosyVoiceMediaDriver(BaseMediaDriver):
    """
    基于阿里开源 CosyVoice2-0.5B / CosyVoice 架构的本地低延迟零样本声音克隆驱动
    特点：支持 10 秒语音快速克隆、自然情绪起伏、流式分块输出（80ms chunk）
    """
    audio_codec = "pcm_s16le"
    audio_mime_type = "audio/L16"
    audio_sample_rate = 24000
    audio_channels = 1
    def __init__(self, api_base: str = "http://127.0.0.1:9233", prompt_wav_path: Optional[str] = None):
        super().__init__()
        self.api_base = api_base
        self.prompt_wav_path = prompt_wav_path
        self.speed = 1.0
        self.volume = 1.0
        self.is_running = False
        self.current_task: Optional[asyncio.Task] = None

    async def start(self):
        self.is_running = True
        logger.info(f"CosyVoice 媒体驱动就绪，API Base: {self.api_base}, 参考音色: {self.prompt_wav_path}")

    async def health_check(self, timeout: float = 2.0) -> bool:
        """探测本地 CosyVoice 推理服务连通性 (ADR-10 降级链的前置健康检查)"""
        import httpx
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                resp = await client.get(f"{self.api_base}/")
                return resp.status_code < 500
        except Exception:
            return False

    async def stop(self):
        self.is_running = False
        await self.interrupt("CosyVoice Driver Stop")
        logger.info("CosyVoice 媒体驱动已停止")

    async def set_prompt_voice(self, wav_path: str):
        """动态更新当前克隆参考音色样本"""
        self.prompt_wav_path = wav_path
        logger.info(f"已动态切换 CosyVoice 克隆音色参考样本: {wav_path}")

    async def apply_speech_speed(self, speed: float):
        """应用口播语速倍率"""
        self.speed = float(speed or 1.0)

    async def apply_volume_gain(self, gain: float):
        """应用音色档案音量增益倍率 (注入推理请求)"""
        self.volume = float(gain or 1.0)

    async def apply_role(self, role):
        """兼容旧接口：从角色对象同步语速"""
        self.speed = float(getattr(role, "speech_speed", 1.0) or 1.0) if role else 1.0

    async def feed_audio_chunk(self, audio_bytes: bytes, text_snippet: str):
        self.is_speaking = True
        # 送入下游混音器/声卡设备

    async def synthesize_stream(self, text: str) -> AsyncGenerator[bytes, None]:
        """
        向本地 CosyVoice 推理服务发送流式推理请求，分块流式产生 PCM 音频
        """
        import httpx
        url = f"{self.api_base}/inference_stream"
        payload = {
            "text": text,
            "prompt_wav": self.prompt_wav_path or "",
            "speed": self.speed,
            "volume": self.volume,
            "stream": True
        }
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                async with client.stream("POST", url, json=payload) as response:
                    if response.status_code != 200:
                        raise RuntimeError(f"CosyVoice 服务响应非 200: {response.status_code}")
                    received = False
                    async for chunk in response.aiter_bytes(chunk_size=1280):
                        if chunk:
                            received = True
                            yield chunk
                    if not received:
                        raise RuntimeError("CosyVoice 返回空音频")
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning(f"CosyVoice 运行期合成失败: {e}")
            raise RuntimeError(f"CosyVoice synthesis failed: {e}") from e

    async def interrupt(self, reason: str = "Barge-in"):
        """毫秒级抢占打断"""
        logger.info(f"CosyVoiceDriver 收到打断信号: {reason}")
        self.is_speaking = False
        if self.current_task and not self.current_task.done():
            self.current_task.cancel()
            try:
                await self.current_task
            except asyncio.CancelledError:
                pass
            self.current_task = None
