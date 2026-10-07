import re
import math
from typing import List, Tuple, Dict, Any, Optional

# 标准 Viseme 参数对照表 (mouth_open: 开合度 0.0~1.0, mouth_form: 唇形 -1.0圆唇 ~ +1.0扁平)
# 标准 Viseme 参数对照表 (mouth_open: 开合度 0.0~1.0, mouth_form: 唇形 -1.0圆唇 ~ +1.0扁平, protrude: 噘唇/前伸度 0.0~1.0)
VISEME_MAP: Dict[str, Tuple[float, float, float]] = {
    # tag:      (mouth_open, mouth_form, protrude)
    "REST":     (0.00,  0.00, 0.00),   # 闭唇静止/呼吸态
    "A":        (0.85,  0.40, 0.00),   # 大张口：啊/爸/妈
    "E_I":      (0.55,  0.85, 0.00),   # 扁唇微张：一/衣/谢
    "O":        (0.75, -0.65, 0.15),   # 圆唇中开：哦/我/多
    "U":        (0.45, -0.90, 0.30),   # 紧圆噘唇：屋/如/出
    "M_B_P":    (0.05,  0.00, 0.00),   # 双唇闭合音：波/泼/摸
    "F_V":      (0.20,  0.35, 0.55),   # 齿唇咬合音：佛/非 (高 protrude)
    "L_N":      (0.45,  0.30, 0.00),   # 齿龈舌音：特/德
    "L":        (0.40,  0.45, 0.00),   # 舌尖抵齿龈：勒 (唇略扁)
    "N":        (0.40,  0.20, 0.00),   # 鼻音：呢 (唇中性)
}

# 常见声母分类
_BILABIAL_CONSONANTS = {"b", "p", "m"}
_LABIODENTAL_CONSONANTS = {"f", "v"}
# 仅 d/t 走通用齿龈通路: l 与 n 在 text_to_viseme_sequence 中被先行拆出,
# 分别映射到专属的 "L" / "N" (舌尖抵齿龈, 唇形与通用 L_N 不同), 故不在此集合内。
_ALVEOLAR_CONSONANTS = {"d", "t"}

# 拼音韵母到 Viseme 的映射
# 涵盖汉语普通话全量 39 个韵母，确保每个发音都有精准舌位与唇形归宿
_FINAL_TO_VISEME = {
    # 大张口类 (/a/, /ai/, /uai/, /ia/, /ua/, /an/, /ang/)
    "a": "A", "ia": "A", "ua": "A", "va": "A",
    "ai": "A", "uai": "A",
    "an": "A", "ang": "A",
    # 扁唇微张类 (/i/, /e/, /ie/, /ei/, /en/, /eng/, /er/, /ian/, /iang/, /in/, /ing/, /ue/, /ve/)
    "ian": "E_I", "iang": "E_I",
    "e": "E_I", "ie": "E_I", "ei": "E_I", "en": "E_I",
    "eng": "E_I", "er": "E_I",
    "i": "E_I", "in": "E_I", "ing": "E_I",
    "ve": "E_I", "ue": "E_I",
    # 圆唇中开类 (/o/, /uo/, /ou/, /iu/, /ao/, /iao/, /ong/)
    "o": "O", "uo": "O", "ou": "O", "iu": "O",
    "ao": "O", "iao": "O", "ong": "O",
    # 紧圆噘唇类 (/u/, /ui/, /un/, /uan/, /uang/, /van/, /vn/, /ü/, /iong/, /ueng/)
    "u": "U", "ui": "U", "un": "U",
    "uan": "U",   "uang": "U",
    "van": "U",   "vn": "U",
    "v": "U", "iong": "U", "ueng": "U",
}

# 按照韵母键的长度降序排序匹配，防止 "ian"/"uan" 被短后缀 "an" 抢占误伤。
#
# 这条不变量由 test_g2p_viseme_mapping.py::test_final_sorted_keys_are_longest_first
# 守护: 韵母匹配是 endswith 后缀匹配, 因此**任何**新增键只要是另一个键的后缀,
# 就必须保证更长的先被检查。违反会导致 e.g. 新增 "iang" 后被 "ang" 抢占而静默错映射。
# 当前已验证: 表中不存在「短键是长键后缀」的冲突对, 故长度降序足以保证最长匹配。
_FINAL_SORTED_KEYS = sorted(_FINAL_TO_VISEME.keys(), key=len, reverse=True)

# 音素时长权重 (相对帧单位)。闭塞音短促快闭，元音持续舒展
_DURATION_WEIGHT: Dict[str, float] = {
    "M_B_P": 0.6,   # 闭塞音: 快闭快放, 占帧较短
    "F_V":   0.8,
    "L":     0.8, "N": 0.8, "L_N": 0.8,
    "E_I":   1.2, "A": 1.4, "O": 1.4, "U": 1.3,   # 核心元音: 持续稳态
    "REST":  0.5,
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
    支持中文字符拼音解析、英文常用词启发式音素序列提取以及标点符号自然停顿
    """
    tokens = re.findall(r"[\u4e00-\u9fa5]|[a-zA-Z]+|[，。！？、；…,\.!\?;:]+|\s+", text)
    sequence: List[Tuple[str, float]] = []

    for token in tokens:
        if token.isspace() or re.match(r"^[，。！？、；…,\.!\?;:]+$", token):
            sequence.append(("REST", 0.6))
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
            elif py.startswith("l"):
                sequence.append(("L", 0.3))
                py_body = py[1:]
            elif py.startswith("n"):
                sequence.append(("N", 0.3))
                py_body = py[1:]
            elif py[:1] in _ALVEOLAR_CONSONANTS:
                sequence.append(("L_N", 0.3))
                py_body = py[1:]
            else:
                py_body = py

            # 按最长匹配原则匹配韵母
            matched_final = "A"
            for fin_key in _FINAL_SORTED_KEYS:
                if py_body.endswith(fin_key):
                    matched_final = _FINAL_TO_VISEME[fin_key]
                    break

            sequence.append((matched_final, 1.0))

    if not sequence:
        sequence = [("REST", 1.0)]

    return sequence


class G2PVisemeTimeline:
    """
    基于文本 G2P 音素序列与音频时长的 Viseme 时间线对齐与协同发音平滑器
    """
    ATTACK_ALPHA = 0.75    # 目标与当前差异大时 —— 快速跟随, 保证 b/p/m 闭合不被拖尾
    RELEASE_ALPHA = 0.35   # 差异小时 —— 平滑过渡, 避免元音跳变

    def __init__(self, fps: float = 25.0, smooth_alpha: float = 0.35):
        """
        :param fps: 渲染帧率 (默认 25fps，即每帧 40ms)
        :param smooth_alpha: 协同发音平滑基准系数 (当外部传入时保留接口兼容)
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

        # 时间分配：按音素发音权重分配帧数，避免闭塞音与长元音均分
        weights = [_DURATION_WEIGHT.get(v_tag, 1.0) for v_tag, _ in seq]
        total_w = sum(weights) or 1.0
        allocated = [max(1, int(round(total_frames * (w / total_w)))) for w in weights]

        diff = total_frames - sum(allocated)
        if diff != 0 and len(allocated) > 0:
            allocated[-1] = max(1, allocated[-1] + diff)

        raw_targets: List[Tuple[float, float]] = []
        for (vis_tag, _), n_f in zip(seq, allocated):
            m_vals = VISEME_MAP.get(vis_tag, VISEME_MAP["REST"])
            # 只取前两维。VISEME_MAP 的第三维 protrude (噘唇/前伸度) 目前**无消费端**:
            # C 层几何控制尚未实施 (见 LIPSYNC_OPTIMIZATION_PLAN §6 的启动条件),
            # 在此之前它只是为 C 层预留的数据。切勿误以为它已影响唇形。
            m_open, m_form = m_vals[0], m_vals[1]
            for _ in range(n_f):
                raw_targets.append((m_open, m_form))

        # 补齐或截断到精确帧数
        # 注意 allocated 用 max(1, ...) 兜底, 故音素数 > total_frames 时总数必然超出,
        # 此处截断会**静默丢弃句尾音素**。这是短音频 + 长文本的固有边界:
        # 文本比音频长意味着 TTS 已压缩/截断了语音, 尾部音素本就不该有对应唇形。
        # 调用方 (musetalk_driver) 在 frame_idx 超出时间线时会回落到声学估计。
        if len(raw_targets) < total_frames:
            last = raw_targets[-1] if raw_targets else (0.0, 0.0)
            raw_targets.extend([last] * (total_frames - len(raw_targets)))
        else:
            raw_targets = raw_targets[:total_frames]

        # 非对称平滑滤波: 激变大动作快速跟随，稳态过渡平滑释放
        smoothed_sequence: List[Tuple[float, float]] = []
        prev_vals = VISEME_MAP["REST"]
        prev_open, prev_form = prev_vals[0], prev_vals[1]

        for target_open, target_form in raw_targets:
            delta = abs(target_open - prev_open) + abs(target_form - prev_form)
            alpha = self.ATTACK_ALPHA if delta > 0.30 else self.RELEASE_ALPHA
            curr_open = prev_open * (1.0 - alpha) + target_open * alpha
            curr_form = prev_form * (1.0 - alpha) + target_form * alpha
            smoothed_sequence.append((round(curr_open, 3), round(curr_form, 3)))
            prev_open, prev_form = curr_open, curr_form

        return smoothed_sequence
