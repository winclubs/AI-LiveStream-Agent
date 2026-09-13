"""
程序化实时数字人渲染驱动 (规划 §4.2 / §7 / §15.1)

说明（能力边界，避免命名误导）：
- 本驱动为 **零权重程序化数字人渲染器**：基于 OpenCV 在主播底图上合成待机微呼吸、
  周期眨眼与由音频能量驱动的唇形开合，并以 25fps 输出连续视频帧。
- 它 **不加载 MuseTalk/UNet 等神经唇形模型**，口型为音频能量开合而非音素级对齐；
  真正的 MuseTalk 权重推理通道预留于 `RENDER_BACKEND=musetalk` 分支（需另行部署）。
- 默认开启本地虚拟摄像头输出 (pyvirtualcam / OBS DirectShow)，未安装则平滑软降级。
- 提供最新 JPEG 图像帧流，供给前端 Web 大屏实时监视器。

依赖缺失时（未安装 numpy/opencv）本模块仍可安全导入，但渲染能力自动关闭，
由 media_router 回退到仿真媒体驱动，保证核心服务永不因缺失可选依赖而启动失败。
"""

import asyncio
import logging
import math
import queue
import threading
import time
from typing import Optional, Tuple
from pathlib import Path

from server.adapters.media.base_driver import BaseMediaDriver
from server.core.media.virtual_cam import global_virtual_cam
from server.core.media.av_sync import global_av_sync
from server.core.media.scene_overlay import compose_scene_overlays, global_scene_overlay_state

# 可选重依赖：numpy / opencv-python。缺失时优雅降级而非崩溃
try:
    import numpy as np
    import cv2
    CV_AVAILABLE = True
except Exception:  # pragma: no cover - 环境相关
    np = None
    cv2 = None
    CV_AVAILABLE = False

# 渲染后端：默认程序化渲染；设置为 musetalk 时预留接入真实神经唇形权重
import os as _os
RENDER_BACKEND = _os.getenv("LIVE_AGENT_RENDER_BACKEND", "procedural").lower()

logger = logging.getLogger("LiveAgent.MuseTalkDriver")

class MuseTalkMediaDriver(BaseMediaDriver):
    def __init__(self, avatar_source_path: str = "", landmarks_cache: Optional[str] = None):
        super().__init__()
        self.avatar_source_path = avatar_source_path
        self.landmarks_cache = landmarks_cache
        self.cv_available = CV_AVAILABLE
        if RENDER_BACKEND != "procedural":
            # 真实 MuseTalk 神经唇形推理通道尚未随包交付 (需权重部署，见规划 §4.2)；
            # 为避免状态上报与实际能力不符 (ADR-16③)，显式回退程序化渲染并如实上报
            logger.warning(
                f"请求的渲染后端 '{RENDER_BACKEND}' 暂未随包提供推理实现，已回退 procedural 程序化渲染"
            )
            self.render_backend = "procedural"
        else:
            self.render_backend = RENDER_BACKEND
        self.fps = 25
        self.width = 720
        self.height = 960  # 直播标准 9:16 竖屏高宽比
        self.is_running = False
        self.is_speaking = False
        self.current_frame_id = 0
        self.render_thread: Optional[threading.Thread] = None

        # 音频能量队列 (驱动唇形开合度: 0.0 闭合 ~ 1.0 完全张开)；渲染在独立线程，用线程安全队列
        self.mouth_open_queue = queue.Queue()
        self.current_mouth_open = 0.0
        self.target_mouth_open = 0.0
        self._generation_lock = threading.Lock()
        self._accepted_audio_generation = 0
        self._accepted_session_generation = 0

        # 泊松过程眨眼调度器 (规划 §4.4)
        from server.core.media.procedural_renderer import MicroExpressionState
        self.micro_expr = MicroExpressionState()

        # 肖像底图与面部关键点参考
        self.base_portrait: Optional[np.ndarray] = None
        self.face_box: Optional[Tuple[int, int, int, int]] = None
        self.action_clip = None

        # 最新 JPEG 二进制帧缓存 (供前端 /live/stream/preview 实时抓取)
        self.latest_jpeg_frame: bytes = b""
        self.total_frames_rendered = 0
        self.start_ts = 0.0

        # 初始化肖像底图
        self._load_or_generate_avatar(avatar_source_path)

    def _load_landmarks(self, path: str, transform: Optional[dict] = None):
        """读取原图人脸框并映射到 720x960 cover-crop 输出坐标。"""
        if not path:
            return None
        try:
            import json
            candidate = Path(path)
            if candidate.exists() and candidate.suffix.lower() == ".json":
                data = json.loads(candidate.read_text(encoding="utf-8"))
                box = data.get("face_box")
                if box and len(box) == 4 and box[2] > 0:
                    from server.core.media.procedural_renderer import transform_face_box
                    return transform_face_box(box, transform)
        except Exception:
            pass
        return None

    def _load_or_generate_avatar(self, path: str):
        """加载主播底图 (委托共享渲染器)，并加载人脸框与可选真人动作切片"""
        if not CV_AVAILABLE:
            self.base_portrait = None
            self.latest_jpeg_frame = b""
            return
        from server.core.media.procedural_renderer import load_or_build_portrait, encode_jpeg
        self.base_portrait, transform = load_or_build_portrait(path, self.width, self.height)
        self.face_box = self._load_landmarks(self.landmarks_cache, transform)

        # 可选：用户预录真人小切片目录 (data/avatars/actions/*.jpg)
        self.action_clip = None
        try:
            actions_dir = Path(path).parent / "actions" if path else None
            if actions_dir and actions_dir.exists():
                clips = sorted([p for p in actions_dir.iterdir() if p.suffix.lower() in (".jpg", ".jpeg", ".png")])
                if clips:
                    self.action_clip = cv2.cvtColor(cv2.imread(str(clips[0])), cv2.COLOR_BGR2RGB)
                    logger.info(f"已加载真人动作切片用于防封杀穿插: {clips[0].name}")
        except Exception:
            self.action_clip = None

        self.latest_jpeg_frame = encode_jpeg(self.base_portrait, quality=85)

    async def start(self):
        if self.is_running:
            return
        self.is_running = True
        self.start_ts = time.time()

        if not CV_AVAILABLE:
            logger.warning(
                "未安装 numpy/opencv-python，程序化数字人渲染已禁用（仅音频链路可用）。"
                "如需数字人画面，请执行: pip install numpy opencv-python"
            )
            return

        # 尝试开启本地虚拟摄像头
        global_virtual_cam.start()

        # 启动 25 FPS 连续驱动渲染主线程 (独立于事件循环，避免 cv2/JPEG 编码阻塞主循环)
        self.render_thread = threading.Thread(
            target=self._render_thread_loop,
            name="AvatarRenderLoop",
            daemon=True,
        )
        self.render_thread.start()
        logger.info(f"数字人渲染引擎已启动 (后端: {self.render_backend}, 25fps 视频线程，底图分辨率: {self.width}x{self.height})")

    async def stop(self):
        self.is_running = False
        if self.render_thread and self.render_thread.is_alive():
            self.render_thread.join(timeout=3.0)
        self.render_thread = None
        await self.interrupt("Stop Driver")
        global_virtual_cam.stop()
        logger.info("MuseTalkMediaDriver 渲染引擎已停止")

    def set_avatar(self, source_path: str, landmarks_path: Optional[str] = None):
        """动态切换数字人形象底图与人脸关键点缓存 (口型/眨眼定位)"""
        self.avatar_source_path = source_path
        self.landmarks_cache = landmarks_path
        self._load_or_generate_avatar(source_path)
        logger.info(f"数字人形象底图已更新: {source_path} (关键点: {landmarks_path or '未提供'})")

    def _generation_is_current(self, audio_generation, session_generation) -> bool:
        with self._generation_lock:
            return (
                (audio_generation is None or audio_generation >= self._accepted_audio_generation)
                and (session_generation is None or session_generation >= self._accepted_session_generation)
            )

    async def feed_audio_chunk(
        self,
        audio_bytes: bytes,
        text_snippet: str,
        *,
        codec: str = "pcm_s16le",
        sample_rate: int = 24000,
        channels: int = 1,
        audio_generation: Optional[int] = None,
        session_generation: Optional[int] = None,
    ):
        """按真实采样时长生成带双代际标签的 25fps 口型目标。"""
        if not audio_bytes:
            return

        with self._generation_lock:
            if audio_generation is not None:
                if audio_generation < self._accepted_audio_generation:
                    return
                self._accepted_audio_generation = max(
                    self._accepted_audio_generation, int(audio_generation)
                )
            if session_generation is not None:
                if session_generation < self._accepted_session_generation:
                    return
                self._accepted_session_generation = max(
                    self._accepted_session_generation, int(session_generation)
                )

        self.is_speaking = True
        if not self.cv_available:
            return
        try:
            from server.core.media.audio_decode import decode_audio_to_float32
            samples, decoded_sample_rate = await asyncio.to_thread(
                decode_audio_to_float32,
                audio_bytes,
                sample_rate,
                codec=codec,
                channels=channels,
            )
            if not self._generation_is_current(audio_generation, session_generation):
                return
            if samples is None or decoded_sample_rate <= 0:
                raise ValueError("无法解码音频")
            samples_per_frame = max(1, int(decoded_sample_rate / self.fps))
            for start in range(0, len(samples), samples_per_frame):
                if not self._generation_is_current(audio_generation, session_generation):
                    return
                frame_samples = samples[start:start + samples_per_frame]
                if len(frame_samples) == 0:
                    continue
                rms = float(np.sqrt(np.mean(frame_samples.astype(np.float32) ** 2)))
                self.mouth_open_queue.put((
                    audio_generation,
                    session_generation,
                    min(1.0, max(0.15, rms * 11.7)),
                ))
        except Exception as exc:
            if self._generation_is_current(audio_generation, session_generation):
                logger.warning("口型音频解码失败，使用单帧保守口型: %s", exc)
                self.mouth_open_queue.put((audio_generation, session_generation, 0.3))

    async def interrupt(self, reason: str = "Barge-in", next_generation: Optional[int] = None):
        """推进口型代际并清空排队帧，旧 producer 恢复后也无法重新入队。"""
        with self._generation_lock:
            if next_generation is not None:
                self._accepted_audio_generation = max(
                    self._accepted_audio_generation, int(next_generation)
                )
        self.is_speaking = False
        self.target_mouth_open = 0.0
        while True:
            try:
                self.mouth_open_queue.get_nowait()
            except queue.Empty:
                break
        logger.info(f"MuseTalk 驱动收到打断信号 [{reason}]，口型立即平滑归位")

    def _render_thread_loop(self):
        """核心视频驱动主循环 (独立线程)：精确维持 25 FPS (40ms)，绝不阻塞事件循环"""
        frame_interval = 1.0 / self.fps
        t = 0.0

        while self.is_running:
            loop_start = time.time()
            t += frame_interval

            # 1. 消费唇形开合目标
            try:
                queued_generation, queued_session, mouth_open = self.mouth_open_queue.get_nowait()
                if self._generation_is_current(queued_generation, queued_session):
                    self.target_mouth_open = mouth_open
                    self.is_speaking = True
                else:
                    self.target_mouth_open = 0.0
            except queue.Empty:
                self.target_mouth_open = 0.0
                if self.current_mouth_open < 0.05:
                    self.is_speaking = False

            # 平滑过渡插值
            self.current_mouth_open += (self.target_mouth_open - self.current_mouth_open) * 0.45

            # 2. 生成当前合成视频帧 (呼吸扰动 + 泊松眨眼 + 口型形变 + 防封杀运镜光影)
            render_started = time.time()
            frame_rgb = self._synthesize_frame(t, self.current_mouth_open)
            # 记录单帧基础渲染耗时用于音画同步补偿 (规划 §15.1)
            global_av_sync.record_render_latency((time.time() - render_started) * 1000.0)

            # 3/4. 先合成一次发布画层，再分发到虚拟摄像头与 JPEG 预览。
            self._publish_frame(frame_rgb)

            self.current_frame_id += 1
            self.total_frames_rendered += 1

            # 精确控制 25 FPS 时间步长
            elapsed = time.time() - loop_start
            time.sleep(max(0.001, frame_interval - elapsed))

    def _publish_frame(self, frame_rgb: "np.ndarray") -> None:
        """Compose once, then fan the same publish frame out to camera and JPEG."""
        publish_frame = compose_scene_overlays(frame_rgb, global_scene_overlay_state.snapshot())
        if global_virtual_cam.is_active:
            global_virtual_cam.send_frame(publish_frame)
        ok, buf = cv2.imencode(
            ".jpg",
            cv2.cvtColor(publish_frame, cv2.COLOR_RGB2BGR),
            [cv2.IMWRITE_JPEG_QUALITY, 80],
        )
        if ok:
            self.latest_jpeg_frame = buf.tobytes()

    def _synthesize_frame(self, t: float, mouth_open: float) -> "np.ndarray":
        """合成单帧 (委托共享渲染器：呼吸/泊松眨眼/口型/运镜/光影/真人动作切片)"""
        if self.base_portrait is None:
            return np.zeros((self.height, self.width, 3), dtype=np.uint8)
        from server.core.media.procedural_renderer import synth_frame
        return synth_frame(
            self.base_portrait, self.width, self.height, t, mouth_open,
            face_box=self.face_box, action_clip=self.action_clip,
            is_blinking=self.micro_expr.is_blinking(t),
        )

    def get_latest_jpeg(self) -> bytes:
        """获取最新的 JPEG 视频帧供前端推流"""
        return self.latest_jpeg_frame

    def get_preview_status(self) -> dict:
        uptime = max(0.1, time.time() - self.start_ts) if self.is_running else 0.0
        real_fps = round(self.total_frames_rendered / uptime, 1) if (self.is_running and self.total_frames_rendered > 0) else float(self.fps)
        return {
            "fps": self.fps,
            "real_fps": real_fps,
            "is_running": self.is_running,
            "is_speaking": self.is_speaking,
            "mouth_open": round(self.current_mouth_open, 2),
            "current_frame": self.current_frame_id,
            "resolution": f"{self.width}x{self.height}",
            "cv_available": self.cv_available,
            "render_backend": self.render_backend,
            "virtual_cam": global_virtual_cam.get_status()
        }

# 全局数字人媒体单例
global_musetalk_driver = MuseTalkMediaDriver()
