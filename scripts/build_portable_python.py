#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
构建内置绿色便携 Python 运行时 (供 Electron 桌面版打包引用)

原理：
1. 以当前解释器为母本，使用 pip install --target 安装全部依赖到独立目录
2. 用 Python 官方 embeddable 包 (Windows) 或 venv 复制 (跨平台兜底) 组装最小运行时
3. 产出到 apps/desktop-ui/resources/python/，electron-builder 会将其并入 extraResources

使用：
    python scripts/build_portable_python.py            # 完整构建
    python scripts/build_portable_python.py --clean    # 清理旧产物后构建
    python scripts/build_portable_python.py --check     # 仅校验产物可用性
"""

import argparse
import os
import shutil
import subprocess
import sys
import urllib.request
import zipfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
OUTPUT_DIR = PROJECT_ROOT / "apps" / "desktop-ui" / "resources" / "python"
EMBED_VERSION = "3.12.10"
EMBED_URL = f"https://www.python.org/ftp/python/{EMBED_VERSION}/python-{EMBED_VERSION}-embed-amd64.zip"
REQUIREMENTS = PROJECT_ROOT / "server" / "requirements.txt"

# embeddable 包默认不含 pip 与 site 机制，需开启这些行
# 注意：embeddable 的 ._pth 会完全接管 sys.path (脚本目录/cwd 不自动加入)，
# 而 Electron 端以 `python -m server.run` 启动，需要 cwd 在搜索路径中。
PYTHON_312_INIT = (
    "import sys\n"
    "import os\n"
    "_here = os.path.dirname(sys.executable)\n"
    "sys.path.insert(0, _here)\n"
    "sys.path.insert(0, os.path.join(_here, 'Lib\\site-packages'))\n"
    "sys.path.insert(0, os.getcwd())\n"
)


def log(msg: str):
    print(f"[portable-python] {msg}", flush=True)


def clean_previous():
    if OUTPUT_DIR.exists():
        log(f"清理旧产物: {OUTPUT_DIR}")
        shutil.rmtree(OUTPUT_DIR, ignore_errors=True)


def build_embeddable() -> bool:
    """Windows: 下载官方 embeddable zip 并组装"""
    if sys.platform != "win32":
        return False
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    archive = OUTPUT_DIR.parent / f"python-{EMBED_VERSION}-embed-amd64.zip"

    if not archive.exists():
        log(f"下载 Python {EMBED_VERSION} embeddable 包...")
        try:
            urllib.request.urlretrieve(EMBED_URL, archive)
        except Exception as e:
            log(f"下载失败: {e}，请检查网络或手动放置 {archive}")
            return False

    log("解压 embeddable 运行时...")
    with zipfile.ZipFile(archive) as zf:
        zf.extractall(OUTPUT_DIR)

    pth_file = OUTPUT_DIR / "python312._pth"
    if pth_file.exists():
        content = pth_file.read_text(encoding="utf-8")
        content = content.replace("#import site", "import site")
        pth_file.write_text(content, encoding="utf-8")
        log("已启用 site 机制 (python312._pth)")

    log("注入启动初始化 (_boot_.pth)...")
    boot = OUTPUT_DIR / "_boot_.pth"
    boot.write_text("import _boot_\n", encoding="utf-8")
    (OUTPUT_DIR / "_boot_.py").write_text(PYTHON_312_INIT, encoding="utf-8")
    return True


def build_venv_copy() -> bool:
    """跨平台兜底: 基于 venv 克隆当前解释器环境"""
    log("使用 venv 克隆方案构建便携运行时...")
    venv_dir = OUTPUT_DIR.parent / "_venv_tmp"
    if venv_dir.exists():
        shutil.rmtree(venv_dir, ignore_errors=True)
    ret = subprocess.run(
        [sys.executable, "-m", "venv", "--copies", str(venv_dir)],
        capture_output=True, text=True, errors="replace",
    )
    if ret.returncode != 0:
        log(f"venv 创建失败: {ret.stderr}")
        return False

    bin_dir = venv_dir / ("Scripts" if sys.platform == "win32" else "bin")
    for item in venv_dir.iterdir():
        if item.name in ("pyvenv.cfg",):
            continue
        target = OUTPUT_DIR / item.name
        if item.is_dir():
            shutil.copytree(item, target, dirs_exist_ok=True)
        else:
            shutil.copy2(item, target)
    shutil.rmtree(venv_dir, ignore_errors=True)
    return True


def install_dependencies() -> bool:
    """将 requirements.txt 全量安装到便携运行时的 site-packages。

    关键点：embeddable 运行时无 pip，因此用宿主 pip 下载 wheel，
    并以 --python-version/--platform 强制匹配便携运行时的解释器版本，
    避免拉到宿主 (如 3.13) 的 C 扩展 wheel 导致 pydantic_core 等无法加载。
    """
    site_packages = OUTPUT_DIR / "Lib" / "site-packages"
    site_packages.mkdir(parents=True, exist_ok=True)

    platform_flag = "win_amd64" if sys.platform == "win32" else "manylinux2014_x86_64"
    log(f"安装依赖到 {site_packages} (运行时: {EMBED_VERSION} {platform_flag})...")
    cmd = [
        sys.executable, "-m", "pip", "install",
        "--target", str(site_packages),
        "--upgrade",
        "--python-version", EMBED_VERSION,
        "--platform", platform_flag,
        "--only-binary=:all:",
        "-r", str(REQUIREMENTS),
    ]
    ret = subprocess.run(cmd, capture_output=True, text=True, errors="replace")
    if ret.returncode != 0:
        log(f"依赖安装失败:\n{ret.stdout}\n{ret.stderr}")
        return False
    log("依赖安装完成")
    return True


def verify_runtime() -> bool:
    """用便携解释器做导入冒烟：能加载 FastAPI 应用即视为可用"""
    exe = OUTPUT_DIR / ("python.exe" if sys.platform == "win32" else "bin/python3")
    if not exe.exists():
        exe = OUTPUT_DIR / ("python.exe" if sys.platform == "win32" else "python3")
    if not exe.exists():
        log(f"校验失败: 找不到解释器 {exe}")
        return False

    env = {**os.environ, "PYTHONUTF8": "1", "LIVE_AGENT_DATA_DIR": str(PROJECT_ROOT / "data")}
    ret = subprocess.run(
        [str(exe), "-c",
         "import fastapi, uvicorn, sqlalchemy, aiosqlite, pydantic, httpx; "
         "import sys; print('portable-runtime-ok', sys.version)"],
        capture_output=True, text=True, errors="replace", env=env,
        cwd=str(PROJECT_ROOT),
    )
    if ret.returncode != 0 or "portable-runtime-ok" not in ret.stdout:
        log(f"校验失败:\nstdout: {ret.stdout}\nstderr: {ret.stderr}")
        return False
    log(f"校验通过: {ret.stdout.strip()}")
    return True


def main():
    parser = argparse.ArgumentParser(description="构建 Electron 桌面版内置便携 Python 运行时")
    parser.add_argument("--clean", action="store_true", help="构建前清理旧产物")
    parser.add_argument("--check", action="store_true", help="仅校验现有产物，不构建")
    args = parser.parse_args()

    if args.check:
        sys.exit(0 if verify_runtime() else 1)

    if args.clean:
        clean_previous()

    if not REQUIREMENTS.exists():
        log(f"未找到 {REQUIREMENTS}")
        sys.exit(1)

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    if not (build_embeddable() or build_venv_copy()):
        log("运行时骨架构建失败")
        sys.exit(1)

    if not install_dependencies():
        sys.exit(1)

    if not verify_runtime():
        log("产物校验未通过，请检查上方日志")
        sys.exit(1)

    log(f"便携运行时构建完成: {OUTPUT_DIR}")
    log("下一步: cd apps/desktop-ui && npm run dist  (electron-builder 会自动引用 resources/python)")


if __name__ == "__main__":
    main()
