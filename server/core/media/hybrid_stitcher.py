# -*- coding: utf-8 -*-
"""
无缝流缝合调度器与断点续播引擎 (HybridStitcher)
适用阶段：7x24小时无人值守直播带货「动静分离 / 批播结合 (Batch-Stream Hybrid)」架构。

核心职责：
1. 双轨热切换：离线高精商品切片轨 (静轨) 与 弹幕实时驱动答复轨 (动轨) 零感知缝合；
2. 4-step Smoothstep S-Curve 视频空间 Alpha 混合，彻底消除断点跳切撕裂与面部瞬移；
3. 音频轨 50ms 线性交叉淡入淡出 (Cross-fade)，消除截断爆音 (Clicks & Pops)；
4. 弹幕插播时留存 SliceSuspendContext，并在答复完毕后执行句首回退重入与 SharedPlaybackClock PTS 锚点校准。
"""
from __future__ import annotations

import collections
import logging
import threading
import random
from pathlib import Path
from typing import Any, Callable, Deque, Dict, List, Optional

import cv2
import numpy as np

from server.core.avatar.sku_slice_renderer import apply_audio_varispeed
from server.core.avatar.transition_filler import (
    TransitionFillerManager,
    global_transition_filler_manager,
)
from server.core.media.frame_bus import FramePublishBus
from server.core.media.shared_playback_clock import SharedPlaybackClock
from server.core.media.stitcher_protocol import (
    ResumePolicy,
    SliceSuspendContext,
    StitcherState,
    StitcherStateMachine,
)

logger = logging.getLogger("LiveAgent.HybridStitcher")

CROSSFADE_DURATION_MS = 50.0
CROSSFADE_SAMPLES_16K = int(16000 * CROSSFADE_DURATION_MS / 1000.0)  # 800 采样 @ 16kHz
DEFAULT_FPS = 25


def apply_audio_crossfade(
    outgoing_pcm: np.ndarray,
    incoming_pcm: np.ndarray,
    crossfade_samples: int = CROSSFADE_SAMPLES_16K,
) -> np.ndarray:
    """
    对两段音频进行线性交叉淡入淡出 (Cross-fade) 拼接，消除硬切截断爆音
    :param outgoing_pcm: 淡出音频 (尾部被淡出)
    :param incoming_pcm: 淡入音频 (头部被淡入)
    :param crossfade_samples: 交叉渐变样本数 (默认 50ms = 800 点 @ 16kHz)
    :return: 缝合后的音频 PCM 数组
    """
    if outgoing_pcm is None or outgoing_pcm.size == 0:
        return incoming_pcm if incoming_pcm is not None else np.zeros(0, dtype=np.float32)
    if incoming_pcm is None or incoming_pcm.size == 0:
        return outgoing_pcm

    n_fade = min(crossfade_samples, len(outgoing_pcm), len(incoming_pcm))
    if n_fade <= 0:
        return np.concatenate([outgoing_pcm, incoming_pcm])

    # 1. 提取尾部与头部渐变区
    out_body = outgoing_pcm[:-n_fade]
    out_tail = outgoing_pcm[-n_fade:].astype(np.float32)

    in_head = incoming_pcm[:n_fade].astype(np.float32)
    in_body = incoming_pcm[n_fade:]

    # 2. 线性权重
    t = np.linspace(0.0, 1.0, n_fade, endpoint=True, dtype=np.float32)
    faded_transition = out_tail * (1.0 - t) + in_head * t

    return np.concatenate([out_body, faded_transition, in_body]).astype(np.float32)


class HybridSliceHandle:
    """单个在播或排队的商品切片内存与磁盘句柄"""

    def __init__(
        self,
        slice_id: str,
        product_id: str,
        slot_code: str,
        video_path: str,
        audio_path: str,
        duration_sec: float,
        sentence_boundaries: Optional[List[float]] = None,
        loudness_lufs: float = -16.0,
        noise_profile: Optional[Dict[str, Any]] = None,
        preloaded_frames: Optional[List[np.ndarray]] = None,
    ) -> None:
        self.slice_id = slice_id
        self.product_id = product_id
        self.slot_code = slot_code
        self.video_path = Path(video_path)
        self.audio_path = Path(audio_path)
        self.duration_sec = max(0.1, float(duration_sec))
        self.total_frames = int(round(self.duration_sec * DEFAULT_FPS))
        self.sentence_boundaries = sentence_boundaries or [0.0]
        self.loudness_lufs = loudness_lufs
        self.noise_profile = noise_profile or {}
        # 阶段五防查：每轮播动态微变速比率 (初始随机抽取)
        self.current_loop_speed_ratio = float(random.uniform(0.985, 1.015))
        self._raw_audio_cache: Optional[bytes] = None

        # 视频帧缓存 (懒加载/预载)
        if preloaded_frames:
            self._frames_cache: List[np.ndarray] = list(preloaded_frames)
            self.total_frames = len(self._frames_cache)
            self.duration_sec = round(len(self._frames_cache) / float(DEFAULT_FPS), 3)
            self._is_loaded = True
        else:
            self._frames_cache = []
            self._is_loaded = False

    def refresh_varispeed_for_new_loop(self) -> float:
        """
        每次切片重新进入下一轮轮播时调用：动态刷新微变速比率 (±1.5%)
        彻底打破 7x24 小时长时间直播中固定音频指纹周期重复！
        """
        self.current_loop_speed_ratio = float(random.uniform(0.985, 1.015))
        logger.debug(
            f"[HybridSliceHandle] 切片 {self.slice_id} 新一轮播放微变速比率: "
            f"{self.current_loop_speed_ratio:.4f}"
        )
        return self.current_loop_speed_ratio

    def _read_pure_pcm16_bytes(self) -> bytes:
        """从 audio_path 读取纯净 int16 单声道 PCM 字节；若为 WAV 文件则自动剥离文件头，杜绝杂音"""
        if self._raw_audio_cache is None and self.audio_path.exists():
            try:
                raw_data = self.audio_path.read_bytes()
                if raw_data.startswith(b"RIFF"):
                    import io
                    import wave
                    try:
                        with wave.open(io.BytesIO(raw_data), "rb") as wf:
                            self._raw_audio_cache = wf.readframes(wf.getnframes())
                    except Exception:
                        # 降级：剥离 44 字节标准 WAV 头部
                        self._raw_audio_cache = raw_data[44:] if len(raw_data) > 44 else raw_data
                else:
                    self._raw_audio_cache = raw_data
            except Exception as e:
                logger.warning(f"读取切片音频失败: {self.audio_path}, {e}")
                return b""
        return self._raw_audio_cache or b""

    def get_audio_pcm(
        self,
        dynamic_varispeed: bool = True,
        start_frame: int = 0,
    ) -> bytes:
        """
        读取切片音频 PCM 字节流；支持每轮播动态微变速 (±1.5%) 与指定起始帧偏移。
        自动解包 WAV 容器头，确保返回纯净 16-bit 单声道 16000Hz PCM 字节，杜绝爆音。
        """
        raw = self._read_pure_pcm16_bytes()
        if not raw:
            return b""

        if dynamic_varispeed and abs(self.current_loop_speed_ratio - 1.0) > 1e-4:
            stretched = apply_audio_varispeed(
                raw,
                sample_rate=16000,
                speed_ratio=self.current_loop_speed_ratio,
            )
        else:
            stretched = raw

        # 若指定了起始帧偏移 (如打断后断点回切重入)，截取对应时间偏移后的 PCM 采样
        if start_frame > 0 and self.total_frames > 0:
            frame_ratio = min(1.0, max(0.0, float(start_frame) / float(self.total_frames)))
            # 每个采样 2 字节 (int16 mono)
            total_samples = len(stretched) // 2
            offset_samples = int(total_samples * frame_ratio)
            byte_offset = offset_samples * 2
            return stretched[byte_offset:]

        return stretched

    def ensure_loaded(self, max_frames: int = 1500) -> None:
        """从 MP4 解复用读取视频帧"""
        if self._is_loaded:
            return
        if not self.video_path.exists():
            logger.warning(f"[HybridSliceHandle] 切片视频文件不存在: {self.video_path}")
            self._is_loaded = True
            return

        cap = cv2.VideoCapture(str(self.video_path))
        frames = []
        try:
            while cap.isOpened() and len(frames) < max_frames:
                ret, frame = cap.read()
                if not ret or frame is None:
                    break
                frames.append(frame)
        finally:
            cap.release()

        self._frames_cache = frames
        if frames:
            self.total_frames = len(frames)
            self.duration_sec = round(len(frames) / float(DEFAULT_FPS), 3)
        self._is_loaded = True

    def release_cache(self) -> None:
        """释放已缓存的解码帧，节约 7x24 小时无人值守常驻内存"""
        self._frames_cache.clear()
        self._is_loaded = False
        self._raw_audio_cache = None

    def get_frame(self, frame_idx: int) -> Optional[np.ndarray]:
        """按帧号安全提取切片画面"""
        if not self._is_loaded:
            self.ensure_loaded()
        if not self._frames_cache:
            return None
        clamped_idx = max(0, min(frame_idx, len(self._frames_cache) - 1))
        return self._frames_cache[clamped_idx]


class HybridStitcher:
    """
    无缝流缝合调度器
    连接切片静轨与实时动轨的核心控制中枢
    """

    def __init__(
        self,
        clock: Optional[SharedPlaybackClock] = None,
        frame_bus: Optional[FramePublishBus] = None,
        blend_total_steps: int = 4,
        transition_filler_manager: Optional[TransitionFillerManager] = None,
        loop_playlist: bool = True,
    ) -> None:
        self._lock = threading.Lock()
        self.clock = clock
        self.frame_bus = frame_bus
        self.loop_playlist = bool(loop_playlist)
        self.transition_filler_manager = (
            transition_filler_manager or global_transition_filler_manager
        )
        self.on_transition_hook: Optional[
            Callable[[HybridSliceHandle, Optional[HybridSliceHandle], str], None]
        ] = None
        self.on_slice_audio_playback: Optional[
            Callable[[HybridSliceHandle, int, bool], None]
        ] = None
        self.sm = StitcherStateMachine(
            initial_state=StitcherState.PLAYING_BATCH_SLICE,
            blend_total_steps=blend_total_steps,
        )

        # 切片播放队列与播放指针
        self._playlist: Deque[HybridSliceHandle] = collections.deque()
        self._current_slice: Optional[HybridSliceHandle] = None
        self._current_frame_idx: int = 0

        # 过渡帧缓冲：用于 4 步 Smoothstep 混合
        self._last_rendered_frame: Optional[np.ndarray] = None
        self._last_realtime_frame: Optional[np.ndarray] = None

        # 实时动轨输入缓冲 (由实时引擎提交最新推理帧)
        self._latest_realtime_frame: Optional[np.ndarray] = None
        self._realtime_frame_clock: int = 0

    @property
    def state(self) -> StitcherState:
        return self.sm.state

    @property
    def current_slice(self) -> Optional[HybridSliceHandle]:
        with self._lock:
            return self._current_slice

    @property
    def current_frame_idx(self) -> int:
        with self._lock:
            return self._current_frame_idx

    def enqueue_slice(self, slice_handle: HybridSliceHandle) -> None:
        """追加高精切片到播放排期表"""
        with self._lock:
            self._playlist.append(slice_handle)
            if self._current_slice is None and self.sm.state == StitcherState.PLAYING_BATCH_SLICE:
                self._current_slice = self._playlist.popleft()
                self._current_frame_idx = 0

    def clear_playlist(self) -> None:
        """清空切片队列并释放内存"""
        with self._lock:
            for item in self._playlist:
                item.release_cache()
            self._playlist.clear()
            if self._current_slice:
                self._current_slice.release_cache()
                self._current_slice = None

    # ------------------------------------------------------------------
    # 核心帧步进生成循环 (25 FPS 驱动调用)
    # ------------------------------------------------------------------
    def tick_frame(self, base_idle_frame: Optional[np.ndarray] = None) -> Optional[np.ndarray]:
        """
        每帧调用：依据缝合状态机推进视频帧，并执行必要的 4-step 缓动
        :param base_idle_frame: 待机底图 (当切片为空或处于待机姿态时兜底)
        :return: 合成/解复用后的最终视频帧 (960x720 或底模原尺寸)
        """
        with self._lock:
            current_state = self.sm.state

            # --------------------------------------------------------------
            # 分支 1：正在播放离线高精带货切片 (静轨)
            # --------------------------------------------------------------
            if current_state == StitcherState.PLAYING_BATCH_SLICE:
                frame = self._render_batch_slice_step(base_idle_frame)
                self._last_rendered_frame = frame
                return frame

            # --------------------------------------------------------------
            # 分支 2：收到打断，正在执行 4-step 缓动停顿
            # --------------------------------------------------------------
            elif current_state == StitcherState.INTERRUPTED_HOLDING:
                done, alpha = self.sm.step_holding_blend()
                # 将打断前的尾帧 (_last_rendered_frame) 与 base_idle_frame 进行 S-Curve 缓动
                target = base_idle_frame
                frame = self._blend_frames(
                    source=self._last_rendered_frame,
                    target=target,
                    alpha=alpha,
                    fallback=base_idle_frame,
                )
                self._last_rendered_frame = frame
                return frame

            # --------------------------------------------------------------
            # 分支 3：实时神经驱动（回答观众提问）
            # --------------------------------------------------------------
            elif current_state == StitcherState.STREAMING_REALTIME:
                frame = self._latest_realtime_frame
                if frame is None:
                    frame = base_idle_frame
                self._last_realtime_frame = frame
                self._last_rendered_frame = frame
                return frame

            # --------------------------------------------------------------
            # 分支 4：实时答复结束，正在平滑过渡回切片断点
            # --------------------------------------------------------------
            elif current_state == StitcherState.BLENDING_TRANSITION:
                done, alpha = self.sm.step_transition_blend()
                # 目标是切片重入帧
                target_slice_frame = None
                if self._current_slice:
                    target_slice_frame = self._current_slice.get_frame(self._current_frame_idx)
                if target_slice_frame is None:
                    target_slice_frame = base_idle_frame

                source_frame = (
                    self._last_realtime_frame
                    if self._last_realtime_frame is not None
                    else self._last_rendered_frame
                )
                frame = self._blend_frames(
                    source=source_frame,
                    target=target_slice_frame,
                    alpha=alpha,
                    fallback=base_idle_frame,
                )
                self._last_rendered_frame = frame
                if done:
                    # 缓动完成，已自动跳回 PLAYING_BATCH_SLICE
                    logger.debug("[HybridStitcher] 回切过渡缓动完成，恢复播放带货切片")
                    if self._current_slice and self.on_slice_audio_playback:
                        try:
                            self.on_slice_audio_playback(
                                self._current_slice,
                                self._current_frame_idx,
                                False,
                            )
                        except Exception as e:
                            logger.debug(f"切片断点音频恢复回调异常: {e}")
                return frame

            return base_idle_frame

    def _render_batch_slice_step(self, base_idle_frame: Optional[np.ndarray]) -> Optional[np.ndarray]:
        """推进带货切片播放指针"""
        if self._current_slice is None:
            if self._playlist:
                self._current_slice = self._playlist.popleft()
                self._current_frame_idx = 0
            else:
                return base_idle_frame

        # 切片首帧播发时，触发音频播放回调 (向虚拟声卡投递本轮切片音频)
        if self._current_frame_idx == 0 and self.on_slice_audio_playback:
            try:
                self.on_slice_audio_playback(self._current_slice, 0, True)
            except Exception as e:
                logger.debug(f"切片音频播放回调异常: {e}")

        frame = self._current_slice.get_frame(self._current_frame_idx)
        self._current_frame_idx += 1

        # 若当前切片播放完毕，换下一切片
        if self._current_frame_idx >= self._current_slice.total_frames:
            old_slice = self._current_slice
            next_slice = self._playlist[0] if self._playlist else self._current_slice

            # 阶段五过场词决策联动 (转场拟人过渡)
            if self.transition_filler_manager and self.transition_filler_manager.should_inject():
                curr_slot = old_slice.slot_code if old_slice else ""
                next_slot = next_slice.slot_code if next_slice else ""
                filler_text = self.transition_filler_manager.pick_filler_text(curr_slot, next_slot)
                logger.info(f"[HybridStitcher] 切片转场过场词触发: {filler_text}")
                if self.on_transition_hook:
                    try:
                        self.on_transition_hook(old_slice, next_slice, filler_text)
                    except Exception as e:
                        logger.debug(f"过场词回调执行异常: {e}")

            if self._playlist:
                if self.loop_playlist and old_slice:
                    # 轮播模式：切片入队等待下一轮，并提前重置新一轮随机微变速比率
                    old_slice.refresh_varispeed_for_new_loop()
                    self._playlist.append(old_slice)
                elif old_slice:
                    old_slice.release_cache()

                self._current_slice = self._playlist.popleft()
                self._current_frame_idx = 0
            else:
                # 队列仅有当前切片：循环播放当前切片，并刷新微变速比率 (每次轮播全新时域随机微扰)
                self._current_frame_idx = 0
                if self._current_slice:
                    self._current_slice.refresh_varispeed_for_new_loop()

        return frame if frame is not None else base_idle_frame

    def _blend_frames(
        self,
        source: Optional[np.ndarray],
        target: Optional[np.ndarray],
        alpha: float,
        fallback: Optional[np.ndarray],
    ) -> Optional[np.ndarray]:
        """对两幅画面应用平滑 Alpha 权重融合 (Alpha 由 calculate_smoothstep 计算)"""
        if source is None:
            return target if target is not None else fallback
        if target is None:
            return source

        if source.shape != target.shape:
            # 尺寸不匹配时重设大小
            target_resized = cv2.resize(target, (source.shape[1], source.shape[0]))
        else:
            target_resized = target

        clamped_alpha = max(0.0, min(1.0, float(alpha)))
        return cv2.addWeighted(target_resized, clamped_alpha, source, 1.0 - clamped_alpha, 0)

    # ------------------------------------------------------------------
    # 弹幕打断与断点重入
    # ------------------------------------------------------------------
    def trigger_interrupt(
        self,
        resume_policy: ResumePolicy = ResumePolicy.REWIND_SENTENCE,
    ) -> Optional[SliceSuspendContext]:
        """
        收到观众弹幕插播时调用：留存现场并转入 4-step 打断停顿
        :return: 留存的挂起上下文
        """
        with self._lock:
            if self.sm.state != StitcherState.PLAYING_BATCH_SLICE:
                logger.info(f"[HybridStitcher] 当前状态为 {self.sm.state.value}，忽略重复打断")
                return None

            curr_slice = self._current_slice
            if not curr_slice:
                logger.info("[HybridStitcher] 无在播切片，忽略打断")
                return None

            offset_sec = float(self._current_frame_idx) / float(DEFAULT_FPS)
            remaining_sec = max(0.0, curr_slice.duration_sec - offset_sec)
            now_pts_ms = self.clock.now_ms() if self.clock else (offset_sec * 1000.0)

            suspend_ctx = SliceSuspendContext(
                slice_id=curr_slice.slice_id,
                product_id=curr_slice.product_id,
                slot_code=curr_slice.slot_code,
                suspended_pts_ms=now_pts_ms,
                playback_offset_sec=offset_sec,
                duration_sec=curr_slice.duration_sec,
                remaining_sec=remaining_sec,
                resume_policy=resume_policy,
                sentence_boundary_offsets=curr_slice.sentence_boundaries,
                loudness_lufs=curr_slice.loudness_lufs,
                bg_noise_profile=curr_slice.noise_profile,
            )

            # 驱动状态机跃迁到 INTERRUPTED_HOLDING
            success = self.sm.trigger_interrupt(suspend_ctx)
            if success:
                logger.info(
                    f"[HybridStitcher] 成功打断切片 {curr_slice.slice_id} "
                    f"@ {offset_sec:.2f}s (剩余 {remaining_sec:.2f}s)"
                )
                return suspend_ctx
            return None

    def feed_realtime_frame(self, frame: np.ndarray) -> None:
        """动轨供帧：实时神经驱动器提交新渲染的口型帧"""
        with self._lock:
            self._latest_realtime_frame = frame
            self._realtime_frame_clock += 1

    def finish_realtime_reply(self) -> bool:
        """
        实时回复话音结束时调用：开始向切片断点执行 4-step 平滑过渡并重锚定时钟
        """
        with self._lock:
            if self.sm.state != StitcherState.STREAMING_REALTIME:
                logger.warning(
                    f"[HybridStitcher] 无法结束实时回复：当前状态为 {self.sm.state.value}"
                )
                return False

            suspend_ctx = self.sm.active_suspend_context
            if suspend_ctx and self._current_slice:
                resume_offset_sec = suspend_ctx.get_resume_offset_sec()
                # 将切片帧号重新定位到重入偏移点
                self._current_frame_idx = int(round(resume_offset_sec * DEFAULT_FPS))
                logger.info(
                    f"[HybridStitcher] 切片重入定位: policy={suspend_ctx.resume_policy.value}, "
                    f"offset={resume_offset_sec:.2f}s (帧号={self._current_frame_idx})"
                )

                # 向 SharedPlaybackClock 重新校准锚点
                if self.clock:
                    resume_pts_ms = resume_offset_sec * 1000.0
                    self.clock.stamp_video(
                        frame_idx=self._current_frame_idx,
                        pts_ms=resume_pts_ms,
                    )

            # 启动过渡缓动
            return self.sm.finish_realtime_speech()
