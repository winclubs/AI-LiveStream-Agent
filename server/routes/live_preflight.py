# -*- coding: utf-8 -*-
"""开播门禁体检：结构化预检报告与各分项检查器。

由 server/routes/live.py 拆分而来（原 3863-4333 行）。
`_legacy_preflight_check` 仍是 335 行超长函数，属阶段 S3 的拆分目标，
本次仅做位置迁移，不改其内部结构。

本模块实现侧调用「缝」时一律写在函数体内，
以保住 server.routes.live 上的晚绑定语义（见设计文档 §5.2）。
"""
import asyncio
import logging
import os
import uuid
from typing import Optional

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from server.core.guardrails.aho_corasick import global_guardrail
from server.database.db import get_db
from server.database.models import Product

router = APIRouter()
logger = logging.getLogger("LiveAgent.LiveController")


@router.get("/preflight")
async def get_preflight_status(
    mode: str = "PREVIEW",
    anchor_id: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
):
    """
    供前端控制台主动执行开播全栈门禁预检：
    返回结构化检查清单、各项目通过状态、严重等级与阻断原因，驱动开播按钮置灰与诊断展示。
    """
    # 默认无参数请求保持旧控制台契约；显式 PRODUCTION/PILOT 使用统一结构化门禁。
    # 这样可以平滑升级旧前端，同时不允许正式开播绕过统一门禁。
    if mode.upper() == "PREVIEW" and not anchor_id:
        legacy = await _legacy_preflight_check(db)
        legacy["data"]["mode"] = mode.upper()
        return legacy

    from server.core.operations.preflight import global_preflight_service
    avatar_asset_dir = None
    if anchor_id:
        from server.database.db import AsyncSessionLocal
        from server.database.models import Anchor
        async with AsyncSessionLocal() as session:
            anchor = await session.get(Anchor, anchor_id)
            if anchor and getattr(anchor, "avatar_asset_dir", None):
                avatar_asset_dir = str(anchor.avatar_asset_dir)
    report = await global_preflight_service.run_preflight(
        mode=mode,
        avatar_asset_dir=avatar_asset_dir,
        force=True,
    )
    return {"code": 0 if report.passed else 400, "data": report.to_dict()}


# ---------------------------------------------------------------------------
# 开播前检查 (Preflight)：真实探测各项开播条件，杜绝盲目引导 (v1.1.3)
# ---------------------------------------------------------------------------
OBS_INSTALL_PATHS = [
    r"C:\Program Files\obs-studio\bin\64bit\obs64.exe",
    r"C:\Program Files (x86)\obs-studio\bin\64bit\obs64.exe",
    r"C:\Program Files\obs-studio\bin\32bit\obs32.exe",
]

# 模式对本地硬件的需求强度 (local 纯本地硬件要求独显；hybrid 本地云端混合兼容轻量硬件)
MODE_DEMAND_RANK = {"local": 2, "hybrid": 1}


def _pf(status: str, key: str, title: str, message: str, fix_hint: str = "", action_tab: Optional[str] = None, auto_in_progress: bool = False) -> dict:
    return {
        "status": status,  # pass / warn / fail
        "key": key,
        "title": title,
        "message": message,
        "fix_hint": fix_hint,
        "action_tab": action_tab,  # 前端一键跳转处理的页签
        "auto_in_progress": auto_in_progress,  # 后台自愈进行中 (前端据此自动轮询刷新进度)
    }


async def _pf_ping(url: str, headers: Optional[dict] = None, timeout: float = 4.0) -> Optional[int]:
    """轻量连通性探测，返回 HTTP 状态码或 None(不可达)"""
    import httpx
    try:
        async with httpx.AsyncClient(timeout=timeout, verify=True) as client:
            resp = await client.get(url, headers=headers or {})
            return resp.status_code
    except Exception:
        return None


async def _pf_llm_check(cfg: Optional[dict]) -> dict:
    """大模型 (AI 大脑) 真实可用性检查"""
    if not cfg:
        return _pf("warn", "llm", "大模型 (AI 大脑)",
                   "尚未配置大模型，AI 将使用内置离线话术应答，互动效果有限",
                   "前往【LLM 配置(1)】启用大模型 (如 DeepSeek) 或本地 Ollama", "settings_llm")
    provider = (cfg.get("provider_name") or "").lower()
    base = (cfg.get("base_url") or "").rstrip("/")

    if "ollama" in provider:
        code = await _pf_ping(base + "/api/tags" if base else "http://127.0.0.1:11434/api/tags")
        if code == 200:
            return _pf("pass", "llm", "大模型 (AI 大脑)", f"本地 Ollama 服务连通正常 ({cfg.get('model_name')})")
        status = "不可达" if code is None else f"HTTP {code}"
        return _pf("fail", "llm", "大模型 (AI 大脑)",
                   f"已选择本地 Ollama，但服务检查失败 ({status})，AI 将降级为离线话术",
                   "请先启动 Ollama (ollama serve) 并拉取模型，或改用云端 API", "settings_llm")

    # 云端 OpenAI 兼容接口
    if not cfg.get("api_key"):
        return _pf("fail", "llm", "大模型 (AI 大脑)",
                   "已启用云端大模型但未填写 API Key，AI 将降级为离线话术",
                   "在【LLM 配置(1)】的大模型卡片填入 sk- 开头的密钥", "settings_llm")
    code = await _pf_ping(base + "/models", {"Authorization": f"Bearer {cfg['api_key']}"})
    if code == 200:
        return _pf("pass", "llm", "大模型 (AI 大脑)", f"大模型 API 连通正常 ({cfg.get('model_name')})")
    if code == 401:
        message = "API Key 无效或已过期 (HTTP 401)，AI 将降级为离线话术"
        hint = "请到服务商开放平台重新生成密钥并在【LLM 配置(1)】更新"
    elif code is None:
        message = "无法连通大模型服务 (超时或地址错误)，AI 将降级为离线话术"
        hint = "检查 Base URL 是否正确、本机网络/代理是否可用"
    else:
        message = f"大模型服务检查失败 (HTTP {code})，AI 将降级为离线话术"
        hint = "检查 Base URL、API 权限、账户额度和服务商状态"
    return _pf("fail", "llm", "大模型 (AI 大脑)", message, hint, "settings_llm")


async def _pf_tts_check(cfg: Optional[dict]) -> dict:
    """语音合成 (TTS) 真实可用性检查"""
    if not cfg:
        return _pf("warn", "tts", "语音合成 (TTS)",
                   "未选择语音服务，开播将自动使用微软免费语音 (Edge-TTS)",
                   "如需专属克隆音色，可在【TTS 语音(3)】启用", "settings_tts")
    provider = (cfg.get("provider_name") or "").lower()
    base = (cfg.get("base_url") or "").rstrip("/")
    api_key = cfg.get("api_key") or ""
    model_name = cfg.get("model_name") or "cosyvoice-v3.5-flash"

    if "edge" in provider:
        return _pf("pass", "tts", "语音合成 (TTS)", "微软免费语音 (Edge-TTS) 就绪，零配置零费用")

    # 阿里云百炼 CosyVoice 云端音色克隆通道 (包含 aliyuncs / dashscope / maas 或云端域名)
    is_bailian_cloud = any(k in base.lower() for k in ["aliyuncs.com", "dashscope", "maas"]) or "bailian" in provider
    if "cosyvoice" in provider or is_bailian_cloud:
        if is_bailian_cloud:
            if not api_key:
                return _pf("warn", "tts", "语音合成 (TTS)",
                           "阿里云百炼 CosyVoice 未配置 API Key，开播将自动回退微软免费语音",
                           "请前往【TTS 语音(3)】填入 DashScope API Key", "settings_tts")
            return _pf("pass", "tts", "语音合成 (TTS)", f"阿里云百炼 CosyVoice 云端音色克隆已就绪 ({model_name})")

        # 否则属于本地私有化部署的 CosyVoice 服务 (如 127.0.0.1:50000 / 9233 等)
        if not base:
            return _pf("warn", "tts", "语音合成 (TTS)",
                       "已启用本地 CosyVoice 但未填写服务地址，开播将自动回退微软免费语音",
                       "请前往【TTS 语音(3)】填入本地 CosyVoice 服务地址", "settings_tts")
        code = await _pf_ping(base + "/")
        if code is not None and 200 <= code < 300:
            return _pf("pass", "tts", "语音合成 (TTS)", "本地 CosyVoice 声音克隆服务已就绪")
        status = "不可达" if code is None else f"HTTP {code}"
        return _pf("warn", "tts", "语音合成 (TTS)",
                   f"本地 CosyVoice 服务检查失败 ({status})，开播将自动回退微软免费语音 (克隆音色不可用)",
                   "请启动本地 CosyVoice 推理服务，或在【TTS 语音(3)】改选微软免费语音", "settings_tts")

    return _pf("pass", "tts", "语音合成 (TTS)", f"语音服务已配置 ({cfg.get('provider_name')})")


def _pf_obs_check() -> dict:
    """OBS 推流软件检测 (SSOT 统一探测)：进程运行 > 已安装未运行 > 未安装"""
    from server.routes.system import find_obs_studio
    obs_info = find_obs_studio()
    if obs_info["is_running"]:
        return _pf("pass", "obs", "OBS 本机组件", "检测到 OBS 进程正在运行；是否已捕获本地音视频源仍需人工确认")
    if obs_info["is_installed"]:
        ver_str = f" (v{obs_info['version']})" if obs_info.get("version") else ""
        return _pf("warn", "obs", "OBS 推流软件",
                   f"检测到 OBS 已安装{ver_str}但尚未运行（本机仿真试播不受影响，正式对外推流前请启动 OBS）",
                   "请启动 OBS Studio 并将视频源设为『OBS 虚拟摄像头』", None)
    return _pf("warn", "obs", "OBS 推流软件",
               "未检测到 OBS（本机仿真试播不受影响，正式对外推流需要 OBS 抓取画面）",
               "免费下载安装 OBS Studio: obsproject.com", None)


async def _legacy_preflight_check(db: AsyncSession):
    """
    开播前真实检查：
    模式/角色/大模型/TTS/Avatar/OBS/商品或主题/硬件/违禁词库
    全部基于真实状态探测，检查未通过时给出可执行的修复指引
    """
    from sqlalchemy import func
    from server.database.models import ApiProviderConfig
    from server.routes.settings import SETTING_KEY_LIVE_MODE, LIVE_MODES, _get_setting
    from server.config import decrypt_secret

    # 测试缝（late-binding seam）：必须函数体内解析，
    # patch('server.routes.live.<name>') 才生效（见 test_live_seam_late_binding.py）。
    from server.routes.live import _probe_gpu, _recommend_tier, global_role_manager

    # ---- 集中读取 DB 状态 (AsyncSession 禁止并发查询) ----
    mode = await _get_setting(db, SETTING_KEY_LIVE_MODE)
    theme = await _get_setting(db, "live_theme") or ""

    llm_cfg = tts_cfg = None
    res = await db.execute(
        select(ApiProviderConfig).where(ApiProviderConfig.config_group == "llm", ApiProviderConfig.is_active == 1)
    )
    row = res.scalars().first()
    if row:
        llm_cfg = {
            "provider_name": row.provider_name,
            "base_url": row.base_url or "",
            "model_name": row.model_name or "",
            "api_key": decrypt_secret(str(row.encrypted_api_key)) if row.encrypted_api_key else "",
        }
    res = await db.execute(
        select(ApiProviderConfig).where(ApiProviderConfig.config_group == "tts", ApiProviderConfig.is_active == 1)
    )
    row = res.scalars().first()
    if row:
        tts_cfg = {
            "provider_name": row.provider_name,
            "base_url": row.base_url or "",
            "model_name": row.model_name or "",
            "api_key": decrypt_secret(str(row.encrypted_api_key)) if row.encrypted_api_key else "",
        }

    role = global_role_manager.get_active_role()

    product_count = 0
    if role.role_type == "ecommerce":
        res = await db.execute(select(func.count()).select_from(Product).where(Product.is_active == 1))
        product_count = res.scalar() or 0

    # ---- 网络探测并行执行 ----
    llm_check, tts_check = await asyncio.gather(_pf_llm_check(llm_cfg), _pf_tts_check(tts_cfg))

    checks = []

    # 1. 直播模式
    from server.routes.settings import normalize_live_mode
    norm_mode = normalize_live_mode(mode)
    mode_info = LIVE_MODES.get(norm_mode)
    if mode_info:
        checks.append(_pf("pass", "mode", "直播模式", f"{mode_info['name']} · {mode_info['cost']}"))
    else:
        checks.append(_pf("fail", "mode", "直播模式",
                          "尚未选择直播运行模式",
                          "在【开播向导】第一步选择模式 (纯本地硬件 或 本地云端混合)", "wizard"))

    # 2. 主播角色
    checks.append(_pf("pass", "role", "主播角色", f"当前主播：{role.role_name} ({role.role_type})"))

    # 3 & 4. 大模型与语音 (真实连通探测)
    checks.append(llm_check)
    checks.append(tts_check)

    # 5. OBS 仅探测本机组件，不代表平台已收流
    checks.append(_pf_obs_check())
    checks.append(_pf(
        "warn", "external_publish", "外部平台发布验收 (人工确认)",
        "系统负责提供高拟真音画源与智能互动，不托管平台伴侣开播按钮（合规防封要求）；平台是否有声有画需由人工最后确认",
        "启动本地源后，请在抖音伴侣/快手伴侣中选择【添加摄像头】或【窗口捕获】，确认音画正常后，人工在伴侣中点击【开始直播】", None,
    ))

    # 6. 带货主播必须有商品
    if role.role_type == "ecommerce":
        if product_count > 0:
            checks.append(_pf("pass", "products", "商品库", f"{product_count} 款商品就绪，可正常带货促单"))
        else:
            checks.append(_pf("fail", "products", "商品库",
                              "带货主播还没有上架任何商品，开播后将无货可卖",
                              "前往【商品管理】添加商品 (标题/价格/库存)", "products"))

    # 7. 娱乐/闲聊主播建议配置今日直播主题
    if role.role_type in ("entertainment", "chitchat"):
        if theme:
            checks.append(_pf("pass", "theme", "今日直播主题", f"主题：{theme}"))
        else:
            checks.append(_pf("warn", "theme", "今日直播主题",
                              "尚未设置今日直播主题，冷场时 AI 缺少话题锚点",
                              "在【开播向导】第四步填写今日主题", "wizard"))

    # 8. 硬件与所选模式匹配度
    gpu = await asyncio.to_thread(_probe_gpu)
    rec_tier = _recommend_tier(gpu)
    rec_code = "local" if "纯本地硬件" in rec_tier else "hybrid"
    rec_name = "纯本地硬件" if rec_code == "local" else "本地云端混合"
    curr_mode = normalize_live_mode(mode)
    curr_name = "纯本地硬件" if curr_mode == "local" else "本地云端混合"

    if curr_mode == "local" and rec_code == "hybrid":
        checks.append(_pf("warn", "hardware", "硬件与模式匹配",
                          "检测到本地显卡算力有限，当前已选【纯本地硬件】可能导致渲染卡顿，建议切换为【本地云端混合】模式",
                          "可前往【开播向导】或【运行模式】切换为【本地云端混合】", "wizard"))
    else:
        gpu_name = gpu.get("gpu_name") or "未检测到独立显卡"
        checks.append(_pf("pass", "hardware", "硬件与模式匹配",
                          f"{gpu_name} · 推荐【{rec_name}】/ 当前已选【{curr_name}】，匹配无冲突"))

    # 9. 违禁词合规护栏
    guardrail_count = len(global_guardrail.word_meta) if global_guardrail.is_ready else 0
    if guardrail_count > 0:
        checks.append(_pf("pass", "guardrails", "违禁词护栏", f"已加载 {guardrail_count} 条违禁词实时拦截"))
    else:
        checks.append(_pf("warn", "guardrails", "违禁词护栏",
                          "违禁词库为空，直播缺少合规护栏",
                          "前往【违禁词汇】添加 (如广告法极限词)", "guardrails"))

    # 10. 高性能显卡与云端算力智能调度研判 (按用户战略规则优先云端调度)
    from server.core.hardware.gpu_capability import evaluate_compute
    cap_plan = await evaluate_compute(feature_name="数字人高保真口型渲染", required_vram_gb=2.0)
    if cap_plan.use_cloud:
        cloud_pname = cap_plan.cloud_gpu.get("provider_name", "Sidecar") if cap_plan.cloud_gpu else "Sidecar"
        checks.append(_pf(
            "pass", "gpu_cloud_dispatch", "高性能显卡调度",
            f"已优先调度已配置的云端显卡算力节点 [{cloud_pname}]，本地轻量低负载运行",
        ))
    elif cap_plan.is_low_spec_local:
        checks.append(_pf(
            "warn", "gpu_cloud_dispatch", "高性能显卡调度",
            f"本地显卡仅 {cap_plan.local_gpu.get('vram_total_gb', 0)}GB 显存且未对接云端显卡。高清深度神经渲染不可用，已自动选用轻量免显卡 CPU 模式",
            "如需高清写实数字人，请前往【GPU配置(2)】添加云端显卡 (Sidecar) 节点", "settings_gpu",
        ))
    else:
        checks.append(_pf(
            "pass", "gpu_cloud_dispatch", "高性能显卡调度",
            f"本地显卡（{cap_plan.local_gpu.get('gpu_name')}，显存 {cap_plan.local_gpu.get('vram_total_gb')}GB）显存充足，支持高性能运行",
        ))

    # 11. 数字人渲染引擎就绪度 (恪守 ADR-16 架构诚实)
    #     注意：程序化"微动态"引擎 (RealAvatarLite) 已被移出渲染链路 —— 其 reload_source()
    #     仍在调用但产出的帧从不上屏。唇形硬性规约要求真实神经推理，引擎未就绪时
    #     `ProceduralAvatarDriver.start()` 直接 raise 拒绝启动。因此这里**不得**再以
    #     RealAvatarLite 为由宣称"就绪开播 / 零显存稳定开播"，否则用户点开播必然失败。
    from server.core.avatar.neural_model_manager import global_neural_model_manager
    model_status = global_neural_model_manager.get_model_status_matrix()
    if model_status.get("has_neural_model"):
        checks.append(_pf(
            "pass", "avatar_engine", "自包含数字人渲染引擎",
            "真实神经唇形引擎已就绪 (NeuralLipRenderer)：唇形由 ONNX 深度推理实时重绘，"
            "口型由音频特征驱动，不存在程序化假唇形回退",
        ))
    else:
        checks.append(_pf(
            "fail", "avatar_engine", "自包含数字人渲染引擎",
            "未检测到神经唇形引擎权重，开播必须具备真实神经推理能力，当前无法开播；"
            "系统已禁用程序化微动态回退（其产出帧不上屏，冒充真实唇形属架构禁止项）",
            "请先完成 ONNX 神经唇形权重下载与当前主播的切片资产生成，随后重新体检",
            None,
        ))

    # 12. 数字人切片资产健康度检查 (杜绝损坏切片与无人脸坏帧带病开播)
    try:
        from server.config import DATA_DIR
        from server.core.avatar.asset_health import check_asset_health, STATUS_OK, STATUS_DEGRADED, STATUS_UNUSABLE
        from server.routes.settings import _get_setting
        anchor_id = await _get_setting(db, "selected_anchor_id")
        avatar_dir = None
        if anchor_id:
            p = DATA_DIR / "avatars" / anchor_id
            if p.exists() and p.is_dir():
                avatar_dir = str(p)
        if not avatar_dir:
            avatars_root = DATA_DIR / "avatars"
            if avatars_root.exists():
                for sub in avatars_root.iterdir():
                    if sub.is_dir() and ((sub / "coords.pkl").exists() or (sub / "face_imgs").exists()):
                        avatar_dir = str(sub)
                        break

        if avatar_dir:
            a_health = check_asset_health(avatar_dir, allow_sampled=True)
            allow_unusable = os.getenv("LIVE_AGENT_ALLOW_UNUSABLE_ASSETS", "0").lower() in ("1", "true", "yes")
            if a_health.status == STATUS_UNUSABLE:
                if not allow_unusable:
                    checks.append(_pf(
                        "fail", "asset_health", "主播切片资产健康度",
                        f"当前主播资产严重损坏：{a_health.reason}。切片中存在过多非人脸/损坏帧，送入神经重绘模型会导致剧烈抽搐与闪烁，已禁止开播",
                        "请在控制台重新切片生成，或配置环境变量 LIVE_AGENT_ALLOW_UNUSABLE_ASSETS=1 强制跳过", "anchors"
                    ))
                else:
                    checks.append(_pf(
                        "warn", "asset_health", "主播切片资产健康度",
                        f"资产损坏：{a_health.reason}；已配置允许强制使用，画面可能偶发严重畸变",
                        "建议重新切片生成高质量底模", "anchors"
                    ))
            elif a_health.status == STATUS_DEGRADED:
                checks.append(_pf(
                    "warn", "asset_health", "主播切片资产健康度",
                    f"资产质量次优：{a_health.reason}，可能偶发微小跳变",
                    "建议重新上传无遮挡清晰视频重新切片", "anchors"
                ))
            elif a_health.status == STATUS_OK:
                checks.append(_pf(
                    "pass", "asset_health", "主播切片资产健康度",
                    f"人脸切片健康度达标 (检出率 {a_health.detect_rate:.1%})，支持真人高保真重绘"
                ))
    except Exception:
        logger.debug("预体检资产健康度检查异常", exc_info=True)

    # 13. 核心带货动作切片完整性 (缺失会如实告警并给出一键补齐指引)
    from server.core.avatar.action_state_machine import get_action_state_machine
    action_sm = get_action_state_machine()
    try:
        await action_sm.load_configs_from_db()
    except Exception:
        logger.debug("预体检加载动作切片配置失败，按内存默认槽位评估", exc_info=True)
    action_status = action_sm.get_status()
    missing_codes = action_status.get("missing_action_codes", [])
    if not missing_codes:
        checks.append(_pf(
            "pass", "action_clips", "带货动作切片",
            f"{action_status.get('loaded_clips_count', 0)}/{action_status.get('total_configured_actions', 0)} "
            f"组动作切片全部就绪 (待机/欢迎/点赞/促单/致谢)",
        ))
    else:
        missing_names = "、".join(action_status.get("missing_action_names", [])) or "未命名"
        checks.append(_pf(
            "warn", "action_clips", "带货动作切片",
            f"缺少 {len(missing_codes)} 组核心动作切片：{missing_names}；触发该动作时将保持待机画面，影响促单节奏",
            "执行 `python scripts/generate_action_clip.py --all-missing` 一键补齐，或在控制台上传真人实拍切片", None,
        ))

    # 13. 神经唇形权重就绪度 (缺失时自动后台下载，完成后热挂载真实推理)
    try:
        from server.config import auto_download_enabled
        from server.core.avatar.lipsync_weight_downloader import get_lipsync_weight_downloader
        downloader = get_lipsync_weight_downloader()
        dl_status = downloader.get_status()
        if downloader.is_weight_ready():
            checks.append(_pf(
                "pass", "lipsync_weight", "神经唇形权重",
                "ONNX 神经唇形重绘权重已就绪，NeuralLipRenderer 将执行真实深度推理 (高保真口型)",
            ))
        elif dl_status.get("is_busy"):
            progress = dl_status.get("progress", 0)
            checks.append(_pf(
                "warn", "lipsync_weight", "神经唇形权重",
                f"后台下载进行中 ({progress}%)：{dl_status.get('message', '')}；下载完成前无法开播，请等待下载就绪",
                "下载全自动进行，无需操作；完成后即可正常开播，可稍后重新体检确认结果", None, True,
            ))
        elif dl_status.get("state") == "failed" and not auto_download_enabled():
            checks.append(_pf(
                "fail", "lipsync_weight", "神经唇形权重",
                f"神经唇形权重下载失败：{dl_status.get('message', '')}；开播必须具备神经唇形权重，当前无法开播",
                "网络恢复后重新体检将自动重试下载 (约45MB，ModelScope 源)", None,
            ))
        else:
            # 命中自愈：体检发现缺失即自动触发后台下载，用户无需任何手动操作
            downloader.start_download(source="modelscope", task_id=f"dl_{uuid.uuid4().hex[:8]}")
            checks.append(_pf(
                "warn", "lipsync_weight", "神经唇形权重",
                "未检测到 ONNX 神经唇形权重 (onnx_lipsync.onnx)，已自动触发后台下载 (约45MB，ModelScope 源)；"
                "开播必须具备真实神经唇形权重，下载完成前无法开播",
                "下载在后台自动进行，无需操作；完成后即可正常开播，可稍后重新体检确认下载结果", None, True,
            ))
    except Exception:
        logger.debug("预体检神经唇形权重检查异常", exc_info=True)

    # 14. ASR 语音转写引擎 (faster-whisper 轻量默认；缺失时自动后台安装)
    try:
        from server.config import auto_download_enabled
        from server.core.audio.asr_engine import get_asr_status, is_faster_whisper_installed
        from server.core.audio.asr_dependency_installer import get_asr_dependency_installer
        asr_status = get_asr_status()
        if asr_status.get("ready"):
            checks.append(_pf(
                "pass", "asr_engine", "语音转写 (ASR)",
                f"{asr_status.get('message')} (后端: {asr_status.get('backend')})",
            ))
        elif asr_status.get("backend") is None and is_faster_whisper_installed():
            # 依赖已装、引擎待首次懒加载：这是正常就绪状态，不算缺陷
            checks.append(_pf(
                "pass", "asr_engine", "语音转写 (ASR)",
                "faster-whisper 已安装，首次语音输入时自动懒加载轻量转写引擎；VAD 极速打断立即可用",
            ))
        else:
            installer = get_asr_dependency_installer()
            inst_status = installer.get_status()
            if inst_status.get("is_busy"):
                progress = inst_status.get("progress", 0)
                checks.append(_pf(
                    "warn", "asr_engine", "语音转写 (ASR)",
                    f"正在自动安装 faster-whisper 轻量转写引擎 ({progress}%)：{inst_status.get('message', '')}；"
                    "安装完成前语音转写不可用，打断功能不受影响",
                    "安装在后台自动进行，无需操作；可稍后重新体检确认结果", None, True,
                ))
            elif inst_status.get("state") == "failed" and not auto_download_enabled():
                checks.append(_pf(
                    "warn", "asr_engine", "语音转写 (ASR)",
                    f"自动安装 faster-whisper 失败：{inst_status.get('message', '')}；语音转写暂不可用 (VAD 极速打断不受影响)",
                    "网络恢复后重新体检将自动重试安装 (约30MB，无 torch 依赖)", None,
                ))
            else:
                # 命中自愈：体检发现依赖缺失即自动触发后台 pip 安装
                installer.start_install()
                checks.append(_pf(
                    "warn", "asr_engine", "语音转写 (ASR)",
                    "检测到未安装 faster-whisper 轻量转写引擎，已自动触发后台安装 (约30MB，国内镜像源)；"
                    "安装完成前语音转写暂不可用，打断功能不受影响",
                    "安装在后台自动进行，无需操作；可稍后重新体检确认安装结果", None, True,
                ))
    except Exception:
        logger.debug("预体检 ASR 引擎检查异常", exc_info=True)

    fails = sum(1 for c in checks if c["status"] == "fail")
    warns = sum(1 for c in checks if c["status"] == "warn")
    passes = sum(1 for c in checks if c["status"] == "pass")

    return {
        "code": 0,
        "data": {
            "ready": fails == 0,
            "local_ready": fails == 0,
            "external_publish": {
                "status": "not_managed",
                "validation": "pending_external_acceptance",
            },
            "summary": f"{passes} 项通过 · {warns} 项建议/待外部验收 · {fails} 项未通过",
            "checks": checks
        }
    }
