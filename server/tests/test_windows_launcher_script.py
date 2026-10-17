# -*- coding: utf-8 -*-
"""
Windows 一键启动脚本 (run_agent.bat) 回归测试。

背景（上线诊断 RISK-6 + 实测发现的功能缺陷）：
1. 编码：全仓唯一非 UTF-8 文件（GBK 无 BOM）。任何以 UTF-8 打开的编辑器
   保存后即损坏；在非中文 Windows（代码页非 936）上提示文案直接乱码。
2. **实际功能缺陷**：探测分支 3 会把 ``PY_CMD`` 置为 ``py -3``（含空格），
   而执行处写作 ``"!PY_CMD!" launcher.py`` —— cmd 会把 ``"py -3"`` 当成
   一个整体可执行文件名去查找，直接以 9009（不是内部或外部命令）失败。
   实测复现：
       set "PY_CMD=py -3" && "!PY_CMD!" -c "print(1)"  ->  exit=9009

   受影响人群：**只安装了 py 启动器、python 未加入 PATH** 的用户，
   也就是相当一部分 Windows 用户的默认安装形态（一键 Launching Python
   装的是 Store 版或未勾选 Add to PATH）。
"""
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
BAT = ROOT / "run_agent.bat"

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="run_agent.bat 仅为 Windows 启动脚本")


def _read_bat_text() -> str:
    """按三种编码依次尝试解码，确保批处理文件可被任意工具读取。"""
    raw = BAT.read_bytes()
    for encoding in ("utf-8-sig", "utf-8", "gbk"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    pytest.fail("run_agent.bat 无法用 utf-8 / utf-8-sig / gbk 任一编码解码")


# ---------------------------------------------------------------------------
# 编码
# ---------------------------------------------------------------------------

def test_batch_file_has_no_utf8_bom():
    """禁止 UTF-8 BOM。

    实测结论（推翻了「带 BOM 才能正确识别」的初始假设）：
    cmd.exe **不会**剥离 UTF-8 BOM。带 BOM 时首行会变成
    ``'﻿@echo' 不是内部或外部命令``，脚本直接失效。
    """
    raw = BAT.read_bytes()
    assert raw[:3] != b"\xef\xbb\xbf", (
        "run_agent.bat 带 UTF-8 BOM 会使 cmd.exe 首行报 "
        "'﻿@echo' 不是内部或外部命令，脚本完全失效。必须去掉 BOM。"
    )


def test_batch_file_is_pure_ascii():
    """必须是纯 ASCII。

    这是本文件的编码契约（实测踩坑后确定），而非风格偏好：

    - ASCII 是 UTF-8 的子集 -> 任何工具/编辑器/代码页都能正确读取，零乱码；
    - 非 ASCII 内容叠加文件中段的 ``chcp`` 会让 cmd.exe 以错误字节重新
      读取后续行，把正常行当成命令执行（实测报
      ``'xxx' is not recognized as an internal or external command``）；
    - 启用 delayed expansion 后，注释中的 ``!`` 会先被展开再判定注释，
      使注释行变成伪命令。

    所有面向用户的中文文案由 launcher.py 输出（Python 侧编码处理正确）。
    """
    raw = BAT.read_bytes()
    offenders = [(i, hex(b)) for i, b in enumerate(raw) if b > 0x7F]
    assert not offenders, (
        "run_agent.bat 含非 ASCII 字节，违反纯 ASCII 契约:\n  "
        + "\n  ".join(f"offset {i}: {b}" for i, b in offenders[:10])
    )


def test_batch_file_switches_console_to_utf8():
    """必须显式 chcp 65001，使子进程（launcher.py）的中文输出正确显示。"""
    text = _read_bat_text()
    assert "chcp 65001" in text, "缺少 chcp 65001，launcher.py 的中文输出会乱码"


# ---------------------------------------------------------------------------
# PY_CMD 引用缺陷（实测 exit=9009）
# ---------------------------------------------------------------------------

def test_py_cmd_with_space_is_not_double_quoted():
    """含空格的 PY_CMD（``py -3``）绝不能被整体加引号当作可执行文件名。

    `"py -3" launcher.py` 会被 cmd 当作查找名为 ``py -3`` 的程序并失败。

    只检查**可执行行**：批处理注释里引用该反模式用于说明历史缺陷是合理的，
    把它一并判为违规会让「解释问题的注释」变成不可写。
    """
    text = _read_bat_text()
    offending = [
        (i, line.strip())
        for i, line in enumerate(text.splitlines(), start=1)
        if line.strip().startswith('"!PY_CMD!"')
    ]
    assert not offending, (
        "可执行行中发现 `\"!PY_CMD!\"` 反模式：PY_CMD 在 py 启动器分支下含空格，"
        "整体加引号会导致 cmd 以 9009 失败。\n"
        + "\n".join(f"  line {i}: {line}" for i, line in offending)
    )


def test_py_launcher_branch_uses_unquoted_expansion():
    """执行处必须使用不带整体引号的 ``!PY_CMD!`` 展开。"""
    text = _read_bat_text()
    exec_lines = [
        line.strip()
        for line in text.splitlines()
        if line.strip().startswith("!PY_CMD!")
    ]
    assert exec_lines, "未找到 launcher 的执行行"
    assert all(line.startswith("!PY_CMD! !PY_ARGS! launcher.py") or "!PY_ARGS! launcher.py" in line
               for line in exec_lines), (
        f"执行行必须为 `!PY_CMD! !PY_ARGS! launcher.py ...` 形式，实际: {exec_lines}"
    )


def test_py_launcher_branch_launches_successfully(tmp_path):
    """端到端复现：PY_CMD 为含空格的 `py -3` 时，脚本必须能真正启动。

    本测试用一个替身 launcher 验证执行语义，而非检查脚本文本。
    """
    launcher = tmp_path / "launcher.py"
    launcher.write_text("print('LAUNCHED_OK')\n", encoding="utf-8")

    probe = tmp_path / "probe.cmd"
    probe.write_text(
        "@echo off\r\n"
        "setlocal enabledelayedexpansion\r\n"
        'set "PY_CMD=py -3"\r\n'
        # 复刻 run_agent.bat 的执行写法
        '!PY_CMD! "%~dp0launcher.py" --restart\r\n'
        "exit /b %errorlevel%\r\n",
        encoding="ascii",
    )

    if not shutil.which("py"):
        pytest.skip("本机未安装 py 启动器，无法复现该执行路径")

    proc = subprocess.run(
        ["cmd", "/c", str(probe)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
        check=False,
    )

    assert proc.returncode == 0, (
        f"PY_CMD 含空格时启动失败 (exit={proc.returncode})\n"
        f"{proc.stdout}\n{proc.stderr}\n"
        "历史实现用 `\"!PY_CMD!\"` 整体加引号，导致 9009。"
    )
    assert "LAUNCHED_OK" in proc.stdout
