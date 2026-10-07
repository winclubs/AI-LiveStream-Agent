import logging
import time
import psutil
from fastapi import APIRouter, Response
from server.config import APP_VERSION

router = APIRouter(tags=["系统指标与可观测性"])
logger = logging.getLogger("LiveAgent.Metrics")


def _prom_label(value: object) -> str:
    return str(value).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _safe_snapshot(component: str, getter, default: dict | None = None) -> dict:
    """隔离观测组件异常，诊断端点在故障发生时仍必须可用。"""
    try:
        value = getter()
        if not isinstance(value, dict):
            raise TypeError("status getter 未返回 dict")
        return value
    except Exception as exc:
        logger.warning("采集 %s 状态失败: %s", component, exc)
        return {
            **(default or {}),
            "available": False,
            "component_up": False,
            "stale": True,
            "error": str(exc),
        }


def _collect_live_metrics() -> dict:
    """收集核心系统指标快照"""
    from server.routes.live import global_live_controller
    from server.core.media.virtual_cam import global_virtual_cam

    is_live = 1 if global_live_controller.is_live else 0
    stats = global_live_controller.stats if hasattr(global_live_controller, "stats") else {}
    danmaku_count = stats.get("danmaku_count", 0)
    viewer_count = stats.get("viewer_count", 0)
    peak_viewers = stats.get("peak_viewer_count", 0)
    gift_count = stats.get("gift_count", 0)

    # 控制器入口指标与队列内部淘汰/过期指标保持分层。
    event_metrics = {
        key: stats.get(key, 0)
        for key in (
            "events_received_total",
            "events_accepted_total",
            "events_dropped_total",
            "danmaku_received_total",
            "danmaku_accepted_total",
            "danmaku_dropped_total",
            "gift_received_total",
            "gift_accepted_total",
            "gift_dropped_total",
        )
    }
    queue_metrics = {}
    if hasattr(global_live_controller, "event_queue") and global_live_controller.event_queue:
        queue_metrics = global_live_controller.event_queue.get_stats()
    queue_size = queue_metrics.get("queue_depth", 0)

    # 熔断器状态 (0: 未初始化/正常, 1: 熔断降级开启)
    circuit_broken = 0
    circuit_state = "CLOSED"
    if hasattr(global_live_controller, "fetcher") and global_live_controller.fetcher:
        if hasattr(global_live_controller.fetcher, "state"):
            circuit_state = global_live_controller.fetcher.state
            if circuit_state in ("OPEN", "HALF_OPEN"):
                circuit_broken = 1

    # 虚拟摄像头/渲染与帧级音频管线指标
    cam_status = _safe_snapshot(
        "virtual_cam",
        global_virtual_cam.get_status,
        {"fps": 0, "frames_sent": 0},
    ) if global_virtual_cam else {}
    fps = cam_status.get("fps", 0)
    frames_sent = cam_status.get("frames_sent", 0)
    from server.core.media.audio_pipeline import global_audio_frame_pipeline
    from server.core.media.incremental_audio import global_incremental_audio_pipeline
    from server.core.media.virtual_audio import global_virtual_audio
    from server.adapters.media.media_router import global_media_router

    audio_pipeline = _safe_snapshot("audio_pipeline", global_audio_frame_pipeline.get_status)
    incremental_audio = _safe_snapshot(
        "incremental_audio", global_incremental_audio_pipeline.get_status
    )
    virtual_audio = _safe_snapshot("virtual_audio", global_virtual_audio.get_status)
    sidecar = getattr(global_media_router, "sidecar_driver", None)
    sidecar_status = _safe_snapshot(
        "neural_sidecar", sidecar.get_preview_status
    ) if sidecar is not None else {
        "configured": False,
        "remote_connected": False,
        "remote_ready": False,
        "degraded": False,
    }
    orchestrator = getattr(global_media_router, "avatar_orchestrator", None)
    avatar_provider = _safe_snapshot(
        "avatar_provider_orchestrator", orchestrator.get_snapshot
    ) if orchestrator is not None else {
        "running": False,
        "provider_count": 0,
        "requests_total": 0,
        "outcomes": {},
        "providers": [],
        "procedural_shadow": True,
        "same_sentence_failover": False,
    }

    # 音画同步时钟与帧发布总线实况 (P2-8：drift/recommended_delay/frame_pacing 纳入监控)
    from server.core.media.shared_playback_clock import global_shared_playback_clock
    from server.core.media.frame_bus import global_frame_bus
    from server.core.media.av_sync import global_av_sync
    clock_status = _safe_snapshot("shared_playback_clock", global_shared_playback_clock.get_alignment_status)
    frame_bus_status = _safe_snapshot("frame_publish_bus", global_frame_bus.get_status)
    av_sync = _safe_snapshot("av_sync_controller", global_av_sync.get_status)

    # 进程与系统资源
    process = psutil.Process()
    mem_info = process.memory_info()
    mem_rss_mb = round(mem_info.rss / 1024 / 1024, 2)
    cpu_percent = process.cpu_percent(interval=None)

    realtime_latency = av_sync.get("realtime_latency", {}) if isinstance(av_sync, dict) else {}

    def _realtime_value(metric: str, field: str) -> float:
        item = realtime_latency.get(metric, {}) if isinstance(realtime_latency, dict) else {}
        return float(item.get(field, 0.0) or 0.0) if isinstance(item, dict) else 0.0

    return {
        "is_live": is_live,
        "session_id": getattr(global_live_controller, "session_id", None),
        "danmaku_count": danmaku_count,
        "viewer_count": viewer_count,
        "peak_viewers": peak_viewers,
        "gift_count": gift_count,
        "queue_size": queue_size,
        "queue_metrics": queue_metrics,
        **event_metrics,
        "circuit_state": circuit_state,
        "circuit_broken": circuit_broken,
        "render_fps": fps,
        "frames_sent": frames_sent,
        "audio_pipeline": audio_pipeline,
        "incremental_audio": incremental_audio,
        "neural_sidecar": sidecar_status,
        "avatar_provider": avatar_provider,
        "audio_frame_transactions_total": audio_pipeline.get("transactions_total", 0),
        "audio_frame_transactions_framed": audio_pipeline.get("transactions_framed", 0),
        "audio_frame_transactions_fallback": audio_pipeline.get("transactions_fallback", 0),
        "audio_frame_transactions_cancelled": audio_pipeline.get("transactions_cancelled", 0),
        "audio_frames_total": audio_pipeline.get("frames_total", 0),
        "audio_frame_decode_ms": audio_pipeline.get("last_decode_ms", 0.0),
        "audio_true_streaming_tts": 1 if (
            audio_pipeline.get("true_streaming_tts")
            or _realtime_value("tts_first_chunk_ms", "count") > 0
        ) else 0,
        "incremental_pcm_transactions_opened": incremental_audio.get("transactions_opened", 0),
        "incremental_pcm_transactions_committed": incremental_audio.get("transactions_committed", 0),
        "incremental_pcm_transactions_aborted": incremental_audio.get("transactions_aborted", 0),
        "incremental_pcm_active_transactions": incremental_audio.get("active_transactions", 0),
        "virtual_audio_pending_chunks": virtual_audio.get("pending_chunks", 0),
        "virtual_audio_pending_bytes": virtual_audio.get("pending_audio_bytes", 0),
        "neural_sidecar_connected": 1 if sidecar_status.get("remote_connected") else 0,
        "neural_sidecar_ready": 1 if sidecar_status.get("remote_ready") else 0,
        "neural_sidecar_degraded": 1 if sidecar_status.get("degraded") else 0,
        # P2-8: 音画同步漂移与帧发布总线仲裁状态
        "av_drift_ms": float(clock_status.get("drift_ms", 0.0) or 0.0),
        "av_recommended_delay_ms": int(clock_status.get("recommended_delay_ms", 0) or 0),
        "av_frame_pacing_hint": 1 if clock_status.get("frame_pacing_hint") else 0,
        "av_audio_anchored": 1 if clock_status.get("audio_anchored") else 0,
        "av_sync_recommended_delay_ms": int(av_sync.get("recommended_delay_ms", 0) or 0),
        "av_sync_drift_locked": 1 if av_sync.get("drift_locked") else 0,
        "realtime_latency": realtime_latency,
        "tts_first_chunk_p50_ms": _realtime_value("tts_first_chunk_ms", "p50_ms"),
        "tts_first_chunk_p95_ms": _realtime_value("tts_first_chunk_ms", "p95_ms"),
        "tts_first_chunk_p99_ms": _realtime_value("tts_first_chunk_ms", "p99_ms"),
        "audio_first_frame_p50_ms": _realtime_value("audio_first_frame_ms", "p50_ms"),
        "audio_first_frame_p95_ms": _realtime_value("audio_first_frame_ms", "p95_ms"),
        "audio_first_frame_p99_ms": _realtime_value("audio_first_frame_ms", "p99_ms"),
        "playback_start_p50_ms": _realtime_value("playback_start_ms", "p50_ms"),
        "playback_start_p95_ms": _realtime_value("playback_start_ms", "p95_ms"),
        "playback_start_p99_ms": _realtime_value("playback_start_ms", "p99_ms"),
        "frame_bus_owner": frame_bus_status.get("frame_owner", ""),
        "frame_bus_frames_published": int(frame_bus_status.get("total_published", 0) or 0),
        "frame_bus_frames_suppressed": int(frame_bus_status.get("frames_suppressed_by_owner", 0) or 0),
        "process_mem_rss_mb": mem_rss_mb,
        "process_cpu_percent": cpu_percent,
        "timestamp": time.time(),
        "version": APP_VERSION,
    }


@router.get("/metrics")
async def prometheus_metrics():
    """Prometheus 标准纯文本格式指标输出"""
    m = _collect_live_metrics()
    lines = [
        "# HELP live_agent_status 直播引擎当前运行状态 (1 为正在直播，0 为停止)",
        "# TYPE live_agent_status gauge",
        f"live_agent_status {m['is_live']}",
        "",
        "# HELP live_agent_danmaku_total 当前场次被调度队列接受的真实弹幕事件数",
        "# TYPE live_agent_danmaku_total counter",
        f"live_agent_danmaku_total {m['danmaku_count']}",
        "",
        "# HELP live_agent_events_received_total 当前场次收到的真实事件数",
        "# TYPE live_agent_events_received_total counter",
        f"live_agent_events_received_total {m['events_received_total']}",
        "",
        "# HELP live_agent_events_accepted_total 当前场次被调度队列接受的真实事件数",
        "# TYPE live_agent_events_accepted_total counter",
        f"live_agent_events_accepted_total {m['events_accepted_total']}",
        "",
        "# HELP live_agent_events_dropped_total 当前场次入队拒绝或入队驱逐的真实事件数",
        "# TYPE live_agent_events_dropped_total counter",
        f"live_agent_events_dropped_total {m['events_dropped_total']}",
        "",
        "# HELP live_agent_danmaku_received_total 当前场次收到的真实弹幕事件数",
        "# TYPE live_agent_danmaku_received_total counter",
        f"live_agent_danmaku_received_total {m['danmaku_received_total']}",
        "",
        "# HELP live_agent_danmaku_dropped_total 当前场次入队被拒或被高优事件驱逐的真实弹幕事件数",
        "# TYPE live_agent_danmaku_dropped_total counter",
        f"live_agent_danmaku_dropped_total {m['danmaku_dropped_total']}",
        "",
        "# HELP live_agent_viewers_current 当前直播间在线观众人数",
        "# TYPE live_agent_viewers_current gauge",
        f"live_agent_viewers_current {m['viewer_count']}",
        "",
        "# HELP live_agent_viewers_peak 历史峰值在线观众人数",
        "# TYPE live_agent_viewers_peak gauge",
        f"live_agent_viewers_peak {m['peak_viewers']}",
        "",
        "# HELP live_agent_queue_depth 待播报弹幕调度队列深度",
        "# TYPE live_agent_queue_depth gauge",
        f"live_agent_queue_depth {m['queue_size']}",
        "",
        "# HELP live_agent_danmaku_circuit_broken 弹幕抓取熔断降级状态 (1 熔断中, 0 正常)",
        "# TYPE live_agent_danmaku_circuit_broken gauge",
        f"live_agent_danmaku_circuit_broken {m['circuit_broken']}",
        "",
        "# HELP live_agent_render_fps 视频帧输出设定帧率",
        "# TYPE live_agent_render_fps gauge",
        f"live_agent_render_fps {m['render_fps']}",
        "",
        "# HELP live_agent_render_frames_total 累计发送视频帧数",
        "# TYPE live_agent_render_frames_total counter",
        f"live_agent_render_frames_total {m['frames_sent']}",
        "",
        "# HELP live_agent_audio_frame_transactions_total 尝试进入标准音频帧管线的整句事务数",
        "# TYPE live_agent_audio_frame_transactions_total counter",
        f"live_agent_audio_frame_transactions_total {m['audio_frame_transactions_total']}",
        "",
        "# HELP live_agent_audio_frame_transactions_framed_total 成功解码并帧化的整句事务数",
        "# TYPE live_agent_audio_frame_transactions_framed_total counter",
        f"live_agent_audio_frame_transactions_framed_total {m['audio_frame_transactions_framed']}",
        "",
        "# HELP live_agent_audio_frame_transactions_fallback_total 帧化失败并回退兼容路径的整句事务数",
        "# TYPE live_agent_audio_frame_transactions_fallback_total counter",
        f"live_agent_audio_frame_transactions_fallback_total {m['audio_frame_transactions_fallback']}",
        "",
        "# HELP live_agent_audio_frame_transactions_cancelled_total 被取消的帧化事务数",
        "# TYPE live_agent_audio_frame_transactions_cancelled_total counter",
        f"live_agent_audio_frame_transactions_cancelled_total {m['audio_frame_transactions_cancelled']}",
        "",
        "# HELP live_agent_audio_frames_total 累计生成的标准 PCM 音频帧数",
        "# TYPE live_agent_audio_frames_total counter",
        f"live_agent_audio_frames_total {m['audio_frames_total']}",
        "",
        "# HELP live_agent_audio_frame_decode_milliseconds 最近一次完整事务解码耗时",
        "# TYPE live_agent_audio_frame_decode_milliseconds gauge",
        f"live_agent_audio_frame_decode_milliseconds {m['audio_frame_decode_ms']}",
        "",
        "# HELP live_agent_audio_true_streaming_tts 当前是否实现 TTS 边生成边播放 (1 是, 0 否)",
        "# TYPE live_agent_audio_true_streaming_tts gauge",
        f"live_agent_audio_true_streaming_tts {m['audio_true_streaming_tts']}",
        "",
        "# HELP live_agent_tts_first_chunk_latency_ms TTS 首个 PCM chunk 延迟分位数 (毫秒)",
        "# TYPE live_agent_tts_first_chunk_latency_ms gauge",
        f"live_agent_tts_first_chunk_latency_p50_ms {m['tts_first_chunk_p50_ms']}",
        f"live_agent_tts_first_chunk_latency_p95_ms {m['tts_first_chunk_p95_ms']}",
        f"live_agent_tts_first_chunk_latency_p99_ms {m['tts_first_chunk_p99_ms']}",
        "# HELP live_agent_audio_first_frame_latency_ms 首个音频帧提交延迟分位数 (毫秒)",
        "# TYPE live_agent_audio_first_frame_latency_ms gauge",
        f"live_agent_audio_first_frame_latency_p50_ms {m['audio_first_frame_p50_ms']}",
        f"live_agent_audio_first_frame_latency_p95_ms {m['audio_first_frame_p95_ms']}",
        f"live_agent_audio_first_frame_latency_p99_ms {m['audio_first_frame_p99_ms']}",
        "# HELP live_agent_playback_start_latency_ms 播放线程启动延迟分位数 (毫秒)",
        "# TYPE live_agent_playback_start_latency_ms gauge",
        f"live_agent_playback_start_latency_p50_ms {m['playback_start_p50_ms']}",
        f"live_agent_playback_start_latency_p95_ms {m['playback_start_p95_ms']}",
        f"live_agent_playback_start_latency_p99_ms {m['playback_start_p99_ms']}",
        "",
        "# HELP live_agent_virtual_audio_pending_chunks 虚拟声卡待播放事务数",
        "# TYPE live_agent_virtual_audio_pending_chunks gauge",
        f"live_agent_virtual_audio_pending_chunks {m['virtual_audio_pending_chunks']}",
        "",
        "# HELP live_agent_virtual_audio_pending_bytes 虚拟声卡队列待处理字节数",
        "# TYPE live_agent_virtual_audio_pending_bytes gauge",
        f"live_agent_virtual_audio_pending_bytes {m['virtual_audio_pending_bytes']}",
        "",
        "# HELP live_agent_incremental_pcm_transactions_opened_total 已开启的增量 PCM 事务数",
        "# TYPE live_agent_incremental_pcm_transactions_opened_total counter",
        f"live_agent_incremental_pcm_transactions_opened_total {m['incremental_pcm_transactions_opened']}",
        "",
        "# HELP live_agent_incremental_pcm_transactions_committed_total 已原子提交的增量 PCM 事务数",
        "# TYPE live_agent_incremental_pcm_transactions_committed_total counter",
        f"live_agent_incremental_pcm_transactions_committed_total {m['incremental_pcm_transactions_committed']}",
        "",
        "# HELP live_agent_incremental_pcm_transactions_aborted_total 已取消或回滚的增量 PCM 事务数",
        "# TYPE live_agent_incremental_pcm_transactions_aborted_total counter",
        f"live_agent_incremental_pcm_transactions_aborted_total {m['incremental_pcm_transactions_aborted']}",
        "",
        "# HELP live_agent_incremental_pcm_active_transactions 当前活跃增量 PCM 事务数",
        "# TYPE live_agent_incremental_pcm_active_transactions gauge",
        f"live_agent_incremental_pcm_active_transactions {m['incremental_pcm_active_transactions']}",
        "",
        "# HELP live_agent_neural_sidecar_connected sidecar 传输连接状态",
        "# TYPE live_agent_neural_sidecar_connected gauge",
        f"live_agent_neural_sidecar_connected {m['neural_sidecar_connected']}",
        "",
        "# HELP live_agent_neural_sidecar_ready sidecar 渲染 backend 就绪状态",
        "# TYPE live_agent_neural_sidecar_ready gauge",
        f"live_agent_neural_sidecar_ready {m['neural_sidecar_ready']}",
        "",
        "# HELP live_agent_neural_sidecar_degraded sidecar 是否已降级到本地 shadow",
        "# TYPE live_agent_neural_sidecar_degraded gauge",
        f"live_agent_neural_sidecar_degraded {m['neural_sidecar_degraded']}",
        "",
        "# HELP live_agent_av_drift_ms 音画同步漂移 (毫秒，负值表示音频滞后于画面)",
        "# TYPE live_agent_av_drift_ms gauge",
        f"live_agent_av_drift_ms {m['av_drift_ms']}",
        "",
        "# HELP live_agent_av_recommended_delay_ms 共享时钟推荐的音频前置延迟补偿量 (毫秒)",
        "# TYPE live_agent_av_recommended_delay_ms gauge",
        f"live_agent_av_recommended_delay_ms {m['av_recommended_delay_ms']}",
        "",
        "# HELP live_agent_av_frame_pacing_hint_active 帧节奏建议是否激活 (1=skip/duplicate frame)",
        "# TYPE live_agent_av_frame_pacing_hint_active gauge",
        f"live_agent_av_frame_pacing_hint_active {m['av_frame_pacing_hint']}",
        "",
        "# HELP live_agent_av_audio_anchored 音频侧锚点是否已建立 (0 时 drift 不可信)",
        "# TYPE live_agent_av_audio_anchored gauge",
        f"live_agent_av_audio_anchored {m['av_audio_anchored']}",
        "",
        "# HELP live_agent_av_sync_drift_locked AVSyncController 漂移是否已锁定",
        "# TYPE live_agent_av_sync_drift_locked gauge",
        f"live_agent_av_sync_drift_locked {m['av_sync_drift_locked']}",
        "",
        "# HELP live_agent_frame_bus_frames_published_total 帧发布总线实际发布帧数",
        "# TYPE live_agent_frame_bus_frames_published_total counter",
        f"live_agent_frame_bus_frames_published_total {m['frame_bus_frames_published']}",
        "",
        "# HELP live_agent_frame_bus_frames_suppressed_total 帧发布总线被租约仲裁抑制的帧数 (shadow 帧被云端帧抑制)",
        "# TYPE live_agent_frame_bus_frames_suppressed_total counter",
        f"live_agent_frame_bus_frames_suppressed_total {m['frame_bus_frames_suppressed']}",
        "",
        "# HELP live_agent_memory_rss_bytes 进程常驻内存字节数",
        "# TYPE live_agent_memory_rss_bytes gauge",
        f"live_agent_memory_rss_bytes {int(m['process_mem_rss_mb'] * 1024 * 1024)}",
        "",
    ]
    lines.extend([
        "# HELP live_agent_avatar_provider_up Provider adapter 生命周期状态",
        "# TYPE live_agent_avatar_provider_up gauge",
        "# HELP live_agent_avatar_provider_ready Provider 当前是否可承接新句子",
        "# TYPE live_agent_avatar_provider_ready gauge",
        "# HELP live_agent_avatar_provider_selected Provider 当前是否有在途句子",
        "# TYPE live_agent_avatar_provider_selected gauge",
        "# HELP live_agent_avatar_provider_in_flight Provider 当前在途请求数",
        "# TYPE live_agent_avatar_provider_in_flight gauge",
        "# HELP live_agent_avatar_provider_circuit_state Provider 熔断状态 (0 closed, 1 half_open, 2 open)",
        "# TYPE live_agent_avatar_provider_circuit_state gauge",
        "# HELP live_agent_avatar_provider_requests_total Provider 整句请求总数",
        "# TYPE live_agent_avatar_provider_requests_total counter",
        "# HELP live_agent_avatar_provider_outcomes_total Provider 低基数结果计数",
        "# TYPE live_agent_avatar_provider_outcomes_total counter",
        "# HELP live_agent_avatar_provider_last_latency_milliseconds Provider 最近请求耗时",
        "# TYPE live_agent_avatar_provider_last_latency_milliseconds gauge",
        "# HELP live_agent_avatar_provider_quota_minor Provider 运行期预算金额（最小币种单位）",
        "# TYPE live_agent_avatar_provider_quota_minor gauge",
    ])
    circuit_values = {"closed": 0, "half_open": 1, "open": 2}
    for provider in m["avatar_provider"].get("providers", []):
        provider_id = _prom_label(provider.get("provider_id", "unknown"))
        label = f'provider_id="{provider_id}"'
        lines.extend([
            f"live_agent_avatar_provider_up{{{label}}} {1 if provider.get('up') else 0}",
            f"live_agent_avatar_provider_ready{{{label}}} {1 if provider.get('ready') else 0}",
            f"live_agent_avatar_provider_selected{{{label}}} {1 if provider.get('selected') else 0}",
            f"live_agent_avatar_provider_in_flight{{{label}}} {provider.get('in_flight', 0)}",
            f"live_agent_avatar_provider_circuit_state{{{label}}} {circuit_values.get(provider.get('circuit_state'), 2)}",
            f"live_agent_avatar_provider_requests_total{{{label}}} {provider.get('requests_total', 0)}",
            f"live_agent_avatar_provider_last_latency_milliseconds{{{label}}} {provider.get('last_latency_ms', 0)}",
        ])
        for outcome, count in provider.get("outcomes", {}).items():
            lines.append(
                "live_agent_avatar_provider_outcomes_total"
                f'{{{label},outcome="{_prom_label(outcome)}"}} {count}'
            )
        quota = provider.get("quota") or {}
        for kind in ("spent", "reserved", "remaining"):
            value = quota.get(f"{kind}_minor")
            if value is not None:
                lines.append(
                    "live_agent_avatar_provider_quota_minor"
                    f'{{{label},kind="{kind}"}} {value}'
                )
    lines.append("")
    return Response(content="\n".join(lines), media_type="text/plain; version=0.0.4; charset=utf-8")


@router.get("/api/v1/system/health-summary")
async def health_summary():
    """供 Web 控制台实时拉取展示的指标看板 JSON 数据"""
    m = _collect_live_metrics()
    return {
        "code": 0,
        "data": m,
    }
