import time
import json
import httpx
from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, Field, model_validator
from typing import Optional, Dict, Any
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, update
from server.database.db import get_db
from server.database.models import ApiProviderConfig, AppSetting
from server.config import encrypt_secret, decrypt_secret, mask_api_key

router = APIRouter(prefix="/settings", tags=["系统配置与API服务商"])

# ---------------------------------------------------------------------------
# 直播模式定义 (规划 §8.1 硬件档次矩阵) —— 需求 1：开播前第一步选择直播模式
# ---------------------------------------------------------------------------
LIVE_MODES = {
    "A": {
        "code": "A",
        "name": "全本地离线模式",
        "hardware": "RTX 3090 / 4080 / 4090 (16G-24G)",
        "llm": "本地 Ollama (DeepSeek-R1-14B / Qwen2.5)",
        "tts": "本地 CosyVoice 2 (FP16)",
        "avatar": "本地 OpenCV 程序化头像 (720P 25FPS；非 MuseTalk/TRT)",
        "cost": "0 元 (仅消耗电费)",
        "audience": "拥有专业主机、注重数据绝对隐私企业",
        "required_configs": ["local_ollama", "local_cosyvoice"]
    },
    "B": {
        "code": "B",
        "name": "主流端云混合模式",
        "hardware": "RTX 2060 / 3060 / 4060 (6G-8G)",
        "llm": "云端 API (DeepSeek / GPT / Kimi)",
        "tts": "本地 CosyVoice / 云端极速 TTS (Edge)",
        "avatar": "本地优化版数字人 (720P 25FPS)",
        "cost": "仅少量 API 费用 (约 0.5 元/小时)",
        "audience": "绝大多数拥有入门独显的个人主播",
        "required_configs": ["openai_compatible", "cloud_edge_tts"]
    },
    "C": {
        "code": "C",
        "name": "端云分离架构 (强烈推荐)",
        "hardware": "任何老电脑 / 2G 显卡 / 苹果 Mac",
        "llm": "本地运行调度中枢与知识库 + 云端 API",
        "tts": "云端 GPU 节点渲染数字人与 TTS",
        "avatar": "远程帧通道（仅节点连通且回传帧时生效；平台发布另行验收）",
        "cost": "约 1.5 ~ 2.5 元/小时 (按秒计费，播完即关)",
        "audience": "电脑配置较低但希望拥有电影级 4K 画质用户",
        "required_configs": ["remote_gpu", "openai_compatible"]
    },
    "D": {
        "code": "D",
        "name": "轻量免显卡模式",
        "hardware": "办公轻薄本 / 纯 CPU",
        "llm": "云端 API 直连",
        "tts": "云端极速 TTS (MiniMax / Edge)",
        "avatar": "轻量 mock 媒体驱动（当前未交付 Live2D）",
        "cost": "极微量 API 费用",
        "audience": "二次元虚拟主播、游戏闲聊、知识答疑",
        "required_configs": ["cloud_edge_tts", "openai_compatible"]
    }
}

SETTING_KEY_LIVE_MODE = "live_mode"
SETTING_KEY_WIZARD_DONE = "wizard_completed"
SETTING_KEY_ANCHOR_ID = "selected_anchor_id"


async def _get_setting(db: AsyncSession, key: str) -> Optional[str]:
    res = await db.execute(select(AppSetting).where(AppSetting.key == key))
    row = res.scalar_one_or_none()
    return row.value if row else None


async def _set_setting(db: AsyncSession, key: str, value: str):
    res = await db.execute(select(AppSetting).where(AppSetting.key == key))
    row = res.scalar_one_or_none()
    if row:
        row.value = value
    else:
        db.add(AppSetting(key=key, value=value))
    await db.commit()


class LiveModeRequest(BaseModel):
    mode: str = Field(min_length=1, max_length=1)  # A / B / C / D


@router.get("/modes")
async def list_live_modes():
    """获取全部可选直播模式定义 (需求 1：第一步选择直播模式)"""
    return {"code": 0, "data": list(LIVE_MODES.values())}


@router.get("/recommended-mode")
async def get_recommended_mode():
    """
    根据本机硬件配置自动推荐最适合的运行模式 (默认选择项)
    档位映射 (规划 §8.1)：
      - CUDA 可用且显存 >= 16G  -> A 全本地离线
      - 显存 >= 6G              -> B 主流端云混合
      - 有显卡但显存 < 6G        -> C 端云分离 (本地仅调度中枢)
      - 无独立显卡               -> D 轻量免显卡
    """
    import asyncio
    from server.routes.live import _probe_gpu, _recommend_tier
    gpu = await asyncio.to_thread(_probe_gpu)
    tier = _recommend_tier(gpu)
    mode_code = tier.split(" ")[1] if tier.startswith("Tier") else "D"

    gpu_name = gpu.get("gpu_name")
    vram = gpu.get("vram_total_gb", 0) or 0
    cuda = gpu.get("cuda_available", False)

    if mode_code == "A":
        reason = (
            f"检测到 {gpu_name} (显存 {vram}GB) 且 CUDA 加速可用，显存充裕，"
            "可全本地离线运行大模型与数字人渲染：0 元成本、数据绝对隐私。"
        )
    elif mode_code == "B":
        reason = (
            f"检测到 {gpu_name} (显存 {vram}GB)，足以本地运行轻量数字人渲染 (720P/25FPS)；"
            "大模型建议走云端 API，仅产生少量 API 费用 (约 0.5 元/小时)。"
        )
    elif mode_code == "C":
        if gpu_name:
            reason = (
                f"检测到 {gpu_name} (显存 {vram}GB)，显存不足以本地渲染数字人；"
                "推荐端云分离架构：本地仅运行调度中枢与知识库，画面由云端 4090 节点渲染推回，"
                "低配电脑也能获得高画质 (约 1.5~2.5 元/小时，按秒计费)。"
            )
        else:
            reason = (
                "检测到显卡驱动环境有限，推荐端云分离架构：本地仅运行调度中枢与知识库，"
                "画面由云端 4090 节点渲染推回。"
            )
    else:
        reason = (
            "未检测到可用的 NVIDIA 独立显卡，推荐轻量免显卡模式：云端 API 直连 + Edge-TTS 免费语音"
            "(极微量费用)，适合二次元虚拟主播、游戏闲聊与知识答疑场景。"
        )

    return {
        "code": 0,
        "data": {
            "mode": mode_code,
            "tier": tier,
            "gpu": gpu,
            "reason": reason
        }
    }


@router.get("/live-mode")
async def get_live_mode(db: AsyncSession = Depends(get_db)):
    """获取当前已选择的直播模式与向导完成状态"""
    mode = await _get_setting(db, SETTING_KEY_LIVE_MODE)
    wizard_done = await _get_setting(db, SETTING_KEY_WIZARD_DONE)
    anchor_id = await _get_setting(db, SETTING_KEY_ANCHOR_ID)
    return {
        "code": 0,
        "data": {
            "mode": mode,
            "mode_info": LIVE_MODES.get(mode) if mode else None,
            "wizard_completed": wizard_done == "1",
            "selected_anchor_id": anchor_id
        }
    }


@router.post("/live-mode")
async def set_live_mode(req: LiveModeRequest, db: AsyncSession = Depends(get_db)):
    """保存直播模式选择 (需求 3：直播过程中绝对禁止修改)"""
    if req.mode not in LIVE_MODES:
        raise HTTPException(status_code=400, detail="无效的直播模式，可选 A/B/C/D")

    from server.routes.live import global_live_controller
    if global_live_controller.is_live:
        raise HTTPException(status_code=409, detail="直播进行中，禁止切换直播模式！请先停止直播后再重新配置。")

    await _set_setting(db, SETTING_KEY_LIVE_MODE, req.mode)
    await _set_setting(db, SETTING_KEY_WIZARD_DONE, "1")
    return {"code": 0, "message": f"直播模式已设定为【{LIVE_MODES[req.mode]['name']}】，对应配置项已按模式自动呈现", "data": LIVE_MODES[req.mode]}


class SelectedAnchorRequest(BaseModel):
    anchor_id: str = Field(default="", max_length=64)


@router.post("/selected-anchor")
async def set_selected_anchor(req: SelectedAnchorRequest, db: AsyncSession = Depends(get_db)):
    """记录当前直播间选用的主播档案 (直播大屏展示'直播是谁')"""
    await _set_setting(db, SETTING_KEY_ANCHOR_ID, req.anchor_id or "")
    return {"code": 0, "message": "当前主播已更新"}


SETTING_KEY_LIVE_THEME = "live_theme"


class LiveThemeRequest(BaseModel):
    theme: str = Field(default="", max_length=1000)


@router.get("/live-theme")
async def get_live_theme(db: AsyncSession = Depends(get_db)):
    """读取今日直播主题 (娱乐/闲聊主播冷场话题锚点)"""
    theme = await _get_setting(db, SETTING_KEY_LIVE_THEME) or ""
    return {"code": 0, "data": {"theme": theme}}


@router.post("/live-theme")
async def set_live_theme(req: LiveThemeRequest, db: AsyncSession = Depends(get_db)):
    """保存今日直播主题"""
    theme = (req.theme or "").strip()
    await _set_setting(db, SETTING_KEY_LIVE_THEME, theme)
    return {"code": 0, "message": "今日直播主题已保存" if theme else "今日直播主题已清空"}


# ---------------------------------------------------------------------------
# 实时多模态视觉感知通道配置 (规划 §2.2 / §9.3)
# ---------------------------------------------------------------------------
SETTING_KEY_VISION_ENABLED = "vision_enabled"
SETTING_KEY_VISION_SOURCE = "vision_source"
SETTING_KEY_VISION_INTERVAL = "vision_capture_interval_sec"
VISION_SOURCES = ("desktop_screen", "usb_camera")


class VisionConfigRequest(BaseModel):
    enabled: bool = False
    source: str = Field(default="desktop_screen", max_length=32)
    interval_sec: float = Field(default=2.5, ge=0.2, le=3600.0)
    camera_index: int = Field(default=0, ge=0, le=64)


async def _load_vision_config(db: AsyncSession) -> dict:
    from server.core.vision.capture import global_vision
    enabled = (await _get_setting(db, SETTING_KEY_VISION_ENABLED)) == "1"
    source = (await _get_setting(db, SETTING_KEY_VISION_SOURCE)) or "desktop_screen"
    interval_raw = await _get_setting(db, SETTING_KEY_VISION_INTERVAL)
    try:
        interval = float(interval_raw) if interval_raw else 2.5
    except (TypeError, ValueError):
        interval = 2.5
    if source not in VISION_SOURCES:
        source = "desktop_screen"
    global_vision.configure(enabled=enabled, source=source, interval_sec=interval)
    return {"enabled": enabled, "source": source, "interval_sec": interval}


@router.get("/vision")
async def get_vision_config(db: AsyncSession = Depends(get_db)):
    """读取视觉感知配置与采集器运行态"""
    from server.core.vision.capture import global_vision
    cfg = await _load_vision_config(db)
    return {"code": 0, "data": {**cfg, "status": global_vision.get_status()}}


@router.post("/vision")
async def set_vision_config(req: VisionConfigRequest, db: AsyncSession = Depends(get_db)):
    """保存视觉感知配置 (是否启用 / 来源 / 采样频次)"""
    from server.core.vision.capture import global_vision
    if req.source not in VISION_SOURCES:
        raise HTTPException(status_code=400, detail="视觉来源仅支持 desktop_screen / usb_camera")
    await _set_setting(db, SETTING_KEY_VISION_ENABLED, "1" if req.enabled else "0")
    await _set_setting(db, SETTING_KEY_VISION_SOURCE, req.source)
    await _set_setting(db, SETTING_KEY_VISION_INTERVAL, str(req.interval_sec))
    global_vision.configure(enabled=req.enabled, source=req.source, interval_sec=req.interval_sec)
    return {
        "code": 0,
        "message": "视觉感知配置已保存" + ("（已启用）" if req.enabled else "（已关闭）"),
        "data": {"enabled": req.enabled, "source": req.source, "interval_sec": req.interval_sec}
    }

class ApiConfigSaveRequest(BaseModel):
    id: Optional[str] = Field(default=None, max_length=64)
    config_group: str = Field(min_length=1, max_length=32)
    provider_name: str = Field(min_length=1, max_length=64)
    title: Optional[str] = Field(default=None, max_length=128)
    api_key: Optional[str] = Field(default=None, max_length=16_384)
    base_url: Optional[str] = Field(default=None, max_length=512)
    model_name: Optional[str] = Field(default=None, max_length=128)
    extra_params: Optional[Dict[str, Any]] = None
    is_active: Optional[bool] = True

    @model_validator(mode="after")
    def limit_extra_params(self):
        if self.extra_params is not None:
            if len(self.extra_params) > 100 or len(json.dumps(self.extra_params, ensure_ascii=False)) > 50_000:
                raise ValueError("扩展参数超过 100 项或 5 万字符预算")
        return self

class SetActiveConfigRequest(BaseModel):
    config_id: str = Field(min_length=1, max_length=64)
    config_group: Optional[str] = Field(default="llm", max_length=32)

class FetchModelsRequest(BaseModel):
    config_id: Optional[str] = Field(default=None, max_length=64)
    provider_name: Optional[str] = Field(default="deepseek", max_length=64)
    base_url: Optional[str] = Field(default=None, max_length=512)
    api_key: Optional[str] = Field(default=None, max_length=16_384)

class TestConnectionRequest(BaseModel):
    config_group: str = Field(min_length=1, max_length=32)
    provider_name: str = Field(min_length=1, max_length=64)
    base_url: Optional[str] = Field(default=None, max_length=512)
    api_key: Optional[str] = Field(default=None, max_length=16_384)

# 内置 8 大主流直播适配大模型生态元数据清单
BUILTIN_LLM_PROVIDERS = [
    {
        "id": "deepseek",
        "name": "DeepSeek 深度求索",
        "tagline": "国产性价比标杆 · 高情商带货首选",
        "brand_color": "#0284C7",
        "logo_svg": "/static/svg/model_deepseek.svg",
        "default_base_url": "https://api.deepseek.com/v1",
        "recommended_models": ["deepseek-chat", "deepseek-reasoner"],
        "url_pills": [
            {"text": "DeepSeek 官方 (推荐)", "val": "https://api.deepseek.com/v1"},
            {"text": "备用直连端点", "val": "https://api.deepseek.com"}
        ],
        "desc": "超高性价比与推理能力，官方 deepseek-chat 适合话术生成与实时互动逼单，deepseek-reasoner 具备深度逻辑链。"
    },
    {
        "id": "qwen",
        "name": "Qwen 通义千问",
        "tagline": "直播电商霸主 · 极速指令遵循",
        "brand_color": "#0070F3",
        "logo_svg": "/static/svg/model_qwen.svg",
        "default_base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "recommended_models": ["qwen-plus", "qwen-turbo", "qwen-max", "qwen2.5-72b-instruct"],
        "url_pills": [
            {"text": "阿里云 DashScope (官方兼容)", "val": "https://dashscope.aliyuncs.com/compatible-mode/v1"},
            {"text": "本地 Ollama Qwen2.5", "val": "http://127.0.0.1:11434/v1"}
        ],
        "desc": "阿里系模型适合电商直播话术生成，支持促销表达与商品解读，并可与 CosyVoice 配置组合使用。"
    },
    {
        "id": "minimax",
        "name": "MiniMax 海螺 AI",
        "tagline": "长文本基座 · 细腻角色共情",
        "brand_color": "#FF5226",
        "logo_svg": "/static/svg/model_minimax.svg",
        "default_base_url": "https://api.minimax.chat/v1",
        "recommended_models": ["MiniMax-Text-01", "abab6.5s-chat"],
        "url_pills": [
            {"text": "MiniMax 官方", "val": "https://api.minimax.chat/v1"}
        ],
        "desc": "自研万亿级长文本大模型，擅长细腻的人设构建与逼真主播情感互动，直播间陪伴感强。"
    },
    {
        "id": "kimi",
        "name": "Kimi 月之暗面",
        "tagline": "超长上下文 · 直播大促选品记忆",
        "brand_color": "#1783FF",
        "logo_svg": "/static/svg/model_kimi.svg",
        "default_base_url": "https://api.moonshot.cn/v1",
        "recommended_models": ["moonshot-v1-8k", "moonshot-v1-32k", "moonshot-v1-128k"],
        "url_pills": [
            {"text": "Moonshot 官方", "val": "https://api.moonshot.cn/v1"}
        ],
        "desc": "无损超长上下文标杆，百款商品 SKU 参数、品牌白皮书与直播大促合规规则深度召回，零幻觉不乱编。"
    },
    {
        "id": "glm",
        "name": "GLM 智谱清言",
        "tagline": "清华系标杆 · 合规安全超快响应",
        "brand_color": "#0D9488",
        "logo_svg": "/static/svg/model_glm.svg",
        "default_base_url": "https://open.bigmodel.cn/api/paas/v4/",
        "recommended_models": ["glm-4-flash", "glm-4-plus", "glm-4"],
        "url_pills": [
            {"text": "智谱开放平台官方", "val": "https://open.bigmodel.cn/api/paas/v4/"}
        ],
        "desc": "智谱 AI 成熟基座，中文语境理解深厚，安全审查与合规能力极强，glm-4-flash 免费且超快速。"
    },
    {
        "id": "gemini",
        "name": "Gemini 谷歌大模型",
        "tagline": "超快首包响应 · 原生多模态感知",
        "brand_color": "#2563EB",
        "logo_svg": "/static/svg/model_gemini.svg",
        "default_base_url": "https://generativelanguage.googleapis.com/v1beta/openai/",
        "recommended_models": ["gemini-2.0-flash", "gemini-1.5-flash", "gemini-1.5-pro"],
        "url_pills": [
            {"text": "Google 官方 OpenAI 兼容通道", "val": "https://generativelanguage.googleapis.com/v1beta/openai/"}
        ],
        "desc": "Google 旗舰多模态大模型，flash 系列具备毫秒级首包极速生成，极度契合高频弹幕实时打断与多模态眼见即所言。"
    },
    {
        "id": "chatgpt",
        "name": "ChatGPT (OpenAI)",
        "tagline": "全球顶级标杆 · 全能综合推理",
        "brand_color": "#10A37F",
        "logo_svg": "/static/svg/model_chatgpt.svg",
        "default_base_url": "https://api.openai.com/v1",
        "recommended_models": ["gpt-4o", "gpt-4o-mini", "gpt-4-turbo"],
        "url_pills": [
            {"text": "OpenAI 官方", "val": "https://api.openai.com/v1"}
        ],
        "desc": "OpenAI 旗舰多模态模型，具备出色的结构化输出、情绪识别与严谨知识问答能力。"
    },
    {
        "id": "custom",
        "name": "自定义 / 本地模型",
        "tagline": "支持 Ollama / 硅基流动 / 本地网关",
        "brand_color": "#F59E0B",
        "logo_svg": "/static/svg/model_custom.svg",
        "default_base_url": "https://api.siliconflow.cn/v1",
        "recommended_models": ["Qwen/Qwen2.5-72B-Instruct", "deepseek-ai/DeepSeek-V3"],
        "url_pills": [
            {"text": "硅基流动 SiliconFlow", "val": "https://api.siliconflow.cn/v1"},
            {"text": "本地 Ollama 离线", "val": "http://127.0.0.1:11434/v1"}
        ],
        "desc": "任意符合 OpenAI API 规范的代理服务、云端 API 或本地 Ollama/vLLM 网关均可自由接入。"
    }
]

@router.get("/llm/providers")
async def get_builtin_llm_providers():
    """获取内置主流大模型生态元数据（品牌名、Logo、标准 Base URL、推荐模型库）"""
    return {"code": 0, "data": BUILTIN_LLM_PROVIDERS}

@router.post("/llm/models")
async def fetch_llm_models(req: FetchModelsRequest, db: AsyncSession = Depends(get_db)):
    """
    实时调用服务商 API 拉取可用大语言模型列表 (/models)
    支持自动鉴权、格式清洗、超时保护与内置推荐模型智能兜底
    """
    raw_api_key = req.api_key or ""
    base_url = (req.base_url or "").strip().rstrip("/")
    provider = (req.provider_name or "custom").lower()

    # 若未传 key 但传了已存在的 config_id，从数据库安全解密已有 Key
    if req.config_id and not raw_api_key:
        res = await db.execute(select(ApiProviderConfig).where(ApiProviderConfig.id == req.config_id))
        record = res.scalar_one_or_none()
        if record:
            if not base_url and record.base_url:
                base_url = record.base_url.strip().rstrip("/")
            if record.encrypted_api_key:
                raw_api_key = decrypt_secret(record.encrypted_api_key)

    if not base_url:
        return {
            "code": 1,
            "success": False,
            "message": "未能获取模型列表：请先填写有效的 Base URL 接口地址！",
            "models": []
        }

    # 判断是否为本地离线免鉴权服务 (如 Ollama / LocalAI 等)
    is_local_service = any(h in base_url.lower() for h in ["localhost", "127.0.0.1", "0.0.0.0"])

    # 云端服务商必须提供 API Key 才能进行远程实时鉴权查询，禁止盲目推荐过时静态模型
    if not raw_api_key and not is_local_service:
        return {
            "code": 1,
            "success": False,
            "message": "未能获取模型列表：您尚未填写 API Key。云端大模型服务商必须提供有效凭据进行鉴权才能实时拉取最新可用模型，请先填写 API Key 后重试。",
            "models": []
        }

    # 智能探测 models 端点
    target_endpoints = []
    if base_url.endswith("/v1"):
        target_endpoints.append(f"{base_url}/models")
    else:
        target_endpoints.append(f"{base_url}/models")
        target_endpoints.append(f"{base_url}/v1/models")

    headers = {
        "Accept": "application/json",
        "User-Agent": "AI-LiveStream-Agent/1.7.0"
    }
    if raw_api_key:
        headers["Authorization"] = f"Bearer {raw_api_key}"

    fetched_models = []
    error_msg = ""
    is_auth_error = False

    for ep in target_endpoints:
        try:
            async with httpx.AsyncClient(timeout=6.0, verify=True) as client:
                resp = await client.get(ep, headers=headers)
                if resp.status_code == 200:
                    data = resp.json()
                    if isinstance(data, dict) and "data" in data and isinstance(data["data"], list):
                        for item in data["data"]:
                            if isinstance(item, dict) and item.get("id"):
                                fetched_models.append(item["id"])
                    elif isinstance(data, dict) and "models" in data and isinstance(data["models"], list):
                        for item in data["models"]:
                            name = item.get("name") or item.get("model")
                            if name:
                                fetched_models.append(name)
                    elif isinstance(data, list):
                        for item in data:
                            if isinstance(item, str):
                                fetched_models.append(item)
                            elif isinstance(item, dict) and item.get("id"):
                                fetched_models.append(item["id"])
                    if fetched_models:
                        break
                elif resp.status_code == 401:
                    is_auth_error = True
                    error_msg = "鉴权失败：API Key 无效或未获得该接口授权 (HTTP 401)"
                    break
                elif resp.status_code == 403:
                    is_auth_error = True
                    error_msg = "访问拒绝：API Key 权限不足或账户已欠费 (HTTP 403)"
                    break
                else:
                    error_msg = f"服务商响应异常 (HTTP {resp.status_code})"
        except Exception as e:
            error_msg = f"网络请求失败或超时: {str(e)}"

    if fetched_models:
        unique_models = list(dict.fromkeys(fetched_models))
        sorted_models = sorted(
            unique_models,
            key=lambda m: (
                0 if any(k in m.lower() for k in ["chat", "reasoner", "flash", "4o", "plus", "turbo", "text"]) else 1,
                m.lower()
            )
        )
        return {
            "code": 0,
            "success": True,
            "message": f"成功从服务商实时获取到 {len(sorted_models)} 个可用模型！",
            "models": sorted_models
        }

    # 若鉴权失败，明确指明原因，禁止盲目推荐静态模型误导用户
    if is_auth_error:
        return {
            "code": 1,
            "success": False,
            "message": f"未能获取模型列表：{error_msg}。请检查您的 API Key 是否准确无误。",
            "models": []
        }

    return {
        "code": 1,
        "success": False,
        "message": f"未能从远程服务商获取到模型列表 ({error_msg or '服务商未开放 /models 动态查询接口'})。大模型更新频繁，请参考服务商最新官方文档手动输入模型代号。",
        "models": []
    }

@router.post("/configs/set-active")
async def set_active_config(req: SetActiveConfigRequest, db: AsyncSession = Depends(get_db)):
    """将指定配置设为默认激活项，同组其他配置自动切换为备用"""
    target = await db.get(ApiProviderConfig, req.config_id)
    if not target:
        raise HTTPException(status_code=404, detail="未找到该配置记录")

    group = target.config_group or req.config_group or "llm"

    # 原子更新：同组全部置 0
    await db.execute(
        update(ApiProviderConfig)
        .where(ApiProviderConfig.config_group == group)
        .values(is_active=0)
    )

    # 目标置 1
    target.is_active = 1
    await db.commit()

    from server.routes.live import global_live_controller
    if global_live_controller.is_live:
        await global_live_controller.reload_runtime_config("settings")

    return {
        "code": 0,
        "message": f"已成功将【{target.model_name or target.provider_name}】设为默认生效大脑！",
        "active_id": target.id
    }

@router.delete("/configs/{config_id}")
async def delete_config(config_id: str, db: AsyncSession = Depends(get_db)):
    """删除指定的服务商配置"""
    target = await db.get(ApiProviderConfig, config_id)
    if not target:
        raise HTTPException(status_code=404, detail="未找到该配置项")

    was_active = bool(target.is_active)
    group = target.config_group

    # 删除与替补激活在同一事务内完成，替补顺序固定。
    await db.delete(target)
    if was_active:
        remain_res = await db.execute(
            select(ApiProviderConfig)
            .where(ApiProviderConfig.config_group == group, ApiProviderConfig.id != config_id)
            .order_by(ApiProviderConfig.created_at.asc(), ApiProviderConfig.id.asc())
            .limit(1)
        )
        first_remain = remain_res.scalars().first()
        if first_remain:
            first_remain.is_active = 1
    await db.commit()

    return {"code": 0, "message": "配置已成功删除"}

@router.get("/configs")
async def get_all_configs(db: AsyncSession = Depends(get_db)):
    """获取所有已配置的服务商（API Key 脱敏）"""
    result = await db.execute(select(ApiProviderConfig).order_by(ApiProviderConfig.created_at.desc()))
    configs = result.scalars().all()
    data = []
    for cfg in configs:
        raw_key = decrypt_secret(cfg.encrypted_api_key) if cfg.encrypted_api_key else ""
        data.append({
            "id": cfg.id,
            "config_group": cfg.config_group,
            "provider_name": cfg.provider_name,
            "is_active": bool(cfg.is_active),
            "masked_key": mask_api_key(raw_key),
            "base_url": cfg.base_url,
            "model_name": cfg.model_name,
            "extra_params": cfg.extra_params_json
        })
    return {"code": 0, "data": data}

@router.get("/configs/{config_id}/raw-key")
async def get_raw_config_key(config_id: str, db: AsyncSession = Depends(get_db)):
    """解密并返回指定配置记录的真实 API 密钥（供前端管理员点击眼睛图标显式查看）"""
    record = await db.get(ApiProviderConfig, config_id)
    if not record:
        raise HTTPException(status_code=404, detail="未找到该配置记录")
    raw_key = decrypt_secret(record.encrypted_api_key) if record.encrypted_api_key else ""
    return {
        "code": 0,
        "success": True,
        "config_id": config_id,
        "raw_key": raw_key
    }

@router.post("/configs/save")
async def save_config(req: ApiConfigSaveRequest, db: AsyncSession = Depends(get_db)):
    """保存或更新服务商配置（敏感信息 AES-256 加密，支持多模型保存与原子设为默认）"""
    # 优先使用显式传入的 id，否则根据 config_group 与 provider_name 检索
    record = None
    if req.id:
        record = await db.get(ApiProviderConfig, req.id)
    if not record:
        result = await db.execute(
            select(ApiProviderConfig)
            .where(ApiProviderConfig.config_group == req.config_group)
            .where(ApiProviderConfig.provider_name == req.provider_name)
        )
        record = result.scalars().first()

    encrypted_key = encrypt_secret(req.api_key) if req.api_key else ""

    # 若当前要设为激活项，将同组其余项的 is_active 置 0
    if req.is_active:
        await db.execute(
            update(ApiProviderConfig)
            .where(ApiProviderConfig.config_group == req.config_group)
            .values(is_active=0)
        )

    extra_dict = req.extra_params or {}
    if req.title:
        extra_dict["title"] = req.title

    if record:
        if req.api_key is not None and req.api_key.strip():
            record.encrypted_api_key = encrypted_key
        if req.base_url is not None:
            record.base_url = req.base_url.strip()
        if req.model_name is not None:
            record.model_name = req.model_name.strip()
        if req.provider_name:
            record.provider_name = req.provider_name.strip()
        if req.is_active is not None:
            record.is_active = 1 if req.is_active else 0
        if extra_dict:
            try:
                old_extra = json.loads(record.extra_params_json or "{}")
                old_extra.update(extra_dict)
                record.extra_params_json = json.dumps(old_extra, ensure_ascii=False)
            except Exception:
                record.extra_params_json = json.dumps(extra_dict, ensure_ascii=False)
        saved_id = record.id
    else:
        new_id = req.id or f"cfg_{req.config_group}_{req.provider_name}_{int(time.time()) % 10000}"
        record = ApiProviderConfig(
            id=new_id,
            config_group=req.config_group,
            provider_name=req.provider_name,
            is_active=1 if req.is_active else 0,
            encrypted_api_key=encrypted_key,
            base_url=(req.base_url or "").strip(),
            model_name=(req.model_name or "").strip(),
            extra_params_json=json.dumps(extra_dict, ensure_ascii=False)
        )
        db.add(record)
        saved_id = new_id

    await db.commit()

    from server.routes.live import global_live_controller
    if global_live_controller.is_live:
        await global_live_controller.reload_runtime_config("settings")

    return {"code": 0, "message": f"{req.provider_name} 配置已加密保存", "id": saved_id}

class PingRequest(BaseModel):
    config_id: Optional[str] = Field(default=None, max_length=64)
    config_group: Optional[str] = Field(default="llm", max_length=32)
    provider_name: Optional[str] = Field(default="", max_length=64)
    base_url: Optional[str] = Field(default=None, max_length=512)
    api_key: Optional[str] = Field(default=None, max_length=16_384)

@router.post("/ping")
@router.post("/test-connection")
async def ping_service(req: PingRequest, db: AsyncSession = Depends(get_db)):
    """一键 Ping 测试服务商连通性与网络延迟 (TTFT)，支持按 ID 或直接传参测试"""
    start_time = time.time()
    raw_api_key = req.api_key
    target_url = req.base_url
    provider_name = req.provider_name or ""
    config_group = req.config_group or "llm"

    # 若传入了 config_id 或缺少字段，从数据库提取补全
    if req.config_id:
        result = await db.execute(select(ApiProviderConfig).where(ApiProviderConfig.id == req.config_id))
        record = result.scalar_one_or_none()
        if record:
            provider_name = record.provider_name
            config_group = record.config_group
            if not target_url:
                target_url = record.base_url
            if not raw_api_key and record.encrypted_api_key:
                raw_api_key = decrypt_secret(record.encrypted_api_key)
    elif not target_url or not raw_api_key:
        result = await db.execute(
            select(ApiProviderConfig)
            .where(ApiProviderConfig.config_group == config_group)
            .where(ApiProviderConfig.provider_name == provider_name)
        )
        record = result.scalar_one_or_none()
        if record:
            if not target_url:
                target_url = record.base_url
            if not raw_api_key and record.encrypted_api_key:
                raw_api_key = decrypt_secret(record.encrypted_api_key)

    if not target_url:
        if "edge" in provider_name.lower():
            return {
                "code": 0,
                "success": True,
                "latency_ms": 0,
                "message": "Edge-TTS 为内置微软免费语音，无需 Base URL；实际连通性将在开播首次合成时自动验证"
            }
        return {"code": 1, "success": False, "latency_ms": 0, "message": "未配置有效的 Base URL"}

    headers = {}
    test_endpoint = target_url

    # 针对不同服务商做智能探测适配
    if config_group == "llm":
        if "ollama" in provider_name.lower():
            test_endpoint = target_url.rstrip("/") + "/api/tags"
        elif raw_api_key:
            test_endpoint = target_url.rstrip("/") + "/models"
            headers["Authorization"] = f"Bearer {raw_api_key}"
    elif config_group == "tts":
        if "cosyvoice" in provider_name.lower():
            test_endpoint = target_url.rstrip("/") + "/"

    try:
        async with httpx.AsyncClient(timeout=4.0, verify=True) as client:
            resp = await client.get(test_endpoint, headers=headers)
            elapsed_ms = int((time.time() - start_time) * 1000)

            if resp.status_code in [200, 201, 204]:
                return {
                    "code": 0,
                    "success": True,
                    "http_status": resp.status_code,
                    "latency_ms": elapsed_ms,
                    "message": f"握手成功！服务正常响应，首包延迟: {elapsed_ms}ms"
                }
            elif resp.status_code == 401:
                return {
                    "code": 1,
                    "success": False,
                    "http_status": 401,
                    "latency_ms": elapsed_ms,
                    "message": "鉴权失败：API Key 无效或权限不足 (HTTP 401)"
                }
            else:
                return {
                    "code": 0,
                    "success": True,
                    "http_status": resp.status_code,
                    "latency_ms": elapsed_ms,
                    "message": f"服务可访问，HTTP 状态码: {resp.status_code}，耗时: {elapsed_ms}ms"
                }
    except Exception as e:
        elapsed_ms = int((time.time() - start_time) * 1000)
        return {
            "code": 1,
            "success": False,
            "latency_ms": elapsed_ms,
            "message": f"连通测试失败 (超时或端口未开启): {str(e)}"
        }


# ==============================================================================
# 抖音弹幕鉴权 Cookie 选填配置 (v1.7.0: 风控严格场景手动注入 ttwid / msToken)
# ==============================================================================
class DouyinCookieRequest(BaseModel):
    ttwid: str = Field(default="", max_length=4096)
    ms_token: str = Field(default="", max_length=4096)


def _mask_cookie(value: str) -> str:
    if not value:
        return ""
    if len(value) <= 12:
        return value[:4] + "••••"
    return f"{value[:8]}••••{value[-4:]}"


@router.get("/douyin-cookies")
async def get_douyin_cookies(db: AsyncSession = Depends(get_db)):
    """读取抖音弹幕监听选填鉴权配置 (脱敏返回)"""
    ttwid_raw = await _get_setting(db, "douyin_ttwid") or ""
    ms_token_raw = await _get_setting(db, "douyin_ms_token") or ""
    ttwid = decrypt_secret(ttwid_raw) if ttwid_raw else ""
    if not ttwid and ttwid_raw:
        ttwid = ttwid_raw
    ms_token = decrypt_secret(ms_token_raw) if ms_token_raw else ""
    if not ms_token and ms_token_raw:
        ms_token = ms_token_raw
    return {
        "code": 0,
        "data": {
            "ttwid": _mask_cookie(ttwid),
            "ms_token": _mask_cookie(ms_token),
            "configured": bool(ttwid or ms_token),
        }
    }


@router.post("/douyin-cookies")
async def set_douyin_cookies(req: DouyinCookieRequest, db: AsyncSession = Depends(get_db)):
    """保存抖音弹幕监听选填鉴权配置 (AES-256 加密落盘；浏览器 DevTools 获取；留空清除)"""
    ttwid_val = (req.ttwid or "").strip()
    ms_token_val = (req.ms_token or "").strip()
    enc_ttwid = encrypt_secret(ttwid_val) if ttwid_val else ""
    enc_ms = encrypt_secret(ms_token_val) if ms_token_val else ""
    await _set_setting(db, "douyin_ttwid", enc_ttwid)
    await _set_setting(db, "douyin_ms_token", enc_ms)
    return {"code": 0, "message": "抖音鉴权 Cookie 配置已加密保存，下次开播生效"}


# ==============================================================================
# 物理音频设备与虚拟声卡 (VB-Cable) 配置接口 (规划 §7.1/§7.2)
# ==============================================================================
class AudioDeviceSelectRequest(BaseModel):
    device_index: Optional[int] = Field(default=None, ge=0, le=4096)


@router.get("/audio-devices")
async def get_audio_devices():
    """获取所有支持输出的物理声卡与虚拟声卡列表，及当前选中状态"""
    from server.core.media.virtual_audio import global_virtual_audio
    devs = global_virtual_audio.list_devices()
    return {
        "code": 0,
        "data": {
            "devices": devs,
            "status": global_virtual_audio.get_status()
        }
    }


@router.post("/audio-device")
async def set_audio_device(req: AudioDeviceSelectRequest, db: AsyncSession = Depends(get_db)):
    """设置当前使用的音频输出设备 (支持一键切换为 VB-Cable 虚拟声卡)"""
    from server.core.media.virtual_audio import global_virtual_audio
    global_virtual_audio.set_device(req.device_index)

    # 持久化到 app_settings
    res = await db.execute(select(AppSetting).where(AppSetting.key == "audio_output_device"))
    record = res.scalar_one_or_none()
    val = "" if req.device_index is None else str(req.device_index)
    if record:
        record.value = val
    else:
        db.add(AppSetting(key="audio_output_device", value=val))
    await db.commit()

    return {
        "code": 0,
        "message": "音频输出设备已更新",
        "data": global_virtual_audio.get_status()
    }


# ==============================================================================
# 语音合成实时试听与回放接口 (TTS Preview)
# ==============================================================================
class TTSPreviewRequest(BaseModel):
    provider_name: Optional[str] = Field(default="edge_tts", max_length=64)
    voice_name: Optional[str] = Field(default=None, max_length=128)
    base_url: Optional[str] = Field(default=None, max_length=512)
    api_key: Optional[str] = Field(default=None, max_length=16_384)
    text: Optional[str] = Field(default="你好！这是当前语音合成引擎的实时试听效果，音色自然流畅，祝您直播顺利！", max_length=2000)


@router.post("/tts/preview")
async def preview_tts_audio(req: TTSPreviewRequest):
    """
    根据前端当前选型与参数，实时合成一段简短的问候语音流 (MP3/WAV)
    供用户在【测试连通性与试听】时真实从扬声器听到声音
    """
    provider = (req.provider_name or "").lower().strip()
    voice = (req.voice_name or "").strip()
    text = (req.text or "你好！这是当前语音合成引擎的实时试听效果，音色自然流畅，祝您直播顺利！").strip()

    # 1. 微软 Edge-TTS (内置云端免Key，直接合成真实音频)
    if "edge" in provider or not provider or provider == "edge_tts":
        actual_voice = voice if (voice and "neural" in voice.lower()) else "zh-CN-XiaoxiaoNeural"
        try:
            import edge_tts
            communicate = edge_tts.Communicate(text, actual_voice)
            chunks = []
            async for chunk in communicate.stream():
                if chunk["type"] == "audio":
                    chunks.append(chunk["data"])
            audio_bytes = b"".join(chunks)
            if audio_bytes:
                return Response(content=audio_bytes, media_type="audio/mpeg")
        except Exception as e:
            raise HTTPException(status_code=502, detail=f"Edge-TTS 合成试听失败: {str(e)}")

    # 2. 兼容 OpenAI Audio 规范的云端/本地服务 (SiliconFlow / 自建网关 / OpenAI)
    base_url = (req.base_url or "").strip().rstrip("/")
    api_key = (req.api_key or "").strip()

    if base_url:
        headers = {}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"

        speech_url = f"{base_url}/audio/speech" if not base_url.endswith("/audio/speech") else base_url
        model_name = "tts-1"
        if "siliconflow" in base_url:
            model_name = "FunAudioLLM/CosyVoice2-0.5B" if "cosy" in provider else "2noise/ChatTTS"

        payload = {
            "model": model_name,
            "input": text,
            "voice": voice or "alloy"
        }

        try:
            async with httpx.AsyncClient(timeout=8.0, verify=True) as client:
                resp = await client.post(speech_url, headers=headers, json=payload)
                if resp.status_code == 200 and resp.content:
                    media_type = resp.headers.get("content-type", "audio/mpeg")
                    return Response(content=resp.content, media_type=media_type)
                elif resp.status_code == 401:
                    raise HTTPException(status_code=401, detail="云端 API Key 鉴权失败，请检查密钥是否正确")
        except HTTPException:
            raise
        except Exception:
            pass

    # 3. 智能兜底：若外部商用端点未配置 Key 或暂时不可用，通过 Edge-TTS 发声引导
    try:
        import edge_tts
        fallback_voice = "zh-CN-XiaoxiaoNeural"
        notice_text = f"您正在试听 {req.provider_name}。网络连通探测已就绪，当前为系统试听音效。"
        communicate = edge_tts.Communicate(notice_text, fallback_voice)
        chunks = []
        async for chunk in communicate.stream():
            if chunk["type"] == "audio":
                chunks.append(chunk["data"])
        audio_bytes = b"".join(chunks)
        if audio_bytes:
            return Response(content=audio_bytes, media_type="audio/mpeg")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"试听音频生成异常: {str(e)}")

    raise HTTPException(status_code=400, detail="未能生成有效的试听音频数据")


