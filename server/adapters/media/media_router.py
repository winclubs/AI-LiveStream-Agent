"""
本地媒体驱动路由中枢：管理程序化头像、远程帧与 mock 降级，不负责外部平台发布。
"""
import inspect
import logging
from typing import Optional
from server.adapters.media.base_driver import BaseMediaDriver
from server.adapters.media.musetalk_driver import (
    global_musetalk_driver,
    MuseTalkMediaDriver,
    CV_AVAILABLE,
)
from server.adapters.media.mock_driver import global_media_driver, MockMediaDriver
from server.core.media.virtual_cam import global_virtual_cam

logger = logging.getLogger("LiveAgent.MediaRouter")

class MediaDriverRouter(BaseMediaDriver):
    def __init__(self):
        super().__init__()
        # 依赖能力检测：无 numpy/opencv 时回退仿真驱动，保证服务可用
        if CV_AVAILABLE:
            self.active_driver: BaseMediaDriver = global_musetalk_driver
            self.driver_type = "procedural_avatar"
        else:
            self.active_driver = global_media_driver
            self.driver_type = "mock"
        # 端云分离 (Tier C) 远程渲染驱动挂载点
        self.remote_driver = None

    def attach_remote(self, driver):
        """挂载端云分离远程渲染驱动 (帧回传优先显示)"""
        self.remote_driver = driver
        logger.info("已挂载端云分离远程渲染帧通道 (Tier C)")

    def detach_remote(self):
        self.remote_driver = None

    def select_driver(self, mode: str = "B", avatar_path: str = "", landmarks_path: str = "") -> BaseMediaDriver:
        """根据当前直播模式与媒体依赖能力选择渲染驱动 (主播底图与人脸关键点同步下发)"""
        if mode == "D" or not CV_AVAILABLE:
            self.active_driver = global_media_driver
            self.driver_type = "mock"
            if not CV_AVAILABLE:
                logger.warning("未安装 numpy/opencv-python，已回退轻量仿真媒体驱动 (功能降级)")
            else:
                logger.info("已切换至轻量仿真媒体驱动 (MockMediaDriver - Tier D)")
        else:
            self.active_driver = global_musetalk_driver
            self.driver_type = "procedural_avatar"
            if avatar_path:
                try:
                    global_musetalk_driver.set_avatar(avatar_path, landmarks_path or None)
                except Exception as e:
                    logger.warning(f"数字人底图/关键点加载失败，使用内置底板: {e}")
            logger.info(f"已切换至程序化数字人渲染驱动 (Procedural Avatar Renderer - 模式 {mode})")
        return self.active_driver

    @staticmethod
    def _supported_metadata(callable_obj, metadata: dict) -> dict:
        try:
            parameters = inspect.signature(callable_obj).parameters
        except (TypeError, ValueError):
            return {}
        if any(param.kind == inspect.Parameter.VAR_KEYWORD for param in parameters.values()):
            return metadata
        return {key: value for key, value in metadata.items() if key in parameters}

    async def feed_audio_chunk(self, audio_bytes: bytes, text_snippet: str, **metadata):
        self.is_speaking = True
        target = self.active_driver.feed_audio_chunk
        await target(
            audio_bytes,
            text_snippet,
            **self._supported_metadata(target, metadata),
        )

    async def interrupt(self, reason: str = "Barge-in", **metadata):
        self.is_speaking = False
        target = self.active_driver.interrupt
        await target(reason, **self._supported_metadata(target, metadata))

    async def start(self):
        await self.active_driver.start()

    async def stop(self):
        self.is_speaking = False
        await self.active_driver.stop()

    @staticmethod
    def _with_capability_boundary(status: dict) -> dict:
        status["capabilities"] = {
            "procedural_avatar": status.get("render_backend") == "procedural",
            "neural_lipsync": False,
            "local_preview": bool(status.get("is_running")),
            "virtual_camera": bool((status.get("virtual_cam") or {}).get("is_active")),
        }
        status["external_publish"] = {
            "status": "not_managed",
            "platform_live": None,
            "validation": "pending_external_acceptance",
        }
        return status

    def get_preview_status(self) -> dict:
        if self.remote_driver is not None and getattr(self.remote_driver, "has_frames", False):
            status = self.remote_driver.get_preview_status()
            status["driver_type"] = "remote_gpu"
            status["virtual_cam"] = global_virtual_cam.get_status()
            status["cv_available"] = CV_AVAILABLE
            return self._with_capability_boundary(status)
        if hasattr(self.active_driver, "get_preview_status"):
            status = self.active_driver.get_preview_status()
            status["driver_type"] = self.driver_type
            return self._with_capability_boundary(status)
        return self._with_capability_boundary({
            "driver_type": self.driver_type,
            "is_speaking": self.is_speaking,
            "fps": 25,
            "cv_available": CV_AVAILABLE,
            "render_backend": "mock",
            "virtual_cam": global_virtual_cam.get_status()
        })

    def get_latest_jpeg(self) -> bytes:
        # 端云分离模式下优先展示云端渲染回传帧
        if self.remote_driver is not None and getattr(self.remote_driver, "has_frames", False):
            return self.remote_driver.get_latest_jpeg()
        if hasattr(self.active_driver, "get_latest_jpeg"):
            return self.active_driver.get_latest_jpeg()
        if hasattr(global_musetalk_driver, "get_latest_jpeg"):
            return global_musetalk_driver.get_latest_jpeg()
        return b""

# 全局媒体中枢单例
global_media_router = MediaDriverRouter()
