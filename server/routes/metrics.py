import time
import psutil
from fastapi import APIRouter, Response
from server.config import APP_VERSION

router = APIRouter(tags=["系统指标与可观测性"])


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

    # 弹幕队列深度
    queue_size = 0
    if hasattr(global_live_controller, "event_queue") and global_live_controller.event_queue:
        queue_size = getattr(global_live_controller.event_queue, "qsize", lambda: 0)()

    # 熔断器状态 (0: 未初始化/正常, 1: 熔断降级开启)
    circuit_broken = 0
    circuit_state = "CLOSED"
    if hasattr(global_live_controller, "fetcher") and global_live_controller.fetcher:
        if hasattr(global_live_controller.fetcher, "state"):
            circuit_state = global_live_controller.fetcher.state
            if circuit_state in ("OPEN", "HALF_OPEN"):
                circuit_broken = 1

    # 虚拟摄像头/渲染指标
    cam_status = global_virtual_cam.get_status() if global_virtual_cam else {}
    fps = cam_status.get("fps", 25)
    frames_sent = cam_status.get("frames_sent", 0)

    # 进程与系统资源
    process = psutil.Process()
    mem_info = process.memory_info()
    mem_rss_mb = round(mem_info.rss / 1024 / 1024, 2)
    cpu_percent = process.cpu_percent(interval=None)

    return {
        "is_live": is_live,
        "session_id": getattr(global_live_controller, "session_id", None),
        "danmaku_count": danmaku_count,
        "viewer_count": viewer_count,
        "peak_viewers": peak_viewers,
        "gift_count": gift_count,
        "queue_size": queue_size,
        "circuit_state": circuit_state,
        "circuit_broken": circuit_broken,
        "render_fps": fps,
        "frames_sent": frames_sent,
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
        "# HELP live_agent_danmaku_total 累计处理弹幕条数",
        "# TYPE live_agent_danmaku_total counter",
        f"live_agent_danmaku_total {m['danmaku_count']}",
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
        "# HELP live_agent_memory_rss_bytes 进程常驻内存字节数",
        "# TYPE live_agent_memory_rss_bytes gauge",
        f"live_agent_memory_rss_bytes {int(m['process_mem_rss_mb'] * 1024 * 1024)}",
        "",
    ]
    return Response(content="\n".join(lines), media_type="text/plain; version=0.0.4; charset=utf-8")


@router.get("/api/v1/system/health-summary")
async def health_summary():
    """供 Web 控制台实时拉取展示的指标看板 JSON 数据"""
    m = _collect_live_metrics()
    return {
        "code": 0,
        "data": m,
    }
