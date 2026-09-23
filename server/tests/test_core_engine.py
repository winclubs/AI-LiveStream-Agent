import asyncio
import pytest
from server.config import encrypt_secret, decrypt_secret, mask_api_key
from server.core.guardrails.aho_corasick import ProhibitedWordSanitizer
from server.core.queue.priority_queue import PriorityBargeInQueue, BarrageAggregator
from server.core.roles.ecommerce_anchor import EcommerceAnchorRole
from server.core.roles.entertainment_host import EntertainmentHostRole
from server.core.roles.domain_expert import DomainExpertRole

# 1. 测试 AES-256-GCM 加密与解密
def test_aes_encryption_roundtrip():
    original_key = "sk-proj-test1234567890abcdef"
    encrypted = encrypt_secret(original_key)
    assert encrypted != original_key
    assert len(encrypted) > 20

    decrypted = decrypt_secret(encrypted)
    assert decrypted == original_key

    masked = mask_api_key(original_key)
    assert masked.startswith("sk-p")
    assert masked.endswith("cdef")
    assert "••••••••" in masked

# 2. 测试 Aho-Corasick 违禁词平替与阻断
def test_aho_corasick_guardrail():
    sanitizer = ProhibitedWordSanitizer()
    test_words = [
        {"word": "全网第一", "category": "extreme", "role_scope": "all", "action_policy": "substitute", "replacement_word": "深受大家喜爱", "is_enabled": 1},
        {"word": "最好", "category": "extreme", "role_scope": "all", "action_policy": "substitute", "replacement_word": "深受好评", "is_enabled": 1},
        {"word": "秒杀", "category": "extreme", "role_scope": "ecommerce", "action_policy": "substitute", "replacement_word": "限时抢购", "is_enabled": 1},
        {"word": "根治", "category": "medical", "role_scope": "all", "action_policy": "drop", "replacement_word": "", "is_enabled": 1},
    ]
    sanitizer.load_words(test_words)

    # 测试平替
    input_text = "我们这款大衣是全网第一好用的大衣，质量最好！"
    sanitized, hits, is_dropped = sanitizer.sanitize(input_text, current_role="ecommerce")
    assert not is_dropped
    assert len(hits) == 2
    assert "全网第一" not in sanitized
    assert "深受大家喜爱" in sanitized
    assert "深受好评" in sanitized

    # 测试整句阻断
    drop_input = "这款茶饮能彻底根治高血压！"
    sanitized_drop, hits_drop, is_dropped = sanitizer.sanitize(drop_input, current_role="expert")
    assert is_dropped
    assert sanitized_drop == ""

def test_multi_platform_guardrail():
    """测试多平台违禁词规则隔离与动态匹配 (通用规则 + 抖音 + 视频号 + 快手 + B站)"""
    sanitizer = ProhibitedWordSanitizer()
    test_words = [
        # 通用规则
        {"word": "全网第一", "category": "extreme", "role_scope": "all", "platform": "all", "action_policy": "substitute", "replacement_word": "深受大家喜爱", "is_enabled": 1},
        # 抖音专属
        {"word": "加我微信", "category": "traffic", "role_scope": "all", "platform": "douyin", "action_policy": "substitute", "replacement_word": "关注小黄车", "is_enabled": 1},
        {"word": "某宝", "category": "competitor", "role_scope": "all", "platform": "douyin", "action_policy": "drop", "replacement_word": "", "is_enabled": 1},
        # 视频号专属
        {"word": "加我私人微信", "category": "traffic", "role_scope": "all", "platform": "wechat", "action_policy": "drop", "replacement_word": "", "is_enabled": 1},
        # 快手专属
        {"word": "跟老板撕破脸", "category": "sensitive", "role_scope": "all", "platform": "kuaishou", "action_policy": "drop", "replacement_word": "", "is_enabled": 1},
        # B站专属
        {"word": "私下交易账号", "category": "traffic", "role_scope": "all", "platform": "bilibili", "action_policy": "drop", "replacement_word": "", "is_enabled": 1},
    ]
    sanitizer.load_words(test_words)

    # 1. 通用规则在所有平台均触发
    txt_gen = "我们家品质全网第一！"
    res_dy, hits_dy, drop_dy = sanitizer.sanitize(txt_gen, current_role="ecommerce", current_platform="douyin")
    res_bili, hits_bili, drop_bili = sanitizer.sanitize(txt_gen, current_role="entertainment", current_platform="bilibili")
    assert "深受大家喜爱" in res_dy and not drop_dy
    assert "深受大家喜爱" in res_bili and not drop_bili

    # 2. 抖音专属词在抖音触发平替，在视频号/B站不触发 (平台隔离)
    txt_traffic = "想要优惠的朋友可以加我微信咨询"
    res_in_dy, hits_in_dy, drop_in_dy = sanitizer.sanitize(txt_traffic, current_platform="douyin")
    assert "关注小黄车" in res_in_dy and len(hits_in_dy) == 1

    res_in_wx, hits_in_wx, drop_in_wx = sanitizer.sanitize(txt_traffic, current_platform="wechat")
    assert "加我微信" in res_in_wx and len(hits_in_wx) == 0  # 视频号未配置该词，安全放行

    # 3. 快手剧本营销整句阻断
    txt_ks = "今天我跟老板撕破脸了，直接骨折价给大家送！"
    res_ks, hits_ks, drop_ks = sanitizer.sanitize(txt_ks, current_platform="kuaishou")
    assert drop_ks and res_ks == ""
    # 同一句话在抖音（未配置该快手专属词）不触发整句阻断
    res_dy_ks, hits_dy_ks, drop_dy_ks = sanitizer.sanitize(txt_ks, current_platform="douyin")
    assert not drop_dy_ks

# 3. 测试：四级优先级队列与抢占式打断 (Barge-in)
def test_priority_queue_and_barge_in():
    async def _async_test():
        interrupted_events = []

        def on_interrupt(reason):
            interrupted_events.append(reason)

        pq = PriorityBargeInQueue(on_interrupt_callback=on_interrupt)

        # 先放入一个 P2 普通消息
        await pq.put("evt_1", "chat", "普通观众", {"text": "主播好"}, priority=2)
        # 再放入一个 P1 促单消息
        await pq.put("evt_2", "chat", "买家小王", {"text": "这款能便宜点吗"}, priority=1)
        # 突发 P0 大额打赏（触发打断）
        await pq.put("evt_3", "gift", "土豪张哥", {"gift_name": "超级火箭"}, priority=0)

        # 验证是否触发打断回调
        assert len(interrupted_events) == 1
        assert "超级火箭" in interrupted_events[0]

        # 取出事件时，P0 必须排在第一个
        first = await pq.get()
        assert first.priority == 0
        assert first.user_name == "土豪张哥"

        # 第二个必须是 P1
        second = await pq.get()
        assert second.priority == 1
        assert second.user_name == "买家小王"

        # 第三个是 P2
        third = await pq.get()
        assert third.priority == 2
        assert third.user_name == "普通观众"

    asyncio.run(_async_test())

# 4. 测试：三大主播角色状态机流式生成
def test_anchor_roles_event_processing():
    async def _async_test():
        # 测试带货主播
        e_role = EcommerceAnchorRole()
        context = {"products": [{"title": "澳洲美丽诺羊毛衫", "live_price": 299, "sku": "SKU_001"}]}
        e_chunks = []
        async for chunk in e_role.process_event("chat", "李女士", {"text": "这个羊毛衫多少钱？"}, context):
            e_chunks.append(chunk)
        e_text = "".join(e_chunks)
        assert "299" in e_text
        assert "小黄车" in e_text

        # 测试娱乐主播
        ent_role = EntertainmentHostRole()
        ent_chunks = []
        async for chunk in ent_role.process_event("gift", "榜一大哥", {"gift_name": "嘉年华"}, {}):
            ent_chunks.append(chunk)
        ent_text = "".join(ent_chunks)
        assert "嘉年华" in ent_text
        assert "比心" in ent_text

        # 测试行业专家主播：知识库为空时必须触发置信度安全兜底并带免责声明
        # (先清空全局 RAG 单例，保证测试确定性、不受其他用例影响)
        from server.core.rag.engine import global_rag
        global_rag.clear()
        exp_role = DomainExpertRole()
        exp_chunks = []
        async for chunk in exp_role.process_event("chat", "打工人张三", {"text": "公司突然辞退我怎么申请赔偿？"}, {}):
            exp_chunks.append(chunk)
        exp_text = "".join(exp_chunks)
        assert "声明" in exp_text
        assert "私信" in exp_text or "材料" in exp_text

    asyncio.run(_async_test())

# 4.5 测试：双路混合 RAG 检索引擎 (BM25 + TF-IDF)
def test_rag_engine_dual_recall():
    from server.core.rag.engine import KnowledgeBaseEngine

    rag = KnowledgeBaseEngine(min_score=0.30)
    rag.add_chunk("kc_1", "doc_1", "劳动合同法", "经济补偿金按工作年限计算，每满一年支付一个月工资，违法解除应付二倍赔偿金即 2N。")
    rag.add_chunk("kc_2", "doc_2", "合同纠纷", "定金具有担保性质，收受定金一方不履行债务应当双倍返还；订金一般视为预付款。")
    rag.add_chunk("kc_3", "doc_3", "美食菜谱", "红烧肉的做法：五花肉焯水后加冰糖炒糖色，小火慢炖四十分钟。")

    # 命中知识库
    hit = rag.search("被公司辞退能拿到多少经济补偿？")
    assert hit["matched"] is True
    assert "经济补偿" in hit["hits"][0]["content"] or "赔偿金" in hit["hits"][0]["content"]
    assert hit["best_score"] >= 0.30

    # 未命中 (置信度不足触发兜底)
    miss = rag.search("今天天气怎么样适合穿什么衣服？")
    assert miss["matched"] is False

# 4.6 测试：滑动窗口弹幕聚合器相似度聚类
def test_barrage_aggregator_similarity():
    async def _async_test():
        agg = BarrageAggregator(window_seconds=3.0)

        r1 = await agg.add_message("观众A", "这款大衣怎么发货？")
        assert r1["is_aggregated"] is False

        # 相似弹幕被合并 (相似度阈值 0.82)；满 2 条即合并集中答复，绝不静默吞掉观众提问
        r2 = await agg.add_message("观众B", "这款大衣怎么发货呀？")
        assert r2 is not None, "第二条相似提问被静默吞掉，观众永远得不到答复"
        assert r2["is_aggregated"] is True
        assert r2["count"] == 2
        assert set(r2["users"]) == {"观众A", "观众B"}

        # 合并答复输出后开启新一轮聚合周期：后续相似弹幕作为新首条正常答复
        r3 = await agg.add_message("观众C", "这款大衣怎么发货？？")
        assert r3["is_aggregated"] is False

        # 第四条再次凑满一对，触发新一轮合并答复
        r4 = await agg.add_message("观众D", "这款大衣怎么发货？！")
        assert r4 is not None and r4["is_aggregated"] is True
        assert r4["count"] == 2

        # 完全不同的弹幕独立处理
        r5 = await agg.add_message("观众E", "主播今天几点下播？")
        assert r5["is_aggregated"] is False

    asyncio.run(_async_test())

# 4.7 测试：MCP 异步工具总线 (库存查询与优惠券下发)
def test_mcp_tool_bus():
    async def _async_test():
        from server.core.tools.tool_bus import global_mcp_tools

        assert "query_stock" in global_mcp_tools.list_tools()
        assert "trigger_onscreen_coupon" in global_mcp_tools.list_tools()

        # 未注册工具的优雅报错
        missing = await global_mcp_tools.call("not_exist_tool")
        assert "error" in missing

        # 优惠券下发广播 (无前端连接时安全空转)
        coupon = await global_mcp_tools.call("trigger_onscreen_coupon", desc="测试券", seconds=60)
        assert coupon["ok"] is True

    asyncio.run(_async_test())

# 4.8 测试：语音防机械感人类化器 (规划 §14.2)
def test_humanizer():
    import random
    from server.core.guardrails.humanizer import humanize_text, jitter_speed, speed_to_edge_rate

    rng = random.Random(42)
    # 带货角色可注入语气词 (固定随机源保证可测)
    text = "今天这款羊毛衫只要两百九十九元！"
    outputs = {humanize_text(text, "ecommerce", rng) for _ in range(200)}
    assert any(o != text for o in outputs)  # 存在注入语气词的样本
    assert text in outputs                  # 也存在未注入的样本

    # 专家角色永不注入语气词 (保持严谨)
    expert_outputs = {humanize_text(text, "expert", rng) for _ in range(100)}
    assert expert_outputs == {text}

    # 语速微扰范围 ±3%
    jittered = {jitter_speed(1.1, rng) for _ in range(100)}
    assert all(1.06 <= j <= 1.14 for j in jittered)

    # Edge-TTS rate 参数转换
    assert speed_to_edge_rate(1.1) == "+10%"
    assert speed_to_edge_rate(0.95) == "-5%"
    assert speed_to_edge_rate(1.0) == "+0%"

# 4.9 测试：Bilibili 真实协议封包与解包
def test_bilibili_packet_codec():
    from server.adapters.danmaku.bilibili_fetcher import BilibiliDanmakuFetcher
    import struct
    import zlib

    # 认证包构造
    auth_body = b'{"uid":0,"roomid":1}'
    packet = BilibiliDanmakuFetcher._pack_packet(1, 7, auth_body)
    total_len, header_len, protover, op, seq = struct.unpack(">IHHII", packet[:16])
    assert total_len == 16 + len(auth_body)
    assert header_len == 16
    assert op == 7

    # zlib 压缩业务帧解析
    inner = BilibiliDanmakuFetcher._pack_packet(0, 5, b'{"cmd":"TEST"}')
    outer = BilibiliDanmakuFetcher._pack_packet(2, 5, zlib.compress(inner))
    parsed = BilibiliDanmakuFetcher._unpack_packets(outer)
    assert parsed[0][1] == 5 and parsed[0][0] == 2
    subs = BilibiliDanmakuFetcher._unpack_packets(zlib.decompress(parsed[0][2]))
    assert subs[0][2] == b'{"cmd":"TEST"}'

# 5. 测试：真实驱动与弹幕适配器
def test_media_and_danmaku_adapters():
    from server.adapters.media.edgetts_driver import EdgeTTSMediaDriver
    from server.adapters.media.cosyvoice_driver import CosyVoiceMediaDriver
    from server.adapters.media.musetalk_driver import MuseTalkMediaDriver
    from server.adapters.danmaku.bilibili_fetcher import BilibiliDanmakuFetcher

    async def _async_test():
        # 1. EdgeTTS 测试
        edge_driver = EdgeTTSMediaDriver()
        await edge_driver.start()
        chunks = []
        async for c in edge_driver.synthesize_stream("欢迎来到直播间"):
            chunks.append(c)
            break
        assert len(chunks) > 0
        await edge_driver.interrupt("测试打断")
        assert edge_driver.is_speaking is False
        await edge_driver.stop()

        # 2. CosyVoice 测试
        cosy_driver = CosyVoiceMediaDriver()
        await cosy_driver.start()
        await cosy_driver.set_prompt_voice("uploads/voices/sample.wav")
        await cosy_driver.interrupt("紧急介入")
        assert cosy_driver.is_speaking is False
        await cosy_driver.stop()

        # 3. MuseTalk 测试
        muse_driver = MuseTalkMediaDriver()
        await muse_driver.start()
        await muse_driver.feed_audio_chunk(b"\x00" * 3200, "测试文本")
        assert muse_driver.is_speaking is True
        await muse_driver.interrupt("Barge-in")
        assert muse_driver.is_speaking is False
        preview = muse_driver.get_preview_status()
        assert preview["fps"] == 25
        await muse_driver.stop()

        # 4. Bilibili 弹幕解析测试
        received_events = []
        async def on_event(ev_type, user, payload, priority):
            received_events.append((ev_type, user, priority))

        bili = BilibiliDanmakuFetcher("123456", on_event)
        # 测试普通弹幕
        await bili.inject_parsed_packet("DANMU_MSG", {"uname": "小明", "msg": "主播好"})
        assert received_events[-1] == ("danmaku", "小明", 2)

        # 测试促单提问 (自动提权至 P1)
        await bili.inject_parsed_packet("DANMU_MSG", {"uname": "买家小红", "msg": "这个羊毛衫多少钱？"})
        assert received_events[-1] == ("danmaku", "买家小红", 1)

        # 测试大额礼物打赏 (自动触发 P0 强打断)
        await bili.inject_parsed_packet("SEND_GIFT", {"uname": "土豪哥", "giftName": "超级大火箭", "total_coin": 5000})
        assert received_events[-1] == ("gift", "土豪哥", 0)

    asyncio.run(_async_test())


# 4.23 测试：Barge-in 打断令牌必须"粘滞"直到消费方显式复位
# (回归：旧实现 trigger_barge_in 在同一同步段内 set 后立即 clear，消费循环的 is_cancelled() 检查永远为 False，打断形同虚设)
def test_barge_in_token_sticky_until_reset():
    async def _async_test():
        pq = PriorityBargeInQueue()

        await pq.put("evt_p0", "gift", "土豪哥", {"gift_name": "火箭"}, priority=0)

        # P0 触发打断后，令牌必须保持置位，供播报协程在句边界观察到
        assert pq.is_cancelled() is True
        assert pq.is_interrupted_flag is True

        # 消费方处理完毕后显式复位，令牌与标志必须同时清除
        pq.reset_interrupt()
        assert pq.is_cancelled() is False
        assert pq.is_interrupted_flag is False

    asyncio.run(_async_test())


# 4.24 测试：正在播报的消费协程必须在句边界观察到打断并停止播报 (协作式中断)
def test_barge_in_cooperative_playback_interrupt():
    async def _async_test():
        pq = PriorityBargeInQueue()
        spoken = []

        async def fake_playback():
            # 模拟逐句播报：每句播出前先检查打断令牌，句间让出事件循环
            for sentence in ["第一句", "第二句", "第三句", "第四句", "第五句"]:
                if pq.is_cancelled():
                    return "interrupted"
                spoken.append(sentence)
                await asyncio.sleep(0)
            return "completed"

        playback_task = asyncio.create_task(fake_playback())
        await asyncio.sleep(0)  # 让播报协程先播出第一句

        await pq.put("evt_p0", "gift", "土豪哥", {"gift_name": "火箭"}, priority=0)
        result = await playback_task

        assert result == "interrupted"
        assert "第一句" in spoken
        assert "第二句" not in spoken
        assert len(spoken) < 5

    asyncio.run(_async_test())


# 4.25 测试：硬件探测必须卸载到线程池执行，不得阻塞事件循环
# (回归：WMI/nvidia-smi 子进程探测在事件循环上同步执行，前端 5s 轮询会让低配机整卡顿)
# 判别方式：探测进行期间，事件循环上的心跳协程必须持续跳动 (旧同步实现会令心跳完全停摆)
def test_gpu_probe_offloads_blocking_io():
    import time as _time
    from server.routes import live as _live_mod

    async def _async_test():
        original_probe = _live_mod._probe_gpu
        ticks = {"n": 0}
        stop_heartbeat = asyncio.Event()

        async def heartbeat():
            while not stop_heartbeat.is_set():
                ticks["n"] += 1
                await asyncio.sleep(0.01)

        def slow_probe():
            _time.sleep(0.3)
            return {"gpu_name": "FakeGPU", "vram_total_gb": 8.0, "vram_used_gb": 1.0, "cuda_available": False}

        _live_mod._probe_gpu = slow_probe
        _live_mod._HW_PAYLOAD_CACHE["data"] = None
        _live_mod._HW_PAYLOAD_CACHE["ts"] = 0.0
        try:
            hb_task = asyncio.create_task(heartbeat())
            await asyncio.sleep(0.05)
            ticks_before = ticks["n"]

            result = await _live_mod._hardware_payload()

            ticks_during_probe = ticks["n"] - ticks_before
            stop_heartbeat.set()
            await hb_task

            assert result["gpu"]["gpu_name"] == "FakeGPU"
            assert ticks_during_probe >= 5, (
                f"探测期间事件循环被阻塞 0.3s，心跳仅跳动 {ticks_during_probe} 次 "
                "(重量级探测未卸载到线程池)"
            )
        finally:
            _live_mod._probe_gpu = original_probe
            _live_mod._HW_PAYLOAD_CACHE["data"] = None
            _live_mod._HW_PAYLOAD_CACHE["ts"] = 0.0

    asyncio.run(_async_test())


# 4.26 测试：CosyVoice 服务健康检查与运行期降级信号
def test_cosyvoice_health_check_unreachable():
    """CosyVoice 不可达时必须显式失败，供控制器执行整句 Edge 降级。"""
    async def _async_test():
        from server.adapters.media.cosyvoice_driver import CosyVoiceMediaDriver

        driver = CosyVoiceMediaDriver(api_base="http://127.0.0.1:1")
        assert await driver.health_check() is False
        with pytest.raises(RuntimeError, match="CosyVoice synthesis failed"):
            async for _ in driver.synthesize_stream("测试一句话"):
                pass

    asyncio.run(_async_test())


# 4.27 测试：TTS 驱动选择链——CosyVoice 不可用时必须自动降级 Edge-TTS (ADR-10)
def test_select_tts_driver_falls_back_when_cosyvoice_down():
    async def _async_test():
        from server.routes.live import global_live_controller
        from server.adapters.media.cosyvoice_driver import CosyVoiceMediaDriver
        from server.adapters.media.edgetts_driver import EdgeTTSMediaDriver

        async def unhealthy(self, timeout: float = 2.0):
            return False

        CosyVoiceMediaDriver.health_check = unhealthy
        try:
            driver = await global_live_controller._select_tts_driver()
            assert isinstance(driver, EdgeTTSMediaDriver), (
                f"CosyVoice 不可达时期望降级 Edge-TTS，实际选择了 {type(driver).__name__}"
            )
        finally:
            del CosyVoiceMediaDriver.health_check

    asyncio.run(_async_test())


# 4.29 测试：虚拟摄像头 send_frame 不得内部节流
# (回归：send_frame 内调用 sleep_until_next_frame(40ms) 与 25fps 渲染循环双重睡眠，实际帧率减半)
def test_virtual_cam_send_frame_does_not_double_throttle():
    import time as _time
    import numpy as np
    from server.core.media.virtual_cam import VirtualCameraService

    cam = VirtualCameraService(width=64, height=48, fps=25)
    cam.is_active = True
    sent = []

    class FakeDev:
        def send(self, frame):
            sent.append(1)

        def sleep_until_next_frame(self):
            _time.sleep(0.04)

    cam.cam_device = FakeDev()
    frame = np.zeros((48, 64, 3), dtype=np.uint8)

    start = _time.perf_counter()
    for _ in range(5):
        cam.send_frame(frame)
    elapsed = _time.perf_counter() - start

    assert len(sent) == 5
    # 帧率节流由渲染循环保有职责；send_frame 连续 5 次应远快于 5×40ms=200ms
    assert elapsed < 0.1, f"send_frame 连续 5 次耗时 {elapsed:.3f}s，内部存在 40ms/帧双重节流 (25fps 会被减半)"


# 4.30 测试：RAG 置信度门控阈值按向量后端动态化 (ADR-11)
# 哈希投影代理阈值 0.30；接入真实 ONNX 语义向量引擎后上调至 0.65；显式传参永远优先
def test_rag_confidence_threshold_by_backend_adr11():
    from server.core.rag.engine import KnowledgeBaseEngine

    rag = KnowledgeBaseEngine()
    backend = rag.get_status()["vector_backend"]
    expected = 0.65 if backend == "onnx" else 0.30
    assert rag.min_score == expected, (
        f"向量后端 {backend} 的置信度门控应为 {expected}，实际 {rag.min_score} (ADR-11 阈值漂移)"
    )
    assert KnowledgeBaseEngine(min_score=0.5).min_score == 0.5


# 4.31 测试：人脸关键点必须接入直播渲染链
# (回归：avatars 生成的 landmarks 从未传入 set_avatar，口型/眨眼画在固定比例位置)
def test_select_driver_passes_landmarks_to_renderer():
    import json as _json
    import tempfile
    import os as _os
    import numpy as np
    from PIL import Image as _PILImage
    from server.adapters.media.media_router import global_media_router
    from server.adapters.media.musetalk_driver import global_musetalk_driver

    async def _async_test():
        tmp = tempfile.mkdtemp(prefix="landmark_wiring_")
        photo = _os.path.join(tmp, "portrait.png")
        _PILImage.fromarray((np.random.rand(160, 120, 3) * 255).astype("uint8")).save(photo)
        lm_path = _os.path.join(tmp, "portrait_landmarks.json")
        with open(lm_path, "w", encoding="utf-8") as f:
            _json.dump({"method": "haarcascade", "face_box": [11, 22, 130, 160], "eyes": []}, f)

        try:
            global_media_router.select_driver(mode="B", avatar_path=photo, landmarks_path=lm_path)
            expected_box = (66, 132, 654, 828)  # 120x160 原图 cover 到 720x960 后裁剪至画布
            assert global_musetalk_driver.face_box == expected_box, (
                f"landmarks 未按渲染画布变换, face_box={global_musetalk_driver.face_box}"
            )
        finally:
            # 恢复默认底板与关键点，避免污染其他用例
            global_musetalk_driver.set_avatar("")
            global_musetalk_driver.face_box = None

    asyncio.run(_async_test())


# 4.32 测试：开播时按主播底图惰性检测人脸关键点并落盘缓存 (幂等)
def test_resolve_landmarks_for_avatar(monkeypatch):
    import json as _json
    import tempfile
    import os as _os
    from pathlib import Path as _Path
    import numpy as np
    from PIL import Image as _PILImage
    from server.routes import live as _live_mod
    from server.core.vision import face_landmarks as _fl

    tmp = tempfile.mkdtemp(prefix="landmark_resolve_")
    photo = _Path(tmp) / "anchor_portrait.jpg"
    _PILImage.fromarray((np.random.rand(160, 120, 3) * 255).astype("uint8")).save(photo)

    # 1. 无缓存 -> 执行检测并落盘
    cache = photo.with_name("anchor_portrait_landmarks.json")
    assert not cache.exists()
    resolved = _live_mod._resolve_landmarks_for_avatar(str(photo))
    assert resolved and _Path(resolved).exists(), "无缓存时应执行检测并生成缓存"
    data = _json.loads(cache.read_text(encoding="utf-8"))
    assert data.get("face_box") and len(data["face_box"]) == 4

    # 2. 已有缓存 -> 不得重复检测 (若再次调用检测函数则直接失败)
    def _must_not_detect(*a, **k):
        raise AssertionError("已有缓存时不应重复执行人脸检测")

    monkeypatch.setattr(_fl, "detect_face_landmarks", _must_not_detect)
    resolved2 = _live_mod._resolve_landmarks_for_avatar(str(photo))
    assert resolved2 == str(cache)

    # 3. 空路径 -> 空字符串
    assert _live_mod._resolve_landmarks_for_avatar("") == ""

    # 4. 不存在的图片 -> 安全返回空
    assert _live_mod._resolve_landmarks_for_avatar(str(_Path(tmp) / "ghost.jpg")) == ""


# 4.33 测试：专家领域推断与离线兜底话术动态化
# (回归：LLM 兜底与冷场垫场硬编码"劳动合同/劳动维权"，中医/财税等自定义专家全部答非所问)
def test_infer_expert_domain():
    from server.core.roles.expert_domains import infer_expert_domain

    assert infer_expert_domain("你是资深劳动法律师，解答劳动仲裁与合同纠纷")["key"] == "legal"
    assert infer_expert_domain("财商分析师老张，解答观众理财与防诈骗咨询")["key"] == "finance"
    assert infer_expert_domain("中医健康养生专家，讲解体质调理")["key"] == "medical"
    assert infer_expert_domain("考研升学规划师，擅长择校")["key"] == "education"
    assert infer_expert_domain("一位行业顾问")["key"] == "general"


def test_expert_fallback_domain_aware():
    from server.core.llm.client import LLMClient

    medical_prompt = "你是一位中医健康养生专家，讲解体质调理与日常养生。"
    reply = LLMClient._generate_fallback(medical_prompt, "我最近失眠严重怎么办？", {})
    # 旧实现此处返回"劳动合同/劳动仲裁"话术，属于严重张冠李戴
    assert "劳动" not in reply and "合同" not in reply
    assert "声明" in reply or "参考" in reply          # 保留免责声明
    assert "医疗机构" in reply or "医嘱" in reply       # 领域适配的安全指引

    legal_prompt = "你是一位资深劳动法律师，解答劳动仲裁与合同纠纷。"
    legal_reply = LLMClient._generate_fallback(legal_prompt, "公司拖欠工资怎么办？", {})
    assert "劳动" in legal_reply or "证据" in legal_reply
    assert "声明" in legal_reply or "参考" in legal_reply


def test_expert_idle_prompt_uses_domain():
    from server.core.roles.domain_expert import DomainExpertRole

    medical_role = DomainExpertRole(
        role_id="r_med", role_name="中医养生顾问·白老师",
        system_prompt="你是一位中医健康养生专家，讲解体质调理与日常养生。"
    )
    prompt = medical_role.build_idle_prompt({})
    assert "劳动维权" not in prompt
    assert "中医养生顾问·白老师" in prompt
    assert medical_role.domain["key"] == "medical"


# 4.34 测试：主密钥 DPAPI 硬件级保护与旧明文密钥零损迁移 (规划 §9.6)
def test_master_key_dpapi_protected_and_legacy_migration(tmp_path, monkeypatch):
    import sys as _sys
    from server import config as _cfg

    key_file = tmp_path / ".master.key"

    if _sys.platform == "win32":
        # 1. 全新生成 -> 文件带 DPAPI 前缀 (受系统凭据保护，不再是明文)，加载后为 32 字节
        key1 = _cfg.load_or_create_master_key(key_file)
        assert len(key1) == 32
        assert key_file.read_bytes().startswith(_cfg.DPAPI_KEY_PREFIX), "主密钥仍以明文落盘"
        assert _cfg.load_or_create_master_key(key_file) == key1

        # 2. 旧版明文 32 字节密钥 -> 零损迁移为 DPAPI 形态 (存量 AES 密文可继续解密)
        legacy = b"\x07" * 32
        key_file.write_bytes(legacy)
        key3 = _cfg.load_or_create_master_key(key_file)
        assert key3 == legacy
        assert key_file.read_bytes().startswith(_cfg.DPAPI_KEY_PREFIX)

        # 3. 损坏的 DPAPI 密文 -> 必须显式报错，严禁静默换钥导致存量密钥全部失联
        key_file.write_bytes(_cfg.DPAPI_KEY_PREFIX + b"\x00" * 64)
        try:
            _cfg.load_or_create_master_key(key_file)
            raised = False
        except RuntimeError:
            raised = True
        assert raised, "无法解密的主密钥应显式报错而非静默重置"
    else:
        # 非 Windows 平台回退：无前缀原始 32 字节
        monkeypatch.setattr(_cfg, "_DPAPI_AVAILABLE", False)
        key1 = _cfg.load_or_create_master_key(key_file)
        assert len(key1) == 32
        assert not key_file.read_bytes().startswith(_cfg.DPAPI_KEY_PREFIX)
        assert _cfg.load_or_create_master_key(key_file) == key1


# 4.35 测试：场观数据真实化——无真实人气源时严禁随机游走伪造数据
def test_viewer_count_no_fabrication():
    async def _async_test():
        from server.routes.live import global_live_controller

        # 1. 无真实数据源 -> 不得凭空生成场观
        original_fetcher = global_live_controller.fetcher
        try:
            global_live_controller.fetcher = None
            global_live_controller.stats["viewer_count"] = 0
            global_live_controller.stats["peak_viewer_count"] = 0
            global_live_controller._refresh_viewer_count()
            assert global_live_controller.stats["viewer_count"] == 0, "无数据源时场观被伪造"
            assert global_live_controller.stats["peak_viewer_count"] == 0

            # 2. 真实人气源 -> 如实采集并更新峰值
            class _RealFetcher:
                watched_count = 1234

            global_live_controller.fetcher = _RealFetcher()
            global_live_controller._refresh_viewer_count()
            assert global_live_controller.stats["viewer_count"] == 1234
            assert global_live_controller.stats["peak_viewer_count"] == 1234
        finally:
            global_live_controller.fetcher = original_fetcher

    asyncio.run(_async_test())


# 4.36 测试：零依赖 protobuf 线格式读写器 (抖音弹幕深度重构的地基)
def test_protobuf_reader_wire_format():
    from server.adapters.danmaku import proto_reader as pr

    # field1 varint=300 -> 08 AC 02 ; field2 string "hi" -> 12 02 68 69
    data = pr.varint(1, 300) + pr.string(2, "hi")
    assert pr.get_varint(data, 1) == 300
    assert pr.get_string(data, 2) == "hi"
    assert pr.get_bytes(data, 2) == b"hi"

    # 未知 wire type (fixed64/fixed32/group) 必须安全跳过，不得错位解析
    data2 = b"\x29" + b"\x00" * 8 + data      # field5 fixed64 前缀
    data3 = bytes([0x35]) + b"\x00" * 4 + data  # field6 fixed32 前缀
    assert pr.get_string(data2, 2) == "hi"
    assert pr.get_string(data3, 2) == "hi"

    # 重复字段 (repeated) 全量枚举
    repeated = pr.string(1, "a") + pr.string(1, "b")
    assert [v for _, v in pr.iter_delimited(repeated, 1)] == [b"a", b"b"]

    # 缺失字段返回 None
    assert pr.get_varint(data, 9) is None
    assert pr.get_string(data, 9) is None


# 4.37 测试：抖音真实 protobuf 信封逐级解包 (PushFrame→Response→Message)
def _build_douyin_frame(pb, method="WebcastChatMessage", user="抖音老铁", text="主播你好呀",
                        count=None, coin=None):
    user_msg = pb.varint(1, 42) + pb.string(2, user)
    inner = pb.bytes(2, user_msg) + pb.string(3, text)
    if count is not None:
        inner += pb.varint(5, count)
    if coin is not None:
        inner += pb.varint(11, coin)
    msg = pb.string(1, method) + pb.bytes(2, inner)
    resp = pb.bytes(1, msg)
    return pb.bytes(3, resp) + pb.varint(4, 0)


def test_douyin_fetcher_parses_real_protobuf_envelope():
    import asyncio
    import gzip
    from server.adapters.danmaku import proto_reader as pb
    from server.adapters.danmaku.douyin_fetcher import DouyinDanmakuFetcher

    events = []

    async def cb(et, u, p, prio):
        events.append((et, u, p, prio))

    f = DouyinDanmakuFetcher("123", cb)
    f.is_running = True

    # 1. 明文 PushFrame：聊天弹幕结构化解包 (旧 utf-8 正则方案取不到正确用户名/内容)
    f._parse_push_frame(_build_douyin_frame(pb))
    assert events[-1][0] == "danmaku"
    assert events[-1][1] == "抖音老铁", f"用户名解析错误: {events[-1][1]}"
    assert events[-1][2]["text"] == "主播你好呀", f"弹幕内容解析错误: {events[-1][2]}"
    assert events[-1][3] == 2

    # 2. 促单关键词提权 P1
    f._parse_push_frame(_build_douyin_frame(pb, text="这个怎么买"))
    assert events[-1][3] == 1

    # 3. gzip 压缩帧自动解压
    compressed = gzip.compress(_build_douyin_frame(pb, text="讲得真好"))
    f._parse_push_frame(compressed)
    assert events[-1][2]["text"] == "讲得真好"

    # 4. 礼物：使用结构化提取的数量与金额，严禁硬编码"小心心/100瓜子"
    f._parse_push_frame(_build_douyin_frame(pb, method="WebcastGiftMessage", count=3, coin=6000))
    assert events[-1][0] == "gift"
    assert events[-1][2]["count"] == 3
    assert events[-1][2]["total_coin"] == 6000, f"礼物金额应取结构化字段: {events[-1][2]}"
    assert events[-1][3] == 1  # 6000 < 50000 -> P1

    # 5. 大额礼物 P0
    f._parse_push_frame(_build_douyin_frame(pb, method="WebcastGiftMessage", count=1, coin=600000))
    assert events[-1][3] == 0

    # 6. 进房与点赞事件
    f._parse_push_frame(_build_douyin_frame(pb, method="WebcastMemberMessage", text=""))
    assert events[-1][0] == "enter"
    f._parse_push_frame(_build_douyin_frame(pb, method="WebcastLikeMessage", count=5, text=""))
    assert events[-1][0] == "like"
    assert "5" in events[-1][2]["text"]


def test_douyin_fetcher_accepts_configured_cookies():
    from server.adapters.danmaku.douyin_fetcher import DouyinDanmakuFetcher

    f = DouyinDanmakuFetcher("123", lambda *a: None, ttwid="TT-Token", ms_token="MS-Token")
    cookie = f._cookie_header()
    assert "ttwid=TT-Token" in cookie
    assert "msToken=MS-Token" in cookie


# 4.38 测试：多模态视觉注入 × Ollama native 接口兼容
# (回归：Ollama /api/chat 不接受 OpenAI 的 content 数组格式，Tier A 全本地模式下多模态完全失效)
def test_vision_payload_ollama_native_compat(monkeypatch):
    from server.core.llm import client as llm_mod
    from server.core.llm.client import LLMClient

    captured = {}

    class FakeResponse:
        status_code = 200

        async def aiter_lines(self):
            return
            yield

    class FakeStreamCtx:
        async def __aenter__(self):
            return FakeResponse()

        async def __aexit__(self, *a):
            return False

    class FakeAsyncClient:
        def __init__(self, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        def stream(self, method, url, headers=None, json=None):
            captured["url"] = url
            captured["payload"] = json
            return FakeStreamCtx()

    monkeypatch.setattr(llm_mod.httpx, "AsyncClient", FakeAsyncClient)

    ollama_cfg = {
        "provider_name": "local_ollama",
        "base_url": "http://127.0.0.1:11434",
        "model_name": "qwen2.5:14b",
        "api_key": "",
        "extra_params": {},
    }
    openai_cfg = {
        "provider_name": "openai_compatible",
        "base_url": "https://api.deepseek.com/v1",
        "model_name": "deepseek-chat",
        "api_key": "sk-test",
        "extra_params": {},
    }
    vision_ctx = {"vision_image_b64": "FAKEB64"}
    vision_query = "主播手里拿的是什么？"

    async def _collect(cfg, msg, ctx):
        async def fake_cfg():
            return cfg

        monkeypatch.setattr(LLMClient, "get_active_llm_config", fake_cfg)
        chunks = []
        async for c in LLMClient.generate_stream("你是主播", msg, context=ctx):
            chunks.append(c)
        return chunks

    # 1. Ollama native + 视觉: content 必须是纯字符串，图像走 images 字段
    asyncio.run(_collect(ollama_cfg, vision_query, vision_ctx))
    user_msg = captured["payload"]["messages"][-1]
    assert captured["url"].endswith("/api/chat")
    assert isinstance(user_msg["content"], str), "Ollama native 不接受 content 数组"
    assert user_msg.get("images") == ["FAKEB64"], "Ollama native 必须用 images 字段携带图像"

    # 2. OpenAI 兼容 + 视觉: content 数组 + image_url (保持原协议)
    asyncio.run(_collect(openai_cfg, vision_query, vision_ctx))
    user_msg2 = captured["payload"]["messages"][-1]
    assert isinstance(user_msg2["content"], list)
    assert any(p.get("type") == "image_url" for p in user_msg2["content"])
    assert "images" not in user_msg2

    # 3. Ollama native 无视觉: 不携带 images 键
    asyncio.run(_collect(ollama_cfg, "今天天气不错", {}))
    user_msg3 = captured["payload"]["messages"][-1]
    assert isinstance(user_msg3["content"], str)
    assert "images" not in user_msg3


# 4.39 测试：语速微扰与音量增益驱动接线 (规划 §14.2 / §9.2)
def test_apply_speech_speed_and_volume_gain():
    async def _async_test():
        from server.adapters.media.base_driver import BaseMediaDriver
        from server.adapters.media.edgetts_driver import EdgeTTSMediaDriver
        from server.adapters.media.cosyvoice_driver import CosyVoiceMediaDriver
        from server.adapters.media.remote_gpu_driver import RemoteGPUMediaDriver

        # 1. Edge: 语速 -> rate 参数；音量增益 -> volume 参数
        edge = EdgeTTSMediaDriver()
        await edge.apply_speech_speed(1.1)
        assert edge.rate == "+10%"
        await edge.apply_volume_gain(1.2)
        assert edge.volume == "+20%"
        await edge.apply_speech_speed(0.95)
        assert edge.rate == "-5%"

        # 2. CosyVoice: 语速与音量注入推理 payload
        cosy = CosyVoiceMediaDriver()
        await cosy.apply_speech_speed(1.1)
        await cosy.apply_volume_gain(1.2)
        assert cosy.speed == 1.1 and cosy.volume == 1.2

        # 3. 远程 GPU: 语速同步
        remote = RemoteGPUMediaDriver()
        await remote.apply_speech_speed(1.05)
        assert remote.speed == 1.05

        # 4. 基类默认空实现 (不抛异常)
        class _Minimal(BaseMediaDriver):
            async def feed_audio_chunk(self, a, t): pass
            async def interrupt(self, r="x"): pass
            async def start(self): pass
            async def stop(self): pass
        minimal = _Minimal()
        await minimal.apply_speech_speed(1.0)
        await minimal.apply_volume_gain(1.0)

    asyncio.run(_async_test())


# 4.40 测试：MiniMax 云端 TTS 驱动 (T2A v2 协议 + 失败自动回退 Edge-TTS，规划 §9.2)
def test_minimax_tts_driver_contract(monkeypatch):
    from server.adapters.media import minimax_driver as mm_mod
    from server.adapters.media.minimax_driver import MinimaxTTSMediaDriver
    from server.adapters.media.edgetts_driver import EdgeTTSMediaDriver

    captured = {}
    fake_hex = ("ab" * 64)  # 64 bytes 音频

    class FakeResponse:
        status_code = 200
        def json(self):
            return {"base_resp": {"status_code": 0}, "data": {"audio": fake_hex}}

    class FakeAsyncClient:
        def __init__(self, **kw):
            pass
        async def __aenter__(self):
            return self
        async def __aexit__(self, *a):
            return False
        async def post(self, url, params=None, headers=None, json=None):
            captured["url"] = url
            captured["params"] = params
            captured["headers"] = headers
            captured["body"] = json
            return FakeResponse()

    monkeypatch.setattr(mm_mod.httpx, "AsyncClient", FakeAsyncClient)

    async def _run():
        d = MinimaxTTSMediaDriver(api_base="https://api.minimax.chat/v1", api_key="sk-mm", group_id="g123", voice_id="female-shaonv")
        await d.apply_speech_speed(1.1)
        await d.apply_volume_gain(1.2)
        chunks = []
        async for c in d.synthesize_stream("你好直播间"):
            chunks.append(c)
        return chunks

    chunks = asyncio.run(_run())

    # 1. 请求契约: T2A_v2 端点 + GroupId + Bearer 鉴权 + 语速/音量进 voice_setting
    assert "/t2a_v2" in captured["url"]
    assert captured["params"].get("GroupId") == "g123"
    assert captured["headers"]["Authorization"] == "Bearer sk-mm"
    vs = captured["body"]["voice_setting"]
    assert vs["voice_id"] == "female-shaonv"
    assert abs(vs["speed"] - 1.1) < 1e-6
    assert abs(vs["vol"] - 1.2) < 1e-6

    # 2. hex 音频被正确解码产出
    assert b"\xab" * 64 in b"".join(chunks)

    # 3. 合成失败时自动回退 Edge-TTS (ADR-10)，不产出静默伪音频
    async def boom(self, url, params=None, headers=None, json=None):
        raise RuntimeError("network down")

    monkeypatch.setattr(mm_mod.httpx, "AsyncClient", _BoomClient := type("C", (), {
        "__init__": lambda self, **kw: None,
        "__aenter__": lambda self: _async_self(self),
        "__aexit__": lambda self, *a: _async_false(),
        "post": boom,
    }))

    async def fake_edge(self, text):
        yield b"EDGE_CHUNK"

    monkeypatch.setattr(EdgeTTSMediaDriver, "synthesize_stream", fake_edge)
    chunks2 = asyncio.run(_run())
    assert chunks2 == [b"EDGE_CHUNK"], "MiniMax 失败后应回退 Edge-TTS"


def _async_self(x):
    async def _f():
        return x
    return _f()


def _async_false():
    async def _f():
        return False
    return _f()


# 4.41 测试：TTS 选择链接入 MiniMax (远程 GPU -> CosyVoice -> MiniMax -> Edge, ADR-10)
def test_select_tts_driver_minimax_chain(monkeypatch):
    async def _async_test():
        from sqlalchemy import select
        from server.database.db import AsyncSessionLocal
        from server.database.models import ApiProviderConfig
        from server.routes.live import global_live_controller
        from server.adapters.media.minimax_driver import MinimaxTTSMediaDriver
        from server.adapters.media.edgetts_driver import EdgeTTSMediaDriver

        async with AsyncSessionLocal() as s:
            # 关闭 cosyvoice，启用 minimax
            for r in (await s.execute(select(ApiProviderConfig).where(ApiProviderConfig.config_group == "tts"))).scalars().all():
                r.is_active = 1 if "minimax" in (r.provider_name or "") else 0
            s.add(ApiProviderConfig(
                id="cfg_tts_minimax", config_group="tts", provider_name="minimax", is_active=1,
                encrypted_api_key="", base_url="https://api.minimax.chat/v1",
                extra_params_json='{"group_id": "g1", "voice_id": "female-shaonv"}'
            ))
            await s.commit()

        async def healthy(self, timeout: float = 2.0):
            return True

        monkeypatch.setattr(MinimaxTTSMediaDriver, "health_check", healthy)
        driver = await global_live_controller._select_tts_driver()
        assert isinstance(driver, MinimaxTTSMediaDriver), f"期望 MiniMax 驱动，实际 {type(driver).__name__}"

        # 健康检查不过 -> 自动降级 Edge
        async def unhealthy(self, timeout: float = 2.0):
            return False
        monkeypatch.setattr(MinimaxTTSMediaDriver, "health_check", unhealthy)
        driver2 = await global_live_controller._select_tts_driver()
        assert isinstance(driver2, EdgeTTSMediaDriver), "MiniMax 无 Key 时必须降级 Edge-TTS"

        # 还原默认种子状态
        async with AsyncSessionLocal() as s:
            for r in (await s.execute(select(ApiProviderConfig).where(ApiProviderConfig.config_group == "tts"))).scalars().all():
                r.is_active = 1 if "cosyvoice" in (r.provider_name or "") else 0
            await s.commit()

    asyncio.run(_async_test())


# 4.42 测试：渲染循环必须运行在独立线程 (回归：25fps cv2+JPEG 编码阻塞事件循环 5~15ms/帧)
def test_render_loop_runs_off_event_loop_thread():
    import threading as _threading
    import time as _time
    from server.core.media import procedural_renderer as _pr
    from server.adapters.media.musetalk_driver import global_musetalk_driver as _driver

    async def _async_test():
        loop_tid = _threading.get_ident()
        synth_tids = []
        real_synth = _pr.synth_frame

        def spy_synth(*a, **k):
            synth_tids.append(_threading.get_ident())
            return real_synth(*a, **k)

        _pr.synth_frame = spy_synth
        try:
            await _driver.start()
            deadline = _time.time() + 6
            while _driver.total_frames_rendered < 5 and _time.time() < deadline:
                await asyncio.sleep(0.05)

            assert _driver.total_frames_rendered >= 5, "渲染循环未产出帧"
            assert synth_tids, "帧合成未被调用"
            assert all(t != loop_tid for t in synth_tids), (
                "帧合成仍在事件循环线程内执行 (25fps 渲染会持续阻塞主循环)"
            )
        finally:
            _pr.synth_frame = real_synth
            await _driver.stop()

    asyncio.run(_async_test())


# 4.43 测试：泊松过程眨眼调度器 (规划 §4.4：平均 2.8 秒一次，闭合 100~150ms)
def test_poisson_blink_scheduler():
    import random as _random
    from server.core.media.procedural_renderer import MicroExpressionState

    state = MicroExpressionState(rng=_random.Random(42))

    blinks = []
    was_blinking = False
    t = 0.0
    while t < 120.0:
        b = state.is_blinking(t)
        if b and not was_blinking:
            blinks.append(t)
        was_blinking = b
        t += 0.04

    # 1. 频次符合泊松均值 2.8s (120s 约 42 次，容差 ±40%)
    assert 25 <= len(blinks) <= 60, f"眨眼频次偏离泊松均值: {len(blinks)} 次 / 120s"

    # 2. 间隔非固定周期 (打破检测指纹)
    intervals = [b - a for a, b in zip(blinks, blinks[1:])]
    mean_i = sum(intervals) / len(intervals)
    var = sum((i - mean_i) ** 2 for i in intervals) / len(intervals)
    assert var > 0.3, f"眨眼间隔过于固定 (方差 {var:.3f})，仍是指纹周期"

    # 3. 单次闭合时长 100~150ms
    state2 = MicroExpressionState(rng=_random.Random(7))
    t = 0.0
    while not state2.is_blinking(t):
        t += 0.02
    start = t
    end = t
    while state2.is_blinking(end + 0.02):
        end += 0.02
    duration = end - start
    assert 0.08 <= duration <= 0.2, f"单次眨眼闭合时长 {duration:.3f}s 超出 100~150ms"

    # 4. synth_frame 兼容显式眨眼状态
    import numpy as np
    from server.core.media.procedural_renderer import synth_frame
    base = synth_frame.__globals__["generate_default_portrait"](64, 96)
    f = synth_frame(base, 64, 96, 1.0, 0.0, is_blinking=True)
    assert f.shape == (96, 64, 3)


# 4.44 测试：带货促单状态机 (规划 §5.2：CAROUSEL / QA_CLOSING / URGENCY_BURST)
def test_ecommerce_fsm_transitions():
    from server.core.roles.ecommerce_anchor import EcommerceState, EcommerceAnchorRole

    role = EcommerceAnchorRole()
    assert role.current_state == EcommerceState.CAROUSEL

    # 1. 促单提问 -> QA_CLOSING
    assert role.transition("danmaku", {"text": "这款多少钱"}) == EcommerceState.QA_CLOSING
    assert role.transition("danmaku", {"text": "有优惠吗"}) == EcommerceState.QA_CLOSING

    # 2. 打赏鸣谢不改变销售状态
    assert role.transition("gift", {"gift_name": "火箭"}) == EcommerceState.QA_CLOSING

    # 3. 冷场 1 次 -> 回轮播；QA 连续 >= 2 后冷场 -> URGENCY 逼单
    role2 = EcommerceAnchorRole()
    role2.transition("danmaku", {"text": "怎么买"})
    assert role2.transition("idle_filler", {}) == EcommerceState.CAROUSEL
    role2.transition("danmaku", {"text": "有库存吗"})
    role2.transition("danmaku", {"text": "怎么买"})
    assert role2.current_state == EcommerceState.QA_CLOSING
    assert role2.transition("idle_filler", {}) == EcommerceState.URGENCY
    # 4. 逼单播报完成后回轮播
    assert role2.transition("idle_filler", {}) == EcommerceState.CAROUSEL

    # 5. 运营 flash_sale 指令直接进入 URGENCY
    role3 = EcommerceAnchorRole()
    assert role3.transition("danmaku", {"text": "任意", "flash_sale": True}) == EcommerceState.URGENCY

    # 6. process_event 走完 URGENCY 逼单后状态复位
    async def _urgent_reset():
        role4 = EcommerceAnchorRole()
        role4.transition("danmaku", {"text": "多少钱"})
        role4.transition("danmaku", {"text": "怎么买"})
        ctx = {"products": [{"title": "测试大衣", "live_price": 299, "sku": "S1"}]}
        chunks = []
        async for c in role4.process_event("idle_filler", "系统", {}, ctx):
            chunks.append(c)
        assert role4.current_state == EcommerceState.CAROUSEL, "URGENCY 逼单播报后未复位回 CAROUSEL"

    asyncio.run(_urgent_reset())


# 4.45 测试：CosyVoice 兼容参考服务端契约 (让声音克隆客户端开箱可验证)
def test_cosyvoice_reference_server_contract():
    import importlib.util
    import sys as _sys
    import tempfile
    import wave
    import numpy as np
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    spec = importlib.util.spec_from_file_location("cosyvoice_server_ref", root / "scripts" / "cosyvoice_server.py")
    assert spec is not None, "缺少 scripts/cosyvoice_server.py"
    mod = importlib.util.module_from_spec(spec)
    _sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)

    from fastapi.testclient import TestClient
    client = TestClient(mod.app)

    # 1. 健康检查 (CosyVoiceMediaDriver.health_check 的探测目标)
    res = client.get("/")
    assert res.status_code == 200
    assert res.json()["status"] == "ok"

    # 2. clone_speaker：注册参考音色样本
    tmp = tempfile.mkdtemp(prefix="cosyvoice_ref_")
    wav_path = str(Path(tmp) / "sample.wav")
    with wave.open(wav_path, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(16000)
        wf.writeframes((np.zeros(1600, dtype=np.int16)).tobytes())
    res = client.post("/clone_speaker", json={"sample_wav": wav_path, "speaker_id": "voice_test_1"})
    assert res.status_code == 200
    assert res.json()["ok"] is True

    # 3. inference_stream：流式返回音频字节 (测试桩后端，契约与驱动客户端一致)
    async def fake_synth(text, prompt_wav, speed, volume):
        yield b"\x11" * 128
        yield b"\x22" * 128

    real_synth = mod.synthesize_audio
    mod.synthesize_audio = fake_synth
    try:
        res = client.post("/inference_stream", json={
            "text": "测试一句话", "prompt_wav": wav_path, "speed": 1.0, "volume": 1.0, "stream": True
        })
        assert res.status_code == 200
        body = res.content
        assert b"\x11" * 128 in body and b"\x22" * 128 in body
    finally:
        mod.synthesize_audio = real_synth

    # 4. 缺失文本返回 400
    res = client.post("/inference_stream", json={"text": "", "prompt_wav": "", "speed": 1.0, "volume": 1.0})
    assert res.status_code == 400


# 4.46 测试：渲染后端诚实化 (ADR-16③：能力表述必须与实现一致)
def test_render_backend_honest_fallback():
    """回归: RENDER_BACKEND=musetalk 是空壳开关，请求未交付的后端必须显式回退 procedural 并如实上报"""
    import subprocess
    import sys as _sys
    import textwrap
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    script = textwrap.dedent(
        """
        import os
        os.environ["LIVE_AGENT_RENDER_BACKEND"] = "musetalk"
        from server.adapters.media.musetalk_driver import MuseTalkMediaDriver
        d = MuseTalkMediaDriver()
        assert d.render_backend == "procedural", f"未交付的后端被如实上报: {d.render_backend}"
        st = d.get_preview_status()
        assert st["render_backend"] == "procedural"
        print("HONEST_BACKEND_OK")
        """
    )
    proc = subprocess.run(
        [_sys.executable, "-c", script],
        cwd=str(root),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=90,
    )
    assert proc.returncode == 0, f"stdout={proc.stdout}\nstderr={proc.stderr}"
    assert "HONEST_BACKEND_OK" in proc.stdout


# 4.47 测试：声卡流的 abort/close 必须只在播放工作线程内执行
# (回归：事件循环线程调用 stop() 直接 abort/close 正被 write 的流 → PortAudio 原生崩溃，直播中进程静默死亡)
def test_virtual_audio_stream_lifecycle_worker_thread_only():
    import threading as _threading
    import time as _time
    import numpy as np
    from server.core.media import virtual_audio as _va

    svc = _va.VirtualAudioService()
    calls = []  # (op, thread_id)
    lock = _threading.Lock()

    class FakeStream:
        def __init__(self, samplerate):
            self.samplerate = samplerate
            self.active = True

        def write(self, data):
            with lock:
                calls.append(("write", _threading.get_ident()))
            _time.sleep(0.05)

        def abort(self):
            with lock:
                calls.append(("abort", _threading.get_ident()))

        def close(self):
            with lock:
                calls.append(("close", _threading.get_ident()))
            self.active = False

    holder = {"stream": None}

    def fake_get_stream(sr, gen):
        if holder["stream"] is None or holder["stream"].samplerate != sr:
            holder["stream"] = FakeStream(sr)
            # 模拟真实 _get_stream 的副作用：登记到实例供 _discard_stream_locked 处置
            svc._stream = holder["stream"]
            svc._stream_sr = sr
            svc._stream_gen_used = gen
        return holder["stream"]

    svc._get_stream = fake_get_stream
    caller_tid = _threading.get_ident()

    # 一段较长音频 (worker 写入期间调用 stop)
    pcm = (np.sin(np.linspace(0, 500, 24000)) * 8000).astype("<i2").tobytes()
    svc.play_chunk(pcm, fallback_sample_rate=24000)
    _time.sleep(0.02)  # 确保 worker 已进入 write
    svc.stop()

    # 等待 worker 处理完失效信号并自行关闭流
    deadline = _time.time() + 5
    while _time.time() < deadline:
        with lock:
            ops = [op for op, _ in calls]
            if "close" in ops and "abort" in ops:
                break
        _time.sleep(0.05)

    with lock:
        abort_close_tids = [tid for op, tid in calls if op in ("abort", "close")]
    assert abort_close_tids, "stop() 后流未被关闭"
    assert all(tid != caller_tid for tid in abort_close_tids), (
        "abort/close 在调用方线程 (事件循环) 执行——PortAudio 跨线程并发操作会导致原生崩溃"
    )

    # stop 后再次播放必须能重新打开流工作
    with lock:
        calls.clear()
    svc.play_chunk(pcm[:4800], fallback_sample_rate=24000)
    deadline = _time.time() + 5
    while _time.time() < deadline:
        with lock:
            if any(op == "write" for op, _ in calls):
                break
        _time.sleep(0.05)
    with lock:
        assert any(op == "write" for op, _ in calls), "stop() 后无法恢复播放"
    svc.stop()


# 4.11 测试：电商价格防幻觉双重审计引擎 (规划 §14.3)
def test_price_auditor_guardrail():
    from server.core.guardrails.price_auditor import global_price_auditor

    prod = {"title": "法式重磅真丝衬衫", "live_price": 129.0, "sku": "SKU_SILK_01"}

    # 1. 触发价格幻觉纠偏 (虚报 19.9 元远低于底价 129 元)
    bad_sentence = "家人们今天艾米给你们发大福利，这款只要19.9元包邮带走！"
    corrected, triggered, audit = global_price_auditor.audit_sentence(bad_sentence, current_product=prod)
    assert triggered is True
    assert "只要129元包邮带走" in corrected
    assert audit is not None
    assert audit["spoken_price"] == 19.9
    assert audit["official_price"] == 129.0

    # 2. 正常价格播报，不触发误杀
    good_sentence = "现在小黄车下单只要129元，库存不多快去抢！"
    c2, t2, _ = global_price_auditor.audit_sentence(good_sentence, current_product=prod)
    assert t2 is False
    assert c2 == good_sentence


# 4.12 测试：缺失可选重依赖 (numpy/opencv) 时媒体中枢必须优雅降级而非启动崩溃
def test_media_router_graceful_degradation_without_cv2():
    """
    模拟全新环境未安装 numpy/opencv-python：
    媒体路由必须回退到仿真驱动、状态遥测可读、且不抛异常（回归 ADR-16 部署健壮性）
    """
    import subprocess
    import sys
    import textwrap
    from pathlib import Path

    project_root = Path(__file__).resolve().parents[2]
    script = textwrap.dedent(
        """
        import sys, importlib.abc
        class Blocker(importlib.abc.MetaPathFinder):
            def find_spec(self, name, path=None, target=None):
                if name.split(".")[0] in ("cv2", "numpy"):
                    raise ImportError("blocked for test")
                return None
        sys.meta_path.insert(0, Blocker())
        import asyncio
        from server.adapters.media.musetalk_driver import CV_AVAILABLE
        from server.adapters.media.media_router import global_media_router
        assert CV_AVAILABLE is False
        assert global_media_router.driver_type == "mock"
        status = global_media_router.get_preview_status()
        assert status["cv_available"] is False
        assert "virtual_cam" in status
        assert global_media_router.get_latest_jpeg() == b""
        asyncio.run(global_media_router.start())
        asyncio.run(global_media_router.stop())
        print("DEGRADATION_OK")
        """
    )
    proc = subprocess.run(
        [sys.executable, "-c", script],
        cwd=str(project_root),
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=90,
    )
    assert proc.returncode == 0, f"stdout={proc.stdout}\nstderr={proc.stderr}"
    assert "DEGRADATION_OK" in proc.stdout


# 4.14 测试：娱乐主播小游戏引擎 (成语接龙 / 脑筋急转弯 / 分级打赏)
def test_mini_games_engine():
    from server.core.roles.games import MiniGameEngine, gift_tier

    g = MiniGameEngine()
    intro = g.start_idiom()
    assert "成语接龙" in intro
    assert g.pending_char and g.pending_idiom

    # 正确接龙：使用以当前待接字开头的成语
    good = [i for i in __import__("server.core.roles.games", fromlist=["IDIOMS"]).IDIOMS if i.startswith(g.pending_char)]
    if good:
        reply = g.continue_idiom(good[0])
        assert reply is not None

    riddle_intro = g.start_riddle()
    assert "脑筋急转弯" in riddle_intro
    assert g.current_riddle is not None
    answer = g.current_riddle[1]
    assert "答对" in g.answer_riddle(answer)

    # 分级打赏
    assert gift_tier(100)[0] == "light"
    assert gift_tier(30000)[0] == "high"
    assert gift_tier(10 ** 9)[0] == "super"


# 4.15 测试：真实声学特征提取 (替换占位 embedding)
def test_voice_feature_extraction_real():
    import os
    import tempfile
    import wave
    import numpy as np
    from server.core.audio.features import extract_voice_features, save_embedding

    sr = 16000
    t = np.linspace(0, 1.0, sr, endpoint=False)
    sig = (0.3 * np.sin(2 * np.pi * 220 * t) * 32767).astype(np.int16)
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
        path = f.name
    with wave.open(path, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sr)
        wf.writeframes(sig.tobytes())

    feat = extract_voice_features(path)
    assert feat["dim"] == 128
    assert len(feat["vector"]) == 128
    assert feat["method"] in ("mel_fft", "wave_fft")
    assert any(abs(v) > 1e-6 for v in feat["vector"])

    out = path + ".npy"
    assert save_embedding(feat["vector"], out) is True
    arr = np.load(out)
    assert arr.shape[0] == 128
    os.remove(path)
    os.remove(out)


# 4.16 测试：MCP 工具箱扩充 (商品特写 / 尺码表)
def test_mcp_tools_registered():
    from server.core.tools.tool_bus import global_mcp_tools
    tools = global_mcp_tools.list_tools()
    for name in ["query_stock", "trigger_onscreen_coupon", "product_closeup", "show_size_chart"]:
        assert name in tools


# 4.17 测试：音画同步补偿控制器
def test_av_sync_controller():
    from server.core.media.av_sync import AVSyncController
    c = AVSyncController()
    c.record_render_latency(80)
    assert 0 <= c.recommended_delay_ms <= 300
    c.record_render_latency(9999)
    assert c.recommended_delay_ms <= 300
    c.record_tts_latency(120)
    assert c.get_status()["tts_latency_ms"] == 120


# 4.18 测试：RAG 引擎状态上报 (向量后端)
def test_rag_engine_status():
    from server.core.rag.engine import KnowledgeBaseEngine
    rag = KnowledgeBaseEngine()
    st = rag.get_status()
    assert st["vector_backend"] in ("onnx", "hash")
    assert st["dim"] == 512


# 4.19 测试：人脸关键点检测 (真实检测或中心框兜底)
def test_face_landmark_detection():
    import os
    import tempfile
    from server.core.vision.face_landmarks import detect_face_landmarks

    path = None
    try:
        import numpy as np
        from PIL import Image
        img = Image.fromarray((np.random.rand(240, 200, 3) * 255).astype("uint8"))
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
            path = f.name
        img.save(path)
        res = detect_face_landmarks(path)
        assert "face_box" in res
        assert "method" in res
        assert res["method"] in ("haarcascade", "center_fallback", "read_failed", "opencv_unavailable")
    finally:
        if path and os.path.exists(path):
            os.remove(path)


# 4.20 测试：程序化渲染器帧合成与音频能量映射
def test_procedural_renderer_frame():
    from server.core.media.procedural_renderer import (
        generate_default_portrait, synth_frame, encode_jpeg, audio_rms_to_mouth,
    )
    base = generate_default_portrait(120, 160)
    f_idle = synth_frame(base, 120, 160, 0.0, 0.0)
    f_talk = synth_frame(base, 120, 160, 1.0, 0.8)
    assert f_idle.shape == (160, 120, 3)
    assert f_talk.shape == (160, 120, 3)
    assert encode_jpeg(f_idle)[:2] == b"\xff\xd8"
    m = audio_rms_to_mouth(b"\x00\x00" * 100)
    assert 0.15 <= m <= 1.0


# 4.21 测试：端云分离远程渲染帧接收
def test_remote_gpu_frame_reception():
    import asyncio
    import base64
    from server.adapters.media.remote_gpu_driver import RemoteGPUMediaDriver

    d = RemoteGPUMediaDriver()
    assert d.has_frames is False
    jpeg = b"\xff\xd8remote-frame\xff\xd9"
    asyncio.run(d._handle_video_frame(base64.b64encode(jpeg).decode("ascii")))
    assert d.has_frames is True
    assert d.get_latest_jpeg() == jpeg
    st = d.get_preview_status()
    assert st["render_backend"] == "remote_gpu"
    assert st["remote_frames"] == 1


# 4.22 测试：弹幕平台注册表 (插件化)
def test_danmaku_registry():
    from server.adapters.danmaku.registry import global_danmaku_registry
    assert global_danmaku_registry.has("bilibili")
    assert "bilibili" in global_danmaku_registry.list_platforms()
    # 未注册平台返回 None (由上层回退中继/仿真)
    assert global_danmaku_registry.create("unknown_platform", "1", lambda *a, **k: None) is None


# 4.13 测试：核心、测试与可选依赖清单职责清晰且直接版本精确固定
def test_requirements_are_split_and_exactly_pinned():
    from pathlib import Path
    import re

    root = Path(__file__).resolve().parents[1]
    core = (root / "requirements.txt").read_text(encoding="utf-8").lower()
    test = (root / "requirements-test.txt").read_text(encoding="utf-8").lower()
    optional = (root / "requirements-optional.txt").read_text(encoding="utf-8").lower()

    for pkg in ["fastapi", "sqlalchemy", "numpy"]:
        assert re.search(rf"(?m)^{re.escape(pkg)}[^\n]*==[^\n]+$", core)
    for pkg in ["pytest", "pytest-cov", "opencv-python-headless"]:
        assert re.search(rf"(?m)^{re.escape(pkg)}[^\n]*==[^\n]+$", test)
    for pkg in ["opencv-python", "pyvirtualcam", "openpyxl", "pillow"]:
        assert re.search(rf"(?m)^{re.escape(pkg)}[^\n]*==[^\n]+$", optional)

    for content in (core, test, optional):
        declarations = [line for line in content.splitlines() if line and not line.startswith(("#", "-r"))]
        assert all("==" in line for line in declarations), "直接依赖必须使用精确版本"


# 4.28 测试：音频统一解码器与口型能量驱动的容器格式支持
# (回归：旧 audio_rms_to_mouth 直接把 MP3/WAV 压缩字节当 PCM 算 RMS，口型驱动完全失真)
def test_audio_decode_and_mouth_driving():
    import io
    import numpy as np
    from server.core.media.audio_decode import decode_audio_to_float32
    from server.core.media.procedural_renderer import audio_rms_to_mouth

    sr = 24000
    t = np.linspace(0, 0.1, int(sr * 0.1), endpoint=False)
    quiet_sine = (0.05 * np.sin(2 * np.pi * 220 * t)).astype(np.float32)

    # 1. 裸 PCM 解码
    samples, out_sr = decode_audio_to_float32(
        (quiet_sine * 32767).astype("<i2").tobytes(),
        24000,
        codec="pcm_s16le",
        channels=1,
    )
    assert samples is not None and out_sr == 24000
    assert abs(len(samples) - len(quiet_sine)) <= 1

    # 2. WAV 容器解码 (soundfile 可用时验证采样值保真)
    try:
        import soundfile as sf
        have_sf = True
    except ImportError:
        have_sf = False

    if have_sf:
        buf = io.BytesIO()
        sf.write(buf, quiet_sine, sr, format="WAV")
        wav_bytes = buf.getvalue()
        samples2, out_sr2 = decode_audio_to_float32(wav_bytes, codec="wav")
        assert out_sr2 == sr and samples2 is not None
        # 解码后的 RMS 必须与原始正弦一致，而不是压缩字节的伪随机噪声
        expected_rms = float(np.sqrt(np.mean(quiet_sine ** 2)))
        decoded_rms = float(np.sqrt(np.mean(samples2 ** 2)))
        assert abs(decoded_rms - expected_rms) < 0.01

    # 3. 口型驱动：有声正弦必须显著高于静音，且数值稳定可预期
    mouth_silent = audio_rms_to_mouth(b"\x00\x00" * 4800)
    if have_sf:
        mouth_loud = audio_rms_to_mouth(wav_bytes)
        assert 0.15 <= mouth_silent <= 0.2
        assert mouth_loud > 0.35, "WAV 容器音频的口型驱动值异常 (疑似压缩字节伪 RMS)"
        assert mouth_loud > mouth_silent
        # 确定性校验：解码后正弦 RMS(0.05/√2) × 增益 11.7 ≈ 0.414
        expected_mouth = min(1.0, max(0.15, expected_rms * 11.7))
        assert abs(mouth_loud - expected_mouth) < 0.02, (
            f"口型驱动值 {mouth_loud:.3f} 偏离解码预期 {expected_mouth:.3f} (音频未被正确解码)"
        )

    # 4. 无法解码的垃圾数据安全返回
    samples3, sr3 = decode_audio_to_float32(b"", 24000)
    assert samples3 is None and sr3 == 0




def test_bilibili_direct_json_brotli_and_sync_callback():
    """回归：非压缩 JSON 不得二次解包，Brotli 可解析，同步回调不得被 await。"""
    import asyncio
    import json
    brotli = pytest.importorskip("brotli")
    from server.adapters.danmaku.bilibili_fetcher import BilibiliDanmakuFetcher

    events = []

    def on_event(event_type, user, payload, priority):
        events.append((event_type, user, payload, priority))

    fetcher = BilibiliDanmakuFetcher("123", on_event)
    body = json.dumps({
        "cmd": "DANMU_MSG",
        "info": [[], "直接消息", [0, "同步观众"]],
    }, ensure_ascii=False).encode("utf-8")

    async def run():
        direct = fetcher._pack_packet(1, fetcher.OP_NORMAL, body)
        await fetcher._handle_raw_packet(direct)
        inner = fetcher._pack_packet(0, fetcher.OP_NORMAL, body)
        compressed = fetcher._pack_packet(3, fetcher.OP_NORMAL, brotli.compress(inner))
        await fetcher._handle_raw_packet(compressed)

    asyncio.run(run())
    assert events == [
        ("danmaku", "同步观众", {"text": "直接消息"}, 2),
        ("danmaku", "同步观众", {"text": "直接消息"}, 2),
    ]


def test_llm_http_200_empty_or_compact_sse_falls_back(monkeypatch):
    """回归：兼容 data: 无空格 SSE；HTTP 200 空流必须进入离线兜底。"""
    import asyncio
    from server.core.llm import client as llm_mod
    from server.core.llm.client import LLMClient

    line_batches = [
        ['data:{"choices":[{"delta":{"content":"正常输出"}}]}', "data:[DONE]"],
        [],
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
        return [chunk async for chunk in LLMClient.generate_stream("系统提示", "观众问题")]

    monkeypatch.setattr(llm_mod.httpx, "AsyncClient", FakeAsyncClient)
    monkeypatch.setattr(LLMClient, "get_active_llm_config", fake_config)
    monkeypatch.setattr(LLMClient, "_generate_fallback", staticmethod(lambda *args: "离线兜底"))

    assert asyncio.run(collect()) == ["正常输出"]
    assert "".join(asyncio.run(collect())) == "离线兜底"
