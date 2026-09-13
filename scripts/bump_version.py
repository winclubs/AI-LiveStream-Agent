"""多端版本号一键同步工具。

功能：
  1. 检查当前各文件版本号是否一致：
     python scripts/bump_version.py --check

  2. 一键递增/指定新版本号并同步写入所有元数据文件：
     python scripts/bump_version.py 1.8.1
"""
import argparse
import datetime
import json
import re
import sys
from pathlib import Path

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


REPO_ROOT = Path(__file__).resolve().parent.parent
VERSION_JSON = REPO_ROOT / "version.json"
PYPROJECT_TOML = REPO_ROOT / "pyproject.toml"
PACKAGE_JSON = REPO_ROOT / "apps" / "desktop-ui" / "package.json"
CONSOLE_JS = REPO_ROOT / "server" / "static" / "js" / "console.js"


def get_current_versions() -> dict[str, str]:
    versions: dict[str, str] = {}

    if VERSION_JSON.exists():
        try:
            data = json.loads(VERSION_JSON.read_text(encoding="utf-8"))
            versions["version.json"] = data.get("version", "")
        except Exception as e:
            versions["version.json"] = f"error: {e}"

    if PYPROJECT_TOML.exists():
        try:
            content = PYPROJECT_TOML.read_text(encoding="utf-8")
            match = re.search(r'(?m)^version\s*=\s*"([^"]+)"', content)
            versions["pyproject.toml"] = match.group(1) if match else "not found"
        except Exception as e:
            versions["pyproject.toml"] = f"error: {e}"

    if PACKAGE_JSON.exists():
        try:
            data = json.loads(PACKAGE_JSON.read_text(encoding="utf-8"))
            versions["package.json"] = data.get("version", "")
        except Exception as e:
            versions["package.json"] = f"error: {e}"

    if CONSOLE_JS.exists():
        try:
            content = CONSOLE_JS.read_text(encoding="utf-8")
            match = re.search(r'FRONTEND_VERSION\s*=\s*"([^"]+)"', content)
            versions["console.js"] = match.group(1) if match else "not found"
        except Exception as e:
            versions["console.js"] = f"error: {e}"

    return versions


def check_versions() -> bool:
    versions = get_current_versions()
    print("\n当前各配置项版本号：")
    for file, ver in versions.items():
        print(f"  - {file}: {ver}")

    unique_versions = {v for v in versions.values() if not v.startswith("error")}
    if len(unique_versions) == 1:
        print(f"\n[OK] 所有元数据版本一致: {next(iter(unique_versions))}")
        return True
    else:
        print("\n[ERROR] 发现版本号不一致，请使用本脚本同步！", file=sys.stderr)
        return False


def bump_version(new_version: str) -> None:
    # 严格校验语义化版本号格式 (如 1.8.0, 1.8.1-rc1)
    if not re.match(r"^\d+\.\d+\.\d+.*$", new_version):
        print(f"[ERROR] 错误: 版本号 '{new_version}' 不符合 SemVer 格式 (例: 1.8.1)", file=sys.stderr)
        sys.exit(1)

    today = datetime.date.today().isoformat()
    print(f"\n正在同步版本号至: {new_version} (发布日期: {today}) ...")

    # 1. 更新 version.json
    if VERSION_JSON.exists():
        v_data = json.loads(VERSION_JSON.read_text(encoding="utf-8"))
        old_v = v_data.get("version", "")
        v_data["version"] = new_version
        v_data["build_date"] = today
        VERSION_JSON.write_text(json.dumps(v_data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        print(f"  [OK] [version.json] {old_v} -> {new_version}")

    # 2. 更新 pyproject.toml
    if PYPROJECT_TOML.exists():
        content = PYPROJECT_TOML.read_text(encoding="utf-8")
        new_content, count = re.subn(
            r'(?m)^version\s*=\s*"[^"]+"',
            f'version = "{new_version}"',
            content,
            count=1,
        )
        if count > 0:
            PYPROJECT_TOML.write_text(new_content, encoding="utf-8")
            print(f"  [OK] [pyproject.toml] -> {new_version}")

    # 3. 更新 package.json
    if PACKAGE_JSON.exists():
        pkg_data = json.loads(PACKAGE_JSON.read_text(encoding="utf-8"))
        old_pkg_v = pkg_data.get("version", "")
        pkg_data["version"] = new_version
        PACKAGE_JSON.write_text(json.dumps(pkg_data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        print(f"  [OK] [apps/desktop-ui/package.json] {old_pkg_v} -> {new_version}")

    # 4. 更新 console.js 前端版本常量 (前端用它判断后端是否为残留旧进程, 漏更会导致误报)
    if CONSOLE_JS.exists():
        content = CONSOLE_JS.read_text(encoding="utf-8")
        new_content, count = re.subn(
            r'FRONTEND_VERSION\s*=\s*"[^"]+"',
            f'FRONTEND_VERSION = "{new_version}"',
            content,
            count=1,
        )
        if count > 0:
            CONSOLE_JS.write_text(new_content, encoding="utf-8")
            print(f"  [OK] [server/static/js/console.js] FRONTEND_VERSION -> {new_version}")
        else:
            print("  [WARN] console.js 未找到 FRONTEND_VERSION 常量, 请人工确认", file=sys.stderr)

    print("\n[OK] 版本号同步完毕！")


def main() -> None:
    parser = argparse.ArgumentParser(description="AI-LiveStream-Agent 版本号多端同步工具")
    parser.add_argument("version", nargs="?", help="要设定的新版本号 (例如: 1.8.1)")
    parser.add_argument("--check", action="store_true", help="仅检查当前版本一致性")

    args = parser.parse_args()

    if args.check or not args.version:
        consistent = check_versions()
        if not consistent and args.check:
            sys.exit(1)
        if not args.check:
            print("\n提示: 指定版本号可直接同步，例: python scripts/bump_version.py 1.8.1")
        return

    bump_version(args.version)


if __name__ == "__main__":
    main()
