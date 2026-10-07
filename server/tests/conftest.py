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

import pytest

import server.database.db as _db_mod


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture(autouse=True)
def _reset_global_overlay_state():
    """每个测试前后清空全局画层状态，杜绝优惠券/特写画层跨用例残留。"""
    try:
        from server.core.media.scene_overlay import global_scene_overlay_state
        global_scene_overlay_state.clear()
        yield
        global_scene_overlay_state.clear()
    except Exception:
        yield


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


# 为纯单元测试 (不经 TestClient 启动 app) 预初始化隔离库表结构与种子数据，
# 保证 test_core_engine.py 等文件可独立运行 (ADR-07 测试隔离)
_db_mod.enable_test_isolation()
_asyncio.run(_db_mod.init_db())
