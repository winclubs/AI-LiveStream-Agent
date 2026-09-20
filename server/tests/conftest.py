"""
Pytest 全局夹具：测试前将数据目录重定向到临时文件夹
确保单元测试完全隔离于生产库 data/live_agent.db，不再污染真实数据
"""
import os
import shutil
import tempfile


def pytest_sessionfinish(session, exitstatus):
    """无论测试成功或失败，都清理隔离数据目录。"""
    shutil.rmtree(_TEST_DATA_DIR, ignore_errors=True)

# 必须在导入任何 server 模块之前设置 (config.py 在导入时读取该变量)
_TEST_DATA_DIR = tempfile.mkdtemp(prefix="liveagent_test_data_")
os.environ["LIVE_AGENT_DATA_DIR"] = _TEST_DATA_DIR

# 为纯单元测试 (不经 TestClient 启动 app) 预初始化临时库表结构与种子数据，
# 保证 test_core_engine.py 等文件可独立运行 (ADR-07 测试隔离)
import asyncio as _asyncio
import pytest
from server.database.db import init_db as _init_db

@pytest.fixture
def anyio_backend():
    return "asyncio"

_asyncio.run(_init_db())
