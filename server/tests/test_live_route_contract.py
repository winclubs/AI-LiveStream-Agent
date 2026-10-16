# -*- coding: utf-8 -*-
"""
``server/routes/live.py`` 拆分契约守护 (characterization test)

本文件是 live.py 拆分重构的**行为安全网**。它必须在拆分**之前**就通过，
并在拆分**之后**继续通过 —— 任何一条契约被破坏，重构即为回归。

守护三类契约：
1. **路由契约**：63 个端点的 HTTP 方法 + 完整路径。
   拆分后端点可跨模块，但对外 URL 与方法**一个都不能变**。
2. **导出符号契约**：被其他路由模块与 20+ 测试文件导入的公共/半公共符号。
3. **patch 目标契约**：测试大量使用 ``patch("server.routes.live.<符号>")``，
   这些名字必须继续存在于 live 模块命名空间，否则 mock 全部失效
   （表现为「测试通过但线上行为不可信」——比测试直接报错更危险）。

拆分完成后，本文件应与拆分前**逐字节一致**地通过。

.. note::
   第 4 组结构性契约（模块行数 / 单函数 / 单类预算）由 ``STRUCTURE_BUDGETS``
   清单驱动，覆盖拆分后的全部新模块。拆分完成前它们**必然为红** —— 它们是
   目标态守门灯，不是回归线。
"""
import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]

#: 对外暴露的 HTTP 契约（方法, 路径）。
#:
#: **采集纪律**：本表由 ``_router_prefixed_paths()`` 于 2026-10-16 实测导出，
#: 不是手写期望值。拆分过程中若契约真的变了，必须是一次**明确的、有意的**
#: 接口变更，并在此处更新采集日期与理由 —— 禁止凭推测补写端点。
#:
#: 基线归属：``/api/v1/live/*`` 61 条属 ``server/routes/live.py``（本次拆分范围）；
#: ``/ws/live_control``、``/ws/danmaku-ingest`` 2 条属 ``server/routes/ws_live.py``
#: （不在拆分范围，但一并纳入校验以确保拆分未误伤 WS 路由）。
EXPECTED_ROUTES: set[tuple[str, str]] = {
    # —— 开播 / 直播生命周期（live_lifecycle.py）——
    ("POST", "/api/v1/live/start"),
    ("POST", "/api/v1/live/stop"),
    ("GET", "/api/v1/live/status"),
    ("POST", "/api/v1/live/interrupt"),
    ("POST", "/api/v1/live/manual-speech"),
    ("POST", "/api/v1/live/voice-interrupt"),
    ("POST", "/api/v1/live/takeover"),
    ("POST", "/api/v1/live/pause-ai"),
    ("POST", "/api/v1/live/resume-ai"),
    # —— 开播门禁体检（live_preflight.py）——
    ("GET", "/api/v1/live/preflight"),
    # —— OBS 协同 / 虚拟摄像头 / 预览（live_obs.py）——
    ("GET", "/api/v1/live/obs/status"),
    ("POST", "/api/v1/live/obs/connect"),
    ("POST", "/api/v1/live/obs/disconnect"),
    ("POST", "/api/v1/live/obs/stream/start"),
    ("POST", "/api/v1/live/obs/stream/stop"),
    ("GET", "/api/v1/live/stream/preview"),
    ("POST", "/api/v1/live/virtual-cam/start"),
    ("POST", "/api/v1/live/virtual-cam/stop"),
    ("GET", "/api/v1/live/virtual-cam/status"),
    # —— 硬件与算力档位（live_hardware.py）——
    ("GET", "/api/v1/live/hardware"),
    # —— 弹幕接入与场控（live_danmaku.py）——
    ("POST", "/api/v1/live/danmaku-webhook"),
    ("POST", "/api/v1/live/danmaku/block"),
    ("POST", "/api/v1/live/danmaku/probe"),
    ("POST", "/api/v1/live/mock-event"),
    # —— 语音识别（live_asr.py）——
    ("WEBSOCKET", "/api/v1/live/asr/ws"),
    ("POST", "/api/v1/live/asr/transcribe"),
    ("POST", "/api/v1/live/asr/install-dependency"),
    ("GET", "/api/v1/live/asr/install-status"),
    # —— 电商订单与场次统计（live_orders.py）——
    ("POST", "/api/v1/live/stats/order"),
    ("GET", "/api/v1/live/orders"),
    ("POST", "/api/v1/live/orders/{order_id}/cancel"),
    ("POST", "/api/v1/live/orders/{order_id}/refund"),
    ("GET", "/api/v1/live/sessions"),
    ("GET", "/api/v1/live/stats"),
    # —— 叠加层与媒体诊断（live_overlay.py）——
    ("POST", "/api/v1/live/overlay/coupon"),
    ("POST", "/api/v1/live/overlay/urgency"),
    ("GET", "/api/v1/live/media/status"),
    # —— 推流与流媒体（live_stream.py）——
    ("POST", "/api/v1/live/rtmp/start"),
    ("POST", "/api/v1/live/rtmp/stop"),
    ("GET", "/api/v1/live/rtmp/status"),
    ("GET", "/api/v1/live/llm/budget"),
    ("POST", "/api/v1/live/llm/budget"),
    ("POST", "/api/v1/live/webrtc/whep"),
    ("DELETE", "/api/v1/live/webrtc/whep/{session_id}"),
    ("GET", "/api/v1/live/webrtc/status"),
    # —— 数字人实时控制（live_avatar_ctl.py）——
    ("GET", "/api/v1/live/is-speaking"),
    ("POST", "/api/v1/live/avatar/flush-talk"),
    ("POST", "/api/v1/live/avatar/action"),
    # —— 切片录制（live_recording.py）——
    ("POST", "/api/v1/live/record/start"),
    ("POST", "/api/v1/live/record/stop"),
    ("GET", "/api/v1/live/record/status"),
    ("GET", "/api/v1/live/record/list"),
    ("DELETE", "/api/v1/live/record/{record_id}"),
    # —— 运营治理：质量 / 灰度 / 成本 / 隐私（live_governance.py）——
    ("GET", "/api/v1/live/quality/metrics"),
    ("GET", "/api/v1/live/quality/runbook"),
    ("GET", "/api/v1/live/governance/kill-switch"),
    ("POST", "/api/v1/live/governance/kill-switch"),
    ("POST", "/api/v1/live/governance/kill-switch/emergency-cut"),
    ("GET", "/api/v1/live/cost/summary"),
    ("POST", "/api/v1/live/cost/budget"),
    ("POST", "/api/v1/live/privacy/erase-user"),
    # —— WebSocket（server/routes/ws_live.py，不在拆分范围）——
    ("WEBSOCKET", "/ws/live_control"),
    ("WEBSOCKET", "/ws/danmaku-ingest"),
}

#: 必须继续从 ``server.routes.live`` 命名空间导出的符号。
#: 前 6 项被**其他生产模块**导入；其余被测试导入或 patch。
REQUIRED_EXPORTS = {
    # —— 生产依赖（改动会直接打断其他路由模块）——
    "router",
    "global_live_controller",
    "restore_obs_connection",
    "_hardware_payload",
    "_probe_gpu",
    "_refresh_order_stats",
    # —— 测试 patch 目标 ——
    "global_obs_client",
    "global_media_driver",
    "global_role_manager",
    "global_virtual_audio",
    "LiveSessionController",
    "LiveStartRequest",
    "_start_live_unlocked",
    "_stop_live_unlocked",
    "_configure_media_watchdog",
    "_stream_output_ready_probe",
    "_build_external_publish_status",
    "_recommend_tier",
    "get_live_status",
}


def _iter_flat_routes(routes, prefix: str = ""):
    """递归下钻展开 FastAPI 的路由注册树，兼容懒包含与摊平两种形态。

    FastAPI >= 0.1xx 不再把 include_router 的路由摊平进 ``app.routes``，
    而是保留为 ``_IncludedRouter`` 包装对象；旧版本直接摊平。
    两种形态都必须展开，否则契约校验会静默看到 0 条端点（假绿）。
    """
    for route in routes:
        if type(route).__name__ == "_IncludedRouter":
            ctx = getattr(route, "include_context", None)
            sub_prefix = (getattr(ctx, "prefix", "") or "") if ctx is not None else ""
            yield from _iter_flat_routes(route.original_router.routes, prefix + sub_prefix)
        else:
            yield prefix + getattr(route, "path", ""), getattr(route, "methods", None)


def _router_prefixed_paths() -> set[tuple[str, str]]:
    """带 app 挂载前缀 /api/v1 后的对外路径。

    live_router 以 ``prefix="/api/v1"`` 挂载（见 server/app.py），
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


# ---------------------------------------------------------------------------
# 1. 路由契约
# ---------------------------------------------------------------------------

def test_live_router_endpoint_contract_is_unchanged():
    """对外 HTTP/WS 契约必须与拆分前完全一致。"""
    actual = _router_prefixed_paths()
    expected = EXPECTED_ROUTES

    missing = expected - actual
    added = actual - expected

    assert not missing, f"拆分丢失了以下端点:\n  {sorted(missing)}"
    assert not added, (
        f"拆分引入了计划外端点:\n  {sorted(added)}\n"
        "  若为有意的新增接口，请同步更新 EXPECTED_ROUTES 并在此说明理由。"
    )


def test_live_route_count_is_stable():
    """端点总数不得漂移（防止漏挂载或重复注册）。"""
    actual = _router_prefixed_paths()
    assert len(actual) == len(EXPECTED_ROUTES), (
        f"端点数从 {len(EXPECTED_ROUTES)} 变为 {len(actual)}"
    )


def test_all_routes_uniquely_registered():
    """同一 (方法, 路径) 不得重复注册（FastAPI 会静默取首个，掩盖冲突）。"""
    from server.app import app

    seen: dict[tuple[str, str], int] = {}
    for path, methods in _iter_flat_routes(app.routes):
        if not methods:
            continue
        if not (path.startswith("/api/v1/live") or path.startswith("/ws/")):
            continue
        for method in methods:
            key = (method.upper(), path)
            seen[key] = seen.get(key, 0) + 1

    dupes = {k: v for k, v in seen.items() if v > 1}
    assert not dupes, f"存在重复注册的端点（后者永远不会被命中）: {dupes}"


# ---------------------------------------------------------------------------
# 2. 导出符号契约
# ---------------------------------------------------------------------------

def test_required_exports_still_available():
    """被其他模块与测试依赖的符号必须继续存在于 live 命名空间。"""
    import server.routes.live as live

    missing = sorted(name for name in REQUIRED_EXPORTS if not hasattr(live, name))
    assert not missing, (
        f"拆分后以下符号从 server.routes.live 消失:\n  {missing}\n"
        "  这些符号被其他路由模块或测试直接导入/patch，消失会造成导入错误或"
        "（更糟）mock 静默失效。"
    )


@pytest.mark.parametrize("name", sorted(REQUIRED_EXPORTS))
def test_export_is_not_none(name):
    """导出的符号不得为 None（patch 一个 None 是静默失效）。"""
    import server.routes.live as live

    assert getattr(live, name) is not None, f"{name} 导出为 None，patch 将静默失效"


# ---------------------------------------------------------------------------
# 3. 结构性契约（拆分质量的守门指标）
# ---------------------------------------------------------------------------

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

#: 拆分完成后必须存在的路由子模块。与 live.py 的端点分组一一对应。
EXPECTED_SUBMODULES = (
    "hardware", "preflight", "lifecycle", "obs", "danmaku",
    "orders", "overlay", "stream", "avatar_ctl", "asr",
    "recording", "governance",
)


def _budget_modules() -> list[tuple[Path, int, int, int]]:
    matched: list[tuple[Path, int, int, int]] = []
    for pattern, lines, func, cls in STRUCTURE_BUDGETS:
        matched += [(p, lines, func, cls) for p in sorted(ROOT.glob(pattern)) if p.is_file()]
    return matched


def test_structure_budget_covers_live_surface():
    """预算表必须覆盖 façade + 全部路由子模块 + core 控制器。

    拆分完成前本测试为红（子模块尚未创建），这是设计意图：
    它是 S1/S2 的进度守门灯。**不得放宽断言**来「让它变绿」。
    """
    covered = {p.name for p, *_ in _budget_modules()}
    expected = {"live.py"} | {f"live_{n}.py" for n in EXPECTED_SUBMODULES}
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
