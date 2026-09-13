"""
声学特征提取 (声纹代理向量) — 规划 §4.1 零样本声音克隆工程化流水线的本地可落地实现
- 对上传音频做分帧 FFT + Mel 滤波器组能量统计，产出固定维度 L2 归一化特征向量
- 支持 soundfile 优先、wave 兜底；两者均不可用时退化为字节级频谱直方图
- 说明：这是确定性的声学特征向量（可用于相似度/校验/驱动参数），
  并非 CosyVoice 神经 Speaker Embedding；接入真实引擎时由引擎覆盖 embedding。
"""
import io
import wave
import logging
from pathlib import Path
from typing import Optional, Tuple

logger = logging.getLogger("LiveAgent.VoiceFeatures")

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

FEATURE_DIM = 128
N_MELS = 64
FRAME = 1024
HOP = 512


def _load_audio(path: str) -> Tuple[Optional["np.ndarray"], int]:
    """加载音频为单声道 float32，返回 (samples, sample_rate)"""
    if not NP_AVAILABLE:
        return None, 0
    # 1. soundfile (支持 wav/flac/ogg，部分 mp3)
    if SF_AVAILABLE:
        try:
            data, sr = sf.read(path, dtype="float32", always_2d=False)
            if data.ndim > 1:
                data = data.mean(axis=1)
            return data, int(sr)
        except Exception:
            pass
    # 2. wave (标准 PCM WAV)
    try:
        with wave.open(path, "rb") as wf:
            sr = wf.getframerate()
            n_channels = wf.getnchannels()
            sampwidth = wf.getsampwidth()
            raw = wf.readframes(wf.getnframes())
        if sampwidth == 2:
            arr = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
        elif sampwidth == 1:
            arr = (np.frombuffer(raw, dtype=np.uint8).astype(np.float32) - 128.0) / 128.0
        elif sampwidth == 4:
            arr = np.frombuffer(raw, dtype=np.int32).astype(np.float32) / 2147483648.0
        else:
            arr = None
        if arr is not None:
            if n_channels > 1:
                arr = arr.reshape(-1, n_channels).mean(axis=1)
            return arr, int(sr)
    except Exception:
        pass
    return None, 0


def _mel_filterbank(sr: int, n_fft: int, n_mels: int) -> "np.ndarray":
    """构造简化的三角 Mel 滤波器组 [n_mels, n_fft//2+1]"""
    def hz_to_mel(f):
        return 2595.0 * np.log10(1.0 + f / 700.0)

    def mel_to_hz(m):
        return 700.0 * (10.0 ** (m / 2595.0) - 1.0)

    low, high = 80.0, sr / 2.0
    mel_points = np.linspace(hz_to_mel(low), hz_to_mel(high), n_mels + 2)
    hz_points = mel_to_hz(mel_points)
    bins = np.floor((n_fft + 1) * hz_points / sr).astype(int)
    fb = np.zeros((n_mels, n_fft // 2 + 1), dtype=np.float32)
    for m in range(1, n_mels + 1):
        f_left, f_center, f_right = bins[m - 1], bins[m], bins[m + 1]
        if f_center == f_left:
            f_center = f_left + 1
        if f_right == f_center:
            f_right = f_center + 1
        for k in range(f_left, f_center):
            fb[m - 1, k] = (k - f_left) / max(1, f_center - f_left)
        for k in range(f_center, f_right):
            fb[m - 1, k] = (f_right - k) / max(1, f_right - f_center)
    return fb


def extract_voice_features(path: str) -> dict:
    """提取音频声学特征向量，返回 {vector, dim, duration_sec, sample_rate, method}"""
    if not NP_AVAILABLE:
        return {"vector": [], "dim": 0, "method": "unavailable"}

    samples, sr = _load_audio(path)
    if samples is None or len(samples) < FRAME:
        # 兜底：字节级频谱直方图 (保证流程闭环)
        try:
            raw = Path(path).read_bytes()
        except Exception:
            raw = b""
        if not raw:
            return {"vector": [], "dim": 0, "method": "empty"}
        arr = np.frombuffer(raw, dtype=np.uint8).astype(np.float32)
        hist, _ = np.histogram(arr, bins=FEATURE_DIM, range=(0, 255))
        vec = hist.astype(np.float32)
        norm = np.linalg.norm(vec)
        if norm > 1e-6:
            vec = vec / norm
        return {"vector": vec.tolist(), "dim": FEATURE_DIM, "duration_sec": 0.0, "sample_rate": 0, "method": "byte_spectrum"}

    window = np.hanning(FRAME).astype(np.float32)
    fb = _mel_filterbank(sr, FRAME, N_MELS)
    frames = []
    for start in range(0, len(samples) - FRAME, HOP):
        chunk = samples[start:start + FRAME] * window
        spec = np.abs(np.fft.rfft(chunk, n=FRAME))
        mel_energy = np.log(fb @ spec[: FRAME // 2 + 1] + 1e-6)
        frames.append(mel_energy)
    if not frames:
        frames = [np.zeros(N_MELS, dtype=np.float32)]
    mat = np.array(frames, dtype=np.float32)          # [num_frames, N_MELS]
    mean_vec = mat.mean(axis=0)
    std_vec = mat.std(axis=0)
    vec = np.concatenate([mean_vec, std_vec])          # 128 维
    if vec.shape[0] < FEATURE_DIM:
        vec = np.pad(vec, (0, FEATURE_DIM - vec.shape[0]))
    vec = vec[:FEATURE_DIM].astype(np.float32)
    norm = np.linalg.norm(vec)
    if norm > 1e-6:
        vec = vec / norm

    return {
        "vector": vec.tolist(),
        "dim": FEATURE_DIM,
        "duration_sec": round(len(samples) / float(sr), 3) if sr else 0.0,
        "sample_rate": sr,
        "method": "mel_fft" if SF_AVAILABLE else "wave_fft",
    }


def save_embedding(vector, path: str) -> bool:
    """将特征向量持久化为 .npy"""
    if not NP_AVAILABLE or not vector:
        return False
    try:
        np.save(path, np.asarray(vector, dtype=np.float32))
        return True
    except Exception as e:
        logger.warning(f"声学特征向量保存失败: {e}")
        return False
