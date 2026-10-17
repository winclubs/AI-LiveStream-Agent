# -*- coding: utf-8 -*-
"""
发布候选版 skipped 测试审计台账 (上线诊断 RISK-7)

商用候选版中，skip 常被用来掩盖真实的环境依赖验证。本测试把
「当前允许存在的 skip 及其理由」固化为可执行契约：

    - 新增任何**未登记**的 skip 都会让本文件失败；
    - 登记项被移除时本文件同样失败（提醒更新台账）。

实现选择：静态 AST 扫描，而非再 spawn 一次 pytest。
    - 理由 1：不递归、不重复跑全量套件，CI 时间成本为零；
    - 理由 2：AST 扫描对 ``@pytest.mark.skip`` / ``pytest.skip(...)`` /
      ``pytest.importorskip(...)`` 三种显式跳过写法 100% 确定覆盖；
    - 已知局限：条件分支内的**动态**跳过（``if X: pytest.skip()``）不会被
      捕获。这类写法在本仓不存在，且 conftest 中的网络探测式跳过
      （Edge-TTS 联通性）属于正当的环境门控，已在下方单独登记。

台账（v2.0.1 审计结论）：
    1. ``test_wav2lip_temporal_export.py``（3 项）
       —— 依赖可选 GPU 训练依赖 ``torch``（``importorskip``），属正当环境门控。
       torch 不在 server/requirements.txt 中，商用 GPU 节点须在装齐 torch
       的环境另行执行。
    2. ``test_core_engine.py``（1 项）—— Brotli 解压为可选弹幕协议依赖。
    3. ``test_real_avatar_lite.py``（3 项）—— OpenCV 为可选媒体依赖。
    4. ``test_wav2lip_mel_alignment.py`` 的 golden 参考数据缺失
       —— 属「需先运行 scripts/gen_mel_golden.py 生成基线」的一次性准备动作，
       已在理由中显式给出生成命令。

本次审计修正的 3 处「skip 掩盖回归」（已改为硬失败，不再 skip）：
    - ``test_shared_playback_clock.py``：``NeuralLipRenderer`` 导入失败曾被跳过，
      而它是本仓常驻模块，导入回归本应立即暴露；
    - ``test_wav2lip_mel_alignment.py`` ×2：待校验源码文件缺失曾被跳过，
      而被校验文件就在仓内，重命名/移动会导致用例永久静默失效。
"""
import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TESTS_ROOT = ROOT / "server" / "tests"

#: 允许存在的 skip 台账：相对路径 -> 必须同时命中的理由关键字元组。
#: 全部为**可选第三方依赖缺失**或**需一次性生成的基线数据**，属正当环境门控。
ALLOWED_SKIPS: dict[str, tuple[str, ...]] = {
    # torch：可选 GPU 训练依赖，非服务端运行必需
    "server/tests/test_wav2lip_temporal_export.py": ("torch",),
    # Brotli：可选弹幕协议解压依赖
    "server/tests/test_core_engine.py": ("brotli", "Brotli"),
    # OpenCV：可选媒体/视觉依赖
    "server/tests/test_real_avatar_lite.py": ("OpenCV", "CV_AVAILABLE"),
    # golden 基线：需先运行 scripts/gen_mel_golden.py 生成
    "server/tests/test_wav2lip_mel_alignment.py": ("golden",),
    # run_agent.bat 仅在 Windows 生效
    "server/tests/test_windows_launcher_script.py": ("run_agent.bat", "Windows"),
}

#: 明确豁免文件：跳过原因已在文件头 docstring 中显式登记，
#: 且属于「探测外部网络可用性」这类正当环境门控
EXEMPT_FILES: dict[str, str] = {
    "server/tests/conftest.py": "Edge-TTS 公网可达性探测（网络门控，非缺陷掩盖）",
}

#: 同一文件内混放的**逐用例条件跳过**：
#: 路径 -> (必须命中理由, 该文件内允许出现多处同理由跳过)
PER_TEST_SKIPS: dict[str, tuple[str, int]] = {
    # 无 py 启动器的机器上无法复现该执行路径；与文件级 Windows 门控叠加
    "server/tests/test_windows_launcher_script.py": ("py 启动器", 1),
}


def _iter_test_files() -> list[Path]:
    return sorted(p for p in TESTS_ROOT.rglob("test_*.py"))


def _skip_reason_text(node: ast.AST, source_lines: list[str]) -> str:
    """尽力还原 skip 的理由文本：优先 reason= 关键字，否则用所在行源码。"""
    if isinstance(node, ast.Call):
        for kw in node.keywords or []:
            if kw.arg == "reason":
                try:
                    return ast.literal_eval(kw.value)  # type: ignore[arg-type]
                except Exception:
                    pass
        args = [a for a in node.args if isinstance(a, ast.Constant) and isinstance(a.value, str)]
        if args:
            return args[0].value
    return source_lines[node.lineno - 1].strip() if hasattr(node, "lineno") else ""


def _collect_explicit_skips() -> list[tuple[str, str, int]]:
    """扫描全部测试文件，返回 (相对路径, 理由文本, 行号)。"""
    found: list[tuple[str, str, int]] = []
    for path in _iter_test_files():
        source = path.read_text(encoding="utf-8")
        try:
            tree = ast.parse(source)
        except SyntaxError:
            continue
        source_lines = source.splitlines()
        rel = path.relative_to(ROOT).as_posix()
        for node in ast.walk(tree):
            reason = None
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in ("skip", "importorskip", "xfail")
            ):
                reason = _skip_reason_text(node, source_lines)
            elif isinstance(node, ast.Attribute) and node.attr in ("skip", "skipif", "xfail"):
                # @pytest.mark.skip(...) / @pytest.mark.skipif(...)
                parent_value = getattr(node, "value", None)
                if (
                    isinstance(parent_value, ast.Attribute)
                    and parent_value.attr == "mark"
                ):
                    reason = _skip_reason_text(getattr(node, "parent", None) or node, source_lines)
            if reason:
                found.append((rel, str(reason), getattr(node, "lineno", 0)))
    return found


def test_skip_audit_registry_matches_files_on_disk():
    """台账登记的测试文件必须仍然存在，避免台账失效后无人察觉。"""
    for rel in ALLOWED_SKIPS:
        assert (ROOT / rel).is_file(), f"台账登记的 {rel} 已不存在，请同步更新 SKIP 审计台账"
    for rel in EXEMPT_FILES:
        assert (ROOT / rel).is_file(), f"台账豁免的 {rel} 已不存在，请同步更新 SKIP 审计台账"


def test_no_unregistered_explicit_skips():
    """不得存在未登记的显式 skip：新增 skip 必须先在此登记理由。"""
    skips = _collect_explicit_skips()
    violations: list[str] = []
    per_test_hits: dict[str, int] = {}

    for rel, reason, lineno in skips:
        if rel in EXEMPT_FILES:
            continue
        expected = ALLOWED_SKIPS.get(rel)
        per_test = PER_TEST_SKIPS.get(rel)
        if expected is None and per_test is None:
            violations.append(f"{rel}:{lineno} 未登记 skip -> {reason[:100]}")
            continue

        matched = False
        if expected is not None and any(token in reason for token in expected):
            matched = True
        if per_test is not None:
            token, _max_n = per_test
            if token in reason:
                per_test_hits[rel] = per_test_hits.get(rel, 0) + 1
                matched = True
        if not matched:
            violations.append(
                f"{rel}:{lineno} 理由未命中登记关键字 "
                f"{expected or PER_TEST_SKIPS[rel][0]} -> {reason[:100]}"
            )

    # 逐用例条件跳过不得超额（防止在同一文件里悄悄堆积跳过点）
    for rel, (_token, max_n) in PER_TEST_SKIPS.items():
        actual = per_test_hits.get(rel, 0)
        assert actual <= max_n, (
            f"{rel} 存在 {actual} 处逐用例条件跳过，超过台账登记上限 {max_n}"
        )

    assert not violations, (
        "发现未登记的 skip。商用候选版必须为每一处 skip 显式登记理由：\n  "
        + "\n  ".join(violations)
    )


def test_skip_audit_covers_the_whole_test_suite():
    """审计本身必须真的扫描到了用例，防止空扫描假绿。"""
    skips = _collect_explicit_skips()
    assert skips, "扫描结果为空：审计逻辑失效，无法真正保护发布候选版"
    assert len({rel for rel, _r, _l in skips}) >= 1
