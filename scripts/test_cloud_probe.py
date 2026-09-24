# -*- coding: utf-8 -*-
import asyncio
import json
import sqlite3
import time
import websockets
from server.config import decrypt_secret

async def main():
    conn = sqlite3.connect("data/live_agent.db")
    cur = conn.cursor()
    cur.execute(
        "SELECT id, provider_name, base_url, encrypted_api_key FROM api_provider_configs WHERE is_active=1 AND config_group='neural_renderer'"
    )
    row = cur.fetchone()
    if not row:
        print("[-] 未在数据库中找到处于激活状态的云端 GPU (neural_renderer) 配置。")
        return

    cfg_id, p_name, url, enc_key = row
    token = decrypt_secret(enc_key) if enc_key else ""
    print(f"[*] 目标云端节点配置: {cfg_id}")
    print(f"[*] 节点类型: {p_name}")
    print(f"[*] 节点 URL: {url}")
    print(f"[*] 密钥配置: {'已配置 Token' if token else '未配置 Token (免密)'}")
    print("[*] 正在向云端 GPU 发起连接握手...")

    try:
        t0 = time.time()
        async with websockets.connect(url, open_timeout=6.0, close_timeout=2.0) as ws:
            connect_ms = int((time.time() - t0) * 1000)
            print(f"[+] TCP/WebSocket 连接成功! 建立连接耗时: {connect_ms}ms")

            if token:
                from server.core.media.sidecar_protocol import build_auth_message
                await ws.send(json.dumps(build_auth_message(token)))

            raw_resp = await asyncio.wait_for(ws.recv(), timeout=5.0)
            print(f"[+] 云端握手首帧响应: {raw_resp}")

            # 进行多轮压测
            print("[*] 开始进行 10 轮端到端性能与压力响应测试...")
            latencies = []
            for i in range(10):
                t_req = time.time()
                # 发送探活 ping
                await ws.send(json.dumps({"event": "ping", "seq": i, "timestamp": time.time()}))
                resp = await asyncio.wait_for(ws.recv(), timeout=5.0)
                dur = (time.time() - t_req) * 1000.0
                latencies.append(dur)
                print(f"    - 轮次 #{i+1}: 往返耗时 {dur:.1f}ms, 响应长度: {len(resp)} 字节")
                await asyncio.sleep(0.05)

            avg_lat = sum(latencies) / len(latencies)
            max_lat = max(latencies)
            min_lat = min(latencies)
            print(f"[+] 压测完成! 平均延迟: {avg_lat:.1f}ms, 极值: {min_lat:.1f}ms ~ {max_lat:.1f}ms")

    except Exception as e:
        print(f"[-] 连接或测试失败: {type(e).__name__} -> {e}")

if __name__ == "__main__":
    asyncio.run(main())
