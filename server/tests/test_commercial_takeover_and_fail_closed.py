import asyncio
from unittest.mock import AsyncMock, MagicMock, patch
from fastapi.testclient import TestClient

from server.app import app
from server.routes.live import global_live_controller


def test_takeover_atomically_interrupts_and_switches_state():
    """验证人工接管原子打断 AI/TTS 并将状态流转为 HUMAN_TAKEOVER"""
    async def _run():
        ctrl = global_live_controller
        ctrl.is_live = True
        ctrl.execution_mode = "PRODUCTION"
        ctrl.state_detail = "LOCAL_READY"
        ctrl.session_id = "sess_test_takeover"

        # 触发人工接管
        res = await ctrl.takeover(operator="op_alice", reason="运营插播口播")
        assert res["ok"] is True
        assert ctrl.state_detail == "HUMAN_TAKEOVER"
        assert ctrl.event_queue.is_interrupted_flag is True

        # 释放接管
        rel = await ctrl.release_takeover(operator="op_alice")
        assert rel["ok"] is True
        assert ctrl.state_detail == "LOCAL_READY"

        ctrl.is_live = False

    asyncio.run(_run())


def test_takeover_api_endpoints():
    """测试 /api/v1/live/takeover 接口"""
    client = TestClient(app)
    ctrl = global_live_controller
    ctrl.is_live = True
    ctrl.session_id = "sess_test_takeover_api"
    ctrl.execution_mode = "PRODUCTION"
    ctrl.state_detail = "LOCAL_READY"

    try:
        # 1. 触发接管
        resp = client.post("/api/v1/live/takeover", json={"action": "takeover", "operator": "op_test"})
        assert resp.status_code == 200
        assert resp.json()["code"] == 0
        assert ctrl.state_detail == "HUMAN_TAKEOVER"

        # 2. 释放接管
        resp2 = client.post("/api/v1/live/takeover", json={"action": "release", "operator": "op_test"})
        assert resp2.status_code == 200
        assert resp2.json()["code"] == 0
        assert ctrl.state_detail == "LOCAL_READY"

        # 3. 手动暂停 AI 与恢复
        resp3 = client.post("/api/v1/live/pause-ai")
        assert resp3.status_code == 200
        assert ctrl.state_detail == "AI_PAUSED"

        resp4 = client.post("/api/v1/live/resume-ai")
        assert resp4.status_code == 200
        assert ctrl.state_detail == "LOCAL_READY"
    finally:
        ctrl.is_live = False


def test_tts_failure_in_production_enters_ai_paused():
    """在 PRODUCTION 商业档位下，TTS 合成异常禁止静默切换 Edge-TTS，必须进入 AI_PAUSED"""
    async def _run():
        ctrl = global_live_controller
        ctrl.is_live = True
        ctrl.execution_mode = "PRODUCTION"
        ctrl.state_detail = "LOCAL_READY"
        ctrl.session_id = "sess_test_tts_paused"
        ctrl.event_queue.reset_interrupt()

        mock_driver = MagicMock()
        mock_driver.synthesize_stream = MagicMock(side_effect=RuntimeError("GPU 显存不足，TTS 驱动宕机"))
        mock_driver.is_progressive_pcm = False
        ctrl.tts_driver = mock_driver

        mock_role = MagicMock()
        mock_role.role_name = "金牌主播"
        mock_role.role_type = "ecommerce"
        mock_role.speech_speed = 1.0

        with patch.object(ctrl, "_switch_to_edge_tts", new_callable=AsyncMock) as mock_switch:
            # 模拟执行 _speak_sentence 遇到 TTS 故障
            await ctrl._speak_sentence("测试口播句子", active_role=mock_role)

            # 验证禁止静默降级为 Edge-TTS
            assert mock_switch.call_count == 0
            # 验证已安全停顿为 AI_PAUSED
            assert ctrl.state_detail == "AI_PAUSED"

        ctrl.is_live = False

    asyncio.run(_run())
