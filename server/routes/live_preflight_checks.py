# -*- coding: utf-8 -*-
"""开播门禁体检的分项检查器。

由 server/routes/live_preflight.py 的 `_legacy_preflight_check` 拆分而来：
原函数 339 行超预算，逐项抽成「一个关注点一个检查器」，检查条目顺序与返回结构不变。

每个检查器只负责一件事，返回自己的检查条目（可放弃时返回 None），由
`_legacy_preflight_check` 统一汇总。本模块实现侧调用「缝」时一律写在函数体内，
以保住 server.routes.live 上的晚绑定语义（见 test_live_seam_late_binding.py）。
"""
import asyncio
import logging
import os
import uuid
from typing import Any, Dict, Optional

from sqlalchemy import select

from server.core.guardrails.aho_corasick import global_guardrail

logger = logging.getLogger("LiveAgent.LiveController")


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


async def _pf_hardware_match_check(mode: str) -> dict:
    """硬件与所选模式匹配度：GPU 探测在子线程执行，绝不阻塞事件循环。"""
    from server.routes.live import _probe_gpu, _recommend_tier
    from server.routes.settings import normalize_live_mode

    gpu = await asyncio.to_thread(_probe_gpu)
    rec_tier = _recommend_tier(gpu)
    rec_code = "local" if "纯本地硬件" in rec_tier else "hybrid"
    rec_name = "纯本地硬件" if rec_code == "local" else "本地云端混合"
    curr_mode = normalize_live_mode(mode)
    curr_name = "纯本地硬件" if curr_mode == "local" else "本地云端混合"

    if curr_mode == "local" and rec_code == "hybrid":
        return _pf("warn", "hardware", "硬件与模式匹配",
                   "检测到本地显卡算力有限，当前已选【纯本地硬件】可能导致渲染卡顿，建议切换为【本地云端混合】模式",
                   "可前往【开播向导】或【运行模式】切换为【本地云端混合】", "wizard")
    gpu_name = gpu.get("gpu_name") or "未检测到独立显卡"
    return _pf("pass", "hardware", "硬件与模式匹配",
               f"{gpu_name} · 推荐【{rec_name}】/ 当前已选【{curr_name}】，匹配无冲突")


def _pf_guardrail_check() -> dict:
    """违禁词合规护栏就绪度。"""
    guardrail_count = len(global_guardrail.word_meta) if global_guardrail.is_ready else 0
    if guardrail_count > 0:
        return _pf("pass", "guardrails", "违禁词护栏", f"已加载 {guardrail_count} 条违禁词实时拦截")
    return _pf("warn", "guardrails", "违禁词护栏",
               "违禁词库为空，直播缺少合规护栏",
               "前往【违禁词汇】添加 (如广告法极限词)", "guardrails")


async def _pf_gpu_dispatch_check() -> dict:
    """高性能显卡与云端算力智能调度研判 (按用户战略规则优先云端调度)。"""
    from server.core.hardware.gpu_capability import evaluate_compute

    cap_plan = await evaluate_compute(feature_name="数字人高保真口型渲染", required_vram_gb=2.0)
    if cap_plan.use_cloud:
        cloud_pname = cap_plan.cloud_gpu.get("provider_name", "Sidecar") if cap_plan.cloud_gpu else "Sidecar"
        return _pf(
            "pass", "gpu_cloud_dispatch", "高性能显卡调度",
            f"已优先调度已配置的云端显卡算力节点 [{cloud_pname}]，本地轻量低负载运行",
        )
    if cap_plan.is_low_spec_local:
        return _pf(
            "warn", "gpu_cloud_dispatch", "高性能显卡调度",
            f"本地显卡仅 {cap_plan.local_gpu.get('vram_total_gb', 0)}GB 显存且未对接云端显卡。高清深度神经渲染不可用，已自动选用轻量免显卡 CPU 模式",
            "如需高清写实数字人，请前往【GPU配置(2)】添加云端显卡 (Sidecar) 节点", "settings_gpu",
        )
    return _pf(
        "pass", "gpu_cloud_dispatch", "高性能显卡调度",
        f"本地显卡（{cap_plan.local_gpu.get('gpu_name')}，显存 {cap_plan.local_gpu.get('vram_total_gb')}GB）显存充足，支持高性能运行",
    )


def _pf_avatar_engine_check() -> dict:
    """数字人渲染引擎就绪度 (恪守 ADR-16 架构诚实)。

    注意：程序化"微动态"引擎 (RealAvatarLite) 已被移出渲染链路 —— 其 reload_source()
    仍在调用但产出的帧从不上屏。唇形硬性规约要求真实神经推理，引擎未就绪时
    `ProceduralAvatarDriver.start()` 直接 raise 拒绝启动。因此这里**不得**再以
    RealAvatarLite 为由宣称"就绪开播 / 零显存稳定开播"，否则用户点开播必然失败。
    """
    from server.core.avatar.neural_model_manager import global_neural_model_manager

    model_status = global_neural_model_manager.get_model_status_matrix()
    if model_status.get("has_neural_model"):
        return _pf(
            "pass", "avatar_engine", "自包含数字人渲染引擎",
            "真实神经唇形引擎已就绪 (NeuralLipRenderer)：唇形由 ONNX 深度推理实时重绘，"
            "口型由音频特征驱动，不存在程序化假唇形回退",
        )
    return _pf(
        "fail", "avatar_engine", "自包含数字人渲染引擎",
        "未检测到神经唇形引擎权重，开播必须具备真实神经推理能力，当前无法开播；"
        "系统已禁用程序化微动态回退（其产出帧不上屏，冒充真实唇形属架构禁止项）",
        "请先完成 ONNX 神经唇形权重下载与当前主播的切片资产生成，随后重新体检",
        None,
    )


async def _pf_asset_health_check(db) -> Optional[dict]:
    """数字人切片资产健康度检查 (杜绝损坏切片与无人脸坏帧带病开播)。

    探测异常或未找到主播切片时返回 None（不产出检查条目）。
    """
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

        if not avatar_dir:
            return None

        a_health = check_asset_health(avatar_dir, allow_sampled=True)
        allow_unusable = os.getenv("LIVE_AGENT_ALLOW_UNUSABLE_ASSETS", "0").lower() in ("1", "true", "yes")
        if a_health.status == STATUS_UNUSABLE:
            if not allow_unusable:
                return _pf(
                    "fail", "asset_health", "主播切片资产健康度",
                    f"当前主播资产严重损坏：{a_health.reason}。切片中存在过多非人脸/损坏帧，送入神经重绘模型会导致剧烈抽搐与闪烁，已禁止开播",
                    "请在控制台重新切片生成，或配置环境变量 LIVE_AGENT_ALLOW_UNUSABLE_ASSETS=1 强制跳过", "anchors"
                )
            return _pf(
                "warn", "asset_health", "主播切片资产健康度",
                f"资产损坏：{a_health.reason}；已配置允许强制使用，画面可能偶发严重畸变",
                "建议重新切片生成高质量底模", "anchors"
            )
        if a_health.status == STATUS_DEGRADED:
            return _pf(
                "warn", "asset_health", "主播切片资产健康度",
                f"资产质量次优：{a_health.reason}，可能偶发微小跳变",
                "建议重新上传无遮挡清晰视频重新切片", "anchors"
            )
        if a_health.status == STATUS_OK:
            return _pf(
                "pass", "asset_health", "主播切片资产健康度",
                f"人脸切片健康度达标 (检出率 {a_health.detect_rate:.1%})，支持真人高保真重绘"
            )
        return None
    except Exception:
        logger.debug("预体检资产健康度检查异常", exc_info=True)
        return None


async def _pf_action_clips_check() -> dict:
    """核心带货动作切片完整性 (缺失会如实告警并给出一键补齐指引)。"""
    from server.core.avatar.action_state_machine import get_action_state_machine

    action_sm = get_action_state_machine()
    try:
        await action_sm.load_configs_from_db()
    except Exception:
        logger.debug("预体检加载动作切片配置失败，按内存默认槽位评估", exc_info=True)
    action_status = action_sm.get_status()
    missing_codes = action_status.get("missing_action_codes", [])
    if not missing_codes:
        return _pf(
            "pass", "action_clips", "带货动作切片",
            f"{action_status.get('loaded_clips_count', 0)}/{action_status.get('total_configured_actions', 0)} "
            f"组动作切片全部就绪 (待机/欢迎/点赞/促单/致谢)",
        )
    missing_names = "、".join(action_status.get("missing_action_names", [])) or "未命名"
    return _pf(
        "warn", "action_clips", "带货动作切片",
        f"缺少 {len(missing_codes)} 组核心动作切片：{missing_names}；触发该动作时将保持待机画面，影响促单节奏",
        "执行 `python scripts/generate_action_clip.py --all-missing` 一键补齐，或在控制台上传真人实拍切片", None,
    )


def _pf_lipsync_weight_check() -> Optional[dict]:
    """神经唇形权重就绪度 (缺失时自动后台下载，完成后热挂载真实推理)。"""
    try:
        from server.config import auto_download_enabled
        from server.core.avatar.lipsync_weight_downloader import get_lipsync_weight_downloader

        downloader = get_lipsync_weight_downloader()
        dl_status = downloader.get_status()
        if downloader.is_weight_ready():
            return _pf(
                "pass", "lipsync_weight", "神经唇形权重",
                "ONNX 神经唇形重绘权重已就绪，NeuralLipRenderer 将执行真实深度推理 (高保真口型)",
            )
        if dl_status.get("is_busy"):
            progress = dl_status.get("progress", 0)
            return _pf(
                "warn", "lipsync_weight", "神经唇形权重",
                f"后台下载进行中 ({progress}%)：{dl_status.get('message', '')}；下载完成前无法开播，请等待下载就绪",
                "下载全自动进行，无需操作；完成后即可正常开播，可稍后重新体检确认结果", None, True,
            )
        if dl_status.get("state") == "failed" and not auto_download_enabled():
            return _pf(
                "fail", "lipsync_weight", "神经唇形权重",
                f"神经唇形权重下载失败：{dl_status.get('message', '')}；开播必须具备神经唇形权重，当前无法开播",
                "网络恢复后重新体检将自动重试下载 (约45MB，ModelScope 源)", None,
            )
        # 命中自愈：体检发现缺失即自动触发后台下载，用户无需任何手动操作
        downloader.start_download(source="modelscope", task_id=f"dl_{uuid.uuid4().hex[:8]}")
        return _pf(
            "warn", "lipsync_weight", "神经唇形权重",
            "未检测到 ONNX 神经唇形权重 (onnx_lipsync.onnx)，已自动触发后台下载 (约45MB，ModelScope 源)；"
            "开播必须具备真实神经唇形权重，下载完成前无法开播",
            "下载在后台自动进行，无需操作；完成后即可正常开播，可稍后重新体检确认下载结果", None, True,
        )
    except Exception:
        logger.debug("预体检神经唇形权重检查异常", exc_info=True)
        return None


def _pf_asr_engine_check() -> Optional[dict]:
    """ASR 语音转写引擎 (faster-whisper 轻量默认；缺失时自动后台安装)。"""
    try:
        from server.config import auto_download_enabled
        from server.core.audio.asr_engine import get_asr_status, is_faster_whisper_installed
        from server.core.audio.asr_dependency_installer import get_asr_dependency_installer

        asr_status = get_asr_status()
        if asr_status.get("ready"):
            return _pf(
                "pass", "asr_engine", "语音转写 (ASR)",
                f"{asr_status.get('message')} (后端: {asr_status.get('backend')})",
            )
        if asr_status.get("backend") is None and is_faster_whisper_installed():
            # 依赖已装、引擎待首次懒加载：这是正常就绪状态，不算缺陷
            return _pf(
                "pass", "asr_engine", "语音转写 (ASR)",
                "faster-whisper 已安装，首次语音输入时自动懒加载轻量转写引擎；VAD 极速打断立即可用",
            )
        installer = get_asr_dependency_installer()
        inst_status = installer.get_status()
        if inst_status.get("is_busy"):
            progress = inst_status.get("progress", 0)
            return _pf(
                "warn", "asr_engine", "语音转写 (ASR)",
                f"正在自动安装 faster-whisper 轻量转写引擎 ({progress}%)：{inst_status.get('message', '')}；"
                "安装完成前语音转写不可用，打断功能不受影响",
                "安装在后台自动进行，无需操作；可稍后重新体检确认结果", None, True,
            )
        if inst_status.get("state") == "failed" and not auto_download_enabled():
            return _pf(
                "warn", "asr_engine", "语音转写 (ASR)",
                f"自动安装 faster-whisper 失败：{inst_status.get('message', '')}；语音转写暂不可用 (VAD 极速打断不受影响)",
                "网络恢复后重新体检将自动重试安装 (约30MB，无 torch 依赖)", None,
            )
        # 命中自愈：体检发现依赖缺失即自动触发后台 pip 安装
        installer.start_install()
        return _pf(
            "warn", "asr_engine", "语音转写 (ASR)",
            "检测到未安装 faster-whisper 轻量转写引擎，已自动触发后台安装 (约30MB，国内镜像源)；"
            "安装完成前语音转写暂不可用，打断功能不受影响",
            "安装在后台自动进行，无需操作；可稍后重新体检确认安装结果", None, True,
        )
    except Exception:
        logger.debug("预体检 ASR 引擎检查异常", exc_info=True)
        return None


async def _pf_load_legacy_context(db) -> Dict[str, Any]:
    """集中读取体检所需的 DB 状态 (AsyncSession 禁止并发查询)。

    返回 mode / theme / llm_cfg / tts_cfg / role / product_count。
    """
    from sqlalchemy import func
    from server.database.models import ApiProviderConfig
    from server.routes.settings import SETTING_KEY_LIVE_MODE, _get_setting
    from server.config import decrypt_secret
    # 测试缝（late-binding seam）：必须函数体内解析，
    # patch('server.routes.live.<name>') 才生效（见 test_live_seam_late_binding.py）。
    from server.routes.live import global_role_manager

    mode = await _get_setting(db, SETTING_KEY_LIVE_MODE)
    theme = await _get_setting(db, "live_theme") or ""

    async def load_provider(group: str) -> Optional[dict]:
        res = await db.execute(
            select(ApiProviderConfig).where(
                ApiProviderConfig.config_group == group, ApiProviderConfig.is_active == 1,
            )
        )
        row = res.scalars().first()
        if not row:
            return None
        return {
            "provider_name": row.provider_name,
            "base_url": row.base_url or "",
            "model_name": row.model_name or "",
            "api_key": decrypt_secret(str(row.encrypted_api_key)) if row.encrypted_api_key else "",
        }

    llm_cfg = await load_provider("llm")
    tts_cfg = await load_provider("tts")

    role = global_role_manager.get_active_role()

    product_count = 0
    if role.role_type == "ecommerce":
        from server.database.models import Product
        res = await db.execute(select(func.count()).select_from(Product).where(Product.is_active == 1))
        product_count = res.scalar() or 0

    return {
        "mode": mode,
        "theme": theme,
        "llm_cfg": llm_cfg,
        "tts_cfg": tts_cfg,
        "role": role,
        "product_count": product_count,
    }