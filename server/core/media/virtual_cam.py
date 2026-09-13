"""
本地虚拟摄像头输出服务 (规划 §4.2 / §7 / §15.1)
基于 pyvirtualcam 提供 DirectShow / OBS Virtual Camera 帧推流
若未安装 pyvirtualcam 驱动或环境不可用，自动平滑软降级，不阻断主链路
"""
import logging
import asyncio
from typing import Optional, Tuple

try:
    import numpy as np
    NUMPY_AVAILABLE = True
except Exception:  # pragma: no cover - 环境相关
    np = None
    NUMPY_AVAILABLE = False

try:
    import pyvirtualcam  # type: ignore
    PYVIRTUALCAM_AVAILABLE = True
except ImportError:  # pragma: no cover
    pyvirtualcam = None  # type: ignore
    PYVIRTUALCAM_AVAILABLE = False

logger = logging.getLogger("LiveAgent.VirtualCam")


def letterbox_frame(frame_rgb, width: int, height: int):
    """Fit an RGB frame into the target canvas without changing its aspect ratio."""
    if not NUMPY_AVAILABLE or frame_rgb is None:
        return frame_rgb
    source = np.asarray(frame_rgb)
    if source.ndim != 3 or source.shape[2] != 3:
        raise ValueError("frame_rgb must have shape (height, width, 3)")
    if width <= 0 or height <= 0 or source.shape[0] <= 0 or source.shape[1] <= 0:
        raise ValueError("source and target dimensions must be positive")

    scale = min(width / source.shape[1], height / source.shape[0])
    fitted_width = max(1, min(width, int(round(source.shape[1] * scale))))
    fitted_height = max(1, min(height, int(round(source.shape[0] * scale))))
    if fitted_width == source.shape[1] and fitted_height == source.shape[0]:
        fitted = source.copy()
    else:
        import cv2
        interpolation = cv2.INTER_AREA if scale < 1 else cv2.INTER_LINEAR
        fitted = cv2.resize(source, (fitted_width, fitted_height), interpolation=interpolation)

    canvas = np.zeros((height, width, 3), dtype=source.dtype)
    x0 = (width - fitted_width) // 2
    y0 = (height - fitted_height) // 2
    canvas[y0:y0 + fitted_height, x0:x0 + fitted_width] = fitted
    return canvas


class VirtualCameraService:
    def __init__(self, width: int = 1280, height: int = 720, fps: int = 25):
        self.width = width
        self.height = height
        self.fps = fps
        self.is_active = False
        self.available = bool(PYVIRTUALCAM_AVAILABLE)
        self.cam_device = None
        self.backend_name = "None"
        self.total_frames_sent = 0
        self._last_error = ""

    def start(self) -> bool:
        """初始化并启动虚拟摄像头输出设备"""
        if self.is_active and self.cam_device:
            return True

        if not PYVIRTUALCAM_AVAILABLE or pyvirtualcam is None:
            self._last_error = "未安装 pyvirtualcam 库 (可执行: pip install pyvirtualcam)"
            self.backend_name = "Virtual (Fallback)"
            logger.info("系统未安装 pyvirtualcam 库，虚拟摄像头切换为软件离线兼容模式")
            return False

        try:
            # 优先尝试 OBS Virtual Camera，若无则自动枚举支持的后端 (DirectShow / Unity 等)
            self.cam_device = pyvirtualcam.Camera(
                width=self.width,
                height=self.height,
                fps=self.fps,
                fmt=pyvirtualcam.PixelFormat.RGB
            )
            self.is_active = True
            self.available = True
            self.backend_name = str(getattr(self.cam_device, "native_backend", "pyvirtualcam"))
            self._last_error = ""
            logger.info(f"本地虚拟摄像头已成功挂载输出 ({self.width}x{self.height} @ {self.fps}fps, 设备: {self.cam_device.device})")
            return True
        except Exception as e:
            self._last_error = str(e)
            self.backend_name = "Disabled"
            logger.warning(f"虚拟摄像头启动失败 (可能未启动 OBS 虚拟摄像头驱动): {e}")
            return False

    def send_frame(self, frame_rgb):
        """发送一帧 RGB 图像到虚拟摄像头 (帧率节流由上游 25fps 渲染循环负责，此处不得二次睡眠)"""
        if not self.is_active or not self.cam_device:
            return

        try:
            output_frame = letterbox_frame(frame_rgb, self.width, self.height)
            self.cam_device.send(output_frame)
            self.total_frames_sent += 1
        except Exception as e:
            self._last_error = str(e)

    def stop(self):
        """关闭虚拟摄像头"""
        if self.cam_device:
            try:
                self.cam_device.close()
            except Exception:
                pass
            self.cam_device = None
        self.is_active = False
        logger.info("本地虚拟摄像头已关闭")

    def get_status(self) -> dict:
        return {
            "is_active": self.is_active,
            "available": self.available,
            "backend": self.backend_name,
            "resolution": f"{self.width}x{self.height}",
            "fps": self.fps,
            "frames_sent": self.total_frames_sent,
            "last_error": self._last_error
        }

# 全局单例
global_virtual_cam = VirtualCameraService()
