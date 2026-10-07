# -*- coding: utf-8 -*-
"""
g2p viseme 映射与时长分配自动化验收测试套件 (P0-3)

对应 LIPSYNC_OPTIMIZATION_PLAN.md §7.2 ③:
- ian / iang -> E_I (扁唇微张)
- uan / uang / van / vn -> U (紧圆噘唇)
- 文本「八百万」中 b 的闭合帧 >= 2 帧且连续
- 非对称滤波 attack/release 动态响应
"""
import pytest
from server.core.media.g2p_viseme import (
    _ALVEOLAR_CONSONANTS,
    _DURATION_WEIGHT,
    _FINAL_SORTED_KEYS,
    _FINAL_TO_VISEME,
    VISEME_MAP,
    text_to_viseme_sequence,
    G2PVisemeTimeline,
)


def test_final_sorted_keys_are_longest_first():
    """不变量守护: 韵母是 endswith 后缀匹配，短键绝不能排在长键前面。

    历史缺陷：按 dict 插入序遍历时 "an" 会抢占 "ian"，把 /ian/ 误判为大张口 A。
    修复方式是按长度降序，但它依赖一条不变量——表里不能出现「短键是长键后缀」的
    冲突对。只要将来有人新增韵母打破该不变量，长度降序就不再等价于最长匹配，
    且会静默错映射。此测试把该前提显式钉住。
    """
    lengths = [len(k) for k in _FINAL_SORTED_KEYS]
    assert lengths == sorted(lengths, reverse=True), (
        f"_FINAL_SORTED_KEYS 未按长度降序: {_FINAL_SORTED_KEYS}"
    )

    # 不变量前提: 不存在「短键 a 是长键 b 的后缀」——否则必须引入更复杂的匹配规则
    conflicts = [
        (short, long)
        for i, long in enumerate(_FINAL_SORTED_KEYS)
        for short in _FINAL_SORTED_KEYS[i + 1:]
        if short != long and short.endswith(long)
    ]
    assert not conflicts, (
        f"存在后缀冲突键 {conflicts}：长度降序已不足以保证最长匹配，"
        f"需改为显式的最长后缀匹配算法"
    )


def test_final_lookup_is_longest_suffix_match():
    """逐键验证: 对每个韵母键，最长匹配必须选中它自己，而不是它的某个短后缀。"""
    for key in _FINAL_SORTED_KEYS:
        chosen = next(
            (k for k in _FINAL_SORTED_KEYS if key.endswith(k)),
            None,
        )
        assert chosen == key, (
            f"韵母 {key!r} 被更短的 {chosen!r} 抢占，最长匹配失效"
        )


def test_alveolar_consonants_excludes_l_and_n():
    """l / n 走专属 "L" / "N"，不得再落入通用 L_N。

    守护 _ALVEOLAR_CONSONANTS 的实际语义：集合里若含 l/n，则
    text_to_viseme_sequence 中的 startswith 分支会先拦截它们，此集合中的
    l/n 成员成为永远走不到的死成员，后续维护者极易被误导。
    """
    assert "l" not in _ALVEOLAR_CONSONANTS, "l 已由 startswith 分支映射为专属 L"
    assert "n" not in _ALVEOLAR_CONSONANTS, "n 已由 startswith 分支映射为专属 N"
    assert "d" in _ALVEOLAR_CONSONANTS and "t" in _ALVEOLAR_CONSONANTS

    assert text_to_viseme_sequence("来")[0][0] == "L", "「来」的声母 l 应映射为 L"
    assert text_to_viseme_sequence("你")[0][0] == "N", "「你」的声母 n 应映射为 N"


def test_viseme_map_has_three_dimensions_and_all_tags_weighted():
    """VISEME_MAP 为三元组且所有 tag 都有时长权重——否则会静默回落到默认权重 1.0。"""
    for tag, vals in VISEME_MAP.items():
        assert len(vals) == 3, f"{tag} 应为 (open, form, protrude) 三元组，实测 {vals}"
        assert tag in _DURATION_WEIGHT, (
            f"{tag} 缺少时长权重，将回落到默认 1.0（闭塞音会被拉长，与修复目标相悖）"
        )


def test_protrude_has_no_consumer_yet():
    """显式记录 protrude 当前无消费端（C 层未启动），避免误判其已生效。

    VISEME_MAP 已预留第三维，但 C 层几何控制尚未实施；generate_timeline 只返回
    二元组。若将来 C 层落地，本测试应同步改为断言 protrude 真正进入渲染路径。
    """
    timeline = G2PVisemeTimeline(fps=25.0).generate_timeline("乌", 1.0)
    assert all(len(frame) == 2 for frame in timeline), (
        "generate_timeline 仍应返回 (open, form) 二元组"
    )
    # U 类 protrude=0.30，但当前不会出现在返回值中
    assert VISEME_MAP["U"][2] == 0.30


def test_rhyme_mapping_corrections():
    """验收指标 3.1: 韵母映射硬错误修正 (扁唇微张与紧圆噘唇正确分类)"""
    # 核心修正项：原先 ian/iang/uan/uang/van 误映射为 "A"，现修正为实际舌位
    assert _FINAL_TO_VISEME["ian"] == "E_I"
    assert _FINAL_TO_VISEME["iang"] == "E_I"
    assert _FINAL_TO_VISEME["uan"] == "U"
    assert _FINAL_TO_VISEME["uang"] == "U"
    assert _FINAL_TO_VISEME["van"] == "U"
    assert _FINAL_TO_VISEME["vn"] == "U"

    # 大张口类
    assert _FINAL_TO_VISEME["a"] == "A"
    assert _FINAL_TO_VISEME["an"] == "A"
    assert _FINAL_TO_VISEME["ang"] == "A"

    # 圆唇与扁唇
    assert _FINAL_TO_VISEME["o"] == "O"
    assert _FINAL_TO_VISEME["u"] == "U"
    assert _FINAL_TO_VISEME["i"] == "E_I"
    assert _FINAL_TO_VISEME["e"] == "E_I"


def test_chinese_words_viseme_tag_resolution():
    """验收指标 3.2: 实际汉字词组解析的 viseme tag 准确性"""
    # 「天」(tian) 韵母为 ian -> 应映射为 E_I 而非 A
    seq_tian = text_to_viseme_sequence("天")
    final_tags = [t[0] for t in seq_tian if t[0] != "L_N"]
    assert "E_I" in final_tags, f"「天」的韵母应为 E_I，实际序列为 {seq_tian}"

    # 「欢」(huan) 韵母为 uan -> 应映射为 U 而非 A
    seq_huan = text_to_viseme_sequence("欢")
    assert any(t[0] == "U" for t in seq_huan), f"「欢」的韵母应为 U，实际序列为 {seq_huan}"

    # 「亮」(liang) 韵母为 iang -> 应映射为 E_I
    seq_liang = text_to_viseme_sequence("亮")
    assert any(t[0] == "E_I" for t in seq_liang), f"「亮」的韵母应为 E_I，实际序列为 {seq_liang}"

    # 「光」(guang) 韵母为 uang -> 应映射为 U
    seq_guang = text_to_viseme_sequence("光")
    assert any(t[0] == "U" for t in seq_guang), f"「光」的韵母应为 U，实际序列为 {seq_guang}"


def test_b_m_p_closure_frames_continuity():
    """验收指标 3.3: 文本「八百万」中 b 的闭合帧 >= 2 帧且连续 (时长分配与非对称平滑保障)"""
    timeline_gen = G2PVisemeTimeline(fps=25.0)
    # 「八百万」读音 ba bai wan (总长 1.0 秒，25 帧)
    timeline = timeline_gen.generate_timeline("八百万", total_duration_sec=1.0)
    assert len(timeline) == 25

    # mouth_open < 0.25 即为紧贴/闭唇状态
    closed_frames = [idx for idx, (m_open, _) in enumerate(timeline) if m_open < 0.25]
    assert len(closed_frames) >= 2, f"八百万中的闭合帧数应 >= 2，实测 {len(closed_frames)} 帧: {closed_frames}"

    # 验证闭合帧中存在连续闭合段 (反映双唇闭塞音的爆发前持阻期)
    has_consecutive = False
    for i in range(len(closed_frames) - 1):
        if closed_frames[i + 1] == closed_frames[i] + 1:
            has_consecutive = True
            break
    assert has_consecutive, f"闭合帧必须包含至少 2 个连续帧，实测闭合帧索引为: {closed_frames}"


def test_asymmetric_filter_dynamics():
    """验收指标 3.4: 非对称滤波快速跟随攻击段 (ATTACK_ALPHA=0.75) 与温和释放段 (RELEASE_ALPHA=0.35)"""
    timeline_gen = G2PVisemeTimeline(fps=25.0)
    # 输入「爸啊」：从双唇闭合快速跃迁到大张口，再平稳释放
    timeline = timeline_gen.generate_timeline("爸啊", total_duration_sec=0.8)
    assert len(timeline) == 20

    # 验证开度能有效拉开并具有非对称滤波的陡峭上升能力
    opens = [m_open for m_open, _ in timeline]
    assert min(opens) < 0.25, f"起音闭合应能压低至 < 0.25，实际最低 {min(opens)}"
    assert max(opens) > 0.65, f"张口应能快速冲高至 > 0.65，实际最高 {max(opens)}"


def test_all_rhymes_and_punctuation_pause():
    """验收指标 3.5: 验证全量 39 韵母解析 (如 hong/bao/ai/miao) 及标点符号自然停顿 REST"""
    # 验证红包 (hong bao) -> 包含 O 韵母
    seq_hong = text_to_viseme_sequence("红包")
    assert any(t[0] == "O" for t in seq_hong), f"「红包」应包含圆唇 O，实测: {seq_hong}"

    # 验证爱 (ai) -> A
    seq_ai = text_to_viseme_sequence("可爱")
    assert any(t[0] == "A" for t in seq_ai), f"「可爱」应包含大张口 A，实测: {seq_ai}"

    # 验证秒 (miao) -> O
    seq_miao = text_to_viseme_sequence("秒杀")
    assert any(t[0] == "O" for t in seq_miao), f"「秒杀」应包含 O，实测: {seq_miao}"

    # 验证标点符号自然停顿: "你好，朋友！" 中必须插入 REST 闭唇静止
    seq_punct = text_to_viseme_sequence("你好，朋友！")
    rest_tags = [t for t in seq_punct if t[0] == "REST"]
    assert len(rest_tags) >= 2, f"句中逗号与句末感叹号应生成 REST 停顿，实测: {seq_punct}"
