# -*- coding: utf-8 -*-
"""弹幕接入与场控：弹幕 Webhook、本地模拟注入、拉黑、平台凭证探测。

由 server/routes/live.py 拆分而来。
本模块实现侧调用「缝」时一律写在函数体内，
以保住 server.routes.live 上的晚绑定语义（见设计文档 §5.2）。
"""
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

router = APIRouter()


class DanmakuWebhookPayload(BaseModel):
    platform: Optional[str] = "douyin"
    user_name: str
    text: Optional[str] = ""
    event_type: Optional[str] = "chat"  # chat / gift / follow
    gift_name: Optional[str] = None
    gift_count: Optional[int] = 1
    total_coin: Optional[int] = 0


@router.post("/danmaku-webhook")
async def receive_danmaku_webhook(payload: DanmakuWebhookPayload):
    """
    通用第三方弹幕 Webhook (支持抖音/快手/视频号弹幕姬、浏览器扩展、本地中继推送)
    """

    # 测试缝（late-binding seam）：必须函数体内解析，
    # patch('server.routes.live.<name>') 才生效（见 test_live_seam_late_binding.py）。
    from server.routes.live import global_live_controller

    if not global_live_controller.is_live:
        return {"code": 1, "message": "直播间尚未开播，弹幕已丢弃"}

    event_type = payload.event_type or "chat"
    danmaku_payload = {
        "platform": payload.platform or "external",
        "text": payload.text or "",
        "gift_name": payload.gift_name,
        "count": payload.gift_count or 1,
        "total_coin": payload.total_coin or 0
    }
    intent_keywords = ["多少钱", "怎么买", "发货", "优惠", "包邮", "领券", "链接", "库存", "正品", "拍了", "保修"]
    text_content = payload.text or ""
    if event_type == "gift":
        priority = 0 if (payload.total_coin or 0) >= 50000 else 1
    elif any(kw in text_content for kw in intent_keywords):
        priority = 1
    else:
        priority = 2
    res = await global_live_controller.ingest_event(
        event_type=event_type,
        user_name=payload.user_name,
        payload=danmaku_payload,
        priority=priority,
        source="webhook",
        is_mock=False,
    )
    return {"code": 0 if res.get("accepted") else 1, "message": "弹幕已接收并进入调度队列", "data": res}


class MockEventRequest(BaseModel):
    type: str = "chat"  # chat(促单提问 P1) / gift(大额打赏 P0 打断)


@router.post("/mock-event")
async def inject_mock_event(req: MockEventRequest):
    """向直播队列注入仿真观众事件 (演示/联调按钮的真实数据通路，强制标记 is_mock 排除真实指标统计)"""

    # 测试缝（late-binding seam）：必须函数体内解析，
    # patch('server.routes.live.<name>') 才生效（见 test_live_seam_late_binding.py）。
    from server.routes.live import global_live_controller

    if not global_live_controller.is_live:
        raise HTTPException(status_code=400, detail="直播间尚未开播，请先在直播大屏一键开播")

    if req.type == "gift":
        res = await global_live_controller.ingest_event(
            event_type="gift",
            user_name="仿真大哥888",
            payload={"gift_name": "超级大火箭", "count": 1, "total_coin": 100000},
            priority=0,
            source="mock",
            is_mock=True,
        )
        return {"code": 0, "message": "已注入仿真大额打赏 (P0 强打断)，AI 主播将立刻感谢", "data": res}
    else:
        res = await global_live_controller.ingest_event(
            event_type="chat",
            user_name="仿真买家小美",
            payload={"text": "主播，这款多少钱？现在还有优惠吗？"},
            priority=1,
            source="mock",
            is_mock=True,
        )
        return {"code": 0, "message": "已注入仿真促单提问 (P1)，AI 主播将优先解答", "data": res}


class BlockDanmakuRequest(BaseModel):
    user_name: str
    keyword: str = ""
    reason: str = "违规弹幕人工拉黑"


@router.post("/danmaku/block")
async def block_danmaku_user(req: BlockDanmakuRequest):
    """一键拉黑违规弹幕发送者或关键词并写入违禁词库 (方案 4.1)"""
    from server.database.db import AsyncSessionLocal
    from server.database.models import ProhibitedWord
    import uuid

    added = []
    async with AsyncSessionLocal() as session:
        if req.keyword.strip():
            kw = req.keyword.strip()
            word_obj = ProhibitedWord(
                id=f"blk_kw_{uuid.uuid4().hex[:8]}",
                word=kw,
                action_policy="drop",
                category="sensitive",
                role_scope="all",
                platform="all",
            )
            session.add(word_obj)
            added.append(kw)

        if req.user_name.strip():
            u_kw = req.user_name.strip()
            user_word = ProhibitedWord(
                id=f"blk_u_{uuid.uuid4().hex[:8]}",
                word=u_kw,
                action_policy="drop",
                category="sensitive",
                role_scope="all",
                platform="all",
            )
            session.add(user_word)
            added.append(u_kw)

        await session.commit()

    return {
        "code": 0,
        "message": f"已成功拉黑违规目标: {', '.join(added)}",
        "data": {"user_name": req.user_name, "keyword": req.keyword, "blocked_rules": added},
    }


# ------------------------------------------------------------------
# 弹幕凭证健康度与连通性探针端点 (P2-2)
# ------------------------------------------------------------------
class DanmakuProbeRequest(BaseModel):
    platform: str = Field("bilibili", description="直播平台 (bilibili, douyin, kuaishou, mock)")
    room_id: str = Field("", description="房间号或链接")
    ttwid: Optional[str] = Field("", description="抖音 ttwid")
    ms_token: Optional[str] = Field("", description="抖音 msToken")
    cookie: Optional[str] = Field("", description="平台 Cookie")


@router.post("/danmaku/probe", summary="开播前弹幕平台与凭证连通性探测")
async def probe_danmaku_endpoint(req: DanmakuProbeRequest):
    """
    无干扰轻量探针：探测目标平台连通性、凭证有效性与环境库完整度
    """
    from server.adapters.danmaku.probe import probe_danmaku_platform
    result = await probe_danmaku_platform(
        platform=req.platform,
        room_id=req.room_id,
        ttwid=req.ttwid or "",
        ms_token=req.ms_token or "",
        cookie=req.cookie or "",
    )
    return {"code": 0, "data": result}
