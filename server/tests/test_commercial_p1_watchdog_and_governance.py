# -*- coding: utf-8 -*-
"""
server/tests/test_commercial_p1_watchdog_and_governance.py
商业化运营改造方案 P1 级别质量看门狗、灰度熔断与成本隐私治理全覆盖测试
"""

import asyncio
import time
import threading
import pytest
from unittest.mock import MagicMock, AsyncMock, patch
from fastapi import HTTPException
from starlette.testclient import TestClient

from server.core.monitoring.media_quality_watchdog import MediaQualityWatchdog
from server.core.compliance.kill_switch import FeatureKillSwitchManager, global_kill_switch
from server.core.compliance.cost_privacy_manager import CostAndPrivacyManager


def test_media_quality_watchdog_sli_calculation_and_slo_violations():
    """测试 1: 媒体质量看门狗 SLI/SLO 计算、连续黑帧侦测与排障 Runbook 生成"""
    degraded_reasons = []
    watchdog = MediaQualityWatchdog(on_degraded_callback=lambda r: degraded_reasons.append(r))
    watchdog.start()
    assert watchdog.is_monitoring is True

    # 1. 模拟正常渲染 25 帧 (耗时约 1 秒)
    now = time.time()
    for i in range(25):
        watchdog._frame_render_times.append(now + (i * 0.04))
        watchdog._total_rendered_frames += 1
        watchdog.record_av_drift(25.0)  # 25ms 正常漂移

    fps = watchdog.calculate_effective_fps()
    assert 23.0 <= fps <= 27.0
    eval_res = watchdog.evaluate_slo()
    assert eval_res["status"] == "HEALTHY"
    assert len(eval_res["violations"]) == 0

    # 2. 模拟注入严重丢帧 (丢帧率 > 20%)
    watchdog.record_frame_dropped(15)  # 15 / (25 + 15) = 37.5%
    drop_rate = watchdog.calculate_drop_rate()
    assert drop_rate >= 0.20
    eval_res = watchdog.evaluate_slo()
    assert eval_res["status"] == "CRITICAL"
    assert any(v["code"] == "DROP_FRAME_HIGH" for v in eval_res["violations"])
    # 确认包含了排障 Runbook
    assert "Runbook-M02" in eval_res["violations"][0]["runbook"]

    # 3. 模拟连续黑帧达到 10 帧触发阻断
    for _ in range(10):
        watchdog.record_frame_anomaly(is_black=True)
    assert any("黑屏" in r for r in degraded_reasons)
    eval_res = watchdog.evaluate_slo()
    assert any(v["code"] == "ANOMALOUS_FRAMES" for v in eval_res["violations"])

    # 4. 确认结构化看板快照
    snapshot = watchdog.get_sli_snapshot()
    assert "sli_metrics" in snapshot
    assert "slo_evaluation" in snapshot
    assert len(snapshot["recent_alerts"]) > 0

    watchdog.stop()
    assert watchdog.is_monitoring is False


def test_anomaly_degradation_is_edge_latched_and_rearms_on_healthy_sample():
    """测试 2: 连续异常只广播一次降级；健康抽检样本重新武装闩锁

    7x24 无人值守下，30 次连续黑帧曾触发 21 次降级回调与广播刷屏；
    闩锁后同一异常段只广播一次，且恢复后的新异常段仍可触发。
    """
    degraded_reasons = []
    watchdog = MediaQualityWatchdog(on_degraded_callback=lambda r: degraded_reasons.append(r))
    watchdog.start()

    for _ in range(30):
        watchdog.record_frame_anomaly(is_black=True)
    assert len(degraded_reasons) == 1
    assert watchdog._anomaly_degraded_latched is True

    # 正常帧是帧流事件，不得清空抽检判定的连续异常计数
    for _ in range(30):
        watchdog.record_frame_rendered()
    assert watchdog._consecutive_black_frames == 30

    # 健康抽检样本：计数清零并解除闩锁
    for _ in range(30):
        watchdog.record_frame_anomaly()
    assert watchdog._consecutive_black_frames == 0
    assert watchdog._anomaly_degraded_latched is False
    assert len(degraded_reasons) == 1

    for _ in range(watchdog.MAX_CONSECUTIVE_ANOMALOUS_FRAMES):
        watchdog.record_frame_anomaly(is_black=True)
    assert len(degraded_reasons) == 2

    watchdog.stop()


def test_frame_stall_triggers_single_degradation_and_rearms_on_resume():
    """测试 3: 视频帧停滞（渲染链路无帧可发）触发 MEDIA_DEGRADED

    帧抽检只在有帧发布时才会运行，因此断流/卡死必须由独立计时器判定；
    同一段停滞只广播一次，帧恢复后重新武装。
    """
    degraded_reasons = []
    watchdog = MediaQualityWatchdog(on_degraded_callback=lambda r: degraded_reasons.append(r))
    watchdog.start()
    try:
        watchdog.set_stream_output_ready(True)
        base = watchdog._last_frame_monotonic
        assert base is not None

        assert watchdog.check_frame_stall(now=base + 1.0) is False
        assert degraded_reasons == []

        assert watchdog.check_frame_stall(now=base + watchdog.FRAME_STALL_SECONDS) is True
        assert len(degraded_reasons) == 1
        assert "FRAME_STALL" in watchdog._recent_alerts[-1]["code"]
        assert "Runbook-M07" in watchdog._recent_alerts[-1]["runbook"]

        # 停滞持续：闩锁生效，不得按检测周期刷屏
        assert watchdog.check_frame_stall(now=base + 3600) is False
        assert len(degraded_reasons) == 1

        # 停滞期间 /metrics 必须暴露违规项与帧龄 (按真实时钟回拨最后一帧时间)
        watchdog._last_frame_monotonic -= 3600
        eval_res = watchdog.evaluate_slo()
        assert any(v["code"] == "FRAME_STALL" for v in eval_res["violations"])
        assert eval_res["frame_age_ms"] >= watchdog.FRAME_STALL_SECONDS * 1000
        assert "frame_age_ms" in watchdog.get_sli_snapshot()["sli_metrics"]

        # 帧恢复：闩锁解除，第二段停滞仍能触发
        watchdog.record_frame_rendered()
        resumed = watchdog._last_frame_monotonic
        assert watchdog.check_frame_stall(now=resumed + 0.1) is False
        assert watchdog.check_frame_stall(now=resumed + watchdog.FRAME_STALL_SECONDS) is True
        assert len(degraded_reasons) == 2
    finally:
        watchdog.stop()

    # 停止后不再判定
    assert watchdog.check_frame_stall(now=base + 3600) is False
    assert len(degraded_reasons) == 2


def test_frame_stall_timer_thread_detects_silence():
    """测试 4: 后台计时器在真实时间轴上自动检出无帧停滞"""
    degraded_reasons = []
    watchdog = MediaQualityWatchdog(
        on_degraded_callback=lambda r: degraded_reasons.append(r),
        frame_stall_seconds=0.05,
        check_interval_seconds=0.02,
    )
    watchdog.start()
    deadline = time.monotonic() + 2.0
    watchdog.set_stream_output_ready(True)
    while time.monotonic() < deadline and not degraded_reasons:
        time.sleep(0.01)
    watchdog.stop()

    assert len(degraded_reasons) == 1
    assert "FRAME_STALL" in watchdog._recent_alerts[-1]["code"]


def test_stall_degradation_callback_dispatched_on_event_loop():
    """测试 5: 计时器线程触发的降级回调必须回到事件循环线程派发

    回调内部使用 asyncio.create_task，在计时器线程直接调用会丢事件并静默失败。
    """
    main_thread = threading.current_thread()
    observations = []

    async def _run():
        watchdog = MediaQualityWatchdog(
            on_degraded_callback=lambda reason: observations.append((threading.current_thread(), reason)),
            frame_stall_seconds=0.05,
            check_interval_seconds=0.02,
        )
        watchdog.start()
        await asyncio.sleep(0.05)
        watchdog.set_stream_output_ready(True)
        await asyncio.sleep(0.25)
        watchdog.stop()

    asyncio.run(_run())

    assert observations
    assert observations[0][0] is main_thread


def test_stall_threshold_follows_execution_mode():
    """测试 6: 帧停滞门限按运行档位分级，未知档位从严"""
    watchdog = MediaQualityWatchdog()

    watchdog.set_execution_mode("PRODUCTION")
    assert watchdog.frame_stall_seconds == watchdog.FRAME_STALL_SECONDS
    watchdog.set_execution_mode("PILOT")
    assert watchdog.frame_stall_seconds == watchdog.FRAME_STALL_SECONDS
    # 仅本地预览：观众不可见，门限放宽
    watchdog.set_execution_mode("PREVIEW")
    assert watchdog.frame_stall_seconds == watchdog.PREVIEW_STALL_SECONDS
    # 仿真档位无真实输出，直接关闭判定
    watchdog.set_execution_mode("DEMO")
    assert watchdog.frame_stall_seconds is None
    # 未知档位必须按最严处理（fail-closed）
    watchdog.set_execution_mode("SOMETHING_NEW")
    assert watchdog.frame_stall_seconds == watchdog.FRAME_STALL_SECONDS


def test_stall_policy_disabled_in_demo_mode():
    """测试 7: DEMO 档位关闭停滞判定，长时间无帧也不降级"""
    degraded_reasons = []
    watchdog = MediaQualityWatchdog(on_degraded_callback=lambda r: degraded_reasons.append(r))
    watchdog.set_execution_mode("DEMO")
    watchdog.set_stream_output_ready(True)
    watchdog.start()
    try:
        base = watchdog._last_frame_monotonic
        assert watchdog.check_frame_stall(now=base + 3600) is False
        assert degraded_reasons == []
        snapshot = watchdog.get_sli_snapshot()["sli_metrics"]
        assert snapshot["stall_policy_enabled"] is False
        assert snapshot["frame_stall_seconds"] is None
    finally:
        watchdog.stop()


def test_stall_not_judged_until_stream_output_ready():
    """测试 8: 推流输出未就绪时不判定，输出就绪瞬间重新起算避免开播竞态误判"""
    degraded_reasons = []
    watchdog = MediaQualityWatchdog(on_degraded_callback=lambda r: degraded_reasons.append(r))
    watchdog.set_execution_mode("PRODUCTION")
    watchdog.start()
    try:
        base = watchdog._last_frame_monotonic

        assert watchdog.check_frame_stall(now=base + 3600) is False
        assert degraded_reasons == []

        watchdog.set_stream_output_ready(True)
        armed = watchdog._last_frame_monotonic
        assert armed >= base
        # 开播瞬间重新起算：即便上一帧已很久以前也不误判
        assert watchdog.check_frame_stall(now=armed + 0.1) is False

        assert watchdog.check_frame_stall(now=armed + 3600) is True
        assert len(degraded_reasons) == 1

        # 输出撤下后停止判定，不再产生新降级
        watchdog.set_stream_output_ready(False)
        assert watchdog.check_frame_stall(now=armed + 7200) is False
        assert len(degraded_reasons) == 1

        # 输出恢复后重新武装，第二段断流仍可判定
        watchdog.set_stream_output_ready(True)
        rearmed = watchdog._last_frame_monotonic
        assert watchdog.check_frame_stall(now=rearmed + 3600) is True
        assert len(degraded_reasons) == 2
    finally:
        watchdog.stop()


def test_stream_output_readiness_probe_gates_stall_judgement():
    """测试 9: 外部就绪探针（RTMP/虚拟摄像头/WebRTC/录制）门控停滞判定"""
    state = {"ready": False}
    degraded_reasons = []
    watchdog = MediaQualityWatchdog(
        on_degraded_callback=lambda r: degraded_reasons.append(r),
        output_readiness_probe=lambda: state["ready"],
    )
    watchdog.start()
    try:
        base = watchdog._last_frame_monotonic
        assert watchdog.check_frame_stall(now=base + 3600) is False
        assert degraded_reasons == []

        state["ready"] = True
        assert watchdog.check_frame_stall(now=base + 3600) is True
        assert len(degraded_reasons) == 1
    finally:
        watchdog.stop()


def test_configure_media_watchdog_applies_mode_policy_and_probe():
    """测试 10: 开播时按档位配置门限策略并挂载推流输出就绪探针"""
    from server.core.monitoring.media_quality_watchdog import global_media_watchdog
    from server.routes.live import _configure_media_watchdog, _stream_output_ready_probe

    try:
        _configure_media_watchdog("PILOT")
        assert global_media_watchdog.execution_mode == "PILOT"
        assert global_media_watchdog.frame_stall_seconds == MediaQualityWatchdog.FRAME_STALL_SECONDS
        # 探针在无任何输出通道的测试环境下必须为 False（不得凭空判定就绪）
        assert _stream_output_ready_probe() is False
    finally:
        global_media_watchdog.set_execution_mode("PREVIEW")
        global_media_watchdog.set_output_readiness_probe(None)
        global_media_watchdog.set_stream_output_ready(False)


def test_stream_output_ready_probe_reflects_real_sinks(monkeypatch):
    """测试 11: 就绪探针如实反映真实输出通道状态"""
    from server.core.media import recorder as recorder_module
    from server.core.media import rtmp_streamer as rtmp_module
    from server.core.media import virtual_cam as cam_module
    from server.routes.live import _stream_output_ready_probe

    monkeypatch.setattr(type(rtmp_module.global_rtmp_streamer), "is_streaming", property(lambda self: False))
    monkeypatch.setattr(cam_module.global_virtual_cam, "is_active", False)
    monkeypatch.setattr(recorder_module.get_record_manager(), "is_recording", lambda: False)
    assert _stream_output_ready_probe() is False

    monkeypatch.setattr(type(rtmp_module.global_rtmp_streamer), "is_streaming", property(lambda self: True))
    assert _stream_output_ready_probe() is True

    monkeypatch.setattr(type(rtmp_module.global_rtmp_streamer), "is_streaming", property(lambda self: False))
    monkeypatch.setattr(cam_module.global_virtual_cam, "is_active", True)
    assert _stream_output_ready_probe() is True


def test_kill_switch_multi_level_resolution_and_emergency_cut():
    """测试 12: 灰度 Feature Flag 多层级优先级匹配与紧急一键熔断"""
    manager = FeatureKillSwitchManager()

    # 默认全部功能开启
    assert manager.is_feature_enabled("commerce_agent", platform="douyin", anchor_id="anchor_01") is True
    assert manager.is_feature_enabled("stream_push", platform="kuaishou") is True

    # 1. 主播级独立覆盖：关闭 anchor_01 的带货促单
    manager.set_switch("commerce_agent", False, anchor_id="anchor_01", operator="risk_ctrl", reason="主播商品资质审核中")
    assert manager.is_feature_enabled("commerce_agent", platform="douyin", anchor_id="anchor_01") is False
    # 其他主播不受影响
    assert manager.is_feature_enabled("commerce_agent", platform="douyin", anchor_id="anchor_02") is True

    # 2. 平台级覆盖：关闭 kuaishou 的推流
    manager.set_switch("stream_push", False, platform="kuaishou", operator="ops", reason="快手接口升级维护")
    assert manager.is_feature_enabled("stream_push", platform="kuaishou") is False
    assert manager.is_feature_enabled("stream_push", platform="douyin") is True

    # 3. 版本与模型哈希完整性校验门禁
    ok, reason = manager.validate_and_enforce_version_manifest(
        anchor_id="anchor_03",
        platform="douyin",
        current_version="3.0.0",
        min_required_version="3.2.0",
        expected_model_hashes={"unet": "hash_abc"},
        actual_model_hashes={"unet": "hash_abc"},
    )
    assert ok is False
    assert "低于商业发布准入最低版本" in reason
    # 自动切断推流开关
    assert manager.is_feature_enabled("stream_push", platform="douyin", anchor_id="anchor_03") is False

    # 模型哈希不匹配防篡改阻断
    ok, reason = manager.validate_and_enforce_version_manifest(
        anchor_id="anchor_04",
        platform="douyin",
        current_version="3.5.0",
        min_required_version="3.0.0",
        expected_model_hashes={"unet": "expected_hash"},
        actual_model_hashes={"unet": "tampered_hash"},
    )
    assert ok is False
    assert "哈希校验失败" in reason
    assert manager.is_feature_enabled("stream_push", platform="douyin", anchor_id="anchor_04") is False

    # 4. 一键紧急熔断全局/平台所有功能
    manager.emergency_kill_all(platform="douyin", reason="收到平台官方高危风控预警")
    for feat in manager.SUPPORTED_FEATURES:
        assert manager.is_feature_enabled(feat, platform="douyin") is False
    # 其他平台依然保留自身状态
    assert manager.is_feature_enabled("danmaku_interaction", platform="bilibili") is True


def test_start_live_blocked_by_kill_switch():
    """测试 3: 当推流被 Kill Switch 熔断时，开播流程强阻断并返回 HTTP 403"""
    from server.routes.live import _start_live_unlocked, LiveStartRequest

    async def _run():
        req = LiveStartRequest(
            platform="douyin",
            room_id="123456",
            anchor_id="blocked_anchor",
            execution_mode="PILOT",
        )
        # 设置针对该主播的推流熔断
        global_kill_switch.set_switch(
            "stream_push", False, platform="douyin", anchor_id="blocked_anchor",
            operator="admin", reason="安全风控阻断"
        )

        mock_db = AsyncMock()
        mock_role = MagicMock()
        mock_role.role_id = "role_001"
        mock_anchor = MagicMock()
        mock_anchor.voice_id = None
        mock_anchor.avatar_asset_dir = None
        mock_anchor.photo_portrait = None

        with patch("server.routes.live.global_role_manager.get_active_role", return_value=mock_role), \
             patch("server.routes.settings._get_setting", return_value="B"), \
             patch("server.core.operations.preflight.global_preflight_service.run_preflight") as mock_pf:
            # 即使 preflight 通过，Kill Switch 也必须强行阻断
            report_mock = MagicMock()
            report_mock.passed = True
            mock_pf.return_value = report_mock

            mock_db.get.return_value = mock_anchor
            exec_mock = MagicMock()
            exec_mock.scalars.return_value.all.return_value = []
            mock_db.execute.return_value = exec_mock

            with pytest.raises(HTTPException) as exc_info:
                await _start_live_unlocked(req, mock_db)
            assert exc_info.value.status_code == 403
            assert exc_info.value.detail["code"] == "KILL_SWITCH_BLOCKED"

        # 测试完成后恢复开关
        global_kill_switch.set_switch("stream_push", True, platform="douyin", anchor_id="blocked_anchor")

    asyncio.run(_run())


def test_cost_metering_budget_cap_and_privacy_erasure():
    """测试 4: 场次成本精确核算、预算超限自动暂停与用户隐私合规抹除"""
    budget_paused_reasons = []
    manager = CostAndPrivacyManager(
        on_budget_exceeded=lambda sess, cost, lim: budget_paused_reasons.append(f"PAUSED: {cost}>{lim}")
    )
    manager.start_session(session_id="sess_cost_001", budget_limit_cny=1.00)

    # 1. 记录用量并推算费用
    # 10,000 prompt tokens = 10 * 0.012 = 0.12 元
    # 10,000 completion tokens = 10 * 0.024 = 0.24 元
    # 10,000 字符 TTS = 10 * 0.005 = 0.05 元
    manager.record_llm_usage(prompt_tokens=10000, completion_tokens=10000)
    manager.record_tts_usage(char_count=10000)
    manager.record_order_conversion(count=2)

    cost_snapshot = manager.calculate_current_cost()
    assert cost_snapshot["session_id"] == "sess_cost_001"
    assert cost_snapshot["llm_cost_cny"] == 0.36
    assert cost_snapshot["tts_cost_cny"] == 0.05
    assert cost_snapshot["orders_count"] == 2
    assert cost_snapshot["cap_exceeded"] is False

    # 2. 注入超额使用量，触发 ¥1.00 预算上限熔断
    # 再加 30,000 completion tokens = 0.72 元，累计突破 1.00 元
    manager.record_llm_usage(prompt_tokens=0, completion_tokens=30000)
    assert manager.cap_exceeded is True
    assert len(budget_paused_reasons) > 0

    # 3. 敏感数据正则脱敏测试
    raw_danmaku = "我的手机号是13812345678，身份证是110101199003072345，请联系我！"
    masked = CostAndPrivacyManager.mask_sensitive_user_info(raw_danmaku)
    assert "138****5678" in masked
    assert "110101********2345" in masked
    assert "13812345678" not in masked

    # 4. 隐私被遗忘权抹除与审计记录
    record = manager.erase_user_data(user_id="user_sensitive_999", operator="dpo_alice", reason="用户注销账号")
    assert record["action"] == "ERASE_USER_DATA"
    assert manager.is_user_erased("user_sensitive_999") is True
    audit_logs = manager.get_privacy_audit_log()
    assert len(audit_logs) >= 1
    assert audit_logs[-1]["operator"] == "dpo_alice"


def test_p1_governance_and_watchdog_rest_apis():
    """测试 5: P1 媒体质量看板、Kill Switch 与成本预算 REST API 端点验证"""
    from server.app import app
    client = TestClient(app)

    # 1. 质量看板与 Runbook 查询
    resp = client.get("/api/v1/live/quality/metrics")
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert "sli_metrics" in data
    assert "slo_evaluation" in data

    resp = client.get("/api/v1/live/quality/runbook")
    assert resp.status_code == 200
    assert "FPS_DEGRADED" in resp.json()["data"]

    # 2. 查询与动态更新 Kill Switch
    resp = client.get("/api/v1/live/governance/kill-switch")
    assert resp.status_code == 200

    resp = client.post("/api/v1/live/governance/kill-switch", json={
        "feature": "danmaku_interaction",
        "enabled": False,
        "platform": "bilibili",
        "operator": "test_ops",
        "reason": "测试禁用B站弹幕",
    })
    assert resp.status_code == 200
    assert resp.json()["data"]["enabled"] is False

    # 3. 成本核算与预算设置
    resp = client.get("/api/v1/live/cost/summary")
    assert resp.status_code == 200

    resp = client.post("/api/v1/live/cost/budget", json={"budget_limit_cny": 88.50})
    assert resp.status_code == 200
    assert resp.json()["data"]["budget_limit_cny"] == 88.50

    # 4. 隐私抹除接口
    resp = client.post("/api/v1/live/privacy/erase-user", json={
        "user_id": "user_api_test_01",
        "operator": "api_tester",
        "reason": "合规抽检抹除",
    })
    assert resp.status_code == 200
    assert resp.json()["data"]["action"] == "ERASE_USER_DATA"
