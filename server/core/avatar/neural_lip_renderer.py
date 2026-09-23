# -*- coding: utf-8 -*-
"""
真实神经唇形重绘驱动引擎 (NeuralLipRenderer)
基于 ONNX Runtime (CUDA/CPU) 与 Wav2Lip 工业标准契约：
1. 音频特征：80 维 Mel 频谱窗口 (16 步长，前后 200ms 上下文)；
2. 人脸特征：256x256 对齐切片拼接 Masked 人脸 ([1, 6, 256, 256])；
3. 真实推理：ONNX 前向推理重绘唇周与下半脸细节；
4. 动态回贴：依据当前帧 coords (ymin, ymax, xmin, xmax) 高斯羽化无缝回贴原帧；
5. 降级保护：恪守 ADR-16 架构诚实，无模型或异常时优雅回退，绝不中断直播推流。
"""
import logging
import os
import pickle
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import cv2
import numpy as np

from server.core.avatar.neural_model_manager import global_neural_model_manager

logger = logging.getLogger("LiveAgent.NeuralLipRenderer")

# 尝试导入 onnxruntime
try:
    import onnxruntime as ort
    ORT_AVAILABLE = True
except ImportError:
    ort = None
    ORT_AVAILABLE = False


class MelFeatureExtractor:
    """标准音频 Mel 频谱切片提取器 (对齐 Wav2Lip 80 维声学特征)"""

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
        """
        从单声道 float32 音频切片中提取标准 [1, 1, 80, 16] Mel 特征窗口
        若长度不足则对称填充，若过长则裁切中心部分
        """
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

        # 扩充为 [1, 1, 80, 16]
        return mel_spec[np.newaxis, np.newaxis, :, :].astype(np.float32)


class NeuralLipRenderer:
    """真实神经唇形重绘驱动引擎"""

    def __init__(self, model_key: str = "onnx_lipsync", custom_onnx_path: Optional[Path] = None):
        self.model_key = model_key
        self.session: Optional[Any] = None
        self.is_ready: bool = False
        self.mel_extractor = MelFeatureExtractor()

        self.input_names: List[str] = []
        self.output_names: List[str] = []
        self.face_input_shape: Optional[List[int]] = None
        self.audio_input_shape: Optional[List[int]] = None

        # 主播资产缓存
        self.current_anchor_dir: Optional[Path] = None
        self.coords: List[Tuple[int, int, int, int]] = []
        self.face_imgs: List[np.ndarray] = []
        self.full_imgs: List[np.ndarray] = []
        self.has_anchor_assets: bool = False

        self._init_session(custom_onnx_path)

    def _init_session(self, custom_onnx_path: Optional[Path] = None) -> None:
        if not ORT_AVAILABLE or ort is None:
            logger.info("onnxruntime 未安装，神经唇形引擎自动降级 (运行模式: RealAvatarLite)")
            self.is_ready = False
            return

        assert ort is not None

        model_path = custom_onnx_path or global_neural_model_manager.find_model_path(self.model_key)
        if not model_path or not model_path.exists():
            logger.info(f"神经唇形模型权重未就绪 ({self.model_key})，将平滑走 RealAvatarLite 引擎")
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

            self.is_ready = True
            logger.info(
                f"⚡ 真实神经唇形重绘引擎初始化成功! (模型: {model_path.name}, Providers: {self.session.get_providers()})"
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
            dummy_face = np.zeros((1, 6, 256, 256), dtype=np.float32)
            dummy_mel = np.zeros((1, 1, 80, 16), dtype=np.float32)
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

    def load_anchor_assets(self, anchor_dir: Path) -> bool:
        """加载主播已预生成的脸部切片序列 (face_imgs/) 与 coords.pkl 坐标清单"""
        if not anchor_dir.exists():
            return False

        coords_file = anchor_dir / "coords.pkl"
        face_dir = anchor_dir / "face_imgs"

        if not coords_file.exists() or not face_dir.exists():
            self.has_anchor_assets = False
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

            self.current_anchor_dir = anchor_dir
            self.has_anchor_assets = len(self.face_imgs) > 0 and len(self.coords) > 0
            logger.info(
                f"主播切片资产加载就绪: {anchor_dir.name} (人脸切片数: {len(self.face_imgs)}, 坐标数: {len(self.coords)})"
            )
            return self.has_anchor_assets
        except Exception as e:
            logger.warning(f"加载主播切片资产异常: {e}")
            self.has_anchor_assets = False
            return False

    def _prepare_face_input(self, face_256: np.ndarray) -> np.ndarray:
        """
        构建 Wav2Lip 标准 6 通道输入张量 [1, 6, 256, 256]
        前 3 通道为原图，后 3 通道为将下半脸 (y >= 128) 置零的 Masked 人脸
        """
        face_f = face_256.astype(np.float32) / 255.0  # 归一化至 [0, 1]
        masked_face = face_f.copy()
        masked_face[128:, :, :] = 0.0  # 下半脸掩码

        # 拼接 6 通道: shape (256, 256, 6)
        concat_face = np.concatenate([face_f, masked_face], axis=2)
        # 转换为 NCHW: shape (1, 6, 256, 256)
        tensor_face = np.transpose(concat_face, (2, 0, 1))[np.newaxis, :, :, :]
        return tensor_face.astype(np.float32)

    def _blend_back(
        self,
        full_frame: np.ndarray,
        rendered_face_256: np.ndarray,
        coord_box: Tuple[int, int, int, int],
    ) -> np.ndarray:
        """
        利用 coords 坐标与自适应高斯羽化，将神经重绘的口型 ROI 完美无缝融合回原全尺寸帧
        严格保留原片额头、眼睛、脸颊与皮肤真实纹理，杜绝方形切片边缘痕迹
        """
        ymin, ymax, xmin, xmax = coord_box
        fh, fw = full_frame.shape[:2]

        ymin = max(0, min(fh - 1, ymin))
        ymax = max(0, min(fh, ymax))
        xmin = max(0, min(fw - 1, xmin))
        xmax = max(0, min(fw, xmax))

        box_h = ymax - ymin
        box_w = xmax - xmin
        if box_h < 10 or box_w < 10:
            return full_frame

        # 缩放重绘人脸到原检测框大小
        resized_face = cv2.resize(rendered_face_256, (box_w, box_h), interpolation=cv2.INTER_LINEAR)

        # 构建唇周与下半脸的自适应椭圆羽化蒙版 (仅针对下半区做渐变过渡)
        mask = np.zeros((box_h, box_w), dtype=np.float32)
        center_x = box_w // 2
        center_y = int(box_h * 0.72)  # 下唇与嘴中位置
        radius_x = max(5, int(box_w * 0.38))
        radius_y = max(5, int(box_h * 0.28))

        cv2.ellipse(mask, (center_x, center_y), (radius_x, radius_y), 0, 0, 360, 1.0, -1)

        # 高斯模糊羽化边界
        ksize_x = max(3, (box_w // 8) * 2 + 1)
        ksize_y = max(3, (box_h // 8) * 2 + 1)
        mask = cv2.GaussianBlur(mask, (ksize_x, ksize_y), 0)
        mask_3c = np.repeat(mask[:, :, np.newaxis], 3, axis=2)

        # 局部高保真 Alpha 羽化混合
        target_roi = full_frame[ymin:ymax, xmin:xmax].astype(np.float32)
        blended_roi = resized_face.astype(np.float32) * mask_3c + target_roi * (1.0 - mask_3c)
        full_frame[ymin:ymax, xmin:xmax] = np.clip(blended_roi, 0, 255).astype(np.uint8)

        return full_frame

    def render_lip_frame(
        self,
        full_frame: np.ndarray,
        frame_idx: int,
        pcm_window: np.ndarray,
        mouth_open: float = 0.0,
    ) -> Optional[np.ndarray]:
        """
        执行单帧真实神经唇形重绘管线
        参数:
          - full_frame: 原尺寸底模画面 (BGR 或 RGB)
          - frame_idx: 当前播放帧序号
          - pcm_window: 前后 200ms 的单声道 float32 音频切片 (约 3200 采样点)
          - mouth_open: 当前音频能量，低于静音门限时直接跳过推理
        """
        if not self.is_ready or not self.session or not self.has_anchor_assets:
            return None

        # 静音或能量极低时，无需执行深度推理，直接返回原帧保持纯正自然
        if mouth_open < 0.01 and np.max(np.abs(pcm_window)) < 0.01:
            return full_frame

        total_frames = len(self.face_imgs)
        if total_frames == 0 or len(self.coords) == 0:
            return None

        # 循环索引对应切片人脸与坐标
        idx = frame_idx % total_frames
        face_256 = self.face_imgs[idx]
        coord_box = self.coords[idx % len(self.coords)]

        try:
            # 1. 提取 80 维 Mel 窗口 [1, 1, 80, 16]
            mel_tensor = self.mel_extractor.extract_mel_window(pcm_window, target_steps=16)

            # 2. 构建 6 通道输入人脸 [1, 6, 256, 256]
            face_tensor = self._prepare_face_input(face_256)

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
            return result_frame

        except Exception as e:
            logger.debug(f"神经唇形渲染单帧推理跳过 ({e})，将回退微动态引擎")
            return None
