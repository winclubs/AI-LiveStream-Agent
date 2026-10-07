# -*- coding: utf-8 -*-
"""
真实神经唇形重绘驱动引擎 (NeuralLipRenderer)
基于 ONNX Runtime (CUDA/CPU) 与 LatentSync 工业标准契约：
1. 音频特征：80 维 Mel 频谱窗口 (16 步长，前后 200ms 上下文)；
2. 人脸特征：256x256 对齐切片拼接 Masked 人脸 ([1, 6, 256, 256])；
3. 真实推理：ONNX 前向推理重绘唇周与下半脸细节；
4. 动态回贴：依据当前帧 coords (ymin, ymax, xmin, xmax) 高斯羽化无缝回贴原帧；
5. 架构诚实：恪守 ADR-16 规约，无模型或异常时严格上报未就绪，拒绝伪造假唇形。
"""
import logging
import os
import pickle
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, List, Literal, Optional, Tuple, Union, overload

import cv2
import numpy as np

from server.core.avatar.action_state_machine import mirror_index
from server.core.avatar.neural_model_manager import global_neural_model_manager

logger = logging.getLogger("LiveAgent.NeuralLipRenderer")

# 尝试导入 onnxruntime
if TYPE_CHECKING:
    import onnxruntime as ort
    ORT_AVAILABLE: bool = True
else:
    try:
        import onnxruntime as ort
        ORT_AVAILABLE = True
    except ImportError:
        ort = None
        ORT_AVAILABLE = False


class MelFeatureExtractor:
    """旧版音频 Mel 频谱提取器 (保留用于回退与 A/B 对比)"""

    def __init__(self, sample_rate: int = 16000, n_fft: int = 800, hop_length: int = 200, n_mels: int = 80):
        self.sample_rate = sample_rate
        self.n_fft = n_fft
        self.hop_length = hop_length
        self.n_mels = n_mels
        self._mel_basis = self._build_mel_basis()

    def _build_mel_basis(self) -> np.ndarray:
        weights = np.zeros((self.n_mels, int(1 + self.n_fft // 2)), dtype=np.float32)
        fftfreqs = np.linspace(0, self.sample_rate / 2.0, int(1 + self.n_fft // 2))
        mel_min = 0.0
        mel_max = 2595.0 * np.log10(1.0 + (self.sample_rate / 2.0) / 700.0)
        mels = np.linspace(mel_min, mel_max, self.n_mels + 2)
        freqs = 700.0 * (10.0 ** (mels / 2595.0) - 1.0)

        for i in range(self.n_mels):
            f_prev = freqs[i]
            f_curr = freqs[i + 1]
            f_next = freqs[i + 2]
            for j, f in enumerate(fftfreqs):
                if f_prev <= f <= f_curr and (f_curr - f_prev) > 0:
                    weights[i, j] = (f - f_prev) / (f_curr - f_prev)
                elif f_curr < f <= f_next and (f_next - f_curr) > 0:
                    weights[i, j] = (f_next - f) / (f_next - f_curr)
        return weights

    def extract_mel_window(self, pcm_samples: np.ndarray, target_steps: int = 16) -> np.ndarray:
        if len(pcm_samples) < self.n_fft:
            pcm_samples = np.pad(pcm_samples, (0, self.n_fft - len(pcm_samples)), mode="constant")

        window = np.hanning(self.n_fft)
        num_frames = max(1, (len(pcm_samples) - self.n_fft) // self.hop_length + 1)
        stft_matrix = []
        for i in range(num_frames):
            start = i * self.hop_length
            chunk = pcm_samples[start : start + self.n_fft]
            if len(chunk) < self.n_fft:
                chunk = np.pad(chunk, (0, self.n_fft - len(chunk)), mode="constant")
            fft_mag = np.abs(np.fft.rfft(chunk * window))
            stft_matrix.append(fft_mag)
        stft_matrix = np.array(stft_matrix).T  # shape: (n_fft//2 + 1, num_frames)

        mel_spec = np.dot(self._mel_basis, stft_matrix)
        mel_spec = np.log(np.maximum(1e-5, mel_spec))

        # 对齐到目标步长 (默认 16 步，对应约 200ms 上下文)
        current_steps = mel_spec.shape[1]
        if current_steps < target_steps:
            pad_left = (target_steps - current_steps) // 2
            pad_right = target_steps - current_steps - pad_left
            mel_spec = np.pad(mel_spec, ((0, 0), (pad_left, pad_right)), mode="edge")
        elif current_steps > target_steps:
            start_idx = (current_steps - target_steps) // 2
            mel_spec = mel_spec[:, start_idx : start_idx + target_steps]

        return mel_spec[np.newaxis, np.newaxis, :, :].astype(np.float32)


class LatentSyncMelExtractor:
    """严格遵循工业标准声学梅尔频谱提取契约 (纯 numpy, 80维 Slaney Mel 刻度, 0.97 波形预加重)

    官方标准链 (preemphasize=True / preemphasis=0.97 / signal_normalization=True / symmetric_mels=True):
        preemphasis(wav)                      # signal.lfilter([1,-k],[1],wav) —— 作用于**波形**
          -> stft(n_fft=800, hop=200, win=800, hann)
          -> |D| -> slaney mel(80, 55~7600Hz, htk=False)
          -> 20*log10(max(1e-5, .)) - ref_level_db
          -> _normalize: clip(2*max_abs*((S-min_level_db)/(-min_level_db)) - max_abs, ±max_abs)

    注意:
      1) 预加重作用在**波形**上, 且默认开启; mel 域做 m[:-1]-k*m[1:] 等价于全通滤波器
         1+k*z^-1, 只旋转相位不做高频提升, 不能替代官方波形预加重。
      2) _normalize 是**纯仿射映射**, 官方链中不存在任何 mean/std 归一化。
         逐窗 (mel-mu)/sigma 会抹平静音->响亮的动态范围 (实测 4.70 -> 0.00),
         并把静音映射到全 0 而非官方训练时的全 -4, 导致能量-开口度关联被移除。
    """

    MIN_LEVEL_DB = -100.0
    REF_LEVEL_DB = 20.0
    MAX_ABS_VALUE = 4.0
    PREEMPHASIS = 0.97
    PREEMPHASIZE = True

    def __init__(
        self,
        sample_rate: int = 16000,
        n_fft: int = 800,
        hop_length: int = 200,
        n_mels: int = 80,
        fmin: float = 55.0,
        fmax: float = 7600.0,
    ):
        self.sample_rate = sample_rate
        self.n_fft = n_fft
        self.hop_length = hop_length
        self.n_mels = n_mels
        self.fmin = fmin
        self.fmax = fmax
        self._mel_basis = self._build_mel_basis_slaney()
        self._window = np.hanning(self.n_fft)

    @staticmethod
    def _hz_to_mel(f):
        """Slaney 换算 (librosa 默认, htk=False)
        <1000Hz 线性, 斜率 200/3; >=1000Hz 对数, logstep=ln(6.4)/27
        """
        f = np.asarray(f, dtype=np.float64)
        mels = f / (200.0 / 3.0)
        min_log_hz, min_log_mel, logstep = 1000.0, 15.0, np.log(6.4) / 27.0
        log_t = f >= min_log_hz
        return np.where(
            log_t,
            min_log_mel + np.log(np.maximum(f, 1e-10) / min_log_hz) / logstep,
            mels,
        )

    @staticmethod
    def _mel_to_hz(mels):
        mels = np.asarray(mels, dtype=np.float64)
        freqs = mels * (200.0 / 3.0)
        min_log_hz, min_log_mel, logstep = 1000.0, 15.0, np.log(6.4) / 27.0
        log_t = mels >= min_log_mel
        return np.where(
            log_t,
            min_log_hz * np.exp(logstep * (mels - min_log_mel)),
            freqs,
        )

    def _build_mel_basis_slaney(self) -> np.ndarray:
        n_bins = 1 + self.n_fft // 2
        fftfreqs = np.fft.rfftfreq(self.n_fft, 1.0 / self.sample_rate)
        mel_f = self._mel_to_hz(
            np.linspace(self._hz_to_mel(self.fmin), self._hz_to_mel(self.fmax), self.n_mels + 2)
        )
        fdiff = np.diff(mel_f)
        ramps = np.subtract.outer(mel_f, fftfreqs)  # (n_mels+2, n_bins)
        weights = np.zeros((self.n_mels, n_bins), dtype=np.float32)
        for i in range(self.n_mels):
            lower = -ramps[i] / fdiff[i]
            upper = ramps[i + 2] / fdiff[i + 1]
            weights[i] = np.maximum(0.0, np.minimum(lower, upper))
        # Slaney 面积归一化 —— 消除增益偏差 142x
        weights *= (2.0 / (mel_f[2 : self.n_mels + 2] - mel_f[: self.n_mels]))[:, np.newaxis]
        return weights.astype(np.float32)

    def pre_emphasis(self, pcm: np.ndarray) -> np.ndarray:
        """官方 signal.lfilter([1, -k], [1], wav) 的等价实现 —— 作用于波形。

        y[0] = x[0]; y[n] = x[n] - k*x[n-1]
        作用是抬升高频 (辅音 b/p/m/f/d/t 的爆破与摩擦特征), 直接决定咬字清晰度。
        """
        if len(pcm) <= 1:
            return pcm
        return np.append(pcm[0], pcm[1:] - self.PREEMPHASIS * pcm[:-1]).astype(pcm.dtype)

    def extract_mel_window(self, pcm_samples: np.ndarray, target_steps: int = 16) -> np.ndarray:
        """输出 [1, 1, 80, 16], 与官方神经声学特征训练分布一致"""
        pcm = np.asarray(pcm_samples, dtype=np.float32)
        if len(pcm) < self.n_fft:
            pcm = np.pad(pcm, (0, self.n_fft - len(pcm)), mode="constant")

        # 1) 波形预加重 (官方 melspectrogram 第一步, 必须在 STFT 之前)
        if self.PREEMPHASIZE:
            pcm = self.pre_emphasis(pcm)

        # 2) 分帧加窗 -> STFT 幅度
        window = self._window
        num_frames = max(1, (len(pcm) - self.n_fft) // self.hop_length + 1)
        idx = np.arange(self.n_fft)[None, :] + self.hop_length * np.arange(num_frames)[:, None]
        frames = pcm[idx] * window[None, :]
        spec = np.abs(np.fft.rfft(frames, axis=1)).T  # (n_bins, num_frames)

        # 3) Slaney mel 投影
        mel = np.dot(self._mel_basis, spec)

        # 4) 官方 _amp_to_db: 20*log10(max(min_level, x)) - ref_level_db
        #    注意是 20*log10(幅度) 而非自然对数; min_level = 10^(min_level_db/20) = 1e-5
        min_level = float(10.0 ** (self.MIN_LEVEL_DB / 20.0))
        mel = 20.0 * np.log10(np.maximum(min_level, mel)) - self.REF_LEVEL_DB

        # 5) 官方 _normalize (symmetric_mels + allow_clipping_in_normalization):
        #    纯仿射映射 + 削波, 不含任何 mean/std 统计量。
        #    静音输入自然落到 -max_abs_value (= -4), 即模型训练时见过的静音底。
        mel = 2.0 * self.MAX_ABS_VALUE * ((mel - self.MIN_LEVEL_DB) / (-self.MIN_LEVEL_DB)) - self.MAX_ABS_VALUE
        mel = np.clip(mel, -self.MAX_ABS_VALUE, self.MAX_ABS_VALUE)

        # 6) 对齐到 target_steps
        current_steps = mel.shape[1]
        if current_steps < target_steps:
            pad_left = (target_steps - current_steps) // 2
            pad_right = target_steps - current_steps - pad_left
            mel = np.pad(mel, ((0, 0), (pad_left, pad_right)), mode="edge")
        elif current_steps > target_steps:
            start_idx = (current_steps - target_steps) // 2
            mel = mel[:, start_idx : start_idx + target_steps]

        return mel[np.newaxis, np.newaxis, :, :].astype(np.float32)

    def extract_full_mel(self, pcm_samples: np.ndarray) -> np.ndarray:
        """提取整段音频的完整连续全局梅尔频谱 (shape: [80, num_frames])，供滑动窗口切片"""
        pcm = np.asarray(pcm_samples, dtype=np.float32)
        if len(pcm) < self.n_fft:
            pcm = np.pad(pcm, (0, self.n_fft - len(pcm)), mode="constant")
        if self.PREEMPHASIZE:
            pcm = self.pre_emphasis(pcm)
        num_frames = max(1, (len(pcm) - self.n_fft) // self.hop_length + 1)
        idx = np.arange(self.n_fft)[None, :] + self.hop_length * np.arange(num_frames)[:, None]
        frames = pcm[idx] * self._window[None, :]
        spec = np.abs(np.fft.rfft(frames, axis=1)).T
        mel = np.dot(self._mel_basis, spec)
        min_level = float(10.0 ** (self.MIN_LEVEL_DB / 20.0))
        mel = 20.0 * np.log10(np.maximum(min_level, mel)) - self.REF_LEVEL_DB
        mel = 2.0 * self.MAX_ABS_VALUE * ((mel - self.MIN_LEVEL_DB) / (-self.MIN_LEVEL_DB)) - self.MAX_ABS_VALUE
        return np.clip(mel, -self.MAX_ABS_VALUE, self.MAX_ABS_VALUE).astype(np.float32)


# 别名兼容旧版引用与单元测试
Wav2LipMelExtractor = LatentSyncMelExtractor


class NeuralLipRenderer:
    """真实神经唇形重绘驱动引擎

    时序模式 (LIPSYNC_OPTIMIZATION_PLAN.md v3.2 §7)
    --------------------------------------------
    官方推理本身带 `seq_len=5` 的时序上下文 (`inference.py` 把
    `frames[-4:]` 与当前帧一起送入生成器)。逐帧独立推理会丢失这一信息，
    是唇形帧间突变的直接来源。

    本引擎支持两种形态，通过模型自身的输入维度自动识别：

    * **单帧 (T=1)**：`face [1,6,256,256]`，历史行为，所有模型均适用。
    * **时序 (T=5)**：`face [1,6,256,256,T]`，仅时序导出版适用，需 GPU。

    不做「按模型名猜测」——直接读 ONNX 的 face 输入维度判定，避免模型换名后
    静默走错分支（这类错误不会抛异常，只会输出质量下降的画面）。
    """

    # 时序上下文帧数（T）。奇数时中心帧为 seq - T//2，与官方取帧一致。
    TEMPORAL_FRAMES = 5

    def __init__(
        self,
        model_key: str = "onnx_lipsync",
        custom_onnx_path: Optional[Path] = None,
        mel_extractor: Optional[Any] = None,
        temporal_frames: Optional[int] = None,
    ):
        self.model_key = model_key
        self.session: Optional[Any] = None
        self.is_ready: bool = False
        self.mel_extractor = mel_extractor or LatentSyncMelExtractor()

        self.input_names: List[str] = []
        self.output_names: List[str] = []
        self.face_input_shape: Optional[List[int]] = None
        self.audio_input_shape: Optional[List[int]] = None

        # 时序上下文：0 表示该模型为单帧形态
        self.temporal_frames: int = int(temporal_frames or 0)
        # 上一帧人脸张量缓存（时序模式下 T=1 时退化为空，靠复制首帧补齐）
        self._prev_face_tensor: Optional[np.ndarray] = None
        self._prev_frame_idx: Optional[int] = None

        # 主播资产缓存
        self.current_anchor_dir: Optional[Path] = None
        self.coords: List[Tuple[int, int, int, int]] = []
        self.face_imgs: List[np.ndarray] = []
        self.full_imgs: List[np.ndarray] = []
        self.has_anchor_assets: bool = False

        self._init_session(custom_onnx_path)

    @property
    def supports_temporal(self) -> bool:
        """该模型是否接受时序输入（由 ONNX 实际维度决定，不靠模型名猜）

        用 getattr 兜底：本类在测试中会被 `__new__` 绕过 `__init__` 构造，
        此时 temporal_frames 尚未赋值。若直接访问会抛 AttributeError 并被
        渲染主循环的兜底 except 吞掉，表现为「静默返回 None」——极难排查。
        """
        return int(getattr(self, "temporal_frames", 0) or 0) > 1

    def _init_session(self, custom_onnx_path: Optional[Path] = None) -> None:
        if not ORT_AVAILABLE or ort is None:
            logger.warning("onnxruntime 未安装，神经唇形引擎不可用")
            self.is_ready = False
            return

        assert ort is not None

        model_path = custom_onnx_path or global_neural_model_manager.find_model_path(self.model_key)
        if not model_path or not model_path.exists():
            logger.warning(f"神经唇形模型权重未就绪 ({self.model_key})，引擎置为未就绪")
            self.is_ready = False
            return

        try:
            # 优先调用 GPU 加速，若显存不足则平滑回退 CPU
            available_providers = ort.get_available_providers()
            providers = []
            if "CUDAExecutionProvider" in available_providers:
                providers.append("CUDAExecutionProvider")
            providers.append("CPUExecutionProvider")

            opts = ort.SessionOptions()
            opts.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
            cpu_cores = os.cpu_count() or 4
            opts.intra_op_num_threads = max(1, cpu_cores // 2)

            self.session = ort.InferenceSession(str(model_path), sess_options=opts, providers=providers)
            inputs = self.session.get_inputs()
            self.input_names = [inp.name for inp in inputs]
            self.output_names = [out.name for out in self.session.get_outputs()]

            # 探测张量维度
            for inp in inputs:
                if "face" in inp.name.lower() or len(inp.shape) == 4 and (inp.shape[1] in (3, 6) or inp.shape[-1] == 256):
                    self.face_input_shape = list(inp.shape)
                elif "audio" in inp.name.lower() or "mel" in inp.name.lower() or len(inp.shape) == 4 and inp.shape[2] == 80:
                    self.audio_input_shape = list(inp.shape)

            # 时序判定：face 输入为 5 维 [B,6,H,W,T] 即为时序导出版。
            # 只认维度，不认模型名 —— 换名/改名不应导致静默走错分支。
            for inp in inputs:
                shape = list(inp.shape)
                is_face = (
                    "face" in inp.name.lower()
                    or "video" in inp.name.lower()
                    or (len(shape) == 5 and shape[1] in (3, 6))
                    or (len(shape) == 4 and shape[1] in (3, 6) and shape[-1] == 256)
                )
                if is_face:
                    self.face_input_shape = shape
                    if len(shape) == 5:
                        t_dim = shape[4]
                        self.temporal_frames = int(t_dim) if isinstance(t_dim, int) and t_dim > 1 \
                            else self.TEMPORAL_FRAMES
                    break
            for inp in inputs:
                shape = list(inp.shape)
                if len(shape) == 4 and shape[2] == 80:
                    self.audio_input_shape = shape
                    break

            self.is_ready = True
            logger.info(
                f"⚡ 真实神经唇形重绘引擎初始化成功! (模型: {model_path.name}, "
                f"Providers: {self.session.get_providers()}, "
                f"时序上下文: T={self.temporal_frames if self.supports_temporal else '单帧'})"
            )
            self._warmup()
        except Exception as e:
            logger.warning(f"神经唇形模型加载失败 ({e})，自动回退微动态引擎")
            self.session = None
            self.is_ready = False

    def _warmup(self) -> None:
        """前向推理预热，吸收首次冷启动开销"""
        if not self.is_ready or not self.session:
            return
        try:
            dummy_mel = np.zeros((1, 1, 80, 16), dtype=np.float32)
            if self.supports_temporal:
                dummy_face = np.zeros(
                    (1, 6, 256, 256, self.temporal_frames), dtype=np.float32)
            else:
                dummy_face = np.zeros((1, 6, 256, 256), dtype=np.float32)
            feed_dict = {}
            for name in self.input_names:
                if "audio" in name.lower() or "mel" in name.lower():
                    feed_dict[name] = dummy_mel
                else:
                    feed_dict[name] = dummy_face
            self.session.run(self.output_names, feed_dict)
            logger.debug("神经唇形引擎预热完成")
        except Exception as e:
            logger.debug(f"预热推理略过: {e}")

    def _build_temporal_face_tensor(self, face_tensor: np.ndarray,
                                    frame_idx: int) -> np.ndarray:
        """构造时序 face 输入 [1,6,256,256,T]。

        帧号不连续时（如跳转/打断）必须重置历史，否则会把两个无关时刻的人脸
        拼在一起，产生比单帧更糟的抖动。

        注意：`face_tensor` 为 _prepare_face_input 的 4-D 输出 [1,6,256,256]。
        历史缓存统一存**带时间维的 5-D** 形式，这样两条分支（首帧/续帧）
        产出的维度与轴位置完全一致 —— 否则首帧 5-D、续帧 4-D，
        模型会因输入 rank 不匹配而报错。
        """
        t = max(1, int(getattr(self, "temporal_frames", 1) or 1))
        half = t // 2

        # 当前帧补成 5-D [1,6,256,256,1]
        cur = face_tensor if face_tensor.ndim == 5 else face_tensor[:, :, :, :, None]

        prev = getattr(self, "_prev_face_tensor", None)
        prev_idx = getattr(self, "_prev_frame_idx", None)

        # 上一帧不可用或帧号不连续 -> 用当前帧填充全部历史位
        if prev is None or prev_idx is None or frame_idx != prev_idx + 1:
            self._prev_face_tensor = cur
            self._prev_frame_idx = frame_idx
            return np.repeat(cur, t, axis=4)

        # 历史窗：[prev, cur, cur, ...] 总长 t
        seq = [prev] + [cur] * half
        need_left = t - 1 - half
        for _ in range(need_left):
            seq.insert(0, prev)
        stacked = np.concatenate(seq, axis=4)
        self._prev_face_tensor = cur
        self._prev_frame_idx = frame_idx
        return stacked

    def reset_temporal_state(self) -> None:
        """清空时序历史（打断、跳转、静音恢复时必须调用）

        用 hasattr 兜底：测试中通过 `__new__` 构造的实例尚无这些属性。
        """
        if hasattr(self, "_prev_face_tensor"):
            self._prev_face_tensor = None
        if hasattr(self, "_prev_frame_idx"):
            self._prev_frame_idx = None

    def load_anchor_assets(self, anchor_dir: Union[str, Path]) -> bool:
        """加载主播已预生成的脸部切片序列 (face_imgs/) 与 coords.pkl 坐标清单"""
        self.coords = []
        self.face_imgs = []
        self.has_anchor_assets = False
        self.current_anchor_dir = None

        p = Path(anchor_dir)
        if not p.exists():
            return False
        target_dir = p if p.is_dir() else p.parent
        if not target_dir.exists():
            return False

        coords_file = target_dir / "coords.pkl"
        face_dir = target_dir / "face_imgs"

        if not coords_file.exists() or not face_dir.exists():
            return False

        try:
            with open(coords_file, "rb") as f:
                self.coords = pickle.load(f)

            # 预加载对齐脸部序列
            img_files = sorted(list(face_dir.glob("*.jpg")), key=lambda p: int(p.stem) if p.stem.isdigit() else 0)
            self.face_imgs = []
            for img_p in img_files:
                img = cv2.imread(str(img_p))
                if img is not None:
                    if img.shape[0] != 256 or img.shape[1] != 256:
                        img = cv2.resize(img, (256, 256), interpolation=cv2.INTER_AREA)
                    self.face_imgs.append(img)

            self.current_anchor_dir = target_dir
            self.has_anchor_assets = len(self.face_imgs) > 0 and len(self.coords) > 0
            logger.info(
                f"主播切片资产加载就绪: {target_dir.name} (人脸切片数: {len(self.face_imgs)}, 坐标数: {len(self.coords)})"
            )
            return self.has_anchor_assets
        except Exception as e:
            logger.warning(f"加载主播切片资产异常: {e}")
            self.coords = []
            self.face_imgs = []
            self.has_anchor_assets = False
            self.current_anchor_dir = None
            return False

    def _prepare_face_input(self, face_256: np.ndarray) -> np.ndarray:
        """
        构建标准 6 通道输入张量 [1, 6, 256, 256]
        前 3 通道为将下半脸 (y >= 128) 置零的 Masked 人脸，后 3 通道为原图。

        顺序说明: 模型权重按 [masked, face] 顺序训练。若误用 [face, masked]，
        输出下半脸会被破坏成色块 (实测下半脸平均绝对误差 ~119/255)。
        """
        face_f = face_256.astype(np.float32) / 255.0  # 归一化至 [0, 1]
        masked_face = face_f.copy()
        masked_face[128:, :, :] = 0.0  # 下半脸掩码

        # 拼接 6 通道: shape (256, 256, 6)
        concat_face = np.concatenate([masked_face, face_f], axis=2)
        # 转换为 NCHW: shape (1, 6, 256, 256)
        tensor_face = np.transpose(concat_face, (2, 0, 1))[np.newaxis, :, :, :]
        return tensor_face.astype(np.float32)

    @staticmethod
    def _align_color_lab(source_bgr: np.ndarray, target_bgr: np.ndarray, ref_ratio: float = 0.5) -> np.ndarray:
        """P2-2: 在 LAB 空间对齐生成图像与底片 ROI 的光照色彩分布

        以人脸上半部（额头与鼻梁，不含嘴唇区域）作为基准，计算真实皮肤的均值和标准差，
        使重绘的下半脸消除偏灰/偏色，消除贴皮色差跳跃。
        """
        try:
            # 防御性保护：若参考底图过暗 (全黑图) 或完全平坦零方差 (纯色图)，跳过色彩校准直接返回源图
            if float(np.mean(target_bgr)) < 8.0 or float(np.std(target_bgr)) < 1.0:
                return source_bgr

            h, w = source_bgr.shape[:2]
            ref_h = max(4, int(h * ref_ratio))
            src_lab = cv2.cvtColor(source_bgr, cv2.COLOR_BGR2LAB).astype(np.float32)
            tgt_lab = cv2.cvtColor(target_bgr, cv2.COLOR_BGR2LAB).astype(np.float32)

            src_ref = src_lab[:ref_h, :]
            tgt_ref = tgt_lab[:ref_h, :]

            src_mu = np.mean(src_ref, axis=(0, 1), keepdims=True)
            src_sigma = np.std(src_ref, axis=(0, 1), keepdims=True)
            tgt_mu = np.mean(tgt_ref, axis=(0, 1), keepdims=True)
            tgt_sigma = np.std(tgt_ref, axis=(0, 1), keepdims=True)

            src_sigma = np.maximum(src_sigma, 1e-4)
            tgt_sigma = np.maximum(tgt_sigma, 1.0)
            aligned_lab = (src_lab - src_mu) / src_sigma * tgt_sigma + tgt_mu
            aligned_lab = np.clip(aligned_lab, 0, 255).astype(np.uint8)
            return cv2.cvtColor(aligned_lab, cv2.COLOR_LAB2BGR)
        except Exception:
            return source_bgr

    @classmethod
    def _blend_back(
        cls,
        full_frame: np.ndarray,
        rendered_face_256: np.ndarray,
        coord_box: Tuple[int, int, int, int],
    ) -> np.ndarray:
        """
        利用 coords 坐标、LAB 色彩动态对齐与自适应下半脸羽化，将神经重绘的口型 ROI 完美无缝融合回原全尺寸帧
        严格保留原片额头、眼睛、脸颊与皮肤真实纹理，杜绝生硬边缘与贴皮面具感
        """
        ymin, ymax, xmin, xmax = coord_box
        fh, fw = full_frame.shape[:2]

        ymin = max(0, min(fh - 1, int(ymin)))
        ymax = max(ymin + 1, min(fh, int(ymax)))
        xmin = max(0, min(fw - 1, int(xmin)))
        xmax = max(xmin + 1, min(fw, int(xmax)))

        box_h = ymax - ymin
        box_w = xmax - xmin
        if box_h < 10 or box_w < 10:
            return full_frame

        # 缩放重绘人脸到原检测框大小
        resized_face = cv2.resize(rendered_face_256, (box_w, box_h), interpolation=cv2.INTER_LINEAR)
        target_roi = full_frame[ymin:ymax, xmin:xmax]

        # P2-2: 执行局部 LAB 光照与色彩动态直方图对齐
        resized_face = cls._align_color_lab(resized_face, target_roi)

        # P2-1: 构建自适应唇周与下半脸渐变羽化蒙版 (包裹口周与下唇活动区)
        mask = np.zeros((box_h, box_w), dtype=np.float32)
        center_x = box_w // 2
        center_y = int(box_h * 0.72)  # 下唇与嘴中基准
        radius_x = max(5, int(box_w * 0.40))
        radius_y = max(5, int(box_h * 0.28))

        # 主嘴唇椭圆
        cv2.ellipse(mask, (center_x, center_y), (radius_x, radius_y), 0, 0, 360, 1.0, -1)
        # 下巴平滑扩展：梯形微过渡包裹下颌，防止低头/下巴下拉时切边生硬
        jaw_top = int(box_h * 0.65)
        jaw_bottom = int(box_h * 0.96)
        jaw_pts = np.array([
            [int(box_w * 0.22), jaw_top],
            [int(box_w * 0.78), jaw_top],
            [int(box_w * 0.68), jaw_bottom],
            [int(box_w * 0.32), jaw_bottom],
        ], dtype=np.int32)
        cv2.fillPoly(mask, [jaw_pts], 0.85)

        # 双级高斯模糊羽化边界，形成柔和漫射边缘
        ksize_x = max(3, (box_w // 6) * 2 + 1)
        ksize_y = max(3, (box_h // 6) * 2 + 1)
        mask = cv2.GaussianBlur(mask, (ksize_x, ksize_y), 0)
        mask_3c = np.repeat(mask[:, :, np.newaxis], 3, axis=2)

        # 局部高保真 Alpha 羽化混合
        target_f = target_roi.astype(np.float32)
        resized_f = resized_face.astype(np.float32)
        blended_roi = resized_f * mask_3c + target_f * (1.0 - mask_3c)
        full_frame[ymin:ymax, xmin:xmax] = np.clip(blended_roi, 0, 255).astype(np.uint8)

        return full_frame

    def crop_face_256(
        self,
        full_frame: np.ndarray,
        coord_box: Tuple[int, int, int, int],
    ) -> Optional[np.ndarray]:
        """
        动作帧路径专用：从当前帧按坐标裁剪人脸并缩放为 256x256 送推理。
        与待机底片预切图 (face_imgs) 等价，但保证与当前帧人脸位置对齐。
        """
        try:
            if full_frame is None or full_frame.size == 0:
                return None
            ymin, ymax, xmin, xmax = coord_box
            fh, fw = full_frame.shape[:2]
            ymin = max(0, min(fh - 1, int(ymin)))
            ymax = max(ymin + 1, min(fh, int(ymax)))
            xmin = max(0, min(fw - 1, int(xmin)))
            xmax = max(xmin + 1, min(fw, int(xmax)))
            box_h = ymax - ymin
            box_w = xmax - xmin
            if box_h < 10 or box_w < 10:
                return None

            # 统一几何口径：直接从 coord_box 裁剪，与 face_imgs 预切片及 _blend_back 严格对齐
            crop = full_frame[ymin:ymax, xmin:xmax]
            if crop.size == 0:
                return None
            if crop.shape[2] == 4:
                crop = cv2.cvtColor(crop, cv2.COLOR_BGRA2BGR)
            return cv2.resize(crop, (256, 256), interpolation=cv2.INTER_AREA)
        except Exception as e:
            logger.debug(f"动作帧人脸裁剪失败 ({e})，该帧回退程序化渲染")
            return None

    @overload
    def render_lip_frame(
        self,
        full_frame: np.ndarray,
        frame_idx: int,
        pcm_window: np.ndarray,
        mouth_open: float = 0.0,
        override_coord: Optional[Tuple[int, int, int, int]] = None,
        return_face: Literal[False] = False,
    ) -> Optional[np.ndarray]: ...

    @overload
    def render_lip_frame(
        self,
        full_frame: np.ndarray,
        frame_idx: int,
        pcm_window: np.ndarray,
        mouth_open: float = 0.0,
        override_coord: Optional[Tuple[int, int, int, int]] = None,
        return_face: Literal[True] = ...,
    ) -> Optional[Tuple[np.ndarray, np.ndarray]]: ...

    @overload
    def render_lip_frame(
        self,
        full_frame: np.ndarray,
        frame_idx: int,
        pcm_window: np.ndarray,
        mouth_open: float = 0.0,
        override_coord: Optional[Tuple[int, int, int, int]] = None,
        return_face: bool = ...,
    ) -> Optional[Union[np.ndarray, Tuple[np.ndarray, np.ndarray]]]: ...

    def render_lip_frame(
        self,
        full_frame: np.ndarray,
        frame_idx: int,
        pcm_window: np.ndarray,
        mouth_open: float = 0.0,
        override_coord: Optional[Tuple[int, int, int, int]] = None,
        return_face: bool = False,
    ) -> Optional[Union[np.ndarray, Tuple[np.ndarray, np.ndarray]]]:
        """
        执行单帧真实神经唇形重绘管线
        参数:
          - full_frame: 原尺寸底模画面 (BGR 或 RGB)
          - frame_idx: 当前播放帧序号
          - pcm_window: 前后 200ms 的单声道 float32 音频切片 (约 3200 采样点)
          - mouth_open: 当前音频能量，低于静音门限时直接跳过推理
          - override_coord: 外部传入的人脸包围盒 (ymin, ymax, xmin, xmax)。
            用于动作切片等非底图帧：动作帧与待机底片素材不对齐，需显式指定
            当前帧的人脸位置，并从当前帧实时裁剪 256x256 人脸送推理，
            避免把底片素材贴到动作帧的错误位置。
          - return_face: 为 True 时额外返回神经重绘的 256x256 原始人脸，
            供试播主监视舱直接展示神经输出（避免从全图裁剪上采样失真）。
            默认 False，保持真实开播管路调用签名与行为完全不变。
        """
        if not self.is_ready or not self.session:
            return None

        # 外部坐标模式不依赖预切片素材 (动作帧无专属 face_imgs)；底图模式必须有
        if override_coord is None and not self.has_anchor_assets:
            return None

        # 静音、能量极低或音频切片为空时，无需执行深度推理，直接返回原帧保持纯正自然
        if pcm_window is None or len(pcm_window) == 0:
            return self._wrap_silent(full_frame, frame_idx, override_coord, return_face)
        if mouth_open < 0.01 and float(np.max(np.abs(pcm_window))) < 0.01:
            return self._wrap_silent(full_frame, frame_idx, override_coord, return_face)

        if override_coord is not None:
            # 动作帧路径：从当前帧实时裁剪对齐人脸，坐标用外部传入值
            face_256 = self.crop_face_256(full_frame, override_coord)
            if face_256 is None:
                return None
            coord_box = override_coord
        else:
            total_frames = len(self.face_imgs)
            if total_frames == 0 or len(self.coords) == 0:
                return None
            # 循环索引对应切片人脸与坐标。
            # 必须用乒乓镜像循环（0→1→…→N-1→N-2→…→0）而非模运算：
            # 眼睛区域 100% 来自这些底片帧（模型只重绘下半脸），模运算在末帧→首帧
            # 产生硬跳变，并把降采样后残留的闭眼帧变成周期性"连续眨眼"指纹。
            # 对齐 LatentSync loop_video() / MuseTalk prepare_material() 的官方做法。
            idx = mirror_index(frame_idx, total_frames)
            face_256 = self.face_imgs[idx]
            coord_box = self.coords[mirror_index(frame_idx, len(self.coords))]

        try:
            # 1. 提取 80 维 Mel 窗口 [1, 1, 80, 16]
            mel_tensor = self.mel_extractor.extract_mel_window(pcm_window, target_steps=16)

            # 2. 构建 6 通道输入人脸 [1, 6, 256, 256]
            face_tensor = self._prepare_face_input(face_256)

            # 2b. 时序模式：拼装 [1, 6, 256, 256, T]（单帧模型跳过此步）
            if self.supports_temporal:
                face_tensor = self._build_temporal_face_tensor(face_tensor, frame_idx)

            # 3. 动态组装输入执行真实前向推理
            feed_dict = {}
            for name in self.input_names:
                if "audio" in name.lower() or "mel" in name.lower():
                    feed_dict[name] = mel_tensor
                else:
                    feed_dict[name] = face_tensor

            out = self.session.run(self.output_names, feed_dict)
            rendered_face = out[0][0]  # shape: (3, 256, 256)

            # 4. 转换维度与色彩为 uint8
            if rendered_face.shape[0] == 3:
                rendered_face = np.transpose(rendered_face, (1, 2, 0))  # (256, 256, 3)

            # 归一化反变换
            if rendered_face.max() <= 1.05:
                rendered_face = np.clip(rendered_face * 255.0, 0, 255).astype(np.uint8)
            else:
                rendered_face = np.clip(rendered_face, 0, 255).astype(np.uint8)

            # 5. 动态无缝羽化融合回贴原帧
            result_frame = full_frame.copy()
            result_frame = self._blend_back(result_frame, rendered_face, coord_box)
            return (result_frame, rendered_face) if return_face else result_frame

        except Exception as e:
            logger.debug(f"神经唇形渲染单帧推理跳过 ({e})，将回退微动态引擎")
            return None

    def _wrap_silent(
        self,
        full_frame: np.ndarray,
        frame_idx: int,
        override_coord: Optional[Tuple[int, int, int, int]],
        return_face: bool,
    ) -> Optional[Union[np.ndarray, Tuple[np.ndarray, np.ndarray]]]:
        """静音帧：跳过推理，返回原帧；需要时附带未形变的对齐人脸"""
        # 静音帧不参与推理，但时序历史必须清空：否则静音结束后第一帧会把
        # 跨静音的两端人脸拼在一起，产生比单帧更明显的跳变。
        self.reset_temporal_state()
        if not return_face:
            return full_frame
        if override_coord is not None:
            face = self.crop_face_256(full_frame, override_coord)
        else:
            total_frames = len(self.face_imgs)
            if total_frames == 0:
                return full_frame
            # 与 render_lip_frame 保持同一套乒乓镜像循环（静音段也不产生首尾跳变）
            face = self.face_imgs[mirror_index(frame_idx, total_frames)]
        if face is None:
            return full_frame
        return full_frame, face
