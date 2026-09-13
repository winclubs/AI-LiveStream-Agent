#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AI-LiveStream-Agent 本地执行引擎一键启动脚本
"""
import os
import sys
import webbrowser
import threading
import time
from pathlib import Path

# 适配 Windows 控制台编码
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

# 核心修复：确保项目根目录在 sys.path 首位，避免 ModuleNotFoundError
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import uvicorn
from server.config import SERVER_HOST, SERVER_PORT

def open_browser_later():
    """等待 1.5 秒确保端口监听建立后，自动呼起默认浏览器打开中控台"""
    time.sleep(1.5)
    url = f"http://127.0.0.1:{SERVER_PORT}/console"
    try:
        webbrowser.open(url)
    except Exception as e:
        print(f"[!] 自动唤起浏览器失败: {e}，请手动访问: {url}")

def main():
    print("=" * 65)
    print("  AI-LiveStream-Agent 本地私有化 AI 互动直播执行引擎")
    print(f"  [中控控制台]: http://{SERVER_HOST}:{SERVER_PORT}/console")
    print(f"  [API 文档]:   http://{SERVER_HOST}:{SERVER_PORT}/docs")
    print(f"  [WebSocket]:  ws://{SERVER_HOST}:{SERVER_PORT}/ws/live_control")
    print("=" * 65)

    # 启动后台守护线程唤起浏览器
    threading.Thread(target=open_browser_later, daemon=True).start()

    # 启动 ASGI 服务
    uvicorn.run("server.app:app", host=SERVER_HOST, port=SERVER_PORT, reload=False)

if __name__ == "__main__":
    main()
