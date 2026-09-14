import re
import math
from typing import List, Tuple, Dict, Any, Optional

# 标准 Viseme 参数对照表 (mouth_open: 开合度 0.0~1.0, mouth_form: 唇形 -1.0圆唇 ~ +1.0扁平)
VISEME_MAP: Dict[str, Tuple[float, float]] = {
    "REST": (0.0, 0.0),       # 闭唇静止/呼吸态
    "A": (0.85, 0.4),         # 大张口：啊/爸/妈
    "E_I": (0.55, 0.85),      # 扁唇微张：一/衣/谢
    "O": (0.75, -0.65),       # 圆唇中开：哦/我/多
    "U": (0.45, -0.9),        # 紧圆噘唇：屋/如/出
    "M_B_P": (0.05, 0.0),     # 双唇闭合音：波/泼/摸
    "F_V": (0.2, 0.35),       # 齿唇咬合音：佛/非
    "L_N": (0.45, 0.3),       # 齿龈舌音：勒/呢
}

# 常见声母分类
_BILABIAL_CONSONANTS = {"b", "p", "m"}
_LABIODENTAL_CONSONANTS = {"f", "v"}
_ALVEOLAR_CONSONANTS = {"l", "n", "d", "t"}

# 拼音韵母到 Viseme 的映射
_FINAL_TO_VISEME = {
    "a": "A", "ia": "A", "ua": "A", "va": "A", "an": "A", "ang": "A", "ian": "A", "iang": "A", "uan": "A", "uang": "A",
    "o": "O", "uo": "O", "ou": "O", "iu": "O",
    "u": "U", "ui": "U", "un": "U",
    "e": "E_I", "ie": "E_I", "ei": "E_I", "en": "E_I", "eng": "E_I", "er": "E_I",
    "i": "E_I", "in": "E_I", "ing": "E_I",
    "v": "U", "ve": "E_I", "ue": "E_I",
}

# 电商直播常用汉字轻量内置拼音映射 (当未安装 pypinyin 或调用失败时的确定性音素兜底)
_BUILTIN_PINYIN_MAP: Dict[str, str] = {
    "欢": "huan", "迎": "ying", "来": "lai", "到": "dao", "直": "zhi", "播": "bo", "间": "jian",
    "朋": "peng", "友": "you", "大": "da", "家": "jia", "好": "hao", "谢": "xie", "你": "ni",
    "们": "men", "我": "wo", "他": "ta", "她": "ta", "看": "kan", "这": "zhe", "款": "kuan",
    "商": "shang", "品": "pin", "包": "bao", "邮": "you", "特": "te", "惠": "hui", "抢": "qiang",
    "购": "gou", "拍": "pai", "下": "xia", "单": "dan", "点": "dian", "赞": "zan", "关": "guan",
    "注": "zhu", "粉": "fen", "丝": "si", "福": "fu", "利": "li", "领": "ling", "券": "quan",
    "更": "geng", "便": "pian", "宜": "yi", "质": "zhi", "量": "liang", "保": "bao", "证": "zheng",
    "正": "zheng", "现": "xian", "货": "huo", "发": "fa", "速": "su", "度": "du",
    "快": "kuai", "喜": "xi", "可": "ke", "以": "yi", "爱": "ai", "心": "xin", "送": "song",
    "红": "hong", "有": "you", "没": "mei", "是": "shi", "不": "bu", "了": "le", "么": "me",
    "什": "shen", "怎": "zen", "样": "yang", "多": "duo", "少": "shao", "钱": "qian",
    "元": "yuan", "块": "kuai", "主": "zhu", "艾": "ai", "米": "mi", "助": "zhu",
    "理": "li", "给": "gei", "力": "li", "超": "chao", "值": "zhi", "秒": "miao", "杀": "sha",
}

# 英文常用词启发式音素序列
_ENGLISH_WORD_VISEMES: Dict[str, List[str]] = {
    "hello": ["E_I", "L_N", "O"],
    "live": ["L_N", "E_I", "F_V"],
    "stream": ["E_I", "M_B_P"],
    "phone": ["F_V", "O", "L_N"],
    "this": ["E_I", "E_I"],
    "yes": ["E_I", "E_I"],
    "no": ["L_N", "O"],
    "good": ["U", "L_N"],
}


def _char_to_pinyin_approx(char: str) -> str:
    """轻量拼音音素提取（优先精确 pypinyin，次选内置高频字典，最后规则提取）"""
    try:
        from pypinyin import pinyin, Style
        res = pinyin(char, style=Style.NORMAL)
        if res and res[0] and res[0][0]:
            return res[0][0].lower()
    except Exception:
        pass

    # 查询内置高频直播间字典
    if char in _BUILTIN_PINYIN_MAP:
        return _BUILTIN_PINYIN_MAP[char]

    # 英文字母直接返回
    if "a" <= char.lower() <= "z":
        return char.lower()
    return ""


def text_to_viseme_sequence(text: str) -> List[Tuple[str, float]]:
    """
    将文本转换为 G2P/启发式音素驱动的 (viseme_tag, relative_weight) 序列
    支持中文字符拼音解析与英文常用词启发式音素序列提取
    """
    tokens = re.findall(r"[\u4e00-\u9fa5]|[a-zA-Z]+|\s+", text)
    sequence: List[Tuple[str, float]] = []

    for token in tokens:
        if token.isspace():
            sequence.append(("REST", 0.5))
            continue

        token_lower = token.lower()
        if token_lower in _ENGLISH_WORD_VISEMES:
            for v_tag in _ENGLISH_WORD_VISEMES[token_lower]:
                sequence.append((v_tag, 0.8))
            continue

        for ch in token:
            py = _char_to_pinyin_approx(ch)
            if not py:
                sequence.append(("A", 1.0))
                continue

            # 检查声母
            if py.startswith(("b", "p", "m")):
                sequence.append(("M_B_P", 0.4))
                py_body = py[1:]
            elif py.startswith(("f", "v")):
                sequence.append(("F_V", 0.4))
                py_body = py[1:]
            elif py.startswith(("zh", "ch", "sh")):
                py_body = py[2:]
            elif py[:1] in _ALVEOLAR_CONSONANTS:
                sequence.append(("L_N", 0.3))
                py_body = py[1:]
            else:
                py_body = py

            # 匹配韵母
            matched_final = "A"
            for fin_key, vis in _FINAL_TO_VISEME.items():
                if py_body.endswith(fin_key):
                    matched_final = vis
                    break

            sequence.append((matched_final, 1.0))

    if not sequence:
        sequence = [("REST", 1.0)]

    return sequence


class G2PVisemeTimeline:
    """
    基于文本 G2P 音素序列与音频时长的 Viseme 时间线对齐与协同发音平滑器
    """
    def __init__(self, fps: float = 25.0, smooth_alpha: float = 0.35):
        """
        :param fps: 渲染帧率 (默认 25fps，即每帧 40ms)
        :param smooth_alpha: 协同发音平滑系数 (target * alpha + previous * (1 - alpha))
        """
        self.fps = fps
        self.frame_interval_sec = 1.0 / fps
        self.smooth_alpha = smooth_alpha

    def generate_timeline(
        self,
        text: str,
        total_duration_sec: float,
    ) -> List[Tuple[float, float]]:
        """
        根据文本和总音频时长生成平滑后的每帧 (mouth_open, mouth_form) 序列
        """
        total_duration_sec = max(0.1, total_duration_sec)
        total_frames = max(1, int(round(total_duration_sec * self.fps)))
        seq = text_to_viseme_sequence(text)

        # 时间分配：计算各音素在时间轴上的持续帧数
        total_units = len(seq)
        frames_per_unit = max(1, total_frames // total_units)

        raw_targets: List[Tuple[float, float]] = []
        for vis_tag, _ in seq:
            m_open, m_form = VISEME_MAP.get(vis_tag, VISEME_MAP["REST"])
            for _ in range(frames_per_unit):
                raw_targets.append((m_open, m_form))

        # 补齐或截断到精确帧数
        if len(raw_targets) < total_frames:
            last = raw_targets[-1] if raw_targets else VISEME_MAP["REST"]
            raw_targets.extend([last] * (total_frames - len(raw_targets)))
        else:
            raw_targets = raw_targets[:total_frames]

        # 协同发音平滑 (0.65 previous + 0.35 target)
        smoothed_sequence: List[Tuple[float, float]] = []
        prev_open, prev_form = VISEME_MAP["REST"]

        for target_open, target_form in raw_targets:
            curr_open = prev_open * (1.0 - self.smooth_alpha) + target_open * self.smooth_alpha
            curr_form = prev_form * (1.0 - self.smooth_alpha) + target_form * self.smooth_alpha
            smoothed_sequence.append((round(curr_open, 3), round(curr_form, 3)))
            prev_open, prev_form = curr_open, curr_form

        return smoothed_sequence
