# -*- coding: utf-8 -*-
"""
原生自包含数字人神经驱动器 (NativeNeuralAvatarDriver)
实现目标：
1. 彻底摆脱对外部独立系统 (如 D:\\LiveTalking) 或外部进程 HTTP 接口的依赖；
2. 将数字人音视频对齐、Mel 频谱分析、面部神经重绘与动作状态机原生融合到本项目进程中；
3. 当项目内部 data/models/ 存在深度学习模型权重 (Wav2Lip / ONNX) 时，直接在进程内 GPU/CPU 推理；
4. 当模型权重尚未下载时，自动平滑无感降级至 RealAvatarLite 高保真微动态引擎，绝不中断直播；
5. 严格遵守全双工打断 (flush_talk)、时间线对齐与四路推流扇出规范。
"""
import asyncio
import logging
import math
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import cv2
import numpy as np

from server.core.avatar.base_driver import BaseAvatarDriver
from server.core.avatar.neural_model_manager import global_neural_model_manager
from server.core.avatar.registry import register_avatar_driver

logger = logging.getLogger("LiveAgent.NativeNeuralDriver")


def _resolve_active_anchor_asset_dir() -> Optional[Path]:
    """查询数据库中当前激活主播的数字人切片资产目录 (无绑定记录时返回 None)"""
    try:
        import asyncio
        from server.database.db import AsyncSessionLocal
        from server.database.models import Anchor
        from sqlalchemy import select

        async def _query() -> Optional[Path]:
            async with AsyncSessionLocal() as db:
                res = await db.execute(
                    select(Anchor.avatar_asset_dir)
                    .where(Anchor.avatar_asset_dir.isnot(None))
                    .order_by(Anchor.created_at.desc())
                    .limit(1)
                )
                row = res.first()
                if not row or not row[0]:
                    return None
                p = Path(row[0])
                return p if (p / "coords.pkl").exists() and (p / "face_imgs").exists() else None

        return asyncio.run(_query())
    except Exception:
        return None


@register_avatar_driver("native_neural")
class NativeNeuralAvatarDriver(BaseAvatarDriver):
    """
    原生自包含神经数字人驱动器
    - 项目内部一等公民，自包含音画闭环；
    - 当 data/models/ 中存在神经权重时走原生网络推理；
    - 模型未就绪时平滑走内置 RealAvatarLite 真人微动态引擎；
    - 绝无对外部 D:\\LiveTalking 等路径的寄生式依赖。
    """

    def __init__(self, config: Optional[Dict[str, Any]] = None):
        super().__init__(config)
        self.fps = self.config.get("fps", 25)
        self.width = self.config.get("width", 720)
        self.height = self.config.get("height", 960)
        self.active_model_key = self.config.get("model_key", "onnx_lipsync")

        self._audio_queue: "asyncio.Queue[bytes]" = asyncio.Queue(maxsize=1000)
        self._loop_task: Optional[asyncio.Task] = None
        self._current_energy = 0.0
        self._current_viseme = 0.0
        self.total_frames_synthesized = 0

        # 复用全局动作状态机单例，确保弹幕礼物事件与话术关键词驱动同一画面状态机
        from server.core.avatar.action_state_machine import get_action_state_machine
        self.action_state_machine = get_action_state_machine()

        # 检测自包含模型就绪状态
        self.is_neural_ready = global_neural_model_manager.is_model_available(self.active_model_key)
        self.active_model_path = global_neural_model_manager.find_model_path(self.active_model_key)

        # 真实神经唇形重绘引擎 (与主 ProceduralAvatarDriver 同款的 Wav2Lip ONNX 契约)
        self.lip_renderer: Optional[Any] = None
        try:
            from server.core.avatar.neural_lip_renderer import NeuralLipRenderer
            self.lip_renderer = NeuralLipRenderer(model_key=self.active_model_key)
        except Exception as e:
            logger.debug(f"神经唇形渲染引擎加载失败，保留 RealAvatarLite 回退: {e}")

        # 最近一帧的 16kHz float32 音频切片，供神经 Mel 特征提取使用
        self._latest_pcm_16k: Optional["np.ndarray"] = None

    async def start(self) -> bool:
        self.is_active = True
        self.bind_default_outputs()

        # 重新探查本地内部模型
        self.is_neural_ready = global_neural_model_manager.is_model_available(self.active_model_key)
        self.active_model_path = global_neural_model_manager.find_model_path(self.active_model_key)

        # 自动挂载当前激活主播的切片资产 (coords.pkl + face_imgs)，权重到位即激活真实推理
        if self.lip_renderer is not None and not getattr(self.lip_renderer, "has_anchor_assets", False):
            try:
                asset_dir = await asyncio.to_thread(_resolve_active_anchor_asset_dir)
                if asset_dir:
                    await asyncio.to_thread(self.lip_renderer.load_anchor_assets, asset_dir)
            except Exception as e:
                logger.debug(f"自动挂载主播切片资产跳过: {e}")

        if self._loop_task is None or self._loop_task.done():
            self._loop_task = asyncio.create_task(self._main_render_loop())

        neural_active = bool(
            self.lip_renderer is not None
            and getattr(self.lip_renderer, "is_ready", False)
            and getattr(self.lip_renderer, "has_anchor_assets", False)
        )
        engine_desc = "⚡ 真实深度神经网络驱动 (Wav2Lip ONNX 唇形重绘)" if neural_active else "🌿 内置高保真 RealAvatarLite (平滑回退)"
        logger.info(
            f"原生自包含数字人驱动器已启动 (引擎模式: {engine_desc}, 分辨率: {self.width}x{self.height}@{self.fps}FPS)"
        )
        return True

    async def stop(self) -> None:
        self.is_active = False
        self._speaking = False
        if self._loop_task and not self._loop_task.done():
            self._loop_task.cancel()
            self._loop_task = None
        # 清空音频缓冲
        while not self._audio_queue.empty():
            try:
                self._audio_queue.get_nowait()
                self._audio_queue.task_done()
            except Exception:
                break
        logger.info("原生自包含数字人驱动器已平稳停止")

    async def push_audio_chunk(self, pcm_bytes: bytes, eventpoint: Optional[Dict[str, Any]] = None) -> bool:
        if not self.is_active or not pcm_bytes:
            return False

        # 偶数长度切片防御 (s16le 每采样 2 字节)
        if len(pcm_bytes) % 2 != 0:
            pcm_bytes = pcm_bytes[: len(pcm_bytes) - (len(pcm_bytes) % 2)]
            if not pcm_bytes:
                return False

        self._speaking = True
        self._last_speech_time = time.time()

        # 1. 计算时域 RMS 能量与粗粒度开合度
        samples = np.frombuffer(pcm_bytes, dtype=np.int16)
        if len(samples) > 0:
            energy = float(np.mean(np.abs(samples))) / 32768.0
            # 引入非线性平滑放大函数
            self._current_energy = min(1.0, math.sqrt(energy) * 2.8)
            # 保留 16kHz float32 切片供神经唇形 Mel 特征提取
            self._latest_pcm_16k = samples.astype(np.float32) / 32768.0

        # 2. 话术关键词双轨研判动作状态机 (复用全局单例，弹幕事件可驱动同一画面)
        if eventpoint and eventpoint.get("text"):
            self.action_state_machine.evaluate_text(str(eventpoint["text"]))

        # 3. 推入音频队列用于维持讲话时钟与驱动开合
        try:
            self._audio_queue.put_nowait(pcm_bytes)
            return True
        except asyncio.QueueFull:
            return False

    async def flush_talk(self) -> None:
        """极速打断回路：清空发声队列，嘴部立即闭合并复位动作状态机"""
        self._speaking = False
        self._current_energy = 0.0
        self._last_speech_time = 0.0
        self.action_state_machine.reset_to_idle()
        while not self._audio_queue.empty():
            try:
                self._audio_queue.get_nowait()
                self._audio_queue.task_done()
            except Exception:
                break
        logger.info("原生自包含数字人已执行瞬间打断 (flush_talk)")

    def _render_frame(self, frame_idx: int) -> np.ndarray:
        """单帧画面原生合成管线 (动作切片 → 神经唇形重绘 → 真人微动态 → 兜底底图)"""
        mouth_open = self._current_energy if self._speaking else 0.0
        # 语音停顿超时平滑归零
        if time.time() - self._last_speech_time > 0.25:
            self._speaking = False
            mouth_open = 0.0

        # 1. 优先从 (全局) 动作状态机获取当前动作切片帧
        base_frame = self.action_state_machine.get_frame(frame_idx)

        # 2. 神经唇形重绘：模型与主播切片资产均就绪时执行真实 ONNX 前向推理
        if (
            self.lip_renderer is not None
            and getattr(self.lip_renderer, "is_ready", False)
            and getattr(self.lip_renderer, "has_anchor_assets", False)
        ):
            pcm_window = self._latest_pcm_16k if self._speaking else None
            try:
                neural_frame = self.lip_renderer.render_lip_frame(
                    base_frame if base_frame is not None else np.zeros((self.height, self.width, 3), dtype=np.uint8),
                    frame_idx,
                    pcm_window,
                    mouth_open=mouth_open,
                )
                if neural_frame is not None:
                    base_frame = neural_frame
            except Exception as e:
                logger.debug(f"神经唇形重绘异常，平滑降级: {e}")

        # 3. 待机或神经未就绪：调用 RealAvatarLite 渲染真人微动态底模
        if base_frame is None:
            from server.core.media.real_avatar_lite import global_real_avatar_lite
            t = frame_idx / float(max(1, self.fps))
            rendered = global_real_avatar_lite.render_frame(
                t=t,
                mouth_open=mouth_open,
                mouth_form=0.0,
                is_blinking=(frame_idx % 80 in (78, 79)),
            )
            if rendered is not None:
                base_frame = rendered
            else:
                # 优雅兜底：若均未加载则构建暖色调拟真人待机底板 (严禁全黑或极暗无光画面)
                base_frame = np.full((self.height, self.width, 3), (180, 160, 140), dtype=np.uint8)

        if base_frame.shape[0] != self.height or base_frame.shape[1] != self.width:
            base_frame = cv2.resize(base_frame, (self.width, self.height), interpolation=cv2.INTER_LINEAR)

        self.total_frames_synthesized += 1
        return base_frame

    async def _main_render_loop(self):
        """恒定帧率推流驱动循环 (严禁盲目重复向 RTMP 灌入伴音)"""
        frame_idx = 0
        interval = 1.0 / max(1, self.fps)

        while self.is_active:
            t0 = time.time()
            try:
                # 尝试消费一小包音频更新能量开合度
                if not self._audio_queue.empty():
                    _pcm_chunk = self._audio_queue.get_nowait()
                    self._audio_queue.task_done()
                else:
                    self._current_energy = max(0.0, self._current_energy * 0.75)

                frame = self._render_frame(frame_idx)
                # 仅分发视频帧；音频严格由 virtual_audio 统一播放时钟分发至 RTMP，彻底避免音轨双倍速与杂音
                self.publish_frame(frame)
                frame_idx += 1
            except Exception as e:
                logger.debug(f"原生驱动渲染帧循环异常: {e}")

            elapsed = time.time() - t0
            sleep_time = max(0.002, interval - elapsed)
            await asyncio.sleep(sleep_time)

    def get_capabilities(self) -> Dict[str, Any]:
        """如实上报当前自包含驱动器的硬件与渲染能力 (恪守 ADR-16 架构诚实契约)"""
        neural_lip_active = bool(
            self.lip_renderer is not None
            and getattr(self.lip_renderer, "is_ready", False)
            and getattr(self.lip_renderer, "has_anchor_assets", False)
        )
        return {
            "driver": "native_neural_driver",
            "self_contained": True,
            # 恪守 ADR-16：只有 ONNX 会话与主播切片资产同时就绪才宣称神经推理
            "neural_lipsync": neural_lip_active,
            "engine_type": "wav2lip_onnx" if neural_lip_active else "real_avatar_lite",
            "model_key": self.active_model_key,
            "model_installed": bool(self.is_neural_ready),
            "model_path": str(self.active_model_path) if self.active_model_path else "",
            "anchor_assets_loaded": bool(getattr(self.lip_renderer, "has_anchor_assets", False)) if self.lip_renderer else False,
            "resolution": f"{self.width}x{self.height}",
            "fps": self.fps,
            "external_dependencies": False,  # 绝无外部系统依赖
        }

    def get_status(self) -> Dict[str, Any]:
        base = super().get_status()
        neural_lip_active = bool(
            self.lip_renderer is not None
            and getattr(self.lip_renderer, "is_ready", False)
            and getattr(self.lip_renderer, "has_anchor_assets", False)
        )
        base.update({
            "self_contained": True,
            "neural_lipsync": neural_lip_active,
            "is_neural_ready": neural_lip_active,
            "engine_type": "wav2lip_onnx" if neural_lip_active else "real_avatar_lite",
            "active_model_key": self.active_model_key,
            "total_frames_synthesized": self.total_frames_synthesized,
            "current_energy": round(self._current_energy, 3),
        })
        return base
