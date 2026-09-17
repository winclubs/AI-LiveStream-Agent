# -*- coding: utf-8 -*-
"""
数字人核心演进阶段四全功能验证测试套件 (Phase 4 Test Suite)
覆盖：
1. 全双工 ASR 语音与麦克风极速打断系统 (Full-Duplex ASR & VAD & Instant Interrupt)；
2. ASR 语音离线识别转录 API (/asr/transcribe)；
3. 原生 WebRTC (WHEP) 极速低延迟协商与 PeerConnection 生命周期管理 (/webrtc/whep)；
4. 带货讲解切片一键录制、音画封装、列表查询与清理删除全流程 (/record/*)；
5. BaseAvatarDriver 多路音画分发管道集成验证。
"""
import asyncio
import io
import time
import wave
from pathlib import Path
import cv2
import numpy as np
import pytest
from httpx import ASGITransport, AsyncClient
from aiortc import RTCPeerConnection

from server.app import app
from server.core.audio.full_duplex_asr import ASRSession, get_full_duplex_asr_manager
from server.core.avatar import get_active_avatar_driver
from server.core.avatar.drivers import Procedural2DDriver
from server.core.media.webrtc_streamer import get_webrtc_stream_manager, AvatarVideoTrack
from server.core.media.recorder import get_record_manager, VideoAudioRecorder


@pytest.fixture
def anyio_backend():
    return "asyncio"


def _generate_sine_pcm(duration_sec: float = 0.5, sample_rate: int = 16000, freq: float = 440.0, volume: float = 0.8) -> bytes:
    """生成测试用高能正弦波 PCM 音频 (模拟开嗓说话)"""
    t = np.linspace(0, duration_sec, int(sample_rate * duration_sec), endpoint=False)
    waveform = volume * np.sin(2 * np.pi * freq * t)
    pcm_data = (waveform * 32767).astype(np.int16).tobytes()
    return pcm_data


def _generate_silent_pcm(duration_sec: float = 0.5, sample_rate: int = 16000) -> bytes:
    """生成测试用静音 PCM 音频"""
    return np.zeros(int(sample_rate * duration_sec), dtype=np.int16).tobytes()


def _create_test_wav_bytes(duration_sec: float = 0.3) -> bytes:
    """快速生成带有标准的 WAV 头部的音频字节流"""
    pcm = _generate_sine_pcm(duration_sec)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(16000)
        wf.writeframes(pcm)
    return buf.getvalue()


# ============================================================================
# 1. 全双工 ASR 语音活动检测与极速打断验证
# ============================================================================
@pytest.mark.anyio
async def test_full_duplex_asr_vad_and_instant_interrupt():
    """验证 VAD 能量灵敏度与用户说话瞬间触发数字人闭嘴打断 (flush_talk)"""
    interrupted_called = False

    def on_interrupt_callback():
        nonlocal interrupted_called
        interrupted_called = True

    session = ASRSession(
        session_id="test_session_1",
        energy_threshold=200.0,
        silence_timeout_sec=0.3,
        on_interrupt=on_interrupt_callback,
    )

    driver = get_active_avatar_driver()
    await driver.start()
    # 模拟数字人当前正在说话播报
    driver._speaking = True
    driver._last_speech_time = time.time()
    assert driver.is_speaking() is True

    # 1. 输入静音帧，不应判定说话，也不应触发打断
    silent_chunk = _generate_silent_pcm(0.04)
    res_silent = session.feed_pcm(silent_chunk)
    assert res_silent["has_voice"] is False
    assert res_silent["is_speaking"] is False
    assert interrupted_called is False
    assert driver.is_speaking() is True

    # 2. 输入高能量人声发言帧，连续送入触发说话态与极速打断！
    voice_chunk = _generate_sine_pcm(0.04, volume=0.9)
    session.feed_pcm(voice_chunk)
    res_voice = session.feed_pcm(voice_chunk)  # 第 2 帧，达到触发阈值

    assert res_voice["has_voice"] is True
    assert res_voice["is_speaking"] is True
    assert "speech_start" in res_voice["events"]
    assert "interrupted" in res_voice["events"]
    assert interrupted_called is True

    # 验证驱动已执行打断，重置口型回待机状态
    await asyncio.sleep(0.05)
    assert driver.is_speaking() is False

    # 3. 模拟后续静音，持续 0.4s (大于 silence_timeout_sec 0.3s)，应触发发言结束 speech_end
    for _ in range(12):
        res_end = session.feed_pcm(silent_chunk)
        if "speech_end" in res_end["events"]:
            break
        await asyncio.sleep(0.03)

    assert session.is_speaking is False


# ============================================================================
# 2. ASR 音频离线识别端点测试
# ============================================================================
@pytest.mark.anyio
async def test_asr_transcribe_http_endpoint():
    """测试 POST /api/v1/live/asr/transcribe 单段音频转录端点"""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        wav_content = _create_test_wav_bytes(0.3)
        files = {"file": ("test_mic.wav", wav_content, "audio/wav")}
        res = await client.post("/api/v1/live/asr/transcribe", files=files)
        assert res.status_code == 200
        json_data = res.json()
        assert json_data["code"] == 0
        assert "data" in json_data
        assert json_data["data"]["file_size"] == len(wav_content)


# ============================================================================
# 3. WebRTC (WHEP) 极速低延迟流媒体端点协商测试
# ============================================================================
@pytest.mark.anyio
async def test_webrtc_whep_sdp_negotiation_and_lifecycle():
    """验证 WHEP 标准端点接收 SDP Offer、协商生成 SDP Answer 并能正常释放资源"""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # 1. 模拟前端 WebRTC 客户端创建 PeerConnection 与 recvonly Video Track
        client_pc = RTCPeerConnection()
        client_pc.addTransceiver("video", direction="recvonly")
        offer = await client_pc.createOffer()
        await client_pc.setLocalDescription(offer)

        # 2. 发起 WHEP POST 请求
        headers = {"Content-Type": "application/sdp", "Accept": "application/sdp"}
        res = await client.post("/api/v1/live/webrtc/whep", content=offer.sdp, headers=headers)
        assert res.status_code == 201
        assert "application/sdp" in res.headers.get("content-type", "")
        location_header = res.headers.get("location", "")
        assert "/api/v1/live/webrtc/whep/" in location_header

        session_id = location_header.split("/")[-1]
        answer_sdp = res.text
        assert "v=0" in answer_sdp
        assert "m=video" in answer_sdp

        # 3. 查询 WebRTC 服务健康与活跃连接指标
        status_res = await client.get("/api/v1/live/webrtc/status")
        assert status_res.status_code == 200
        status_data = status_res.json()["data"]
        assert status_data["active_connections"] >= 1

        # 4. 断开并释放该会话
        del_res = await client.delete(f"/api/v1/live/webrtc/whep/{session_id}")
        assert del_res.status_code == 200
        assert del_res.json()["code"] == 0

        await client_pc.close()


# ============================================================================
# 4. 短视频带货切片录制系统 (/record) 全生命周期测试
# ============================================================================
@pytest.mark.anyio
async def test_video_slice_recording_pipeline(tmp_path):
    """验证短视频切片一键启动、帧喂入、FFmpeg 标准 MP4 封装、列表查询与删除"""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # 1. 启动录制
        start_payload = {
            "title": "爆款真丝衬衫讲解切片",
            "sku": "SKU-SILK-001",
            "width": 320,
            "height": 240,
        }
        res_start = await client.post("/api/v1/live/record/start", json=start_payload)
        assert res_start.status_code == 200
        start_json = res_start.json()
        assert start_json["code"] == 0
        rec_id = start_json["data"]["record_id"]

        # 2. 查询当前录制中状态
        res_status = await client.get("/api/v1/live/record/status")
        assert res_status.status_code == 200
        status_json = res_status.json()["data"]
        assert status_json["is_recording"] is True
        assert status_json["record_id"] == rec_id

        # 3. 模拟录制管道喂入 12 帧图像与配套音频
        rec_mgr = get_record_manager()
        assert rec_mgr.is_recording() is True

        dummy_frame = np.full((240, 320, 3), 45, dtype=np.uint8)
        cv2.circle(dummy_frame, (160, 120), 40, (0, 200, 255), -1)
        audio_chunk = _generate_sine_pcm(0.04)

        for _ in range(12):
            rec_mgr.feed_frame(dummy_frame)
            rec_mgr.feed_audio(audio_chunk)

        # 4. 停止录制并封装 MP4
        res_stop = await client.post("/api/v1/live/record/stop")
        assert res_stop.status_code == 200
        stop_json = res_stop.json()
        assert stop_json["code"] == 0
        record_info = stop_json["data"]

        # 验证文件真实生成且不为空
        final_video = Path(record_info["video_path"])
        assert final_video.exists()
        assert final_video.stat().st_size > 0
        assert record_info["total_frames"] >= 12
        assert record_info["sku"] == "SKU-SILK-001"

        # 5. 查询切片录制历史列表
        res_list = await client.get("/api/v1/live/record/list")
        assert res_list.status_code == 200
        list_json = res_list.json()
        assert list_json["total"] >= 1
        found = any(item["record_id"] == rec_id for item in list_json["data"])
        assert found is True

        # 6. 删除切片并验证清理
        res_del = await client.delete(f"/api/v1/live/record/{rec_id}")
        assert res_del.status_code == 200
        assert res_del.json()["code"] == 0
        assert not final_video.exists()


# ============================================================================
# 5. BaseAvatarDriver 多路音画分发管道集成测试
# ============================================================================
@pytest.mark.anyio
async def test_driver_multipath_frame_distribution():
    """验证 BaseAvatarDriver.publish_frame 能无缝向 WebRTC 视窗与正在录制的切片管道广播"""
    driver = Procedural2DDriver()
    await driver.start()

    # 启动录制任务
    rec_mgr = get_record_manager()
    rec_mgr.start_recording(title="管道多路分发集成测试", width=320, height=240)
    assert rec_mgr.is_recording() is True

    # 准备测试帧
    test_frame = np.full((240, 320, 3), 60, dtype=np.uint8)
    test_pcm = _generate_sine_pcm(0.04)

    # 驱动分发音画帧
    driver.publish_frame(test_frame, pcm_bytes=test_pcm)
    assert driver.total_published_frames >= 1

    # 检查活跃录制器是否捕获到该帧
    assert rec_mgr.active_recorder.total_frames >= 1

    # 检查 WebRTC 模块是否收到最新帧
    webrtc_mgr = get_webrtc_stream_manager()
    status = webrtc_mgr.get_status()
    assert status["has_live_frame"] is True

    rec_mgr.stop_recording()
    await driver.stop()
