# -*- coding: utf-8 -*-
"""
server/core/monitoring/media_quality_watchdog.py
媒体质量看门狗与 SLI/SLO 监控中枢 (依据《商业化运营改造方案》§7, §9 与 §12 P1)

【设计目标】
1. 监控全链路关键媒体 SLI：有效渲染 FPS、丢帧率、音画漂移 (AV Sync Drift p95/p99)、TTS 首包延时、LLM 首字延时与连续异常帧；
2. SLO 基线与分级告警：对 FPS、丢帧率、音画漂移设立 Warn / Critical 双重门限；
3. 商业安全态联动：在 Production/Pilot 档位下，严重击穿 SLO 时自动触发系统流转至 MEDIA_DEGRADED 安全态并阻断虚假输出；
4. 诊断 Runbook 映射：针对具体违规指标精准输出运维排障指引。
"""

import asyncio
import threading
import time
import logging
from collections import deque
from typing import Dict, Any, List, Optional, Callable

logger = logging.getLogger("LiveAgent.MediaQualityWatchdog")


class MediaQualityWatchdog:
    """媒体质量看门狗与 SLI/SLO 监控器"""

    # SLO 基线定义
    FPS_TARGET = 25.0
    FPS_WARN_THRESHOLD = 15.0
    FPS_CRITICAL_THRESHOLD = 10.0

    DROP_RATE_WARN_THRESHOLD = 0.10      # 10% 丢帧率告警
    DROP_RATE_CRITICAL_THRESHOLD = 0.20  # 20% 丢帧率阻断

    AV_DRIFT_WARN_MS = 200.0             # 音画同步漂移 > 200ms 告警
    AV_DRIFT_CRITICAL_MS = 400.0         # 音画同步漂移 > 400ms 阻断

    MAX_CONSECUTIVE_ANOMALOUS_FRAMES = 10  # 连续黑帧/静止帧阻断门限

    # 帧停滞看门狗：抽检只在有帧发布时运行，断流/卡死必须由独立计时器判定。
    # 25fps 下 2.0s 相当于连续丢失 50 帧，超出观众可接受的黑屏时长。
    FRAME_STALL_SECONDS = 2.0
    # 仅本地预览时观众不可见，门限放宽避免频繁误降级。
    PREVIEW_STALL_SECONDS = 6.0
    STALL_CHECK_INTERVAL_SECONDS = 0.25

    # 按运行档位分级的停滞门限；None 表示该档位不判定（DEMO 仿真无真实输出）。
    # 未列出的档位按最严的 FRAME_STALL_SECONDS 处理 (fail-closed)。
    STALL_THRESHOLD_BY_MODE = {
        "PRODUCTION": FRAME_STALL_SECONDS,
        "PILOT": FRAME_STALL_SECONDS,
        "PREVIEW": PREVIEW_STALL_SECONDS,
        "DEMO": None,
    }

    # 标准排障 Runbook 字典
    RUNBOOKS = {
        "FPS_DEGRADED": "【Runbook-M01】有效 FPS 低于安全基线：请检查 GPU 显存/CUDA 算力占用，关闭后台重度进程，或在配置文件中降低渲染分辨率梯度至 720P。",
        "DROP_FRAME_HIGH": "【Runbook-M02】渲染丢帧率过高：请排查 CPU 调度抢占与 I/O 阻塞，确认系统资源看门狗已提升进程为 HIGH_PRIORITY_CLASS。",
        "AV_DRIFT_EXCESSIVE": "【Runbook-M03】音画同步漂移严重：请核查系统声卡采样率是否固定为 16kHz/48kHz，检查虚拟声卡缓冲区是否积压，执行时钟重新校准对齐。",
        "ANOMALOUS_FRAMES": "【Runbook-M04】检测到连续黑帧或卡顿静止帧：底层神经渲染引擎可能发生卡死或模型异常，系统已进入 MEDIA_DEGRADED 安全态，严禁继续推流！",
        "TTS_LATENCY_HIGH": "【Runbook-M05】TTS 语音合成首帧延时过长：请检查网络到语音服务商的延迟，或本地 CosyVoice 显存缓存机制。",
        "LLM_LATENCY_HIGH": "【Runbook-M06】LLM 首字吐出延时过长：请检查大模型 API 响应速度或触发模型熔断备用 Key。",
        "FRAME_STALL": "【Runbook-M07】超过门限未收到任何新视频帧：请检查神经渲染 sidecar 进程与云端渲染会话是否存活、WebSocket 回传是否断开、推理队列是否堆积，必要时执行人工接管。",
    }

    def __init__(
        self,
        on_degraded_callback: Optional[Callable[[str], Any]] = None,
        frame_stall_seconds: Optional[float] = FRAME_STALL_SECONDS,
        check_interval_seconds: float = STALL_CHECK_INTERVAL_SECONDS,
        output_readiness_probe: Optional[Callable[[], bool]] = None,
    ):
        self._on_degraded_callback = on_degraded_callback
        self.frame_stall_seconds = frame_stall_seconds
        self.check_interval_seconds = float(check_interval_seconds)
        self.execution_mode = "PREVIEW"
        self._output_readiness_probe = output_readiness_probe
        self._stream_output_ready = False
        self.is_monitoring = False

        # 滑动窗口数据记录 (保留最近 100 次采样)
        self._frame_render_times = deque(maxlen=100)
        self._total_rendered_frames = 0
        self._total_dropped_frames = 0

        self._av_drifts_ms = deque(maxlen=100)
        self._tts_first_frame_latencies_ms = deque(maxlen=50)
        self._llm_first_token_latencies_ms = deque(maxlen=50)

        # 连续异常状态跟踪
        self._consecutive_black_frames = 0
        self._consecutive_frozen_frames = 0
        self._anomaly_degraded_latched = False
        self._recent_alerts: List[Dict[str, Any]] = []

        # RTMP 事件统计
        self._rtmp_reconnects = 0
        self._rtmp_disconnects = 0

        # 帧停滞监测：单调时钟基准、闩锁、计时线程与回调派发所在事件循环
        self._last_frame_monotonic: Optional[float] = None
        self._stall_reported = False
        self._stall_thread: Optional[threading.Thread] = None
        self._stall_stop = threading.Event()
        self._loop: Optional[asyncio.AbstractEventLoop] = None

    def start(self):
        """启动媒体质量监控 (含帧停滞计时器)"""
        self.is_monitoring = True
        try:
            self._loop = asyncio.get_running_loop()
        except RuntimeError:
            self._loop = None
        self._stall_reported = False
        self._last_frame_monotonic = time.monotonic()
        self._stall_stop.clear()
        if self._stall_thread is None or not self._stall_thread.is_alive():
            self._stall_thread = threading.Thread(
                target=self._stall_monitor_loop,
                name="media-quality-stall",
                daemon=True,
            )
            self._stall_thread.start()
        logger.info("媒体质量看门狗 (MediaQualityWatchdog) 已启动")

    def stop(self):
        """停止媒体质量监控 (含帧停滞计时器)"""
        self.is_monitoring = False
        self._stall_stop.set()
        thread = self._stall_thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=1.0)
        self._stall_thread = None
        self._loop = None
        logger.info("媒体质量看门狗 (MediaQualityWatchdog) 已停止")

    def reset(self):
        """重置所有监控统计指标"""
        self._frame_render_times.clear()
        self._total_rendered_frames = 0
        self._total_dropped_frames = 0
        self._av_drifts_ms.clear()
        self._tts_first_frame_latencies_ms.clear()
        self._llm_first_token_latencies_ms.clear()
        self._consecutive_black_frames = 0
        self._consecutive_frozen_frames = 0
        self._anomaly_degraded_latched = False
        self._recent_alerts.clear()
        self._rtmp_reconnects = 0
        self._rtmp_disconnects = 0
        self._last_frame_monotonic = None
        self._stall_reported = False

    # ------------------------------------------------------------------
    # 帧停滞检测
    # ------------------------------------------------------------------
    def set_execution_mode(self, execution_mode: str, stall_seconds: Optional[float] = None) -> None:
        """按运行档位套用停滞门限策略；未列出的档位按最严门限处理 (fail-closed)。"""
        mode = (execution_mode or "PREVIEW").upper()
        self.execution_mode = mode
        self.frame_stall_seconds = self.STALL_THRESHOLD_BY_MODE.get(mode, self.FRAME_STALL_SECONDS) \
            if stall_seconds is None else stall_seconds

    def set_output_readiness_probe(self, probe: Optional[Callable[[], bool]]) -> None:
        """挂载推流输出就绪探针 (RTMP / 虚拟摄像头 / WebRTC / 录制)。"""
        self._output_readiness_probe = probe

    def set_stream_output_ready(self, ready: bool) -> None:
        """声明推流输出是否就绪；就绪瞬间重新起算帧龄，避免开播竞态误判。"""
        ready = bool(ready)
        self._stream_output_ready = ready
        self._stall_reported = False
        if ready:
            self._last_frame_monotonic = time.monotonic()

    @property
    def is_stall_policy_enabled(self) -> bool:
        """当前档位是否启用帧停滞判定。"""
        return self.frame_stall_seconds is not None

    def is_stream_output_ready(self) -> bool:
        """推流输出是否就绪：优先采信外部探针，探针异常时按未就绪处理。"""
        probe = self._output_readiness_probe
        if probe is None:
            return self._stream_output_ready
        try:
            return bool(probe())
        except Exception:
            logger.exception("推流输出就绪探针异常，按未就绪处理")
            return False

    def frame_age_seconds(self, now: Optional[float] = None) -> Optional[float]:
        """距最后一帧的时间间隔 (秒)；尚未开始计时时返回 None。"""
        if self._last_frame_monotonic is None:
            return None
        current = time.monotonic() if now is None else now
        return max(0.0, current - self._last_frame_monotonic)

    def _stall_monitor_loop(self):
        """计时线程：周期性判定帧停滞 (抽检在无帧时无法运行)"""
        while not self._stall_stop.wait(self.check_interval_seconds):
            try:
                self.check_frame_stall()
            except Exception:  # 计时线程必须绝不退出
                logger.exception("帧停滞检测异常")

    def check_frame_stall(self, now: Optional[float] = None) -> bool:
        """判定帧停滞；触发降级返回 True。

        前置条件：档位策略启用且推流输出已就绪。无真实输出时判定没有商业意义，
        不得触发 MEDIA_DEGRADED。同一段停滞只触发一次，帧恢复后重新武装。
        """
        if not self.is_monitoring or self._last_frame_monotonic is None:
            return False
        if not self.is_stall_policy_enabled:
            return False
        if not self.is_stream_output_ready():
            return False

        age = self.frame_age_seconds(now)
        if age is None or age < float(self.frame_stall_seconds):
            # 帧龄回到门限内即视为恢复，解除闩锁
            self._stall_reported = False
            return False

        if self._stall_reported:
            return False

        self._stall_reported = True
        reason = (
            f"视频帧停滞：已 {age:.1f}s 未收到任何新帧"
            f" (门限 {self.frame_stall_seconds}s，档位 {self.execution_mode})，渲染链路可能已断流或卡死"
        )
        self._record_alert("CRITICAL", "FRAME_STALL", reason)
        self._trigger_degradation(reason)
        return True

    def record_frame_rendered(self, render_cost_ms: float = 40.0):
        """记录一次成功渲染的帧

        注意：本事件逐帧发生，而黑帧/冻结抽检按步长采样 (见 FramePublishBus)。
        若在此清零连续异常计数，异常计数将永远无法跨越抽检步长累计到阈值，
        生产环境的黑屏虚假输出将无法触发降级 (fail-open)。
        连续异常的判定与闩锁解除只由 record_frame_anomaly 的健康样本负责。
        """
        now = time.time()
        self._frame_render_times.append(now)
        self._total_rendered_frames += 1
        self._last_frame_monotonic = time.monotonic()

    def record_frame_dropped(self, count: int = 1):
        """记录丢失的帧数"""
        self._total_dropped_frames += count

    def record_av_drift(self, drift_ms: float):
        """记录一次音画同步漂移绝对值 (毫秒)"""
        self._av_drifts_ms.append(abs(float(drift_ms)))

    def record_tts_latency(self, first_frame_ms: float):
        """记录 TTS 首包延时 (毫秒)"""
        self._tts_first_frame_latencies_ms.append(float(first_frame_ms))

    def record_llm_latency(self, first_token_ms: float):
        """记录大模型首字返回延时 (毫秒)"""
        self._llm_first_token_latencies_ms.append(float(first_token_ms))

    def record_frame_anomaly(self, is_black: bool = False, is_frozen: bool = False):
        """记录一帧异常画面 (黑帧或静止卡顿帧)。

        降级回调采用**边沿闩锁**：一次持续异常只触发一次 MEDIA_DEGRADED，
        直到出现健康帧才重新武装。否则长时间黑屏会让回调与 CRITICAL 告警
        按帧重复刷屏 (实测 30 次连续黑帧可触发 21 次降级回调与广播)，
        在 7x24 无人值守场景中淹没真实告警。
        """
        if is_black:
            self._consecutive_black_frames += 1
        else:
            self._consecutive_black_frames = 0

        if is_frozen:
            self._consecutive_frozen_frames += 1
        else:
            self._consecutive_frozen_frames = 0

        worst = max(self._consecutive_black_frames, self._consecutive_frozen_frames)

        if not is_black and not is_frozen:
            # 健康帧：解除闩锁，允许下一次异常重新触发
            self._anomaly_degraded_latched = False
            return

        if worst >= self.MAX_CONSECUTIVE_ANOMALOUS_FRAMES and not self._anomaly_degraded_latched:
            self._anomaly_degraded_latched = True
            reason = f"检测到连续 {worst} 帧黑屏/画面冻结卡死"
            self._record_alert("CRITICAL", "ANOMALOUS_FRAMES", reason)
            self._trigger_degradation(reason)

    def record_rtmp_event(self, event_type: str, details: Optional[Dict[str, Any]] = None):
        """记录 RTMP 推流状态变更"""
        if event_type == "reconnect":
            self._rtmp_reconnects += 1
        elif event_type == "disconnect":
            self._rtmp_disconnects += 1
            self._record_alert("WARN", "RTMP_DISCONNECT", f"RTMP 连接断开 (累计 {self._rtmp_disconnects} 次)")

    def calculate_effective_fps(self) -> float:
        """根据最近渲染帧时间戳计算当前有效 FPS"""
        if len(self._frame_render_times) < 2:
            return self.FPS_TARGET if self._total_rendered_frames > 0 else 0.0
        time_span = self._frame_render_times[-1] - self._frame_render_times[0]
        if time_span <= 0:
            return self.FPS_TARGET
        return round(float(len(self._frame_render_times) - 1) / time_span, 2)

    def calculate_drop_rate(self) -> float:
        """计算累计渲染丢帧率"""
        total = self._total_rendered_frames + self._total_dropped_frames
        if total == 0:
            return 0.0
        return round(self._total_dropped_frames / total, 4)

    def calculate_percentile(self, values: deque, percentile: float) -> float:
        """计算指定分位数值 (如 p95, p99)"""
        if not values:
            return 0.0
        sorted_vals = sorted(list(values))
        k = (len(sorted_vals) - 1) * (percentile / 100.0)
        f = int(k)
        c = f + 1
        if c < len(sorted_vals):
            d = k - f
            return round(sorted_vals[f] * (1 - d) + sorted_vals[c] * d, 2)
        return round(float(sorted_vals[f]), 2)

    def _record_alert(self, level: str, code: str, message: str):
        """记录一次告警，并保留排障 Runbook"""
        runbook = self.RUNBOOKS.get(code, "请检查直播链路网络与系统状态。")
        alert = {
            "timestamp": time.time(),
            "level": level,
            "code": code,
            "message": message,
            "runbook": runbook,
        }
        self._recent_alerts.append(alert)
        if len(self._recent_alerts) > 50:
            self._recent_alerts.pop(0)

        if level == "CRITICAL":
            logger.critical("🚨【媒体质量看门狗 CRITICAL】[%s] %s | Runbook: %s", code, message, runbook)
        else:
            logger.warning("⚠️【媒体质量看门狗 WARN】[%s] %s | Runbook: %s", code, message, runbook)

    def _trigger_degradation(self, reason: str):
        """触发系统安全态降级回调

        计时器线程与渲染线程都没有事件循环，回调内部使用 asyncio.create_task 时
        必须先派发回 start() 捕获的事件循环，否则事件会被静默丢弃。
        """
        callback = self._on_degraded_callback
        if callback is None:
            return

        loop = self._loop
        if loop is not None:
            try:
                loop.call_soon_threadsafe(callback, reason)
                return
            except RuntimeError:
                logger.error("事件循环已关闭，降级回调无法派发: %s", reason)
                return

        try:
            callback(reason)
            # 回调可能自行创建异步任务；此处不保存无用返回值。
        except Exception as e:
            logger.error("触发质量降级回调异常: %s", e)

    def evaluate_slo(self) -> Dict[str, Any]:
        """执行全方位 SLO 合规评估并返回状态与违规项"""
        effective_fps = self.calculate_effective_fps()
        drop_rate = self.calculate_drop_rate()
        av_p95 = self.calculate_percentile(self._av_drifts_ms, 95)
        av_p99 = self.calculate_percentile(self._av_drifts_ms, 99)
        tts_p95 = self.calculate_percentile(self._tts_first_frame_latencies_ms, 95)
        llm_p95 = self.calculate_percentile(self._llm_first_token_latencies_ms, 95)

        violations = []
        status = "HEALTHY"

        # 1. 评估 FPS
        if effective_fps < self.FPS_CRITICAL_THRESHOLD and self._total_rendered_frames > 20:
            status = "CRITICAL"
            violations.append({
                "code": "FPS_DEGRADED",
                "severity": "CRITICAL",
                "current": effective_fps,
                "threshold": self.FPS_CRITICAL_THRESHOLD,
                "msg": f"有效 FPS 为 {effective_fps}，严重击穿临界基线 ({self.FPS_CRITICAL_THRESHOLD})",
                "runbook": self.RUNBOOKS["FPS_DEGRADED"]
            })
            self._record_alert("CRITICAL", "FPS_DEGRADED", f"FPS 严重跌至 {effective_fps}")
            self._trigger_degradation(f"有效 FPS 严重不足 ({effective_fps} < {self.FPS_CRITICAL_THRESHOLD})")
        elif effective_fps < self.FPS_WARN_THRESHOLD and self._total_rendered_frames > 20:
            if status != "CRITICAL":
                status = "WARNING"
            violations.append({
                "code": "FPS_DEGRADED",
                "severity": "WARNING",
                "current": effective_fps,
                "threshold": self.FPS_WARN_THRESHOLD,
                "msg": f"有效 FPS 为 {effective_fps}，低于警戒基线 ({self.FPS_WARN_THRESHOLD})",
                "runbook": self.RUNBOOKS["FPS_DEGRADED"]
            })

        # 2. 评估丢帧率
        if drop_rate >= self.DROP_RATE_CRITICAL_THRESHOLD:
            status = "CRITICAL"
            violations.append({
                "code": "DROP_FRAME_HIGH",
                "severity": "CRITICAL",
                "current": drop_rate,
                "threshold": self.DROP_RATE_CRITICAL_THRESHOLD,
                "msg": f"丢帧率达到 {drop_rate * 100:.1f}%，超过阻断门限 ({self.DROP_RATE_CRITICAL_THRESHOLD * 100:.1f}%)",
                "runbook": self.RUNBOOKS["DROP_FRAME_HIGH"]
            })
            self._record_alert("CRITICAL", "DROP_FRAME_HIGH", f"丢帧率达到 {drop_rate * 100:.1f}%")
            self._trigger_degradation(f"丢帧率严重超标 ({drop_rate * 100:.1f}%)")
        elif drop_rate >= self.DROP_RATE_WARN_THRESHOLD:
            if status != "CRITICAL":
                status = "WARNING"
            violations.append({
                "code": "DROP_FRAME_HIGH",
                "severity": "WARNING",
                "current": drop_rate,
                "threshold": self.DROP_RATE_WARN_THRESHOLD,
                "msg": f"丢帧率达到 {drop_rate * 100:.1f}%，超过预警门限 ({self.DROP_RATE_WARN_THRESHOLD * 100:.1f}%)",
                "runbook": self.RUNBOOKS["DROP_FRAME_HIGH"]
            })

        # 3. 评估音画漂移
        if av_p95 >= self.AV_DRIFT_CRITICAL_MS:
            status = "CRITICAL"
            violations.append({
                "code": "AV_DRIFT_EXCESSIVE",
                "severity": "CRITICAL",
                "current": av_p95,
                "threshold": self.AV_DRIFT_CRITICAL_MS,
                "msg": f"音画漂移 p95 达到 {av_p95}ms，严重击穿同步极限 ({self.AV_DRIFT_CRITICAL_MS}ms)",
                "runbook": self.RUNBOOKS["AV_DRIFT_EXCESSIVE"]
            })
            self._record_alert("CRITICAL", "AV_DRIFT_EXCESSIVE", f"音画漂移 p95 达到 {av_p95}ms")
            self._trigger_degradation(f"音画同步严重漂移 (p95={av_p95}ms)")
        elif av_p95 >= self.AV_DRIFT_WARN_MS:
            if status != "CRITICAL":
                status = "WARNING"
            violations.append({
                "code": "AV_DRIFT_EXCESSIVE",
                "severity": "WARNING",
                "current": av_p95,
                "threshold": self.AV_DRIFT_WARN_MS,
                "msg": f"音画漂移 p95 达到 {av_p95}ms，超过预警基线 ({self.AV_DRIFT_WARN_MS}ms)",
                "runbook": self.RUNBOOKS["AV_DRIFT_EXCESSIVE"]
            })

        # 4. 连续黑帧/静止
        if max(self._consecutive_black_frames, self._consecutive_frozen_frames) >= self.MAX_CONSECUTIVE_ANOMALOUS_FRAMES:
            status = "CRITICAL"
            violations.append({
                "code": "ANOMALOUS_FRAMES",
                "severity": "CRITICAL",
                "current": max(self._consecutive_black_frames, self._consecutive_frozen_frames),
                "threshold": self.MAX_CONSECUTIVE_ANOMALOUS_FRAMES,
                "msg": f"检测到连续异常帧数达到 {max(self._consecutive_black_frames, self._consecutive_frozen_frames)}",
                "runbook": self.RUNBOOKS["ANOMALOUS_FRAMES"]
            })

        # 5. 帧停滞 (断流/卡死)：抽检在无帧时无法运行，由计时器独立判定；
        # 仅在档位策略启用且推流输出就绪时才是违规项。
        frame_age = self.frame_age_seconds()
        frame_age_ms = round(frame_age * 1000.0, 2) if frame_age is not None else None
        stall_active = self.is_stall_policy_enabled and self.is_stream_output_ready()
        if stall_active and frame_age is not None and frame_age >= float(self.frame_stall_seconds):
            status = "CRITICAL"
            violations.append({
                "code": "FRAME_STALL",
                "severity": "CRITICAL",
                "current": round(frame_age, 2),
                "threshold": self.frame_stall_seconds,
                "msg": (
                    f"已 {frame_age:.1f}s 未收到任何新视频帧"
                    f" (门限 {self.frame_stall_seconds}s，档位 {self.execution_mode})"
                ),
                "runbook": self.RUNBOOKS["FRAME_STALL"]
            })

        return {
            "status": status,
            "effective_fps": effective_fps,
            "drop_frame_rate": drop_rate,
            "av_drift_p95_ms": av_p95,
            "av_drift_p99_ms": av_p99,
            "tts_first_frame_p95_ms": tts_p95,
            "llm_first_token_p95_ms": llm_p95,
            "consecutive_black_frames": self._consecutive_black_frames,
            "consecutive_frozen_frames": self._consecutive_frozen_frames,
            "frame_age_ms": frame_age_ms,
            "stall_policy_enabled": stall_active,
            "stream_output_ready": self.is_stream_output_ready(),
            "violations": violations,
            "timestamp": time.time(),
        }

    def get_sli_snapshot(self) -> Dict[str, Any]:
        """获取结构化质量监控看板与诊断快照"""
        eval_result = self.evaluate_slo()
        return {
            "is_monitoring": self.is_monitoring,
            "sli_metrics": {
                "effective_fps": eval_result["effective_fps"],
                "target_fps": self.FPS_TARGET,
                "drop_frame_rate": eval_result["drop_frame_rate"],
                "total_rendered_frames": self._total_rendered_frames,
                "total_dropped_frames": self._total_dropped_frames,
                "av_sync_drift_p95_ms": eval_result["av_drift_p95_ms"],
                "av_sync_drift_p99_ms": eval_result["av_drift_p99_ms"],
                "tts_first_frame_p95_ms": eval_result["tts_first_frame_p95_ms"],
                "llm_first_token_p95_ms": eval_result["llm_first_token_p95_ms"],
                "frame_age_ms": eval_result["frame_age_ms"],
                "frame_stall_seconds": self.frame_stall_seconds,
                "stall_policy_enabled": eval_result["stall_policy_enabled"],
                "stream_output_ready": eval_result["stream_output_ready"],
                "rtmp_reconnect_count": self._rtmp_reconnects,
                "rtmp_disconnect_count": self._rtmp_disconnects,
            },
            "slo_evaluation": {
                "overall_status": eval_result["status"],
                "violations_count": len(eval_result["violations"]),
                "violations": eval_result["violations"],
            },
            "recent_alerts": list(self._recent_alerts)[-10:],
        }


# 全局单例
global_media_watchdog = MediaQualityWatchdog()
