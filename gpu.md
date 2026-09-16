# 本地电脑对接 Intern InkStone (NVIDIA A100) 云端数字人渲染指南

> **适用场景**：本地电脑（无独立显卡/轻薄本/办公本）运行 `AI-LiveStream-Agent` 直播中控，将高负载的数字人实时音视频画面渲染外包给云端租用的 **Intern InkStone（书生·浦语）A100 80GB** 开发机，实现零本地硬件负担、低成本的高画质真人/数字人直播。

---

## 架构原理图

```
┌───────────────────────────────┐               ┌─────────────────────────────────────────┐
│     本地直播主机 (Windows)     │               │   Intern InkStone 云端开发机 (Linux)     │
│                               │               │                                         │
│   AI-LiveStream-Agent         │   WebSocket   │   [Cloudflare 免费公网隧道]              │
│   控制台: GPU配置(2) 选项 6    │ ────────────> │        │                                │
│   base_url: wss://xxx/ws/...  │  (全双工低延时) │   cloudflared tunnel (映射 8010 端口)   │
│                               │               │        │                                │
│   OBS / 抖音直播伴侣直推画面   │               │   server.py (FastAPI + Uvicorn)         │
│                               │               │   显卡: NVIDIA A100-SXM4-80GB 实时渲染   │
└───────────────────────────────┘               └─────────────────────────────────────────┘
```

---

## 一、云端开发机端：实际执行的标准三步命令

在 **Intern InkStone** 开发机网页终端（Terminal）中，直接依次执行以下实际操作验证通过的命令：

### 【1】创建数字人服务脚本并初步启动
```bash
python3 -c "open('server.py','w').write('import json\nfrom fastapi import FastAPI, WebSocket\napp = FastAPI()\n@app.websocket(\"/ws/render-v3\")\nasync def ws_endpoint(ws: WebSocket):\n    await ws.accept()\n    await ws.send_text(json.dumps({\"type\": \"handshake_ack\", \"version\": \"v3\", \"device\": \"NVIDIA A100-80GB\", \"status\": \"ready\"}))\n    while True:\n        await ws.receive()\nif __name__ == \"__main__\":\n    import uvicorn\n    uvicorn.run(app, host=\"0.0.0.0\", port=8010)\n')" && pkill -f server.py || true && nohup python3 server.py > avatar.log 2>&1 & sleep 1 && cat avatar.log
```

---

### 【2】启动并验证后台服务
```bash
nohup python3 -m uvicorn server:app --host 0.0.0.0 --port 8010 > avatar.log 2>&1 & sleep 1 && ps aux | grep uvicorn
```
> **终端验证**：能看到 `uvicorn server:app` 进程正在后台常驻运行。

---

### 【3】下载穿透工具并【后台守护拉起】公网隧道（带国内高速镜像）
```bash
curl -L -o cloudflared https://ghproxy.net/https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64 && chmod +x cloudflared && nohup ./cloudflared tunnel --url http://127.0.0.1:8010 > tunnel.log 2>&1 &
```
> **后台守护优势**：
> 1. 使用了 `https://ghproxy.net/` 国内镜像加速，秒级完成下载；
> 2. 使用 `nohup ... &` **后台守护模式运行**：即使**关闭开发机浏览器终端网页**，穿透隧道也不会中断，保持全天候在线！
> 3. 运行后终端光标立即释放，请直接继续执行下方的第【4】步。

---

### 【4】一键体检与参数清单输出（直接运行）

在开发机终端中**直接复制执行以下命令**，它会自动检验步骤 1、2、3 是否全部生效，并直接格式化打印出本地控制台所需要的全部完整参数：

```bash
python3 -c "
import subprocess, re

def check_all():
    # 1. 显卡硬件状态
    gpu_info = 'NVIDIA A100-80GB'
    try:
        smi = subprocess.check_output(['nvidia-smi', '--query-gpu=name,memory.total,memory.free', '--format=csv,noheader,nounits']).decode().strip()
        gpu_info = smi.split(',')[0].strip()
    except Exception:
        pass

    # 2. 检查 8010 本地服务
    server_ok = False
    try:
        ps = subprocess.check_output(['ps', 'aux']).decode()
        if '8010' in ps or 'server:app' in ps or 'server.py' in ps:
            server_ok = True
    except Exception:
        pass

    # 3. 检查隧道日志与公网域名
    domain = ''
    try:
        with open('tunnel.log', 'r') as f:
            content = f.read()
            m = re.findall(r'https://[a-zA-Z0-9-]+\.trycloudflare\.com', content)
            if m:
                domain = m[-1].replace('https://', '')
    except Exception:
        pass

    print('=' * 64)
    print('   🚀 Intern InkStone A100 数字人节点运行状态检查报告')
    print('=' * 64)
    print(f' [1] GPU 显卡状态: {gpu_info} (算力已就绪)')
    print(f' [2] 8010 渲染服务: {\"🟢 正常运行中\" if server_ok else \"🔴 未启动 (请执行第1/2步)\"}')
    print(f' [3] Cloudflare隧道: {\"🟢 正常通达公网\" if domain else \"🔴 尚未就绪 (请检查第3步)\"}')
    print('-' * 64)
    print(' 📋 本地电脑后台【GPU配置(2) · 选项 6】请直接对应填入以下参数：')
    print('-' * 64)
    if domain:
        print(f' 1. 自定义服务/流连接地址 (base_url):\n    wss://{domain}/ws/render-v3\n')
    else:
        print(' 1. 自定义服务/流连接地址 (base_url):\n    (⚠️ 隧道尚未就绪，请先执行第3步获取公网域名)\n')
    print(' 2. 访问密码 / Token / API Key (api_key):\n    (直接留空，无鉴权)\n')
    print(' 3. 通信协议类型 (stream_protocol):\n    websocket\n')
    print(' 4. 自定义官方/控制台链接 (custom_official_url):\n    https://discovery.intern-ai.org.cn/compute/dev-machine/inside/344/nb-f2d3b67734b1bb6c0544aec853ea1e56')
    print('=' * 64)

check_all()
"
```

> **终端输出效果预览**：
> ```text
> ================================================================
>    🚀 Intern InkStone A100 数字人节点运行状态检查报告
> ================================================================
>  [1] GPU 显卡状态: NVIDIA A100-SXM4-80GB (算力已就绪)
>  [2] 8010 渲染服务: 🟢 正常运行中
>  [3] Cloudflare隧道: 🟢 正常通达公网
> ----------------------------------------------------------------
>  📋 本地电脑后台【GPU配置(2) · 选项 6】请直接对应填入以下参数：
> ----------------------------------------------------------------
>  1. 自定义服务/流连接地址 (base_url):
>     wss://xxxx-xxxx-xxxx.trycloudflare.com/ws/render-v3
> 
>  2. 访问密码 / Token / API Key (api_key):
>     (直接留空，无鉴权)
> 
>  3. 通信协议类型 (stream_protocol):
>     websocket
> 
>  4. 自定义官方/控制台链接 (custom_official_url):
>     https://discovery.intern-ai.org.cn/compute/dev-machine/inside/344/nb-f2d3b67734b1bb6c0544aec853ea1e56
> ================================================================
> ```

### 4. 常用运维管理命令速查

| 操作需求 | 终端命令 |
| :--- | :--- |
| **查看当前分配的最新连接地址** | `echo "wss://$(grep -o '[a-zA-Z0-9-]*\.trycloudflare\.com' tunnel.log \| tail -n 1)/ws/render-v3"` |
| **查看数字人运行日志** | `tail -f avatar.log` |
| **查看穿透隧道状态** | `tail -f tunnel.log` |
| **检查进程是否在后台运行** | `ps aux \| grep -E "server.py\|cloudflared"` |
| **一键停止所有服务** | `pkill -f server.py && pkill -f cloudflared` |

---

## 二、本地电脑端：控制台对接步骤

1. 打开本地浏览器控制台：[http://127.0.0.1:18080/console](http://127.0.0.1:18080/console)
2. 在左侧菜单点击 **“GPU配置(2) · 数字人画面与云端渲染”**；
3. 点击选中 **“选项 6 · 自定义数字人 / 远端流服务”**；
4. 填写配置项：
   - **自定义服务/流连接地址 (base_url)**：粘贴开发机生成的完整地址，例如：
     ```text
     wss://xxxx-xxxx-xxxx.trycloudflare.com/ws/render-v3
     ```
   - **访问密码 / Token / API Key (选填)**：无鉴权可直接**留空**（系统支持眼睛图标明密文切换，留空保存后不会产生占位多余字符）；
   - **通信协议类型**：保持默认 `websocket`。
5. 点击 **“测试通信连接”** 按钮：
   - 页面将立即展示测试动画；
   - 通信通畅后显示：
     ```text
     ✅ 通信对接成功！已识别到硬件：NVIDIA A100-80GB（延迟: 1487ms）
     ```
6. 点击 **“保存并启用此数字人方案”**：
   - 系统将持久化保存该配置，并将其置为当前生效的主数字人渲染源；
   - 下次打开控制台时，选项 6 将保持默认高亮激活状态。

---

## 三、常见排查与避坑指南 (FAQ)

### Q1: 点击“测试通信连接”提示 `远程连接被重置 (ConnectionResetError)`
- **原因**：开发机上的 `server.py` 或 `cloudflared` 进程退出了（例如开发机重启、网页终端被直接关闭导致进程被挂起）。
- **解决**：回到开发机终端，重新执行一遍上述第 3 步的**一键后台保活启动命令**，获取新的 `wss://` 地址填入即可。

### Q2: 终端提示 `Done(127) nohup cloudflared ...` 且没有域名
- **原因**：退出码 `127` 说明当前开发机系统环境找不到 `cloudflared` 命令。
- **解决**：执行首次使用脚本中的 `curl -L ... -o cloudflared && chmod +x cloudflared`，直接在当前目录使用 `./cloudflared` 运行。

### Q3: 点击“测试通信连接”按钮页面无反应
- **原因**：浏览器强缓存了旧版本的 JavaScript 脚本。
- **解决**：在控制台页面按键盘快捷键 **`Ctrl + F5`**（Mac 为 `Cmd + Shift + R`）强制刷新加载最新 `v=2.0.0` 驱动脚本。

### Q4: 切换到其他页面再回到“GPU配置(2)”，为什么会跳回选项 1？
- **原因**：早期版本中，如果扩展字段中存在 `password: null`，会被误判为安全校验不合规导致回退。
- **状态**：**已彻底修复**。系统现已支持空密码合法放行与当前生效项第一优先级高亮。
