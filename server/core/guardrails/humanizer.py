"""
语音声学防机械感检测盾 (规划 §14.2)
- 随机按 15% 概率注入真实语气词，打破 TTS 机械韵律指纹
- 语速/音调随机微扰，避免固定参数被平台声学指纹检测
"""
import random
from typing import Optional

FILLER_WORDS = ["嗯...", "那什么", "哈", "对的宝子们", "哎"]

# 语气词注入仅适用于节奏感强的角色 (专家角色保持严谨不注入)
APPLICABLE_ROLES = {"ecommerce", "entertainment"}

FILLER_PROBABILITY = 0.15
RATE_JITTER_RANGE = (0.97, 1.03)


def humanize_text(text: str, role_type: str = "all", rng: Optional[random.Random] = None) -> str:
    """
    对待播报句子做人类化处理
    :param text: 已通过违禁词过滤的句子
    :param role_type: 当前角色类型 (ecommerce / entertainment / expert)
    :param rng: 可注入随机源 (测试用)
    """
    if not text or not text.strip():
        return text
    _rng = rng or random
    if role_type in APPLICABLE_ROLES and _rng.random() < FILLER_PROBABILITY and len(text) > 6:
        filler = _rng.choice(FILLER_WORDS)
        return f"{filler}{text}"
    return text


def jitter_speed(speech_speed: float, rng: Optional[random.Random] = None) -> float:
    """语速随机微扰 ±3%，返回修正后的语速倍率"""
    _rng = rng or random
    return round(speech_speed * _rng.uniform(*RATE_JITTER_RANGE), 3)


def speed_to_edge_rate(speed: float) -> str:
    """语速倍率转 Edge-TTS rate 参数 (+x% / -x%)"""
    percent = int(round((speed - 1.0) * 100))
    return f"{percent:+d}%"
