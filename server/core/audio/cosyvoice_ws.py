# -*- coding: utf-8 -*-
"""
阿里云百炼 Qwen-Audio-TTS / CosyVoice 官方 WebSocket 实时语音合成客户端
文档依据: https://docs.bailian.console.aliyun.com/zh/model-studio/cosyvoice-websocket-api
协议规范: 全双工双向流式 (duplex)，通过 WebSocket Binary 帧高速接收音频流
"""

import asyncio
import json
import logging
import re
import uuid
from typing import Optional
from urllib.parse import urlparse
import websockets

logger = logging.getLogger("LiveAgent.CosyVoiceWS")


class CosyVoiceWSError(Exception):
    """百炼 WebSocket 合成异常，携带可直接向前端展示的中文 detail"""
    def __init__(self, detail: str, error_code: Optional[str] = None):
        super().__init__(detail)
        self.detail = detail
        self.error_code = error_code


def resolve_ws_endpoint(base_url: str) -> tuple[str, str]:
    """
    根据用户配置的百炼 Base URL 解析出 WebSocket 端点及 Workspace ID。
    例如:
      https://ws-mw0wa7jqi376y132.cn-beijing.maas.aliyuncs.com/api/v1
      ->
      wss://ws-mw0wa7jqi376y132.cn-beijing.maas.aliyuncs.com/api-ws/v1/inference, ws-mw0wa7jqi376y132
    """
    clean = (base_url or "").strip()
    if not clean:
        clean = "https://dashscope.aliyuncs.com/api/v1"

    parsed = urlparse(clean)
    host = parsed.netloc or parsed.path.split("/")[0]

    # 提取 workspace_id
    workspace_id = ""
    match = re.search(r"^(ws-[a-zA-Z0-9]+)\.", host)
    if match:
        workspace_id = match.group(1)

    # 若已经是 ws/wss 地址
    if clean.startswith("wss://") or clean.startswith("ws://"):
        ws_url = clean
    elif "maas.aliyuncs.com" in host:
        ws_url = f"wss://{host}/api-ws/v1/inference"
    else:
        # 百炼公用域名
        ws_url = "wss://dashscope.aliyuncs.com/api-ws/v1/inference"

    return ws_url, workspace_id


def infer_model_from_voice_id(voice_id: str, default_model: Optional[str] = None) -> str:
    """
    根据百炼复刻 Voice-ID 前缀自动推导必须配对的官方底层大模型。
    例如:
      qwen-audio-3.0-tts-plus-bailian-xxxx -> qwen-audio-3.0-tts-plus
      cosyvoice-v3.5-plus-xxxx -> cosyvoice-v3.5-plus
      cosyvoice-v3.5-flash-xxxx -> cosyvoice-v3.5-flash
    官方预置系统音色 (如 longxiaochun, longanchong, longlaotie 等):
      自动路由到 cosyvoice-v1 (声音复刻专属模型 cosyvoice-v3.5-flash 会直接报 418 拒绝非复刻音色)。
    """
    clean_vid = (voice_id or "").strip().lower()

    # 1. 优先识别带有显式模型前缀的专属复刻音色
    for known in [
        "qwen-audio-3.0-tts-plus",
        "qwen-audio-3.0-tts-flash",
        "cosyvoice-v3.5-plus",
        "cosyvoice-v3.5-flash",
        "cosyvoice-v3-flash",
        "cosyvoice-v3-plus",
        "cosyvoice-v2",
        "cosyvoice-v1",
    ]:
        if clean_vid.startswith(known):
            return known

    # 2. 官方预置系统音色：以 long 或 loong 开头，且不属于用户自定义克隆 (未包含 cloned / custom)
    is_preset_voice = (clean_vid.startswith("long") or clean_vid.startswith("loong")) and "cloned" not in clean_vid and "custom" not in clean_vid
    if is_preset_voice:
        return "cosyvoice-v1"

    if default_model and default_model.strip():
        # 如果传入了 default_model，但该音色是预置音色，而 default_model 是复刻专属模型 (v3.5-flash/plus)，自动纠偏为 v1
        if is_preset_voice and ("flash" in default_model.lower() or "plus" in default_model.lower()):
            return "cosyvoice-v1"
        return default_model.strip()

    return "cosyvoice-v3.5-flash"


async def synthesize_via_cosyvoice_ws(
    base_url: str,
    api_key: str,
    voice_id: str,
    text: str,
    target_model: Optional[str] = None,
    audio_format: str = "mp3",
    sample_rate: int = 22050,
    timeout: float = 30.0,
) -> bytes:
    """
    使用阿里云百炼官方 WebSocket 双向全双工流式协议合成语音。

    :param base_url: 百炼端点或专属业务空间 URL
    :param api_key: 百炼 API Key (sk-...)
    :param voice_id: 专属声音复刻 Voice-ID 或系统音色
    :param text: 待合成的台词
    :param target_model: 模型名称 (默认根据 voice_id 自适应推导)
    :param audio_format: 音频编码格式 (mp3/pcm/wav)
    :param sample_rate: 采样率 (默认 22050)
    :param timeout: 超时时间 (秒)
    :return: 完整的 MP3/音频二进制数据
    """
    if not api_key:
        raise CosyVoiceWSError("未配置百炼 API Key，无法连接云端大模型合成台词。")
    if not voice_id:
        raise CosyVoiceWSError("缺少 Voice-ID，无法指定声线合成台词。")
    if not text.strip():
        raise CosyVoiceWSError("合成台词不能为空。")

    ws_url, workspace_id = resolve_ws_endpoint(base_url)
    model = infer_model_from_voice_id(voice_id, target_model)

    headers = {
        "Authorization": f"Bearer {api_key.strip()}",
        "user-agent": "AI-LiveStream-Agent/1.0",
    }
    if workspace_id:
        headers["X-DashScope-WorkSpace"] = workspace_id

    task_id = str(uuid.uuid4())
    logger.info(f"发起百炼 WebSocket 语音合成: url={ws_url}, model={model}, voice={voice_id}, task_id={task_id}")

    audio_chunks: list[bytes] = []

    try:
        async with websockets.connect(
            ws_url,
            additional_headers=headers,
            open_timeout=10.0,
            close_timeout=5.0,
        ) as ws:
            # 1. 发送 run-task 指令
            run_task_payload = {
                "header": {
                    "action": "run-task",
                    "task_id": task_id,
                    "streaming": "duplex",
                },
                "payload": {
                    "task_group": "audio",
                    "task": "tts",
                    "function": "SpeechSynthesizer",
                    "model": model,
                    "parameters": {
                        "text_type": "PlainText",
                        "voice": voice_id,
                        "format": audio_format,
                        "sample_rate": sample_rate,
                        "volume": 50,
                        "rate": 1.0,
                        "pitch": 1.0,
                    },
                    "input": {},
                },
            }
            await ws.send(json.dumps(run_task_payload))

            # 2. 状态机循环
            task_started = False
            task_finished = False

            while True:
                try:
                    msg = await asyncio.wait_for(ws.recv(), timeout=timeout)
                except asyncio.TimeoutError:
                    raise CosyVoiceWSError(f"百炼 WebSocket 语音合成等待服务端响应超时（超过 {timeout} 秒）")

                if isinstance(msg, bytes):
                    # 二进制音频流帧
                    audio_chunks.append(msg)
                elif isinstance(msg, str):
                    try:
                        data = json.loads(msg)
                    except Exception:
                        continue

                    header = data.get("header") or {}
                    action = header.get("action") or header.get("event")

                    if action == "task-started":
                        task_started = True
                        # 发送待合成文本
                        continue_payload = {
                            "header": {
                                "action": "continue-task",
                                "task_id": task_id,
                                "streaming": "duplex",
                            },
                            "payload": {
                                "input": {"text": text},
                            },
                        }
                        await ws.send(json.dumps(continue_payload))

                        # 紧接着发送 finish-task，告知服务端当前台词输入完毕
                        finish_payload = {
                            "header": {
                                "action": "finish-task",
                                "task_id": task_id,
                                "streaming": "duplex",
                            },
                            "payload": {
                                "input": {},
                            },
                        }
                        await ws.send(json.dumps(finish_payload))

                    elif action == "task-finished":
                        task_finished = True
                        break

                    elif action == "task-failed":
                        err_code = header.get("error_code") or ""
                        err_msg = header.get("error_message") or str(data)
                        logger.warning(f"百炼 WebSocket 任务失败: code={err_code}, msg={err_msg}")
                        if "418" in err_msg or "418" in err_code:
                            friendly = (
                                f"百炼专属复刻模型【{model}】拒绝了该 Voice-ID【{voice_id}】。"
                                "请确认该 Voice-ID 是在百炼控制台由相同模型（如 cosyvoice-v3.5-flash）复刻生成的专属音色。"
                            )
                        elif "401" in str(err_code) or "Unauthorized" in str(err_msg):
                            friendly = "百炼 API Key 鉴权失败，请检查设置中的百炼 API Key 是否有效。"
                        else:
                            friendly = f"百炼语音合成服务报错: [{err_code}] {err_msg}"
                        raise CosyVoiceWSError(friendly, error_code=err_code)

        if not audio_chunks:
            raise CosyVoiceWSError("百炼 WebSocket 任务结束但未返回任何有效音频数据。")

        full_audio = b"".join(audio_chunks)
        logger.info(f"百炼 WebSocket 语音合成完成，共获取音频流 {len(full_audio)} 字节")
        return full_audio

    except websockets.exceptions.InvalidStatus as e:
        status_code = getattr(e.response, "status_code", None)
        if status_code == 401:
            raise CosyVoiceWSError("百炼 API Key 认证失败 (HTTP 401)，请在系统设置中填入有效百炼 Key。")
        elif status_code == 404:
            raise CosyVoiceWSError(f"百炼 WebSocket 端点不存在 (HTTP 404): {ws_url}")
        else:
            raise CosyVoiceWSError(f"连接百炼 WebSocket 失败 (HTTP {status_code})")
    except CosyVoiceWSError:
        raise
    except Exception as e:
        logger.error(f"百炼 WebSocket 合成未知异常: {e}", exc_info=True)
        raise CosyVoiceWSError(f"百炼 WebSocket 语音合成连接异常: {e}")
