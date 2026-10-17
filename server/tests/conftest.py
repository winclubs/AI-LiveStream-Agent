"""
Pytest 全局夹具：为 server/tests 套件提供测试隔离数据库与全局单例状态清理。

隔离策略 (ADR-07)：
- 仅重定向 *数据库* 到临时目录，绝不重定向 DATA_DIR 全局根。
  原因：DATA_DIR 同时承载主播数字人资产 (avatar_assets/coords.pkl/face_imgs)
  与神经模型权重 (data/models/)，整体重定向会让依赖真实资产的断言失效，
  也会让权重落盘目录与 NeuralModelManager 检索路径脱钩。
- 数据库文件通过 server.database.db.enable_test_isolation() 切换，
  不触碰 LIVE_AGENT_DATA_DIR 环境变量，避免污染同进程其它套件。
- 每个测试自动清空全局画层状态，防止电商优惠券/特写画层残留跨用例漂移。
- 关闭可选依赖后台自动下载总开关：杜绝体检/启动钩子在测试环境触发真实
  的 45MB 权重下载或 pip 安装 (ADR-16 架构诚实：测试不应依赖外部网络)。
"""
import asyncio as _asyncio
import os as _os

# 必须在导入任何业务模块之前设定：auto_download_enabled() 运行期读取本变量，
# 设定后体检与启动钩子的自动下载/安装逻辑在测试全程保持关闭。
_os.environ.setdefault("LIVE_AGENT_DISABLE_AUTO_DOWNLOAD", "1")

import socket as _socket

import pytest

import server.database.db as _db_mod

_edge_tts_probe_host = "speech.platform.bing.com"
_edge_tts_probe_port = 443
_edge_tts_reachable: "bool | None" = None


def edge_tts_network_available() -> bool:
    """探测 Edge-TTS 合成端点是否可达，结果在整个会话内缓存。

    架构诚实 (ADR-16)：依赖真实外网的用例必须**如实跳过**而非静默失败，
    也不得以此为由掩盖真实缺陷——本探针仅覆盖「本机是否具备出网条件」，
    不替代任何功能断言。
    """
    global _edge_tts_reachable
    if _edge_tts_reachable is None:
        try:
            with _socket.create_connection(
                (_edge_tts_probe_host, _edge_tts_probe_port), timeout=3.0
            ):
                _edge_tts_reachable = True
        except OSError:
            _edge_tts_reachable = False
    return _edge_tts_reachable


def pytest_collection_modifyitems(config, items):
    """按真实出网条件决定 requires_edge_tts_network 标记用例的存废。

    探针结果在会话内缓存，因此无论标记多少用例都只发生一次 TCP 探测。
    """
    if edge_tts_network_available():
        return
    reason = "需要出网访问 Edge-TTS 合成端点 (speech.platform.bing.com) 才能执行"
    skip = pytest.mark.skip(reason=reason)
    for item in items:
        if item.get_closest_marker("requires_edge_tts_network") is not None:
            item.add_marker(skip)


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture(autouse=True)
def _reset_commercial_global_state():
    """隔离商业化开关、全局画层与媒体看门狗计时器，避免测试顺序污染。"""
    from server.core.compliance.kill_switch import global_kill_switch
    from server.core.media.scene_overlay import global_scene_overlay_state
    from server.core.monitoring.media_quality_watchdog import global_media_watchdog
    from server.routes.live import global_live_controller
    global_kill_switch.reset()
    global_media_watchdog.stop()
    global_media_watchdog.set_execution_mode("PREVIEW")
    global_media_watchdog.set_output_readiness_probe(None)
    global_media_watchdog.set_stream_output_ready(False)
    global_live_controller.is_live = False
    global_live_controller.execution_mode = "PREVIEW"
    global_live_controller.state_detail = "STOPPED"
    global_live_controller.session_id = None
    yield
    global_kill_switch.reset()
    global_media_watchdog.stop()
    global_media_watchdog.set_execution_mode("PREVIEW")
    global_media_watchdog.set_output_readiness_probe(None)
    global_media_watchdog.set_stream_output_ready(False)
    global_live_controller.is_live = False
    global_live_controller.execution_mode = "PREVIEW"
    global_live_controller.state_detail = "STOPPED"
    global_live_controller.session_id = None
    global_scene_overlay_state.clear()


@pytest.fixture(autouse=True)
def _ready_neural_renderer(request):
    """注入就绪的神经唇形渲染器。

    硬性规约：开播必须走真实神经渲染，mouth_open 模拟驱动回退已彻底移除，
    引擎未就绪时 /live/start 会如实返回 500。因此任何覆盖开播链路的用例都必须
    面对一个"就绪"的引擎，否则会在启动前置校验处失败——这不是被掩盖的缺陷，
    而是禁止模拟驱动的直接后果。

    例外：需要断言"未就绪时如实上报 neural_lipsync=False"这类**架构诚实**契约的
    用例，必须给注入让路，否则 mock 会让能力上报变成谎报。请加标记：
        @pytest.mark.no_neural_mock
    """
    from unittest.mock import MagicMock
    import numpy as np

    if request.node.get_closest_marker("no_neural_mock"):
        yield None
        return

    from server.adapters.media.musetalk_driver import global_musetalk_driver

    mock_lip = MagicMock()
    mock_lip.is_ready = True
    mock_lip.has_anchor_assets = True
    mock_lip.render_lip_frame.return_value = np.zeros((960, 720, 3), dtype=np.uint8)
    real_renderer = global_musetalk_driver.lip_renderer
    global_musetalk_driver.lip_renderer = mock_lip
    yield mock_lip
    global_musetalk_driver.lip_renderer = real_renderer


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "no_neural_mock: 该用例需断言神经引擎未就绪时的诚实上报，跳过全局 mock 注入",
    )
    config.addinivalue_line(
        "markers",
        "requires_edge_tts_network: 需要出网访问 Edge-TTS 合成端点，不可达时如实跳过",
    )


# 为纯单元测试 (不经 TestClient 启动 app) 预初始化隔离库表结构与种子数据，
# 保证 test_core_engine.py 等文件可独立运行 (ADR-07 测试隔离)
_db_mod.enable_test_isolation()
_asyncio.run(_db_mod.init_db())
