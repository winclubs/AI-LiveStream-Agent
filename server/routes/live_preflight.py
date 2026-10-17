# -*- coding: utf-8 -*-
"""开播门禁体检：结构化预检报告入口与统一生产门禁。

由 server/routes/live.py 拆分而来（原 3863-4333 行）。
分项检查器已进一步拆至 server/routes/live_preflight_checks.py：
`_legacy_preflight_check` 只负责「读取上下文 → 并行网络探测 → 顺序汇总」，
每个检查项由一个检查器实现，条目顺序与对外返回结构保持不变。

本模块实现侧调用「缝」时一律写在函数体内，
以保住 server.routes.live 上的晚绑定语义（见设计文档 §5.2）。
"""
import asyncio
import logging
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from server.database.db import get_db
from server.routes.live_preflight_checks import (
    _pf,
    _pf_action_clips_check,
    _pf_asr_engine_check,
    _pf_asset_health_check,
    _pf_avatar_engine_check,
    _pf_gpu_dispatch_check,
    _pf_guardrail_check,
    _pf_hardware_match_check,
    _pf_lipsync_weight_check,
    _pf_llm_check,
    _pf_load_legacy_context,
    _pf_obs_check,
    _pf_tts_check,
)

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
async def _legacy_preflight_check(db: AsyncSession):
    """
    开播前真实检查：
    模式/角色/大模型/TTS/Avatar/OBS/商品或主题/硬件/违禁词库
    全部基于真实状态探测，检查未通过时给出可执行的修复指引
    """
    ctx = await _pf_load_legacy_context(db)
    mode = ctx["mode"]
    theme = ctx["theme"]
    role = ctx["role"]

    # ---- 网络探测并行执行 ----
    llm_check, tts_check = await asyncio.gather(
        _pf_llm_check(ctx["llm_cfg"]), _pf_tts_check(ctx["tts_cfg"]),
    )

    checks = []

    # 1. 直播模式
    from server.routes.settings import LIVE_MODES, normalize_live_mode
    mode_info = LIVE_MODES.get(normalize_live_mode(mode))
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
        if ctx["product_count"] > 0:
            checks.append(_pf("pass", "products", "商品库", f"{ctx['product_count']} 款商品就绪，可正常带货促单"))
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

    # 8-14. 硬件/护栏/算力调度/数字人引擎/切片资产/动作切片/唇形权重/ASR。
    #        顺序即控制台展示顺序，追加顺序不得调整；None 表示该项放弃检查。
    checks.append(await _pf_hardware_match_check(mode))
    checks.append(_pf_guardrail_check())
    checks.append(await _pf_gpu_dispatch_check())
    checks.append(_pf_avatar_engine_check())
    for optional_check in (
        await _pf_asset_health_check(db),
        await _pf_action_clips_check(),
        _pf_lipsync_weight_check(),
        _pf_asr_engine_check(),
    ):
        if optional_check is not None:
            checks.append(optional_check)

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


async def assert_production_gates(
    *, execution_mode: str, avatar_asset_dir: str, voice_id: Optional[str],
    platform: str, anchor_id: Optional[str],
) -> Dict[str, Any]:
    """统一生产开播门禁 + Kill Switch 校验。未通过则强制阻断。"""
    from server.core.compliance.kill_switch import global_kill_switch
    from server.core.operations.preflight import global_preflight_service

    preflight_report = await global_preflight_service.run_preflight(
        mode=execution_mode,
        avatar_asset_dir=avatar_asset_dir or None,
        voice_id=voice_id,
        force=True,
    )
    if not preflight_report.passed:
        logger.error(
            "统一生产开播门禁未通过，已强制阻断: mode=%s, reasons=%s",
            execution_mode, preflight_report.blocking_reasons,
        )
        raise HTTPException(
            status_code=400,
            detail={
                "code": "PREFLIGHT_BLOCKED",
                "message": f"当前环境未达到 {execution_mode} 档位生产准入标准，已强制阻断开播",
                "blocking_reasons": preflight_report.blocking_reasons,
                "report": preflight_report.to_dict(),
            },
        )

    if not global_kill_switch.is_feature_enabled("stream_push", platform=platform, anchor_id=anchor_id):
        logger.critical(
            "🚨【Kill Switch 熔断阻断开播】主播 %s 在平台 %s 的推流权限已被熔断切断！",
            anchor_id, platform,
        )
        raise HTTPException(
            status_code=403,
            detail={
                "code": "KILL_SWITCH_BLOCKED",
                "message": f"当前主播或平台 ({platform}) 已被 Kill Switch 熔断禁用开播与推流",
            },
        )
    return preflight_report
