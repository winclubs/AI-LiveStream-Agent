# -*- coding: utf-8 -*-
"""
LLM Token 会话预算熔断控制器与冷场本地话术兜底引擎 (LLMBudgetManager)
阶段二商业化加固：
1. 监控单场直播的 LLM Token 累计消耗与调用频次，防止 24h 无人值守冷场轮播导致 Token 账单失控；
2. 支持配置单场最大 Token 上限 (LIVE_AGENT_MAX_SESSION_TOKENS，默认 300,000)；
3. 超额或节能模式下，冷场轮播自动降级为“高质量本地商品带货话术模板库”，零成本、零延迟稳定开播；
4. 真实用户弹幕互动保持最高响应优先级。
"""
import logging
import os
import random
from typing import Any, Dict, List, Optional

logger = logging.getLogger("LiveAgent.LLMBudgetManager")

DEFAULT_MAX_SESSION_TOKENS = int(os.getenv("LIVE_AGENT_MAX_SESSION_TOKENS", "300000"))

# 高质量本地电商带货叫卖模板 (插值商品名称、专享价格、卖点等)
LOCAL_CAROUSEL_TEMPLATES = [
    "欢迎新进直播间的家人们！正在讲解的这号爆款【{title}】，今天直播间专享到手只要 {price} 元！喜欢的抓紧点右下角小黄车！",
    "家人们注意看，咱们家这款【{title}】，今天破价直发只要 {price} 元！{selling_point}，品质绝对信得过，手慢无！",
    "刚来的宝子不要走开！今天主推的【{title}】库存已经见底，只要 {price} 元拍下即发，早拍早发货！",
    "大家都在问的【{title}】，今天给大家争取到了绝密底价 {price} 元！{selling_point}，直接戳右下方小黄车下单抢购！",
    "新进来的朋友扣波关注！这号链接的【{title}】是咱们店铺镇店之宝，今天特惠价 {price} 元，错过今天再等一年！",
]

LOCAL_URGENCY_TEMPLATES = [
    "最后三分钟！这批【{title}】特惠名额马上截单，还剩最后几单抢完恢复原价，快去拍下！",
    "库存告急！后台只剩最后几件【{title}】，拍下付款才算抢到，手速慢了真没啦！",
    "倒计时一分钟！优惠券马上失效，全场现货直发，现在下单直接安排优先打包！",
]


class LLMBudgetManager:
    """LLM 消耗预算监控与冷场本地降级管理器"""

    def __init__(self, max_tokens: int = DEFAULT_MAX_SESSION_TOKENS):
        self.max_tokens = max_tokens
        self.session_id: str = "default"
        self.used_tokens: int = 0
        self.total_calls: int = 0
        self.is_tripped: bool = False
        # 节能模式：开启后冷场垫场 100% 走本地商品话术模板，零消耗
        self.eco_mode: bool = os.getenv("LIVE_AGENT_ECO_MODE", "true").lower() in ("1", "true", "yes")

    def reset_session(self, session_id: str):
        """开播时重置会话预算计数器"""
        self.session_id = session_id
        self.used_tokens = 0
        self.total_calls = 0
        self.is_tripped = False
        logger.info(f"LLM 预算管理器已为新场次 {session_id} 复位，上限: {self.max_tokens} Tokens")

    def record_usage(self, estimated_prompt_tokens: int, estimated_completion_tokens: int):
        """记录 Token 消耗并研判是否触发熔断"""
        consumed = estimated_prompt_tokens + estimated_completion_tokens
        self.used_tokens += consumed
        self.total_calls += 1

        if self.used_tokens >= self.max_tokens and not self.is_tripped:
            self.is_tripped = True
            logger.warning(
                f"🚨 直播场次 {self.session_id} LLM 累计消耗达到上限 ({self.used_tokens}/{self.max_tokens} Tokens)！"
                "已自动触发预算熔断保护：冷场与轮播将全部切换为本地话术模板，防止账单超标。"
            )

    def is_budget_exceeded(self) -> bool:
        """检查当前会话是否已超出预算熔断线"""
        return self.used_tokens >= self.max_tokens

    def should_use_local_filler(self) -> bool:
        """冷场轮播是否应当使用本地模板 (节能模式开启或已熔断时使用)"""
        return self.eco_mode or self.is_budget_exceeded()

    def generate_local_carousel_speech(
        self,
        product: Optional[Dict[str, Any]] = None,
        is_urgency: bool = False,
    ) -> str:
        """根据商品上下文通过本地高转化模板生成带货话术 (0 Token 消耗)"""
        if not product:
            if is_urgency:
                return "家人们手速要快！小黄车多款热销爆款库存告急，抢完立即恢复原价，看中直接拍！"
            return "欢迎新进直播间的朋友们！全场好物现货直发，右下角小黄车多款专享福利正在热抢，喜欢直接带回家！"

        title = str(product.get("title") or "精选好物")[:40]
        price = product.get("live_price") or product.get("price") or 99
        points = product.get("selling_points") or []
        sp_text = f"特点是{points[0]}" if points else "正品保障支持七天无理由"

        if is_urgency:
            tmpl = random.choice(LOCAL_URGENCY_TEMPLATES)
        else:
            tmpl = random.choice(LOCAL_CAROUSEL_TEMPLATES)

        return tmpl.format(title=title, price=price, selling_point=sp_text)

    def get_status(self) -> Dict[str, Any]:
        """获取当前 Token 预算遥测数据"""
        pct = round((self.used_tokens / max(1, self.max_tokens)) * 100, 1)
        return {
            "session_id": self.session_id,
            "used_tokens": self.used_tokens,
            "max_tokens": self.max_tokens,
            "usage_percent": min(100.0, pct),
            "total_calls": self.total_calls,
            "is_tripped": self.is_tripped,
            "eco_mode": self.eco_mode,
        }


_global_llm_budget_manager: Optional[LLMBudgetManager] = None


def get_llm_budget_manager() -> LLMBudgetManager:
    """获取全局 LLM 预算管理器单例"""
    global _global_llm_budget_manager
    if _global_llm_budget_manager is None:
        _global_llm_budget_manager = LLMBudgetManager()
    return _global_llm_budget_manager
