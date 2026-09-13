# AI-LiveStream-Agent

本地 AI 互动直播执行引擎与 Electron 控制台。当前交付提供本地程序化头像、音频与虚拟摄像头源、商品/订单/角色/知识库管理；外部直播平台发布不由本程序管理，必须在直播工作台中另行配置并验收。

## 运行要求

- 64 位 CPython 3.12 或 3.13
- Node.js 22（仅桌面开发与打包需要）
- 核心依赖：`python -m pip install -r requirements.txt`（或 `python -m pip install -r server/requirements.txt`）
- 测试依赖：`python -m pip install -r server/requirements-test.txt`
- 可选媒体能力：见 `server/requirements-optional.txt`

桌面端支持**可选内置便携版 Python (3.12.10)**（通过 `scripts/build_portable_python.py` 构建并置于 `apps/desktop-ui/resources/python/` 目录下）；未打包内置环境时，Electron 启动时会自动平滑回退并逐项核对系统 Python 版本、架构、核心依赖精确版本和 `server.app` 导入结果；预检失败时不会创建正常控制台窗口。


## 生产部署与文档导航

- 📘 [生产环境部署指南 (docs/deployment.md)](docs/deployment.md)：服务守护（NSSM / systemd）、环境变量配置清单、反向代理与探针监控
- 🎥 [OBS 与直播伴侣协同指南 (docs/obs_integration.md)](docs/obs_integration.md)：数字人浏览器源接入、音画延迟对齐（A/V Sync）与开播冒烟检查清单
- 🛡️ [运维、备份与回滚手册 (docs/operations.md)](docs/operations.md)：停服一致备份、数据恢复与版本升级标准作业程序

## 启动

PowerShell：

```powershell
$env:LIVE_AGENT_DATA_DIR = "G:\ai-live-agent-data"
python launcher.py --no-browser
```

也可运行 `run_agent.bat`。开发启动器使用热重载；稳定运行可使用：

```powershell
python -m uvicorn server.app:app --host 127.0.0.1 --port 18080
```

Electron 开发：

```powershell
npm.cmd ci --prefix apps/desktop-ui
npm.cmd start --prefix apps/desktop-ui
```

Electron 自动将数据目录设为其 `userData\data`，不会写入安装资源目录。

## 健康、监控与日志

- 存活：`GET /livez`
- 启动完成：`GET /startupz`
- 就绪（含 SQLite 查询）：`GET /readyz`
- 监控指标 (Prometheus)：`GET /metrics`
- 实时健康摘要看板：`GET /api/v1/system/health-summary`
- 实例版本/数据目录：`GET /api/v1/system/version`
- 轮转 JSON Lines 日志：`<LIVE_AGENT_DATA_DIR>\logs\server.log`

## 备份、恢复、升级

完整备份包含 SQLite 自包含快照、`.master.key` 和持久媒体资产，且明确要求停服：

```powershell
python scripts/backup_data.py --data-dir "G:\ai-live-agent-data" --output "G:\backups\before-upgrade"
python scripts/restore_data.py --backup "G:\backups\before-upgrade" --data-dir "G:\ai-live-agent-data" --confirm
```

恢复会校验 manifest、SHA-256 与 SQLite 完整性，并保留 `data.rollback-<时间>`。Windows DPAPI 保护的主密钥只承诺原机器、原 Windows 用户恢复。升级与回滚步骤见 [docs/operations.md](docs/operations.md)。

## 质量检查
 
推荐使用统一检查脚本，一键完成语法编译、代码风格、类型检查、前端解析与覆盖率验证：
 
```powershell
# Windows PowerShell 运行：
powershell .\scripts\check.ps1

# 或跨平台 Python 运行：
python scripts/check.py
```

也可手动分步执行：
```powershell
python -m compileall -q launcher.py scripts server
python -m ruff check launcher.py scripts server
python -m mypy server/core/queue/priority_queue.py server/core/cpu_worker.py
node --check apps/desktop-ui/main.js
node --check server/static/js/console.js
$env:LIVE_AGENT_DATA_DIR = Join-Path $env:TEMP ('ai-live-agent-test-' + [guid]::NewGuid().ToString('N'))
python -m pytest -q server/tests --cov=server --cov-branch
```


测试和本机验收不得调用收费 API 或真实直播平台；外部 LLM、TTS、OBS/直播工作台能力使用 mock、本地降级或单独外部验收。
