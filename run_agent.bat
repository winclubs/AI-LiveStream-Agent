@echo off
chcp 65001 >nul
setlocal enabledelayedexpansion
cd /d "%~dp0"
title AI-LiveStream-Agent Local Engine

echo ============================================================================
echo   Starting AI-LiveStream-Agent local engine...
echo ============================================================================

rem ---------------------------------------------------------------------------
rem Encoding contract for this file:
rem   This batch file is intentionally PURE ASCII.
rem   ASCII is a subset of UTF-8, so the file stays readable by any text tool,
rem   any editor and any code page, with zero mojibake risk.
rem   Do NOT add non-ASCII characters here:
rem     - UTF-8 BOM breaks cmd.exe (it does not strip the BOM, so the first
rem       line becomes an illegal command instead of "@echo off");
rem     - multi-byte content combined with a mid-file "chcp" makes cmd.exe
rem       mis-read subsequent lines as commands;
rem     - delayed expansion rewrites "!" inside comments before cmd treats
rem       them as comments, turning comment lines into bogus commands.
rem   All user-facing Chinese text is printed by launcher.py, which handles
rem   encoding correctly via PYTHONUTF8.
rem ---------------------------------------------------------------------------

rem 1. Probe for a project-local virtual environment
if exist "%~dp0.venv\Scripts\python.exe" (
    set "PY_CMD=%~dp0.venv\Scripts\python.exe"
    goto :FOUND_PY
)
if exist "%~dp0venv\Scripts\python.exe" (
    set "PY_CMD=%~dp0venv\Scripts\python.exe"
    goto :FOUND_PY
)

rem 2. Probe for python on PATH
where python >nul 2>&1
if not errorlevel 1 (
    set "PY_CMD=python"
    goto :FOUND_PY
)

rem 3. Probe for the Windows py launcher.
rem    Command and argument are stored separately and expanded independently.
rem    The previous implementation stored "py -3" as a single PY_CMD and then
rem    executed it double-quoted; cmd.exe then searched for an executable
rem    literally named "py -3" and failed with exit code 9009
rem    ("is not recognized as an internal or external command").
rem    That broke every user who has only the py launcher and no python on PATH.
where py >nul 2>&1
if not errorlevel 1 (
    set "PY_CMD=py"
    set "PY_ARGS=-3"
    goto :FOUND_PY
)

rem 4. Probe common install locations
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
echo [ERROR] No usable 64-bit Python 3.12/3.13 interpreter was found.
echo.
echo How to fix:
echo 1. Download a 64-bit Python 3.12 or 3.13 from:
echo    https://www.python.org/downloads/
echo 2. During setup, tick "Add python.exe to PATH".
echo 3. Re-run this script after the installation completes.
echo.
pause
exit /b 1

:FOUND_PY
if not defined PY_ARGS set "PY_ARGS="
echo [*] Python command found: !PY_CMD! !PY_ARGS!
echo [*] Running the preflight health check (slow on first run)...
echo.
rem PY_CMD and PY_ARGS are expanded separately and without whole-value quoting.
!PY_CMD! !PY_ARGS! launcher.py --restart %*
if errorlevel 1 goto :LAUNCH_ERR
goto :END

:LAUNCH_ERR
echo.
echo ============================================================================
echo [NOTICE] Startup terminated with exit code %errorlevel%
echo If the port is already in use, force-clean it with:
echo    !PY_CMD! !PY_ARGS! launcher.py --force
echo ============================================================================
pause
exit /b 1

:END
endlocal
exit /b 0