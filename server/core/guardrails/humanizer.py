"""
语音声学防机械感检测盾与发音拟人化韵律引擎 (规划 §14.2 / 任务 2.4)
- 智能注入人类口语自然的换气点与微停顿 (0.3s~0.8s)，杜绝机器连珠炮节奏；
- 随机注入真实语气词 (句首与句中助词)，打破 TTS 机械韵律指纹；
- 语速/音调随机微扰，避免固定参数被平台声学指纹检测。
"""
import random
import re
from typing import Optional

# 句首口癖与暖场语气词
START_FILLER_WORDS = ["嗯...", "那什么", "哈", "对的宝子们", "哎", "来", "好的呢"]

# 句中口语连接助词与呼吸点
MID_SENTENCE_PARTICLES = ["那个", "就是说", "对吧", "哈", "然后呢", "嗯"]

# 语气词注入仅适用于节奏感强的角色 (专家角色保持严谨不注入)
APPLICABLE_ROLES = {"ecommerce", "entertainment", "chitchat"}

FILLER_PROBABILITY = 0.20
PAUSE_PROBABILITY = 0.35
RATE_JITTER_RANGE = (0.97, 1.03)


def inject_human_pauses(
    text: str,
    role_type: str = "ecommerce",
    pause_format: str = "punctuation",
    rng: Optional[random.Random] = None,
) -> str:
    """
    在长句中智能寻找语义切分点，注入人类自然的换气微停顿 (0.3s~0.8s)
    :param text: 原始文本
    :param role_type: 角色类型
    :param pause_format: 停顿格式 ("punctuation"=标点声学延时, "ssml"=SSML break 标签)
    :param rng: 随机数源
    """
    if not text or len(text) < 12 or role_type not in APPLICABLE_ROLES:
        return text

    _rng = rng or random

    # 按常见语义标点拆分子句
    segments = re.split(r"([，,；;。!！?？])", text)
    if len(segments) <= 2:
        return text

    result = []
    i = 0
    while i < len(segments):
        seg = segments[i]
        result.append(seg)
        # 如果是标点符号且满足概率要求，注入换气停顿
        if i + 1 < len(segments) and segments[i + 1] in ("，", ",", "；", ";", "。"):
            punc = segments[i + 1]
            if _rng.random() < PAUSE_PROBABILITY:
                if pause_format == "ssml":
                    pause_ms = _rng.randint(300, 750)
                    result.append(f"{punc}<break time=\"{pause_ms}ms\"/>")
                else:
                    # 标点符号声学延时扩展（如加破折号或省略符号）
                    result.append(f"{punc}…… ")
            else:
                result.append(punc)
            i += 2
            continue
        i += 1

    return "".join(result)


def inject_conversational_fillers(
    text: str,
    role_type: str = "ecommerce",
    rng: Optional[random.Random] = None,
) -> str:
    """
    随机在句首或句中适度注入自然语气词，打破机械等间隔播报
    """
    if not text or not text.strip() or role_type not in APPLICABLE_ROLES:
        return text

    _rng = rng or random
    out = text

    # 1. 句首语气词注入 (18% 概率)
    if _rng.random() < FILLER_PROBABILITY and len(out) > 6:
        start_filler = _rng.choice(START_FILLER_WORDS)
        out = f"{start_filler}，{out}"

    # 2. 句中逗号处偶发注入连接助词 (15% 概率)
    if _rng.random() < 0.15 and "，" in out:
        mid_filler = _rng.choice(MID_SENTENCE_PARTICLES)
        # 仅替换第一个逗号
        out = out.replace("，", f"，{mid_filler}，", 1)

    return out


def humanize_text(
    text: str,
    role_type: str = "all",
    pause_format: str = "punctuation",
    rng: Optional[random.Random] = None,
) -> str:
    """
    对待播报句子做全套拟人化处理 (语气词注入 + 换气微停顿注入)
    :param text: 已通过违禁词过滤的句子
    :param role_type: 当前角色类型 (ecommerce / entertainment / expert)
    :param pause_format: 停顿格式 ("punctuation" 或 "ssml")
    :param rng: 可注入随机源 (测试用)
    """
    if not text or not text.strip():
        return text

    _rng = rng or random

    # 1. 注入语气词
    out = inject_conversational_fillers(text, role_type=role_type, rng=_rng)

    # 2. 注入换气停顿
    out = inject_human_pauses(out, role_type=role_type, pause_format=pause_format, rng=_rng)

    return out


def jitter_speed(speech_speed: float, rng: Optional[random.Random] = None) -> float:
    """语速随机微扰 ±3%，返回修正后的语速倍率"""
    _rng = rng or random
    return round(speech_speed * _rng.uniform(*RATE_JITTER_RANGE), 3)


def speed_to_edge_rate(speed: float) -> str:
    """语速倍率转 Edge-TTS rate 参数 (+x% / -x%)"""
    percent = int(round((speed - 1.0) * 100))
    return f"{percent:+d}%"

