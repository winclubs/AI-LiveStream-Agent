"""
音频字节流统一解码器（供虚拟声卡输出与口型能量驱动共用）。

容器格式必须成功通过 soundfile/libsndfile 解码；声明为 MP3/WAV/OGG/FLAC
却无法解码时绝不把压缩字节误当作 int16 PCM。裸 PCM 仅在调用方明确声明
``codec="pcm_s16le"`` 或 ``allow_raw_pcm=True`` 时启用。
"""
import io
import logging

logger = logging.getLogger("LiveAgent.AudioDecode")

try:
    import numpy as np
    NP_AVAILABLE = True
except Exception:  # pragma: no cover
    np = None
    NP_AVAILABLE = False

try:
    import soundfile as sf
    SF_AVAILABLE = True
except Exception:  # pragma: no cover
    sf = None
    SF_AVAILABLE = False


_CONTAINER_CODECS = {"mp3", "wav", "wave", "ogg", "flac", "opus", "aac", "m4a"}
_RAW_PCM_CODECS = {"pcm_s16le", "s16le", "pcm", "raw"}


def decode_audio_to_float32(
    data: bytes,
    fallback_sample_rate: int = 24000,
    *,
    codec: str | None = None,
    channels: int = 1,
    allow_raw_pcm: bool = False,
):
    """解码为 ``(float32 单声道 samples, sample_rate)``；失败返回 ``(None, 0)``。"""
    if not data or np is None:
        return None, 0

    normalized_codec = (codec or "").lower().strip()
    declared_raw = normalized_codec in _RAW_PCM_CODECS
    declared_container = bool(normalized_codec) and not declared_raw

    if not declared_raw and SF_AVAILABLE and sf is not None:
        try:
            raw, sr = sf.read(io.BytesIO(data), dtype="float32", always_2d=False)
            if raw.ndim == 2:
                raw = raw.mean(axis=1)
            if raw.size > 0:
                return raw.astype(np.float32), int(sr)
        except Exception:
            # 已声明容器时解码失败即失败，不允许降级为裸 PCM。
            if declared_container:
                return None, 0

    if not (declared_raw or allow_raw_pcm):
        return None, 0

    try:
        channel_count = max(1, int(channels or 1))
        arr = np.frombuffer(data, dtype="<i2").astype(np.float32) / 32768.0
        if channel_count > 1:
            complete = arr.size - (arr.size % channel_count)
            if complete <= 0:
                return None, 0
            arr = arr[:complete].reshape(-1, channel_count).mean(axis=1)
        if arr.size > 0:
            return arr.astype(np.float32), int(fallback_sample_rate)
    except Exception:
        pass

    return None, 0
