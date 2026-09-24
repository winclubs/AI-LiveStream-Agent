# -*- coding: utf-8 -*-
"""
共享单调播放时钟与音画漂移补偿引擎 (SharedPlaybackClock)
规划 §15.1 PTS 驱动补偿的核心实现 (替代纯软件近似)：
1. 会话级单调时钟基准 (time.monotonic)，视频帧发布时打 PTS；
2. 音频播放头复用 virtual_audio.get_playback_clock() 的真实游标 (samples_played)；
3. 实时计算音画漂移 (音频头 PTS - 视频最新 PTS)，EMA 平滑去抖；
4. 输出漂移驱动的音频前置延迟补偿量与帧节奏建议 (丢帧/复帧)，供渲染循环吸收；
5. 线程安全：PTS 单调递增不回退；flush_talk 打断时重置基准。
"""
import logging
import threading
import time
from collections import deque
from typing import Any, Dict, Optional

logger = logging.getLogger("LiveAgent.SharedPlaybackClock")

# 漂移平滑系数 (EMA)：越小越稳越慢，越大越灵敏越抖
DRIFT_EMA_ALPHA = 0.2
# 补偿量上下限 (毫秒)，与 av_sync.MAX_DELAY_MS 对齐
MIN_DELAY_MS = 0
MAX_DELAY_MS = 300
# 触发帧节奏建议的漂移阈值 (毫秒)
FRAME_PACING_THRESHOLD_MS = 120.0
# 采样窗口长度
_DRIFT_WINDOW = 20


class SharedPlaybackClock:
    """会话级共享单调时钟与音画漂移补偿器"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._epoch: float = time.monotonic()
        # 视频侧 PTS (毫秒，单调递增)
        self._video_pts_ms: float = 0.0
        self._video_frame_count: int = 0
        # 音频侧播放头 (毫秒)
        self._audio_pts_ms: float = 0.0
        self._last_audio_audio_id: Optional[str] = None
        # 漂移 EMA 状态
        self._drift_ms: float = 0.0
        self._drift_samples: "deque[float]" = deque(maxlen=_DRIFT_WINDOW)
        self._recommended_delay_ms: int = 0
        # 是否已观测到有效音频锚点 (未观测前不做补偿，避免误判)
        self._audio_anchored: bool = False
        self._enabled: bool = True

    # ------------------------------------------------------------------
    # 基准与重置
    # ------------------------------------------------------------------
    def reset(self) -> None:
        """重置时钟基准 (新场次或 flush_talk 打断后调用)"""
        with self._lock:
            self._epoch = time.monotonic()
            self._video_pts_ms = 0.0
            self._video_frame_count = 0
            self._audio_pts_ms = 0.0
            self._last_audio_audio_id = None
            self._drift_ms = 0.0
            self._drift_samples.clear()
            self._recommended_delay_ms = 0
            self._audio_anchored = False

    def set_enabled(self, enabled: bool) -> None:
        """运行时开关 (关闭时退回纯软件近似语义)"""
        with self._lock:
            self._enabled = bool(enabled)

    def now_ms(self) -> float:
        """自会话基准以来的单调毫秒数"""
        return (time.monotonic() - self._epoch) * 1000.0

    # ------------------------------------------------------------------
    # PTS 打点
    # ------------------------------------------------------------------
    def stamp_video(self, frame_idx: Optional[int] = None) -> float:
        """
        视频帧发布时打 PTS。返回该帧的 PTS (毫秒)。
        PTS 严格单调递增不回退：取 max(上一帧, 时钟读数)。
        """
        with self._lock:
            if not self._enabled:
                return self._video_pts_ms
            clock_ms = self.now_ms()
            # 单调不回退保护
            if clock_ms <= self._video_pts_ms:
                clock_ms = self._video_pts_ms + 1.0
            self._video_pts_ms = clock_ms
            self._video_frame_count += 1
            return self._video_pts_ms

    def report_audio_head(self, audio_id: Optional[str], pts_ms: float) -> None:
        """
        上报音频播放头 PTS (毫秒)。通常由 virtual_audio 游标换算得到。
        播放头可能因 flush 回退，因此不做单调强制；仅更新最新观测。
        """
        if audio_id is None or pts_ms is None:
            return
        with self._lock:
            if not self._enabled:
                return
            self._audio_pts_ms = float(pts_ms)
            self._last_audio_audio_id = audio_id
            self._audio_anchored = True

    # ------------------------------------------------------------------
    # 漂移计算
    # ------------------------------------------------------------------
    def compute_drift(self) -> float:
        """
        计算当前音画漂移 (毫秒)。
        drift = 音频头PTS - 视频最新PTS；负值表示音频滞后于画面。
        未观测到音频锚点时漂移为 0 (不补偿)。
        """
        with self._lock:
            if not self._enabled or not self._audio_anchored:
                return 0.0
            raw = self._audio_pts_ms - self._video_pts_ms
            # 窗口均值去抖
            self._drift_samples.append(raw)
            avg = sum(self._drift_samples) / len(self._drift_samples)
            # EMA 平滑
            self._drift_ms = DRIFT_EMA_ALPHA * avg + (1.0 - DRIFT_EMA_ALPHA) * self._drift_ms
            return self._drift_ms

    def get_recommended_delay_ms(self) -> int:
        """漂移驱动的音频前置延迟补偿量 (钳制 0~300ms)"""
        drift = self.compute_drift()
        # 若音频滞后于画面 (drift<0)，需让音频延迟少一点/画面等一等；
        # 若音频超前 (drift>0)，需加大音频延迟对齐画面。
        delay = int(round(abs(drift)))
        return max(MIN_DELAY_MS, min(MAX_DELAY_MS, delay))

    def get_frame_pacing_hint(self) -> Optional[str]:
        """漂移超阈值时的帧节奏建议 (供渲染循环吸收)"""
        drift = self.compute_drift()
        if abs(drift) < FRAME_PACING_THRESHOLD_MS:
            return None
        # 音频滞后于画面 -> 视频侧应丢帧追赶
        if drift < 0:
            return "skip_frame"
        # 音频超前于画面 -> 视频侧应复帧等待
        return "duplicate_frame"

    # ------------------------------------------------------------------
    # 状态上报
    # ------------------------------------------------------------------
    def get_alignment_status(self) -> Dict[str, Any]:
        """供媒体能力契约与前端诊断消费的时钟实况"""
        with self._lock:
            anchored = self._audio_anchored
            drift = self._drift_ms
        return {
            "shared_playback_clock": True,
            "hardware_dac_clock": False,
            "clock_source": "shared_monotonic_pts" if anchored else "video_monotonic_only",
            "clock_precision": "sample_aligned" if anchored else "estimated",
            "alignment_mode": "shared_monotonic_pts" if anchored else "heuristic_uniform",
            "drift_ms": round(drift, 2),
            "video_pts_ms": round(self._video_pts_ms, 2),
            "audio_pts_ms": round(self._audio_pts_ms, 2),
            "audio_anchored": anchored,
            "video_frame_count": self._video_frame_count,
            "recommended_delay_ms": self.get_recommended_delay_ms(),
            "frame_pacing_hint": self.get_frame_pacing_hint(),
            "enabled": self._enabled,
        }


# 全局单例
global_shared_playback_clock = SharedPlaybackClock()
