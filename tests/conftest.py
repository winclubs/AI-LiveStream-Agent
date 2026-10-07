"""根 `tests/` 套件夹具。

历史问题：本目录从未被 pytest 收集（`pyproject.toml` 的
`testpaths = ["server/tests"]` 不含此处），因此这里的用例既没有夹具、
也从未被执行 —— 数据库表结构未初始化，异步标记缺少插件，全部处于失效状态。

本文件把 `server/tests/conftest.py` 的数据库隔离策略同样应用到根目录套件：
只重定向**数据库**到临时目录，绝不重定向 DATA_DIR 全局根（主播资产与
神经模型权重仍在真实目录，依赖真实资产的断言才不会失效）。
"""

from __future__ import annotations

import asyncio as _asyncio
import os as _os

# 与 server/tests/conftest.py 一致：测试环境禁止触发真实权重下载与 pip 安装。
_os.environ.setdefault("LIVE_AGENT_DISABLE_AUTO_DOWNLOAD", "1")

import pytest

import server.database.db as _db_mod


@pytest.fixture
def anyio_backend():
    """anyio 插件的异步后端声明 (根目录用例使用 @pytest.mark.anyio)。"""
    return "asyncio"


# 必须与 server/tests/conftest.py 一样在**模块导入期**完成隔离：
# 根目录用例在模块顶层就创建 TestClient(app) 并导入 AsyncSessionLocal，
# 若等到 fixture 执行才切换，引擎早已绑定到真实库文件，表结构不会建到隔离库上，
# 结果就是 "no such table"。
_db_mod.enable_test_isolation()
_asyncio.run(_db_mod.init_db())
