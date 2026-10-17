# -*- coding: utf-8 -*-
"""
平台合规治理与 AI 生成内容披露服务 (依据《商业化运营改造方案》§6.4 与 §12)
支持全平台规则矩阵查询、AI 显著披露标识与商业资质核验。
"""
import logging
import os
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("LiveAgent.Compliance")

# 多平台直播数字人规则矩阵
PLATFORM_RULE_MATRIX: Dict[str, Dict[str, Any]] = {
    "douyin": {
        "name": "抖音",
        "requires_ai_disclosure": True,
        "allow_unattended": False,
        "max_unattended_minutes": 0,
        "requires_realtime_human_takeover": True,
        "restricted_categories": ["医疗保健", "金融理财", "母婴奶粉", "特定药品"],
        "license_required": ["portrait_authorization", "voice_authorization"],
    },
    "kuaishou": {
        "name": "快手",
        "requires_ai_disclosure": True,
        "allow_unattended": False,
        "max_unattended_minutes": 0,
        "requires_realtime_human_takeover": True,
        "restricted_categories": ["医疗器械", "高风险投资", "处方药"],
        "license_required": ["portrait_authorization"],
    },
    "wechat": {
        "name": "微信视频号",
        "requires_ai_disclosure": True,
        "allow_unattended": False,
        "max_unattended_minutes": 0,
        "requires_realtime_human_takeover": True,
        "restricted_categories": ["金融", "两性健康", "医美"],
        "license_required": ["portrait_authorization", "voice_authorization"],
    },
    "bilibili": {
        "name": "哔哩哔哩",
        "requires_ai_disclosure": True,
        "allow_unattended": True,
        "max_unattended_minutes": 120,
        "requires_realtime_human_takeover": True,
        "restricted_categories": ["高风险金融", "虚假广告"],
        "license_required": ["portrait_authorization"],
    },
    "taobao": {
        "name": "淘宝直播",
        "requires_ai_disclosure": True,
        "allow_unattended": False,
        "max_unattended_minutes": 0,
        "requires_realtime_human_takeover": True,
        "restricted_categories": ["违禁品", "医疗保健品"],
        "license_required": ["portrait_authorization", "store_authorization"],
    },
}


class ComplianceGovernanceService:
    """平台合规治理中心"""

    DEFAULT_DISCLOSURE_TEXT = "【AI合成声明】本直播间采用人工智能技术辅助生成数字人形象与声音，商品交易由官方人工团队真实履约保障。"

    @classmethod
    def get_platform_rules(cls, platform: str) -> Dict[str, Any]:
        """获取指定平台的合规规则矩阵"""
        key = (platform or "bilibili").lower().strip()
        return PLATFORM_RULE_MATRIX.get(key, {
            "name": platform,
            "requires_ai_disclosure": True,
            "allow_unattended": False,
            "requires_realtime_human_takeover": True,
            "restricted_categories": [],
            "license_required": ["portrait_authorization"],
        })

    @classmethod
    def get_disclosure_text(cls) -> str:
        """获取法规要求的 AI 显著标识文案"""
        return os.getenv("AI_DISCLOSURE_TEXT", "").strip() or cls.DEFAULT_DISCLOSURE_TEXT

    @classmethod
    def verify_live_compliance(
        cls,
        platform: str = "bilibili",
        anchor_info: Optional[Dict[str, Any]] = None,
        is_production: bool = False,
    ) -> Tuple[bool, List[str]]:
        """
        验证开播资质与合规凭据
        :return: (是否合规, 违规阻断原因列表)
        """
        reasons: List[str] = []
        rules = cls.get_platform_rules(platform)

        # 商业生产档位严格检查主播肖像与声音授权
        if is_production:
            if not anchor_info:
                reasons.append("商业生产档位必须绑定已核验的主播档案，不得使用匿名素材")
            else:
                for req_lic in rules.get("license_required", []):
                    if req_lic == "portrait_authorization" and not anchor_info.get("portrait_authorized", True):
                        reasons.append(f"【{rules['name']}】正式开播必须具备主播本人实名肖像授权证明")
                    if req_lic == "voice_authorization" and not anchor_info.get("voice_authorized", True):
                        reasons.append(f"【{rules['name']}】正式开播必须具备主播声音克隆与音色商用授权证明")

        return len(reasons) == 0, reasons


global_compliance_service = ComplianceGovernanceService()
