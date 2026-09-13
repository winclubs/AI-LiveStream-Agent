"""Electron 桌面包使用的外部 Python 运行时预检（仅依赖标准库）。"""
from __future__ import annotations

import importlib.metadata
import json
import platform
import re
import struct
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
REQUIREMENTS = PROJECT_ROOT / "server" / "requirements.txt"
SUPPORTED_MIN = (3, 12)
SUPPORTED_MAX_EXCLUSIVE = (3, 14)


def _result(ok: bool, message: str, **extra) -> int:
    print(json.dumps({"ok": ok, "message": message, **extra}, ensure_ascii=False))
    return 0 if ok else 1


def _requirements() -> list[tuple[str, str]]:
    direct = []
    pattern = re.compile(r"^([A-Za-z0-9_.-]+)(?:\[[^]]+\])?==([^\s;]+)$")
    for raw in REQUIREMENTS.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        match = pattern.match(line)
        if not match:
            raise ValueError(f"核心依赖未精确固定: {line}")
        direct.append((match.group(1), match.group(2)))
    return direct


def main() -> int:
    version = sys.version_info[:2]
    if not (SUPPORTED_MIN <= version < SUPPORTED_MAX_EXCLUSIVE):
        return _result(False, "需要 CPython 3.12 或 3.13", python=sys.executable, version=platform.python_version())
    if platform.python_implementation() != "CPython" or struct.calcsize("P") * 8 != 64:
        return _result(False, "需要 64 位 CPython", python=sys.executable, implementation=platform.python_implementation())
    if not REQUIREMENTS.exists():
        return _result(False, "安装资源缺少 server/requirements.txt", python=sys.executable)

    mismatches = []
    try:
        for name, expected in _requirements():
            try:
                actual = importlib.metadata.version(name)
            except importlib.metadata.PackageNotFoundError:
                mismatches.append(f"{name} 缺失（需要 {expected}）")
                continue
            if actual != expected:
                mismatches.append(f"{name}={actual}（需要 {expected}）")
    except Exception as exc:
        return _result(False, str(exc), python=sys.executable)

    if mismatches:
        return _result(False, "核心依赖未就绪", python=sys.executable, mismatches=mismatches)

    sys.path.insert(0, str(PROJECT_ROOT))
    try:
        from server.app import app  # noqa: F401
    except Exception as exc:
        return _result(False, f"server.app 导入失败: {exc}", python=sys.executable)

    return _result(
        True,
        "外部 Python 运行时预检通过",
        python=str(Path(sys.executable).resolve()),
        version=platform.python_version(),
        architecture="64bit",
    )


if __name__ == "__main__":
    raise SystemExit(main())
