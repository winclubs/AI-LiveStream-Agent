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
from server.core.media.audio_frame import validate_audio_frame_batch
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

logger = logging.getLogger("LiveAgent.ProceduralAvatar")

class ProceduralAvatarDriver(BaseMediaDriver):
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

        # 音频能量与形态队列 (驱动 Viseme 唇形开合与形态: mouth_open, mouth_form)；渲染在独立线程
        self.mouth_open_queue = queue.Queue()
        self.current_mouth_open = 0.0
        self.target_mouth_open = 0.0
        self.current_mouth_form = 0.0
        self.target_mouth_form = 0.0
        self._generation_lock = threading.Lock()
        self._accepted_audio_generation = 0
        self._accepted_session_generation = 0

        # 按音频提交顺序保存句子；后句不能覆盖仍在播放的前句。
        self._sentence_lock = threading.Lock()
        self._active_sentences: list[dict] = []
        self._active_sentence: Optional[dict] = None  # 旧诊断/测试兼容视图（队首）

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

        try:
            from server.core.media.real_avatar_lite import global_real_avatar_lite
            global_real_avatar_lite.reload_source(path, landmarks_path=self.landmarks_cache)
        except Exception as e:
            logger.warning(f"同步真人微动态底模失败: {e}")

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

    def get_media_capabilities(self) -> dict:
        """声明程序化渲染器可消费标准 PCM 帧批次。"""
        from server.core.media.audio_frame import MediaCapabilities

        return MediaCapabilities(
            accepts_audio_frames=True,
            tts_output_codec="pcm_s16le",
            chunk_semantics="transactional_sentence",
            transactional_sentence=True,
            supports_cancel=True,
            supports_shared_clock=True,
            extra={"frame_batch_mode": "coalesced_transaction"},
        ).to_dict()

    async def feed_audio_frames(self, frames) -> None:
        """消费标准帧批次，并复用现有整句 G2P/播放 cursor 对齐算法。"""
        frame_list = validate_audio_frame_batch(frames)
        first = frame_list[0]
        await self.feed_audio_chunk(
            b"".join(frame.data for frame in frame_list),
            first.text,
            codec=first.format.codec,
            sample_rate=first.format.sample_rate,
            channels=first.format.channels,
            audio_generation=first.audio_generation,
            session_generation=first.session_generation,
            audio_id=first.audio_id,
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
        audio_id: Optional[str] = None,
    ):
        """按真实采样时长生成带双代际标签与 audio_id 播放时钟绑定的 25fps 口型目标。"""
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
            duration_sec = len(samples) / float(decoded_sample_rate)

            # 优先采用文本 G2P 音素序列与时长对齐 + 0.65/0.35 协同发音平滑
            viseme_timeline = []
            if text_snippet and text_snippet.strip():
                try:
                    from server.core.media.g2p_viseme import G2PVisemeTimeline
                    g2p = G2PVisemeTimeline(fps=float(self.fps), smooth_alpha=0.35)
                    viseme_timeline = g2p.generate_timeline(text_snippet, duration_sec)
                except Exception as g2p_err:
                    logger.debug("G2P 时间线生成异常，降级声学估计: %s", g2p_err)

            from server.core.media.procedural_renderer import audio_samples_to_viseme
            frame_idx = 0
            viseme_list = []
            for start in range(0, len(samples), samples_per_frame):
                if not self._generation_is_current(audio_generation, session_generation):
                    return
                frame_samples = samples[start:start + samples_per_frame]
                if len(frame_samples) == 0:
                    continue

                if viseme_timeline and frame_idx < len(viseme_timeline):
                    m_open, m_form = viseme_timeline[frame_idx]
                else:
                    m_open, m_form = audio_samples_to_viseme(frame_samples)

                viseme_list.append((m_open, m_form))
                # audio_id 模式完全由共享播放头驱动，禁止重复进入兼容队列造成二次回放。
                if not audio_id:
                    self.mouth_open_queue.put((
                        audio_generation,
                        session_generation,
                        m_open,
                        m_form,
                    ))
                frame_idx += 1

            if self._generation_is_current(audio_generation, session_generation) and audio_id:
                sentence = {
                    "audio_id": audio_id,
                    "audio_generation": audio_generation,
                    "session_generation": session_generation,
                    "visemes": viseme_list,
                }
                with self._sentence_lock:
                    self._active_sentences.append(sentence)
                    self._active_sentence = self._active_sentences[0]
        except Exception as exc:
            if self._generation_is_current(audio_generation, session_generation):
                logger.warning("口型音频解码失败，使用单帧保守口型: %s", exc)
                if audio_id:
                    # cursor 会继续提供音频终态；不把失败帧塞入兼容队列，避免句后幽灵回放。
                    with self._sentence_lock:
                        sentence = {
                            "audio_id": audio_id,
                            "audio_generation": audio_generation,
                            "session_generation": session_generation,
                            "visemes": [(0.3, 0.0)],
                        }
                        self._active_sentences.append(sentence)
                        self._active_sentence = self._active_sentences[0]
                else:
                    self.mouth_open_queue.put((audio_generation, session_generation, 0.3, 0.0))

        # 解码可能在线程池运行；渲染线程可能在此期间观察到空队列并清除状态。
        # 在提交完成点重新确认“已接受待播音频”，下一渲染 tick 再按真实队列/时钟收敛。
        if self._generation_is_current(audio_generation, session_generation):
            self.is_speaking = True

    async def interrupt(self, reason: str = "Barge-in", next_generation: Optional[int] = None):
        """推进口型代际并清空排队帧与句子缓存，旧 producer 恢复后也无法重新入队。"""
        with self._generation_lock:
            if next_generation is not None:
                self._accepted_audio_generation = max(
                    self._accepted_audio_generation, int(next_generation)
                )
        with self._sentence_lock:
            self._active_sentences.clear()
            self._active_sentence = None
        self.is_speaking = False
        self.target_mouth_open = 0.0
        self.target_mouth_form = 0.0
        while True:
            try:
                self.mouth_open_queue.get_nowait()
            except queue.Empty:
                break
        logger.info(f"数字人驱动收到打断信号 [{reason}]，口型立即平滑归位")

    def _render_thread_loop(self):
        """核心视频驱动主循环 (独立线程)：精确维持 25 FPS (40ms)，绝不阻塞事件循环"""
        frame_interval = 1.0 / self.fps
        t = 0.0

        while self.is_running:
            loop_start = time.time()
            t += frame_interval

            # 1. audio_id 句子严格按提交顺序绑定共享绝对播放头；兼容队列仅处理无 audio_id。
            viseme_target = None
            sent = None
            while True:
                with self._sentence_lock:
                    sent = self._active_sentences[0] if self._active_sentences else None
                    self._active_sentence = sent
                if sent is None:
                    break
                if not self._generation_is_current(
                    sent.get("audio_generation"), sent.get("session_generation")
                ):
                    terminal = True
                    clock = None
                else:
                    from server.core.media.virtual_audio import global_virtual_audio
                    clock = global_virtual_audio.get_playback_clock(sent["audio_id"])
                    reject_reason = clock.get("reject_reason") if clock else None
                    browser_fallback = bool(
                        clock
                        and reject_reason in {
                            "service_disabled",
                            "audio_unavailable",
                            "queue_full",
                            "stream_unavailable",
                            "decode_empty",
                            "playback_error",
                        }
                    )
                    if browser_fallback and sent.get("fallback_started_at") is None:
                        inherited_elapsed = max(0.0, float(clock.get("elapsed_sec", 0.0) or 0.0))
                        sent["fallback_started_at"] = time.monotonic() - inherited_elapsed
                    terminal = bool(
                        clock
                        and (
                            clock.get("is_interrupted") and not browser_fallback
                            or clock.get("is_rejected") and not browser_fallback
                            or clock.get("is_finished")
                        )
                    )
                    # cursor 理论上已由 live.prepare 建立；缺失时清理而不是擅自按 feed_time 抢跑。
                    terminal = terminal or clock is None
                if terminal:
                    with self._sentence_lock:
                        if self._active_sentences and self._active_sentences[0] is sent:
                            self._active_sentences.pop(0)
                        self._active_sentence = self._active_sentences[0] if self._active_sentences else None
                    continue
                fallback_started_at = sent.get("fallback_started_at")
                if fallback_started_at is not None:
                    elapsed = max(0.0, time.monotonic() - float(fallback_started_at))
                    frame_index = int(elapsed * self.fps)
                    visemes = sent.get("visemes", [])
                    if frame_index >= len(visemes):
                        with self._sentence_lock:
                            if self._active_sentences and self._active_sentences[0] is sent:
                                self._active_sentences.pop(0)
                            self._active_sentence = self._active_sentences[0] if self._active_sentences else None
                        continue
                    viseme_target = visemes[frame_index]
                elif not clock.get("has_started"):
                    viseme_target = (0.0, 0.0)
                else:
                    elapsed = max(0.0, float(clock.get("elapsed_sec", 0.0) or 0.0))
                    frame_index = int(elapsed * self.fps)
                    visemes = sent.get("visemes", [])
                    viseme_target = visemes[frame_index] if frame_index < len(visemes) else (0.0, 0.0)
                break

            # 共享时钟句子存在时绝不消费兼容 queue；终态清理后也不会二次回放。
            if viseme_target is not None:
                m_open, m_form = viseme_target
                self.target_mouth_open = m_open
                self.target_mouth_form = m_form
                self.is_speaking = (m_open > 0.05)
            else:
                try:
                    queued_data = self.mouth_open_queue.get_nowait()
                    if len(queued_data) >= 4:
                        queued_generation, queued_session, mouth_open, mouth_form = queued_data[:4]
                    else:
                        queued_generation, queued_session, mouth_open = queued_data[:3]
                        mouth_form = 0.0

                    if self._generation_is_current(queued_generation, queued_session):
                        self.target_mouth_open = mouth_open
                        self.target_mouth_form = mouth_form
                        self.is_speaking = True
                    else:
                        self.target_mouth_open = 0.0
                        self.target_mouth_form = 0.0
                except queue.Empty:
                    self.target_mouth_open = 0.0
                    self.target_mouth_form = 0.0
                    if self.current_mouth_open < 0.05:
                        self.is_speaking = False

            # 直接采用发音目标，消除双重低通 EMA 引起的动态压缩与 40~80ms 相位迟滞；仅静音回落时轻量平滑
            if self.target_mouth_open > 0.0 or abs(self.target_mouth_form) > 0.0:
                self.current_mouth_open = self.target_mouth_open
                self.current_mouth_form = self.target_mouth_form
            else:
                self.current_mouth_open += (0.0 - self.current_mouth_open) * 0.5
                self.current_mouth_form += (0.0 - self.current_mouth_form) * 0.5

            # 2. 生成当前合成视频帧 (呼吸扰动 + 泊松眨眼 + Viseme 口型 + 防封杀运镜光影)
            render_started = time.time()
            frame_rgb = self._synthesize_frame(t, self.current_mouth_open, self.current_mouth_form)
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
            # 单参数调用保持第三方/测试替身兼容；VirtualCameraService 默认赋予最低优先级。
            global_virtual_cam.send_frame(publish_frame)
        try:
            from server.core.media.rtmp_streamer import global_rtmp_streamer
            if global_rtmp_streamer.is_streaming:
                global_rtmp_streamer.send_video_frame(publish_frame)
        except Exception:
            pass
        ok, buf = cv2.imencode(
            ".jpg",
            cv2.cvtColor(publish_frame, cv2.COLOR_RGB2BGR),
            [cv2.IMWRITE_JPEG_QUALITY, 80],
        )
        if ok:
            self.latest_jpeg_frame = buf.tobytes()

    def _synthesize_frame(self, t: float, mouth_open: float, mouth_form: float = 0.0) -> "np.ndarray":
        """合成单帧 (真人微动态底池羽化融合 / 共享程序化兜底渲染)"""
        if self.base_portrait is None:
            return np.zeros((self.height, self.width, 3), dtype=np.uint8)

        # 优先接入低配真人微动态与下唇自适应羽化融合引擎
        if self.avatar_source_path and _os.path.exists(self.avatar_source_path):
            try:
                from server.core.media.real_avatar_lite import global_real_avatar_lite
                real_frame = global_real_avatar_lite.render_frame(
                    t,
                    mouth_open=mouth_open,
                    mouth_form=mouth_form,
                    is_blinking=self.micro_expr.is_blinking(t),
                )
                if real_frame is not None:
                    return real_frame
            except Exception:
                pass

        from server.core.media.procedural_renderer import synth_frame
        return synth_frame(
            self.base_portrait, self.width, self.height, t, mouth_open,
            face_box=self.face_box, action_clip=self.action_clip,
            is_blinking=self.micro_expr.is_blinking(t),
            mouth_form=mouth_form,
        )

    def get_latest_jpeg(self) -> bytes:
        """获取最新的 JPEG 视频帧供前端推流"""
        return self.latest_jpeg_frame

    def get_capabilities(self) -> dict:
        """机器可读能力清单上报 (ADR-16 / 规划 §4.2 如实声明契约，绝不虚报)"""
        from server.core.media.virtual_audio import global_virtual_audio
        audio_status = global_virtual_audio.get_status()
        return {
            "driver": "procedural_avatar",
            "capabilities": {
                "neural_lipsync": False,
                "viseme_lipsync": True,
                "g2p_aligned": False,  # 启发式均分非严格音素强制对齐
                "alignment_mode": "heuristic_uniform",
                "phoneme_source": "pypinyin_or_builtin",
                "forced_alignment": False,
                "shared_playback_clock": bool(audio_status.get("shared_playback_clock")),
                "hardware_dac_clock": bool(audio_status.get("hardware_dac_clock")),
                "clock_source": audio_status.get("clock_source"),
                "clock_precision": audio_status.get("clock_precision", "none"),
                "expressions": True,
                "head_motion": True,
                "remote_rendering": False,
            }
        }

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
            "virtual_cam": global_virtual_cam.get_status(),
            "capabilities": self.get_capabilities()["capabilities"],
        }

# 全局数字人媒体单例与规范命名
global_procedural_avatar_driver = ProceduralAvatarDriver()
global_musetalk_driver = global_procedural_avatar_driver

# 别名兼容定义：保持旧代码与测试导入兼容
MuseTalkMediaDriver = ProceduralAvatarDriver
Viseme2DMediaDriver = ProceduralAvatarDriver


class Live2DDriver(BaseMediaDriver):
    """Live2D 驱动标准接口（预留后续里程碑接入 Cubism SDK，目前未实现）"""
    def __init__(self, model_path: str = ""):
        super().__init__()
        self.model_path = model_path
        self.is_running = False

    def get_capabilities(self) -> dict:
        return {
            "driver": "live2d_avatar",
            "available": False,
            "experimental": True,
            "status": "unimplemented",
            "capabilities": {
                "neural_lipsync": False,
                "viseme_lipsync": False,
                "g2p_aligned": False,
                "alignment_mode": "none",
                "shared_playback_clock": False,
                "hardware_dac_clock": False,
                "clock_source": None,
                "clock_precision": "none",
                "expressions": False,
                "head_motion": False,
                "remote_rendering": False,
                "cubism_sdk": False,
            }
        }

    async def feed_audio_chunk(self, audio_bytes: bytes, text_snippet: str = "", **kwargs):
        raise NotImplementedError("Live2D 驱动尚未接入 Cubism SDK，请使用程序化数字人驱动")

    async def interrupt(self, reason: str = "Barge-in"):
        pass

    async def start(self):
        raise NotImplementedError("Live2D 驱动尚未接入运行时，无法启动")

    async def stop(self):
        self.is_running = False


class NeuralLipSyncDriver(BaseMediaDriver):
    """NeuralLipSync 神经口型驱动标准接口（预留独立 GPU 节点 UNet 权重推理，目前未实现）"""
    def __init__(self, node_url: str = ""):
        super().__init__()
        self.node_url = node_url
        self.is_running = False

    def get_capabilities(self) -> dict:
        return {
            "driver": "neural_lipsync_avatar",
            "available": False,
            "experimental": True,
            "status": "unimplemented",
            "capabilities": {
                "neural_lipsync": False,
                "viseme_lipsync": False,
                "g2p_aligned": False,
                "alignment_mode": "none",
                "shared_playback_clock": False,
                "hardware_dac_clock": False,
                "clock_source": None,
                "clock_precision": "none",
                "expressions": False,
                "head_motion": False,
                "remote_rendering": False,
            }
        }

    async def feed_audio_chunk(self, audio_bytes: bytes, text_snippet: str = "", **kwargs):
        raise NotImplementedError("NeuralLipSync 独立神经口型节点尚未接入，请使用程序化数字人驱动")

    async def interrupt(self, reason: str = "Barge-in"):
        pass

    async def start(self):
        raise NotImplementedError("NeuralLipSync 节点尚未就绪，无法启动")

    async def stop(self):
        self.is_running = False
