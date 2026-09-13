"""
专家主播领域识别与离线话术动态化 (规划 §5.4 / ADR-09)
- 按角色 system_prompt + 角色名关键词推断所属专业领域
- LLM 离线兜底与冷场垫场话术按领域动态生成，杜绝"中医专家答劳动法"式张冠李戴
- 离线话术只提供领域适配的安全通用指引与免责声明，不输出具体专业结论
"""
from typing import Dict

# 领域注册表：顺序即优先级 (同分时取靠前领域)
EXPERT_DOMAINS = [
    {
        "key": "legal",
        "keywords": ["法律", "律师", "劳动", "合同", "维权", "仲裁", "法务", "普法", "消费维权", "劳动法", "侵权"],
        "topic_label": "法律实务与合规维权",
        "idle_hint": "分享一条实用的法律风险防范干货提醒",
        "offline_reply": (
            "关于您提到的这个问题，涉及具体个案事实与证据材料，为避免给出不严谨的判断，"
            "建议您整理好相关证据（合同、聊天记录、转账凭证等），拨打 12348 法律援助热线"
            "或前往当地法律援助中心咨询；也可携带材料通过私信预约，我结合知识库为您做针对性分析。"
        ),
    },
    {
        "key": "medical",
        "keywords": ["医", "健康", "养生", "中医", "药", "诊疗", "营养", "康复", "体质", "食疗"],
        "topic_label": "健康养生与科学健康管理",
        "idle_hint": "分享一条科学的日常健康管理小知识",
        "offline_reply": (
            "为对您的健康负责，线上不作任何诊断结论。建议您记录症状出现的时间与规律，"
            "尽早前往正规医疗机构面诊；用药请严格遵医嘱，切勿自行增减剂量或轻信偏方。"
        ),
    },
    {
        "key": "finance",
        "keywords": ["财", "理财", "金融", "投资", "税务", "财税", "会计", "防诈", "防诈骗", "保险", "基金", "股票"],
        "topic_label": "家庭财务规划与投资防诈知识",
        "idle_hint": "分享一条实用的理财风险防范提醒",
        "offline_reply": (
            "涉及具体投资决策请务必谨慎：任何承诺保本高收益的项目都要高度警惕。"
            "建议通过持牌正规金融机构办理业务，核实资质后再操作；如遇可疑情况，"
            "可向监管部门或反诈专线 96110 咨询核实。"
        ),
    },
    {
        "key": "education",
        "keywords": ["教育", "升学", "考研", "择校", "学业", "留学", "志愿", "高考"],
        "topic_label": "升学规划与学业发展路径",
        "idle_hint": "分享一条学业规划方面的通用建议",
        "offline_reply": (
            "升学与择校决策需要结合孩子的成绩、兴趣与家庭情况综合判断。"
            "建议您整理成绩单与目标院校近三年数据，通过学校招生办或教育考试院官网核实最新政策，"
            "再提供完整材料由我帮您细化分析。"
        ),
    },
    {
        "key": "psychology",
        "keywords": ["心理", "情感咨询", "情绪", "减压", "焦虑", "婚姻家庭咨询"],
        "topic_label": "情绪管理与心理健康常识",
        "idle_hint": "分享一条日常情绪调节的小方法",
        "offline_reply": (
            "如果您最近感到压力较大，先试着保持规律作息，和信任的人聊聊感受。"
            "本交流不构成心理诊疗；若困扰持续两周以上，建议前往正规心理咨询机构或医院心理科寻求专业帮助。"
        ),
    },
]

GENERAL_DOMAIN = {
    "key": "general",
    "topic_label": "本专业领域的常见问题",
    "idle_hint": "分享一条本领域的实用干货提醒",
    "offline_reply": (
        "关于您提到的这个问题，涉及具体个案细节与事实材料，为避免给出不严谨的判断，"
        "建议您稍后通过私信或线下渠道提供完整材料，我会结合知识库为您做针对性分析。"
    ),
}

DISCLAIMER = "特别声明：本回答仅供交流参考，非正式依据。"


def infer_expert_domain(system_prompt: str) -> Dict:
    """按提示词与角色名关键词推断专家领域 (无命中回退 general)"""
    text = system_prompt or ""
    best = GENERAL_DOMAIN
    best_score = 0
    for domain in EXPERT_DOMAINS:
        score = sum(1 for kw in domain["keywords"] if kw in text)
        if score > best_score:
            best, best_score = domain, score
    return best


def build_expert_offline_reply(domain: Dict) -> str:
    """生成领域适配的离线安全兜底话术 (含免责声明，不作具体专业结论)"""
    return f"{DISCLAIMER}{domain['offline_reply']}"


def build_expert_idle_hint(domain: Dict) -> str:
    """生成领域适配的冷场垫场提示"""
    return f"{domain['idle_hint']}"
