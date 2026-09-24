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
"""
import asyncio as _asyncio

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


# 为纯单元测试 (不经 TestClient 启动 app) 预初始化隔离库表结构与种子数据，
# 保证 test_core_engine.py 等文件可独立运行 (ADR-07 测试隔离)
_db_mod.enable_test_isolation()
_asyncio.run(_db_mod.init_db())
