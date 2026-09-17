# -*- coding: utf-8 -*-
"""
P2 商业化运营保障与凭证探针测试套件 (Commercial Safeguards Test Suite)
"""
import pytest
from server.core.llm.budget_manager import LLMBudgetManager, get_llm_budget_manager
from server.adapters.danmaku.probe import probe_danmaku_platform
from server.core.media.rtmp_streamer import find_ffmpeg_binary


def test_llm_budget_manager_trip_and_local_speech():
    """测试 LLM 预算计数、熔断触发与本地商品话术模板生成"""
    mgr = LLMBudgetManager(max_tokens=1000)
    mgr.reset_session("test_session_1")
    assert mgr.is_budget_exceeded() is False
    assert mgr.used_tokens == 0

    # 消耗 600 tokens
    mgr.record_usage(estimated_prompt_tokens=400, estimated_completion_tokens=200)
    assert mgr.used_tokens == 600
    assert mgr.is_budget_exceeded() is False

    # 再次消耗 500 tokens -> 累计 1100 tokens -> 触发熔断
    mgr.record_usage(estimated_prompt_tokens=300, estimated_completion_tokens=200)
    assert mgr.used_tokens == 1100
    assert mgr.is_budget_exceeded() is True
    assert mgr.is_tripped is True
    assert mgr.should_use_local_filler() is True

    # 验证本地叫卖话术生成 (包含商品要素，0 Token 成本)
    test_prod = {
        "title": "极光玻尿酸精华原液",
        "live_price": 69.9,
        "selling_points": ["深层补水锁水", "温和不刺激"],
    }
    speech = mgr.generate_local_carousel_speech(test_prod, is_urgency=False)
    assert "极光玻尿酸精华原液" in speech
    assert "69.9" in speech

    urgency_speech = mgr.generate_local_carousel_speech(test_prod, is_urgency=True)
    assert len(urgency_speech) > 10


@pytest.mark.anyio
async def test_danmaku_probe_scenarios():
    """测试弹幕连通性探针对于各平台的探测与建议"""
    # 1. 仿真演示模式
    res_mock = await probe_danmaku_platform("mock", "room_demo")
    assert res_mock["healthy"] is True
    assert res_mock["status"] == "mock"

    # 2. 抖音模式：未配 ttwid 给出安全警告与建议
    res_douyin = await probe_danmaku_platform("douyin", "12345678", ttwid="")
    assert res_douyin["status"] == "warning"
    assert any("ttwid" in s for s in res_douyin["suggestions"])

    # 3. 抖音模式：配了 ttwid 标记 ready
    res_douyin_ok = await probe_danmaku_platform("douyin", "12345678", ttwid="valid_ttwid_mock_123456789")
    assert res_douyin_ok["status"] == "ready"


def test_find_ffmpeg_binary_fallback():
    """测试 FFmpeg 嗅探机制"""
    bin_path = find_ffmpeg_binary()
    # 环境中若已安装或在 PATH 中，返回路径应包含 ffmpeg
    if bin_path:
        assert "ffmpeg" in bin_path.lower()
