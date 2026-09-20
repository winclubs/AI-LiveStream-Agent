# -*- coding: utf-8 -*-
"""
单元测试：云端 GPU Sidecar 真实实时唇形渲染引擎与协议闭环验证 (任务 1.1)
"""
import asyncio
import json
import socket
import sys
import time
from pathlib import Path

import uvicorn
import numpy as np

# 将项目根目录加入 sys.path
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from server.core.media.sidecar_protocol import (
    PROTOCOL_VERSION,
    ENVELOPE_MAGIC,
    KIND_AUDIO,
    KIND_VIDEO,
    decode_envelope,
    decode_video_frame,
    encode_audio_frame,
    build_auth_message,
    validate_auth_reply
)
from server.core.media.audio_frame import AudioFrame, AudioFormat
from scripts.cloud_sidecar_bootstrap import create_sidecar_server_code


def get_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


import pytest


@pytest.mark.anyio
async def test_cloud_sidecar_engine_e2e():
    """测试云端渲染服务端生成脚本、握手协商、音频输入驱动唇形以及视频输出全流程"""
    port = get_free_port()
    gpu_desc = "NVIDIA Tesla T4 (15360MB 显存)"
    server_code = create_sidecar_server_code(port=port, gpu_info=gpu_desc)
    
    # 动态编译与执行服务端
    scope = {}
    exec(server_code, scope)
    app = scope["app"]
    
    # 在后台启动 uvicorn 实例
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    server_task = asyncio.create_task(server.serve())

    # 等待服务端口就绪
    for _ in range(30):
        await asyncio.sleep(0.1)
        if server.started:
            break

    try:
        import websockets
        uri = f"ws://127.0.0.1:{port}/ws/render-v3"
        async with websockets.connect(uri) as ws:
            # 1. 接收第一帧握手问候
            init_ack_raw = await ws.recv()
            init_ack = json.loads(init_ack_raw)
            assert init_ack.get("event") == "handshake_ack"
            assert init_ack.get("device") == gpu_desc
            print("[PASS] 收到服务端欢迎握手回执:", init_ack["event"])

            # 2. 发起 auth 身份验证与能力协商
            await ws.send(json.dumps(build_auth_message("test_token_123")))
            auth_reply_raw = await ws.recv()
            auth_reply = json.loads(auth_reply_raw)
            negotiated = validate_auth_reply(auth_reply)
            assert negotiated["selected_version"] == PROTOCOL_VERSION
            caps = negotiated["capabilities"]
            assert caps["renderer_available"] is True
            assert caps["neural_lipsync"] is True
            assert caps["streaming_video"] is True
            assert caps["supports_credit"] is True
            print("[PASS] 协议协商验证通过, backend:", caps["render_backends"][0]["id"])

            # 3. 发起 render_open 渲染事务
            req_id = "req_test_001"
            audio_id = "aud_001"
            open_msg = {
                "event": "render_open",
                "request_id": req_id,
                "audio_id": audio_id,
                "format": {"codec": "pcm_s16le", "sample_rate": 16000, "channels": 1, "sample_width": 2},
                "audio_generation": 1,
                "session_generation": 1
            }
            await ws.send(json.dumps(open_msg))

            # 接收 render_accepted 与 render_started
            acc_raw = await ws.recv()
            acc_json = json.loads(acc_raw)
            assert acc_json["event"] == "render_accepted"
            assert acc_json["initial_credit"] > 0
            print("[PASS] 事务创建成功，获得初始渲染信用 credit:", acc_json["initial_credit"])

            started_raw = await ws.recv()
            started_json = json.loads(started_raw)
            assert started_json["event"] == "render_started"

            # 4. 生成 100ms 正弦波音频 (16000Hz, 16-bit, 1600 samples)
            t = np.linspace(0, 0.1, 1600, endpoint=False)
            pcm_samples = (np.sin(2 * np.pi * 440 * t) * 16000).astype(np.int16)
            pcm_bytes = pcm_samples.tobytes()

            audio_frame = AudioFrame(
                data=pcm_bytes,
                format=AudioFormat(codec="pcm_s16le", sample_rate=16000, channels=1, sample_width_bytes=2),
                audio_id=audio_id,
                sequence=0,
                pts_samples=0,
                audio_generation=1,
                session_generation=1,
                is_first=True,
                is_final=False
            )
            audio_pkg = encode_audio_frame(req_id, audio_frame)
            
            # 推送音频数据
            t_start = time.perf_counter()
            await ws.send(audio_pkg)

            # 5. 接收信用回报与视频帧
            received_video_frames = []
            while True:
                resp = await asyncio.wait_for(ws.recv(), timeout=5.0)
                if isinstance(resp, str):
                    ctrl = json.loads(resp)
                    if ctrl.get("event") == "render_credit":
                        continue
                elif isinstance(resp, bytes):
                    # 解码视频帧
                    v_frame = decode_video_frame(resp)
                    assert v_frame.request_id == req_id
                    assert v_frame.audio_id == audio_id
                    assert len(v_frame.jpeg) > 100
                    # JPEG 文件头校验 0xFF, 0xD8
                    assert v_frame.jpeg[:2] == b"\xff\xd8"
                    received_video_frames.append(v_frame)
                    t_infer = (time.perf_counter() - t_start) * 1000
                    print(f"[PASS] 成功接收生成视频帧 seq={v_frame.sequence}, pts={v_frame.pts_samples}, 大小={len(v_frame.jpeg)} 字节, 耗时={t_infer:.1f}ms")
                    break

            # 6. 发送 render_finish 并接收 render_complete
            await ws.send(json.dumps({"event": "render_finish", "request_id": req_id, "audio_id": audio_id}))
            
            while True:
                resp = await asyncio.wait_for(ws.recv(), timeout=5.0)
                if isinstance(resp, bytes):
                    continue
                ctrl = json.loads(resp)
                if ctrl.get("event") == "render_complete":
                    print("[PASS] 收到事务完成回执 render_complete:", ctrl)
                    assert ctrl["rendered_frames"] >= 1
                    break

            print(f"[SUCCESS] 任务 1.1 全链路测试圆满成功！共生成 {len(received_video_frames)} 帧合规真人唇形视频帧！")

    finally:
        server.should_exit = True
        await server_task


if __name__ == "__main__":
    asyncio.run(test_cloud_sidecar_engine_e2e())
