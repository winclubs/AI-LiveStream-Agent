"""
MiniMax 海螺 AI 云端 TTS 驱动 (规划 §9.2 cloud_minimax / v1.8.0 实装)
- 遵循 MiniMax T2A v2 公开接口契约：POST {base_url}/t2a_v2?GroupId={group_id}
  鉴权 Bearer API Key，响应 data.audio 为十六进制编码音频
- 语速/音量来自角色语速微扰与音色档案 volume_gain，经 voice_setting 下发
- 合成失败自动回退 Edge-TTS (ADR-10)，绝不产出静默伪音频
- 能力边界：本驱动为该公开接口的轻量客户端，非流式 (按句请求)；
  voice_id / group_id 经 tts 配置 extra_params 配置 (voice_id / group_id)
"""
import logging
from typing import Optional, AsyncGenerator
from server.adapters.media.base_driver import BaseMediaDriver

logger = logging.getLogger("LiveAgent.MinimaxTTS")

try:
    import httpx
except ImportError:  # pragma: no cover
    httpx = None


class MinimaxTTSMediaDriver(BaseMediaDriver):
    DEFAULT_VOICE = "female-shaonv"

    def __init__(self, api_base: str = "https://api.minimax.chat/v1", api_key: str = "",
                 group_id: str = "", voice_id: str = ""):
        super().__init__()
        self.api_base = (api_base or "https://api.minimax.chat/v1").rstrip("/")
        self.api_key = api_key or ""
        self.group_id = group_id or ""
        self.voice_id = voice_id or self.DEFAULT_VOICE
        self.speed = 1.0
        self.volume = 1.0
        self.is_running = False

    async def start(self):
        self.is_running = True
        logger.info(f"MiniMax TTS 驱动就绪 (端点: {self.api_base}, 音色: {self.voice_id})")

    async def stop(self):
        self.is_running = False
        await self.interrupt("Driver Stop")

    async def health_check(self, timeout: float = 2.0) -> bool:
        """凭据齐备即视为可用 (T2A 无轻量探测端点，实际连通性在首次合成时验证)；与选择链其他驱动保持 async 签名一致"""
        return bool(self.api_key and httpx is not None)

    async def apply_speech_speed(self, speed: float):
        self.speed = float(speed or 1.0)

    async def apply_volume_gain(self, gain: float):
        self.volume = float(gain or 1.0)

    async def feed_audio_chunk(self, audio_bytes: bytes, text_snippet: str):
        self.is_speaking = True

    async def interrupt(self, reason: str = "Barge-in"):
        self.is_speaking = False

    async def synthesize_stream(self, text: str) -> AsyncGenerator[bytes, None]:
        """按句请求 T2A v2，解码 hex 音频产出；失败回退 Edge-TTS"""
        audio = b""
        try:
            audio = await self._request_t2a(text)
        except Exception as e:
            logger.warning(f"MiniMax TTS 合成失败 ({e})，自动回退 Edge-TTS (ADR-10)")
        if audio:
            # 伪流式切片：按 3200 字节切分，保持下游 80ms 级驱动粒度
            for i in range(0, len(audio), 3200):
                yield audio[i:i + 3200]
            return

        from server.adapters.media.edgetts_driver import EdgeTTSMediaDriver
        fallback = EdgeTTSMediaDriver(rate=f"{int(round((self.speed - 1.0) * 100)):+d}%")
        async for chunk in fallback.synthesize_stream(text):
            yield chunk

    async def _request_t2a(self, text: str) -> bytes:
        if httpx is None:
            raise RuntimeError("httpx 未安装，MiniMax TTS 不可用")
        url = f"{self.api_base}/t2a_v2"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        body = {
            "model": "speech-01-turbo",
            "text": text,
            "stream": False,
            "voice_setting": {
                "voice_id": self.voice_id,
                "speed": max(0.5, min(2.0, self.speed)),
                "vol": max(0.1, min(10.0, self.volume)),
                "pitch": 0,
            },
            "audio_setting": {
                "sample_rate": 24000,
                "bitrate": 128000,
                "format": "mp3",
                "channel": 1,
            },
        }
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(url, params={"GroupId": self.group_id} if self.group_id else None,
                                     headers=headers, json=body)
        if resp.status_code != 200:
            raise RuntimeError(f"MiniMax TTS HTTP {resp.status_code}")
        data = resp.json()
        base_resp = data.get("base_resp") or {}
        if base_resp.get("status_code", 0) != 0:
            raise RuntimeError(f"MiniMax TTS 业务错误: {base_resp.get('status_msg', base_resp)}")
        audio_hex = (data.get("data") or {}).get("audio") or ""
        if not audio_hex:
            raise RuntimeError("MiniMax TTS 响应缺少音频数据")
        return bytes.fromhex(audio_hex)
