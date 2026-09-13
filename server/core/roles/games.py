"""
娱乐主播互动小游戏引擎 (规划 §5.3)
- 成语接龙：程序化推进，无需 LLM 即可稳定运行
- 脑筋急转弯 / 灯谜：内置题库，答题判定
- 分级打赏鸣谢：按礼物价值映射话术强度
"""
import random
from typing import Optional, Tuple

IDIOMS = [
    "一马当先", "先声夺人", "人山人海", "海阔天空", "空前绝后", "后来居上",
    "上善若水", "水到渠成", "成竹在胸", "胸有成竹", "竹报平安", "安居乐业",
    "业精于勤", "勤能补拙", "拙嘴笨舌", "舌灿莲花", "花好月圆", "圆满成功",
    "功成名就", "就地取材", "材大难用", "用兵如神", "神采奕奕", "奕奕欲生",
    "生龙活虎", "虎虎生威", "威风凛凛", "凛然正气", "气宇轩昂", "昂首阔步",
]

RIDDLES = [
    ("什么东西越洗越脏，不洗有人吃，洗了没人吃？", "水"),
    ("什么东西天气越热，它爬得越高？", "温度计"),
    ("白天和黑夜之间是什么？", "和"),
    ("什么动物最没有方向感？", "麋鹿（迷路）"),
    ("什么布剪不断？", "瀑布"),
    ("什么东西倒着走反而更快？", "钟表指针"),
    ("什么东西你有别人也有，但别人用得比你多？", "名字"),
    ("一个西瓜和一块石头，砸头哪个疼？", "头最疼"),
]

# 打赏分级映射：(最高金瓜子阈值, 情绪强度, 台词引导)
GIFT_TIERS = [
    (500, "light", "轻轻地道谢，保持俏皮"),
    (5000, "medium", "热情惊喜地道谢，带动公屏氛围"),
    (50000, "high", "非常激动地花式感谢，给足牌面并比心"),
    (10 ** 12, "super", "超级高能鸣谢，全场欢呼，感谢老板火箭嘉年华给足排面"),
]


def gift_tier(total_coin: int) -> Tuple[str, str]:
    for threshold, level, guide in GIFT_TIERS:
        if total_coin < threshold:
            return level, guide
    return "light", GIFT_TIERS[0][2]


class MiniGameEngine:
    """单直播间进程内的轻量游戏状态机"""

    def __init__(self):
        self.mode: Optional[str] = None          # None / idiom / riddle
        self.pending_char: Optional[str] = None   # 成语接龙待接字
        self.pending_idiom: Optional[str] = None
        self.current_riddle: Optional[Tuple[str, str]] = None
        self.score = 0

    def start_idiom(self) -> str:
        self.mode = "idiom"
        self.current_riddle = None
        self.pending_idiom = random.choice(IDIOMS)
        self.pending_char = self.pending_idiom[-1]
        return (
            f"来玩成语接龙啦！我先说【{self.pending_idiom}】，"
            f"轮到你们接一个以「{self.pending_char}」开头的成语，打在公屏上我看看谁最快！"
        )

    def _find_idiom_starting_with(self, ch: str) -> Optional[str]:
        candidates = [i for i in IDIOMS if i.startswith(ch) and i != self.pending_idiom]
        return random.choice(candidates) if candidates else None

    def continue_idiom(self, text: str) -> Optional[str]:
        """玩家接入成语接龙，返回主播应答；无法识别时返回 None 交由普通聊天处理"""
        text = (text or "").strip()
        if not text or self.mode != "idiom" or not self.pending_char:
            return None
        next_idiom = self._find_idiom_starting_with(self.pending_char)
        matched = any(i in text for i in IDIOMS if i.startswith(self.pending_char))
        if matched:
            self.score += 1
            if next_idiom:
                self.pending_idiom = next_idiom
                self.pending_char = next_idiom[-1]
                return f"接得漂亮！那我也来【{next_idiom}】，接下来「{self.pending_char}」开头的，谁来？"
            return "太厉害了，这个我一时接不上，算你赢！要不要换个脑筋急转弯玩？"
        # 未接上：给出提示
        return f"哎呀，要用「{self.pending_char}」开头的成语哦，再想想~"

    def start_riddle(self) -> str:
        self.mode = "riddle"
        self.pending_idiom = None
        self.pending_char = None
        self.current_riddle = random.choice(RIDDLES)
        return f"脑筋急转弯来咯：{self.current_riddle[0]} 知道答案的宝子公屏打出来！"

    def answer_riddle(self, text: str) -> Optional[str]:
        if self.mode != "riddle" or not self.current_riddle:
            return None
        answer = self.current_riddle[1]
        text = (text or "").strip()
        if answer and answer in text:
            self.mode = None
            self.score += 1
            self.current_riddle = None
            return f"答对啦！答案就是【{answer}】，太聪明了！要不要再来一个？"
        return None  # 未答对则交由普通聊天，不打断互动


global_mini_games = MiniGameEngine()
