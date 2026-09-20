# -*- coding: utf-8 -*-
"""
MOSS-TTS-Nano 设置试听与声音克隆 API 集成测试
"""
import io
import httpx
import pytest
from httpx import ASGITransport, AsyncClient
from unittest.mock import MagicMock, patch

from server.app import app


@pytest.mark.anyio
async def test_moss_tts_preview_api_flow():
    """验证 /api/v1/settings/tts/preview 针对 moss_tts_nano 的发声通道"""
    transport = ASGITransport(app=app)
    orig_post = httpx.AsyncClient.post

    async def selective_post(self, url, *args, **kwargs):
        url_str = str(url)
        if "9880" in url_str:
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.content = b"RIFF\x24\x00\x00\x00WAVEfmt \x10\x00\x00\x00data\x00\x00\x00\x00"
            return mock_resp
        return await orig_post(self, url, *args, **kwargs)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # 1. 模拟 MOSS 端点 (9880) 在线，成功接收到音频
        with patch.object(httpx.AsyncClient, "post", side_effect=selective_post, autospec=True):
            resp = await client.post(
                "/api/v1/settings/tts/preview",
                json={
                    "provider_name": "moss_tts_nano",
                    "voice_name": "moss_female_host_01",
                    "text": "你好，这是 MOSS 试听"
                }
            )
            assert resp.status_code == 200
            assert resp.headers["content-type"].startswith("audio/")

        # 2. 模拟 MOSS 服务未启动时，直接命中系统预置高保真静态音频
        async def fail_moss_post(self, url, *args, **kwargs):
            url_str = str(url)
            if "9880" in url_str:
                raise Exception("Connection refused")
            return await orig_post(self, url, *args, **kwargs)

        with patch.object(httpx.AsyncClient, "post", side_effect=fail_moss_post, autospec=True):
            for v_name in ["moss_female_host_01", "moss_male_host_02", "moss_female_warm_03", "moss_female_lively_04"]:
                resp2 = await client.post(
                    "/api/v1/settings/tts/preview",
                    json={
                        "provider_name": "moss_tts_nano",
                        "voice_name": v_name,
                        "text": ""
                    }
                )
                assert resp2.status_code == 200
                assert resp2.headers["content-type"].startswith("audio/")
                # 必须是真实的完整真人音频，绝非 144 字节的伪造空音频
                assert len(resp2.content) > 10000


@pytest.mark.anyio
async def test_moss_tts_clone_api_flow():
    """验证 /api/v1/voices/clone 在 moss_tts_nano 模式下的零样本克隆处理"""
    transport = ASGITransport(app=app)
    orig_post = httpx.AsyncClient.post

    async def mock_moss_clone_post(self, url, *args, **kwargs):
        url_str = str(url)
        if "9880" in url_str:
            mock_synth = MagicMock()
            mock_synth.status_code = 200
            mock_synth.content = b"RIFF\x24\x00\x00\x00WAVEfake_cloned_preview_audio_bytes" + (b"\x00" * 1024)
            return mock_synth
        return await orig_post(self, url, *args, **kwargs)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        fake_wav = io.BytesIO(b"RIFF\x24\x00\x00\x00WAVEfmt \x10\x00\x00\x00\x01\x00\x01\x00\x80>\x00\x00\x00}\x00\x00\x02\x00\x10\x00data\x00\x00\x00\x00")
        fake_wav.name = "sample.wav"

        with patch.object(httpx.AsyncClient, "post", side_effect=mock_moss_clone_post, autospec=True):
            resp = await client.post(
                "/api/v1/voices/clone",
                data={
                    "name": "测试MOSS主播",
                    "speed": "1.0",
                    "volume": "1.0",
                    "provider_name": "moss_tts_nano"
                },
                files={"audio_file": ("sample.wav", fake_wav, "audio/wav")}
            )
            assert resp.status_code == 200
            data = resp.json()
            assert data["code"] == 0
            assert data["data"]["provider_name"] == "moss_tts_nano"
            assert "MOSS-TTS-Nano" in data["message"]
            assert data["data"]["synthesis_status"] == "ready"

            voice_id = data["data"]["id"]
            # 闭环验证：音色资产库在线试听接口正常返回音频流，绝不报 400 阻止播放
            prev_resp = await client.get(f"/api/v1/voices/{voice_id}/preview")
            assert prev_resp.status_code == 200
            assert prev_resp.headers["content-type"].startswith("audio/")
            assert len(prev_resp.content) > 512
