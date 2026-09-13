# scripts/check.ps1
# 本地与 CI 统一质量检查脚本 (包含语法编译、代码规范、静态类型、前端校验与测试覆盖率)

$ErrorActionPreference = "Stop"

Write-Host "==> [1/5] Python 全量语法编译检查..." -ForegroundColor Cyan
python -m compileall -q launcher.py scripts server
if (-not $?) { Write-Host "❌ 编译检查失败" -ForegroundColor Red; exit 1 }

Write-Host "==> [2/5] Ruff 代码规范与风格检查..." -ForegroundColor Cyan
python -m ruff check launcher.py scripts/ server/
if (-not $?) { Write-Host "❌ Ruff 检查失败" -ForegroundColor Red; exit 1 }

Write-Host "==> [3/5] Mypy 核心模块静态类型检查..." -ForegroundColor Cyan
python -m mypy server/core/queue/priority_queue.py server/core/cpu_worker.py
if (-not $?) { Write-Host "❌ Mypy 检查失败" -ForegroundColor Red; exit 1 }

Write-Host "==> [4/5] Node.js 前端脚本语法解析检查..." -ForegroundColor Cyan
node --check apps/desktop-ui/main.js
if (-not $?) { Write-Host "❌ main.js 语法错误" -ForegroundColor Red; exit 1 }
node --check server/static/js/console.js
if (-not $?) { Write-Host "❌ console.js 语法错误" -ForegroundColor Red; exit 1 }

Write-Host "==> [5/5] Pytest 自动化测试与覆盖率校验 (隔离数据目录)..." -ForegroundColor Cyan
$TempDataDir = Join-Path $env:TEMP ('ai-live-agent-ci-' + [guid]::NewGuid().ToString('N'))
$PreviousDataDir = $env:LIVE_AGENT_DATA_DIR
$env:LIVE_AGENT_DATA_DIR = $TempDataDir

try {
    python -m pytest -q server/tests --cov=server --cov-report=term
    if (-not $?) { Write-Host "❌ Pytest 测试未通过或覆盖率未达标" -ForegroundColor Red; exit 1 }
} finally {
    if ($null -ne $PreviousDataDir) {
        $env:LIVE_AGENT_DATA_DIR = $PreviousDataDir
    } else {
        Remove-Item Env:\LIVE_AGENT_DATA_DIR -ErrorAction SilentlyContinue
    }
    if (Test-Path $TempDataDir) {
        Remove-Item -Recurse -Force $TempDataDir -ErrorAction SilentlyContinue
    }
}

Write-Host "========================================" -ForegroundColor Green
Write-Host "🎉 全部质量检查通过！代码可安全提交上线。" -ForegroundColor Green
Write-Host "========================================" -ForegroundColor Green
