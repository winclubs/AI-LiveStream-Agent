import asyncio
import time
from unittest.mock import AsyncMock, MagicMock, patch

from server.core.media.g2p_viseme import G2PVisemeTimeline, text_to_viseme_sequence, VISEME_MAP
from server.adapters.media.musetalk_driver import ProceduralAvatarDriver, Live2DDriver, NeuralLipSyncDriver
from server.routes.live import LiveSessionController, _build_external_publish_status, _stop_live_unlocked


def test_g2p_viseme_sequence_and_timeline():
    """验证文本 G2P 汉字到拼音声韵母映射、时间线对齐与 0.65/0.35 协同发音平滑"""
    text = "主播这款多少钱"
    seq = text_to_viseme_sequence(text)
    assert len(seq) > 0
    # 验证含有有效音素 (例如 A, O, U, E_I 等)
    tags = [t[0] for t in seq]
    assert any(t in ("A", "O", "U", "E_I") for t in tags)

    g2p = G2PVisemeTimeline(fps=25.0, smooth_alpha=0.35)
    timeline = g2p.generate_timeline(text, total_duration_sec=1.0)
    # 1.0s @ 25fps 应该生成 25 帧
    assert len(timeline) == 25
    for m_open, m_form in timeline:
        assert 0.0 <= m_open <= 1.0
        assert -1.0 <= m_form <= 1.0

    # 验证平滑过渡：第一帧应受 REST (0, 0) 平滑影响，不会瞬间突变
    first_open, _ = timeline[0]
    assert first_open < 0.9


def test_avatar_driver_capabilities():
    """验证驱动层标准接口与机器可读能力清单上报 (ADR-16)"""
    procedural = ProceduralAvatarDriver()
    caps = procedural.get_capabilities()
    assert caps["driver"] == "procedural_avatar"
    assert caps["capabilities"]["neural_lipsync"] is False
    assert caps["capabilities"]["viseme_lipsync"] is True
    assert caps["capabilities"]["g2p_aligned"] is True

    live2d = Live2DDriver()
    l2d_caps = live2d.get_capabilities()
    assert l2d_caps["driver"] == "live2d_avatar"
    assert l2d_caps["available"] is False
    assert l2d_caps["capabilities"]["neural_lipsync"] is False

    neural = NeuralLipSyncDriver()
    neural_caps = neural.get_capabilities()
    assert neural_caps["driver"] == "neural_lipsync_avatar"
    assert neural_caps["available"] is False
    assert neural_caps["capabilities"]["neural_lipsync"] is False


def test_fail_safe_closure_cleanup_and_context():
    """验证 stop() 的闭包 Fail-Safe 清理机制与 products 基础上下文保留"""
    async def _run():
        controller = LiveSessionController()
        controller.is_live = True
        controller.session_id = "test_session_123"
        controller.live_context = {"products": [{"id": 1, "name": "测试商品"}]}

        # 执行 stop 流程
        errors = await controller.stop()
        assert controller.is_live is False
        assert controller.session_id is None
        # 验证 products 字段依然存在，绝不被破坏
        assert "products" in controller.live_context
        assert isinstance(controller.live_context["products"], list)

    asyncio.run(_run())


def test_ingest_event_consistency_and_mock_isolation():
    """验证统一事件入口 ingest_event：真实事件计入指标，Mock 仿真事件完全隔离"""
    async def _run():
        controller = LiveSessionController()
        controller.is_live = True
        controller._stats_reset()

        # 1. 注入真实打赏事件
        res_real = await controller.ingest_event(
            event_type="gift",
            user_name="真实金主",
            payload={"gift_name": "超级火箭", "count": 1, "total_coin": 10000},
            priority=0,
            source="real",
            is_mock=False,
        )
        assert res_real["accepted"] is True
        assert controller.stats["gift_count"] == 1
        assert controller.stats["gift_income_yuan"] == 10.0

        # 2. 注入 Mock 仿真打赏事件
        res_mock = await controller.ingest_event(
            event_type="gift",
            user_name="仿真机器人",
            payload={"gift_name": "超级火箭", "count": 1, "total_coin": 50000},
            priority=0,
            source="mock",
            is_mock=True,
        )
        assert res_mock["accepted"] is True
        # 核心保障：正式运营指标严禁增加
        assert controller.stats["gift_count"] == 1
        assert controller.stats["gift_income_yuan"] == 10.0

        await controller.stop()

    asyncio.run(_run())


def test_obs_ownership_protection():
    """验证推流所有权：仅停止由 Agent 自身启动的 OBS 推流，不停止用户手动推流"""
    async def _run():
        controller = LiveSessionController()
        controller.is_live = True
        controller.session_id = "test_obs_owner"

        mock_obs = MagicMock()
        mock_obs.is_connected = True
        mock_obs.is_streaming = True
        mock_obs.stop_stream = AsyncMock()

        # 场景 A: Agent 未启动推流 (用户自己开启)
        controller.obs_stream_started_by_agent = False
        mock_db = AsyncMock()
        mock_db.get.return_value = None

        with patch("server.routes.live.global_obs_client", mock_obs), \
             patch("server.routes.live.global_live_controller", controller):
            await _stop_live_unlocked(mock_db)
            # 绝不调用 stop_stream
            mock_obs.stop_stream.assert_not_called()

        # 场景 B: 由 Agent 启动推流
        controller.is_live = True
        controller.session_id = "test_obs_owner_2"
        controller.obs_stream_started_by_agent = True
        with patch("server.routes.live.global_obs_client", mock_obs), \
             patch("server.routes.live.global_live_controller", controller):
            await _stop_live_unlocked(mock_db)
            # 必须联动关停推流
            mock_obs.stop_stream.assert_called_once()
            assert controller.obs_stream_started_by_agent is False

    asyncio.run(_run())


def test_three_tier_external_publish_status():
    """验证三层状态解耦：未接入平台 API 前 platform_live 诚实为 None，绝不过度宣称"""
    mock_obs = MagicMock()
    mock_obs.get_summary.return_value = {
        "is_connected": True,
        "is_streaming": True,
        "output_bytes": 1024,
    }
    with patch("server.routes.live.global_obs_client", mock_obs):
        pub = _build_external_publish_status()
        assert pub["mode"] == "obs_websocket"
        assert pub["transport_status"] == "active"
        assert pub["platform_live"] is None
        assert pub["validation"] == "obs_output_only"
