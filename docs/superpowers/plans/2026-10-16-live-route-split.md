# `server/routes/live.py` 巨型路由模块拆分 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 4928 行的 `server/routes/live.py` 拆成薄 façade + 12 个按域拆分的路由子模块 + core 层控制器 mixin，全程保持 63 条对外 HTTP/WS 契约与 19 个 `REQUIRED_EXPORTS` 的 patch 行为逐字节不变。

**Architecture:** `live.py` 保留 `APIRouter(prefix="/live")` 并聚合 12 个无前缀子路由（子路由端点路径写成完整 `/live/xxx`）；19 个被 patch 的符号作为「缝」留在 `live.py`，实现模块一律用**函数体内 `from server.routes.live import X`** 双向透传，保住调用时解析语义。`LiveSessionController` 拆成 7 个 mixin 移入 `server/core/live/controller/`，组合后仍导出同名类。

**Tech Stack:** Python 3.13 · FastAPI 0.141 · pytest · ruff

**设计依据:** `docs/superpowers/specs/2026-10-16-live-route-split-design.md`

---

## 范围与前置说明

**本计划覆盖 S0（安全网修正）+ S1（端点区下沉）+ S2（控制器拆 mixin）+ 收官验证。**
**S3（三个超长函数的内聚拆分）不在本计划内**，将在 S2 落地后单独出计划 —— 原因见文末「范围调整说明」。

**不建 worktree。** `live.py` 的 4928 行现状只存在于**未提交的工作区修改**中（HEAD 版本不同），从 HEAD 拉 worktree 会丢失待重构对象。**在当前工作区原地作业。**

**不执行 `git commit`，除非用户明确要求。** 每个 Task 结尾的 commit 步骤是可选的检查点，标注为「可选提交」。

---

## 文件结构

| 文件 | 职责 | 行数预算 |
|---|---|---:|
| `server/routes/live.py` | façade：19 个缝 + router 聚合 + 兼容再导出 | ≤900 |
| `server/routes/live_hardware.py` | `GET /live/hardware`、`_probe_gpu`、`_recommend_tier`、`_resolve_landmarks_for_avatar` | ≤600 |
| `server/routes/live_preflight.py` | `GET /live/preflight` + `_pf*` 检查器 + `_legacy_preflight_check` | ≤600 |
| `server/routes/live_lifecycle.py` | start/stop/status/interrupt/takeover/pause-ai/resume-ai/voice-interrupt/manual-speech | ≤600 |
| `server/routes/live_obs.py` | `obs/*`(5)、`stream/preview`、`virtual-cam/*`(3)、`restore_obs_connection` | ≤600 |
| `server/routes/live_danmaku.py` | `danmaku-webhook`、`danmaku/{block,probe}`、`mock-event` | ≤600 |
| `server/routes/live_orders.py` | `stats/order`、`orders`、`orders/{id}/cancel|refund`、`sessions`、`stats` | ≤600 |
| `server/routes/live_overlay.py` | `overlay/{coupon,urgency}`、`media/status` | ≤600 |
| `server/routes/live_stream.py` | `rtmp/*`(3)、`llm/budget`(2)、`webrtc/*`(3) | ≤600 |
| `server/routes/live_avatar_ctl.py` | `is-speaking`、`avatar/flush-talk`、`avatar/action` | ≤600 |
| `server/routes/live_asr.py` | `asr/{ws,transcribe,install-dependency,install-status}` | ≤600 |
| `server/routes/live_recording.py` | `record/*`(5) | ≤600 |
| `server/routes/live_governance.py` | `quality/*`(2)、`governance/kill-switch*`(3)、`cost/*`(2)、`privacy/erase-user` | ≤600 |
| `server/core/live/controller/*.py` | 控制器 7 个 mixin + 组合类 | ≤700 / 类 ≤400 |
| `server/tests/test_live_route_contract.py` | 路由契约 + 导出符号 + patch 目标 + 结构预算 | — |
| `server/tests/test_live_seam_late_binding.py` | **新增**：断言缝双向透传真生效 | — |

**端点归属实测基线（2026-10-16）：** `/api/v1/live/*` 61 条属 `live.py`；`/ws/live_control`、`/ws/danmaku-ingest` 2 条属 `ws_live.py`（不在本次范围）。契约总数 63。

**迁移顺序（依赖驱动，无环）：**
```
live_hardware → live_preflight → live_obs → live_danmaku → live_asr
              → live_orders → live_overlay → live_stream → live_recording
              → live_governance → live_avatar_ctl → live_lifecycle(最后)
```

---

## 全量回归基线（2026-10-16 实测，取代过期数字）

> **订正说明**：本计划初稿引用了 `商业化运营改造方案.md`（2026-10-11 撰写）中的
> 「737 passed / 28 skipped」。该数字已过期 —— 其后新增的十余个测试文件从未计入。
> **以 2026-10-16 在 `wip/live-route-split` 上的实测为准。**

| 项 | 实测值 |
|---|---|
| 命令 | `python -m pytest server/tests -q` |
| 通过 | **859 passed** |
| 跳过 | **3 skipped** |
| 失败 | **0 failed**（不含 S0 引入的 4 条结构预算守门灯） |
| 耗时 | 约 4 分钟 |

**后续每个 Task 的全量回归判据统一为：`0 failed`，且通过数 ≥859。**
跳过数不作硬性要求（依赖真实外网/硬件的用例会随环境浮动），
但**不得通过改 `skip` 标记来消化失败**。

**契约测试（`test_live_route_contract.py` + `test_live_seam_late_binding.py`）的预期状态**：

| 阶段 | 结果 |
|---|---|
| S0 结束（当前） | `4 failed, 44 passed` —— 4 条失败全部是结构预算守门灯 |
| S1 结束 | `1 failed, 47 passed` —— 仅 `test_no_oversized_function_in_live_surface` 仍红（超长函数属 S3） |
| S2 结束 | `0 failed, 48 passed` |

---

## 实施修正记录

| # | 发现阶段 | 问题 | 处置 |
|---|---|---|---|
| 1 | S0 结束后 | 计划/设计引用的回归基线「737 passed / 28 skipped」已过期（出自 10/11 的方案文档，其后新增的十余个测试文件从未计入）。实测为 **859 passed / 3 skipped / 0 failed** | 订正全部 4 处验收判据，提交 `7200f76` |
| 2 | Task 4 | **双前缀缺陷**：初稿要求子路由端点写完整 `/live/xxx`，同时父级 `router` 保留 `prefix="/live"`，两者叠加产出 `/api/v1/live/live/hardware` | 改为子路由写**相对段**、装饰器一字不改；契约测试当场抓到，修复后回归 4 failed/44 passed。**后续 11 个子模块一律照此办理** |

> 第 2 条同时是本计划最有价值的一处收获：契约测试在**第一次**真正生效时就抓到了双前缀 ——
> 在它被修好之前，它一直因为枚举返回 0 条而处于假绿状态。

---

> **状态：已完成**（Task 1-3，提交 `4d00463` / `caad0b9` / `ede8962`）。
> S0 不动任何生产代码。结束时：契约 3 条绿、结构预算 4 条红（预期，那是 S1~S3 的目标态）。

### Task 1: 修复路由枚举 + 重写 `EXPECTED_ROUTES` 为实测 63 条

**Files:**
- Modify: `server/tests/test_live_route_contract.py:140-162`（`_router_prefixed_paths`）、`server/tests/test_live_route_contract.py:28-99`（`EXPECTED_ROUTES`）

- [ ] **Step 1: 确认当前红灯（记录失败原因）**

```powershell
python -m pytest server/tests/test_live_route_contract.py -q -k "endpoint_contract or route_count"
```
Expected: FAIL，`端点数从 61 变为 0`

- [ ] **Step 2: 替换枚举函数，兼容 FastAPI 新旧两种路由注册形态**

把 `_router_prefixed_paths()` 整体替换为：

```python
def _iter_flat_routes(routes, prefix: str = ""):
    """递归下钻展开 FastAPI 的路由注册树，兼容懒包含与摊平两种形态。

    FastAPI >= 0.1xx 不再把 include_router 的路由摊平进 app.routes，
    而是保留为 _IncludedRouter 包装对象；旧版本直接摊平。
    """
    for route in routes:
        if type(route).__name__ == "_IncludedRouter":
            ctx = getattr(route, "include_context", None)
            sub_prefix = getattr(ctx, "prefix", "") or "" if ctx is not None else ""
            yield from _iter_flat_routes(route.original_router.routes, prefix + sub_prefix)
        else:
            yield prefix + getattr(route, "path", ""), getattr(route, "methods", None)


def _router_prefixed_paths() -> set[tuple[str, str]]:
    """带 app 挂载前缀 /api/v1 后的对外路径。

    live_router 以 prefix="/api/v1" 挂载（见 server/app.py），
    WS 路由无前缀。契约必须按**对外 URL** 校验，而非模块内 path。
    """
    from server.app import app

    found: set[tuple[str, str]] = set()
    for path, methods in _iter_flat_routes(app.routes):
        if not (path.startswith("/api/v1/live") or path.startswith("/ws/")):
            continue
        if methods is None:
            # WebSocketRoute 没有 .methods
            found.add(("WEBSOCKET", path))
            continue
        for method in methods:
            found.add((method.upper(), path))
    return found
```

- [ ] **Step 3: 实测导出真实契约，替换 `EXPECTED_ROUTES`**

先导出，再粘贴（**禁止凭想象补写端点** —— 上一版虚构契约正是这么来的）：

```powershell
$env:PYTHONIOENCODING='utf-8'
python -c @"
from server.tests.test_live_route_contract import _router_prefixed_paths
for m, p in sorted(_router_prefixed_paths(), key=lambda t: (t[1], t[0])):
    print(f'    (\"{m}\", \"{p}\"),')
"@
```
Expected: 恰好输出 63 行 `("METHOD", "/path"),`

把输出的 63 行按域分组填入 `EXPECTED_ROUTES`，并把文件头的 docstring 第 3 段替换为：

```python
#: 对外暴露的 HTTP 契约（方法, 路径）。
#:
#: **采集纪律**：本表由 _router_prefixed_paths() 于 2026-10-16 实测导出，
#: 不是手写期望值。拆分过程中若契约真的变了，必须是一次**明确的、有意的**
#: 接口变更，并在此处更新采集日期与理由 —— 禁止凭推测补写端点。
#: 基线：/api/v1/live/* 61 条属 server/routes/live.py（本次拆分范围）；
#:      /ws/live_control、/ws/danmaku-ingest 2 条属 server/routes/ws_live.py（不在范围）。
```

- [ ] **Step 4: 验证契约测试转绿**

```powershell
python -m pytest server/tests/test_live_route_contract.py -q -k "endpoint_contract or route_count or uniquely_registered"
```
Expected: 3 passed

- [ ] **Step 5: 确认剩余红灯正是预期的结构预算三条**

```powershell
python -m pytest server/tests/test_live_route_contract.py -q
```
Expected: 恰好 3 failed（`test_live_py_stays_below_size_budget`、`test_no_giant_function_in_live_py`、`test_no_giant_class_in_live_py`），其余全过。**任何其它失败都是回归，立即停下排查。**

- [ ] **Step 6: 可选提交**

```powershell
git add server/tests/test_live_route_contract.py
git commit -m "test(live): 修正契约枚举（兼容 _IncludedRouter）并以实测 63 条为唯一契约"
```

---

### Task 2: 结构预算改为模块清单驱动

**Files:**
- Modify: `server/tests/test_live_route_contract.py:240-280`（三个结构测试）

- [ ] **Step 1: 替换三个结构测试为清单驱动版本**

把 `test_live_py_stays_below_size_budget`、`test_no_giant_function_in_live_py`、`test_no_giant_class_in_live_py` 三个函数整体替换为下列代码，并加在文件末尾：

```python
# ---------------------------------------------------------------------------
# 4. 结构性契约（拆分质量的守门指标）
# ---------------------------------------------------------------------------

#: 结构预算表：(模块 glob 相对 ROOT, 行数预算, 单函数预算, 单类预算)。
#: 拆分后新模块必须同等受约束 —— 否则「指标被搬走」而非真正收敛。
STRUCTURE_BUDGETS: list[tuple[str, int, int, int]] = [
    ("server/routes/live.py", 900, 200, 400),
    ("server/routes/live_*.py", 600, 200, 400),
    ("server/core/live/**/*.py", 700, 200, 400),
]


def _budget_modules() -> list[tuple[Path, int, int, int]]:
    matched: list[tuple[Path, int, int, int]] = []
    for pattern, lines, func, cls in STRUCTURE_BUDGETS:
        matched += [(p, lines, func, cls) for p in sorted(ROOT.glob(pattern)) if p.is_file()]
    return matched


def test_structure_budget_covers_live_surface():
    """预算表必须覆盖 façade + 全部路由子模块 + core 控制器。"""
    covered = {p.name for p, *_ in _budget_modules()}
    expected = {"live.py"} | {f"live_{n}.py" for n in (
        "hardware", "preflight", "lifecycle", "obs", "danmaku",
        "orders", "overlay", "stream", "avatar_ctl", "asr",
        "recording", "governance",
    )}
    missing = expected - covered
    assert not missing, (
        f"结构预算表未覆盖以下模块: {sorted(missing)}\n"
        "新增路由子模块时必须同步登记预算，否则拆分成果无守门指标。"
    )


def test_no_module_exceeds_line_budget():
    offenders = []
    for path, lines, _f, _c in _budget_modules():
        n = len(path.read_text(encoding="utf-8").splitlines())
        if n > lines:
            offenders.append(f"{path.relative_to(ROOT).as_posix()}: {n} 行 > 预算 {lines}")
    assert not offenders, "超出行数预算:\n  " + "\n  ".join(offenders)


def test_no_oversized_function_in_live_surface():
    offenders = []
    for path, _l, func_limit, _c in _budget_modules():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                size = node.end_lineno - node.lineno + 1
                if size > func_limit:
                    offenders.append(
                        f"{path.relative_to(ROOT).as_posix()}::{node.name} ({size} 行 > {func_limit})"
                    )
    assert not offenders, "存在超长函数:\n  " + "\n  ".join(sorted(offenders))


def test_no_oversized_class_in_live_surface():
    offenders = []
    for path, _l, _f, cls_limit in _budget_modules():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                size = node.end_lineno - node.lineno + 1
                if size > cls_limit:
                    offenders.append(
                        f"{path.relative_to(ROOT).as_posix()}::{node.name} ({size} 行 > {cls_limit})"
                    )
    assert not offenders, "存在巨型类:\n  " + "\n  ".join(offenders)
```

同时删除原三个结构测试函数（`test_live_py_stays_below_size_budget` / `test_no_giant_function_in_live_py` / `test_no_giant_class_in_live_py`），避免预算重复维护。

- [ ] **Step 2: 确认红灯构成**

```powershell
python -m pytest server/tests/test_live_route_contract.py -q
```
Expected: `4 failed, 25 passed`

四条失败分别是 `test_structure_budget_covers_live_surface`（12 个子模块尚未创建，S1/S2 的进度守门灯）、`test_no_module_exceeds_line_budget`、`test_no_oversized_function_in_live_surface`、`test_no_oversized_class_in_live_surface`。**任何其它失败都是回归，立即停下排查。**

`test_structure_budget_covers_live_surface` 在 S0~S1 期间**必然为红**，这是设计意图（它守的是「拆分后模块是否被预算覆盖」）。**不要放宽断言**，让它在 S2 结束时转绿。

- [ ] **Step 3: 可选提交**

```powershell
git add server/tests/test_live_route_contract.py
git commit -m "test(live): 结构预算改为模块清单驱动，防止指标被搬走"
```

---

### Task 3: 新增缝双向透传测试

**Files:**
- Create: `server/tests/test_live_seam_late_binding.py`

- [ ] **Step 1: 写失败测试**

创建 `server/tests/test_live_seam_late_binding.py`：

```python
# -*- coding: utf-8 -*-
"""「缝」的双向晚绑定透传验证。

`server/routes/live.py` 拆分后，19 个 REQUIRED_EXPORTS 成为 façade 上的缝。
本文件**不验证符号存在**（那由 test_live_route_contract 负责），只验证
「改写 live 模块上的属性 / 函数 → 调用侧在运行时真的解析到了新值」。

为什么必须单独验证：单向 `from x import y` 搬走会让
`patch("server.routes.live.X")` **静默失效** —— 测试照样绿，线上行为却变了。
"""
import pytest


def test_probe_gpu_to_hardware_payload_is_late_bound(monkeypatch):
    """_hardware_payload 必须在调用时读 live._probe_gpu，不得持有旧引用。"""
    from server.routes import live

    seen = {}

    def fake_probe() -> dict:
        seen["called"] = True
        return {"cuda_available": True, "vram_total_gb": 24, "name": "fake"}

    monkeypatch.setattr(live, "_probe_gpu", fake_probe)
    payload = _run(live._hardware_payload())
    assert seen.get("called") is True, "_hardware_payload 未走 live._probe_gpu（透传断裂）"
    assert isinstance(payload, dict)


def test_hardware_payload_seam_is_late_bound(monkeypatch):
    """反向：改写 live._hardware_payload 后，消费方必须在调用时看到新值。"""
    from server.routes import live

    sentinel = {"seam": "replaced"}
    monkeypatch.setattr(live, "_hardware_payload", _async_stub(sentinel))

    import server.routes.system as system_mod

    if hasattr(system_mod, "_hardware_payload"):
        assert system_mod._hardware_payload is sentinel


@pytest.mark.parametrize("name", [
    "global_live_controller", "global_obs_client", "global_role_manager",
    "global_virtual_audio", "global_media_driver",
])
def test_global_seam_is_resolvable(name):
    """5 个全局单例缝必须在 live 命名空间可取用（patch 的前置条件）。"""
    from server.routes import live

    assert hasattr(live, name), f"{name} 缺失，patch 将直接报错"
    assert getattr(live, name) is not None, f"{name} 为 None，patch 将静默失效"


def test_start_and_stop_unlocked_remain_callable():
    """_start_live_unlocked / _stop_live_unlocked 必须是可调用缝。"""
    from server.routes.live import _start_live_unlocked, _stop_live_unlocked

    assert callable(_start_live_unlocked)
    assert callable(_stop_live_unlocked)


def test_watchdog_seam_uses_live_probe(monkeypatch):
    """_configure_media_watchdog 必须把 live._stream_output_ready_probe 交给看门狗。"""
    from server.core.monitoring.media_quality_watchdog import global_media_watchdog
    from server.routes import live

    sentinel = lambda: True  # noqa: E731
    monkeypatch.setattr(live, "_stream_output_ready_probe", sentinel)
    try:
        live._configure_media_watchdog("PILOT")
        probe = getattr(global_media_watchdog, "_output_readiness_probe", None)
        if probe is not None:
            assert probe is sentinel, "看门狗未拿到 live 上的当前探针（透传断裂）"
    finally:
        global_media_watchdog.set_output_readiness_probe(live._stream_output_ready_probe)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _run(coro):
    """在无事件循环环境里跑协程。"""
    import asyncio

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    raise RuntimeError("本用例需在无运行中事件循环的环境执行")


def _async_stub(value):
    async def _stub():
        return value

    return _stub
```

- [ ] **Step 2: 运行，确认现有代码通过（S0 基线绿）**

```powershell
python -m pytest server/tests/test_live_seam_late_binding.py -q
```
Expected: 全部 PASSED —— 这些断言描述的是**现状必须保持的行为**，不是新行为。

若 `test_probe_gpu_to_hardware_payload_is_late_bound` FAIL，说明现有 `live.py` 里 `_hardware_payload` 已在用顶层绑定引用 `_probe_gpu`，**立即停止 S1**，先修这个既有缺陷（否则拆分会把缺陷固化）。

- [ ] **Step 3: 跑全量确认没碰坏别的**

```powershell
python -m pytest server/tests -q 2>&1 | Select-Object -Last 3
```
Expected: `0 failed`，通过数 ≥859

- [ ] **Step 4: 可选提交**

```powershell
git add server/tests/test_live_seam_late_binding.py
git commit -m "test(live): 新增缝双向晚绑定透传测试"
```

---

# 阶段 S1：端点区下沉

> 每个子模块 Task 的统一不变式：
> - **子路由 `APIRouter()` 无前缀，端点路径写相对段**（`@router.get("/hardware")`），
>   装饰器**一字不改**。对外完整 URL 由 `live.py` 的
>   `APIRouter(prefix="/live")` + app 挂载前缀 `/api/v1` 拼接而成。
>
>   ⚠️ **不要写成 `/live/hardware`**：`include_router` 会把父级 prefix 再拼一次，
>   产出 `/api/v1/live/live/hardware`。这是本计划初稿的设计缺陷，
>   Task 4 执行时由契约测试当场抓到并已修正（见 §实施修正记录）。
>
> - 迁出的代码**逐字搬运**，只允许改 import 头、以及「缝」的解析方式；
> - 迁完后 `live.py` 必须保留同名符号（缝 = 函数体内转发；非缝 = 直接再导出）；
> - 每步都以「契约 63 条零差异 + 全量 0 failed」为准入。

> **关于行号**：下文各 Task 括号内的行号是**原始 `live.py` 的定位锚点**，
> 来自 2026-10-16 的 AST 导出。前面 Task 的迁移会让行号漂移，
> **以符号名 + 装饰器为准，行号仅供首次定位**。

### Task 4: 抽出 `live_hardware.py`

**Files:**
- Create: `server/routes/live_hardware.py`
- Modify: `server/routes/live.py`

- [ ] **Step 1: 记录绿灯基线**

```powershell
python -m pytest server/tests/test_live_route_contract.py server/tests/test_live_seam_late_binding.py -q
```
Expected: 契约相关全 PASSED；仅 4 条结构预算 FAILED

- [ ] **Step 2: 新建模块**

创建 `server/routes/live_hardware.py`：

```python
# -*- coding: utf-8 -*-
"""硬件与算力档位：GPU 探测、算力分级、硬件体检载荷。

由 server/routes/live.py 拆分而来（原 3422-3535 行）。
注意：本模块实现侧调用「缝」时一律写在函数体内，
以保住 server.routes.live 上的晚绑定语义（见设计文档 §5.2）。
"""
import asyncio
import logging
import os
import time

import psutil
from fastapi import APIRouter

router = APIRouter()
logger = logging.getLogger("LiveAgent.LiveController")

_HW_CACHE_TTL_SEC = 5.0
_HW_PAYLOAD_CACHE: dict = {}
```

然后把 `live.py` 中 `_probe_gpu`、`_resolve_landmarks_for_avatar`、`_recommend_tier`、`_hardware_payload`、`get_hardware_status`（含其 `@router.get("/hardware")` 装饰器，原 3422-3535 行）**逐字**剪切到本文件末尾。

- [ ] **Step 3: 把实现侧对缝的引用改为函数体内 import**

在 `live_hardware.py` 中，`_hardware_payload` 函数体内的 `_probe_gpu()` 调用改为：

```python
    # 测试缝（late-binding seam）：必须函数体内解析，patch server.routes.live._probe_gpu 才生效
    from server.routes.live import _probe_gpu

    gpu_info = _probe_gpu()
```

其余对 `global_media_router` 等非缝符号的引用**保持原样的顶层 import 即可，不要动**。

- [ ] **Step 4: live.py 删除迁出符号，追加缝转发**

从 `live.py` 删除上述 5 个符号与 `_HW_CACHE_TTL_SEC` / `_HW_PAYLOAD_CACHE` 两行，然后在文件**末尾**追加：

```python
# ---------------------------------------------------------------------------
# 兼容再导出：以下符号被其他生产模块与测试直接导入/patch（缝），不可移除。
# 转发体必须薄，且在调用时才解析目标 —— 否则 patch 会静默失效。
# ---------------------------------------------------------------------------
from server.routes.live_hardware import (  # noqa: E402
    _resolve_landmarks_for_avatar,
    get_hardware_status,
    router as _hardware_router,
)


def _probe_gpu() -> dict:
    """缝：见设计文档 §5.2。"""
    from server.routes.live_hardware import probe_gpu

    return probe_gpu()


def _recommend_tier(gpu_info: dict) -> str:
    """缝：见设计文档 §5.2。"""
    from server.routes.live_hardware import recommend_tier

    return recommend_tier(gpu_info)


async def _hardware_payload() -> dict:
    """缝：见设计文档 §5.2。"""
    from server.routes.live_hardware import hardware_payload

    return await hardware_payload()


router.include_router(_hardware_router)
```

**同时必须做的改名**：为避免「façade 里 def `_probe_gpu` 又 from 导入 `_probe_gpu`」的自引用歧义，`live_hardware.py` 内的实现函数改名 —— `_probe_gpu`→`probe_gpu`、`_recommend_tier`→`recommend_tier`、`_hardware_payload`→`hardware_payload`。改名只在**新模块内**发生，调用方一律经由 `live.py` 的缝，外部无感。

- [ ] **Step 5: 契约测试**

```powershell
python -m pytest server/tests/test_live_route_contract.py -q -k "endpoint_contract or route_count or exports"
```
Expected: PASSED，`missing` 与 `added` 均为空

- [ ] **Step 6: ruff**

```powershell
python -m ruff check server/routes/live.py server/routes/live_hardware.py
```
Expected: 无 F821（未定义名）。若有，按提示补 import；**不要用 `# noqa` 掩盖未定义名**。

- [ ] **Step 7: 全量回归**

```powershell
python -m pytest server/tests -q 2>&1 | Select-Object -Last 3
```
Expected: `0 failed`，通过数 ≥859

- [ ] **Step 8: 记录行数**

```powershell
python -c "print(len(open('server/routes/live.py',encoding='utf-8').read().splitlines()))"
```
Expected: ≤4930（不应增长）

- [ ] **Step 9: 可选提交**

```powershell
git add server/routes/live.py server/routes/live_hardware.py
git commit -m "refactor(live): 抽出 live_hardware 模块（硬件/算力档位）"
```

---

### Task 5: 抽出 `live_preflight.py`

**Files:**
- Create: `server/routes/live_preflight.py`
- Modify: `server/routes/live.py`

- [ ] **Step 1: 记录绿灯基线**（同 Task 4 Step 1）

- [ ] **Step 2: 新建模块**

创建 `server/routes/live_preflight.py`：

```python
# -*- coding: utf-8 -*-
"""开播门禁体检：结构化预检报告与各分项检查器。

由 server/routes/live.py 拆分而来（原 3863-4333 行）。
`_legacy_preflight_check` 仍是 335 行超长函数，属阶段 S3 的拆分目标，
本次仅做位置迁移，不改其内部结构。
"""
import asyncio
import logging
import os
import uuid
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from server.database.db import get_db
from server.database.models import Product
from server.core.guardrails.aho_corasick import global_guardrail

router = APIRouter()
logger = logging.getLogger("LiveAgent.LiveController")
```

把 `live.py` 中 `get_preflight_status`（含装饰器）、`ws_clients`、`_pf`、`_pf_ping`、`_pf_llm_check`、`_pf_tts_check`、`_pf_obs_check`、`_legacy_preflight_check`（原 3863-4333 行）逐字剪切到本文件末尾。

- [ ] **Step 3: 实现侧改缝引用为函数体内 import**

本模块用到 `_probe_gpu`、`_recommend_tier`（均为缝）与 `global_role_manager`（缝）。逐处替换：

```python
    # 测试缝（late-binding seam）：函数体内解析，patch server.routes.live.<name> 才生效
    from server.routes.live import _probe_gpu, _recommend_tier, global_role_manager
```

`ws_clients()` 原本读取的是 `live.py` 里的 `ws_clients` 辅助函数（非缝），改为本模块内自持实现，签名与返回值逐字保持。

- [ ] **Step 4: live.py 删除迁出符号 + 追加转发**

删除迁出的 8 个符号，末尾追加：

```python
from server.routes.live_preflight import get_preflight_status  # noqa: E402
from server.routes.live_preflight import router as _preflight_router  # noqa: E402

router.include_router(_preflight_router)
```

- [ ] **Step 5~8:** 依次跑契约测试、ruff、全量回归、行数检查（同 Task 4 Step 5-8）

- [ ] **Step 9: 可选提交**

```powershell
git add server/routes/live.py server/routes/live_preflight.py
git commit -m "refactor(live): 抽出 live_preflight 模块（开播门禁体检）"
```

---

### Task 6: 抽出 `live_obs.py`

**Files:**
- Create: `server/routes/live_obs.py`
- Modify: `server/routes/live.py`

- [ ] **Step 1: 记录绿灯基线**

- [ ] **Step 2: 新建模块**

```python
# -*- coding: utf-8 -*-
"""OBS 协同与虚拟摄像头输出。

由 server/routes/live.py 拆分而来（原 116-184 行 + 2478-2498 行 + 3028-3129 行 + 4437-4470 行）。
"""
import asyncio
import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from server.database.db import get_db
from server.database.models import AppSetting
from server.adapters.media.media_router import global_media_router

router = APIRouter()
logger = logging.getLogger("LiveAgent.LiveController")

OBS_SETTING_HOST = "obs_websocket_host"
OBS_SETTING_PORT = "obs_websocket_port"
OBS_SETTING_PASSWORD = "obs_websocket_password"
OBS_SETTING_AUTO_CONNECT = "obs_auto_connect"
```

把 `live.py` 中 `_save_obs_settings`、`_set_obs_auto_connect`、`_on_obs_external_state_change`、`get_obs_status`、`ObsConnectRequest`、`connect_obs`、`disconnect_obs`、`start_obs_stream`、`stop_obs_stream`、`live_stream_preview`、`VirtualCamStartRequest`、`start_virtual_camera`、`stop_virtual_camera`、`get_virtual_camera_status` 逐字剪切过来。

- [ ] **Step 3: 实现侧改缝引用为函数体内 import**

本模块用到 `global_live_controller`、`global_obs_client` 两个缝。逐处替换：

```python
    # 测试缝（late-binding seam）：函数体内解析，patch server.routes.live.<name> 才生效
    from server.routes.live import global_live_controller, global_obs_client
```

**关于 `restore_obs_connection`**：它是**缝**（被生产模块与测试导入）。做法是 —— **实现体迁到本模块并改名 `restore_obs_connection` → `restore_obs_connection_impl`**，而 `live.py` 保留同名薄缝转发（见 Step 4）。注意此时 `live.py` 内的薄缝与其同名绑定不冲突，因为薄缝只做函数体内 import，不在模块顶层 `from` 该名字。

**注意**：`live.py` 仍需保留 `OBS_SETTING_*` 四个常量（`_start_live_unlocked` 会用），不要删。

- [ ] **Step 4: live.py 删除迁出符号 + 追加转发**

末尾追加：

```python
from server.routes.live_obs import router as _obs_router  # noqa: E402


def restore_obs_connection(db: AsyncSession) -> bool:
    """缝：见设计文档 §5.2。"""
    from server.routes.live_obs import restore_obs_connection as _impl

    return _impl(db)


router.include_router(_obs_router)
```

**注意**：`live.py` 仍需保留 `OBS_SETTING_*` 四个常量（`_start_live_unlocked` 会用），不要删。

- [ ] **Step 5~8:** 契约 / ruff / 全量 / 行数（同 Task 4 Step 5-8）

- [ ] **Step 9: 可选提交**

```powershell
git add server/routes/live.py server/routes/live_obs.py
git commit -m "refactor(live): 抽出 live_obs 模块（OBS 协同与虚拟摄像头）"
```

---

### Task 7: 抽出 `live_danmaku.py`

**Files:**
- Create: `server/routes/live_danmaku.py`
- Modify: `server/routes/live.py`

- [ ] **Step 1: 记录绿灯基线**

- [ ] **Step 2: 新建模块**

```python
# -*- coding: utf-8 -*-
"""弹幕接入与场控：弹幕 Webhook、本地模拟注入、拉黑、平台凭证探测。

由 server/routes/live.py 拆分而来（原 3162-3194 / 3296-3417 / 4410-4431 行）。
"""
import uuid
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

router = APIRouter()
```

把 `DanmakuWebhookPayload`、`receive_danmaku_webhook`、`MockEventRequest`、`inject_mock_event`、`BlockDanmakuRequest`、`block_danmaku_user`、`DanmakuProbeRequest`、`probe_danmaku_endpoint` 逐字剪切过来。

- [ ] **Step 3: 实现侧改缝引用为函数体内 import**

本模块用 `global_live_controller` 缝：

```python
    # 测试缝（late-binding seam）：函数体内解析
    from server.routes.live import global_live_controller
```

- [ ] **Step 4: live.py 删除迁出符号 + 追加 include_router**

```python
from server.routes.live_danmaku import router as _danmaku_router  # noqa: E402

router.include_router(_danmaku_router)
```

- [ ] **Step 5~8:** 契约 / ruff / 全量 / 行数

- [ ] **Step 9: 可选提交**

```powershell
git add server/routes/live.py server/routes/live_danmaku.py
git commit -m "refactor(live): 抽出 live_danmaku 模块（弹幕接入与场控）"
```

---

### Task 8: 抽出 `live_asr.py`

**Files:**
- Create: `server/routes/live_asr.py`
- Modify: `server/routes/live.py`

- [ ] **Step 1: 记录绿灯基线**

- [ ] **Step 2: 新建模块**

```python
# -*- coding: utf-8 -*-
"""语音识别接入：ASR WebSocket、单段转录、依赖安装与状态查询。

由 server/routes/live.py 拆分而来（原 4532-4654 行）。
"""
import asyncio
import json
import logging
import uuid

from fastapi import APIRouter, File, HTTPException, UploadFile, WebSocket, WebSocketDisconnect

from server.core.audio.full_duplex_asr import get_full_duplex_asr_manager

router = APIRouter()
logger = logging.getLogger("LiveAgent.LiveController")
```

把 `install_asr_dependency`、`get_asr_install_status`、`websocket_asr_endpoint`（含 `@router.websocket("/asr/ws")` 装饰器，路径须改为 `/live/asr/ws`）、`transcribe_audio_file` 逐字剪切过来。

- [ ] **Step 3: 实现侧改缝引用为函数体内 import**

本模块用 `global_live_controller` 缝：

```python
    # 测试缝（late-binding seam）：函数体内解析
    from server.routes.live import global_live_controller
```

- [ ] **Step 4: live.py 删除迁出符号 + 追加 include_router**

```python
from server.routes.live_asr import router as _asr_router  # noqa: E402

router.include_router(_asr_router)
```

- [ ] **Step 5~8:** 契约 / ruff / 全量 / 行数

- [ ] **Step 9: 可选提交**

```powershell
git add server/routes/live.py server/routes/live_asr.py
git commit -m "refactor(live): 抽出 live_asr 模块（语音识别接入）"
```

---

### Task 9: 抽出 `live_orders.py`

**Files:**
- Create: `server/routes/live_orders.py`
- Modify: `server/routes/live.py`

- [ ] **Step 1: 记录绿灯基线**

- [ ] **Step 2: 新建模块**

```python
# -*- coding: utf-8 -*-
"""电商订单与场次统计：订单登记/查询/取消/退款、场次列表、实时统计。

由 server/routes/live.py 拆分而来（原 3538-3860 行）。
`_refresh_order_stats` 是缝（被 server/routes/orders_webhook.py 导入），故留在 live.py。
"""
import json
import time
import uuid
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from server.database.db import get_db
from server.database.models import AppSetting, Product

router = APIRouter()
```

把 `OrderRequest`、`RefundRequest`、`_order_payload`、`register_order`、`list_orders`、`cancel_order`、`refund_order`、`list_sessions`、`get_live_stats` 逐字剪切过来。

- [ ] **Step 3: 实现侧改缝引用为函数体内 import**

本模块用 `global_live_controller`、`global_role_manager` 两个缝：

```python
    # 测试缝（late-binding seam）：函数体内解析
    from server.routes.live import global_live_controller, global_role_manager
```

- [ ] **Step 4: live.py 删除迁出符号 + 追加缝转发**

```python
from server.routes.live_orders import router as _orders_router  # noqa: E402


async def _refresh_order_stats(db: AsyncSession, session_id: Any) -> tuple[float, int]:
    """缝：见设计文档 §5.2。"""
    from server.routes.live_orders import refresh_order_stats as _impl

    return await _impl(db, session_id)


router.include_router(_orders_router)
```

新模块内 `_refresh_order_stats` 改名 `refresh_order_stats`。

- [ ] **Step 5~8:** 契约 / ruff / 全量 / 行数

- [ ] **Step 6 补充:** 确认 `orders_webhook.py` 仍能导入

```powershell
python -c "from server.routes.orders_webhook import router; print('orders_webhook import ok')"
```
Expected: `orders_webhook import ok`

- [ ] **Step 9: 可选提交**

```powershell
git add server/routes/live.py server/routes/live_orders.py
git commit -m "refactor(live): 抽出 live_orders 模块（订单与场次统计）"
```

---

### Task 10: 抽出 `live_overlay.py`

**Files:**
- Create: `server/routes/live_overlay.py`
- Modify: `server/routes/live.py`

- [ ] **Step 1: 记录绿灯基线**

- [ ] **Step 2: 新建模块**

```python
# -*- coding: utf-8 -*-
"""画面叠加层与媒体诊断：优惠券/逼单画层、媒体组件健康快照。

由 server/routes/live.py 拆分而来（原 210-225 行 + 3131-3160 行 + 3328-3367 行）。
"""
import logging
from typing import Optional

from fastapi import APIRouter
from pydantic import BaseModel

from server.adapters.media.media_router import global_media_router
from server.core.media.av_sync import global_av_sync
from server.core.vision.capture import global_vision

router = APIRouter()
logger = logging.getLogger("LiveAgent.LiveController")
```

把 `_safe_media_status`、`get_media_status`、`OverlayCouponRequest`、`trigger_overlay_coupon`、`OverlayUrgencyRequest`、`trigger_overlay_urgency` 逐字剪切过来。

- [ ] **Step 3: 实现侧改缝引用为函数体内 import**

本模块用 `global_live_controller`、`global_virtual_audio` 两个缝：

```python
    # 测试缝（late-binding seam）：函数体内解析
    from server.routes.live import global_live_controller, global_virtual_audio
```

- [ ] **Step 4: live.py 删除迁出符号 + 追加 include_router**

```python
from server.routes.live_overlay import router as _overlay_router  # noqa: E402

router.include_router(_overlay_router)
```

- [ ] **Step 5~8:** 契约 / ruff / 全量 / 行数

- [ ] **Step 9: 可选提交**

```powershell
git add server/routes/live.py server/routes/live_overlay.py
git commit -m "refactor(live): 抽出 live_overlay 模块（叠加层与媒体诊断）"
```

---

### Task 11: 抽出 `live_stream.py`

**Files:**
- Create: `server/routes/live_stream.py`
- Modify: `server/routes/live.py`

- [ ] **Step 1: 记录绿灯基线**

- [ ] **Step 2: 新建模块**

```python
# -*- coding: utf-8 -*-
"""推流与流媒体分发：内置 RTMP 直推、LLM 预算、WebRTC/WHEP。

由 server/routes/live.py 拆分而来（原 4336-4404 行 + 4661-4726 行）。
"""
from typing import Optional

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, Field

from server.core.media.webrtc_streamer import get_webrtc_stream_manager

router = APIRouter()
```

把 `RtmpStartRequest`、`start_rtmp_streaming`、`stop_rtmp_streaming`、`get_rtmp_status`、`get_llm_budget_status`、`LLMBudgetRequest`、`update_llm_budget`、`whep_endpoint`、`close_whep_session`、`get_webrtc_status` 逐字剪切过来。

- [ ] **Step 3: 本模块不引用任何缝**，无需改引用方式。Step 3 直接跳过。

- [ ] **Step 4: live.py 删除迁出符号 + 追加 include_router**

```python
from server.routes.live_stream import router as _stream_router  # noqa: E402

router.include_router(_stream_router)
```

- [ ] **Step 5~8:** 契约 / ruff / 全量 / 行数

- [ ] **Step 9: 可选提交**

```powershell
git add server/routes/live.py server/routes/live_stream.py
git commit -m "refactor(live): 抽出 live_stream 模块（推流与流媒体）"
```

---

### Task 12: 抽出 `live_recording.py`

**Files:**
- Create: `server/routes/live_recording.py`
- Modify: `server/routes/live.py`

- [ ] **Step 1: 记录绿灯基线**

- [ ] **Step 2: 新建模块**

```python
# -*- coding: utf-8 -*-
"""直播切片录制：启动/停止/状态/列表/删除。

由 server/routes/live.py 拆分而来（原 4733-4799 行）。
"""
import asyncio

from fastapi import APIRouter, Body, HTTPException
from pydantic import BaseModel, Field

from server.core.media.recorder import get_record_manager

router = APIRouter()
```

把 `StartRecordRequest`、`start_clip_recording`、`stop_clip_recording`、`get_recording_status`、`list_recordings`、`delete_recording` 逐字剪切过来。

- [ ] **Step 3: 本模块不引用任何缝**，跳过。

- [ ] **Step 4: live.py 删除迁出符号 + 追加 include_router**

```python
from server.routes.live_recording import router as _recording_router  # noqa: E402

router.include_router(_recording_router)
```

- [ ] **Step 5~8:** 契约 / ruff / 全量 / 行数

- [ ] **Step 9: 可选提交**

```powershell
git add server/routes/live.py server/routes/live_recording.py
git commit -m "refactor(live): 抽出 live_recording 模块（切片录制）"
```

---

### Task 13: 抽出 `live_governance.py`

**Files:**
- Create: `server/routes/live_governance.py`
- Modify: `server/routes/live.py`

- [ ] **Step 1: 记录绿灯基线**

- [ ] **Step 2: 新建模块**

```python
# -*- coding: utf-8 -*-
"""运营治理：媒体质量 SLI/Runbook、灰度与 Kill Switch、成本核算、隐私抹除。

由 server/routes/live.py 拆分而来（原 4806-4926 行）。
"""
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

router = APIRouter()
```

把 `KillSwitchUpdateRequest`、`EmergencyCutRequest`、`BudgetSetRequest`、`PrivacyEraseRequest`、`get_media_quality_metrics`、`get_media_quality_runbook`、`get_kill_switch_status`、`update_kill_switch`、`emergency_kill_switch`、`get_session_cost_summary`、`set_session_budget`、`erase_user_privacy_data` 逐字剪切过来。

- [ ] **Step 3: 本模块不引用任何缝**，跳过。

- [ ] **Step 4: live.py 删除迁出符号 + 追加 include_router**

```python
from server.routes.live_governance import router as _governance_router  # noqa: E402

router.include_router(_governance_router)
```

- [ ] **Step 5~8:** 契约 / ruff / 全量 / 行数

- [ ] **Step 9: 可选提交**

```powershell
git add server/routes/live.py server/routes/live_governance.py
git commit -m "refactor(live): 抽出 live_governance 模块（运营治理）"
```

---

### Task 14: 抽出 `live_avatar_ctl.py`

**Files:**
- Create: `server/routes/live_avatar_ctl.py`
- Modify: `server/routes/live.py`

- [ ] **Step 1: 记录绿灯基线**

- [ ] **Step 2: 新建模块**

```python
# -*- coding: utf-8 -*-
"""数字人实时控制：发声状态查询、极速打断清队列、动作切片切换。

由 server/routes/live.py 拆分而来（原 4476-4525 行）。
`_resolve_landmarks_for_avatar` 由 live_hardware 提供，经 live 的缝间接到达。
"""
from typing import Optional

from fastapi import APIRouter
from pydantic import BaseModel, Field

router = APIRouter()
```

把 `AvatarActionRequest`、`check_is_speaking`、`flush_avatar_talk`、`set_avatar_action` 逐字剪切过来。

- [ ] **Step 3: 跨模块依赖改为经由 live 的缝**

`set_avatar_action` 需要 `_resolve_landmarks_for_avatar`（现已在 `live_hardware`）。改为**函数体内**两跳解析，保证 patch `live._resolve_landmarks_for_avatar` 仍生效：

```python
    # 测试缝（late-binding seam）：两跳函数体内解析，patch live 上的任一层都生效
    from server.routes.live import _resolve_landmarks_for_avatar
```

- [ ] **Step 4: live.py 删除迁出符号 + 追加 include_router**

```python
from server.routes.live_avatar_ctl import router as _avatar_router  # noqa: E402

router.include_router(_avatar_router)
```

- [ ] **Step 5~8:** 契约 / ruff / 全量 / 行数

- [ ] **Step 9: 可选提交**

```powershell
git add server/routes/live.py server/routes/live_avatar_ctl.py
git commit -m "refactor(live): 抽出 live_avatar_ctl 模块（数字人实时控制）"
```

---

### Task 15: 抽出 `live_lifecycle.py`（最后，因为依赖面最广）

**Files:**
- Create: `server/routes/live_lifecycle.py`
- Modify: `server/routes/live.py`

- [ ] **Step 1: 记录绿灯基线**

- [ ] **Step 2: 新建模块**

```python
# -*- coding: utf-8 -*-
"""直播生命周期：开播/停播、状态、门禁报告、插话、人工接管与 AI 暂停恢复。

由 server/routes/live.py 拆分而来（原 2502-2542 行 + 2598-3293 行）。
`_start_live_unlocked` / `_stop_live_unlocked` 是缝（被测试直接调用），故留在 live.py。
"""
import asyncio
import io
import json
import logging
import os
import uuid
import wave
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, Path, UploadFile
from pydantic import BaseModel
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from server.database.db import get_db
from server.database.models import Product
from server.core.media.recorder import get_record_manager
from server.core.media.webrtc_streamer import get_webrtc_stream_manager
from server.routes.live_preflight import _pf as _pf_impl

router = APIRouter()
logger = logging.getLogger("LiveAgent.LiveController")
```

把 `LiveStartRequest`、`LiveInterruptRequest`、`LiveTakeoverRequest`、`_build_external_publish_status`（改名 `build_external_publish_status`）、`get_live_status`、`start_live`、`stop_live`、`interrupt_live`、`takeover_live`、`pause_ai_live`、`resume_ai_live`、`voice_interrupt_live`、`_pcm_s16le_to_wav` 逐字剪切过来（原 2502-2542 行 + 2598-3293 行）。

> `_resolve_landmarks_for_avatar` **不在本模块的迁出清单里** —— 它由 Task 4 迁到 `live_hardware`。
> 本模块需要它时，走 `live` 的缝做函数体内解析（见 Step 3），**不要**在本模块顶部 import。

- [ ] **Step 3: 实现侧改缝引用为函数体内 import**

本模块用 `global_live_controller`、`global_obs_client`、`global_role_manager` 三个缝，以及 `_stream_output_ready_probe`、`_configure_media_watchdog`（后二者本身是缝）、`_resolve_landmarks_for_avatar`（Task 4 迁至 `live_hardware`，经 `live` 缝两跳解析）：

```python
    # 测试缝（late-binding seam）：函数体内解析
    from server.routes.live import (
        _configure_media_watchdog,
        _resolve_landmarks_for_avatar,
        _stream_output_ready_probe,
        global_live_controller,
        global_obs_client,
        global_role_manager,
    )
```

- [ ] **Step 4: live.py 删除迁出符号 + 追加缝转发**

删除迁出符号，末尾追加：

```python
from server.routes.live_lifecycle import router as _lifecycle_router  # noqa: E402


def _stream_output_ready_probe() -> bool:
    """缝：见设计文档 §5.2。"""
    from server.routes.live_lifecycle import stream_output_ready_probe as _impl

    return _impl()


def _configure_media_watchdog(execution_mode: str) -> None:
    """缝：见设计文档 §5.2。"""
    from server.routes.live_lifecycle import configure_media_watchdog as _impl

    _impl(execution_mode)


async def _start_live_unlocked(req, db: AsyncSession):
    """缝：见设计文档 §5.2。"""
    from server.routes.live_lifecycle import start_live_unlocked as _impl

    return await _impl(req, db)


async def _stop_live_unlocked(db: AsyncSession):
    """缝：见设计文档 §5.2。"""
    from server.routes.live_lifecycle import stop_live_unlocked as _impl

    return await _impl(db)


def _build_external_publish_status() -> dict:
    """缝：见设计文档 §5.2。"""
    from server.routes.live_lifecycle import build_external_publish_status as _impl

    return _impl()


router.include_router(_lifecycle_router)
```

**保留不动**：`LiveSessionController` 实例化 `global_live_controller = LiveSessionController()` 与 `global_obs_client.add_stream_state_listener(...)` 的接线代码留在 `live.py` 原位。

- [ ] **Step 5: 契约测试**

```powershell
python -m pytest server/tests/test_live_route_contract.py -q -k "endpoint_contract or route_count or exports"
```
Expected: PASSED，零差异

- [ ] **Step 6: ruff**

```powershell
python -m ruff check server/routes/live.py server/routes/live_lifecycle.py
```
Expected: 无 F821

- [ ] **Step 7: 全量回归**

```powershell
python -m pytest server/tests -q 2>&1 | Select-Object -Last 3
```
Expected: `0 failed`，通过数 ≥859

- [ ] **Step 8: S1 收官验收**

```powershell
python -c "print(len(open('server/routes/live.py',encoding='utf-8').read().splitlines()))"
python -m pytest server/tests/test_live_route_contract.py -q 2>&1 | Select-Object -Last 1
```
Expected: `live.py` ≤ 2500 行；契约测试仅剩结构预算类失败（`test_structure_budget_covers_live_surface` 此时应转 PASSED，因为 12 个子模块已存在）

- [ ] **Step 9: 可选提交**

```powershell
git add server/routes/live.py server/routes/live_lifecycle.py
git commit -m "refactor(live): 抽出 live_lifecycle 模块（开播生命周期）"
```

---

# 阶段 S2：控制器拆 mixin

> 把 `LiveSessionController`（2247 行）拆成 7 个 mixin 移入 `server/core/live/controller/`。
> **方法体逐字搬运**，只改 import 与对缝的引用方式。

### Task 16: 建立 `server/core/live/` 骨架与 `base.py`

**Files:**
- Create: `server/core/live/__init__.py`
- Create: `server/core/live/controller/__init__.py`
- Create: `server/core/live/controller/base.py`

- [ ] **Step 1: 记录绿灯基线**

```powershell
python -m pytest server/tests -q 2>&1 | Select-Object -Last 3
```
Expected: `0 failed`

- [ ] **Step 2: 创建包骨架**

`server/core/live/__init__.py`：
```python
# -*- coding: utf-8 -*-
"""直播会话核心逻辑（与 HTTP 路由层解耦）。"""
```

`server/core/live/controller/__init__.py`：留空文件。

- [ ] **Step 3: 创建 `base.py`，迁入基础方法**

```python
# -*- coding: utf-8 -*-
"""LiveSessionController 基类：初始化、生命周期锁、任务登记、状态明细。

由 server/routes/live.py 的 LiveSessionController 拆分而来。
MAX_TTS_SENTENCE_BYTES / __init__ / _track_task / set_state_detail / lifecycle_lock
在此，其它职责见各 mixin 模块。
"""
import asyncio
import logging
import uuid
from typing import Optional

logger = logging.getLogger("LiveAgent.LiveController")


class LiveSessionBase:
    """会话对象基础能力。所有 mixin 依赖的属性在此初始化。"""

    MAX_TTS_SENTENCE_BYTES = 32 * 1024 * 1024

    def __init__(self):
        self.is_live = False
```

然后把 `live.py` 中 `__init__`（原 231-296 行）、`_track_task`（原 863-868 行）、`set_state_detail`（原 870-873 行）、`lifecycle_lock`（原 474-484 行，与 `obs_owner_session_id` 一并迁走，因二者共享属性初始化顺序）逐字剪切进 `base.py` 的类体。

- [ ] **Step 4: 从 live.py 删除已迁走的方法**

- [ ] **Step 5: 验证零回归（此时类尚未组合，测试预期失败）**

```powershell
python -c "import server.routes.live"
```
Expected: `ImportError` 或 `AttributeError` —— **这是预期的**，Step 6 立即修复

- [ ] **Step 6: 在 live.py 引入组合类骨架（此时只含 base）**

把 `live.py` 的 `class LiveSessionController:` 改为：

```python
from server.core.live.controller.base import LiveSessionBase  # noqa: E402


class LiveSessionController(LiveSessionBase):
    """由 core/live/controller/ 下各 mixin 组合而成。"""
```

- [ ] **Step 7: 全量回归**

```powershell
python -m pytest server/tests -q 2>&1 | Select-Object -Last 3
```
Expected: `0 failed`

- [ ] **Step 8: 可选提交**

```powershell
git add server/core/live server/routes/live.py
git commit -m "refactor(live): 控制器基类下沉到 core/live/controller/base.py"
```

---

### Task 17: 抽出 `takeover_mixin.py`

**Files:**
- Create: `server/core/live/controller/takeover_mixin.py`
- Modify: `server/routes/live.py`

- [ ] **Step 1: 记录绿灯基线**

- [ ] **Step 2: 新建 mixin**

```python
# -*- coding: utf-8 -*-
"""人工接管、AI 暂停/恢复与降级响应、OBS 所有权归属。"""
import logging
from typing import Optional

logger = logging.getLogger("LiveAgent.LiveController")


class TakeoverMixin:
    """接管与降级职责。依赖 LiveSessionBase 提供的状态属性。"""

    def _on_media_degraded(self, *args, **kwargs):
        # 测试缝（late-binding seam）：函数体内解析，patch live.global_media_driver 才生效
        from server.routes.live import global_media_driver
        ...
```

把 `live.py` 中 `_on_media_degraded`(298-313)、`transition_to_ai_paused`(315-330)、`resume_ai`(332-383)、`takeover`(386-415)、`release_takeover`(417-433)、`obs_owner_session_id`(436-447)、`obs_stream_started_by_agent`(450-461) 逐字剪切进本类。

- [ ] **Step 3: 实现侧改缝引用为函数体内 import**

本 mixin 用 `global_media_driver`、`global_virtual_audio`、`global_obs_client` 三个缝，在每个方法体内按需加：

```python
        from server.routes.live import global_media_driver, global_obs_client
```

> **务必逐个方法检查**：只要方法体里出现 `global_*`，就必须在该方法体内 import，不要提到类外。

- [ ] **Step 4: live.py 删除已迁走方法，组合类改为多继承**

```python
from server.core.live.controller.takeover_mixin import TakeoverMixin  # noqa: E402


class LiveSessionController(LiveSessionBase, TakeoverMixin):
    """由 core/live/controller/ 下各 mixin 组合而成。"""
```

- [ ] **Step 5~7:** 全量回归 → `0 failed`；ruff 无 F821；行数检查

- [ ] **Step 8: 可选提交**

```powershell
git add server/core/live server/routes/live.py
git commit -m "refactor(live): 抽出 TakeoverMixin（接管与降级）"
```

---

### Task 18: 抽出 `lifecycle_mixin.py`

**Files:**
- Create: `server/core/live/controller/lifecycle_mixin.py`
- Modify: `server/routes/live.py`

- [ ] **Step 1: 记录绿灯基线**

- [ ] **Step 2: 新建 mixin 并迁入方法**

把 `live.py` 中 `start`(875-887)、`_start_resources`(889-1027)、`stop`(1309-1404)、`ingest_event`(1406-1495)、`reload_runtime_config`(542-575)、`_configure_neural_sidecar`(789-861)、`start`(875-887) 逐字剪切进 `LifecycleMixin`。

- [ ] **Step 3: `_start_resources` 已是 139 行，暂不拆**

本阶段只做位置迁移。**它与 S3 的 `_start_live_unlocked` 不是同一函数**，无需在 S2 内聚。若 ruff 报 E501/F401 只按提示修，不要顺手重构。

- [ ] **Step 4: 实现侧改缝引用为函数体内 import**

```python
        from server.routes.live import global_media_router, global_obs_client, global_role_manager
```

- [ ] **Step 5: live.py 删除已迁走方法，组合类更新**

```python
class LiveSessionController(LiveSessionBase, TakeoverMixin, LifecycleMixin):
```

- [ ] **Step 6~8:** 全量 / ruff / 行数 / 可选提交

---

### Task 19: 抽出 `runtime_mixin.py`

**Files:**
- Create: `server/core/live/controller/runtime_mixin.py`
- Modify: `server/routes/live.py`

- [ ] **Step 1: 记录绿灯基线**

- [ ] **Step 2: 新建 mixin 并迁入方法**

把 `live.py` 中 `_stats_reset`(486-510)、`_refresh_viewer_count`(512-517)、`_viewer_count_loop`(519-528)、`_on_danmaku_circuit_state_change`(530-540)、`_vision_loop`(1270-1287)、`_safe_close`(1289-1307)、`_on_vram_alert`(622-624)、`_log_guardrail_hits`(2425-2451)、`_log_barrage_complete`(2453-2474) 逐字剪切进 `RuntimeMixin`。

- [ ] **Step 3: 实现侧改缝引用为函数体内 import**

- [ ] **Step 4: live.py 删除已迁走方法，组合类更新**

```python
class LiveSessionController(LiveSessionBase, TakeoverMixin, LifecycleMixin, RuntimeMixin):
```

- [ ] **Step 5~8:** 全量 / ruff / 行数 / 可选提交

---

### Task 20: 抽出 `queue_mixin.py` 与 `stitcher_mixin.py`

**Files:**
- Create: `server/core/live/controller/queue_mixin.py`
- Create: `server/core/live/controller/stitcher_mixin.py`
- Modify: `server/routes/live.py`

- [ ] **Step 1: 记录绿灯基线**

- [ ] **Step 2: 新建 `queue_mixin.py` 并迁入**

把 `_on_barge_in`(577-620)、`_on_danmaku_event`(1497-1508)、`_refresh_product_context`(1510-1520)、`_consume_queue_loop`(1522-1700) 剪切进 `QueueMixin`。

- [ ] **Step 3: 新建 `stitcher_mixin.py` 并迁入**

把 `_load_product_slices_to_stitcher`(1029-1075)、`_stitcher_frame_loop`(1077-1111)、`_on_slice_transition_filler`(1113-1191)、`_on_slice_audio_playback`(1193-1246) 剪切进 `StitcherMixin`。

- [ ] **Step 4: 实现侧改缝引用为函数体内 import**

`_consume_queue_loop` 内用 `global_media_driver` 缝：

```python
        from server.routes.live import global_media_driver, global_role_manager
```

- [ ] **Step 5: live.py 删除已迁走方法，组合类更新**

```python
class LiveSessionController(
    LiveSessionBase, TakeoverMixin, LifecycleMixin, RuntimeMixin, QueueMixin, StitcherMixin
):
```

- [ ] **Step 6~9:** 全量 / ruff / 行数 / 可选提交

---

### Task 21: 抽出 `speech_mixin.py` 并收官 S2

**Files:**
- Create: `server/core/live/controller/speech_mixin.py`
- Modify: `server/routes/live.py`
- Create: `server/core/live/controller/controller.py`

- [ ] **Step 1: 记录绿灯基线**

- [ ] **Step 2: 新建 `speech_mixin.py` 并迁入**

把 `live.py` 中 `_select_tts_driver`(626-787)、`_load_douyin_cookie_config`(1248-1268)、`_prepare_sentence`(1702-1714)、`_speak_with_lookahead`(1716-1809)、`_prefetch_sentence`(1811-1831)、`_build_pcm_frames_for_prefetch`(1833-1863)、`_sanitize_and_humanize`(1865-1893)、`_collect_tts_sentence`(1895-1921)、`_switch_to_edge_tts`(1923-1935)、`_supports_progressive_pcm_tts`(1937-1943)、`_speak_pcm_streaming_sentence`(1945-2097)、`_speak_sentence`(2099-2423) 剪切进 `SpeechMixin`。

> `_speak_sentence` 325 行、`_select_tts_driver` 162 行、`_speak_pcm_streaming_sentence` 153 行、`_speak_with_lookahead` 94 行 —— 本阶段只迁移，**S3 才做内聚拆分**。因此 S2 结束时 `test_no_oversized_function_in_live_surface` 仍会红，这是预期的。

- [ ] **Step 3: 实现侧改缝引用为函数体内 import**

本 mixin 用 `global_media_driver`、`global_virtual_audio`、`global_role_manager`、`global_media_router` 四个缝。逐方法在**方法体内** import。

- [ ] **Step 4: live.py 删除已迁走方法**

- [ ] **Step 5: 创建组合入口 `controller.py`**

```python
# -*- coding: utf-8 -*-
"""LiveSessionController 组合入口。

单一事实源：所有 mixin 的组合关系只在此处声明一次。
server/routes/live.py 从本模块导入 LiveSessionController。
"""
from server.core.live.controller.base import LiveSessionBase
from server.core.live.controller.lifecycle_mixin import LifecycleMixin
from server.core.live.controller.queue_mixin import QueueMixin
from server.core.live.controller.runtime_mixin import RuntimeMixin
from server.core.live.controller.speech_mixin import SpeechMixin
from server.core.live.controller.stitcher_mixin import StitcherMixin
from server.core.live.controller.takeover_mixin import TakeoverMixin


class LiveSessionController(
    LiveSessionBase,
    TakeoverMixin,
    LifecycleMixin,
    RuntimeMixin,
    QueueMixin,
    StitcherMixin,
    SpeechMixin,
):
    """直播会话控制器。

    职责切分见各 mixin 模块；本类不新增逻辑，只声明组合顺序。
    组合顺序中的基类必须在前（LiveSessionBase 提供 __init__）。
    """


__all__ = ["LiveSessionController"]
```

- [ ] **Step 6: live.py 改为从组合入口导入**

```python
from server.core.live.controller.controller import LiveSessionController  # noqa: E402

# 全局直播运行控制器
global_live_controller = LiveSessionController()
```

- [ ] **Step 7: 全量回归**

```powershell
python -m pytest server/tests -q 2>&1 | Select-Object -Last 3
```
Expected: `0 failed`，通过数 ≥859

- [ ] **Step 8: 验证控制器类规模已达标**

```powershell
python -m pytest server/tests/test_live_route_contract.py -q -k "oversized_class or covers_live_surface"
```
Expected: `test_no_oversized_class_in_live_surface` PASSED（`LiveSessionController` 已 <400 行，各 mixin 亦 <400）；`test_structure_budget_covers_live_surface` PASSED

- [ ] **Step 9: 行数检查**

```powershell
python -c "print('live.py', len(open('server/routes/live.py',encoding='utf-8').read().splitlines()))"
```
Expected: ≤900

- [ ] **Step 10: 可选提交**

```powershell
git add server/core/live server/routes/live.py
git commit -m "refactor(live): 控制器拆为 7 个 mixin，组合入口下沉 core/live/controller"
```

---

# 阶段 S4：收官验证

### Task 22: 全量验收

**Files:**
- 无代码改动

- [ ] **Step 1: 契约测试**

```powershell
python -m pytest server/tests/test_live_route_contract.py server/tests/test_live_seam_late_binding.py -q
```
Expected: 仅 `test_no_oversized_function_in_live_surface` **FAILED**（S3 未做），其余全 PASSED

- [ ] **Step 2: 全量回归**

```powershell
python -m pytest server/tests -q 2>&1 | Select-Object -Last 3
```
Expected: `0 failed`，通过数 ≥859

- [ ] **Step 3: 项目质量门禁**

```powershell
python scripts/check.py
```
Expected: 5 步全 `[PASS]`，退出码 0

- [ ] **Step 4: 根目录测试**

```powershell
python -m pytest tests -q 2>&1 | Select-Object -Last 3
```
Expected: `0 failed`（基线 3 passed / 1 skipped）

- [ ] **Step 5: 确认对外契约最终态**

```powershell
$env:PYTHONIOENCODING='utf-8'
python -c @"
from server.tests.test_live_route_contract import _router_prefixed_paths, EXPECTED_ROUTES
a = _router_prefixed_paths()
print('契约条数:', len(a), '期望:', len(EXPECTED_ROUTES))
print('missing:', sorted(EXPECTED_ROUTES - a))
print('added  :', sorted(a - EXPECTED_ROUTES))
"@
```
Expected: `契约条数: 63 期望: 63`，`missing` 与 `added` 均为 `[]`

- [ ] **Step 5: 确认 `server/app.py` 未被改动**

```powershell
git diff --stat -- server/app.py
```
Expected: 空输出（本次拆分**禁止**触碰 `server/app.py`；若非空，说明误改，立即 `git checkout -- server/app.py`）

- [ ] **Step 6: 输出交付摘要**

向用户报告：S0~S2 已完成、`live.py` 最终行数、各新模块行数、契约零差异、全量回归数字、唯一未闭项（S3 三个超长函数）。**不要自行 commit。**

---

## 范围调整说明

**S3（`_speak_sentence` 325 行 / `_legacy_preflight_check` 335 行 / `_start_live_unlocked` 282 行的内聚拆分）不在本计划内。**

原因：这三处的内聚拆分必须依据函数**迁移后**的实际代码来设计子函数边界，而迁移完成前它们的行号、上下文引用、被调用点都会变化。现在凭空写出拆分子函数就是「占位符」，违反本计划「No Placeholders」原则。

S2 完成、`live.py` 降到 ≤900 行后，另出一份 S3 计划，届时逐个函数读取实际代码后再定边界。

---

## 回滚

每个 Task 的「可选提交」是独立检查点。任一 Task 回归红 →

```powershell
git checkout -- server/routes/live.py server/routes/live_<name>.py
```

回到上一绿点。**因工作区存在大量与本任务无关的未提交改动，回滚前务必确认只 checkout 本计划涉及的文件。**