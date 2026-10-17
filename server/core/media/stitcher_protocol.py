# -*- coding: utf-8 -*-
"""
无缝流缝合状态机协议与切片挂起恢复上下文 (Stitcher Protocol)
适用阶段：7x24小时无人值守直播带货之「动静分离 / 批播结合 (Batch-Stream Hybrid)」架构。

职责与契约：
1. StitcherState：四阶段核心缝合状态枚举（播放切片、打断缓动、实时驱动、平滑回切）；
2. ResumePolicy：挂起切片恢复策略（句首重播、直接顺延、整段重播）；
3. SliceSuspendContext：断点挂起上下文元数据，记录 PTS、已播偏移、剩余秒数与恢复定位；
4. StitcherStateMachine：状态合法转移守卫与事件分发，保障音画流在突发弹幕时的无缝衔接。
"""
from __future__ import annotations

import enum
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

logger = logging.getLogger("LiveAgent.StitcherProtocol")


class StitcherState(str, enum.Enum):
    """缝合状态机核心状态枚举"""
    PLAYING_BATCH_SLICE = "playing_batch_slice"    # 正在播放离线高精带货切片 (静轨/主轨)
    INTERRUPTED_HOLDING = "interrupted_holding"    # 收到弹幕打断，正在执行 4-step 缓动停顿
    STREAMING_REALTIME = "streaming_realtime"      # 实时神经驱动（动轨：回答观众弹幕提问）
    BLENDING_TRANSITION = "blending_transition"    # 实时回复结束，正在平滑过渡回待机或切片


class ResumePolicy(str, enum.Enum):
    """切片恢复策略"""
    REWIND_SENTENCE = "rewind_sentence"  # 回退到本句开头（默认，保障台词语义完整）
    DROP_CONTINUE = "drop_continue"      # 丢弃剩余内容，顺延进入下一句或下一动作切片
    REPLAY_SLICE = "replay_slice"        # 从头重新播放当前切片


def calculate_smoothstep(t: float) -> float:
    """
    S-Curve 缓动函数 (Smoothstep: 3t^2 - 2t^3)
    与 action_state_machine.py 的过渡曲线保持一致，消除硬切帧闪烁与残影。
    """
    clamped = max(0.0, min(1.0, float(t)))
    return clamped * clamped * (3.0 - 2.0 * clamped)


@dataclass
class SliceSuspendContext:
    """高精切片断点挂起与恢复上下文"""
    slice_id: str
    product_id: str
    slot_code: str
    suspended_pts_ms: float
    playback_offset_sec: float
    duration_sec: float
    remaining_sec: float
    resume_policy: ResumePolicy = ResumePolicy.REWIND_SENTENCE
    coords_path: Optional[str] = None
    loudness_lufs: float = -16.0
    bg_noise_profile: Dict[str, Any] = field(default_factory=dict)
    sentence_boundary_offsets: List[float] = field(default_factory=list)
    interrupted_at_monotonic: float = field(default_factory=time.monotonic)

    def get_resume_offset_sec(self) -> float:
        """
        根据恢复策略精确计算重入播放时的音视频偏移（秒）
        """
        if self.resume_policy == ResumePolicy.REPLAY_SLICE:
            return 0.0

        if self.resume_policy == ResumePolicy.DROP_CONTINUE:
            return self.duration_sec

        # 默认：REWIND_SENTENCE (回退到当前句句首)
        if not self.sentence_boundary_offsets:
            # 若无句子分段信息，回退最多 1.5 秒或回退到开头
            return max(0.0, self.playback_offset_sec - 1.5)

        # 寻找小于等于当前播放偏移的最大句子边界时间
        candidate = 0.0
        for boundary in sorted(self.sentence_boundary_offsets):
            if boundary <= self.playback_offset_sec:
                candidate = boundary
            else:
                break
        return float(candidate)

    def to_dict(self) -> Dict[str, Any]:
        """序列化为字典方便日志审计与控制台下发"""
        return {
            "slice_id": self.slice_id,
            "product_id": self.product_id,
            "slot_code": self.slot_code,
            "suspended_pts_ms": self.suspended_pts_ms,
            "playback_offset_sec": round(self.playback_offset_sec, 3),
            "duration_sec": round(self.duration_sec, 3),
            "remaining_sec": round(self.remaining_sec, 3),
            "resume_policy": self.resume_policy.value if isinstance(self.resume_policy, ResumePolicy) else self.resume_policy,
            "resume_offset_sec": round(self.get_resume_offset_sec(), 3),
            "loudness_lufs": self.loudness_lufs,
        }


# 合法状态迁移路径矩阵
_VALID_TRANSITIONS: Dict[StitcherState, Set[StitcherState]] = {
    StitcherState.PLAYING_BATCH_SLICE: {
        StitcherState.PLAYING_BATCH_SLICE,    # 切片自然轮播换切片
        StitcherState.INTERRUPTED_HOLDING,    # 弹幕插播打断
        StitcherState.BLENDING_TRANSITION,    # 无缝切出
    },
    StitcherState.INTERRUPTED_HOLDING: {
        StitcherState.STREAMING_REALTIME,     # 缓动完成，进入实时回复
        StitcherState.PLAYING_BATCH_SLICE,    # 插播被撤销/放弃，回滚继续播放
    },
    StitcherState.STREAMING_REALTIME: {
        StitcherState.BLENDING_TRANSITION,    # 实时回复完毕，开始平滑回切
        StitcherState.STREAMING_REALTIME,     # 连续多条弹幕追加追问
    },
    StitcherState.BLENDING_TRANSITION: {
        StitcherState.PLAYING_BATCH_SLICE,    # 平滑过渡完毕，恢复带货切片
        StitcherState.INTERRUPTED_HOLDING,    # 过渡中再次被高优弹幕打断
        StitcherState.STREAMING_REALTIME,     # 紧急追加实时回复
    },
}


class StitcherStateMachine:
    """
    缝合状态机协议控制器
    用于保障直播间在「静轨高精商品切片」与「动轨实时互动驱动」之间安全稳定切换。
    """

    def __init__(
        self,
        initial_state: StitcherState = StitcherState.PLAYING_BATCH_SLICE,
        blend_total_steps: int = 4,
    ) -> None:
        self._current_state = initial_state
        self._blend_total_steps = max(1, blend_total_steps)
        self._blend_current_step = 0
        self._active_suspend_context: Optional[SliceSuspendContext] = None
        self._listeners: List[Callable[[StitcherState, StitcherState, Optional[Dict[str, Any]]], None]] = []

    @property
    def state(self) -> StitcherState:
        return self._current_state

    @property
    def blend_total_steps(self) -> int:
        return self._blend_total_steps

    @property
    def blend_current_step(self) -> int:
        return self._blend_current_step

    @property
    def active_suspend_context(self) -> Optional[SliceSuspendContext]:
        return self._active_suspend_context

    def add_listener(
        self,
        callback: Callable[[StitcherState, StitcherState, Optional[Dict[str, Any]]], None],
    ) -> None:
        """注册状态变迁监听器"""
        if callback not in self._listeners:
            self._listeners.append(callback)

    def transition_to(
        self,
        target_state: StitcherState,
        payload: Optional[Dict[str, Any]] = None,
    ) -> bool:
        """
        状态受控跃迁
        :param target_state: 目标状态
        :param payload: 携带的上下文数据
        :return: 是否成功转移
        """
        allowed = _VALID_TRANSITIONS.get(self._current_state, set())
        if target_state not in allowed:
            logger.warning(
                f"[StitcherStateMachine] 非法状态跃迁被拒绝: "
                f"{self._current_state.value} -> {target_state.value}"
            )
            return False

        old_state = self._current_state
        self._current_state = target_state

        if target_state == StitcherState.INTERRUPTED_HOLDING or target_state == StitcherState.BLENDING_TRANSITION:
            self._blend_current_step = 0

        # 分发回调
        for listener in list(self._listeners):
            try:
                listener(old_state, target_state, payload)
            except Exception as e:
                logger.error(f"[StitcherStateMachine] 监听器执行异常: {e}", exc_info=True)

        return True

    def trigger_interrupt(self, suspend_context: SliceSuspendContext) -> bool:
        """
        触发弹幕打断并留存当前切片挂起现场
        """
        self._active_suspend_context = suspend_context
        success = self.transition_to(
            StitcherState.INTERRUPTED_HOLDING,
            payload=suspend_context.to_dict(),
        )
        if not success:
            self._active_suspend_context = None
        return success

    def step_holding_blend(self) -> Tuple[bool, float]:
        """
        推进打断停顿阶段的 4 步缓动
        :return: (is_holding_done, current_alpha)
        """
        if self._current_state != StitcherState.INTERRUPTED_HOLDING:
            return True, 1.0

        self._blend_current_step += 1
        ratio = min(1.0, float(self._blend_current_step) / float(self._blend_total_steps))
        alpha = calculate_smoothstep(ratio)

        is_done = self._blend_current_step >= self._blend_total_steps
        if is_done:
            self.transition_to(StitcherState.STREAMING_REALTIME)
        return is_done, alpha

    def finish_realtime_speech(self) -> bool:
        """
        实时回复话音结束，开始向待机/切片执行过渡平滑回切
        """
        return self.transition_to(StitcherState.BLENDING_TRANSITION)

    def step_transition_blend(self) -> Tuple[bool, float]:
        """
        推进回切阶段的 4 步缓动
        :return: (is_transition_done, current_alpha)
        """
        if self._current_state != StitcherState.BLENDING_TRANSITION:
            return True, 1.0

        self._blend_current_step += 1
        ratio = min(1.0, float(self._blend_current_step) / float(self._blend_total_steps))
        alpha = calculate_smoothstep(ratio)

        is_done = self._blend_current_step >= self._blend_total_steps
        if is_done:
            self.transition_to(StitcherState.PLAYING_BATCH_SLICE)
        return is_done, alpha

    def pop_suspend_context(self) -> Optional[SliceSuspendContext]:
        """
        取出并清空挂起上下文（用于重入播放切片）
        """
        ctx = self._active_suspend_context
        self._active_suspend_context = None
        return ctx
