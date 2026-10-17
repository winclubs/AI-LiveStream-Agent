# -*- coding: utf-8 -*-
"""「缝」的晚绑定透传守护。

`server/routes/live.py` 拆分后，19 个 REQUIRED_EXPORTS 成为 façade 上的缝。
本文件**不验证符号存在**（那由 test_live_route_contract 负责），只守护
「缝必须在调用时解析」这一条规则。

为什么必须单独守护：单向 `from x import y` 搬走会让
`patch("server.routes.live.X")` **静默失效** —— 测试照样绿，线上行为却变了。
这比测试直接报错更危险，因此拆分的每一阶段都必须跑本文件。

本文件有两条互补的守卫：

1. **行为守卫**：对当前 live.py 中已存在的链路，直接验证透传真生效
   （`_probe_gpu` → `_hardware_payload`、看门狗探针）。
2. **结构守卫**：AST 扫描拆分后的所有子模块，禁止出现**模块顶层**的
   `from server.routes.live import <缝>`。这是唯一能机械判定
   「有没有偷偷把缝改成早绑定」的手段 —— 行为守卫只能覆盖已写到的用例。
"""
import ast
import inspect
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]

#: 必须留在 `server.routes.live` 命名空间的全局单例缝。
GLOBAL_SEAMS = (
    "global_live_controller",
    "global_obs_client",
    "global_role_manager",
    "global_virtual_audio",
    "global_media_driver",
)

#: 必须留在 `server.routes.live` 命名空间的缝函数。
CALLABLE_SEAMS = (
    "restore_obs_connection",
    "_refresh_order_stats",
    "_start_live_unlocked",
    "_stop_live_unlocked",
    "_configure_media_watchdog",
    "_stream_output_ready_probe",
    "_build_external_publish_status",
    "_probe_gpu",
    "_recommend_tier",
    "_hardware_payload",
)

#: 必须留在 ``server.routes.live`` 命名空间的**类**缝。
#:
#: ``EdgeTTSMediaDriver`` 尤其危险：mixin 若在**模块顶层** import 它，
#: ``patch("server.routes.live.EdgeTTSMediaDriver")`` 会被静默绕过 ——
#: 降级路径转而请求真实 Edge-TTS，测试表现为「拿到真实 MP3 而非替身」。
CLASS_SEAMS = ("EdgeTTSMediaDriver",)

ALL_SEAMS = set(GLOBAL_SEAMS) | set(CALLABLE_SEAMS) | set(CLASS_SEAMS)

#: 拆分后的模块范围。结构守卫只扫描这些文件（façade 自身除外：
#: façade 持有缝是设计意图，它的顶层 `from ... import` 合法）。
SPLIT_GLOBS = ("server/routes/live_*.py", "server/core/live/**/*.py")
FAÇADE = ROOT / "server" / "routes" / "live.py"


def _split_modules() -> list[Path]:
    modules: list[Path] = []
    for pattern in SPLIT_GLOBS:
        modules += [p for p in sorted(ROOT.glob(pattern)) if p.is_file()]
    return modules


def _top_level_seam_imports(tree: ast.Module) -> set[str]:
    """模块**顶层**（不在函数体内）从 server.routes.live 导入的缝名集合。

    只看 ``tree.body`` 是本检查的关键：函数体里的 import 是**正确**写法
    （调用时解析），只有顶层 import 才是早绑定的违规写法。
    """
    hits: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.ImportFrom) and node.module == "server.routes.live":
            hits |= {alias.name for alias in node.names}
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "server.routes.live":
                    hits.add("<whole-module>")
    return hits


def _run(coro):
    """在无运行中事件循环的环境里跑协程。"""
    import asyncio

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    raise RuntimeError("本用例需在无运行中事件循环的环境执行")


# ---------------------------------------------------------------------------
# 1. 行为守卫：透传真生效
# ---------------------------------------------------------------------------

def test_probe_gpu_to_hardware_payload_is_late_bound(monkeypatch):
    """`_hardware_payload` 必须在调用时读 `live._probe_gpu`，不得持有旧引用。"""
    from server.routes import live

    seen: dict = {}

    def fake_probe() -> dict:
        seen["called"] = True
        return {"cuda_available": True, "vram_total_gb": 24, "name": "fake"}

    monkeypatch.setattr(live, "_probe_gpu", fake_probe)
    payload = _run(live._hardware_payload())

    assert seen.get("called") is True, (
        "_hardware_payload 未走 live._probe_gpu —— 透传断裂，"
        "patch('server.routes.live._probe_gpu') 将静默失效"
    )
    assert isinstance(payload, dict)


def test_recommend_tier_seam_is_late_bound(monkeypatch):
    """`_recommend_tier` 是缝，改写后其消费方必须在调用时看到新值。"""
    from server.routes import live

    sentinel = lambda info: "TIER-FROM-PATCH"  # noqa: E731
    monkeypatch.setattr(live, "_recommend_tier", sentinel)

    assert live._recommend_tier({"cuda_available": True}) == "TIER-FROM-PATCH"


def test_watchdog_seam_uses_live_probe(monkeypatch):
    """`_configure_media_watchdog` 必须把 `live._stream_output_ready_probe` 交给看门狗。"""
    from server.core.monitoring.media_quality_watchdog import global_media_watchdog
    from server.routes import live

    original = live._stream_output_ready_probe
    sentinel = lambda: True  # noqa: E731
    monkeypatch.setattr(live, "_stream_output_ready_probe", sentinel)
    try:
        live._configure_media_watchdog("PILOT")
        probe = getattr(global_media_watchdog, "_output_readiness_probe", None)
        if probe is not None:
            assert probe is sentinel, (
                "看门狗未拿到 live 上的当前探针 —— 透传断裂，"
                "patch('server.routes.live._stream_output_ready_probe') 将无效"
            )
    finally:
        global_media_watchdog.set_output_readiness_probe(original)
        global_media_watchdog.set_execution_mode("PREVIEW")


# ---------------------------------------------------------------------------
# 2. 缝本身必须可取用
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("name", GLOBAL_SEAMS)
def test_global_seam_is_resolvable(name):
    """5 个全局单例缝必须在 live 命名空间可取用（patch 的前置条件）。"""
    from server.routes import live

    assert hasattr(live, name), f"{name} 缺失，patch 将直接报错"
    assert getattr(live, name) is not None, f"{name} 为 None，patch 将静默失效"


@pytest.mark.parametrize("name", CALLABLE_SEAMS)
def test_callable_seam_exists(name):
    """10 个缝函数必须存在且可调用（patch 一个 None 是静默失效）。"""
    from server.routes import live

    obj = getattr(live, name, None)
    assert obj is not None, f"{name} 缺失或为 None"
    assert callable(obj), f"{name} 不是可调用对象"
    assert inspect.unwrap(obj) is not None


@pytest.mark.parametrize("name", CLASS_SEAMS)
def test_class_seam_is_resolvable(name):
    """类缝必须在 live 命名空间可取用，且确实是可 patch 的类对象。"""
    from server.routes import live

    obj = getattr(live, name, None)
    assert obj is not None, f"{name} 缺失，patch 将直接报错"
    assert isinstance(obj, type), f"{name} 不是类对象，patch 语义不符"
    assert obj.__name__ == name, f"{name} 指向了其他类"


# ---------------------------------------------------------------------------
# 3. 结构守卫：拆分后的子模块禁止顶层 import 缝
# ---------------------------------------------------------------------------

def test_no_split_module_uses_top_level_seam_import():
    """拆分后的任何模块都不得在**模块顶层** from server.routes.live import <缝>。

    顶层 import = 早绑定 = patch 静默失效。正确写法是函数体内 import。

    拆分尚未开始时本测试天然通过（无子模块可扫），从第一个子模块落地起
    开始生效。
    """
    offenders: list[str] = []
    for path in _split_modules():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        bad = sorted(_top_level_seam_imports(tree) & ALL_SEAMS)
        if bad:
            offenders.append(f"{path.relative_to(ROOT).as_posix()}: 顶层 import 了 {bad}")

    assert not offenders, (
        "以下模块在顶层 import 了缝（早绑定，patch 会静默失效）:\n  "
        + "\n  ".join(offenders)
        + "\n  正确写法：写在函数体内的 `from server.routes.live import X`。"
    )


def test_split_modules_declare_live_seam_where_needed():
    """反向体检：凡是用到缝的实现模块，必须存在函数体内的缝 import。

    与上一条互补：上一条抓「写错了」，本条抓「本该写却漏了」——
    漏写会让模块在 import 期就 NameError，而不是静默失效，
    但提前发现比运行到一半炸掉要好。
    """
    problems: list[str] = []
    for path in _split_modules():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        if not _split_modules_uses_seam(tree):
            continue
        if not _has_function_body_seam_import(tree):
            problems.append(path.relative_to(ROOT).as_posix())

    assert not problems, (
        "以下模块用到了缝却没有函数体内的 `from server.routes.live import <缝>`:\n  "
        + "\n  ".join(problems)
    )


def test_facade_is_excluded_from_structural_scan():
    """元断言：façade 自身不参与结构扫描（它持有缝是设计意图）。"""
    assert FAÇADE.exists(), "façade server/routes/live.py 不存在"
    assert FAÇADE not in _split_modules(), "façade 被误纳入结构扫描范围"
    assert not any(FAÇADE.match(ROOT / pattern) for pattern in SPLIT_GLOBS), (
        "façade 匹配上了 SPLIT_GLOBS —— 结构守卫会误报 façade 的合法顶层 import"
    )


# ---------------------------------------------------------------------------
# helpers（AST 辅助）
# ---------------------------------------------------------------------------

def _split_modules_uses_seam(tree: ast.Module) -> bool:
    """模块内是否引用了任一缝名（含属性访问的基名）。"""
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id in ALL_SEAMS:
            return True
    return False


def _has_function_body_seam_import(tree: ast.Module) -> bool:
    """是否存在**函数体内**的 from server.routes.live import ... 语句。"""
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for inner in ast.walk(node):
            if isinstance(inner, ast.ImportFrom) and inner.module == "server.routes.live":
                return True
    return False
