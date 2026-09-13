# -*- coding: utf-8 -*-
"""
生成符合 Windows cmd.exe 原生规范的 run_agent.bat 文件
规范要求：
1. 换行符必须为 Windows 原生 CRLF (\r\n)；
2. 编码采用 GBK / CP936，原生兼容所有中文 Windows 控制台，消除字符截断与吞字符现象。
"""

bat_content = """@echo off
setlocal enabledelayedexpansion
cd /d "%~dp0"
title AI-LiveStream-Agent 智能直播中控系统

echo ============================================================================
echo   正在准备启动 AI-LiveStream-Agent 智能直播中控系统...
echo ============================================================================

:: 1. 优先探测本地虚拟环境
if exist "%~dp0.venv\\Scripts\\python.exe" (
    set "PY_CMD=%~dp0.venv\\Scripts\\python.exe"
    goto :FOUND_PY
)
if exist "%~dp0venv\\Scripts\\python.exe" (
    set "PY_CMD=%~dp0venv\\Scripts\\python.exe"
    goto :FOUND_PY
)

:: 2. 检查系统 PATH 中的 python
where python >nul 2>&1
if not errorlevel 1 (
    set "PY_CMD=python"
    goto :FOUND_PY
)

:: 3. 检查 Windows py 启动器
where py >nul 2>&1
if not errorlevel 1 (
    set "PY_CMD=py -3"
    goto :FOUND_PY
)

:: 4. 尝试探测常用安装路径
for %%P in (
    "D:\\python\\python.exe"
    "C:\\Python313\\python.exe"
    "C:\\Python312\\python.exe"
    "%LOCALAPPDATA%\\Programs\\Python\\Python313\\python.exe"
    "%LOCALAPPDATA%\\Programs\\Python\\Python312\\python.exe"
    "C:\\Program Files\\Python313\\python.exe"
    "C:\\Program Files\\Python312\\python.exe"
) do (
    if exist %%P (
        set "PY_CMD=%%P"
        goto :FOUND_PY
    )
)

:NO_PYTHON
echo.
echo [错误] 未在当前系统中检测到可用的 Python 64位运行环境！
echo.
echo 解决方案:
echo 1. 请前往 Python 官方网站下载安装 Python 3.12 或 3.13 64位版本:
echo    https://www.python.org/downloads/
echo 2. 安装时请务必勾选 "Add python.exe to PATH"（添加到系统环境变量）。
echo 3. 安装完成后重新双击本脚本即可自动启动。
echo.
pause
exit /b 1

:FOUND_PY
echo [*] 找到 Python 环境: !PY_CMD!
echo [*] 正在呼起商用现场环境自检与核心引擎...
echo.
!PY_CMD! launcher.py %*
if errorlevel 1 goto :LAUNCH_ERR
goto :END

:LAUNCH_ERR
echo.
echo ============================================================================
echo [提示] 服务已停止或异常退出 (错误码: %errorlevel%)
echo 若端口被占用，请在命令行执行: !PY_CMD! launcher.py --force
echo ============================================================================
pause
exit /b 1

:END
endlocal
exit /b 0
"""

# 转换为 CRLF 换行并以 GBK 保存
normalized = bat_content.replace("\r\n", "\n").replace("\n", "\r\n")
with open("run_agent.bat", "wb") as f:
    f.write(normalized.encode("gbk"))

print("[OK] run_agent.bat 已成功生成 (编码: GBK, 换行符: CRLF, 大小:", len(normalized.encode("gbk")), "字节)")
