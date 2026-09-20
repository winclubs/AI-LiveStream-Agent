# -*- coding: utf-8 -*-
"""
端到端集成测试：本地 CloudSidecarDriver 音频推送管道实体化、云端帧回传接管与打断 (任务 1.2 & 1.3)
"""
import asyncio
import socket
import sys
from pathlib import Path

import uvicorn
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from scripts.cloud_sidecar_bootstrap import create_sidecar_server_code
from server.core.avatar import AvatarDriverFactory, CloudSidecarDriver
from server.adapters.media.media_router import global_media_router


def get_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.mark.anyio
async def test_cloud_sidecar_driver_audio_pipeline_and_video_route():
    """验证 CloudSidecarDriver 切片推送 PCM、云端帧接收与 global_media_router 画面接管"""
    port = get_free_port()
    gpu_desc = "NVIDIA Tesla T4 (15360MB 显存)"
    server_code = create_sidecar_server_code(port=port, gpu_info=gpu_desc)
    
    scope = {}
    exec(server_code, scope)
    app = scope["app"]
    
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error")
    server = uvicorn.Server(config)
    server_task = asyncio.create_task(server.serve())

    for _ in range(30):
        await asyncio.sleep(0.1)
        if server.started:
            break

    try:
        # 1. 实例化并启动 CloudSidecarDriver
        ws_url = f"ws://127.0.0.1:{port}/ws/render-v3"
        driver = AvatarDriverFactory.create("cloud_sidecar", {"sidecar_url": ws_url})
        assert isinstance(driver, CloudSidecarDriver)

        started = await driver.start()
        assert started is True
        assert driver.tunnel_status in ("connected", "connecting")
        
        # 验证底层驱动已成功挂载到 global_media_router
        assert global_media_router.sidecar_driver is not None

        # 2. 推送 600ms (30 帧 20ms) PCM 音频流
        sample_rate = 16000
        duration_sec = 0.6
        total_samples = int(sample_rate * duration_sec)
        # 生成带正弦波的 PCM 数据
        import numpy as np
        t = np.linspace(0, duration_sec, total_samples, endpoint=False)
        wave = (np.sin(2 * np.pi * 440 * t) * 15000).astype(np.int16)
        pcm_bytes = wave.tobytes()

        pushed = await driver.push_audio_chunk(pcm_bytes, {"sample_rate": sample_rate, "text": "你好，欢迎来到直播间"})
        assert pushed is True
        assert driver.total_audio_chunks_sent == 30
        assert driver.total_audio_bytes_sent == len(pcm_bytes)
        assert driver.is_speaking() is True

        # 3. 等待云端 GPU 推理完成并回传视频帧，验证本地媒体路由接管
        received_frames = False
        latest_jpeg = b""
        for _ in range(50):
            await asyncio.sleep(0.1)
            latest_jpeg = global_media_router.get_latest_jpeg()
            if latest_jpeg and latest_jpeg.startswith(b"\xff\xd8"):
                received_frames = True
                break

        assert received_frames is True, "未在预期时间内收到来自云端 GPU 的 JPEG 视频帧"
        assert latest_jpeg.startswith(b"\xff\xd8") and latest_jpeg.endswith(b"\xff\xd9")

        # 4. 测试极速打断 flush_talk
        await driver.flush_talk()
        assert driver.is_speaking() is False

        # 5. 安全关闭并解绑
        await driver.stop()
        assert driver.tunnel_status == "disconnected"
        assert global_media_router.sidecar_driver is None

    finally:
        server.should_exit = True
        await server_task
