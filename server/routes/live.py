import io
import re
import logging
import inspect
import wave
from fastapi import APIRouter
from typing import Optional, Any
from sqlalchemy.ext.asyncio import AsyncSession
from server.adapters.media.media_router import global_media_router
global_media_driver = global_media_router  # 兼容现有单例引用
from server.adapters.obs.obs_client import global_obs_client

logger = logging.getLogger("LiveAgent.LiveController")
router = APIRouter(prefix="/live", tags=["直播控场与调度"])

# 句级切分标点 (前瞻播报与串行播报共用同一口径)
_SENTENCE_DELIMITERS = re.compile(r'([，。！？；\n]+)')

OBS_SETTING_HOST = "obs_websocket_host"
OBS_SETTING_PORT = "obs_websocket_port"
OBS_SETTING_PASSWORD = "obs_websocket_password"
OBS_SETTING_AUTO_CONNECT = "obs_auto_connect"


def _stream_output_ready_probe() -> bool:
    """推流输出就绪探针：任一真实输出通道在工作才允许帧停滞判定。

    RTMP 直推 / 虚拟摄像头(OBS) / WebRTC 观看端 / 切片录制任一活动即视为就绪。
    全部未就绪时没有外部观众消费画面，判定 MEDIA_DEGRADED 只会制造误降级。
    """
    try:
        from server.core.media.rtmp_streamer import global_rtmp_streamer
        if bool(getattr(global_rtmp_streamer, "is_streaming", False)):
            return True
    except Exception:
        pass
    try:
        from server.core.media.virtual_cam import global_virtual_cam
        if getattr(global_virtual_cam, "is_active", False):
            return True
    except Exception:
        pass
    try:
        from server.core.media.webrtc_streamer import get_webrtc_stream_manager
        manager = get_webrtc_stream_manager()
        if manager is not None and int(manager.get_status().get("active_connections", 0) or 0) > 0:
            return True
    except Exception:
        pass
    try:
        from server.core.media.recorder import get_record_manager
        recorder = get_record_manager()
        if recorder is not None and bool(getattr(recorder, "is_recording", lambda: False)()):
            return True
    except Exception:
        pass
    return False


def _configure_media_watchdog(execution_mode: str) -> None:
    """按运行档位配置媒体质量看门狗：停滞门限策略 + 推流输出就绪探针。"""
    from server.core.monitoring.media_quality_watchdog import global_media_watchdog
    global_media_watchdog.set_execution_mode(execution_mode)
    global_media_watchdog.set_output_readiness_probe(_stream_output_ready_probe)


def _pcm_s16le_to_wav(pcm: bytes, sample_rate: int, channels: int) -> bytes:
    """将裸 PCM16 封装为浏览器可独立播放的完整 WAV 容器。"""
    output = io.BytesIO()
    with wave.open(output, "wb") as wav_file:
        wav_file.setnchannels(int(channels))
        wav_file.setsampwidth(2)
        wav_file.setframerate(int(sample_rate))
        wav_file.writeframes(pcm)
    return output.getvalue()


# ---------------------------------------------------------------------------
# 兼容再导出：OBS 协同与虚拟摄像头已迁至 server/routes/live_obs.py。
# 端点经子路由挂载；`restore_obs_connection` 被 server/app.py 直接导入，必须保留同名缝。
# OBS_SETTING_* 四个常量仍留在本模块（_start_live_unlocked 依赖它们）。
# ---------------------------------------------------------------------------
from server.routes.live_obs import router as _obs_router  # noqa: E402


def restore_obs_connection(db: AsyncSession) -> bool:
    """缝：OBS 自动连接恢复（实现见 server/routes/live_obs.py）。"""
    from server.routes.live_obs import restore_obs_connection as _impl

    return _impl(db)


router.include_router(_obs_router)


def _supported_metadata(callable_obj, metadata: dict) -> dict:
    """仅向声明支持的 callable 传元数据，兼容旧 driver 与测试替身。"""
    try:
        parameters = inspect.signature(callable_obj).parameters
    except (TypeError, ValueError):
        return {}
    if any(param.kind == inspect.Parameter.VAR_KEYWORD for param in parameters.values()):
        return metadata
    return {key: value for key, value in metadata.items() if key in parameters}


async def _call_async_compat(callable_obj, *args, **metadata):
    """兼容异步驱动与同步测试替身，统一收敛可等待结果。"""
    result = callable_obj(*args, **_supported_metadata(callable_obj, metadata))
    if inspect.isawaitable(result):
        return await result
    return result


def _call_sync_compat(callable_obj, *args, **metadata):
    return callable_obj(*args, **_supported_metadata(callable_obj, metadata))



# 全局直播运行控制器
# 兼容再导出：以下两处实现已被 mixin 搬走，但它们同时是 patch 目标
# （test_live_seam_late_binding 的 GLOBAL_SEAMS），必须继续存在于本模块命名空间，
# 且 mixin 一律在**函数体内**从本模块解析它们以保住晚绑定。
from server.core.media.virtual_audio import global_virtual_audio  # noqa: E402,F401
from server.core.roles.role_manager import global_role_manager  # noqa: E402,F401
# 兼容再导出：TTS 驱动类本身被 tts_voice mixin 搬走，但测试仍 patch 本模块上的它。
from server.adapters.media.edgetts_driver import EdgeTTSMediaDriver  # noqa: E402,F401

# 直播会话控制器：按关注点组合，各实现见 server/core/live/*.py。
from server.core.live.obs_control import ObsControlMixin  # noqa: E402
from server.core.live.queue_ingest import QueueIngestMixin  # noqa: E402
from server.core.live.runtime_slices import RuntimeSlicesMixin  # noqa: E402
from server.core.live.runtime_start import RuntimeStartMixin  # noqa: E402
from server.core.live.session_state import SessionStateMixin  # noqa: E402
from server.core.live.speech_commit import SpeechCommitMixin  # noqa: E402
from server.core.live.speech_prepare import SpeechPrepareMixin  # noqa: E402
from server.core.live.speech_speak import SpeechSpeakMixin  # noqa: E402
from server.core.live.telemetry import TelemetryMixin  # noqa: E402
from server.core.live.tts_voice import TtsVoiceMixin  # noqa: E402


class LiveSessionController(
    SessionStateMixin,
    ObsControlMixin,
    TtsVoiceMixin,
    SpeechPrepareMixin,
    SpeechSpeakMixin,
    SpeechCommitMixin,
    QueueIngestMixin,
    TelemetryMixin,
    RuntimeStartMixin,
    RuntimeSlicesMixin,
):
    """直播会话控制器组合壳。

    关注点与对应实现：
    - SessionStateMixin: 场次状态机：构造、锁、统计、AI 暂停/接管/恢复
    - ObsControlMixin: OBS 所有权属性与运行时配置热加载
    - TtsVoiceMixin: TTS 驱动选择、神经 sidecar 挂接与配置读取
    - SpeechPrepareMixin: 播报前置：断句、预取、人性化与 lookahead 管线
    - SpeechSpeakMixin: 播报执行：选路、PCM 流式与整句合成（含失败降级）
    - SpeechCommitMixin: 播报媒体提交：帧事务、游标栅栏、旁路联动与浏览器下行
    - QueueIngestMixin: 事件入队、优先级消费循环与弹幕路由
    - TelemetryMixin: 违规词与完成弹幕的落库流水
    - RuntimeStartMixin: 开播资源装载：队列/弹幕源/切片装载与帧循环
    - RuntimeSlicesMixin: 切片转场/播放、视觉巡检、优雅关闭与停播

    引用「缝」时一律在方法体内从 server.routes.live 解析，
    以保住 patch 的晚绑定语义（守卫 test_live_seam_late_binding.py）。
    """

    #: 单句 TTS 音频上限，超过即判定合成失控并触发 Edge 降级。
    MAX_TTS_SENTENCE_BYTES = 32 * 1024 * 1024



global_live_controller = LiveSessionController()

def _on_obs_external_state_change(active: bool, state: str, epoch: Optional[int] = None):
    """缝：OBS 外部推流状态回调（实现见 server/routes/live_obs.py）。"""
    from server.routes.live_obs import on_obs_external_state_change

    return on_obs_external_state_change(active, state, epoch)


global_obs_client.add_stream_state_listener(_on_obs_external_state_change)

# ---------------------------------------------------------------------------
# 兼容再导出：开播门禁体检已迁至 server/routes/live_preflight.py。
# 端点经子路由挂载，`server.routes.live.get_preflight_status` 仍可被直接导入。
# ---------------------------------------------------------------------------
from server.routes.live_preflight import (  # noqa: E402
    get_preflight_status,  # noqa: F401  兼容再导出：外部仍可 from server.routes.live import get_preflight_status
    router as _preflight_router,
)

router.include_router(_preflight_router)


# ---------------------------------------------------------------------------
from server.routes.live_overlay import router as _overlay_router  # noqa: E402

router.include_router(_overlay_router)



# ---------------------------------------------------------------------------
# 兼容再导出：弹幕接入与场控已迁至 server/routes/live_danmaku.py，端点经子路由挂载。
# ---------------------------------------------------------------------------
from server.routes.live_danmaku import router as _danmaku_router  # noqa: E402

router.include_router(_danmaku_router)



# ---- 兼容再导出：开播 / 停播生命周期已迁至 server/routes/live_lifecycle.py ----
# 端点经子路由挂载，以下符号仍从 server.routes.live 直接可见（被生产模块与测试
# import / patch）。转发体必须薄，且在**函数体内**解析目标 —— 顶层 import 会把缝
# 变成早绑定，使 patch 静默失效。守卫见 test_live_seam_late_binding.py。
from server.routes.live_lifecycle import (  # noqa: E402
    LiveInterruptRequest,  # noqa: F401  兼容再导出
    LiveStartRequest,  # noqa: F401  兼容再导出
    LiveTakeoverRequest,  # noqa: F401  兼容再导出
    interrupt_live,  # noqa: F401  兼容再导出
    pause_ai_live,  # noqa: F401  兼容再导出
    resume_ai_live,  # noqa: F401  兼容再导出
    takeover_live,  # noqa: F401  兼容再导出
    router as _lifecycle_router,
)


def _build_external_publish_status() -> dict:
    """缝：外部推流状态（实现见 server/routes/live_lifecycle.py）。"""
    from server.routes.live_lifecycle import _build_external_publish_status as _impl

    return _impl()


async def get_live_status():
    """缝：直播状态查询（实现见 server/routes/live_lifecycle.py）。"""
    from server.routes.live_lifecycle import get_live_status as _impl

    return await _impl()


async def _start_live_unlocked(req: LiveStartRequest, db: AsyncSession):
    """缝：生命周期锁内开播（实现见 server/routes/live_lifecycle.py）。"""
    from server.routes.live_lifecycle import _start_live_unlocked as _impl

    return await _impl(req, db)


async def _stop_live_unlocked(db: AsyncSession):
    """缝：生命周期锁内停播（实现见 server/routes/live_lifecycle.py）。"""
    from server.routes.live_lifecycle import _stop_live_unlocked as _impl

    return await _impl(db)


router.include_router(_lifecycle_router)


# ---------------------------------------------------------------------------
# 兼容再导出：以下符号被其他生产模块与测试直接导入 / patch（缝），不可移除。
# 转发体必须薄，且在**函数体内**解析目标 —— 顶层 import 会把缝变成早绑定，
# 使 patch("server.routes.live.<X>") 静默失效（测试绿而线上行为已变）。
# 守卫见 server/tests/test_live_seam_late_binding.py。
# ---------------------------------------------------------------------------
from server.routes.live_hardware import (  # noqa: E402
    _resolve_landmarks_for_avatar,  # noqa: F401  兼容再导出：live_lifecycle 经本模块的缝解析它
    router as _hardware_router,
)


def _probe_gpu() -> dict:
    """缝：本机 GPU 委托探测（单一事实源 gpu_capability）。"""
    from server.routes.live_hardware import probe_gpu

    return probe_gpu()


def _recommend_tier(gpu_info: dict) -> str:
    """缝：按硬件推荐运行模式。"""
    from server.routes.live_hardware import recommend_tier

    return recommend_tier(gpu_info)


async def _hardware_payload() -> dict:
    """缝：硬件负载载荷（含 TTL 缓存）。"""
    from server.routes.live_hardware import hardware_payload

    return await hardware_payload()


router.include_router(_hardware_router)


# ---------------------------------------------------------------------------
# 兼容再导出：订单与场次统计已迁至 server/routes/live_orders.py，端点经子路由挂载。
# `_refresh_order_stats` 被 server/routes/orders_webhook.py 直接导入，必须保留同名缝。
# ---------------------------------------------------------------------------
from server.routes.live_orders import router as _orders_router  # noqa: E402


async def _refresh_order_stats(db: AsyncSession, session_id: Any) -> tuple[float, int]:
    """缝：订单统计聚合刷新（实现见 server/routes/live_orders.py）。"""
    from server.routes.live_orders import refresh_order_stats as _impl

    return await _impl(db, session_id)


router.include_router(_orders_router)




# ---------------------------------------------------------------------------
# 兼容再导出：推流与流媒体已迁至 server/routes/live_stream.py，端点经子路由挂载。
# ---------------------------------------------------------------------------
from server.routes.live_stream import router as _stream_router  # noqa: E402

router.include_router(_stream_router)

# ---------------------------------------------------------------------------
# 兼容再导出：数字人实时控制已迁至 server/routes/live_avatar_ctl.py，端点经子路由挂载。
# ---------------------------------------------------------------------------
from server.routes.live_avatar_ctl import router as _avatar_router  # noqa: E402

router.include_router(_avatar_router)



# ---------------------------------------------------------------------------
# 兼容再导出：ASR 接入已迁至 server/routes/live_asr.py，端点经子路由挂载。
# 子路由无 prefix，路径保持相对形式（/asr/ws），完整路径仍是 /api/v1/live/asr/ws。
# ---------------------------------------------------------------------------
from server.routes.live_asr import router as _asr_router  # noqa: E402

router.include_router(_asr_router)


# ---------------------------------------------------------------------------
# 兼容再导出：切片录制已迁至 server/routes/live_recording.py，端点经子路由挂载。
# ---------------------------------------------------------------------------
from server.routes.live_recording import router as _recording_router  # noqa: E402

router.include_router(_recording_router)


# ---------------------------------------------------------------------------
# 兼容再导出：运营治理已迁至 server/routes/live_governance.py，端点经子路由挂载。
# ---------------------------------------------------------------------------
from server.routes.live_governance import router as _governance_router  # noqa: E402

router.include_router(_governance_router)

