import asyncio
import logging
from pathlib import Path
from typing import Optional, AsyncGenerator
from server.adapters.media.base_driver import BaseMediaDriver

logger = logging.getLogger("LiveAgent.MossTTSDriver")


class MossTTSMediaDriver(BaseMediaDriver):
    """
    基于复旦大学开源 MOSS-TTS-Nano (~100M 参数, ~500MB 显存) 的端侧轻量高保真语音驱动
    特点：
    1. 端侧超轻量，极速冷启动，显存占用极低 (~500MB)
    2. 支持 48kHz 广播级真人音质，支持 5~30 秒录音的极速零样本声音克隆 (Zero-Shot)
    3. 支持流式分块输出与毫秒级抢占打断
    """
    audio_codec = "wav"
    audio_mime_type = "audio/wav"
    audio_sample_rate = 48000
    audio_channels = 1

    def __init__(self, api_base: str = "http://127.0.0.1:9880", prompt_wav_path: Optional[str] = None):
        super().__init__()
        self.api_base = (api_base or "http://127.0.0.1:9880").strip().rstrip("/")
        self.prompt_wav_path = prompt_wav_path
        self.speed = 1.0
        self.volume = 1.0
        self.is_running = False
        self.current_task: Optional[asyncio.Task] = None

    async def start(self):
        self.is_running = True
        logger.info(f"MOSS-TTS-Nano 媒体驱动就绪，API Base: {self.api_base}, 参考音色样本: {self.prompt_wav_path}")

    async def health_check(self, timeout: float = 2.0) -> bool:
        """探测 MOSS-TTS-Nano 推理服务端点连通性"""
        import httpx
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                resp = await client.get(f"{self.api_base}/health")
                if resp.status_code == 200:
                    return True
                # 兼容通用根路径探测
                resp_root = await client.get(f"{self.api_base}/")
                return resp_root.status_code < 500
        except Exception:
            return False

    async def stop(self):
        self.is_running = False
        await self.interrupt("MossTTS Driver Stop")
        logger.info("MOSS-TTS-Nano 媒体驱动已停止")

    async def set_prompt_voice(self, wav_path: str):
        """动态更新当前克隆参考音色样本 (5~30s 清晰人声)"""
        self.prompt_wav_path = wav_path
        logger.info(f"已动态切换 MOSS-TTS-Nano 零样本参考样本: {wav_path}")

    async def apply_speech_speed(self, speed: float):
        """应用口播语速倍率"""
        self.speed = float(speed or 1.0)

    async def apply_volume_gain(self, gain: float):
        """应用音色音量增益倍率"""
        self.volume = float(gain or 1.0)

    async def apply_role(self, role):
        """兼容接口：从角色对象同步语速"""
        self.speed = float(getattr(role, "speech_speed", 1.0) or 1.0) if role else 1.0

    async def feed_audio_chunk(self, audio_bytes: bytes, text_snippet: str):
        self.is_speaking = True

    async def synthesize_stream(self, text: str) -> AsyncGenerator[bytes, None]:
        """
        从 MOSS-TTS-Nano 服务端流式合成返回 48kHz 音频块。
        timeout=60s：CPU 推理 RTF 0.65~0.79×，长句合成可达 15s+，15s 超时会截断直播播报。
        """
        import httpx
        clean_text = (text or "").strip()
        if not clean_text:
            return

        # 优先调用 /tts（兼容旧版 /inference）
        url = f"{self.api_base}/tts"
        payload = {
            "text": clean_text,
            "prompt_wav": self.prompt_wav_path or "",
            "speed": self.speed,
            "volume": self.volume,
            "sample_rate": self.audio_sample_rate,
            "stream": True
        }

        try:
            async with httpx.AsyncClient(timeout=60.0) as client:
                async with client.stream("POST", url, json=payload) as response:
                    if response.status_code == 404:
                        # 兼容备用端点 /inference
                        fallback_url = f"{self.api_base}/inference"
                        async with client.stream("POST", fallback_url, json=payload) as fb_resp:
                            if fb_resp.status_code != 200:
                                raise RuntimeError(f"MOSS-TTS-Nano 服务响应异常: {fb_resp.status_code}")
                            received = False
                            async for chunk in fb_resp.aiter_bytes(chunk_size=4096):
                                if chunk:
                                    received = True
                                    yield chunk
                            if not received:
                                raise RuntimeError("MOSS-TTS-Nano 返回空音频")
                        return

                    if response.status_code != 200:
                        raise RuntimeError(f"MOSS-TTS-Nano 服务响应非 200: {response.status_code}")

                    received = False
                    async for chunk in response.aiter_bytes(chunk_size=4096):
                        if chunk:
                            received = True
                            yield chunk

                    if not received:
                        raise RuntimeError("MOSS-TTS-Nano 返回空音频")

        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning(f"MOSS-TTS-Nano 合成失败: {e}")
            raise RuntimeError(f"MOSS-TTS-Nano synthesis failed: {e}") from e

    async def interrupt(self, reason: str = "Barge-in"):
        """毫秒级抢占打断"""
        logger.info(f"MossTTSMediaDriver 收到打断信号: {reason}")
        self.is_speaking = False
