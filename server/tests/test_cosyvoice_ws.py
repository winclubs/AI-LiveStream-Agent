# -*- coding: utf-8 -*-
import pytest
from server.core.audio.cosyvoice_ws import resolve_ws_endpoint, CosyVoiceWSError


def test_resolve_ws_endpoint_workspace():
    url = "https://ws-mw0wa7jqi376y132.cn-beijing.maas.aliyuncs.com/api/v1"
    ws_url, ws_id = resolve_ws_endpoint(url)
    assert ws_url == "wss://ws-mw0wa7jqi376y132.cn-beijing.maas.aliyuncs.com/api-ws/v1/inference"
    assert ws_id == "ws-mw0wa7jqi376y132"


def test_resolve_ws_endpoint_default():
    url = "https://dashscope.aliyuncs.com/api/v1"
    ws_url, ws_id = resolve_ws_endpoint(url)
    assert ws_url == "wss://dashscope.aliyuncs.com/api-ws/v1/inference"
    assert ws_id == ""


@pytest.mark.anyio
async def test_cosyvoice_ws_missing_params():
    from server.core.audio.cosyvoice_ws import synthesize_via_cosyvoice_ws
    with pytest.raises(CosyVoiceWSError, match="未配置百炼 API Key"):
        await synthesize_via_cosyvoice_ws("", "", "voice-1", "你好")
    with pytest.raises(CosyVoiceWSError, match="缺少 Voice-ID"):
        await synthesize_via_cosyvoice_ws("https://dashscope.aliyuncs.com", "sk-123", "", "你好")
    with pytest.raises(CosyVoiceWSError, match="合成台词不能为空"):
        await synthesize_via_cosyvoice_ws("https://dashscope.aliyuncs.com", "sk-123", "voice-1", "   ")
