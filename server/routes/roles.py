import uuid
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from typing import List, Optional
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, update
from server.database.db import get_db
from server.database.models import AnchorRole
from server.core.roles.role_manager import global_role_manager
from server.routes.ws_live import ws_manager

router = APIRouter(prefix="/roles", tags=["主播角色管理"])

class RoleSwitchRequest(BaseModel):
    role_id: str

class RoleUpsertRequest(BaseModel):
    id: Optional[str] = None
    role_type: str  # ecommerce / entertainment / expert
    role_name: str
    system_prompt: str
    speech_speed: float = 1.0
    pitch_shift: float = 0.0
    associated_guardrail_group: str = "general"

# ---------------------------------------------------------------------------
# 模式 × 角色 组合建议 (需求 2)：约束提示词模板 + 分模式配置指引
# ---------------------------------------------------------------------------
ROLE_SUGGESTIONS = {
    "ecommerce": {
        "label": "带货主播（促单逼单）",
        "default_theme": "爆款好物专场 · 限时直降与库存福利",
        "theme_placeholder": "如：爆款好物专场 · 限时直降手慢无 / 春季新品首发大促",
        "constraint_prompt": (
            "【话题约束】只允许讨论直播间在售商品的价格、规格、库存、发货与售后话题；"
            "观众提出与购物无关的话题（情感、政治、医疗等）时，必须热情委婉地拉回卖货主线，"
            "例如：'宝子这个问题咱们直播后单独聊，先说说这款宝贝，今天真的巨划算！'"
            "严禁脱离商品自由发挥，严禁承诺商品页面没有的售后条款。"
        ),
        "tips": ["开场多用'家人们/宝子们'亲昵称呼，营造抢购紧迫感", "报价必须与商品库实时库存/价格一致，严禁凭空编造优惠"]
    },
    "entertainment": {
        "label": "娱乐主播（逗梗陪伴）",
        "default_theme": "深夜情感树洞 · 聊聊你最近单曲循环的一首歌",
        "theme_placeholder": "如：深夜情感树洞 · 聊聊你最近单曲循环的一首歌 / 周末欢唱连麦",
        "constraint_prompt": (
            "【话题约束】可自由闲聊情感、日常、兴趣爱好等轻松话题，积极接梗玩梗；"
            "涉及政治、金融理财建议、医疗诊断等专业敏感领域时，必须委婉拒绝并转移话题，"
            "例如：'这个可不敢乱说呀，我们聊点开心的！对啦，刚才有宝子点歌想听吗？'"
            "遇到恶意攻击或低俗引流弹幕，一律不予回应。"
        ),
        "tips": ["保持高情绪价值，多用语气词与笑声", "冷场时主动发起脑筋急转弯/成语接龙等小游戏"]
    },
    "expert": {
        "label": "专业专家（咨询法理前置免责）",
        "default_theme": "法律热点剖析 · 劳动争议与维权必知法条解读",
        "theme_placeholder": "如：劳动争议与合同纠纷深度答疑 · 消费者权益保护普法专场",
        "constraint_prompt": (
            "【话题约束】只允许讨论'法律'专业领域内的话题（劳动争议、合同纠纷、消费维权等）；"
            "观众提出法律之外的任何话题（医疗用药、投资理财、娱乐八卦等）时，必须委婉拒绝，"
            "统一话术：'抱歉，这个问题超出了我的专业领域，为了对您负责，建议咨询相关领域的专业人士。'"
            "所有个案解答必须前置免责声明，严禁给出确定性结论。"
        ),
        "tips": ["回答必须基于知识库检索结果，检索不到就引导私信提供材料", "保持客观严谨，分点阐述，语速平稳"]
    },
    "chitchat": {
        "label": "闲聊扯淡（唠嗑搭子）",
        "default_theme": "轻松唠嗑茶话会 · 今天你遇到了什么开心的事",
        "theme_placeholder": "如：生活日常碎碎念 · 吐槽奇葩经历 / 聊聊各地家常美食",
        "constraint_prompt": (
            "【话题约束】生活琐事、趣闻八卦、美食天气、兴趣爱好等轻松话题均可畅聊，观众聊什么就顺着接什么；"
            "仅遇到政治敏感、违法违规、恶意攻击类弹幕时，立即用玩笑岔开话题不予回应，"
            "例如：'哎哎这个咱不聊哈，说回今天中午那碗面才是正经事！'"
            "保持唠嗑感，不输出任何专业知识断言。"
        ),
        "tips": ["像街坊唠家常一样自然，多用'哎你说''可不是嘛'口头禅", "冷场时主动抛话题：今天吃了啥/最近趣事/天气吐槽", "温度拉满，绝不冷场也绝不越界"]
    }
}

# 合法主播类型唯一来源：与 ROLE_CLASS_MAP（运行时行为类）、前端 ROLE_CARD_META 对齐；
# 非法值在 upsert 入口拒绝，避免入库后建议接口 404、运行时被静默回退为带货主播。
VALID_ROLE_TYPES = tuple(ROLE_SUGGESTIONS.keys())

MODE_TIPS = {
    "A": "当前为全本地离线模式：请在 API 配置中启用本地 Ollama（大脑）与本地 CosyVoice（声音），全程断网可播。",
    "B": "当前为主流端云混合模式：请在 API 配置中填入云端大模型 API Key，语音可选免费 Edge-TTS 或本地克隆音色。",
    "C": "当前为端云分离架构：请在 API 配置中启用远程 GPU 节点（云端 4090 渲染），本地仅跑调度中枢与知识库。",
    "D": "当前为轻量免显卡模式：请启用云端 API 与 Edge-TTS 免费语音，无需任何独立显卡。"
}


@router.get("/suggestions")
async def get_role_suggestions(role_type: str = "ecommerce", mode: Optional[str] = None):
    """需求 2：结合'直播模式 + 主播角色'输出建议与 AI 约束提示词模板"""
    base = ROLE_SUGGESTIONS.get(role_type)
    if not base:
        raise HTTPException(status_code=404, detail="未知角色类型")
    return {
        "code": 0,
        "data": {
            "role_type": role_type,
            "label": base["label"],
            "default_theme": base.get("default_theme", ""),
            "theme_placeholder": base.get("theme_placeholder", ""),
            "constraint_prompt": base["constraint_prompt"],
            "tips": base["tips"] + ([MODE_TIPS.get(mode)] if mode and MODE_TIPS.get(mode) else [])
        }
    }

@router.get("/list")
async def list_roles(db: AsyncSession = Depends(get_db)):
    """获取所有可用主播角色人设"""
    result = await db.execute(select(AnchorRole))
    roles = result.scalars().all()
    active_role = global_role_manager.get_active_role()

    return {
        "code": 0,
        "active_role_id": active_role.role_id,
        "data": [
            {
                "id": r.id,
                "name": r.role_name,
                "role_type": r.role_type,
                "system_prompt": r.system_prompt,
                "speech_speed": r.speech_speed,
                "pitch_shift": r.pitch_shift,
                "is_active": (r.id == active_role.role_id)
            }
            for r in roles
        ]
    }

@router.post("/switch")
async def switch_role(req: RoleSwitchRequest, db: AsyncSession = Depends(get_db)):
    """一键动态热切换主播角色 (需求 3：直播进行中绝对禁止切换)"""
    from server.routes.live import global_live_controller
    if global_live_controller.is_live:
        raise HTTPException(status_code=409, detail="直播进行中，禁止切换主播角色！如需更换主播请先停止直播。")

    # 优先查库以确保使用最新的数据库配置（如自定义 prompt 或语速）
    result = await db.execute(select(AnchorRole).where(AnchorRole.id == req.role_id))
    role_db = result.scalar_one_or_none()

    if role_db:
        await db.execute(update(AnchorRole).values(is_active=0))
        role_db.is_active = 1
        await db.commit()
        role_inst = global_role_manager.register_or_update_from_db(role_db, activate=True)
    elif req.role_id in global_role_manager._roles:
        # 运行态内置角色也必须映射到持久化记录。
        persisted = await db.get(AnchorRole, req.role_id)
        if not persisted:
            raise HTTPException(status_code=404, detail=f"未找到指定角色ID: {req.role_id}")
        await db.execute(update(AnchorRole).values(is_active=0))
        persisted.is_active = 1
        await db.commit()
        global_role_manager.switch_role(req.role_id)
        role_inst = global_role_manager.get_active_role()
    else:
        raise HTTPException(status_code=404, detail=f"未找到指定角色ID: {req.role_id}")

    # 发送 WebSocket 广播，实时通知所有大屏或推流端已切换主播
    await ws_manager.broadcast("ROLE_SWITCHED", {
        "role_id": role_inst.role_id,
        "role_name": role_inst.role_name,
        "role_type": role_inst.role_type
    })

    return {
        "code": 0,
        "message": f"主播角色已成功切换为: {role_inst.role_name}",
        "data": {
            "role_id": role_inst.role_id,
            "role_name": role_inst.role_name,
            "role_type": role_inst.role_type
        }
    }

@router.post("/upsert")
async def upsert_role(req: RoleUpsertRequest, db: AsyncSession = Depends(get_db)):
    """创建或更新主播人设配置"""
    if req.role_type not in VALID_ROLE_TYPES:
        raise HTTPException(
            status_code=422,
            detail=f"非法主播类型: {req.role_type}，合法取值为: {', '.join(VALID_ROLE_TYPES)}",
        )
    role_id = req.id or f"role_custom_{uuid.uuid4().hex[:8]}"
    result = await db.execute(select(AnchorRole).where(AnchorRole.id == role_id))
    role_db = result.scalar_one_or_none()

    if not role_db:
        role_db = AnchorRole(
            id=role_id,
            role_type=req.role_type,
            role_name=req.role_name,
            system_prompt=req.system_prompt,
            speech_speed=req.speech_speed,
            pitch_shift=req.pitch_shift,
            associated_guardrail_group=req.associated_guardrail_group
        )
        db.add(role_db)
    else:
        role_db.role_type = req.role_type
        role_db.role_name = req.role_name
        role_db.system_prompt = req.system_prompt
        role_db.speech_speed = req.speech_speed
        role_db.pitch_shift = req.pitch_shift
        role_db.associated_guardrail_group = req.associated_guardrail_group

    await db.commit()
    await db.refresh(role_db)

    # 同步配置到运行时注册表，但保存操作不得隐式切换当前角色。
    global_role_manager.register_or_update_from_db(role_db, activate=False)
    from server.routes.live import global_live_controller
    if global_live_controller.is_live:
        await global_live_controller.reload_runtime_config("roles")

    return {
        "code": 0,
        "message": f"主播角色【{role_db.role_name}】配置保存成功",
        "data": {
            "id": role_db.id,
            "name": role_db.role_name,
            "role_type": role_db.role_type
        }
    }
