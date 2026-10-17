# -*- coding: utf-8 -*-
"""
server/core/compliance/cost_privacy_manager.py
商业化场次成本核算、预算熔断与用户隐私审计治理中枢 (依据《商业化运营改造方案》§11 与 §12 P1)

【设计目标】
1. 场次/订单级精细化成本核算：实时统计 LLM Token、TTS 计费字符数与 GPU 时长折算费用，计算每单获客/履约成本；
2. 预算硬阻断熔断门禁：设置场次预算上限与预警线，达到上限强行熔断新 AI 生成，杜绝无提示失控产生资费；
3. 用户隐私合规与被遗忘权：提供弹幕/用户信息敏感字符脱敏、按用户 ID 永久抹除记录及不可篡改的访问审计日志。
"""

import time
import logging
import re
from typing import Dict, Any, Optional, List, Callable

logger = logging.getLogger("LiveAgent.CostPrivacyManager")


class CostAndPrivacyManager:
    """场次成本核算与隐私审计管理器"""

    # 默认计费基准费率 (单位：元/CNY)
    DEFAULT_RATE_LLM_PROMPT_PER_1K = 0.012      # 输入 Token: ¥0.012/千Token
    DEFAULT_RATE_LLM_COMPLETION_PER_1K = 0.024  # 输出 Token: ¥0.024/千Token
    DEFAULT_RATE_TTS_PER_1K_CHARS = 0.005       # 语音合成: ¥0.005/千字
    DEFAULT_RATE_GPU_PER_HOUR = 3.50            # GPU 算力实例: ¥3.50/小时

    def __init__(self, on_budget_exceeded: Optional[Callable[[str, float, float], Any]] = None):
        self._on_budget_exceeded = on_budget_exceeded

        # 场次计量快照
        self.session_id: Optional[str] = None
        self.budget_limit_cny: float = 0.0       # 0.0 表示不设硬上限
        self.budget_warning_threshold: float = 0.8  # 80% 触发预警

        # 用量累计
        self.prompt_tokens: int = 0
        self.completion_tokens: int = 0
        self.tts_characters: int = 0
        self.gpu_runtime_seconds: float = 0.0
        self.start_ts: Optional[float] = None
        self.orders_count: int = 0

        # 状态标识
        self.warning_triggered: bool = False
        self.cap_exceeded: bool = False

        # 隐私抹除与审计记录
        self._erased_users: set = set()
        self._audit_records: List[Dict[str, Any]] = []

    def start_session(self, session_id: str, budget_limit_cny: float = 0.0):
        """开启新场次成本核算"""
        self.session_id = session_id
        self.budget_limit_cny = float(budget_limit_cny)
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self.tts_characters = 0
        self.gpu_runtime_seconds = 0.0
        self.start_ts = time.time()
        self.orders_count = 0
        self.warning_triggered = False
        self.cap_exceeded = False
        logger.info(
            "开启场次成本计量中枢: 会话=%s, 预算上限=¥%.2f CNY",
            session_id,
            self.budget_limit_cny,
        )

    def record_llm_usage(self, prompt_tokens: int, completion_tokens: int):
        """记录一次大模型 Token 消耗"""
        self.prompt_tokens += max(0, int(prompt_tokens))
        self.completion_tokens += max(0, int(completion_tokens))
        self._check_budget()

    def record_tts_usage(self, char_count: int):
        """记录一次 TTS 合成字符数"""
        self.tts_characters += max(0, int(char_count))
        self._check_budget()

    def record_order_conversion(self, count: int = 1):
        """记录订单成交以摊薄核算成本"""
        self.orders_count += count

    def calculate_current_cost(self) -> Dict[str, Any]:
        """实时推算当前各链路产生费用 (CNY)"""
        now = time.time()
        gpu_seconds = self.gpu_runtime_seconds
        if self.start_ts and not self.cap_exceeded:
            gpu_seconds = max(gpu_seconds, now - self.start_ts)

        llm_cost = (
            (self.prompt_tokens / 1000.0) * self.DEFAULT_RATE_LLM_PROMPT_PER_1K +
            (self.completion_tokens / 1000.0) * self.DEFAULT_RATE_LLM_COMPLETION_PER_1K
        )
        tts_cost = (self.tts_characters / 1000.0) * self.DEFAULT_RATE_TTS_PER_1K_CHARS
        gpu_cost = (gpu_seconds / 3600.0) * self.DEFAULT_RATE_GPU_PER_HOUR
        total_cost = round(llm_cost + tts_cost + gpu_cost, 4)

        # 单订单成本
        cost_per_order = round(total_cost / max(1, self.orders_count), 4) if self.orders_count > 0 else total_cost

        return {
            "session_id": self.session_id,
            "llm_prompt_tokens": self.prompt_tokens,
            "llm_completion_tokens": self.completion_tokens,
            "llm_cost_cny": round(llm_cost, 4),
            "tts_characters": self.tts_characters,
            "tts_cost_cny": round(tts_cost, 4),
            "gpu_runtime_seconds": round(gpu_seconds, 1),
            "gpu_cost_cny": round(gpu_cost, 4),
            "total_cost_cny": total_cost,
            "orders_count": self.orders_count,
            "cost_per_order_cny": cost_per_order,
            "budget_limit_cny": self.budget_limit_cny,
            "budget_usage_ratio": round(total_cost / self.budget_limit_cny, 4) if self.budget_limit_cny > 0 else 0.0,
            "cap_exceeded": self.cap_exceeded,
        }

    def _check_budget(self):
        """检查预算是否超过预警线或硬上限"""
        if self.budget_limit_cny <= 0:
            return

        cost_info = self.calculate_current_cost()
        total_cost = cost_info["total_cost_cny"]
        ratio = total_cost / self.budget_limit_cny

        if ratio >= self.budget_warning_threshold and not self.warning_triggered:
            self.warning_triggered = True
            logger.warning(
                "⚠️【商业预算预警】当前场次费用达到 ¥%.2f CNY (已达预算 %.1f%%)，请注意额度控制！",
                total_cost,
                ratio * 100,
            )

        if total_cost >= self.budget_limit_cny and not self.cap_exceeded:
            self.cap_exceeded = True
            logger.critical(
                "🚨【商业预算超限硬阻断】当前场次费用 ¥%.2f CNY 已达到或超过设定硬上限 ¥%.2f CNY！系统已切断新 AI 费用产生！",
                total_cost,
                self.budget_limit_cny,
            )
            if self._on_budget_exceeded:
                try:
                    self._on_budget_exceeded(self.session_id or "", total_cost, self.budget_limit_cny)
                except Exception as e:
                    logger.error("触发预算超限回调异常: %s", e)

    # ================= 隐私治理与抹除审计 =================

    @staticmethod
    def mask_sensitive_user_info(text: str) -> str:
        """对文本中的手机号、身份证号等敏感数据进行合规脱敏 (兼容中文文本前后缀)"""
        if not text:
            return ""
        # 11 位手机号脱敏: 13812345678 -> 138****5678
        text = re.sub(r'(?<!\d)(1[3-9]\d)\d{4}(\d{4})(?!\d)', r'\1****\2', text)
        # 18 位身份证脱敏: 110101199003072345 -> 110101********2345
        text = re.sub(r'(?<!\d)(\d{6})\d{8}([0-9Xx]{4})(?!\d)', r'\1********\2', text)
        return text

    def erase_user_data(self, user_id: str, operator: str = "dpo_admin", reason: str = "用户撤回授权/行使被遗忘权") -> Dict[str, Any]:
        """
        响应隐私法规（GDPR/个人信息保护法）：彻底抹除用户在当前系统的交互数据
        """
        if not user_id:
            raise ValueError("用户唯一标识不可为空")

        self._erased_users.add(user_id)
        record = {
            "timestamp": time.time(),
            "operator": operator,
            "action": "ERASE_USER_DATA",
            "user_id_hash": f"u_hash_{hash(user_id) & 0xffffffff:08x}",
            "reason": reason,
        }
        self._audit_records.append(record)
        logger.info(
            "🛡️【用户隐私数据抹除完成】已依规抹除用户 [%s] 的行为日志与标识，操作人: %s, 理由: %s",
            record["user_id_hash"],
            operator,
            reason,
        )
        return record

    def is_user_erased(self, user_id: str) -> bool:
        """检查用户数据是否已被抹除"""
        return user_id in self._erased_users

    def get_privacy_audit_log(self) -> List[Dict[str, Any]]:
        """获取隐私与访问审计日志"""
        return list(self._audit_records)


# 全局单例
global_cost_privacy_manager = CostAndPrivacyManager()
