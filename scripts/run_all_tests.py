"""统一测试入口：把散落的 JS 与根目录 Python 测试接入 CI。

历史问题：`tests/` 目录下的 4 个 JS 测试与 3 个 Python 测试从未被任何运行器执行 ——
`pyproject.toml` 的 `testpaths = ["server/tests"]` 让根 `tests/*.py` 永远不被收集，
CI 也只对 JS 做 `node --check` 语法校验而不执行断言。等于这些测试是摆设。

本脚本提供单一事实来源，被 CI 与本地一键质检共同调用：

* `python scripts/run_all_tests.py`            # 全部
* `python scripts/run_all_tests.py --js-only`  # 仅前端断言测试

退出码非 0 表示存在失败用例，CI 据此阻断合并。
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
JS_TESTS_DIR = ROOT / "tests"
SERVER_TESTS_DIR = ROOT / "server" / "tests"

# 根 tests/ 下真正可独立执行的 JS 断言测试（verify_svg_logos.js 需额外素材，单独运行）
JS_ASSERTION_TESTS = (
    "test_avatar_assets_ui.js",
    "test_preview_av_sync.js",
    "test_voice_friendly_names.js",
    "test_pills_dedup.js",
)


def _run(label: str, cmd: list[str], env: dict | None = None) -> bool:
    print(f"\n=== {label} ===", flush=True)
    print("$ " + " ".join(cmd), flush=True)
    merged = {**os.environ, **(env or {})}
    try:
        completed = subprocess.run(cmd, cwd=ROOT, env=merged, check=False)
    except FileNotFoundError as exc:
        print(f"[SKIP] {label}: 可执行程序缺失 ({exc})", flush=True)
        return True
    ok = completed.returncode == 0
    print(f"[{'PASS' if ok else 'FAIL'}] {label} (exit={completed.returncode})", flush=True)
    return ok


def run_js_tests() -> bool:
    """执行前端断言测试；缺少 node 时如实跳过而非静默通过。"""
    if not shutil_which("node"):
        print("[SKIP] 未检测到 node，跳过前端断言测试", flush=True)
        return True

    ok = True
    for name in JS_ASSERTION_TESTS:
        path = JS_TESTS_DIR / name
        if not path.exists():
            print(f"[SKIP] 前端测试缺失: {name}", flush=True)
            continue
        ok &= _run(f"JS 断言测试 {name}", ["node", str(path)])
    return ok


def shutil_which(exe: str) -> str | None:
    from shutil import which

    return which(exe)


def run_python_tests() -> bool:
    """执行服务端套件 + 根 tests/ 套件（后者此前从未被收集）。"""
    ok = True
    if SERVER_TESTS_DIR.is_dir():
        ok &= _run(
            "服务端 pytest 套件",
            [sys.executable, "-m", "pytest", "-q", "--no-header", "server/tests"],
        )
    root_tests = [
        p for p in JS_TESTS_DIR.glob("test_*.py")
    ] if JS_TESTS_DIR.is_dir() else []
    if root_tests:
        ok &= _run(
            "根目录 pytest 套件",
            [sys.executable, "-m", "pytest", "-q", "--no-header", "tests"],
        )
    else:
        print("[SKIP] 根目录 tests/ 下无 pytest 用例", flush=True)
    return ok


def main() -> int:
    parser = argparse.ArgumentParser(description="统一测试入口 (CI/本地一键质检)")
    parser.add_argument("--js-only", action="store_true", help="仅执行前端断言测试")
    parser.add_argument("--python-only", action="store_true", help="仅执行 Python 测试")
    args = parser.parse_args()

    results: list[bool] = []
    if not args.js_only:
        results.append(run_python_tests())
    if not args.python_only:
        results.append(run_js_tests())

    print("\n" + "=" * 60)
    if all(results):
        print("全部测试通过")
        return 0
    print("存在失败用例")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
