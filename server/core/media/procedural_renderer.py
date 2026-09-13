"""
程序化数字人帧渲染器 (规划 §4.2 / §14.1)
供本地 MuseTalk 驱动与云端渲染节点共用，避免双端渲染逻辑分叉。

能力：
- 默认演播室数字人底板合成 / 用户肖像底图加载
- 待机微呼吸、周期眨眼
- 音频能量驱动的唇形开合
- 防封杀视觉盾：缓慢推拉运镜、随机漂移、动态呼吸光影
- 拟真微动作（模拟整理衣领/微侧身），并支持挂载用户预录真人小切片目录
"""
import logging
import math
import os
import random
from pathlib import Path
from typing import Optional, Tuple

logger = logging.getLogger("LiveAgent.ProceduralRenderer")

try:
    import numpy as np
    import cv2
    CV_AVAILABLE = True
except Exception:  # pragma: no cover
    np = None
    cv2 = None
    CV_AVAILABLE = False


class MicroExpressionState:
    """
    泊松过程眨眼调度器 (规划 §4.4 防封杀抗死板算法)
    - 眨眼到达服从指数分布 (泊松过程)，平均间隔 2.8 秒，打破固定周期检测指纹
    - 单次闭合时长 100~150ms 随机
    - 状态按传入的时间轴 t 推进，线程安全 (仅依赖调用方串行推进)
    """
    def __init__(self, mean_interval: float = 2.8, blink_ms_range: Tuple[int, int] = (100, 150), rng: Optional[random.Random] = None):
        self.mean_interval = float(mean_interval)
        self.blink_ms_range = blink_ms_range
        self._rng = rng or random
        self._next_blink_at = self._rng.expovariate(1.0 / self.mean_interval)
        self._blink_end_at: Optional[float] = None

    def is_blinking(self, t: float) -> bool:
        if self._blink_end_at is not None:
            if t < self._blink_end_at:
                return True
            self._blink_end_at = None
            self._next_blink_at = t + self._rng.expovariate(1.0 / self.mean_interval)
        if t >= self._next_blink_at:
            self._blink_end_at = t + self._rng.uniform(*self.blink_ms_range) / 1000.0
            return True
        return False


def generate_default_portrait(width: int, height: int):
    """合成标准演播室数字人剪影底板"""
    img = np.zeros((height, width, 3), dtype=np.uint8)
    for y in range(height):
        bg_val = int(18 + 15 * (y / max(1, height)))
        img[y, :, 0] = int(bg_val * 0.9)
        img[y, :, 1] = int(bg_val * 1.1)
        img[y, :, 2] = int(bg_val * 1.2)
    cx = width // 2
    cv2.circle(img, (cx, int(height * 0.47)), int(width * 0.39), (28, 38, 48), -1)
    cv2.ellipse(img, (cx, int(height * 0.78)), (int(width * 0.30), int(height * 0.27)), 0, 0, 360, (40, 50, 65), -1)
    cv2.circle(img, (cx, int(height * 0.42)), int(width * 0.17), (195, 165, 145), -1)
    cv2.ellipse(img, (cx, int(height * 0.33)), (int(width * 0.18), int(height * 0.10)), 0, 180, 360, (30, 30, 35), -1)
    cv2.circle(img, (cx - int(width * 0.06), int(height * 0.40)), int(width * 0.014), (40, 40, 40), -1)
    cv2.circle(img, (cx + int(width * 0.06), int(height * 0.40)), int(width * 0.014), (40, 40, 40), -1)
    cv2.ellipse(img, (cx, int(height * 0.48)), (int(width * 0.045), int(height * 0.010)), 0, 0, 360, (160, 70, 70), -1)
    return img


def build_cover_transform(source_width: int, source_height: int, output_width: int, output_height: int) -> dict:
    """返回与 cover resize + 中心裁切完全一致的整数像素变换参数。"""
    scale = max(output_width / source_width, output_height / source_height)
    resized_width = int(source_width * scale)
    resized_height = int(source_height * scale)
    return {
        "source_size": [source_width, source_height],
        "output_size": [output_width, output_height],
        "resized_size": [resized_width, resized_height],
        "crop_offset": [(resized_width - output_width) // 2, (resized_height - output_height) // 2],
    }


def transform_face_box(face_box, transform: Optional[dict]):
    """把原图人脸框映射到 cover-crop 输出坐标；完全裁出时返回 None。"""
    if not transform or not face_box or len(face_box) != 4:
        return tuple(face_box) if face_box and len(face_box) == 4 else None
    src_w, src_h = transform["source_size"]
    out_w, out_h = transform["output_size"]
    resized_w, resized_h = transform["resized_size"]
    crop_x, crop_y = transform["crop_offset"]
    x, y, width, height = (float(value) for value in face_box)
    if src_w <= 0 or src_h <= 0 or width <= 0 or height <= 0:
        return None
    import math as _math
    left = _math.floor(x * resized_w / src_w - crop_x)
    top = _math.floor(y * resized_h / src_h - crop_y)
    right = _math.ceil((x + width) * resized_w / src_w - crop_x)
    bottom = _math.ceil((y + height) * resized_h / src_h - crop_y)
    left, top = max(0, left), max(0, top)
    right, bottom = min(out_w, right), min(out_h, bottom)
    if right <= left or bottom <= top:
        return None
    return int(left), int(top), int(right - left), int(bottom - top)


def load_or_build_portrait(path: str, width: int, height: int):
    """加载肖像底图并等比居中裁剪；同时返回坐标变换元数据。"""
    img = None
    if path and Path(path).exists() and not Path(path).suffix.lower().endswith(".svg"):
        try:
            img = cv2.imread(str(path))
            if img is not None:
                img = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
        except Exception as e:
            logger.warning(f"加载肖像底图失败，使用内置底板: {e}")
    if img is None:
        return generate_default_portrait(width, height), None
    h, w = img.shape[:2]
    transform = build_cover_transform(w, h, width, height)
    nw, nh = transform["resized_size"]
    x0, y0 = transform["crop_offset"]
    resized = cv2.resize(img, (nw, nh))
    return resized[y0:y0 + height, x0:x0 + width], transform


def synth_frame(base_portrait, width: int, height: int, t: float, mouth_open: float,
                face_box: Optional[Tuple[int, int, int, int]] = None, action_clip=None,
                is_blinking: Optional[bool] = None):
    """
    合成单帧。face_box 用于定位口型/眨眼区域 (来自人脸检测)，缺省用底板中心比例。
    action_clip: 可选 numpy 图像 (用户预录真人小切片)，周期性无缝插入以打破视频哈希指纹。
    is_blinking: 显式眨眼状态 (由泊松调度器 MicroExpressionState 给出)；缺省回退固定周期。
    """
    if base_portrait is None:
        return np.zeros((height, width, 3), dtype=np.uint8)

    frame = base_portrait.copy()

    # 口型/眼睛定位
    if face_box and face_box[2] > 0 and face_box[3] > 0:
        fx, fy, fw, fh = face_box
        mouth_cx = fx + fw // 2
        mouth_cy = fy + int(fh * 0.78)
        eye_dy = fy + int(fh * 0.42)
        eye_dx = int(fw * 0.22)
        mouth_rx = max(10, int(fw * 0.11))
    else:
        mouth_cx, mouth_cy = width // 2, int(height * 0.48)
        eye_dy = int(height * 0.40)
        eye_dx = int(width * 0.06)
        mouth_rx = max(10, int(width * 0.045))

    breath_offset = int(math.sin(t * 1.96) * 2)
    if is_blinking is None:
        blink_phase = (t % 4.2)
        is_blinking = blink_phase > 4.0

    if mouth_open > 0.05:
        open_h = int(12 * mouth_open)
        cy = mouth_cy + breath_offset
        cv2.ellipse(frame, (mouth_cx, cy), (mouth_rx, 8 + open_h), 0, 0, 360, (50, 15, 20), -1)
        cv2.ellipse(frame, (mouth_cx, cy - 2), (max(6, mouth_rx - 8), 3), 0, 0, 360, (230, 230, 235), -1)
        cv2.ellipse(frame, (mouth_cx, cy), (mouth_rx + 2, 10 + open_h), 0, 0, 360, (170, 75, 80), 2)
    else:
        cv2.ellipse(frame, (mouth_cx, mouth_cy + breath_offset), (mouth_rx, 7), 0, 0, 360, (160, 68, 72), -1)

    if is_blinking:
        cv2.line(frame, (mouth_cx - eye_dx - 10, eye_dy + breath_offset), (mouth_cx - eye_dx + 10, eye_dy + breath_offset), (40, 30, 30), 2)
        cv2.line(frame, (mouth_cx + eye_dx - 10, eye_dy + breath_offset), (mouth_cx + eye_dx + 10, eye_dy + breath_offset), (40, 30, 30), 2)

    # 防封杀视觉盾：缓慢推拉运镜 + 随机漂移 + 动态呼吸光影
    zoom = 1.0 + 0.035 * (0.5 + 0.5 * math.sin(t * 2 * math.pi / 47.0))
    pan_x = int(7 * math.sin(t * 2 * math.pi / 37.0))
    pan_y = int(5 * math.cos(t * 2 * math.pi / 53.0))
    M = cv2.getRotationMatrix2D((width / 2.0, height / 2.0), 0.0, zoom)
    M[0, 2] += pan_x
    M[1, 2] += pan_y
    frame = cv2.warpAffine(frame, M, (width, height), borderMode=cv2.BORDER_REPLICATE)
    flick = 1.0 + 0.018 * math.sin(t * 2 * math.pi / 6.3)
    frame = np.clip(frame.astype(np.float32) * flick, 0, 255).astype(np.uint8)

    # 拟真微动作 / 用户真人小切片周期无缝插入 (每 45~75 秒一次，持续约 1.2 秒)
    if action_clip is not None:
        action_cycle = 55.0
        phase = t % action_cycle
        if phase < 1.2:
            clip = action_clip
            if clip.shape[0] != height or clip.shape[1] != width:
                clip = cv2.resize(clip, (width, height))
            alpha = math.sin(phase / 1.2 * math.pi)  # 淡入淡出
            frame = cv2.addWeighted(clip, alpha, frame, 1 - alpha, 0)

    # 合规微标
    cv2.putText(frame, "AI GENERATED STREAM", (width - 240, height - 24),
                cv2.FONT_HERSHEY_SIMPLEX, 0.45, (160, 180, 190), 1, cv2.LINE_AA)
    return frame


def encode_jpeg(frame, quality: int = 80) -> bytes:
    if not CV_AVAILABLE or frame is None:
        return b""
    ok, buf = cv2.imencode(".jpg", cv2.cvtColor(frame, cv2.COLOR_RGB2BGR), [cv2.IMWRITE_JPEG_QUALITY, quality])
    return buf.tobytes() if ok else b""


def audio_rms_to_mouth(
    audio_bytes: bytes,
    scale: float = 11.7,
    *,
    codec: Optional[str] = None,
    sample_rate: int = 24000,
    channels: int = 1,
) -> float:
    """
    将音频切片能量映射为口型开合度 (0.15~0.95)。
    输入先经统一解码器还原为 float32 采样 (兼容 MP3/WAV 容器与裸 PCM)，
    严禁直接对压缩字节计算伪 RMS。scale 针对归一化浮点域
    (等价旧 int16 域的 2800/32768 ≈ 11.7)。
    """
    try:
        from server.core.media.audio_decode import decode_audio_to_float32
        explicit_codec = codec
        if explicit_codec is None:
            if audio_bytes.startswith(b"RIFF"):
                explicit_codec = "wav"
            elif audio_bytes.startswith(b"ID3") or audio_bytes[:2] in (b"\xff\xfb", b"\xff\xf3", b"\xff\xf2"):
                explicit_codec = "mp3"
            elif audio_bytes.startswith(b"OggS"):
                explicit_codec = "ogg"
            elif audio_bytes.startswith(b"fLaC"):
                explicit_codec = "flac"
            else:
                explicit_codec = "pcm_s16le"
        samples, _sr = decode_audio_to_float32(
            audio_bytes,
            sample_rate,
            codec=explicit_codec,
            channels=channels,
        )
        if samples is None or len(samples) == 0:
            return 0.35
        rms = float(np.sqrt(np.mean(np.square(samples))))
        return min(1.0, max(0.15, rms * scale))
    except Exception:
        return 0.4
