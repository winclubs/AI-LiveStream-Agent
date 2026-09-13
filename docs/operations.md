# 运维、升级与回滚

## 权威数据与日志

活动数据根由 `LIVE_AGENT_DATA_DIR` 决定；未设置时源码运行使用仓库 `data`，Electron 使用 `app.getPath('userData')\data`。权威数据库始终是该目录下的 `live_agent.db`（仓库根部历史遗留的 `livestream.db` 空库与 `install.sql` 初始化脚本已删除，建表一律以 `server/database` 的 SQLAlchemy 模型与 `migrations/` 为准）。

日志位于 `logs/server.log`，默认单文件 5 MiB、保留 5 个轮转文件。`tmp`、`logs`、`backups`、WAL/SHM 和服务锁不会进入完整备份。

## 停服一致备份

1. 停止直播场次并退出 Electron/服务。
2. 确认 `/readyz` 已不可达，且数据目录中没有仍存活 PID 对应的 `.service.lock`。
3. 运行 `python scripts/backup_data.py --data-dir <data> --output <backup>`。
4. 工具使用 SQLite Backup API，生成包含 WAL 已提交内容的自包含数据库；随后复制主密钥与持久资产。
5. 工具执行 `PRAGMA integrity_check`，生成每文件大小与 SHA-256 的 `manifest.json`，最后原子发布备份目录。

运行中的服务会被拒绝。此边界保证数据库、密钥和资产不会在备份期间继续变化；本项目不宣称在线联合快照。

## 恢复

1. 停止服务；恢复脚本检测到活动 PID 会拒绝操作。
2. 运行 `python scripts/restore_data.py --backup <backup> --data-dir <data> --confirm`。
3. 工具在替换前校验 manifest、全部文件 checksum 和 SQLite 完整性。
4. 当前数据目录先原子重命名为 `data.rollback-<时间>`，再将同卷 staging 切换为活动目录；第二步失败会自动恢复旧目录。
5. 启动与备份配套的应用版本，等待 `/startupz` 和 `/readyz` 返回 200，再检查商品、角色、知识库和媒体资产。
6. 验收完成前不要删除 rollback 目录。

Windows 下 `.master.key` 可能受 DPAPI 当前用户/机器保护，因此完整恢复支持范围是原机器、原 Windows 用户。不要在目标端自动生成新密钥替代备份密钥。

## 升级

1. 记录当前应用版本和数据目录。
2. 停服并创建 `before-upgrade` 完整备份，确认命令成功。
3. 安装新代码/桌面包，保持 `version.json` 与包版本配套。
4. 启动后等待 `/startupz`、`/readyz`；初始化或迁移失败不得继续直播。
5. 执行本地 API/WebSocket/开停播 smoke test，再进入外部直播工作台验收。

## 回滚

迁移框架只保证向前迁移，不提供数据库 downgrade。可靠回滚必须同时恢复：

- 升级前的旧应用包；
- 升级前创建的完整数据备份。

不得仅换回旧二进制继续打开已被新版本迁移过的数据。回滚后重复 startup/readiness 与关键业务 smoke test。

## 故障定位

- `/livez` 失败：进程不可达或正在退出。
- `/startupz` 为 503：初始化尚未完成或失败，查看 `logs/server.log` 的 `LiveAgent.App` 记录。
- `/readyz` 为 503：检查响应中的 `phase`、`database` 和 `data_dir`。
- Electron 不开正常窗口：按对话框中的 Python 预检结果修复精确依赖。
- 备份/恢复提示服务运行：先正常退出；仅当确认 PID 已不存在时清理陈旧锁。
