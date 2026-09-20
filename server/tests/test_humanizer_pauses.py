# -*- coding: utf-8 -*-
"""
单元测试：发音韵律拟人化微停顿与语气词注入验证 (任务 2.4)
"""
import random
import pytest

from server.core.guardrails.humanizer import (
    humanize_text,
    inject_human_pauses,
    inject_conversational_fillers,
    jitter_speed,
)


def test_inject_human_pauses():
    """验证长句智能寻找语义标点注入换气微停顿"""
    raw_text = "这款精华液含有高浓度透明质酸，能够深层补水锁水，改善肌肤干燥暗沉问题，现在下单享受买一送一福利。"
    
    # 固定种子测试标点延时模式
    rng = random.Random(42)
    paused_punct = inject_human_pauses(raw_text, role_type="ecommerce", pause_format="punctuation", rng=rng)
    assert "……" in paused_punct, "标点延时模式应注入呼吸微停顿标点"

    # 固定种子测试 SSML break 模式
    rng2 = random.Random(42)
    paused_ssml = inject_human_pauses(raw_text, role_type="ecommerce", pause_format="ssml", rng=rng2)
    assert "<break time=" in paused_ssml, "SSML 模式应注入 <break time='...ms'/> 标签"


def test_expert_role_restraint():
    """验证严谨专业/专家角色不乱加口语语气词与换气停顿"""
    raw_text = "根据第三季度财务报告显示，公司总营业收入同比增长18.5%，净利润表现优异。"
    rng = random.Random(42)
    out = humanize_text(raw_text, role_type="expert", rng=rng)
    assert out == raw_text, "专家角色应保持严肃严谨，不被改动"


def test_ecommerce_conversational_humanize():
    """验证电商带货主播自然语气词与换气综合处理"""
    raw_text = "大家看左下角一号链接，这款面膜今天直播间直接破价，拍一盒发三盒，库存只有最后五十单。"
    rng = random.Random(7)
    humanized = humanize_text(raw_text, role_type="ecommerce", rng=rng)
    assert len(humanized) >= len(raw_text)
    # 语速微扰
    speed = jitter_speed(1.0, rng=rng)
    assert 0.95 <= speed <= 1.05
