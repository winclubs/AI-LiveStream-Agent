"""
低配电脑真人微动态与轻量口型融合渲染器 (RealAvatarLiteRenderer)
基于真人待机呼吸/微动作切片视频循环底池 + 实时下唇仿射形变与高斯羽化无缝融合算法。
专为无独立显卡、低算力核显轻薄本设计，单帧 CPU 运算仅需 1~4ms，平稳输出 1080P/720P 高清写实画面。
"""

import logging
import math
import os
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

try:
    import cv2
    import numpy as np
    CV_AVAILABLE = True
except ImportError:  # pragma: no cover
    cv2 = None
    np = None
    CV_AVAILABLE = False

logger = logging.getLogger("LiveAgent.RealAvatarLite")


def blend_lip_warp(
    frame: "np.ndarray",
    cx: int,
    cy: int,
    roi_w: int,
    roi_h: int,
    mouth_open: float,
    mouth_form: float = 0.0,
    drop_px: float = 4.5,
) -> "np.ndarray":
    """
    在嘴部局部 ROI 内进行精细的真实下唇形变与无缝融合 (直播级口型算法，单一真相源)
    保留原底模真实毛孔与唇纹，杜绝色块涂抹。

    - drop_px: 下唇最大下沉像素 (直播原始分辨率默认 4.5px；
      256x256 对齐人脸切片等小尺寸画面可按 ROI 等比放大，确保口型开合清晰可见)。
    """
    fh, fw = frame.shape[:2]
    x1 = max(0, cx - roi_w // 2)
    x2 = min(fw, cx + roi_w // 2)
    y1 = max(0, cy - roi_h // 2)
    y2 = min(fh, cy + roi_h // 2)

    rw = x2 - x1
    rh = y2 - y1
    if rw < 10 or rh < 10:
        return frame

    roi = frame[y1:y2, x1:x2].copy()
    rel_cy = float(cy - y1)
    rel_cx = float(cx - x1)

    # 真实仿射微位移：下半唇依音频能量下沉，上半唇微抬 (下唇的 1/4)
    drop_y = float(mouth_open * max(1.0, drop_px))
    rx = float(max(10, rw * 0.32))
    ry_down = float(max(8, rh * 0.35))
    ry_up = float(max(6, rh * 0.16))
    stretch = float(mouth_form * 0.08 * mouth_open)

    # 构建局部网格重映射
    map_x = np.tile(np.arange(rw, dtype=np.float32), (rh, 1))
    map_y = np.tile(np.arange(rh, dtype=np.float32)[:, np.newaxis], (1, rw))

    dx = map_x - rel_cx
    dy = map_y - rel_cy

    weight_x = np.exp(-(dx**2) / (2.0 * (rx**2)))
    mask_down = (dy > 0).astype(np.float32)
    dist_y_down = np.clip(dy / ry_down, 0.0, 1.0)
    weight_down = np.sin(dist_y_down * np.pi) * mask_down * weight_x

    mask_up = (dy < 0).astype(np.float32)
    dist_y_up = np.clip(-dy / ry_up, 0.0, 1.0)
    weight_up = np.sin(dist_y_up * np.pi) * mask_up * weight_x

    map_y_shifted = map_y - weight_down * drop_y + weight_up * (drop_y * 0.25)
    map_x_shifted = map_x - weight_x * (dx * stretch)

    warped_roi = cv2.remap(
        roi,
        map_x_shifted.astype(np.float32),
        map_y_shifted.astype(np.float32),
        cv2.INTER_LINEAR,
        borderMode=cv2.BORDER_REFLECT,
    )

    # 唇缝闭合微影 (极微弱自然深红肉色，非黑斑涂抹)
    if mouth_open > 0.18:
        seam_h = max(1.0, mouth_open * 2.2)
        seam_w = max(5.0, float(rw * 0.22) * (1.0 + stretch))
        dist_seam = ((dx / seam_w)**2 + (dy / seam_h)**2)
        alpha = np.clip(1.0 - dist_seam, 0.0, 1.0) ** 2 * float(mouth_open * 0.35)
        alpha_3 = alpha[:, :, np.newaxis]
        # 自适应提取原唇缝色调微调
        lip_dark = np.array([45, 30, 85], dtype=np.float32)  # BGR 深红肉暗影
        warped_roi = (warped_roi.astype(np.float32) * (1.0 - alpha_3) + lip_dark * alpha_3).astype(np.uint8)

    # 外围高斯羽化融入原画面
    mask = np.zeros((rh, rw), dtype=np.float32)
    cv2.ellipse(mask, (int(rel_cx), int(rel_cy)), (int(rw * 0.44), int(rh * 0.42)), 0, 0, 360, 1.0, -1)
    ksize = (max(3, (rw // 8) * 2 + 1), max(3, (rh // 8) * 2 + 1))
    mask = cv2.GaussianBlur(mask, ksize, 0)
    mask_3c = np.repeat(mask[:, :, np.newaxis], 3, axis=2)

    orig_roi = frame[y1:y2, x1:x2].astype(np.float32)
    blended = warped_roi.astype(np.float32) * mask_3c + orig_roi * (1.0 - mask_3c)
    frame[y1:y2, x1:x2] = np.clip(blended, 0, 255).astype(np.uint8)
    return frame


class RealAvatarLiteRenderer:
    """
    轻量级真人微动态与口型融合引擎
    - 支持加载真人循环微动态短视频 (MP4/切片) 或高清真人肖像
    - 采用局部 ROI 仿射位移 + Alpha 羽化融合，保留真实皮肤质感与毛孔细节
    - 纯 CPU 计算，免显卡、免昂贵神经模型权重
    """

    def __init__(
        self,
        source_path: str = "",
        width: int = 720,
        height: int = 960,
        fps: int = 25,
        landmarks_path: Optional[str] = None,
    ):
        self.source_path = source_path
        self.width = width
        self.height = height
        self.fps = fps
        self.landmarks_path = landmarks_path

        self._lock = threading.RLock()
        self._frame_pool: List[Any] = []
        self._pool_size: int = 0
        self._is_video_source: bool = False
        self._static_portrait = None
        self._face_box: Optional[Tuple[int, int, int, int]] = None

        self.total_frames_rendered = 0
        self.avg_render_ms = 0.0

        if CV_AVAILABLE:
            self.reload_source(source_path, landmarks_path)

    def reload_source(self, source_path: str, landmarks_path: Optional[str] = None):
        """重新载入真人素材 (支持短视频 MP4 或高清图片)"""
        with self._lock:
            self.source_path = str(source_path or "").strip()
            self.landmarks_path = landmarks_path
            self._frame_pool.clear()
            self._pool_size = 0
            self._is_video_source = False
            self._static_portrait = None
            self._face_box = None

            if not CV_AVAILABLE:
                return

            if not self.source_path or not os.path.exists(self.source_path):
                # 未指定或不存在，生成高品质默认演示底板
                from server.core.media.procedural_renderer import generate_default_portrait
                self._static_portrait = generate_default_portrait(self.width, self.height)
                return

            ext = os.path.splitext(self.source_path)[1].lower()
            if ext in (".mp4", ".mov", ".avi", ".webm", ".mkv"):
                self._load_video_pool(self.source_path)
            else:
                self._load_image_source(self.source_path)

            # 加载面部框 Landmarks
            if self.landmarks_path and os.path.exists(self.landmarks_path):
                try:
                    import json
                    with open(self.landmarks_path, "r", encoding="utf-8") as f:
                        data = json.load(f)
                    box = data.get("face_box")
                    if box and len(box) == 4 and box[2] > 0 and box[3] > 0:
                        self._face_box = tuple(box)
                except Exception as e:
                    logger.warning(f"读取人脸框标注失败: {e}")

    def _load_video_pool(self, video_path: str, max_frames: int = 250):
        """从微动态视频中读取并预热帧池 (乒乓往复循环，消除首尾跳跃)"""
        try:
            cap = cv2.VideoCapture(video_path)
            raw_frames = []
            while cap.isOpened() and len(raw_frames) < max_frames:
                ret, frame = cap.read()
                if not ret or frame is None:
                    break
                # 转为 RGB 并裁切对齐目标分辨率
                frame_rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                h, w = frame_rgb.shape[:2]
                scale = max(self.width / w, self.height / h)
                nw, nh = int(w * scale), int(h * scale)
                resized = cv2.resize(frame_rgb, (nw, nh))
                x0 = (nw - self.width) // 2
                y0 = (nh - self.height) // 2
                cropped = resized[y0 : y0 + self.height, x0 : x0 + self.width]
                raw_frames.append(cropped)
            cap.release()

            if raw_frames:
                # 构造乒乓循环 (0 -> N-1 -> 1)
                ping_pong = raw_frames + raw_frames[-2:0:-1]
                self._frame_pool = ping_pong
                self._pool_size = len(ping_pong)
                self._is_video_source = True
                logger.info(f"真人微动态视频帧池加载就绪，共缓存 {self._pool_size} 帧")
            else:
                self._load_image_source(video_path)
        except Exception as e:
            logger.error(f"加载真人微动态视频失败，回退静态图: {e}")
            self._load_image_source(video_path)

    def _load_image_source(self, img_path: str):
        """加载高清真人肖像静态图"""
        try:
            from server.core.media.procedural_renderer import load_or_build_portrait
            self._static_portrait, _ = load_or_build_portrait(img_path, self.width, self.height)
            self._is_video_source = False
        except Exception as e:
            logger.warning(f"加载真人图像失败: {e}")
            from server.core.media.procedural_renderer import generate_default_portrait
            self._static_portrait = generate_default_portrait(self.width, self.height)

    def render_frame(
        self,
        t: float,
        mouth_open: float = 0.0,
        mouth_form: float = 0.0,
        is_blinking: bool = False,
    ) -> Any:
        """
        合成单帧真人画面 (局部下唇形变 + 高斯羽化无缝融合)
        纯 CPU 计算，耗时 < 4ms
        """
        if not CV_AVAILABLE:
            return None

        t0 = time.perf_counter()

        with self._lock:
            # 1. 获取当前待机微动态底图
            if self._is_video_source and self._pool_size > 0:
                frame_idx = int(t * self.fps) % self._pool_size
                base_frame = self._frame_pool[frame_idx].copy()
            elif self._static_portrait is not None:
                base_frame = self._static_portrait.copy()
                # 施加平滑微呼吸律动 (Y轴 1~2 像素)
                breath_dy = int(math.sin(t * 2.0) * 1.5)
                if breath_dy != 0:
                    M = np.float32([[1, 0, 0], [0, 1, breath_dy]])
                    base_frame = cv2.warpAffine(
                        base_frame, M, (self.width, self.height), borderMode=cv2.BORDER_REPLICATE
                    )
            else:
                from server.core.media.procedural_renderer import generate_default_portrait
                base_frame = generate_default_portrait(self.width, self.height)

            # 2. 定位嘴部关键区域
            if self._face_box and self._face_box[2] > 0 and self._face_box[3] > 0:
                fx, fy, fw, fh = self._face_box
                mouth_cx = fx + fw // 2
                mouth_cy = fy + int(fh * 0.76)
                mouth_w = max(40, int(fw * 0.46))
                mouth_h = max(30, int(fh * 0.32))
            else:
                mouth_cx = self.width // 2
                mouth_cy = int(self.height * 0.49)
                mouth_w = max(60, int(self.width * 0.18))
                mouth_h = max(40, int(self.height * 0.09))

            # 3. 实时唇部微网格形变与自适应羽化融合
            if mouth_open > 0.04:
                base_frame = self._blend_lip_warp(
                    base_frame,
                    cx=mouth_cx,
                    cy=mouth_cy,
                    roi_w=mouth_w,
                    roi_h=mouth_h,
                    mouth_open=min(1.0, mouth_open),
                    mouth_form=max(-1.0, min(1.0, mouth_form)),
                )

        cost_ms = (time.perf_counter() - t0) * 1000.0
        self.total_frames_rendered += 1
        self.avg_render_ms = self.avg_render_ms * 0.95 + cost_ms * 0.05
        return base_frame

    def _blend_lip_warp(
        self,
        frame: "np.ndarray",
        cx: int,
        cy: int,
        roi_w: int,
        roi_h: int,
        mouth_open: float,
        mouth_form: float,
    ) -> "np.ndarray":
        """直播口型形变：委托模块级 blend_lip_warp 单一真相源实现"""
        return blend_lip_warp(
            frame,
            cx=cx,
            cy=cy,
            roi_w=roi_w,
            roi_h=roi_h,
            mouth_open=min(1.0, mouth_open),
            mouth_form=max(-1.0, min(1.0, mouth_form)),
            drop_px=4.5,
        )


# 全局单例
global_real_avatar_lite = RealAvatarLiteRenderer()
