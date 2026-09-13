@echo off
setlocal enabledelayedexpansion
cd /d "%~dp0"
title AI-LiveStream-Agent 智能直播中控系统

echo ============================================================================
echo   正在为您准备启动 AI-LiveStream-Agent 智能直播中控系统...
echo ============================================================================

:: 1. 优先探测本地项目虚拟环境
if exist "%~dp0.venv\Scripts\python.exe" (
    set "PY_CMD=%~dp0.venv\Scripts\python.exe"
    goto :FOUND_PY
)
if exist "%~dp0venv\Scripts\python.exe" (
    set "PY_CMD=%~dp0venv\Scripts\python.exe"
    goto :FOUND_PY
)

:: 2. 探测系统 PATH 中的 python
where python >nul 2>&1
if not errorlevel 1 (
    set "PY_CMD=python"
    goto :FOUND_PY
)

:: 3. 探测 Windows py 启动器
where py >nul 2>&1
if not errorlevel 1 (
    set "PY_CMD=py -3"
    goto :FOUND_PY
)

:: 4. 兜底探测常见安装路径
for %%P in (
    "D:\python\python.exe"
    "C:\Python313\python.exe"
    "C:\Python312\python.exe"
    "%LOCALAPPDATA%\Programs\Python\Python313\python.exe"
    "%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
    "C:\Program Files\Python313\python.exe"
    "C:\Program Files\Python312\python.exe"
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
echo 1. 前往 Python 官方网站下载安装 Python 3.12 或 3.13 64位版本:
echo    https://www.python.org/downloads/
echo 2. 安装时请勾选 "Add python.exe to PATH"，或手动添加到系统环境变量中
echo 3. 安装完成后，再双击本脚本即可自动启动
echo.
pause
exit /b 1

:FOUND_PY
echo [*] 找到 Python 解释器: !PY_CMD!
echo [*] 正在执行环境全景体检、依赖检查与端口预清理...
echo.
"!PY_CMD!" launcher.py %*
if errorlevel 1 goto :LAUNCH_ERR
goto :END

:LAUNCH_ERR
echo.
echo ============================================================================
echo [提示] 服务停止或异常退出 (退出码: %errorlevel%)
echo 若端口被占用，可尝试强制执行: "!PY_CMD!" launcher.py --force
echo ============================================================================
pause
exit /b 1

:END
endlocal
exit /b 0
