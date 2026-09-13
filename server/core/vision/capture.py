"""
实时多模态视觉感知通道 (规划 §2.2 / §5.3 / §9.3)
- 支持桌面/OBS 画面截屏 (desktop_screen) 与 USB 摄像头 (usb_camera) 两种来源
- 按配置采样频次抓取画面并编码为 JPEG base64，供多模态大模型"眼见即所言"
- 依赖缺失 (PIL/cv2) 时自动禁用并上报状态，不阻断主链路
"""
import base64
import io
import logging
import time
from pathlib import Path
from typing import Optional

logger = logging.getLogger("LiveAgent.Vision")

try:
    import numpy as np
    NP_AVAILABLE = True
except Exception:  # pragma: no cover
    np = None
    NP_AVAILABLE = False

try:
    import cv2
    CV_AVAILABLE = True
except Exception:  # pragma: no cover
    cv2 = None
    CV_AVAILABLE = False

try:
    from PIL import ImageGrab, Image
    PIL_AVAILABLE = True
except Exception:  # pragma: no cover
    ImageGrab = None
    Image = None
    PIL_AVAILABLE = False


class VisionCapture:
    """桌面截屏 / USB 摄像头画面采集器"""

    def __init__(self):
        self.enabled = False
        self.source = "desktop_screen"      # desktop_screen / usb_camera
        self.interval_sec = 2.5
        self.camera_index = 0
        self.last_ts = 0.0
        self.last_jpeg: bytes = b""
        self.last_error = ""
        self._cam = None

    # ------------------------------------------------------------------
    @property
    def available(self) -> bool:
        if self.source == "usb_camera":
            return CV_AVAILABLE
        return PIL_AVAILABLE or CV_AVAILABLE

    def configure(self, enabled: bool, source: str = None, interval_sec: float = None, camera_index: int = None):
        self.enabled = bool(enabled)
        if source:
            self.source = source
        if interval_sec:
            self.interval_sec = max(0.5, float(interval_sec))
        if camera_index is not None:
            self.camera_index = int(camera_index)

    def _grab_desktop(self):
        # 优先 PIL.ImageGrab (Windows/macOS 原生)
        if PIL_AVAILABLE:
            try:
                shot = ImageGrab.grab()
                if np is not None:
                    frame = np.array(shot.convert("RGB"))
                else:
                    buf = io.BytesIO()
                    shot.convert("RGB").save(buf, format="JPEG", quality=80)
                    self.last_jpeg = buf.getvalue()
                    return None
                return frame
            except Exception as e:
                self.last_error = f"桌面截屏失败: {e}"
        return None

    def _grab_camera(self):
        if not CV_AVAILABLE:
            self.last_error = "未安装 opencv-python，无法使用摄像头视觉通道"
            return None
        try:
            if self._cam is None or not self._cam.isOpened():
                self._cam = cv2.VideoCapture(self.camera_index)
            ok, frame = self._cam.read()
            if not ok:
                self.last_error = "摄像头读取失败 (设备被占用或不存在)"
                return None
            return cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        except Exception as e:
            self.last_error = f"摄像头采集异常: {e}"
            return None

    def capture_jpeg(self, force: bool = False) -> bytes:
        """抓取一帧并编码为 JPEG 字节 (按 interval 节流)"""
        if not self.enabled and not force:
            return b""
        now = time.time()
        if not force and now - self.last_ts < self.interval_sec:
            return self.last_jpeg
        self.last_ts = now

        frame = self._grab_desktop() if self.source == "desktop_screen" else self._grab_camera()
        if frame is None:
            # _grab_desktop 在无 numpy 时已直接写入 self.last_jpeg
            return self.last_jpeg

        try:
            if CV_AVAILABLE:
                bgr = cv2.cvtColor(frame, cv2.COLOR_RGB2BGR)
                # 适当缩小以降低多模态 Token 成本
                h, w = bgr.shape[:2]
                if w > 960:
                    scale = 960.0 / w
                    bgr = cv2.resize(bgr, (960, int(h * scale)))
                ok, buf = cv2.imencode(".jpg", bgr, [cv2.IMWRITE_JPEG_QUALITY, 78])
                if ok:
                    self.last_jpeg = buf.tobytes()
            elif PIL_AVAILABLE:
                buf = io.BytesIO()
                Image.fromarray(frame).save(buf, format="JPEG", quality=78)
                self.last_jpeg = buf.getvalue()
            self.last_error = ""
        except Exception as e:
            self.last_error = f"画面编码失败: {e}"
        return self.last_jpeg

    def capture_b64(self, force: bool = False) -> str:
        jpeg = self.capture_jpeg(force=force)
        if not jpeg:
            return ""
        return base64.b64encode(jpeg).decode("utf-8")

    def close(self):
        if self._cam is not None:
            try:
                self._cam.release()
            except Exception:
                pass
            self._cam = None

    def get_status(self) -> dict:
        return {
            "enabled": self.enabled,
            "source": self.source,
            "available": self.available,
            "interval_sec": self.interval_sec,
            "has_frame": bool(self.last_jpeg),
            "last_error": self.last_error,
            "pil_available": PIL_AVAILABLE,
            "cv_available": CV_AVAILABLE,
        }


global_vision = VisionCapture()

# 视觉意图关键词 (规划 §2.2 / §9.3 智能唤醒与 Token 节流)
VISION_INTENT_KEYWORDS = [
    "看", "长相", "外观", "长什么样", "手里", "身上", "穿", "衣服", "颜色", "多大", "展示",
    "屏幕", "板书", "黑板", "课件", "这件", "这款", "特写", "镜头", "比划", "手势",
    "后面", "背景", "身后", "这是啥", "这是什么", "什么样"
]


def is_vision_query(text: str) -> bool:
    """
    智能意图判定：检测当前观众提问/互动是否涉及视觉感知
    避免在普通闲聊中盲目注入高分辨率图像切片导致 Token 消耗激增与请求延迟
    """
    if not text:
        return False
    t = str(text).strip().lower()
    return any(kw in t for kw in VISION_INTENT_KEYWORDS)

