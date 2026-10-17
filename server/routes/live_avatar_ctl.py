# -*- coding: utf-8 -*-
"""数字人实时控制：发声状态查询、极速打断清队列、动作切片切换。

由 server/routes/live.py 拆分而来。
本模块不引用任何「缝」，因此无需函数体内 import。
"""
from typing import Optional

from fastapi import APIRouter
from pydantic import BaseModel, Field

router = APIRouter()


# ------------------------------------------------------------------
# 数字人核心状态查询、极速打断 (flush_talk) 与电商动作切换端点
# ------------------------------------------------------------------
class AvatarActionRequest(BaseModel):
    action_code: int = Field(..., ge=0, le=10, description="动作切片代码: 0=待机, 1=欢迎, 2=点赞关注, 3=促单逼单指购物车, 4=致谢")
    duration: Optional[float] = Field(None, ge=0.0, le=60.0, description="动作持续时间(秒)，超时自动平滑衰减回待机态0")
    priority: Optional[int] = Field(None, ge=0, le=10, description="动作优先级(0-10)")


@router.get("/is-speaking", summary="查询数字人毫秒级实时发声状态")
async def check_is_speaking():
    """供前端呼吸灯与动态波形毫秒级轮询：查询当前主播是否正在发声或播报"""
    from server.core.avatar import get_active_avatar_driver
    driver = get_active_avatar_driver()
    is_speaking = driver.is_speaking()
    status = driver.get_status()
    status["is_speaking"] = is_speaking
    return {
        "code": 0,
        "message": "success",
        "data": status
    }


@router.post("/avatar/flush-talk", summary="瞬间清空数字人口型与音频队列 (极速打断)")
async def flush_avatar_talk():
    """调用数字人驱动瞬间打断接口，清空待播音频队列并恢复待机帧"""
    from server.core.avatar import get_active_avatar_driver
    driver = get_active_avatar_driver()
    await driver.flush_talk()
    return {
        "code": 0,
        "message": "数字人音频与口型队列已瞬间重置",
        "data": driver.get_status()
    }


@router.post("/avatar/action", summary="切换数字人动作切片状态")
async def set_avatar_action(req: AvatarActionRequest):
    """指令式驱动数字人肢体动作切片 (0:呼吸待机, 1:欢迎, 2:求关注, 3:促单指购物车, 4:致谢)"""
    from server.core.avatar import get_active_avatar_driver
    driver = get_active_avatar_driver()
    success = await driver.set_custom_state(
        req.action_code,
        duration=req.duration,
        priority=req.priority,
        source="api"
    )
    return {
        "code": 0 if success else 1,
        "message": f"数字人动作已切换至代码: {req.action_code}",
        "data": driver.get_status()
    }
