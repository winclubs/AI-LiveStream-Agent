"""跨平台统一质量与合规检查脚本。

运行方式：
    python scripts/check.py
"""
import os
import shutil
import subprocess
import sys
import tempfile
import uuid

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
if hasattr(sys.stderr, "reconfigure"):
    try:
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass


def run_step(step_name: str, cmd: list[str], env: dict[str, str] | None = None) -> None:
    print(f"\n==> {step_name} ...")
    res = subprocess.run(cmd, env=env)
    if res.returncode != 0:
        print(f"[FAIL] {step_name} 检查未通过 (退出码: {res.returncode})", file=sys.stderr)
        sys.exit(res.returncode)
    print(f"[PASS] {step_name} 通过")


def main() -> None:
    # 确保当前在根目录
    repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    os.chdir(repo_root)

    # 1. 编译语法
    run_step("1/5 Python 全量语法编译检查", [sys.executable, "-m", "compileall", "-q", "launcher.py", "scripts", "server"])

    # 2. Ruff 风格
    run_step("2/5 Ruff 代码风格与质量检查", [sys.executable, "-m", "ruff", "check", "launcher.py", "scripts/", "server/"])

    # 3. Mypy 检查
    run_step(
        "3/5 Mypy 核心架构类型检查",
        [sys.executable, "-m", "mypy", "server/core/queue/priority_queue.py", "server/core/cpu_worker.py"],
    )

    # 4. Node 前端语法
    run_step("4/5 Node.js 前端脚本语法检查", ["node", "--check", "apps/desktop-ui/main.js", "server/static/js/console.js"])

    # 5. Pytest 全量单测与覆盖率
    temp_dir = os.path.join(tempfile.gettempdir(), f"ai-live-ci-{uuid.uuid4().hex}")
    test_env = os.environ.copy()
    test_env["LIVE_AGENT_DATA_DIR"] = temp_dir

    try:
        run_step(
            "5/5 Pytest 自动化测试与分支覆盖率校验",
            [sys.executable, "-m", "pytest", "-q", "server/tests", "--cov=server", "--cov-report=term"],
            env=test_env,
        )
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)

    print("\n========================================")
    print("[SUCCESS] 全部质量检查通过！代码可安全提交发布。")
    print("========================================")


if __name__ == "__main__":
    main()
