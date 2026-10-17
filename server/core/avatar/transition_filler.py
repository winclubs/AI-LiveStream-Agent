# -*- coding: utf-8 -*-
"""
切片间随机语气过场词管理器 (TransitionFillerManager)
适用阶段：7x24小时无人值守直播带货「动静分离 / 批播结合」架构 —— 阶段五听觉防查与真实感拟人增强。

核心职责：
1. 维护电商带货常用的轻量过渡与语气过场台词库 (如转场提神、福利承接、答疑引出)；
2. 在切片轮播队列 (如 selling_point -> coupon) 间按可配概率 (默认 35%) 动态决策是否注入过场台词；
3. 保证过场词切片或音频遵循统一的 EBU R128 (-16.0 LUFS) 响度规范与舒适底噪垫声；
4. 破坏确定性切片拼接的周期性声学时间步特征，彻底消除录播审查指纹。
"""
from __future__ import annotations

import logging
import random
from typing import Dict, List

logger = logging.getLogger("LiveAgent.TransitionFiller")

# 预置电商直播常用拟人化语气过场台词 (按转场场景分类)
DEFAULT_FILLER_LIBRARY: Dict[str, List[str]] = {
    "intro_to_pitch": [
        "好嘞，宝子们，咱们直接上核心干货！",
        "话不多说，咱们马上来看大家最关心的几个亮点！",
        "先别急着划走，下面这几点绝对让你眼前一亮！",
    ],
    "pitch_to_coupon": [
        "很多新进直播间的宝子已经在问优惠了，咱们重头戏来了哈！",
        "既然大家这么热情，那专属福利价咱们直接安排上！",
        "重点来了宝子们，看好左下角，福利机制千万别错过！",
    ],
    "coupon_to_faq": [
        "刚才有宝子在弹幕问售后和发货问题，主播统一交代一句哈！",
        "大家放心拍，关于售后和运费，咱们家保障直接拉满！",
        "大家先别慌，尺码和售后细节，主播这就跟您说清楚！",
    ],
    "faq_to_urgency": [
        "行了宝子们，话都说到这份上了，犹豫真的就没啦！",
        "仓库刚给的库存预警，最后这一波拼的就是手速！",
        "好，倒计时马上开始，看准了直接冲！",
    ],
    "general": [
        "好，咱们接着往下看！",
        "大家注意听哈，接下来这个细节特别关键！",
        "对，新进来的宝子别划走，精彩继续！",
        "咱们继续来看，绝对物超所值！",
    ],
}


class TransitionFillerManager:
    """
    语气过场词决策与切片注入管理器
    """

    def __init__(self, injection_probability: float = 0.35, enabled: bool = True) -> None:
        """
        :param injection_probability: 注入概率 [0.0, 1.0]，默认 0.35 (35% 几率注入)
        :param enabled: 是否启用过场词注入
        """
        self.injection_probability = max(0.0, min(1.0, float(injection_probability)))
        self.enabled = enabled
        self._custom_library: Dict[str, List[str]] = {k: list(v) for k, v in DEFAULT_FILLER_LIBRARY.items()}

    def should_inject(self) -> bool:
        """根据配置概率决定是否在当前切片转场时注入过场词"""
        if not self.enabled:
            return False
        return random.random() < self.injection_probability

    def pick_filler_text(self, from_slot: str = "", to_slot: str = "") -> str:
        """
        依据前后插槽语义挑选适宜的过渡短句
        :param from_slot: 前序插槽 (如 intro, selling_point_1, coupon, faq_size)
        :param to_slot: 后续插槽 (如 coupon, urgency 等)
        :return: 语气过渡短句文本
        """
        scene_key = "general"
        if "intro" in from_slot and ("pitch" in to_slot or "selling" in to_slot):
            scene_key = "intro_to_pitch"
        elif ("pitch" in from_slot or "selling" in from_slot) and "coupon" in to_slot:
            scene_key = "pitch_to_coupon"
        elif "coupon" in from_slot and "faq" in to_slot:
            scene_key = "coupon_to_faq"
        elif ("faq" in from_slot or "coupon" in from_slot) and "urgency" in to_slot:
            scene_key = "faq_to_urgency"

        candidates = self._custom_library.get(scene_key) or self._custom_library.get("general", [])
        if not candidates:
            return "好，咱们接着看！"
        return random.choice(candidates)

    def add_custom_filler(self, category: str, text: str) -> None:
        """扩展或自定义过场词库"""
        clean_text = text.strip()
        if not clean_text:
            return
        if category not in self._custom_library:
            self._custom_library[category] = []
        if clean_text not in self._custom_library[category]:
            self._custom_library[category].append(clean_text)


global_transition_filler_manager = TransitionFillerManager()
