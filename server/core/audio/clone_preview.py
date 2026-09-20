import uuid
import json
import base64
import logging
from pathlib import Path
from typing import Optional
import httpx
from server.config import DATA_DIR, decrypt_secret
from server.database.db import AsyncSessionLocal
from server.database.models import ApiProviderConfig
from sqlalchemy import select

logger = logging.getLogger("LiveAgent.ClonePreview")
# 必须复用应用级 DATA_DIR（支持 LIVE_AGENT_DATA_DIR 重定向），禁止硬编码相对路径，
# 否则服务换目录/测试隔离时试听文件会落到错误位置，导致“合成了却找不到”。
VOICES_DIR = DATA_DIR / "voices"
VOICES_DIR.mkdir(parents=True, exist_ok=True)

# 百炼全局 API 域名（文件上传凭证等通用接口必须走该域名，而非业务空间专属域名）
DASHSCOPE_API_BASE = "https://dashscope.aliyuncs.com/api/v1"


class DashscopeCloneError(Exception):
    """百炼声音复刻/合成链路的可呈现错误（detail 已是可直接展示给用户的中文说明）"""

    def __init__(self, detail: str, status: Optional[int] = None):
        super().__init__(detail)
        self.detail = detail
        self.status = status


async def get_active_cosyvoice_config():
    """从数据库安全读取已配置并启用的 CosyVoice/TTS 配置"""
    try:
        async with AsyncSessionLocal() as session:
            q = select(ApiProviderConfig).where(
                (ApiProviderConfig.config_group == "tts") & (ApiProviderConfig.is_active == 1)
            )
            res = await session.execute(q)
            cfg = res.scalars().first()
            if not cfg:
                q_any = select(ApiProviderConfig).where(
                    (ApiProviderConfig.config_group == "tts") & (ApiProviderConfig.provider_name.like("%cosy%"))
                )
                res_any = await session.execute(q_any)
                cfg = res_any.scalars().first()
            if cfg:
                base_url = (cfg.base_url or "").strip().rstrip("/")
                api_key = decrypt_secret(cfg.encrypted_api_key) if cfg.encrypted_api_key else ""
                return base_url, api_key
    except Exception as e:
        logger.warning(f"读取数据库 TTS 配置失败: {e}")
    return "", ""


def _customization_url(base_url: str) -> str:
    """声音复刻管理端点：业务空间域名优先，否则走百炼全局域名。"""
    clean = (base_url or "").strip().rstrip("/")
    if "maas.aliyuncs.com" in clean:
        return f"{clean}/services/audio/tts/customization"
    return f"{DASHSCOPE_API_BASE}/services/audio/tts/customization"


def _sanitize_prefix(raw: str) -> str:
    """复刻音色名称前缀仅允许数字与英文字母，不超过 10 个字符。"""
    keep = "".join(ch for ch in (raw or "") if ch.isascii() and ch.isalnum())[:10]
    return keep or "cloned"


async def upload_audio_to_dashscope_tmp(api_key: str, model: str, file_path: str) -> str:
    """
    将本地音频上传至百炼免费临时存储，返回 oss:// 临时 URL（有效期 48 小时）。
    官方机制：GET /api/v1/uploads?action=getPolicy&model=... 取凭证 → POST multipart 到 upload_host。
    使用该 oss:// URL 调用复刻接口时，必须在请求头携带 X-DashScope-OssResourceResolve: enable。
    """
    if not api_key:
        raise DashscopeCloneError("未配置百炼 API Key，无法把音频样本上传到云端复刻。")
    src = Path(file_path)
    if not src.exists():
        raise DashscopeCloneError(f"本地音频样本不存在：{file_path}")
    if src.stat().st_size <= 0:
        raise DashscopeCloneError("本地音频样本为空，无法上传复刻。")

    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(connect=10.0, read=60.0, write=30.0, pool=10.0)) as client:
            p_resp = await client.get(
                f"{DASHSCOPE_API_BASE}/uploads",
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                params={"action": "getPolicy", "model": model},
            )
            if p_resp.status_code != 200:
                raise DashscopeCloneError(
                    f"获取百炼文件上传凭证失败（HTTP {p_resp.status_code}）：{p_resp.text[:200]}",
                    status=p_resp.status_code,
                )
            policy = (p_resp.json() or {}).get("data") or {}
            upload_host = policy.get("upload_host")
            upload_dir = (policy.get("upload_dir") or "").strip("/")
            if not upload_host or not upload_dir:
                raise DashscopeCloneError("百炼返回的上传凭证缺少 upload_host/upload_dir。")
            safe_name = f"clone_{uuid.uuid4().hex}{src.suffix.lower() or '.wav'}"
            key = f"{upload_dir}/{safe_name}"
            # OSS 上传表单字段（文本）+ 音频文件域（file 必须在最后）
            form_data = {
                "OSSAccessKeyId": policy.get("oss_access_key_id") or "",
                "Signature": policy.get("signature") or "",
                "policy": policy.get("policy") or "",
                "x-oss-object-acl": policy.get("x_oss_object_acl") or "private",
                "x-oss-forbid-overwrite": policy.get("x_oss_forbid_overwrite") or "true",
                "key": key,
                "success_action_status": "200",
            }
            files = {
                "file": (safe_name, src.read_bytes(), "application/octet-stream")
            }
            logger.info(f"百炼临时存储上传: host={upload_host}, key={key}, 文件大小={src.stat().st_size}B")
            u_resp = await client.post(upload_host, data=form_data, files=files)
            if u_resp.status_code != 200:
                raise DashscopeCloneError(
                    f"上传音频到百炼临时存储失败（HTTP {u_resp.status_code}，目标: {upload_host}）：{u_resp.text[:200]}",
                    status=u_resp.status_code,
                )
    except DashscopeCloneError:
        raise
    except httpx.TimeoutException:
        logger.warning("百炼临时存储上传超时", exc_info=True)
        raise DashscopeCloneError(
            "上传音频样本到百炼临时存储超时（网络连接超时）。"
            "百炼复刻 API 要求公网可直接访问的音频链接；推荐直接在【阿里云百炼声音复刻中心】(https://bailian.console.aliyun.com/cn-beijing/model/experience/voice/sound-cloning) "
            "在线录制或上传音频生成专属 Voice-ID，并在系统中使用「登记百炼 Voice-ID」即可秒级接入并通过 WebSocket 真实试听！"
        )
    except Exception as e:
        logger.warning("百炼临时存储上传异常", exc_info=True)
        raise DashscopeCloneError(f"上传音频到百炼临时存储异常：{e.__class__.__name__}: {e}")
    return f"oss://{key}"


async def create_dashscope_cloned_voice(
    base_url: str,
    api_key: str,
    *,
    audio_url: str,
    target_model: str,
    prefix: str = "cloned",
) -> str:
    """
    调用百炼 voice-enrollment 真正复刻。CosyVoice 只接受公网可访问的 input.url
    （base64 的 audio.data 仅 Qwen-TTS 支持），成功返回云端 voice_id
    （如 cosyvoice-v3.5-flash-cloned-xxxxxx）。
    """
    if not api_key:
        raise DashscopeCloneError("未配置百炼 API Key，无法创建云端复刻音色。")
    if not audio_url:
        raise DashscopeCloneError("缺少可供百炼访问的音频 URL，无法创建复刻音色。")
    model = (target_model or "cosyvoice-v3.5-flash").strip()
    payload = {
        "model": "voice-enrollment",
        "input": {
            "action": "create_voice",
            "target_model": model,
            "prefix": _sanitize_prefix(prefix),
            "url": audio_url,
            "language_hints": ["zh"],
        },
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "X-DashScope-OssResourceResolve": "enable",
    }
    try:
        async with httpx.AsyncClient(timeout=35.0) as client:
            resp = await client.post(_customization_url(base_url), headers=headers, json=payload)
            body = resp.text or ""
            if resp.status_code not in (200, 201):
                raise DashscopeCloneError(
                    f"百炼声音复刻被拒绝（HTTP {resp.status_code}）：{body[:300]}",
                    status=resp.status_code,
                )
            try:
                data = resp.json()
            except Exception:
                raise DashscopeCloneError(f"百炼复刻返回非 JSON：{body[:200]}", status=resp.status_code)
            voice_id = ((data or {}).get("output") or {}).get("voice_id")
            if not voice_id:
                raise DashscopeCloneError(f"百炼未返回 voice_id：{body[:300]}", status=resp.status_code)
            logger.info(f"百炼声音复刻成功，Voice ID: {voice_id}")
            return str(voice_id)
    except DashscopeCloneError:
        raise
    except Exception as e:
        raise DashscopeCloneError(f"调用百炼声音复刻异常：{e}")


async def list_dashscope_cloned_voices(base_url: str, api_key: str) -> list[dict]:
    """拉取百炼当前业务空间内已复刻的所有音色清单（状态为 OK 的音色）"""
    if not api_key:
        return []
    payload = {
        "model": "voice-enrollment",
        "input": {
            "action": "list_voice",
            "page_index": 0,
            "page_size": 50
        }
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(_customization_url(base_url), headers=headers, json=payload)
            if resp.status_code == 200:
                data = resp.json() or {}
                output = data.get("output") or {}
                return output.get("voice_list") or []
            else:
                logger.warning(f"拉取百炼复刻音色列表 HTTP {resp.status_code}: {resp.text[:200]}")
    except Exception as e:
        logger.warning(f"拉取百炼复刻音色列表异常: {e}")
    return []


async def query_dashscope_cloned_voice(base_url: str, api_key: str, voice_id: str) -> dict:
    """查询复刻音色状态（OK / DEPLOYING / UNDEPLOYED），返回 output 字典。"""
    payload = {"model": "voice-enrollment", "input": {"action": "query_voice", "voice_id": voice_id}}
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "X-DashScope-OssResourceResolve": "enable",
    }
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(_customization_url(base_url), headers=headers, json=payload)
            if resp.status_code in (200, 201):
                return resp.json().get("output") or {}
            elif "ResourceNotExist" in (resp.text or ""):
                return {"_error": "ResourceNotExist", "detail": "资源在百炼云端不存在或已被删除"}
            return {"_error": f"HTTP_{resp.status_code}", "detail": (resp.text or "")[:200]}
    except Exception as e:
        return {"_error": "Exception", "detail": str(e)}


async def diagnose_voice_error(base_url: str, api_key: str, voice_id: str, raw_err: str) -> str:
    """对百炼合成错误进行诊断并生成简短易懂的说明"""
    try:
        q_res = await query_dashscope_cloned_voice(base_url, api_key, voice_id)
        if q_res.get("_error") == "ResourceNotExist":
            return f"主播绑定的音色【{voice_id}】在云端不存在或已被删除，请检查绑定"
        elif q_res.get("status") and q_res.get("status") != "OK":
            return f"云端音色状态为【{q_res.get('status')}】（未就绪），请稍候再试"
    except Exception:
        pass
    if "ResourceNotExist" in raw_err or "not exist" in (raw_err or "").lower():
        return f"主播绑定的音色【{voice_id}】在云端不存在，请检查绑定"
    return raw_err or "音色合成调用失败，请检查配置"


async def synthesize_dashscope_cosyvoice(
    base_url: str,
    api_key: str,
    voice_id: str,
    text: str,
    target_model: Optional[str] = None
) -> bytes:
    """
    使用专属复刻 Voice-ID 真实合成新台词音频流。
    优先采用阿里云百炼官方 WebSocket 双向流式协议，
    若遇到网络阻断自动降级到 HTTP SSE 端点，双保险确保合成成功。
    遇到 411 等模型不匹配错误时，自动查询云端注册的 target_model 自适应纠偏重试。
    """
    if not base_url or not api_key:
        raise DashscopeCloneError("未配置百炼 Base URL / API Key，无法合成克隆台词。")
    if not voice_id:
        raise DashscopeCloneError("缺少云端复刻 Voice-ID，无法合成克隆台词。")

    from server.core.audio.cosyvoice_ws import infer_model_from_voice_id, synthesize_via_cosyvoice_ws, CosyVoiceWSError
    model = infer_model_from_voice_id(voice_id, target_model)

    async def _do_ws_synthesize(m: str) -> Optional[bytes]:
        try:
            logger.info(f"调用百炼官方 WebSocket 合成: voice={voice_id}, model={m}")
            ws_bytes = await synthesize_via_cosyvoice_ws(
                base_url=base_url,
                api_key=api_key,
                voice_id=voice_id,
                text=text,
                target_model=m,
            )
            if ws_bytes and len(ws_bytes) > 512:
                return ws_bytes
        except Exception as ws_err:
            logger.warning(f"百炼 WebSocket (model={m}) 提示: {ws_err}")
            raise
        return None

    # 1. 尝试首选模型的 WebSocket 全双工合成
    last_err_str = ""
    try:
        ws_res = await _do_ws_synthesize(model)
        if ws_res:
            return ws_res
    except Exception as e:
        last_err_str = str(e)
        logger.warning(f"百炼 WebSocket 首选通道异常: {e}")

    # 2. 如果首选通道报错包含 411/418/InvalidParameter，自动查询云端已登记的实际 target_model 纠偏
    if "411" in last_err_str or "418" in last_err_str or "InvalidParameter" in last_err_str:
        q_info = await query_dashscope_cloned_voice(base_url, api_key, voice_id)
        cloud_target_model = q_info.get("target_model")
        if cloud_target_model and cloud_target_model != model:
            logger.info(f"检测到云端音色注册模型为 {cloud_target_model}，自动切换模型重试合成...")
            try:
                ws_retry = await _do_ws_synthesize(cloud_target_model)
                if ws_retry:
                    return ws_retry
            except Exception as e_retry:
                last_err_str = str(e_retry)
                logger.warning(f"纠偏模型 {cloud_target_model} WebSocket 合成仍异常: {e_retry}")

    # 3. 兜底尝试百炼官方 HTTP SSE 端点

    # 2. 兜底尝试百炼官方 HTTP SSE 端点
    clean_base = base_url.rstrip("/")
    if "/services/audio" in clean_base:
        endpoint = clean_base
    else:
        endpoint = f"{clean_base}/services/audio/tts/SpeechSynthesizer"

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "X-DashScope-SSE": "enable"
    }

    payload = {
        "model": model,
        "input": {
            "text": text,
            "voice": voice_id,
            "format": "mp3"
        }
    }

    logger.info(f"调用百炼官方 HTTP SSE 合成: endpoint={endpoint}, voice={voice_id}, model={payload['model']}")
    audio_chunks = []
    full_audio_url = None

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            async with client.stream("POST", endpoint, headers=headers, json=payload) as resp:
                if resp.status_code != 200:
                    raw_body = await resp.aread()
                    err_msg = raw_body.decode("utf-8", errors="ignore")
                    raise DashscopeCloneError(
                        f"百炼语音合成被拒绝（HTTP {resp.status_code}）：{err_msg[:300]}",
                        status=resp.status_code,
                    )

                async for line in resp.aiter_lines():
                    line = line.strip()
                    if not line or not line.startswith("data:"):
                        continue
                    try:
                        data_json = json.loads(line[5:])
                        if data_json.get("code"):
                            raise DashscopeCloneError(
                                f"百炼语音合成返回错误：{data_json}",
                                status=resp.status_code,
                            )
                        output = data_json.get("output", {})
                        audio_obj = output.get("audio", {})
                        chunk_b64 = audio_obj.get("data")
                        if chunk_b64:
                            audio_chunks.append(base64.b64decode(chunk_b64))
                        if audio_obj.get("url"):
                            full_audio_url = audio_obj.get("url")
                    except DashscopeCloneError:
                        raise
                    except Exception:
                        pass

        if audio_chunks:
            logger.info(f"百炼 SSE 流式合成成功，共接收 {len(audio_chunks)} 帧音频块")
            return b"".join(audio_chunks)

        if full_audio_url:
            async with httpx.AsyncClient(timeout=15.0) as client:
                dl_resp = await client.get(full_audio_url)
                if dl_resp.status_code == 200 and dl_resp.content:
                    logger.info("百炼 URL 完整音频下载成功")
                    return dl_resp.content
                raise DashscopeCloneError(
                    f"下载百炼合成音频失败（HTTP {dl_resp.status_code}）",
                    status=dl_resp.status_code,
                )
    except DashscopeCloneError:
        raise
    except Exception as e:
        raise DashscopeCloneError(f"百炼 CosyVoice 语音合成请求异常：{e}")

    raise DashscopeCloneError("百炼未返回任何音频数据（SSE 流为空且无音频 URL）。")


async def generate_cloned_voice_preview(
    voice_id: str,
    voice_name: str,
    sample_audio_path: Optional[str] = None,
    custom_text: Optional[str] = None,
    base_url: Optional[str] = None,
    api_key: Optional[str] = None,
    target_model: Optional[str] = None,
    force_regenerate: bool = False,
    provider: Optional[str] = None,
) -> Path:
    """
    为专属克隆音色合成一段全新台词的试听音频。
    【铁律】：
      1. 严禁播放用户上传的原版音频样本，必须用全新合成的台词验证克隆效果；
      2. 引擎隔离：MOSS-TTS-Nano 走端侧零样本推理，百炼 CosyVoice 走云端复刻，绝不跨引擎报错；
      3. 合成不了就诚实报错（说明缺什么、下一步做什么），绝不静默降级。
    """
    clean_name = (voice_name or voice_id).strip()
    text_to_speak = (custom_text or "").strip()
    default_placeholders = [
        "你好！这是当前语音合成引擎的实时试听效果，音色自然流畅，祝您直播顺利！",
        "你好，欢迎来到直播间！这是当前语音引擎的实时试听效果，祝您开播顺利！",
        "你好！这是当前语音合成引擎的实时试听效果"
    ]
    is_custom = bool(text_to_speak and not any(p in text_to_speak for p in default_placeholders))
    if not is_custom:
        text_to_speak = f"你好！我是您的专属克隆声线【{clean_name}】。这是一句全新合成的实时台词，用于检验声音克隆效果，祝您直播大吉！"

    if is_custom:
        import hashlib
        text_hash = hashlib.md5(text_to_speak.encode("utf-8")).hexdigest()[:10]
        out_file = VOICES_DIR / f"{voice_id}_custom_{text_hash}.mp3"
    else:
        out_file = VOICES_DIR / f"{voice_id}_cloned_preview.mp3"

    if not force_regenerate and out_file.exists() and out_file.stat().st_size > 1024:
        return out_file

    prov_lower = (provider or "").lower().strip()
    is_moss = bool("moss" in prov_lower or "nano" in prov_lower)

    # -------------------------------------------------------------------------
    # 分支 1: MOSS-TTS-Nano 端侧零样本克隆通道 (完全独立于百炼，绝不报百炼错误)
    # -------------------------------------------------------------------------
    if is_moss:
        # 1. 优先检查本地是否已经生成过当前全新台词的试听文件
        if not force_regenerate and out_file.exists() and out_file.stat().st_size > 1024:
            return out_file

        # 2. 必须具备参考音频样本
        if not sample_audio_path or not Path(sample_audio_path).exists() or Path(sample_audio_path).stat().st_size <= 0:
            raise RuntimeError(
                f"音色【{clean_name}】缺少有效的主播录音样本（文件不存在或为空），无法提取声纹进行全新台词合成。\n"
                f"👉 请在下方克隆工作台重新上传 5~30 秒录音文件后重试。"
            )

        # 3. 优先调用主进程原生官方 MOSS-TTS-Nano 端侧零样本神经克隆引擎！
        # 【铁律】：必须使用用户上传的声学指纹配合全新台词动态合成，严禁直接播放原录音！
        try:
            from server.core.audio.moss_nano.moss_cloner import moss_cloner
            logger.info(f"正在调用官方 MOSS-TTS-Nano 零样本神经引擎为【{clean_name}】合成新台词...")
            gen_path = await moss_cloner.clone_and_synthesize(
                text=text_to_speak,
                prompt_audio_path=sample_audio_path,
                output_path=out_file,
                speed=1.0,
                volume=1.0
            )
            if gen_path.exists() and gen_path.stat().st_size > 512:
                logger.info(f"官方 MOSS 端侧引擎克隆新台词合成成功: {gen_path}")
                return gen_path
        except Exception as e:
            logger.warning(f"主进程原生 MOSS-TTS-Nano 试听合成异常: {e}，尝试备用 9880 外部端点...")
            # 备用 9880 端点
            moss_endpoint = (base_url if base_url and ("9880" in base_url or "moss" in base_url) else "http://127.0.0.1:9880").strip().rstrip("/")
            try:
                async with httpx.AsyncClient(timeout=10.0) as client:
                    clone_payload = {
                        "text": text_to_speak,
                        "prompt_wav": sample_audio_path,
                        "speed": 1.0,
                        "volume": 1.0,
                        "sample_rate": 48000,
                        "stream": False
                    }
                    synth_resp = await client.post(f"{moss_endpoint}/tts", json=clone_payload)
                    if synth_resp.status_code == 200 and len(synth_resp.content) > 512:
                        with open(out_file, "wb") as pf:
                            pf.write(synth_resp.content)
                        logger.info(f"已通过 MOSS-TTS-Nano 9880 服务成功合成克隆试听: {out_file}")
                        return out_file
            except Exception as ext_e:
                logger.error(f"MOSS-TTS 外部端点合成亦失败: {ext_e}")
                raise RuntimeError(
                    f"MOSS-TTS-Nano 为专属音色【{clean_name}】合成全新试听台词失败：{str(e)}。\n"
                    f"👉 请检查参考录音是否清晰（建议 5~30 秒单人无杂音音频）。"
                )

        if out_file.exists() and out_file.stat().st_size > 512:
            return out_file

        raise RuntimeError(
            f"音色【{clean_name}】的 MOSS 专属试听文件未生成成功，请重试。"
        )

    # -------------------------------------------------------------------------
    # 分支 2: 阿里云百炼 CosyVoice 云端复刻通道
    # -------------------------------------------------------------------------
    if not base_url or not api_key:
        db_base, db_key = await get_active_cosyvoice_config()
        if not base_url:
            base_url = db_base
        if not api_key:
            api_key = db_key
    if not base_url or not api_key:
        raise RuntimeError(
            "尚未配置阿里云百炼 TTS（Base URL / API Key），无法为克隆音色合成新台词。"
            "请先在 TTS 配置中填入百炼参数并保存，再重新试听。"
        )

    # 本地档案（clone_ 开头）意味着从未拿到云端 Voice-ID，用它调云端合成必定失败。
    # 直接给出可操作的诚实说明，而不是用预置音色冒充原主播声线。
    if (voice_id or "").startswith("clone_"):
        raise RuntimeError(
            f"音色【{clean_name}】当前只是保存在本地的克隆档案，尚未在阿里云百炼完成声音复刻"
            "（没有云端 Voice-ID），因此无法用原主播声线合成新台词。"
            "请确认已配置百炼 API Key 后重新执行「一键克隆」，"
            "或在百炼控制台完成复刻后使用右侧「登记已有 Voice-ID」。"
        )

    # 唯一真实通道：用该音色的云端复刻声线合成全新台词
    try:
        dash_bytes = await synthesize_dashscope_cosyvoice(
            base_url=base_url,
            api_key=api_key,
            voice_id=voice_id,
            text=text_to_speak,
            target_model=target_model
        )
    except DashscopeCloneError as e:
        diag = await diagnose_voice_error(base_url, api_key, voice_id, e.detail)
        raise RuntimeError(diag)
    except Exception as e:
        diag = await diagnose_voice_error(base_url, api_key, voice_id, str(e))
        raise RuntimeError(diag)
    if not dash_bytes or len(dash_bytes) <= 512:
        raise RuntimeError(f"克隆音色【{clean_name}】合成新台词失败：百炼返回的音频为空。")
    with open(out_file, "wb") as pf:
        pf.write(dash_bytes)
    logger.info(f"克隆音色专属声线成功合成并落盘: {out_file} (字节数: {len(dash_bytes)})")
    return out_file
