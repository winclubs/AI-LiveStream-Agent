"""共享数字信号处理工具：s16le PCM 线性插值重采样"""

try:
    import numpy as np
    NUMPY_AVAILABLE = True
except ImportError:  # pragma: no cover
    np = None
    NUMPY_AVAILABLE = False


def resample_s16le(pcm_bytes: bytes, from_rate: int, to_rate: int) -> bytes:
    """线性插值重采样 s16le 单声道 PCM；同速率或无 numpy 运行环境时原样返回。"""
    if not pcm_bytes or from_rate <= 0 or to_rate <= 0:
        return pcm_bytes
    if from_rate == to_rate:
        return pcm_bytes
    if not NUMPY_AVAILABLE:
        return pcm_bytes
    try:
        samples = np.frombuffer(pcm_bytes, dtype=np.int16).astype(np.float32)
    except Exception:
        return pcm_bytes
    if samples.size == 0:
        return pcm_bytes
    ratio = to_rate / from_rate
    out_len = max(1, int(round(samples.size * ratio)))
    indices = np.linspace(0, samples.size - 1, out_len, dtype=np.float32)
    resampled = np.interp(indices, np.arange(samples.size, dtype=np.float32), samples)
    return resampled.astype(np.int16).tobytes()
