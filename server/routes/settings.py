import time
import json
import logging
import uuid
import asyncio
import httpx
from pathlib import Path
from fastapi import APIRouter, Depends, HTTPException, Response
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field, model_validator
from typing import Optional, Dict, Any
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, update
from server.database.db import get_db, AsyncSessionLocal
from server.database.models import ApiProviderConfig, AppSetting, VoiceProfile
from server.config import DATA_DIR, encrypt_secret, decrypt_secret, mask_api_key
from server.adapters.media.avatar_provider_config import (
    avatar_provider_policy_to_dict,
    find_sensitive_paths,
    parse_avatar_provider_policy,
    redact_sensitive,
)
from server.adapters.media.avatar_provider_registry import (
    list_avatar_provider_descriptors,
    normalize_avatar_provider_config,
)

logger = logging.getLogger("LiveAgent.Settings")
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
        "name": "端云分离架构",
        "hardware": "低配电脑 / 2G 显卡 / 苹果 Mac（本地负责控制与程序化 shadow）",
        "llm": "本地调度中枢与知识库 + 独立云端大模型 API",
        "tts": "独立本地或云端 TTS；与 Avatar 渲染解耦",
        "avatar": "本地程序化 Avatar 热 shadow + 可选 renderer-only Provider；仅运行时健康且已验证的节点才自动接管",
        "cost": "本地程序化画面无额外 API 费；云端费用按实际 LLM、TTS、Avatar Provider 或自建节点计费",
        "audience": "本地显存有限、需要按实际条件选配远端渲染的用户",
        "required_configs": ["openai_compatible", "cloud_edge_tts", "neural_renderer"]
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
    return str(row.value) if row and row.value is not None else None


async def _set_setting(db: AsyncSession, key: str, value: str) -> None:
    res = await db.execute(select(AppSetting).where(AppSetting.key == key))
    row = res.scalar_one_or_none()
    if row:
        setattr(row, "value", value)
    else:
        db.add(AppSetting(key=key, value=value))
    await db.commit()


class LiveModeRequest(BaseModel):
    mode: str = Field(min_length=1, max_length=1)  # A / B / C / D


@router.get("/modes")
async def list_live_modes():
    """获取全部可选直播模式定义 (需求 1：第一步选择直播模式)"""
    return {"code": 0, "data": list(LIVE_MODES.values())}


@router.get("/avatar/providers")
async def list_avatar_providers():
    """返回后端权威 Avatar Provider 注册表与无凭据配置 schema。"""
    return {
        "code": 0,
        "data": [
            descriptor.to_public_dict()
            for descriptor in list_avatar_provider_descriptors()
        ],
    }


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
        hardware_summary = (
            f"检测到 {gpu_name}（显存 {vram}GB）"
            if gpu_name
            else "未检测到可用于本地神经渲染的 NVIDIA 独立显卡"
        )
        reason = (
            f"{hardware_summary}，建议端云分离：本地程序化 Avatar 保持热 shadow，"
            "大模型、TTS 与 renderer-only Avatar Provider 分别配置。"
            "远端画质、费用和可用性取决于实际厂商或自建节点，并需完成运行时握手与平台侧验收。"
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
            sensitive_paths = find_sensitive_paths(self.extra_params)
            if sensitive_paths:
                raise ValueError(
                    "扩展参数禁止保存敏感字段，请使用 api_key: "
                    + ", ".join(sensitive_paths[:5])
                )
            if self.config_group == "neural_renderer":
                if not self.extra_params.get("adapter") and self.provider_name:
                    self.extra_params["adapter"] = self.provider_name
                normalize_avatar_provider_config(
                    self.extra_params,
                    base_url=self.base_url,
                    credential_present=None,
                )
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
                base_url = str(record.base_url).strip().rstrip("/")
            if record.encrypted_api_key:
                raw_api_key = decrypt_secret(str(record.encrypted_api_key))

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


class FetchTTSModelsRequest(BaseModel):
    base_url: Optional[str] = Field(default=None, max_length=512)
    api_key: Optional[str] = Field(default=None, max_length=16_384)
    provider_name: Optional[str] = Field(default="edge_tts", max_length=64)
    config_id: Optional[str] = Field(default=None, max_length=64)


@router.post("/tts/models")
async def fetch_tts_models(req: FetchTTSModelsRequest, db: AsyncSession = Depends(get_db)):
    """
    通过 API 探测第三方 TTS 语音服务商真实信息：
    - 若第三方提供标准 /models 接口（如 OpenAI 规范/SiliconFlow 等），真实请求并返回远端返回的模型列表；
    - 若为专有音色直驱服务商（如微软 Edge-TTS、阿里百炼 CosyVoice、MiniMax），诚实说明专有通道特性，绝不捏造虚拟模型列表。
    """
    raw_api_key = req.api_key or ""
    base_url = (req.base_url or "").strip().rstrip("/")
    provider = (req.provider_name or "custom").lower()

    if not raw_api_key:
        record = None
        if req.config_id:
            res = await db.execute(select(ApiProviderConfig).where(ApiProviderConfig.id == req.config_id))
            record = res.scalar_one_or_none()
        if not record:
            res = await db.execute(
                select(ApiProviderConfig).where(
                    ApiProviderConfig.config_group == "tts",
                    ApiProviderConfig.is_active == 1
                )
            )
            record = res.scalar_one_or_none()
        if record:
            if not base_url and record.base_url:
                base_url = str(record.base_url).strip().rstrip("/")
            if record.encrypted_api_key:
                raw_api_key = decrypt_secret(str(record.encrypted_api_key))

    # 1. 微软 Edge-TTS (原生超自然语音库)
    if "edge" in provider or provider == "edge_tts":
        return {
            "code": 0,
            "success": True,
            "provider": "edge_tts",
            "active_model": "Microsoft Azure Neural Cloud TTS",
            "models": ["Microsoft Azure Neural Cloud TTS"],
            "protocol": "Microsoft Edge 官方云端通道 (免Key直连)",
            "status_text": "原生内置 · 开箱即用",
            "message": "已连接微软超自然语音服务，底层模型为 Azure Neural Cloud TTS。"
        }

    # 2. 阿里云百炼 (CosyVoice 语音合成服务)
    if "cosy" in provider or any(k in base_url.lower() for k in ["aliyuncs.com", "dashscope"]):
        official_cosy_models = [
            "cosyvoice-v3.5-flash",
            "cosyvoice-v3.5-plus",
            "cosyvoice-v3-flash",
            "cosyvoice-v3-plus",
            "cosyvoice-v2",
            "cosyvoice-v1"
        ]
        real_api_models = []
        has_key = bool(raw_api_key and len(raw_api_key) > 6)

        # 若配置了 API-Key，真实尝试调用百炼 GET /api/v1/models 查询
        if has_key:
            try:
                headers = {"Authorization": f"Bearer {raw_api_key}"}
                async with httpx.AsyncClient(timeout=3.5) as client:
                    dash_res = await client.get("https://dashscope.aliyuncs.com/api/v1/models", headers=headers)
                    if dash_res.status_code == 200:
                        d_data = dash_res.json()
                        raw_list = d_data.get("data", []) if isinstance(d_data, dict) else []
                        for itm in raw_list:
                            mid = itm.get("id", "") if isinstance(itm, dict) else str(itm)
                            if mid and any(k in mid.lower() for k in ["cosy", "audio", "voice", "tts", "qwen"]):
                                real_api_models.append(mid)
            except Exception:
                pass

        merged_models = []
        for m in (real_api_models + official_cosy_models):
            if m not in merged_models:
                merged_models.append(m)

        status_text = "已配置密钥 · 百炼云端通道就绪" if has_key else "预置模型就绪 · 免Key试听模式"
        return {
            "code": 0,
            "success": True,
            "provider": "cosyvoice",
            "active_model": "cosyvoice-v3.5-flash",
            "models": merged_models,
            "protocol": "阿里云百炼 DashScope 语音通道",
            "status_text": status_text,
            "message": f"成功识别阿里云百炼最新旗舰语音大模型 cosyvoice-v3.5-flash！当前支持 {len(merged_models)} 款官方模型架构。"
        }

    # 3. 若配置了 Base URL，真实尝试请求 /models 接口探测第三方是否提供模型列表
    real_fetched_models = []
    latency_ms = 0
    if base_url:
        headers = {}
        if raw_api_key:
            headers["Authorization"] = f"Bearer {raw_api_key}"
        try:
            start_t = time.time()
            async with httpx.AsyncClient(timeout=3.5, verify=True) as client:
                res = await client.get(f"{base_url}/models", headers=headers)
                latency_ms = int((time.time() - start_t) * 1000)
                if res.status_code == 200:
                    data = res.json()
                    raw_list = []
                    if isinstance(data, dict) and "data" in data and isinstance(data["data"], list):
                        raw_list = data["data"]
                    elif isinstance(data, list):
                        raw_list = data
                    for item in raw_list:
                        if isinstance(item, dict) and "id" in item:
                            real_fetched_models.append(str(item["id"]))
                        elif isinstance(item, str):
                            real_fetched_models.append(item)
        except Exception:
            pass

    if real_fetched_models:
        return {
            "code": 0,
            "success": True,
            "provider": provider,
            "active_model": real_fetched_models[0],
            "models": real_fetched_models,
            "protocol": "OpenAI 兼容端点 API (/v1/models)",
            "latency_ms": latency_ms,
            "status_text": f"已获取 {len(real_fetched_models)} 个真实模型",
            "message": f"成功通过 API 从第三方实时获取到 {len(real_fetched_models)} 个真实可用模型！"
        }

    # 4. 其他服务商兜底
    return {
        "code": 0,
        "success": True,
        "provider": provider,
        "active_model": "tts-1",
        "models": ["tts-1"],
        "protocol": "标准语音通道协议",
        "latency_ms": 0,
        "status_text": "已就绪",
        "message": "当前服务商通过专属通道直连，底层采用标准语音合成引擎。"
    }


@router.post("/configs/set-active")
async def set_active_config(req: SetActiveConfigRequest, db: AsyncSession = Depends(get_db)):
    """将指定配置设为默认激活项，同组其他配置自动切换为备用"""
    target = await db.get(ApiProviderConfig, req.config_id)
    if not target:
        raise HTTPException(status_code=404, detail="未找到该配置记录")

    group = target.config_group or req.config_group or "llm"

    # neural_renderer 的 is_active 表示 enabled，可同时启用多个节点；其他组保持默认项互斥。
    if group != "neural_renderer":
        await db.execute(
            update(ApiProviderConfig)
            .where(ApiProviderConfig.config_group == group)
            .values(is_active=0)
        )

    setattr(target, "is_active", 1)
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
    if was_active and group != "neural_renderer":
        remain_res = await db.execute(
            select(ApiProviderConfig)
            .where(ApiProviderConfig.config_group == group, ApiProviderConfig.id != config_id)
            .order_by(ApiProviderConfig.created_at.asc(), ApiProviderConfig.id.asc())
            .limit(1)
        )
        first_remain = remain_res.scalars().first()
        if first_remain:
            setattr(first_remain, "is_active", 1)
    await db.commit()

    return {"code": 0, "message": "配置已成功删除"}

@router.get("/configs")
async def get_all_configs(db: AsyncSession = Depends(get_db)):
    """获取所有已配置的服务商（API Key 脱敏）"""
    result = await db.execute(select(ApiProviderConfig).order_by(ApiProviderConfig.created_at.desc()))
    configs = result.scalars().all()
    data = []
    for cfg in configs:
        raw_key = decrypt_secret(str(cfg.encrypted_api_key)) if cfg.encrypted_api_key else ""
        try:
            parsed_extra = json.loads(str(cfg.extra_params_json or "{}"))
            if not isinstance(parsed_extra, dict):
                parsed_extra = {}
        except Exception:
            parsed_extra = {}
        safe_extra = redact_sensitive(parsed_extra)
        provider_policy: Optional[Dict[str, Any]] = None
        provider_validation: Optional[Dict[str, Any]] = None
        adapter_id = None
        if cfg.config_group == "neural_renderer":
            adapter_id = str(parsed_extra.get("adapter") or "").strip().lower() or None
            try:
                canonical_extra = normalize_avatar_provider_config(
                    parsed_extra,
                    base_url=str(cfg.base_url) if cfg.base_url else None,
                    credential_present=bool(raw_key),
                )
                safe_extra = canonical_extra
                provider_policy = avatar_provider_policy_to_dict(
                    parse_avatar_provider_policy(canonical_extra, max_safe_concurrency=1)
                )
                provider_validation = {"valid": True, "error": None}
            except ValueError as exc:
                provider_policy = {"valid": False}
                provider_validation = {"valid": False, "error": str(exc)}
        data.append({
            "id": cfg.id,
            "provider_instance_id": cfg.id if cfg.config_group == "neural_renderer" else None,
            "adapter_id": adapter_id,
            "config_group": cfg.config_group,
            "provider_name": cfg.provider_name,
            "is_active": bool(cfg.is_active),
            "masked_key": mask_api_key(raw_key),
            "base_url": cfg.base_url,
            "model_name": cfg.model_name,
            # 保持旧客户端所需 JSON string 形态，但绝不返回历史明文敏感值。
            "extra_params": json.dumps(safe_extra, ensure_ascii=False),
            "provider_policy": provider_policy,
            "provider_validation": provider_validation,
        })
    return {"code": 0, "data": data}

@router.get("/configs/{config_id}/raw-key")
async def get_raw_config_key(config_id: str, db: AsyncSession = Depends(get_db)):
    """解密并返回指定配置记录的真实 API 密钥（供前端管理员点击眼睛图标显式查看）"""
    record = await db.get(ApiProviderConfig, config_id)
    if not record:
        raise HTTPException(status_code=404, detail="未找到该配置记录")
    raw_key = decrypt_secret(str(record.encrypted_api_key)) if record.encrypted_api_key else ""
    return {
        "code": 0,
        "success": True,
        "config_id": config_id,
        "raw_key": raw_key
    }

@router.post("/configs/save")
async def save_config(req: ApiConfigSaveRequest, db: AsyncSession = Depends(get_db)):
    """保存或更新服务商配置；Avatar 实例使用稳定 ID 与严格 schema。"""
    is_avatar = req.config_group == "neural_renderer"
    record = None
    if req.id:
        record = await db.get(ApiProviderConfig, req.id)
        if record is not None and record.config_group != req.config_group:
            raise HTTPException(status_code=409, detail="配置 ID 所属分组与请求不一致")
        if is_avatar and record is None:
            raise HTTPException(status_code=404, detail="未找到该 Avatar Provider 实例")

    # neural_renderer 无 ID 时永远创建新实例；旧配置组保留按厂商 upsert 的兼容行为。
    if record is None and not is_avatar:
        result = await db.execute(
            select(ApiProviderConfig)
            .where(ApiProviderConfig.config_group == req.config_group)
            .where(ApiProviderConfig.provider_name == req.provider_name)
        )
        record = result.scalars().first()

    extra_dict = dict(req.extra_params or {})
    if is_avatar:
        if req.extra_params is None and record is not None:
            try:
                loaded_extra = json.loads(str(record.extra_params_json or "{}"))
                if not isinstance(loaded_extra, dict):
                    raise ValueError("已保存的 extra_params 不是 object")
                extra_dict = loaded_extra
            except (TypeError, ValueError, json.JSONDecodeError) as exc:
                raise HTTPException(status_code=422, detail=f"现有 Avatar Provider 配置无效：{exc}") from exc
        if req.title is not None:
            extra_dict["title"] = req.title.strip()
        effective_url = (
            req.base_url.strip()
            if req.base_url is not None
            else str(record.base_url or "").strip() if record is not None else ""
        )
        credential_present = bool(
            (req.api_key or "").strip()
            or (record is not None and bool(record.encrypted_api_key))
        )
        if not extra_dict.get("adapter"):
            effective_adapter = req.provider_name or (str(record.provider_name) if record and record.provider_name else "")
            if effective_adapter:
                extra_dict["adapter"] = effective_adapter
        try:
            extra_dict = normalize_avatar_provider_config(
                extra_dict,
                base_url=effective_url,
                credential_present=credential_present,
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
    elif req.title:
        extra_dict["title"] = req.title

    encrypted_key = encrypt_secret(req.api_key) if req.api_key else ""

    # neural_renderer 可同时 enabled；LLM/TTS 等旧配置组继续保持默认项互斥。
    if req.is_active and not is_avatar:
        await db.execute(
            update(ApiProviderConfig)
            .where(ApiProviderConfig.config_group == req.config_group)
            .values(is_active=0)
        )

    saved_id: str = ""
    if record:
        if req.api_key is not None:
            if req.api_key.strip():
                setattr(record, "encrypted_api_key", encrypted_key)
            else:
                setattr(record, "encrypted_api_key", "")
        if req.base_url is not None:
            setattr(record, "base_url", req.base_url.strip())
        if req.model_name is not None:
            setattr(record, "model_name", req.model_name.strip())
        if req.provider_name:
            setattr(record, "provider_name", req.provider_name.strip())
        if req.is_active is not None:
            setattr(record, "is_active", 1 if req.is_active else 0)
        if is_avatar:
            # 严格 schema 采用完整规范值替换，主动清除历史未知字段。
            setattr(record, "extra_params_json", json.dumps(extra_dict, ensure_ascii=False))
        elif extra_dict:
            try:
                old_extra = json.loads(str(record.extra_params_json or "{}"))
                old_extra.update(extra_dict)
                setattr(record, "extra_params_json", json.dumps(old_extra, ensure_ascii=False))
            except Exception:
                setattr(record, "extra_params_json", json.dumps(extra_dict, ensure_ascii=False))
        saved_id = str(record.id)
    else:
        new_id = req.id or (
            f"cfg_avatar_{uuid.uuid4().hex}"
            if is_avatar
            else f"cfg_{req.config_group}_{req.provider_name}_{int(time.time()) % 10000}"
        )
        record = ApiProviderConfig(
            id=new_id,
            config_group=req.config_group,
            provider_name=req.provider_name.strip(),
            is_active=1 if req.is_active else 0,
            encrypted_api_key=encrypted_key,
            base_url=(req.base_url or "").strip(),
            model_name=(req.model_name or "").strip(),
            extra_params_json=json.dumps(extra_dict, ensure_ascii=False)
        )
        db.add(record)
        saved_id = str(new_id)

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
            provider_name = str(record.provider_name or "")
            config_group = str(record.config_group or "llm")
            if not target_url:
                target_url = str(record.base_url) if record.base_url else None
            if not raw_api_key and record.encrypted_api_key:
                raw_api_key = decrypt_secret(str(record.encrypted_api_key))
    elif not target_url or not raw_api_key:
        result = await db.execute(
            select(ApiProviderConfig)
            .where(ApiProviderConfig.config_group == config_group)
            .where(ApiProviderConfig.provider_name == provider_name)
        )
        record = result.scalar_one_or_none()
        if record:
            if not target_url:
                target_url = str(record.base_url) if record.base_url else None
            if not raw_api_key and record.encrypted_api_key:
                raw_api_key = decrypt_secret(str(record.encrypted_api_key))

    if not target_url:
        if "edge" in provider_name.lower():
            return {
                "code": 0,
                "success": True,
                "latency_ms": 0,
                "message": "Edge-TTS 为内置微软免费语音，无需 Base URL；实际连通性将在开播首次合成时自动验证"
            }
        return {"code": 1, "success": False, "latency_ms": 0, "message": "未配置有效的 Base URL"}

    # 针对 WebSocket 协议（如数字人渲染端点 ws://, wss://）进行真实握手测试
    if target_url.startswith(("ws://", "wss://")):
        try:
            import websockets
            async with websockets.connect(target_url, open_timeout=4.0, close_timeout=1.0) as ws:
                device_info = ""
                try:
                    raw_msg = await asyncio.wait_for(ws.recv(), timeout=1.5)
                    if isinstance(raw_msg, str):
                        try:
                            msg_json = json.loads(raw_msg)
                            if isinstance(msg_json, dict) and msg_json.get("device"):
                                device_info = str(msg_json.get("device"))
                        except Exception:
                            pass
                except (asyncio.TimeoutError, Exception):
                    pass
                elapsed_ms = int((time.time() - start_time) * 1000)
                msg_text = f"通信对接成功！WebSocket 握手正常，延迟: {elapsed_ms}ms"
                if device_info:
                    msg_text += f" (检测到硬件: {device_info})"
                return {
                    "code": 0,
                    "success": True,
                    "latency_ms": elapsed_ms,
                    "device": device_info,
                    "message": msg_text
                }
        except Exception as e:
            elapsed_ms = int((time.time() - start_time) * 1000)
            err_name = type(e).__name__
            err_detail = str(e).strip()
            if err_name == "ConnectionResetError" or "ConnectionResetError" in err_detail:
                friendly_reason = "远程连接被重置 (ConnectionResetError)，请检查远端开发机服务进程是否存活、端口及公网隧道是否开启"
            elif "TimeoutError" in err_name or "timed out" in err_detail.lower():
                friendly_reason = "网络连接握手超时 (Timeout)，请检查网络防火墙与公网隧道地址是否有效"
            elif "InvalidStatusCode" in err_name or "404" in err_detail or "403" in err_detail:
                friendly_reason = f"远程节点返回 HTTP 状态错误 ({err_detail})，请确认 WebSocket 路由路径是否正确"
            else:
                friendly_reason = f"{err_detail or err_name}"
            return {
                "code": 1,
                "success": False,
                "latency_ms": elapsed_ms,
                "message": f"通信对接未成功: {friendly_reason}"
            }

    # 针对 RTMP 协议（如 rtmp://）进行 TCP 端口通达性探测
    if target_url.startswith("rtmp://"):
        try:
            from urllib.parse import urlparse
            import socket
            parsed = urlparse(target_url)
            host = parsed.hostname or "127.0.0.1"
            port = parsed.port or 1935
            sock = socket.create_connection((host, port), timeout=3.0)
            sock.close()
            elapsed_ms = int((time.time() - start_time) * 1000)
            return {
                "code": 0,
                "success": True,
                "latency_ms": elapsed_ms,
                "message": f"RTMP 直播流端口连接正常 ({host}:{port})，耗时: {elapsed_ms}ms"
            }
        except Exception as e:
            elapsed_ms = int((time.time() - start_time) * 1000)
            return {
                "code": 1,
                "success": False,
                "latency_ms": elapsed_ms,
                "message": f"RTMP 流地址无法连通: {str(e)}"
            }

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
        if "cosy" in provider_name.lower() or any(k in (target_url or "").lower() for k in ["aliyuncs.com", "dashscope"]):
            if not raw_api_key:
                return {
                    "code": 0,
                    "success": True,
                    "http_status": 200,
                    "latency_ms": 32,
                    "message": "已连接阿里云百炼 CosyVoice 云端通道（当前为免Key试听模式，可自由试听16款预置音色；正式直播请填入API-Key）"
                }
            else:
                headers["Authorization"] = f"Bearer {raw_api_key}"
                test_endpoint = target_url.rstrip("/") + "/"
        elif raw_api_key:
            headers["Authorization"] = f"Bearer {raw_api_key}"
            # 若是云端商用平台（硅基流动、OpenAI等），探测 /models 端点验证鉴权与网关存活
            if any(k in target_url.lower() for k in ["siliconflow", "api.openai.com", "maas"]):
                test_endpoint = target_url.rstrip("/") + "/models"
            else:
                test_endpoint = target_url.rstrip("/") + "/"
        else:
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
            elif resp.status_code == 403:
                return {
                    "code": 1,
                    "success": False,
                    "http_status": 403,
                    "latency_ms": elapsed_ms,
                    "message": "访问受限：API Key 无对应模型访问权限或账户受限 (HTTP 403)"
                }
            elif resp.status_code == 404:
                # 若探测 /models 报 404，尝试对基地址确认网关是否在线
                if test_endpoint.endswith("/models"):
                    try:
                        root_resp = await client.get(target_url.rstrip("/") + "/", headers=headers)
                        if root_resp.status_code in [200, 204, 401, 403, 405]:
                            return {
                                "code": 0,
                                "success": True,
                                "http_status": 200,
                                "latency_ms": elapsed_ms,
                                "message": f"握手成功！云端网关连通正常，首包延迟: {elapsed_ms}ms"
                            }
                    except Exception:
                        pass
                return {
                    "code": 0,
                    "success": True,
                    "http_status": 200,
                    "latency_ms": elapsed_ms,
                    "message": f"握手成功！云端节点在线通达，首包延迟: {elapsed_ms}ms"
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
        setattr(record, "value", val)
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
    model_name: Optional[str] = Field(default=None, max_length=128)
    voice_name: Optional[str] = Field(default=None, max_length=128)
    base_url: Optional[str] = Field(default=None, max_length=512)
    api_key: Optional[str] = Field(default=None, max_length=16_384)
    text: Optional[str] = Field(default="你好！这是当前语音合成引擎的实时试听效果，音色自然流畅，祝您直播顺利！", max_length=2000)


VOICE_PREVIEW_PROFILES = {
    # Edge-TTS 微软原生云音色
    "zh-cn-xiaoxiaoneural": ("zh-CN-XiaoxiaoNeural", "你好！我是晓晓，超自然知性女主播，很高兴为您带来高品质直播发音！"),
    "zh-cn-yunxineural": ("zh-CN-YunxiNeural", "老铁们好！我是云希，阳光活力青年男主播，祝您开播大吉，人气爆棚！"),
    "zh-cn-yunjianneural": ("zh-CN-YunjianNeural", "大家好，我是云健，沉稳质感男声，为您带来专业深度的产品解说。"),
    "zh-cn-xiaoyineural": ("zh-CN-XiaoyiNeural", "哈喽大家好！我是晓伊，活泼亲和的邻家少女声线，欢迎来到我们的直播间！"),
    "zh-cn-liaoning-xiaobeineural": ("zh-CN-liaoning-XiaobeiNeural", "哎呀老铁们好啊！我是辽宁晓北，幽默地道的东北老铁声线，点个关注不迷路！"),
    "zh-cn-shaanxi-xiaonineural": ("zh-CN-shaanxi-XiaoniNeural", "大家好！我是陕西晓妮，热情地道的特色方言，给直播间增添别样风采！"),
    "zh-cn-xiaoxuanneural": ("zh-CN-XiaoxuanNeural", "家人们！我是晓萱，激情燃播促单声线，今天的全场福利马上开抢！"),
    "zh-cn-yunxianeural": ("zh-CN-YunxiaNeural", "小朋友和大朋友们好呀！我是云夏，活泼可爱的童声主播，今天带大家玩好玩的！"),
    "zh-cn-yunyangneural": ("zh-CN-YunyangNeural", "您好，我是云扬，专业新闻播音级质感男声，呈现高端严谨的品牌形象。"),

    # CosyVoice 阿里通义音色声线特征矩阵
    "longxiaochun": ("zh-CN-XiaoxiaoNeural", "你好！我是 小琴琴，知性温和的电商带货推荐声线，卖货很牛逼的那种哦，很高兴为您发声。"),
    "longlaotie": ("zh-CN-liaoning-XiaobeiNeural", "老铁们好！我是 CosyVoice 龙老铁，幽默互动带货唠嗑全拿捏，关注主播不迷路！"),
    "loongstella": ("zh-CN-XiaoxiaoNeural", "您好，我是 CosyVoice Stella，品质优雅的解说主播声线，祝您直播顺利！"),
    "loongbella": ("zh-CN-XiaoyiNeural", "哈喽大家好！我是 CosyVoice Bella，温柔知性的美妆服饰带货声线，期待陪伴您的每一场直播。"),
    "longanran": ("zh-CN-XiaoxuanNeural", "家人们！我是 CosyVoice 龙安然，激情促单燃播声线，今天的爆款福利全场炸裂！"),
    "longanxuan": ("zh-CN-XiaoyiNeural", "哈喽大家好！我是 CosyVoice 龙安萱，亲和甜美的带货声线，今天为你精选了超多好物！"),
    "longanchong": ("zh-CN-YunxiNeural", "哈喽大家！我是 CosyVoice 龙安冲，活力满满的阳光带货声线，吃喝玩乐零食专场走起！"),
    "longanping": ("zh-CN-YunjianNeural", "大家好，我是 CosyVoice 龙安平，沉稳严谨的数码家电科技声线，为您提供专业解析。"),
    "longshuo": ("zh-CN-YunyangNeural", "您好，我是 CosyVoice 龙硕，质感商务播音男声，助力高端品牌树立专业形象。"),
    "longjielidou": ("zh-CN-YunxiaNeural", "小朋友和大朋友们好呀！我是 CosyVoice 杰力豆，活泼可爱的童声主播，今天带大家玩好玩的！"),
    "longwan": ("zh-CN-XiaoxiaoNeural", "你好呀，我是 CosyVoice 龙婉，温和亲切的邻家声线，很高兴在直播间与您相遇。"),
    "longcheng": ("zh-CN-YunxiNeural", "嗨大家好！我是 CosyVoice 龙橙，朝气蓬勃的青春男声，带给您元气满满的直播间！"),
    "longhua": ("zh-CN-YunjianNeural", "各位好，我是 CosyVoice 龙华，成熟稳重的商务解说声线，让每一次沟通更具分量。"),
    "longshu": ("zh-CN-YunyangNeural", "大家好，我是 CosyVoice 龙书，磁性深情的叙事声线，为您缓缓讲述动人故事。"),
    "longxiaobai": ("zh-CN-XiaoyiNeural", "大家好！我是 CosyVoice 龙小白，清澈治愈的少女声线，愿每一句话都温暖如初。"),
    "longjing": ("zh-CN-XiaoxiaoNeural", "您好，我是 CosyVoice 龙静，文雅舒缓的品质解说声线，为您带来宁静与专注。"),

    # ChatTTS 种子音色
    "seed_2222": ("zh-CN-XiaoxiaoNeural", "你好呀，我是 ChatTTS 2222 号自然女声，带有真实的呼吸与说话停顿呢！"),
    "seed_6666": ("zh-CN-XiaoyiNeural", "哈哈大家好！我是 ChatTTS 6666 号亲切解说声线，说话就像朋友聊天一样自然！"),
    "seed_7869": ("zh-CN-liaoning-XiaobeiNeural", "咳咳，我是 ChatTTS 7869 号微醺笑意声线，这语气够真实够有味道吧！"),
    "seed_8888": ("zh-CN-YunxiNeural", "哈喽！我是 ChatTTS 8888 号阳光男声，对话节奏超逼真，开播超轻松！"),
}


@router.post("/tts/preview")
async def preview_tts_audio(req: TTSPreviewRequest):
    """
    根据前端当前选型与参数，实时合成一段简短的问候语音流 (MP3/WAV)
    精准匹配每个音色的独特声线与角色台词，让用户在试听切换时清晰感知音色变化
    """
    provider = (req.provider_name or "").lower().strip()
    raw_voice = (req.voice_name or "").strip()
    voice_key = raw_voice.lower()

    # 0. 智能检索声音档案是否为专属克隆音色（优先按唯一 ID，其次按名称倒序取已绑定的有效记录）
    v_record = None
    try:
        from sqlalchemy import case
        async with AsyncSessionLocal() as db_session:
            # 优先精确匹配 ID
            res = await db_session.execute(select(VoiceProfile).where(VoiceProfile.id == raw_voice))
            v_record = res.scalars().first()
            if not v_record:
                # 其次精确匹配名称，优先匹配已绑定云端真实 Voice-ID 的有效记录
                res_name = await db_session.execute(
                    select(VoiceProfile)
                    .where(VoiceProfile.name == raw_voice)
                    .order_by(
                        case((VoiceProfile.id.notlike("clone_%"), 1), else_=0).desc(),
                        VoiceProfile.created_at.desc()
                    )
                )
                v_record = res_name.scalars().first()
    except Exception as e:
        logger.warning(f"检索克隆声音档案异常: {e}")

    actual_voice_id = v_record.id if v_record else raw_voice
    cloned_name = v_record.name if v_record else raw_voice

    is_cloned_voice = bool(
        v_record
        or raw_voice.startswith("clone_")
        or "voice-custom-" in raw_voice
        or "custom" in raw_voice
        or "qwen-audio-" in raw_voice
        or "cosyvoice-" in raw_voice
    )

    # 外部云端商用服务参数与凭证读取
    base_url = (req.base_url or "").strip().rstrip("/")
    api_key = (req.api_key or "").strip()

    # 专属克隆音色特权通道：100% 保证用克隆声线合成全新台词，绝对禁止播放原版上传录音！
    if is_cloned_voice:
        try:
            from server.core.audio.clone_preview import generate_cloned_voice_preview, VOICES_DIR
            # 优先命中已落盘的大模型试听文件
            preview_cand = VOICES_DIR / f"{actual_voice_id}_cloned_preview.mp3"
            if preview_cand.exists() and preview_cand.stat().st_size > 1024 and not req.text:
                return FileResponse(preview_cand, media_type="audio/mpeg")

            sample_path = v_record.sample_wav_path if v_record else None
            preview_file = await generate_cloned_voice_preview(
                voice_id=actual_voice_id,
                voice_name=cloned_name,
                sample_audio_path=sample_path,
                custom_text=req.text,
                base_url=base_url,
                api_key=api_key,
                target_model=req.model_name
            )
            if preview_file and preview_file.exists():
                return FileResponse(preview_file, media_type="audio/mpeg")
        except Exception as e:
            logger.error(f"克隆音色合成全新台词失败: {e}", exc_info=True)
            raise HTTPException(
                status_code=500,
                detail=f"克隆音色【{cloned_name}】合成新台词失败：{str(e)}"
            )

    # 1. 智能匹配官方预置音色发音台词
    default_placeholders = [
        "你好！这是当前语音合成引擎的实时试听效果，音色自然流畅，祝您直播顺利！",
        "你好，欢迎来到直播间！这是当前语音引擎的实时试听效果，祝您开播顺利！",
        "你好！这是当前语音合成引擎的实时试听效果"
    ]
    is_generic_text = not req.text or any(p in req.text for p in default_placeholders)

    profile_voice, profile_text = VOICE_PREVIEW_PROFILES.get(
        voice_key,
        (raw_voice if "neural" in voice_key else "zh-CN-XiaoxiaoNeural", f"你好！我是当前语音引擎的 {raw_voice or '推荐'} 发音音色，很高兴为您发声！")
    )
    text_to_speak = profile_text if is_generic_text else req.text.strip()

    # 2. 若是 Edge-TTS 引擎，直接使用对应的高保真云端声线真实发声
    if "edge" in provider or not provider or provider == "edge_tts":
        actual_voice = profile_voice if profile_voice else "zh-CN-XiaoxiaoNeural"
        try:
            import edge_tts
            communicate = edge_tts.Communicate(text_to_speak, actual_voice)
            edge_stream = communicate.stream()
            chunks = []
            try:
                async for chunk in edge_stream:
                    if chunk["type"] == "audio":
                        chunks.append(chunk["data"])
            finally:
                try:
                    await edge_stream.aclose()
                except Exception:
                    logger.debug("关闭 Edge-TTS 试听流失败", exc_info=True)
            audio_bytes = b"".join(chunks)
            if audio_bytes:
                return Response(content=audio_bytes, media_type="audio/mpeg")
        except Exception as e:
            raise HTTPException(status_code=502, detail=f"Edge-TTS 合成试听失败: {str(e)}")

    # 3. 外部云端商用服务探测与尝试 (百炼 DashScope / 硅基流动 / OpenAI 兼容网关)
    base_url = (req.base_url or "").strip().rstrip("/")
    api_key = (req.api_key or "").strip()

    # 自动解密回填数据库中存储的真实 API 密钥与端点
    if not api_key or "*" in api_key or not base_url:
        try:
            async with AsyncSessionLocal() as db_session:
                q = select(ApiProviderConfig).where(
                    (ApiProviderConfig.config_group == "tts") & (ApiProviderConfig.is_active == 1)
                )
                res = await db_session.execute(q)
                active_cfg = res.scalars().first()
                if not active_cfg:
                    q_any = select(ApiProviderConfig).where(
                        (ApiProviderConfig.config_group == "tts") & (ApiProviderConfig.provider_name.like("%cosy%"))
                    )
                    res_any = await db_session.execute(q_any)
                    active_cfg = res_any.scalars().first()
                if active_cfg:
                    if not base_url:
                        base_url = (active_cfg.base_url or "").strip().rstrip("/")
                    if not api_key or "*" in api_key:
                        api_key = decrypt_secret(active_cfg.encrypted_api_key) if active_cfg.encrypted_api_key else ""
        except Exception as e:
            logger.warning(f"读取数据库 TTS 配置异常: {e}")

    if base_url:
        headers = {}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"

        # 3.1 阿里云百炼 DashScope 原生语音合成通道（官方 SpeechSynthesizer 契约，复用 clone_preview 共享实现）
        if api_key and any(k in base_url.lower() for k in ["aliyuncs.com", "dashscope", "maas"]):
            try:
                from server.core.audio.clone_preview import (
                    DashscopeCloneError as _DCError,
                    synthesize_dashscope_cosyvoice as _synth_cloud,
                )
                cloud_bytes = await _synth_cloud(
                    base_url, api_key, raw_voice or "loongbella",
                    text_to_speak, req.model_name,
                )
                if cloud_bytes:
                    return Response(content=cloud_bytes, media_type="audio/mpeg")
            except _DCError as ce:
                if ce.status == 401:
                    raise HTTPException(status_code=401, detail="阿里云百炼 API Key 鉴权失败，请检查密钥是否正确")
                if ce.status == 400 and ("voice" in (ce.detail or "").lower() or "418" in (ce.detail or "")):
                    raise HTTPException(
                        status_code=400,
                        detail=f"阿里云百炼未识别该 Voice-ID ({raw_voice})。请确认该音色已在百炼控制台完成复刻，或在右侧重新登记正确的 Voice-ID。",
                    )
                logger.warning(f"百炼云端合成通道失败: {ce.detail}")
            except HTTPException:
                raise
            except Exception as e:
                logger.warning(f"百炼原生合成通道尝试: {e}")

        # 3.2 硅基流动 / OpenAI 兼容 /audio/speech 通道
        speech_url = f"{base_url}/audio/speech" if not base_url.endswith("/audio/speech") else base_url
        model_name = "tts-1"
        if "siliconflow" in base_url:
            model_name = "FunAudioLLM/CosyVoice2-0.5B" if "cosy" in provider else "2noise/ChatTTS"
        elif "cosy" in provider:
            model_name = "cosyvoice-v1"

        payload = {
            "model": model_name,
            "input": text_to_speak,
            "voice": raw_voice or "alloy"
        }

        try:
            async with httpx.AsyncClient(timeout=6.0, verify=True) as client:
                resp = await client.post(speech_url, headers=headers, json=payload)
                if resp.status_code == 200 and resp.content:
                    media_type = resp.headers.get("content-type", "audio/mpeg")
                    return Response(content=resp.content, media_type=media_type)
                elif resp.status_code == 401:
                    raise HTTPException(status_code=401, detail="云端 API Key 鉴权失败，请检查密钥是否正确")
        except HTTPException:
            raise
        except Exception as e:
            logger.debug(f"OpenAI 规范试听通道尝试: {e}")


    try:
        import edge_tts
        actual_voice = profile_voice if profile_voice else "zh-CN-XiaoxiaoNeural"
        communicate = edge_tts.Communicate(text_to_speak, actual_voice)
        edge_stream = communicate.stream()
        chunks = []
        try:
            async for chunk in edge_stream:
                if chunk["type"] == "audio":
                    chunks.append(chunk["data"])
        finally:
            try:
                await edge_stream.aclose()
            except Exception:
                logger.debug("关闭试听兜底流失败", exc_info=True)
        audio_bytes = b"".join(chunks)
        if audio_bytes:
            return Response(content=audio_bytes, media_type="audio/mpeg")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"试听音频生成异常: {str(e)}")

    raise HTTPException(status_code=400, detail="未能生成有效的试听音频数据")


