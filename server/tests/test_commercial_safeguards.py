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
async def test_llm_budget_estimation_and_recording_on_stream(monkeypatch):
    """回归：generate_stream 必须把 Token 计入预算，否则熔断器永不触发。

    缺陷复现：record_usage() 曾无任何生产调用方，used_tokens 恒为 0，
    GET /llm/budget 上报 0 消耗，30 万 Token 熔断形同摆设。修复后每次
    generate_stream 完成都会估算 prompt+completion 并记账。
    """
    from server.core.llm import client as llm_mod
    from server.core.llm.client import LLMClient
    from server.core.llm.budget_manager import estimate_tokens

    # 1. estimate_tokens 基本行为：中文与拉丁文系数不同，非空文本必为正
    assert estimate_tokens("") == 0
    assert estimate_tokens("中文带货话术") > 0
    assert estimate_tokens("english product description") > 0
    assert estimate_tokens("中文") > estimate_tokens("ab")

    # 2. 用低上限预算管理器隔离全局单例，模拟流式生成并验证记账
    test_mgr = get_llm_budget_manager()
    monkeypatch.setattr(test_mgr, "max_tokens", 30)
    monkeypatch.setattr(test_mgr, "used_tokens", 0)
    monkeypatch.setattr(test_mgr, "total_calls", 0)
    monkeypatch.setattr(test_mgr, "is_tripped", False)
    assert test_mgr.used_tokens == 0 and test_mgr.total_calls == 0

    long_prompt = "你是带货主播" * 10
    long_reply = "这是一段非常长的中文带货话术输出用于把预算打满触发熔断保护机制" * 6
    line_batches = [
        [f'data:{{"choices":[{{"delta":{{"content":"{long_reply}"}}}}]}}', "data:[DONE]"],
        [f'data:{{"choices":[{{"delta":{{"content":"{long_reply}"}}}}]}}', "data:[DONE]"],
    ]

    class FakeResponse:
        status_code = 200

        def __init__(self, lines):
            self.lines = lines

        async def aiter_lines(self):
            for line in self.lines:
                yield line

    class FakeStreamContext:
        def __init__(self, lines):
            self.response = FakeResponse(lines)

        async def __aenter__(self):
            return self.response

        async def __aexit__(self, *args):
            return False

    class FakeAsyncClient:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        def stream(self, *args, **kwargs):
            return FakeStreamContext(line_batches.pop(0))

    async def fake_config():
        return {
            "provider_name": "openai_compatible",
            "base_url": "https://example.invalid/v1",
            "model_name": "test-model",
            "api_key": "test-key",
            "extra_params": {},
        }

    async def collect():
        return [c async for c in LLMClient.generate_stream(long_prompt, "介绍一下商品")]

    monkeypatch.setattr(llm_mod.httpx, "AsyncClient", FakeAsyncClient)
    monkeypatch.setattr(LLMClient, "get_active_llm_config", staticmethod(fake_config))

    # 第一轮：远程流式成功 -> 记账
    await collect()
    assert test_mgr.total_calls == 1, "远程流式完成后必须记账一次"
    assert test_mgr.used_tokens > 0, "used_tokens 必须被真实累加，不能再恒为 0"

    # 第二轮：预算已熔断 -> 直接走本地兜底，不再发起远程调用
    await collect()
    assert test_mgr.is_tripped is True, "累计达到上限必须触发熔断"
    assert test_mgr.is_budget_exceeded() is True
    status = test_mgr.get_status()
    assert status["used_tokens"] == test_mgr.used_tokens
    assert status["total_calls"] == 1, "熔断后不再发起远程调用，记账次数锁定"
    assert status["is_tripped"] is True


@pytest.mark.anyio
async def test_llm_budget_fallback_path_also_records(monkeypatch):
    """离线兜底路径同样记账，保证遥测口径一致 (无 Key / 断网场景)"""
    from server.core.llm import client as llm_mod
    from server.core.llm.client import LLMClient

    test_mgr = get_llm_budget_manager()
    monkeypatch.setattr(test_mgr, "max_tokens", 10_000_000)
    monkeypatch.setattr(test_mgr, "used_tokens", 0)
    monkeypatch.setattr(test_mgr, "total_calls", 0)
    monkeypatch.setattr(test_mgr, "is_tripped", False)

    class NoKeyClient:
        def __init__(self, **kwargs):
            raise RuntimeError("模拟断网")

    async def fake_config():
        return {
            "provider_name": "openai_compatible",
            "base_url": "https://example.invalid/v1",
            "model_name": "test-model",
            "api_key": "test-key",
            "extra_params": {},
        }

    monkeypatch.setattr(llm_mod.httpx, "AsyncClient", NoKeyClient)
    monkeypatch.setattr(LLMClient, "get_active_llm_config", staticmethod(fake_config))
    monkeypatch.setattr(LLMClient, "_generate_fallback", staticmethod(lambda *a: "离线兜底话术输出"))

    async def collect():
        return [c async for c in LLMClient.generate_stream("你是陪伴主播", "观众问好")]

    chunks = await collect()
    assert "".join(chunks) == "离线兜底话术输出"
    assert test_mgr.total_calls == 1, "兜底路径也必须记账"


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
