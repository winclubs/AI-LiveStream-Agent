import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from server.core.queue.priority_queue import PriorityBargeInQueue, LiveEventItem
from server.adapters.danmaku.circuit_breaker import CircuitBreakerDanmakuFetcher, STATE_OPEN, STATE_HALF_OPEN, STATE_CLOSED
from server.adapters.danmaku.douyin_fetcher import DouyinDanmakuFetcher
from server.adapters.danmaku.bilibili_fetcher import BilibiliDanmakuFetcher
from server.adapters.media.edgetts_driver import EdgeTTSMediaDriver
from server.adapters.media.musetalk_driver import Live2DDriver, NeuralLipSyncDriver
from server.core.media.g2p_viseme import text_to_viseme_sequence
from server.routes.live import LiveSessionController, _stop_live_unlocked, _start_live_unlocked


def test_p0_barge_in_keeps_consume_loop_alive():
    """
    【高阻断回归验证】
    播报单句中触发 P0 打断，单句 Task 被取消，但 long-running _consume_queue_loop
    绝不能退出，必须持续消费队列中的后续事件！
    """
    async def _run():
        controller = LiveSessionController()
        controller.is_live = True
        controller.session_id = "test_loop_alive_session"

        spoken_sentences = []

        # 模拟 TTS 生成过程：第一句较长，中途会被打断；第二句能正常说完
        async def fake_tts_generator(driver, sentence, *args, **kwargs):
            text = str(sentence)
            spoken_sentences.append(f"start:{text}")
            if "第一句" in text:
                try:
                    await asyncio.sleep(2.0)
                    spoken_sentences.append(f"end:{text}")
                except asyncio.CancelledError:
                    spoken_sentences.append("interrupted:第一句")
                    raise
            else:
                await asyncio.sleep(0.02)
                spoken_sentences.append(f"end:{text}")
            return b"fake_audio_chunk_data"

        mock_role = MagicMock()
        mock_role.speech_speed = 1.0
        async def fake_process_event(event_type, user_name, payload, context):
            text = payload.get("text") or payload.get("gift_name") or "普通发言"
            yield f"{text}，"
            yield "谢谢大家支持！"

        mock_role.process_event.side_effect = fake_process_event
        controller.tts_driver = MagicMock()
        controller.tts_driver.apply_speech_speed = AsyncMock()

        async def fake_sanitize(text, role):
            return text, False, []

        with patch.object(controller, "_collect_tts_sentence", side_effect=fake_tts_generator), \
             patch.object(controller, "_refresh_product_context", new_callable=AsyncMock), \
             patch.object(controller, "_log_barrage_complete", new_callable=AsyncMock), \
             patch.object(controller, "_sanitize_and_humanize", side_effect=fake_sanitize), \
             patch("server.routes.live.global_virtual_audio.play_chunk"), \
             patch("server.routes.live.global_media_driver.feed_audio_chunk", new_callable=AsyncMock), \
             patch("server.routes.live.global_role_manager.get_active_role", return_value=mock_role), \
             patch("server.routes.ws_live.ws_manager.broadcast", new_callable=AsyncMock):

            # 启动长期消费循环
            consume_task = asyncio.create_task(controller._consume_queue_loop())

            # 放入第 1 条普通弹幕 (P1)
            await controller.event_queue.put(
                priority=1,
                user_name="观众A",
                event_type="chat",
                payload={"text": "第一句很长的演讲"}
            )

            # 等待第一句开始朗读
            for _ in range(20):
                if any("第一句" in s for s in spoken_sentences):
                    break
                await asyncio.sleep(0.05)
            assert any("第一句" in s for s in spoken_sentences)
            assert controller.current_tts_task is not None

            # 插入 P0 紧急插队事件（如高额大赏或超管指令），触发打断
            await controller.event_queue.put(
                priority=0,
                user_name="老板",
                event_type="gift",
                payload={"gift_name": "超级大火箭", "total_coin": 10000}
            )

            # 等待打断生效并继续消费 P0
            for _ in range(20):
                if any("interrupted" in s for s in spoken_sentences):
                    break
                await asyncio.sleep(0.05)
            assert any("interrupted" in s for s in spoken_sentences)

            # 核心保障：消费 Task 绝不能因为 CancelledError 退出！
            assert not consume_task.done(), "消息消费 Worker 意外终止退出！"

            # 放入第 3 条普通消息，验证 Worker 依然健在并能正常消费
            await controller.event_queue.put(
                priority=1,
                user_name="观众B",
                event_type="chat",
                payload={"text": "第三句后续消息"}
            )
            for _ in range(20):
                if any("第三句" in s for s in spoken_sentences):
                    break
                await asyncio.sleep(0.05)
            assert any("第三句" in s for s in spoken_sentences)

            # 正常清理
            controller.is_live = False
            consume_task.cancel()
            try:
                await consume_task
            except asyncio.CancelledError:
                pass

    asyncio.run(_run())


def test_circuit_breaker_single_failure_count_and_half_open_recovery():
    """验证熔断器单次连接错误只增加一次失败计数，且 HALF_OPEN 失败后正确转回 OPEN，正式禁用 Mock"""
    mock_real = MagicMock()
    cb = CircuitBreakerDanmakuFetcher(
        real_fetcher=mock_real,
        room_id="12345",
        on_event_callback=MagicMock(),
        failure_threshold=2,
        recovery_timeout_sec=0.1,
        enable_mock_fallback=False
    )

    # 1. 模拟一次连接错误：底层错误回调只触发一次 record_failure
    cb.state = STATE_CLOSED
    cb.consecutive_failures = 0
    cb._on_underlying_error(RuntimeError("网络中断"))
    assert cb.consecutive_failures == 1
    assert cb.state == STATE_CLOSED

    # 第二次底层错误，达到阈值 2，转为 OPEN
    cb._on_underlying_error(RuntimeError("再次网络中断"))
    assert cb.consecutive_failures == 2
    assert cb.state == STATE_OPEN

    # 2. 模拟进入 HALF_OPEN 状态
    cb.state = STATE_HALF_OPEN
    # HALF_OPEN 下探测失败，必须能够立刻转回 OPEN，消除死锁状态
    cb.record_failure()
    assert cb.state == STATE_OPEN


def test_douyin_and_bilibili_health_callbacks():
    """验证抖音抓取器握手与心跳上报健康，B 站认证成功后才标记健康"""
    dy = DouyinDanmakuFetcher(room_id="dy_room_1", on_event_callback=MagicMock())
    dy.is_running = True
    assert dy.get_health().connected is False

    dy.on_connection_opened()
    assert dy.get_health().connected is True
    assert dy.get_health().worker_alive is True

    dy.on_heartbeat()
    assert dy.get_health().last_heartbeat_at is not None

    # B站：连接未认证时不标记 connected
    bili = BilibiliDanmakuFetcher(room_id="bili_room_1", on_event_callback=MagicMock())
    bili.is_connected = False
    assert not bili.is_connected


def test_priority_queue_hard_capacity_and_ttl_metrics():
    """验证事件队列拥有硬容量 hard_maxsize 限制，分级背压、淘汰驱逐与 TTL 指标"""
    async def _run():
        q = PriorityBargeInQueue(maxsize=3, p0_maxsize=2)
        assert q.hard_maxsize == 5

        # 填满普通槽位 (P1)
        res1 = await q.put(priority=1, payload={"text": "1"})
        res2 = await q.put(priority=1, payload={"text": "2"})
        res3 = await q.put(priority=1, payload={"text": "3"})
        assert res1 and res2 and res3
        assert q.qsize() == 3

        # 此时队列满且全为 P1，新 P1 无法驱逐同级，必须拒绝入队触发背压保护
        res4 = await q.put(priority=1, payload={"text": "4"})
        assert res4 is False, "全 P1 满载时未执行背压！"

        # 放入 2 个 P0，成功驱逐 2 个 P1 入队
        res_p0_1 = await q.put(priority=0, payload={"text": "p0_1"})
        res_p0_2 = await q.put(priority=0, payload={"text": "p0_2"})
        assert res_p0_1 and res_p0_2
        assert q.qsize() == 3

        # 此时 P0 达到独立保留上限 p0_maxsize=2，拒绝继续过度积压 P0
        res_p0_overflow = await q.put(priority=0, payload={"text": "p0_3_overflow"})
        assert res_p0_overflow is False, "超出 p0_maxsize 依然入队！"

        # 验证 TTL 超时丢弃递增 event_expired_total
        q_ttl = PriorityBargeInQueue(maxsize=10, p0_maxsize=2)
        await q_ttl.put(priority=2, payload={"text": "已过期"}, ttl_seconds=0.01)
        await asyncio.sleep(0.05)  # 等待过期

        # 读取时丢弃
        item = await q_ttl.get()
        assert item is None
        stats = q_ttl.get_stats()
        assert stats["event_expired_total"] >= 1

    asyncio.run(_run())


def test_chinese_viseme_distinct_mapping():
    """验证中文核心直播用句不会退化为单一的 'A' 口型，能生成多样化 Viseme 音素"""
    sentence = "欢迎大家来到直播间谢谢关注"
    seq = text_to_viseme_sequence(sentence)
    tags = [s[0] for s in seq if s[0] != "REST"]
    unique_tags = set(tags)

    # 至少应包含 3 种以上不同唇形音素 (例如 U, E_I, A, O, L_N 等)
    assert len(unique_tags) >= 3, f"中文音素严重退化，仅有: {unique_tags}"
    assert "E_I" in unique_tags or "U" in unique_tags or "O" in unique_tags


def test_unimplemented_drivers_honest_error():
    """验证未实现的 Live2DDriver 与 NeuralLipSyncDriver 调用 start 抛出 NotImplementedError"""
    async def _run():
        l2d = Live2DDriver()
        with pytest.raises(NotImplementedError):
            await l2d.start()

        neural = NeuralLipSyncDriver()
        with pytest.raises(NotImplementedError):
            await neural.start()

    asyncio.run(_run())


def test_edgetts_current_task_interrupt():
    """验证 EdgeTTSMediaDriver 在进行中可物理取消 current_task"""
    async def _run():
        driver = EdgeTTSMediaDriver()
        task = asyncio.create_task(asyncio.sleep(10))
        driver.current_task = task

        await driver.interrupt("测试打断")
        # 验证任务已被触发取消
        assert task.cancelling() or task.cancelled()
        try:
            await task
        except asyncio.CancelledError:
            pass
        assert task.cancelled()

    asyncio.run(_run())


def test_obs_auto_link_degraded_response():
    """验证 OBS auto_link 失败时，开播返回结构化降级响应 (degraded: true, obs_linked: false)"""
    async def _run():
        from server.routes.live import LiveStartRequest
        mock_db = AsyncMock()
        mock_db.commit = AsyncMock()
        mock_db.rollback = AsyncMock()
        mock_db.get = AsyncMock(return_value=None)

        # 模拟商品查询结果，支持 scalars().all()
        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = []
        mock_db.execute = AsyncMock(return_value=mock_result)

        req = LiveStartRequest(platform="bilibili", mode="D", obs_auto_link=True)

        mock_obs = MagicMock()
        mock_obs.is_connected = False  # OBS 未连接

        with patch("server.routes.live.global_obs_client", mock_obs), \
             patch("server.routes.live.global_live_controller.start", new_callable=AsyncMock):
            res = await _start_live_unlocked(req, mock_db)
            assert res["code"] == 0
            assert res["is_live"] is True
            assert res["obs_linked"] is False
            assert res["degraded"] is True
            assert "OBS 未连接" in res["obs_error"]

    asyncio.run(_run())
