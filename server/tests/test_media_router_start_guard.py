# -*- coding: utf-8 -*-
"""
MediaDriverRouter 启动前驱动守卫回归测试。

背景（上线诊断 RISK-10）：
`MediaDriverRouter.__init__` 在 `CV_AVAILABLE is False`（未装 numpy/opencv）
时会把 ``active_driver`` 初始化为**无画面输出的 no-op MockMediaDriver**。
`select_driver()` 本身是正确的 fail-closed（缺依赖直接 raise），但
``start()`` 只是 ``await self.active_driver.start()``，**没有任何 driver_type
守卫**。

后果：任何未来新增的、或因重构而绕过 ``select_driver()`` 的代码路径，
一旦直接调用 ``start()``，就会静默开出一场「有声音、零画面」的直播 ——
在 7x24 商用场景下这是最难被发现的一类事故（后台不报错、前端不告警、
观众看到黑屏或静止画面）。

本文件锁定不变量：**mock 驱动永远不能进入运行态**。
"""
import asyncio

import pytest

from server.adapters.media.media_router import MediaDriverRouter


@pytest.fixture(autouse=True)
def _reset_router_singleton():
    yield


def _make_router(monkeypatch, driver_type: str, driver=None):
    """构造一个 driver_type 已定的 MediaDriverRouter。"""
    router = MediaDriverRouter.__new__(MediaDriverRouter)
    # 绕过 __init__ 的依赖探测，只装配守卫所需的最小状态
    from server.adapters.media.base_driver import BaseMediaDriver

    class _Stub(BaseMediaDriver):
        def __init__(self):
            super().__init__()
            self.started = False

        async def feed_audio_chunk(self, *args, **kwargs):
            return None

        async def interrupt(self, *args, **kwargs):
            return None

        async def start(self):
            self.started = True

        async def stop(self):
            self.is_speaking = False

    router.active_driver = driver if driver is not None else _Stub()
    router.driver_type = driver_type
    router.is_running = False
    router.is_degraded = False
    router.degrade_reason = None
    router.execution_mode = "DEMO"
    router.avatar_orchestrator = None
    router.sidecar_driver = None
    router.remote_driver = None
    return router


# ---------------------------------------------------------------------------
# RED-1: mock 驱动绝不允许进入运行态
# ---------------------------------------------------------------------------

def test_start_refuses_mock_driver_even_in_demo_mode():
    """即使在 DEMO 档，mock 驱动也不得被 start() 放行。

    DEMO/PILOT/PRODUCTION 全部拒绝：mock 驱动只提供音频链路、零画面，
    「让它跑起来」永远不是正确行为 —— 需要音频链路时也必须显式选择。
    """
    router = _make_router(None, "mock")

    with pytest.raises(RuntimeError) as exc:
        asyncio.run(router.start())

    assert "mock" in str(exc.value).lower()
    assert router.is_running is False, "被拒绝后不得进入运行态"
    assert router.active_driver.started is False, "被拒绝时不得调用下游驱动的 start()"


@pytest.mark.parametrize("mode", ["DEMO", "PREVIEW", "PILOT", "PRODUCTION"])
def test_start_refuses_mock_driver_in_every_execution_mode(mode):
    """任何运行档位下都不得静默开出一场零画面的直播。"""
    router = _make_router(None, "mock")
    router.execution_mode = mode

    with pytest.raises(RuntimeError):
        asyncio.run(router.start())

    assert router.is_running is False


# ---------------------------------------------------------------------------
# RED-2: 合规驱动必须正常放行（防止守卫过度拦截）
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("driver_type", ["procedural_avatar", "neural_sidecar", "remote_gpu"])
def test_start_allows_real_renderers(driver_type):
    """真实渲染驱动必须能正常 start()，守卫不得误伤主链路。"""
    router = _make_router(None, driver_type)

    asyncio.run(router.start())

    assert router.is_running is True
    assert router.active_driver.started is True


def test_start_allows_orchestrator_driven_path():
    """挂载了 Avatar Orchestrator 的路径同样必须放行。"""
    router = _make_router(None, "procedural_avatar")

    class _Orch:
        def __init__(self):
            self.started = False

        async def start(self):
            self.started = True

        async def stop(self):
            pass

    orch = _Orch()
    router.avatar_orchestrator = orch
    router._orchestrator_start_task = None
    router._orchestrator_started = lambda *a, **k: None
    router._track_sidecar_task = lambda *a, **k: None

    asyncio.run(router.start())

    assert router.is_running is True
    assert orch.started is True


# ---------------------------------------------------------------------------
# RED-3: 守卫必须在 select_driver 之前也有效（防重构回归）
# ---------------------------------------------------------------------------

def test_router_init_without_cv_never_yields_runnable_mock(monkeypatch):
    """构造函数在缺 CV 依赖时也不应留下一个「看起来能跑」的 mock 状态。

    修复后：构造函数仍可为 mock（供状态查询显示），但 start() 会硬拦截。
    本测试确保 is_running 永远为 False。
    """
    import server.adapters.media.media_router as mr

    monkeypatch.setattr(mr, "CV_AVAILABLE", False)
    router = mr.MediaDriverRouter()

    assert router.driver_type == "mock"
    assert router.is_running is False, "构造函数绝不能把 mock 驱动置于运行态"
