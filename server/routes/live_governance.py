# -*- coding: utf-8 -*-
"""运营治理：媒体质量 SLI/Runbook、灰度与 Kill Switch、成本核算、隐私抹除。

由 server/routes/live.py 拆分而来。
本模块不引用任何「缝」，因此无需函数体内 import。
"""
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

router = APIRouter()


# ============================================================================
# 🛡️ P1 商业化质量看门狗、Kill Switch 熔断与成本隐私治理接口 (依据《商业化方案》§12)
# ============================================================================

class KillSwitchUpdateRequest(BaseModel):
    feature: str
    enabled: bool
    platform: Optional[str] = None
    anchor_id: Optional[str] = None
    operator: Optional[str] = "admin"
    reason: Optional[str] = ""

class EmergencyCutRequest(BaseModel):
    platform: Optional[str] = None
    anchor_id: Optional[str] = None
    operator: Optional[str] = "admin"
    reason: Optional[str] = "紧急风控熔断"

class BudgetSetRequest(BaseModel):
    budget_limit_cny: float

class PrivacyEraseRequest(BaseModel):
    user_id: str
    operator: Optional[str] = "dpo_admin"
    reason: Optional[str] = "用户行使被遗忘权"


@router.get("/quality/metrics", summary="获取媒体质量 SLI 指标与 SLO 评估快照")
async def get_media_quality_metrics():
    """返回有效 FPS、丢帧率、音画漂移 p95/p99、TTS 首包延时与违规诊断清单"""
    from server.core.monitoring.media_quality_watchdog import global_media_watchdog
    return {
        "code": 0,
        "data": global_media_watchdog.get_sli_snapshot(),
    }


@router.get("/quality/runbook", summary="获取媒体质量排障 Runbook 手册")
async def get_media_quality_runbook():
    """获取所有媒体质量指标对应的排障 Runbook 标准操作步骤"""
    from server.core.monitoring.media_quality_watchdog import MediaQualityWatchdog
    return {
        "code": 0,
        "data": MediaQualityWatchdog.RUNBOOKS,
    }


@router.get("/governance/kill-switch", summary="获取灰度 Feature Flag 与 Kill Switch 状态")
async def get_kill_switch_status():
    """获取全局、各平台与各主播的开关矩阵及最近熔断审计记录"""
    from server.core.compliance.kill_switch import global_kill_switch
    return {
        "code": 0,
        "data": global_kill_switch.get_switches_status(),
    }


@router.post("/governance/kill-switch", summary="动态更新 Feature Flag 开关")
async def update_kill_switch(req: KillSwitchUpdateRequest):
    """更新指定主播、平台或全局功能开关 (如禁用带货促单或推流)"""
    from server.core.compliance.kill_switch import global_kill_switch
    try:
        record = global_kill_switch.set_switch(
            feature=req.feature,
            enabled=req.enabled,
            platform=req.platform,
            anchor_id=req.anchor_id,
            operator=req.operator or "admin",
            reason=req.reason or "",
        )
        return {"code": 0, "message": "开关更新成功", "data": record}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/governance/kill-switch/emergency-cut", summary="一键紧急熔断切断 (Kill Switch)")
async def emergency_kill_switch(req: EmergencyCutRequest):
    """在发生平台风控、舆情或严重违规时秒级熔断指定范围或全局所有功能"""
    from server.core.compliance.kill_switch import global_kill_switch
    record = global_kill_switch.emergency_kill_all(
        platform=req.platform,
        anchor_id=req.anchor_id,
        operator=req.operator or "admin",
        reason=req.reason or "紧急风控熔断",
    )
    return {"code": 0, "message": "紧急熔断已执行，已切断全部功能", "data": record}


@router.get("/cost/summary", summary="获取当前场次成本核算与用量快照")
async def get_session_cost_summary():
    """获取 LLM Token、TTS 字符数、GPU 时长产生费用与每单履约成本"""
    from server.core.compliance.cost_privacy_manager import global_cost_privacy_manager
    return {
        "code": 0,
        "data": global_cost_privacy_manager.calculate_current_cost(),
    }


@router.post("/cost/budget", summary="设置当前场次预算硬上限")
async def set_session_budget(req: BudgetSetRequest):
    """设置场次预算硬上限，达到上限后将自动触发 AI 暂停熔断，防止失控计费"""
    from server.core.compliance.cost_privacy_manager import global_cost_privacy_manager
    global_cost_privacy_manager.budget_limit_cny = max(0.0, float(req.budget_limit_cny))
    global_cost_privacy_manager.warning_triggered = False
    global_cost_privacy_manager.cap_exceeded = False
    return {
        "code": 0,
        "message": f"场次预算硬上限已更新为 ¥{global_cost_privacy_manager.budget_limit_cny:.2f} CNY",
        "data": global_cost_privacy_manager.calculate_current_cost(),
    }


@router.post("/privacy/erase-user", summary="响应用户被遗忘权并抹除用户隐私数据")
async def erase_user_privacy_data(req: PrivacyEraseRequest):
    """彻底抹除用户行为日志与标识，并留存不可篡改的数据抹除审计记录"""
    from server.core.compliance.cost_privacy_manager import global_cost_privacy_manager
    try:
        record = global_cost_privacy_manager.erase_user_data(
            user_id=req.user_id,
            operator=req.operator or "dpo_admin",
            reason=req.reason or "用户行使被遗忘权",
        )
        return {"code": 0, "message": "用户隐私数据已成功抹除", "data": record}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
