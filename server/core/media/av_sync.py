"""
音画同步补偿控制器 (规划 §15.1 PTS 驱动补偿)
- 采集 TTS 合成耗时与数字人单帧渲染耗时
- 采集共享单调时钟的音画漂移 (SharedPlaybackClock)，漂移驱动补偿优先于纯耗时估计
- 计算音频前置延迟补偿量 (audio FIFO delay)，使声波与唇形对齐
- 通过 WebSocket AUDIO_CHUNK.delay_ms 下发给前端播放器执行
"""
import os
import time
import logging
from collections import deque
from typing import Optional

logger = logging.getLogger("LiveAgent.AVSync")

# 补偿量上下限 (毫秒)，避免异常抖动导致音画严重漂移
MIN_DELAY_MS = 0
MAX_DELAY_MS = 300


class AVSyncController:
    def __init__(self):
        self.base_delay_ms = int(os.getenv("LIVE_AGENT_AV_DELAY_MS", "0"))
        self.render_latency_ms = 0.0
        self.tts_latency_ms = 0.0
        self.recommended_delay_ms = self.base_delay_ms
        self._render_samples = deque(maxlen=20)
        # 共享时钟是否已提供有效音频锚点；未锚定前沿用纯耗时估计 (平滑过渡)
        self._drift_locked = False

    def record_render_latency(self, ms: float):
        if ms is None or ms < 0:
            return
        self._render_samples.append(float(ms))
        # 平滑平均，抑制单帧抖动
        avg = sum(self._render_samples) / len(self._render_samples)
        self.render_latency_ms = round(avg, 2)
        self._refresh_recommendation()

    def record_tts_latency(self, ms: float):
        self.tts_latency_ms = round(float(ms or 0), 2)

    def apply_drift(self, drift_ms: Optional[float], anchored: bool = True) -> None:
        """
        应用共享单调时钟计算出的音画漂移 (毫秒)。
        - anchored=False 表示音频锚点尚未建立，此时解锁漂移通道并沿用耗时估计。
        - 一旦锚定，补偿量以漂移为准 (耗时估计仅作基线保留)。
        """
        if not anchored:
            self._drift_locked = False
            self._refresh_recommendation()
            return
        if drift_ms is None:
            return
        self._drift_locked = True
        self.recommended_delay_ms = int(
            max(MIN_DELAY_MS, min(MAX_DELAY_MS, self.base_delay_ms + abs(round(float(drift_ms)))))
        )

    def _refresh_recommendation(self) -> None:
        """未锁定漂移通道时，沿用渲染耗时均值的软件近似补偿"""
        if self._drift_locked:
            return
        avg = self.render_latency_ms
        self.recommended_delay_ms = int(max(MIN_DELAY_MS, min(MAX_DELAY_MS, self.base_delay_ms + avg)))

    def measure(self, started_at: float) -> float:
        """返回自 started_at 以来的毫秒耗时并记录为 TTS 延迟"""
        latency = (time.time() - started_at) * 1000.0
        self.record_tts_latency(latency)
        return latency

    def reset(self):
        """重置所有延迟采样与内部状态 (用于测试隔离与场次重启)"""
        self.render_latency_ms = 0.0
        self.tts_latency_ms = 0.0
        self.recommended_delay_ms = self.base_delay_ms
        self._render_samples.clear()
        self._drift_locked = False

    def get_status(self) -> dict:
        return {
            "recommended_delay_ms": self.recommended_delay_ms,
            "base_delay_ms": self.base_delay_ms,
            "render_latency_ms": self.render_latency_ms,
            "tts_latency_ms": self.tts_latency_ms,
            "drift_locked": self._drift_locked,
        }


global_av_sync = AVSyncController()

