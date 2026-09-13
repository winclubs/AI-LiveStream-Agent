# 生产环境部署与运维配置指南

本文档面向运维工程师与系统管理员，提供 AI-LiveStream-Agent 生产环境的服务器配置、环境变量注入、守护进程托管以及探针监控指南。

---

## 1. 运行架构与环境准备

### 1.1 推荐服务器规格
* **操作系统**：Windows Server 2022 / Windows 11 专业版（推荐用于需要虚拟声卡/OBS 同机运行的场景）或 Linux (Ubuntu 22.04 LTS / Debian 12)
* **CPU**：4 核或以上（x86_64 架构）
* **内存**：8 GB 或以上
* **磁盘**：推荐 NVMe/SSD 固态硬盘（SQLite 启用 WAL 模式，高频弹幕与事件写入对 I/O 延迟敏感，**严禁将数据目录挂载在低吞吐的网络共享卷如 NFS/Samba**）
* **Python 运行环境**：CPython 3.12 或 3.13 (64-bit)

### 1.2 依赖安装与校验
```powershell
# 1. 克隆代码或解压交付源码包
cd G:\AI-LiveStream-Agent

# 2. 安装核心依赖
python -m pip install --upgrade pip
python -m pip install -r requirements.txt

# 3. （可选）安装无 GUI 测试与文档支持能力
# python -m pip install -r server/requirements-test.txt

# 4. 验证依赖完整性
python -m compileall -q launcher.py scripts server
```

---

## 2. 环境变量配置清单与注入规范

本项目为了安全与微服务解耦，**默认不会隐式加载当前目录下的 `.env` 文件**。所有运行时配置必须通过操作系统环境变量、服务管理器或容器运行时显式注入。

### 2.1 环境变量完整清单

| 环境变量名 | 默认值 | 说明 |
| :--- | :--- | :--- |
| `LIVE_AGENT_DATA_DIR` | `./data` | **核心**：数据目录，存放数据库 (`live_agent.db`)、密钥、媒体资产与轮转日志 |
| `LIVE_AGENT_HOST` | `127.0.0.1` | 监听地址。若对外提供管理或反代，应配合防火墙指定内网 IP 或 `0.0.0.0` |
| `LIVE_AGENT_PORT` | `18080` | HTTP/WebSocket 服务监听端口 |
| `LIVE_AGENT_LOG_LEVEL` | `INFO` | 日志级别，支持 `DEBUG` / `INFO` / `WARNING` / `ERROR` |
| `LIVE_AGENT_LOG_MAX_BYTES`| `5242880` | 单个日志文件上限（字节，默认 5 MiB） |
| `LIVE_AGENT_LOG_BACKUP_COUNT`| `5` | 日志轮转保留文件数 |
| `LIVE_AGENT_AV_DELAY_MS` | `0` | 音画延迟补偿偏移量（毫秒，用于与 OBS 对齐嘴型与声音） |
| `LIVE_AGENT_RENDER_BACKEND` | `procedural` | 画面渲染后端，默认 `procedural`（程序化头像），可选 `mediapipe` |

---

## 3. 生产环境守护与自启方案

### 3.1 方案 A：Windows 生产环境（NSSM 服务托管 - 推荐）
在 Windows 生产主机上，推荐使用 **NSSM** (Non-Sucking Service Manager) 将 Python 启动器托管为 Windows 系统服务，支持开机无需登录桌面即可启动、异常崩溃自动拉起。

1. **下载 NSSM** 并将其解压到固定路径（如 `C:\tools\nssm.exe`）。
2. **注册服务**：
   以管理员身份打开 PowerShell，执行：
   ```powershell
   # 安装服务
   nssm install LiveStreamAgent "C:\Python312\python.exe" "launcher.py --no-browser"
   nssm set LiveStreamAgent AppDirectory "G:\AI-LiveStream-Agent"

   # 注入环境变量（注意换行分割）
   nssm set LiveStreamAgent AppEnvironmentExtra `
     "LIVE_AGENT_DATA_DIR=G:\ai-live-agent-data"`
     "LIVE_AGENT_HOST=127.0.0.1"`
     "LIVE_AGENT_PORT=18080"`
     "LIVE_AGENT_LOG_LEVEL=INFO"`
     "LIVE_AGENT_AV_DELAY_MS=0"

   # 设置开机自启和异常重启策略
   nssm set LiveStreamAgent Start SERVICE_AUTO_START
   nssm set LiveStreamAgent AppRestartDelay 5000

   # 启动服务
   nssm start LiveStreamAgent
   ```
3. **服务运维**：
   ```powershell
   nssm status LiveStreamAgent
   nssm stop LiveStreamAgent
   nssm restart LiveStreamAgent
   ```

### 3.2 方案 B：Linux 生产环境（systemd 守护）
若以纯后端 API/推流服务部署在 Linux 服务器上：

1. 创建环境配置文件 `/etc/live-agent/live-agent.env`：
   ```ini
   LIVE_AGENT_DATA_DIR=/var/lib/live-agent/data
   LIVE_AGENT_HOST=0.0.0.0
   LIVE_AGENT_PORT=18080
   LIVE_AGENT_LOG_LEVEL=INFO
   LIVE_AGENT_LOG_MAX_BYTES=5242880
   LIVE_AGENT_LOG_BACKUP_COUNT=10
   LIVE_AGENT_AV_DELAY_MS=0
   ```
2. 创建服务单元文件 `/etc/systemd/system/live-agent.service`：
   ```ini
   [Unit]
   Description=AI LiveStream Agent Service
   After=network.target

   [Service]
   Type=simple
   User=liveagent
   Group=liveagent
   WorkingDirectory=/opt/AI-LiveStream-Agent
   EnvironmentFile=/etc/live-agent/live-agent.env
   ExecStart=/usr/local/bin/python launcher.py --no-browser
   Restart=always
   RestartSec=5
   LimitNOFILE=65535

   [Install]
   WantedBy=multi-user.target
   ```
3. 启动并启用：
   ```bash
   sudo systemctl daemon-reload
   sudo systemctl enable --now live-agent
   sudo systemctl status live-agent
   ```

---

## 4. 探针监控与反向代理配置

### 4.1 健康检查探针与可观测性接口
* **存活探针 (Liveness)**：`GET /livez`
  * 响应 200 表示服务进程处于存活状态。
* **启动探针 (Startup)**：`GET /startupz`
  * 响应 200 表示数据迁移、密钥加载和核心子系统已成功初始化完毕。返回 503 时严禁接入流量。
* **就绪探针 (Readiness)**：`GET /readyz`
  * 响应 200 且返回 `status: "ready"`，内部已完成 SQLite 读写探测与数据目录写权限检查。
* **Prometheus 指标端点 (Metrics)**：`GET /metrics`
  * 标准 Prometheus 纯文本格式（Content-Type: `text/plain`），导出直播状态 `live_agent_status`、累计弹幕数 `live_agent_danmaku_total`、在线观众数、调度队列深度、弹幕抓取熔断状态 `live_agent_danmaku_circuit_broken`、视频渲染帧率 `live_agent_render_fps` 以及内存占用。
* **实时健康看板数据 (Health Summary)**：`GET /api/v1/system/health-summary`
  * JSON 格式快照，供中控大屏、第三方监控或 Web 控制台实时渲染健康仪表盘。

### 4.2 Nginx 反向代理配置样例
当需要在局域网内或公网通过 Nginx 对外提供服务时，必须支持 WebSocket 握手升级：

```nginx
upstream live_agent_backend {
    server 127.0.0.1:18080;
    keepalive 32;
}

server {
    listen 80;
    server_name live.yourdomain.com;

    client_max_body_size 50M;

    location / {
        proxy_pass http://live_agent_backend;
        proxy_http_version 1.1;

        # WebSocket 升级头
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";

        # 客户端信息传递
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;

        # 超时设置（保持长连接）
        proxy_read_timeout 86400s;
        proxy_send_timeout 86400s;
    }

    # 探针无需缓存
    location ~* ^/(livez|startupz|readyz)$ {
        proxy_pass http://live_agent_backend;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        access_log off;
    }
}
```

---

## 5. 安全与数据备份建议

1. **密钥隔离**：
   Windows 环境下主密钥 `.master.key` 可能受当前操作系统的 DPAPI 加密保护。**如果要迁移整套数据目录至新机器，必须在原机器备份后导出，不能跨机器直接复制受 DPAPI 保护的原始密钥文件**。
2. **定期离线备份**：
   按 [docs/operations.md](operations.md) 指引，在停播或维护窗口执行：
   ```powershell
   python scripts/backup_data.py --data-dir "G:\ai-live-agent-data" --output "G:\backups\daily-$(Get-Date -Format 'yyyyMMdd')"
   ```
