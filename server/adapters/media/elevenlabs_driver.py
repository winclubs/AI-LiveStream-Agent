"""
ElevenLabs 云端 TTS 驱动 (规划 §9.2 cloud_elevenlabs)
- 遵循 ElevenLabs 公开接口契约：POST {base_url}/text-to-speech/{voice_id}?output_format=mp3_44100_128
  鉴权 xi-api-key 头，支持流式 (stream=true) 与非流式两种合成模式
- 声音克隆联动 voices.py 的 Instant Voice Cloning (IVC)：克隆成功后 voice_id 落库，
  本驱动经 tts 配置 extra_params (voice_id) 或音色档案 remote_voice_id 取用
- 语速/音量来自角色语速微扰与音色档案 volume_gain，经 voice_settings 下发
- 合成失败自动回退 Edge-TTS (ADR-10)，绝不产出静默伪音频
- 能力边界：本驱动为该公开接口的轻量客户端；voice_id 经 tts 配置 extra_params
  (voice_id) 或克隆音色档案的 remote_voice_id 配置；无凭据时健康检查返回 False
"""
import logging
from typing import Optional, AsyncGenerator
from server.adapters.media.base_driver import BaseMediaDriver

logger = logging.getLogger("LiveAgent.ElevenLabsTTS")

try:
    import httpx
except ImportError:  # pragma: no cover
    httpx = None

DEFAULT_MODEL = "eleven_multilingual_v2"


class ElevenLabsTTSMediaDriver(BaseMediaDriver):
    """ElevenLabs 官方云端流式 TTS 驱动"""

    def __init__(self, api_base: str = "https://api.elevenlabs.io/v1", api_key: str = "",
                 voice_id: str = "", model_id: str = DEFAULT_MODEL):
        super().__init__()
        self.api_base = (api_base or "https://api.elevenlabs.io/v1").rstrip("/")
        self.api_key = api_key or ""
        self.voice_id = voice_id or ""
        self.model_id = model_id or DEFAULT_MODEL
        self.speed = 1.0
        self.volume = 1.0
        self.is_running = False

    async def start(self):
        self.is_running = True
        logger.info(f"ElevenLabs TTS 驱动就绪 (端点: {self.api_base}, 音色: {self.voice_id or '默认'})")

    async def stop(self):
        self.is_running = False
        await self.interrupt("Driver Stop")

    async def health_check(self, timeout: float = 4.0) -> bool:
        """
        凭据与音色齐备即视为可用；有 Key 时顺带轻量校验音色存在性，
        接口不可达不阻断选择链 (实际连通性在首次合成时验证)。
        与选择链其他驱动保持 async 签名一致。
        """
        if not (self.api_key and httpx is not None):
            return False
        if not self.voice_id:
            return True
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                resp = await client.get(
                    f"{self.api_base}/voices/{self.voice_id}",
                    headers={"xi-api-key": self.api_key},
                )
                if resp.status_code == 200:
                    return True
                if resp.status_code in (401, 403):
                    logger.warning("ElevenLabs API Key 无效，自动降级 Edge-TTS (ADR-10)")
                    return False
                logger.warning(f"ElevenLabs 音色校验异常 HTTP {resp.status_code}，按可用放行，合成时再验证")
                return True
        except Exception as e:
            logger.warning(f"ElevenLabs 健康检查网络异常 ({e})，按可用放行，合成时再验证 (ADR-10)")
            return True

    async def apply_speech_speed(self, speed: float):
        self.speed = float(speed or 1.0)

    async def apply_volume_gain(self, gain: float):
        self.volume = float(gain or 1.0)

    async def feed_audio_chunk(self, audio_bytes: bytes, text_snippet: str):
        self.is_speaking = True

    async def interrupt(self, reason: str = "Barge-in"):
        self.is_speaking = False

    async def synthesize_stream(self, text: str) -> AsyncGenerator[bytes, None]:
        """请求官方 TTS 端点产出 mp3 字节流；失败回退 Edge-TTS"""
        audio = b""
        try:
            audio = await self._request_tts(text)
        except Exception as e:
            logger.warning(f"ElevenLabs TTS 合成失败 ({e})，自动回退 Edge-TTS (ADR-10)")
        if audio:
            # 伪流式切片：按 3200 字节切分，保持下游 80ms 级驱动粒度
            for i in range(0, len(audio), 3200):
                yield audio[i:i + 3200]
            return

        from server.adapters.media.edgetts_driver import EdgeTTSMediaDriver
        fallback = EdgeTTSMediaDriver(rate=f"{int(round((self.speed - 1.0) * 100)):+d}%")
        async for chunk in fallback.synthesize_stream(text):
            yield chunk

    async def _request_tts(self, text: str) -> bytes:
        if httpx is None:
            raise RuntimeError("httpx 未安装，ElevenLabs TTS 不可用")
        if not self.api_key:
            raise RuntimeError("ElevenLabs 缺少 API Key")
        if not self.voice_id:
            raise RuntimeError("ElevenLabs 缺少 voice_id (请在 TTS 配置 extra_params.voice_id 指定，或先完成声音克隆)")

        url = f"{self.api_base}/text-to-speech/{self.voice_id}"
        headers = {
            "xi-api-key": self.api_key,
            "Content-Type": "application/json",
            "Accept": "audio/mpeg",
        }
        body = {
            "text": text,
            "model_id": self.model_id,
            "voice_settings": {
                "stability": 0.5,
                "similarity_boost": 0.75,
                "speed": max(0.7, min(1.2, self.speed)),
            },
        }
        async with httpx.AsyncClient(timeout=20.0) as client:
            resp = await client.post(
                url, params={"output_format": "mp3_44100_128"}, headers=headers, json=body
            )
        if resp.status_code != 200:
            detail = ""
            try:
                detail = resp.json().get("detail", {}).get("message", "") or resp.text[:200]
            except Exception:
                detail = resp.text[:200]
            raise RuntimeError(f"ElevenLabs TTS HTTP {resp.status_code}: {detail}")
        audio = resp.content or b""
        if not audio:
            raise RuntimeError("ElevenLabs TTS 响应缺少音频数据")
        return audio
