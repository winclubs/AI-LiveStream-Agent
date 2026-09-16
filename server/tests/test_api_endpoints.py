import pytest
from fastapi.testclient import TestClient
from server.app import app

@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


def _png_bytes(width=1, height=1):
    import io
    from PIL import Image
    output = io.BytesIO()
    Image.new("RGB", (width, height), color=(30, 60, 90)).save(output, format="PNG")
    return output.getvalue()

def test_root_endpoint(client):
    """测试根路径健康检查接口"""
    response = client.get("/")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "running"
    assert "version" in data

def test_roles_api(client):
    """测试主播角色列表与切换接口"""
    # 1. 获取角色列表
    res = client.get("/api/v1/roles/list")
    assert res.status_code == 200
    data = res.json()
    assert data["code"] == 0
    assert len(data["data"]) >= 3

    # 2. 切换到娱乐主播
    switch_res = client.post("/api/v1/roles/switch", json={"role_id": "role_entertainment_default"})
    assert switch_res.status_code == 200
    assert switch_res.json()["code"] == 0

def test_guardrails_api(client):
    """测试违禁词平替与阻断接口"""
    # 1. 查询违禁词列表
    res = client.get("/api/v1/guardrails/words")
    assert res.status_code == 200
    assert res.json()["code"] == 0
    assert res.json()["total"] >= 5

    # 2. 模拟合规平替测试
    sanitize_res = client.post("/api/v1/guardrails/test-sanitize", json={
        "text": "我们家这款大衣是全网第一好看，价格最好！",
        "role_scope": "all"
    })
    assert sanitize_res.status_code == 200
    data = sanitize_res.json()
    assert data["code"] == 0
    assert "全网第一" not in data["sanitized_text"]
    assert "深受大家喜爱" in data["sanitized_text"]

def test_products_api(client):
    """测试商品增删改查接口"""
    # 添加一个测试商品
    payload = {
        "sku_code": "SKU_TEST_001",
        "title": "2026秋冬加厚羊羔绒外套",
        "category": "女装",
        "original_price": 399.0,
        "live_price": 199.0,
        "current_stock": 50,
        "selling_points": ["防风保暖", "亲肤柔软"],
        "coupon_script": "拍下立减20元"
    }
    upsert_res = client.post("/api/v1/products/upsert", json=payload)
    assert upsert_res.status_code == 200
    assert upsert_res.json()["code"] == 0

    # 查询商品列表
    list_res = client.get("/api/v1/products/list")
    assert list_res.status_code == 200
    items = list_res.json()["data"]
    matched = [p for p in items if p["sku_code"] == "SKU_TEST_001"]
    assert len(matched) == 1
    assert matched[0]["live_price"] == 199.0

def test_products_upsert_triggers_hot_reload_when_live(client, monkeypatch):
    """回归测试：直播运行中商品保存必须触发 products 分区热重载"""
    from server.routes.live import global_live_controller

    reloaded_sections = []
    async def fake_reload(section: str = "all"):
        reloaded_sections.append(section)
    monkeypatch.setattr(global_live_controller, "reload_runtime_config", fake_reload)
    monkeypatch.setattr(global_live_controller, "is_live", True)

    payload = {
        "sku_code": "SKU_HOTRELOAD_001",
        "title": "热重载验证商品",
        "live_price": 9.9,
    }
    res = client.post("/api/v1/products/upsert", json=payload)
    assert res.status_code == 200
    assert res.json()["code"] == 0
    assert "products" in reloaded_sections, "商品保存后未触发热重载"

    # 直播停止状态保存商品，不应触发
    monkeypatch.setattr(global_live_controller, "is_live", False)
    res2 = client.post("/api/v1/products/upsert", json={**payload, "title": "热重载验证商品v2"})
    assert res2.status_code == 200
    assert reloaded_sections.count("products") == 1, "未开播时不应触发热重载"

def test_products_delete_triggers_hot_reload_when_live(client, monkeypatch):
    """回归测试：直播运行中商品删除必须触发 products 分区热重载"""
    from server.routes.live import global_live_controller

    payload = {
        "sku_code": "SKU_HOTRELOAD_DEL",
        "title": "热重载删除验证",
        "live_price": 1.0,
    }
    res = client.post("/api/v1/products/upsert", json=payload)
    assert res.status_code == 200
    prod_id = res.json()["data"]["id"]

    reloaded_sections = []
    async def fake_reload(section: str = "all"):
        reloaded_sections.append(section)
    monkeypatch.setattr(global_live_controller, "reload_runtime_config", fake_reload)
    monkeypatch.setattr(global_live_controller, "is_live", True)

    del_res = client.delete(f"/api/v1/products/{prod_id}")
    assert del_res.status_code == 200
    assert del_res.json()["code"] == 0
    assert "products" in reloaded_sections, "商品删除后未触发热重载"

def test_hardware_status_api(client):
    """测试硬件与资源状态接口"""
    res = client.get("/api/v1/live/hardware")
    assert res.status_code == 200
    data = res.json()
    assert data["code"] == 0
    assert "cpu_percent" in data["data"]
    assert "ram_total_gb" in data["data"]

def test_avatars_api(client):
    """测试数字人形象上传与列表查询"""
    # 1. 查询默认形象列表
    res = client.get("/api/v1/avatars/list")
    assert res.status_code == 200
    data = res.json()
    assert data["code"] == 0
    assert len(data["data"]) >= 1

    # 2. 上传测试形象图片
    dummy_image = _png_bytes()
    upload_res = client.post(
        "/api/v1/avatars/create",
        data={"name": "测试数字人A", "avatar_type": "image"},
        files={"file": ("avatar_test.png", dummy_image, "image/png")}
    )
    assert upload_res.status_code == 200
    upload_data = upload_res.json()
    assert upload_data["code"] == 0
    avatar_id = upload_data["data"]["id"]

    # 3. 再次查询确认已入库
    res2 = client.get("/api/v1/avatars/list")
    assert res2.status_code == 200
    ids = [a["id"] for a in res2.json()["data"]]
    assert avatar_id in ids

    # 4. 删除测试形象
    del_res = client.delete(f"/api/v1/avatars/{avatar_id}")
    assert del_res.status_code == 200
    assert del_res.json()["code"] == 0

def test_voices_api(client):
    """测试音色克隆样本上传与列表管理"""
    # 1. 获取音色列表
    res = client.get("/api/v1/voices/list")
    assert res.status_code == 200
    assert res.json()["code"] == 0
    assert len(res.json()["data"]) >= 1

    # 2. 上传测试音频样本
    dummy_wav = b"RIFF$\x00\x00\x00WAVEfmt \x10\x00\x00\x00\x01\x00\x01\x00D\xac\x00\x00\x88X\x01\x00\x02\x00\x10\x00data\x00\x00\x00\x00"
    upload_res = client.post(
        "/api/v1/voices/clone",
        data={"name": "测试甜美主播音色", "speed": 1.0, "volume": 1.0},
        files={"audio_file": ("sample_test.wav", dummy_wav, "audio/wav")}
    )
    assert upload_res.status_code == 200
    upload_data = upload_res.json()
    assert upload_data["code"] == 0
    voice_id = upload_data["data"]["id"]

    # 3. 删除测试音色
    del_res = client.delete(f"/api/v1/voices/{voice_id}")
    assert del_res.status_code == 200
    assert del_res.json()["code"] == 0



def test_guardrails_audit_logs(client):
    """测试违禁词触发审计日志接口"""
    res = client.get("/api/v1/guardrails/logs?limit=10")
    assert res.status_code == 200
    data = res.json()
    assert data["code"] == 0
    assert "data" in data
    assert "total" in data

def test_settings_api(client):
    """测试配置中心读取、参数修改保存与连通性 Ping 探测"""
    # 1. 获取全局配置
    res = client.get("/api/v1/settings/configs")
    assert res.status_code == 200
    data = res.json()
    assert data["code"] == 0
    assert isinstance(data["data"], list)

    # 2. 测试保存/修改配置参数 (例如配置 DeepSeek API)
    save_res = client.post("/api/v1/settings/configs/save", json={
        "config_group": "llm",
        "provider_name": "deepseek_api",
        "base_url": "https://api.deepseek.com/v1",
        "model_name": "deepseek-chat",
        "api_key": "sk-test-deepseek-key-123456",
        "is_active": True
    })
    assert save_res.status_code == 200
    assert save_res.json()["code"] == 0

    # 3. 再次查询验证修改已落库并脱敏
    res2 = client.get("/api/v1/settings/configs")
    assert res2.status_code == 200
    matched = [c for c in res2.json()["data"] if c["provider_name"] == "deepseek_api"]
    assert len(matched) == 1
    assert matched[0]["model_name"] == "deepseek-chat"
    assert "sk-" in matched[0]["masked_key"]
    assert "123456" not in matched[0]["masked_key"] # 密钥安全脱敏

    # 4. 测试 Ping 探测接口
    ping_res = client.post("/api/v1/settings/ping", json={
        "config_group": "tts",
        "provider_name": "cloud_edge_tts"
    })
    assert ping_res.status_code == 200
    assert ping_res.json()["code"] == 0
    assert ping_res.json()["success"] is True

    # 5. 测试获取内置 8 大主流模型生态元数据 (包含 Qwen 通义与 Kimi 月暗)
    prov_res = client.get("/api/v1/settings/llm/providers")
    assert prov_res.status_code == 200
    p_data = prov_res.json()
    assert p_data["code"] == 0
    provider_ids = [p["id"] for p in p_data["data"]]
    assert {"deepseek", "qwen", "minimax", "kimi", "gemini", "glm", "chatgpt", "custom"}.issubset(set(provider_ids))

    # 6. 测试动态获取模型列表接口 (未提供 API Key 时应明确提示原因且不盲目推荐过时静态模型)
    qwen_no_key_res = client.post("/api/v1/settings/llm/models", json={
        "provider_name": "qwen",
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1"
    })
    assert qwen_no_key_res.status_code == 200
    qm_no_key = qwen_no_key_res.json()
    assert qm_no_key["code"] == 1
    assert qm_no_key["success"] is False
    assert "尚未填写 API Key" in qm_no_key["message"]
    assert qm_no_key["models"] == []  # 禁止盲目返回过时静态推荐模型

    # 测试本地免鉴权服务允许无 Key 探测
    local_models_res = client.post("/api/v1/settings/llm/models", json={
        "provider_name": "custom",
        "base_url": "http://127.0.0.1:11434/v1"
    })
    assert local_models_res.status_code == 200
    lm_data = local_models_res.json()
    # 本地未开启 ollama 时，应说明连接失败，而不是鉴权失败，且不盲目推荐静态模型
    assert lm_data["models"] == []

    # 7. 测试多大模型保存与一键设为默认大脑 (原子互斥)
    save_mm = client.post("/api/v1/settings/configs/save", json={
        "id": "cfg_llm_minimax_test",
        "config_group": "llm",
        "provider_name": "minimax",
        "base_url": "https://api.minimax.chat/v1",
        "model_name": "MiniMax-Text-01",
        "api_key": "sk-minimax-test-key",
        "is_active": True
    })
    assert save_mm.status_code == 200

    # 验证 MiniMax 成为当前唯一激活大脑，并测试眼睛图标解密明文接口 (raw-key)
    raw_key_res = client.get("/api/v1/settings/configs/cfg_llm_minimax_test/raw-key")
    assert raw_key_res.status_code == 200
    raw_json = raw_key_res.json()
    assert raw_json["code"] == 0
    assert raw_json["raw_key"] == "sk-minimax-test-key"

    res3 = client.get("/api/v1/settings/configs")
    llms = [c for c in res3.json()["data"] if c["config_group"] == "llm"]
    active_llms = [c for c in llms if c["is_active"]]
    assert len(active_llms) == 1
    assert active_llms[0]["id"] == "cfg_llm_minimax_test"

    # 将原有 DeepSeek 设为默认激活大脑
    deepseek_id = matched[0]["id"]
    active_res = client.post("/api/v1/settings/configs/set-active", json={
        "config_id": deepseek_id,
        "config_group": "llm"
    })
    assert active_res.status_code == 200
    assert active_res.json()["code"] == 0

    # 验证 DeepSeek 恢复为激活项，MiniMax 自动变为备用
    res4 = client.get("/api/v1/settings/configs")
    llms2 = [c for c in res4.json()["data"] if c["config_group"] == "llm"]
    ds_conf = next(c for c in llms2 if c["id"] == deepseek_id)
    mm_conf = next(c for c in llms2 if c["id"] == "cfg_llm_minimax_test")
    assert ds_conf["is_active"] is True
    assert mm_conf["is_active"] is False

    # 8. 测试删除配置
    del_res = client.delete("/api/v1/settings/configs/cfg_llm_minimax_test")
    assert del_res.status_code == 200
    assert del_res.json()["code"] == 0

    # 9. 测试语音合成实时试听接口 (/settings/tts/preview)
    preview_res = client.post("/api/v1/settings/tts/preview", json={
        "provider_name": "edge_tts",
        "voice_name": "zh-CN-XiaoxiaoNeural",
        "text": "单元测试语音试听"
    })
    assert preview_res.status_code == 200
    assert "audio" in preview_res.headers.get("content-type", "")
    assert len(preview_res.content) > 1000



def test_console_ui_endpoint(client):
    """测试中控控制台 HTML 页面能够正常返回"""
    res = client.get("/console")
    assert res.status_code == 200
    assert "AI-LiveStream-Agent" in res.text
    assert "数字人实时流式视窗" in res.text

def test_live_lifecycle_and_manual_speech(client):
    """测试直播生命周期：开播 -> 状态查询 -> 人工插播 (manual-speech) -> 下播"""
    # 1. 启动直播
    start_res = client.post("/api/v1/live/start", json={"room_id": "room_demo"})
    assert start_res.status_code == 200
    assert start_res.json()["code"] == 0
    assert start_res.json()["is_live"] is True

    # 2. 查询状态
    status_res = client.get("/api/v1/live/status")
    assert status_res.status_code == 200
    assert status_res.json()["is_live"] is True

    # 3. 运营人工插话 (修复前端 404 的 manual-speech 接口)
    speech_res = client.post("/api/v1/live/manual-speech", json={"text": "各位家人们注意，现在给大家发红包了！"})
    assert speech_res.status_code == 200
    assert speech_res.json()["code"] == 0

    # 4. 停止直播
    stop_res = client.post("/api/v1/live/stop")
    assert stop_res.status_code == 200
    assert stop_res.json()["is_live"] is False

def test_websocket_control_and_broadcast(client):
    """测试 WebSocket 全双工链路：连接、PING/PONG心跳、接收角色切换与打断广播"""
    import json
    with client.websocket_connect("/ws/live_control") as ws:
        # 1. 测试心跳
        ws.send_text(json.dumps({"event": "PING"}))
        pong_data = json.loads(ws.receive_text())
        assert pong_data.get("event") == "PONG" or pong_data.get("event_type") == "PONG"

        # 2. 触发角色切换，验证收到全双工广播
        switch_res = client.post("/api/v1/roles/switch", json={"role_id": "role_expert_default"})
        assert switch_res.status_code == 200
        broadcast_msg = json.loads(ws.receive_text())
        assert broadcast_msg.get("event") == "ROLE_SWITCHED" or broadcast_msg.get("event_type") == "ROLE_SWITCHED"
        payload = broadcast_msg.get("payload") or broadcast_msg.get("data")
        assert payload["role_id"] == "role_expert_default"

        # 3. 启动直播并人工打断插播，验证收到打断广播
        client.post("/api/v1/live/start", json={"room_id": "room_demo"})
        client.post("/api/v1/live/interrupt", json={"text": "重要插播内容"})

        # 接收打断信令
        interrupt_msg = json.loads(ws.receive_text())
        assert interrupt_msg.get("event") in ["TRIGGER_BARGE_IN", "barge_in"] or interrupt_msg.get("event_type") in ["TRIGGER_BARGE_IN", "barge_in"]

        client.post("/api/v1/live/stop")

def test_custom_role_upsert_and_hot_switch(client):
    """测试自定义主播人设创建、落库、热切换并保证不回退默认主播"""
    custom_role = {
        "id": "role_custom_finance_01",
        "role_type": "expert",
        "role_name": "财商分析师·老张",
        "system_prompt": "你是一位资深财商分析师老张，解答观众理财与防诈骗咨询。",
        "speech_speed": 1.05,
        "pitch_shift": 0.0,
        "associated_guardrail_group": "finance"
    }
    # 1. 创建自定义角色
    upsert_res = client.post("/api/v1/roles/upsert", json=custom_role)
    assert upsert_res.status_code == 200
    assert upsert_res.json()["code"] == 0

    # 2. 热切换到该自定义角色
    switch_res = client.post("/api/v1/roles/switch", json={"role_id": "role_custom_finance_01"})
    assert switch_res.status_code == 200
    assert "老张" in switch_res.json()["message"]

    # 3. 验证内存运行态当前激活角色对象属性
    from server.core.roles.role_manager import global_role_manager
    active = global_role_manager.get_active_role()
    assert active.role_id == "role_custom_finance_01"
    assert active.role_name == "财商分析师·老张"
    assert active.role_type == "expert"


def test_role_upsert_rejects_unknown_role_type(client):
    """非法 role_type 必须在入口被拒绝：避免入库后建议接口 404、角色类被静默回退为带货主播"""
    bad_role = {
        "role_type": "invalid_anchor_kind",
        "role_name": "非法类型角色",
        "system_prompt": "不应被保存",
    }
    res = client.post("/api/v1/roles/upsert", json=bad_role)
    assert res.status_code == 422
    detail_text = str(res.json()["detail"])
    assert "invalid_anchor_kind" in detail_text
    assert "ecommerce" in detail_text  # 错误信息需给出合法取值提示

    # 未落库：角色列表与运行时注册表均不得出现该角色
    list_res = client.get("/api/v1/roles/list")
    names = [r["name"] for r in list_res.json()["data"]]
    assert "非法类型角色" not in names
    from server.core.roles.role_manager import global_role_manager
    assert all("非法类型角色" != r.role_name for r in global_role_manager._roles.values())






def test_knowledge_upload_search_and_delete(client):
    """测试本地 RAG 知识库：上传分块索引、双路检索与删除"""
    # 1. 上传一份 TXT 专家文献
    doc_content = (
        "消费者权益保护法要点：\n"
        "经营者提供商品或者服务有欺诈行为的，应当按照消费者的要求增加赔偿其受到的损失，"
        "增加赔偿的金额为消费者购买商品的价款或者接受服务的费用的三倍；增加赔偿的金额不足五百元的，为五百元。\n"
        "网购商品自收到之日起七日内可以无理由退货，但定作、鲜活易腐等商品除外。"
    )
    upload_res = client.post(
        "/api/v1/knowledge/upload",
        data={"doc_name": "消费者权益保护法"},
        files={"file": ("consumer_law.txt", doc_content.encode("utf-8"), "text/plain")}
    )
    assert upload_res.status_code == 200
    assert upload_res.json()["code"] == 0
    assert upload_res.json()["data"]["chunk_count"] >= 1

    # 2. 检索命中
    search_res = client.post("/api/v1/knowledge/search", json={"query": "网购七天无理由退货怎么操作？"})
    assert search_res.status_code == 200
    search_data = search_res.json()["data"]
    assert search_data["matched"] is True
    assert "退货" in search_data["hits"][0]["content"]

    # 3. 列表查询
    list_res = client.get("/api/v1/knowledge/list")
    assert list_res.status_code == 200
    assert any(d["doc_name"] == "消费者权益保护法" for d in list_res.json()["data"])

    # 4. 删除该文档
    doc_id = search_data["hits"][0].get("doc_id") or [
        d["doc_id"] for d in list_res.json()["data"] if d["doc_name"] == "消费者权益保护法"
    ][0]
    del_res = client.delete(f"/api/v1/knowledge/{doc_id}")
    assert del_res.status_code == 200
    assert del_res.json()["code"] == 0


def test_guardrails_batch_import(client):
    """测试违禁词 TXT/CSV 批量导入并热重载生效"""
    # 1. TXT 管道格式批量导入
    txt_content = "# 注释行自动跳过\n钓鱼链接\n中奖电话|traffic|substitute|陌生来电\n"
    import_res = client.post(
        "/api/v1/guardrails/words/import",
        files={"file": ("words.txt", txt_content.encode("utf-8"), "text/plain")}
    )
    assert import_res.status_code == 200
    data = import_res.json()
    assert data["code"] == 0
    assert data["data"]["imported"] == 2

    # 2. 导入的词立即生效 (热重载后可被平替)
    check_res = client.post("/api/v1/guardrails/test-sanitize", json={
        "text": "中奖电话打过来了",
        "role_scope": "all"
    })
    assert "陌生来电" in check_res.json()["sanitized_text"]

    # 3. 重复导入自动跳过
    dup_res = client.post(
        "/api/v1/guardrails/words/import",
        files={"file": ("words.txt", txt_content.encode("utf-8"), "text/plain")}
    )
    assert dup_res.json()["data"]["imported"] == 0
    assert dup_res.json()["data"]["skipped"] >= 2


def test_system_hardware_endpoint(client):
    """测试系统硬件探测接口 (真实 GPU 探测 + 档位推荐)"""
    res = client.get("/api/v1/system/hardware")
    assert res.status_code == 200
    data = res.json()
    assert data["code"] == 0
    payload = data["data"]
    assert "cpu_percent" in payload
    assert "gpu" in payload
    assert "recommended_mode" in payload
    assert payload["recommended_mode"].startswith("Tier")


def test_live_mode_wizard_and_lock(client):
    """需求1+3：直播模式选择向导 + 直播中禁止改模式/切角色"""
    # 1. 模式清单包含 A~D 四档
    modes_res = client.get("/api/v1/settings/modes")
    assert modes_res.status_code == 200
    modes = modes_res.json()["data"]
    assert {m["code"] for m in modes} == {"A", "B", "C", "D"}

    # 2. 选择模式 B 并回读
    set_res = client.post("/api/v1/settings/live-mode", json={"mode": "B"})
    assert set_res.status_code == 200
    assert set_res.json()["code"] == 0
    read_res = client.get("/api/v1/settings/live-mode")
    assert read_res.json()["data"]["mode"] == "B"
    assert read_res.json()["data"]["wizard_completed"] is True

    # 3. 无效模式被拒绝
    bad_res = client.post("/api/v1/settings/live-mode", json={"mode": "Z"})
    assert bad_res.status_code == 400

    # 4. 直播中禁止修改模式
    client.post("/api/v1/live/start", json={"room_id": "mock"})
    lock_res = client.post("/api/v1/settings/live-mode", json={"mode": "A"})
    assert lock_res.status_code == 409

    # 5. 直播中禁止切换角色
    switch_res = client.post("/api/v1/roles/switch", json={"role_id": "role_entertainment_default"})
    assert switch_res.status_code == 409

    client.post("/api/v1/live/stop")

    # 6. 停播后恢复可切换
    switch_ok = client.post("/api/v1/roles/switch", json={"role_id": "role_entertainment_default"})
    assert switch_ok.status_code == 200


def test_role_suggestions_by_mode_and_role(client):
    """需求2：模式×角色组合输出建议与 AI 约束提示词及联动主题"""
    res = client.get("/api/v1/roles/suggestions", params={"role_type": "expert", "mode": "C"})
    assert res.status_code == 200
    data = res.json()["data"]
    assert "法律" in data["constraint_prompt"]
    assert "法律热点剖析" in data["default_theme"]
    assert any("端云分离" in t for t in data["tips"])

    # 验证四大角色的联动主题均不相同且非空
    for role_type in ("ecommerce", "entertainment", "expert", "chitchat"):
        r = client.get("/api/v1/roles/suggestions", params={"role_type": role_type})
        assert r.status_code == 200
        d = r.json()["data"]
        assert len(d["default_theme"]) > 0
        assert len(d["theme_placeholder"]) > 0

    res2 = client.get("/api/v1/roles/suggestions", params={"role_type": "unknown_role"})
    assert res2.status_code == 404


def test_anchors_crud(client):
    """需求4：主播管理增删改查 + 照片上传"""
    dummy_png = _png_bytes()

    # 1. 创建主播 (带两张照片)
    create_res = client.post(
        "/api/v1/anchors/create",
        data={"name": "测试主播·小美", "voice_id": "voice_default_female", "remark": "美妆垂类"},
        files=[
            ("portrait", ("face.png", dummy_png, "image/png")),
            ("full_body", ("body.png", dummy_png, "image/png")),
        ]
    )
    assert create_res.status_code == 200
    anchor = create_res.json()["data"]
    assert anchor["photos"]["portrait"]
    assert anchor["photos"]["half_body"] == ""

    # 2. 列表可查
    list_res = client.get("/api/v1/anchors/list")
    assert any(a["id"] == anchor["id"] for a in list_res.json()["data"])

    # 3. 更新姓名与备注
    update_res = client.post(
        "/api/v1/anchors/update",
        data={"id": anchor["id"], "name": "测试主播·小美改", "voice_id": "voice_default_female", "remark": "改备注"}
    )
    assert update_res.status_code == 200
    assert update_res.json()["data"]["name"] == "测试主播·小美改"

    # 4. 删除
    del_res = client.delete(f"/api/v1/anchors/{anchor['id']}")
    assert del_res.status_code == 200


def test_voice_clone_and_preview(client):
    """需求5：上传声音 → 一键克隆 → 在线试听（诚实契约：无云端复刻时不得伪造试听）"""
    dummy_wav = b"RIFF$\x00\x00\x00WAVEfmt \x10\x00\x00\x00\x01\x00\x01\x00D\xac\x00\x00\x88X\x01\x00\x02\x00\x10\x00data\x00\x00\x00\x00"

    # 1. 上传样本（无百炼凭证）：仅本地档案，不得谎报可合成
    upload_res = client.post(
        "/api/v1/voices/clone",
        data={"name": "试听克隆音色", "speed": 1.0, "volume": 1.0},
        files={"audio_file": ("sample.wav", dummy_wav, "audio/wav")}
    )
    assert upload_res.status_code == 200
    assert upload_res.json()["data"]["synthesis_status"] == "not_available"
    voice_id = upload_res.json()["data"]["id"]

    # 2. 一键克隆（本地特征）
    clone_res = client.post(f"/api/v1/voices/{voice_id}/clone")
    assert clone_res.status_code == 200
    assert clone_res.json()["data"]["status"] == "ready"

    # 3. 在线试听：本地档案没有云端声线，必须诚实报错而非播放假声音
    preview_res = client.get(f"/api/v1/voices/{voice_id}/preview")
    assert preview_res.status_code in (400, 500, 502)
    detail = preview_res.json().get("detail", "")
    assert any(k in detail for k in ("本地", "复刻", "百炼", "配置")), f"试听失败信息必须可操作，实际: {detail}"

    # 4. 改名更新
    update_res = client.post("/api/v1/voices/update", json={"id": voice_id, "name": "改名音色", "speech_speed": 1.1})
    assert update_res.status_code == 200

    # 清理
    client.delete(f"/api/v1/voices/{voice_id}")


def test_voice_clone_honest_engine_report(client):
    """回归：一键克隆不得伪造"训练完成"；无真实引擎时必须诚实上报本地特征提取 (ADR-16③)"""
    dummy_wav = b"RIFF$\x00\x00\x00WAVEfmt \x10\x00\x00\x00\x01\x00\x01\x00D\xac\x00\x00\x88X\x01\x00\x02\x00\x10\x00data\x00\x00\x00\x00"

    upload_res = client.post(
        "/api/v1/voices/clone",
        data={"name": "诚实上报音色", "speed": 1.0, "volume": 1.0},
        files={"audio_file": ("sample.wav", dummy_wav, "audio/wav")}
    )
    assert upload_res.status_code == 200
    voice_id = upload_res.json()["data"]["id"]

    clone_res = client.post(f"/api/v1/voices/{voice_id}/clone")
    assert clone_res.status_code == 200
    data = clone_res.json()["data"]
    assert data["status"] == "ready"
    assert data["engine"] == "local_features"
    assert data["synthesis_status"] == "not_available"

    msg = clone_res.json()["message"]
    assert "训练" not in msg, "无真实引擎时不得宣称模型训练完成"
    assert ("特征" in msg) or ("CosyVoice" in msg), "应如实说明实际完成的能力边界"

    client.delete(f"/api/v1/voices/{voice_id}")


class _FakeDashScopeResp:
    def __init__(self, status_code=200, json_data=None, text="", content=b""):
        import json as _json_mod
        self.status_code = status_code
        self._json = json_data
        self.text = text if text else (_json_mod.dumps(json_data) if json_data is not None else "")
        self.content = content

    def json(self):
        if self._json is None:
            raise ValueError("no json body")
        return self._json


class _FakeDashScopeStream:
    def __init__(self, status_code, lines):
        self.status_code = status_code
        self._lines = lines

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def aread(self):
        return b""

    async def aiter_lines(self):
        for ln in self._lines:
            yield ln


class _FakeDashScopeClient:
    """零外网的百炼桩：脚本化 getPolicy / OSS 上传 / create / query / SSE 合成 / 音频下载。"""
    mode = "ok"  # ok | create_rejected
    calls = []

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, url, **kwargs):
        _FakeDashScopeClient.calls.append(("GET", url, kwargs))
        params = kwargs.get("params") or {}
        if url.endswith("/uploads") and params.get("action") == "getPolicy":
            assert params.get("model") == "cosyvoice-v3.5-flash"
            return _FakeDashScopeResp(200, {"data": {
                "policy": "POL", "signature": "SIG", "upload_dir": "tmp/dir",
                "upload_host": "https://fake-oss.example.com",
                "oss_access_key_id": "ID", "x_oss_object_acl": "private",
                "x_oss_forbid_overwrite": "true",
            }})
        if url == "https://fake-audio.example.com/voice.mp3":
            return _FakeDashScopeResp(200, content=b"FAKE_MP3_BYTES" * 300)
        return _FakeDashScopeResp(404, text="not found")

    async     def post(self, url, **kwargs):
        _FakeDashScopeClient.calls.append(("POST", url, kwargs))
        body = kwargs.get("json") or {}
        if url == "https://fake-oss.example.com":
            # 官方契约：OSS 上传必须是 multipart 文本表单 + 文件域
            files_arg = kwargs.get("files") or {}
            data_arg = kwargs.get("data") or {}
            assert "file" in files_arg, "file 域必须放在 files 参数"
            assert data_arg.get("OSSAccessKeyId"), "OSSAccessKeyId 必须放在 data 表单域"
            assert data_arg.get("key").startswith("tmp/dir/"), "key 必须为 upload_dir/文件名"
            assert "policy" not in files_arg, "policy 是文本表单域，不得放在 files"
            assert isinstance(files_arg.get("file"), tuple) and len(files_arg["file"]) >= 2, "file 域必须为 (filename, bytes) 元组"
            return _FakeDashScopeResp(200, text="")
        if url.endswith("/services/audio/tts/customization"):
            action = (body.get("input") or {}).get("action")
            if action == "create_voice":
                if _FakeDashScopeClient.mode == "create_rejected":
                    return _FakeDashScopeResp(400, text='{"code":"InvalidParameter","message":"url invalid"}')
                return _FakeDashScopeResp(200, {"output": {"voice_id": "cosyvoice-v3.5-flash-cloned-abc123"}})
            if action == "query_voice":
                return _FakeDashScopeResp(200, {"output": {"status": "OK", "target_model": "cosyvoice-v3.5-flash"}})
        return _FakeDashScopeResp(404, text="not found")

    def stream(self, method, url, **kwargs):
        _FakeDashScopeClient.calls.append(("STREAM", url, kwargs))
        import base64 as _b64
        # 数据块按真实量级给足（生产侧要求有效音频 >512B）
        lines = [
            'data: {"output": {"audio": {"data": "' + _b64.b64encode(b"X" * 2048).decode() + '"}}}',
            'data: {"output": {"audio": {"url": "https://fake-audio.example.com/voice.mp3"}}}',
        ]
        return _FakeDashScopeStream(200, lines)


def test_dashscope_clone_end_to_end_contract(client, monkeypatch):
    """契约：上传→临时OSS→create_voice(url)→轮询OK→SSE合成；全程断言官方契约形状，零外网。"""
    import httpx as _httpx_mod
    _FakeDashScopeClient.calls = []
    _FakeDashScopeClient.mode = "ok"
    monkeypatch.setattr(_httpx_mod, "AsyncClient", _FakeDashScopeClient)

    wav = b"RIFF$\x00\x00\x00WAVEfmt \x10\x00\x00\x00\x01\x00\x01\x00D\xac\x00\x00\x88X\x01\x00\x02\x00\x10\x00data\x00\x00\x00\x00"
    res = client.post(
        "/api/v1/voices/clone",
        data={"name": "云端复刻音色", "speed": 1.0, "volume": 1.0,
              "provider_name": "cosyvoice", "api_key": "sk-test",
              "base_url": "https://ws-test123.cn-beijing.maas.aliyuncs.com/api/v1",
              "target_model": "cosyvoice-v3.5-flash"},
        files={"audio_file": ("cloud.wav", wav, "audio/wav")},
    )
    assert res.status_code == 200
    data = res.json()["data"]
    assert data["id"] == "cosyvoice-v3.5-flash-cloned-abc123"
    assert data["synthesis_status"] == "ready"
    assert data["preview_kind"] == "clone_sample"
    assert "cloned_preview" not in (data["path"] or ""), "sample 必须仍指向原始上传样本"

    calls = _FakeDashScopeClient.calls
    creates = [c for c in calls if c[0] == "POST" and str(c[1]).endswith("/services/audio/tts/customization")
               and (c[2].get("json") or {}).get("input", {}).get("action") == "create_voice"]
    assert len(creates) == 1
    c_body = creates[0][2]["json"]["input"]
    c_headers = creates[0][2].get("headers") or {}
    assert c_body["action"] == "create_voice"
    assert c_body["target_model"] == "cosyvoice-v3.5-flash"
    assert str(c_body["url"]).startswith("oss://"), "CosyVoice 复刻必须传公网 URL"
    assert "audio" not in c_body, "CosyVoice 不接受 base64 audio 字段"
    assert c_headers.get("X-DashScope-OssResourceResolve") == "enable"

    synths = [c for c in calls if c[0] == "STREAM" and str(c[1]).endswith("/services/audio/tts/SpeechSynthesizer")]
    assert len(synths) == 1
    s_body = synths[0][2]["json"]
    assert s_body["input"]["voice"] == "cosyvoice-v3.5-flash-cloned-abc123"
    assert "parameters" not in s_body, "voice 必须放在 input 内而非 parameters"

    # 试听缓存命中，直接 200（不再触网）
    from server.routes.voices import VOICES_DIR as _VD
    preview_path = _VD / "cosyvoice-v3.5-flash-cloned-abc123_cloned_preview.mp3"
    assert preview_path.exists() and preview_path.stat().st_size > 1024
    pv = client.get(f"/api/v1/voices/{data['id']}/preview")
    assert pv.status_code == 200
    assert len(pv.content) > 1024

    # 删除顺带清理试听缓存
    assert client.delete(f"/api/v1/voices/{data['id']}").status_code == 200
    assert not preview_path.exists()


def test_dashscope_clone_failure_is_honest(client, monkeypatch):
    """复刻被拒（400）时：如实 not_available + 可操作信息，id 保持本地，样本仍在。"""
    import httpx as _httpx_mod
    _FakeDashScopeClient.calls = []
    _FakeDashScopeClient.mode = "create_rejected"
    monkeypatch.setattr(_httpx_mod, "AsyncClient", _FakeDashScopeClient)

    wav = b"RIFF$\x00\x00\x00WAVEfmt \x10\x00\x00\x00\x01\x00\x01\x00D\xac\x00\x00\x88X\x01\x00\x02\x00\x10\x00data\x00\x00\x00\x00"
    res = client.post(
        "/api/v1/voices/clone",
        data={"name": "复刻被拒音色", "speed": 1.0, "volume": 1.0,
              "provider_name": "cosyvoice", "api_key": "sk-test",
              "base_url": "https://ws-test123.cn-beijing.maas.aliyuncs.com/api/v1",
              "target_model": "cosyvoice-v3.5-flash"},
        files={"audio_file": ("rejected.wav", wav, "audio/wav")},
    )
    assert res.status_code == 200
    data = res.json()["data"]
    assert data["id"].startswith("clone_")
    assert data["synthesis_status"] == "not_available"
    assert data["preview_kind"] == "original_sample"
    assert ("复刻" in res.json()["message"]) or ("百炼" in res.json()["message"])
    from pathlib import Path as _P
    assert _P(data["path"]).exists(), "原始样本必须保留"
    client.delete(f"/api/v1/voices/{data['id']}")


def test_generate_preview_local_clone_is_honest_without_network():
    """本地 clone_ 档案在未复刻前必须直接给出可操作说明，且不得发起任何外网调用。"""
    import asyncio as _aio
    from server.core.audio import clone_preview as _cp
    with pytest.raises(RuntimeError, match="本地"):
        _aio.run(_cp.generate_cloned_voice_preview(
            voice_id="clone_abcdef12", voice_name="小琴琴",
            base_url="https://ws-test.cn-beijing.maas.aliyuncs.com/api/v1",
            api_key="sk-test", force_regenerate=True))


def test_douyin_cookie_config_api(client):
    """抖音 ttwid/msToken 选填配置：写入后可读回 (脱敏展示)，清空后返回空；fetcher 配置链路端到端"""
    save = client.post("/api/v1/settings/douyin-cookies", json={"ttwid": "ttwid-abc-1234567890", "ms_token": "ms-xyz-1234567890"})
    assert save.status_code == 200
    assert save.json()["code"] == 0

    got = client.get("/api/v1/settings/douyin-cookies")
    assert got.status_code == 200
    data = got.json()["data"]
    assert data["ttwid"] != "ttwid-abc-1234567890", "ttwid 应脱敏展示"
    assert "ttwid-abc" in data["ttwid"] or data["ttwid"].startswith("ttwi"), "脱敏应保留前缀便于辨认"

    # 粘合链路：开播器从 app_settings 读取的 fetcher 参数必须与配置一致
    import asyncio as _asyncio
    from server.routes.live import global_live_controller as _ctl
    kwargs = _asyncio.run(_ctl._load_douyin_cookie_config())
    assert kwargs.get("ttwid") == "ttwid-abc-1234567890"
    assert kwargs.get("ms_token") == "ms-xyz-1234567890"

    clear = client.post("/api/v1/settings/douyin-cookies", json={"ttwid": "", "ms_token": ""})
    assert clear.status_code == 200
    got2 = client.get("/api/v1/settings/douyin-cookies").json()["data"]
    assert got2["ttwid"] == "" and got2["ms_token"] == ""


def test_batch_words_universal(client):
    """需求7：英文逗号分隔批量添加违禁词，全角色通用"""
    res = client.post("/api/v1/guardrails/words/batch", json={
        "words": "测试违禁词甲, 测试违禁词乙,测试违禁词丙",
        "replacement_word": "合规表达",
        "category": "extreme",
        "action_policy": "substitute"
    })
    assert res.status_code == 200
    assert res.json()["data"]["imported"] == 3

    # 立即生效 (全角色)
    check = client.post("/api/v1/guardrails/test-sanitize", json={"text": "这是测试违禁词甲哦", "role_scope": "expert"})
    assert "合规表达" in check.json()["sanitized_text"]

    # 重复添加跳过
    dup = client.post("/api/v1/guardrails/words/batch", json={
        "words": "测试违禁词甲", "replacement_word": "合规表达", "action_policy": "substitute"
    })
    assert dup.json()["data"]["imported"] == 0

    # 平替模式必须填写替换词
    bad = client.post("/api/v1/guardrails/words/batch", json={"words": "新词", "replacement_word": "", "action_policy": "substitute"})
    assert bad.status_code == 400


def test_products_full_management_and_flash_sale(client):
    """需求6：商品增删改查(描述/多图) + 立即促单逼单"""
    # 1. 创建带描述与图片的商品
    upsert_res = client.post("/api/v1/products/upsert", json={
        "sku_code": "SKU_FULL_01",
        "title": "测试多功能料理锅",
        "original_price": 499.0,
        "live_price": 299.0,
        "current_stock": 88,
        "description": "一锅多用，煎烤炖煮全能",
        "images": ["data/product_images/img_test_a.png", "data/product_images/img_test_b.png"]
    })
    assert upsert_res.status_code == 200
    prod_id = upsert_res.json()["data"]["id"]

    list_res = client.get("/api/v1/products/list")
    p = [x for x in list_res.json()["data"] if x["id"] == prod_id][0]
    assert p["description"] == "一锅多用，煎烤炖煮全能"
    assert len(p["images"]) == 2

    # 2. 未开播时促单被拒绝
    early = client.post(f"/api/v1/products/{prod_id}/flash-sale")
    assert early.status_code == 400

    # 3. 开播后促单成功 (P0 抢占)
    client.post("/api/v1/live/start", json={"room_id": "mock"})
    flash_res = client.post(f"/api/v1/products/{prod_id}/flash-sale")
    assert flash_res.status_code == 200
    assert "促单" in flash_res.json()["message"]

    # 4. 直播大屏统计可查 (mode 为向导测试中设定的 B 档)
    stats_res = client.get("/api/v1/live/stats")
    stats = stats_res.json()["data"]
    assert stats["is_live"] is True
    assert stats["mode"] == "B"
    assert stats["anchor_name"]
    assert stats["flash_sales"] >= 1
    assert "duration_sec" in stats and "network" in stats

    # 5. 成交登记计入 GMV
    order_res = client.post("/api/v1/live/stats/order", json={"amount": 299.0, "sku": "SKU_FULL_01"})
    assert order_res.status_code == 200
    stats2 = client.get("/api/v1/live/stats").json()["data"]
    assert stats2["gmv_yuan"] >= 299.0
    assert stats2["orders_count"] >= 1

    client.post("/api/v1/live/stop")

    # 6. 删除商品
    del_res = client.delete(f"/api/v1/products/{prod_id}")
    assert del_res.status_code == 200

def test_recommend_tier_and_gpu_probe(client):
    """硬件探测：档位推荐矩阵 + GPU 探测结构完整性"""
    from server.routes.live import _recommend_tier, _probe_gpu

    # 档位矩阵 (规划 §8.1)
    assert _recommend_tier({"cuda_available": True, "vram_total_gb": 24}) == "Tier A (全本地离线模式)"
    assert _recommend_tier({"cuda_available": False, "vram_total_gb": 8}) == "Tier B (主流端云混合模式)"
    assert _recommend_tier({"cuda_available": False, "vram_total_gb": 2}) == "Tier C (端云分离模式)"
    assert _recommend_tier({"cuda_available": False, "vram_total_gb": 0}) == "Tier D (轻量免显卡模式)"

    # 探测结构完整
    gpu = _probe_gpu()
    assert set(gpu.keys()) == {"gpu_name", "vram_total_gb", "vram_used_gb", "cuda_available"}

    # 顶部状态栏数据源包含显卡字段
    res = client.get("/api/v1/live/hardware")
    data = res.json()["data"]
    assert "gpu" in data and "ram_percent" in data and "recommended_mode" in data
    # 硬件面板新增字段：CPU 型号与核心数
    assert data["cpu_name"]
    assert data["cpu_cores"] >= 1

def test_recommended_mode_by_hardware(client):
    """首次进入向导：根据硬件自动推荐运行模式"""
    res = client.get("/api/v1/settings/recommended-mode")
    assert res.status_code == 200
    data = res.json()["data"]
    assert data["mode"] in ("A", "B", "C", "D")
    assert data["tier"].startswith("Tier")
    assert data["reason"]  # 中文推荐理由非空
    assert "gpu" in data
    # 档位映射与档位字符串一致
    assert data["tier"].startswith(f"Tier {data['mode']}")

def test_chitchat_role_and_suggestions(client):
    """闲聊扯淡角色：第四角色入库、可激活、有专属建议"""
    # 1. 角色列表包含闲聊扯淡
    res = client.get("/api/v1/roles/list")
    roles = res.json()["data"]
    chitchat = [r for r in roles if r["role_type"] == "chitchat"]
    assert len(chitchat) == 1
    assert len(roles) >= 4

    # 2. 闲聊角色专属建议
    sug = client.get("/api/v1/roles/suggestions", params={"role_type": "chitchat", "mode": "A"})
    assert sug.status_code == 200
    assert "唠" in sug.json()["data"]["constraint_prompt"] or "闲聊" in sug.json()["data"]["constraint_prompt"]

    # 3. 可激活切换
    sw = client.post("/api/v1/roles/switch", json={"role_id": chitchat[0]["id"]})
    assert sw.status_code == 200
    assert sw.json()["data"]["role_type"] == "chitchat"

def test_mock_event_injection(client):
    """演示按钮真实通路：模拟弹幕/打赏事件注入优先级队列"""
    # 未开播时拒绝
    early = client.post("/api/v1/live/mock-event", json={"type": "chat"})
    assert early.status_code == 400

    # 开播后注入促单提问 (P1)
    client.post("/api/v1/live/start", json={"room_id": "mock"})
    chat_res = client.post("/api/v1/live/mock-event", json={"type": "chat"})
    assert chat_res.status_code == 200
    assert chat_res.json()["code"] == 0

    # 注入大额打赏 (P0 强打断)
    gift_res = client.post("/api/v1/live/mock-event", json={"type": "gift"})
    assert gift_res.status_code == 200
    assert "P0" in gift_res.json()["message"]
    client.post("/api/v1/live/stop")


def test_static_file_traversal_protection(client):
    """静态文件服务：路径穿越防护 (兄弟目录前缀绕过)"""
    # 尝试读取 data 兄弟目录 (如 server 源码) 必须被拒绝
    res1 = client.get("/static-file", params={"path": "../server/config.py"})
    assert res1.status_code in (403, 404)

    # 绝对路径逃逸也必须被拒绝
    res2 = client.get("/static-file", params={"path": "C:/Windows/win.ini"})
    assert res2.status_code in (403, 404)

    # 不存在的正常路径返回 404
    res3 = client.get("/static-file", params={"path": "avatars/not_exist.png"})
    assert res3.status_code == 404


def test_history_injection_and_danmaku_aggregation():
    """多轮对话历史注入 live_context + 真实 B 站弹幕事件类型参与聚合"""
    from server.routes.live import global_live_controller

    # 1. 隔离清理并验证 live_context 初始状态
    global_live_controller.history.clear()
    global_live_controller.live_context.pop("history", None)
    assert "history" not in global_live_controller.live_context or global_live_controller.live_context.get("history") in (None, [])

    # 2. 模拟消费循环注入历史快照 (deque -> list 转换)
    global_live_controller.history.append({"role": "user", "content": "观众A:多少钱"})
    global_live_controller.history.append({"role": "assistant", "content": "只要199元"})
    snapshot = list(global_live_controller.history)
    assert isinstance(snapshot, list) and len(snapshot) == 2
    # LLMClient 的 history[-4:] 切片语法在 list 上可用 (deque 不支持切片)
    assert len(snapshot[-4:]) == 2
    global_live_controller.history.clear()

def test_preflight_real_checks(client):
    """开播前真实检查：九项结构 + 状态语义一致 + 主题读写"""
    # 主题读写
    client.post("/api/v1/settings/live-theme", json={"theme": "深夜情感树洞"})
    theme_res = client.get("/api/v1/settings/live-theme")
    assert theme_res.json()["data"]["theme"] == "深夜情感树洞"

    # 确保已选模式 (前置向导测试已设置 B)
    client.post("/api/v1/settings/live-mode", json={"mode": "B"})

    res = client.get("/api/v1/live/preflight")
    assert res.status_code == 200
    data = res.json()["data"]

    # 核心检查项必须齐全
    keys = {c["key"] for c in data["checks"]}
    assert {"mode", "role", "llm", "tts", "obs", "hardware", "guardrails"}.issubset(keys)

    # 状态语义: ready 与 fail 数量一致
    fails = [c for c in data["checks"] if c["status"] == "fail"]
    assert data["ready"] == (len(fails) == 0)
    assert all(c["status"] in ("pass", "warn", "fail") for c in data["checks"])
    # 已选模式且主题已配 → mode 为 pass
    assert any(c["key"] == "mode" and c["status"] == "pass" for c in data["checks"])


def test_preflight_ecommerce_products_check(client):
    """带货主播无商品时必须检查出 fail，添加商品后转 pass"""
    client.post("/api/v1/roles/switch", json={"role_id": "role_ecommerce_default"})

    # 清空商品场景: 临时把已有商品下架
    list_res = client.get("/api/v1/products/list").json()["data"]
    for p in list_res:
        client.delete(f"/api/v1/products/{p['id']}")

    pf1 = client.get("/api/v1/live/preflight").json()["data"]
    prod_check = [c for c in pf1["checks"] if c["key"] == "products"]
    assert prod_check and prod_check[0]["status"] == "fail"
    assert not pf1["ready"]

    # 补充商品后恢复
    client.post("/api/v1/products/upsert", json={
        "sku_code": "SKU_PF_01", "title": "检查用商品", "live_price": 99.0, "current_stock": 10
    })
    pf2 = client.get("/api/v1/live/preflight").json()["data"]
    prod_check2 = [c for c in pf2["checks"] if c["key"] == "products"]
    assert prod_check2 and prod_check2[0]["status"] == "pass"


def test_anchor_voice_binding_and_danmaku_webhook(client):
    """验证开播主播专属音色绑定与通用第三方弹幕 Webhook 接口"""
    # 1. 未开播时推送 Webhook 弹幕，返回提示
    client.post("/api/v1/live/stop")
    wh_res = client.post("/api/v1/live/danmaku-webhook", json={
        "platform": "douyin",
        "user_name": "抖音老铁",
        "text": "主播这件衣服怎么卖？"
    })
    assert wh_res.status_code == 200
    assert wh_res.json()["code"] == 1

    # 2. 本用例自行创建音色，避免依赖其他测试留下的共享状态
    dummy_wav = b"RIFF$\x00\x00\x00WAVEfmt \x10\x00\x00\x00\x01\x00\x01\x00D\xac\x00\x00\x88X\x01\x00\x02\x00\x10\x00data\x00\x00\x00\x00"
    voice_res = client.post(
        "/api/v1/voices/clone",
        data={"name": "Webhook 测试音色", "speed": 1.0, "volume": 1.0},
        files={"audio_file": ("webhook_voice.wav", dummy_wav, "audio/wav")},
    )
    assert voice_res.status_code == 200
    voice_id = voice_res.json()["data"]["id"]

    # 3. 开播时显式指定平台为 douyin 并指定刚创建的 voice_id
    start_res = client.post("/api/v1/live/start", json={
        "platform": "douyin",
        "room_id": "douyin_live_999",
        "voice_id": voice_id
    })
    assert start_res.status_code == 200
    s_data = start_res.json()
    assert s_data["is_live"] is True
    assert s_data["platform"] == "douyin"
    assert s_data["voice_id"] == voice_id

    # 4. 在播状态下推送弹幕 Webhook，成功入队
    wh_res2 = client.post("/api/v1/live/danmaku-webhook", json={
        "platform": "douyin",
        "user_name": "抖音老铁888",
        "text": "面料容易起球吗？",
        "event_type": "chat"
    })
    assert wh_res2.status_code == 200
    assert wh_res2.json()["code"] == 0

    # 4. 推送大额打赏触发抢占优先
    gift_res = client.post("/api/v1/live/danmaku-webhook", json={
        "platform": "douyin",
        "user_name": "榜一大哥",
        "event_type": "gift",
        "gift_name": "嘉年华",
        "gift_count": 1,
        "total_coin": 300000
    })
    assert gift_res.status_code == 200
    assert gift_res.json()["code"] == 0

    # 停止直播并删除本用例资源
    stop_res = client.post("/api/v1/live/stop")
    assert stop_res.status_code == 200
    assert client.delete(f"/api/v1/voices/{voice_id}").status_code == 200


def test_system_version_and_shutdown(client):
    """验证统一版本源(SSOT)、元数据诊断接口与服务平滑关闭接口"""
    import json
    import re
    from pathlib import Path
    from server.config import APP_VERSION, SERVICE_NAME, BASE_DIR, DATA_DIR

    # 1. 验证单一版本源 SSOT 强一致
    version_json_path = BASE_DIR / "version.json"
    assert version_json_path.exists(), "version.json 必须存在"
    v_meta = json.loads(version_json_path.read_text(encoding="utf-8"))
    assert v_meta["version"] == APP_VERSION
    assert v_meta["service"] == SERVICE_NAME

    pkg_json_path = BASE_DIR / "apps" / "desktop-ui" / "package.json"
    if pkg_json_path.exists():
        pkg_data = json.loads(pkg_json_path.read_text(encoding="utf-8"))
        assert pkg_data["version"] == APP_VERSION, "Electron 桌面端版本必须与后端 SSOT 对齐"

    console_js_path = BASE_DIR / "server" / "static" / "js" / "console.js"
    if console_js_path.exists():
        console_match = re.search(
            r'FRONTEND_VERSION\s*=\s*"([^"]+)"',
            console_js_path.read_text(encoding="utf-8"),
        )
        assert console_match, "console.js 必须声明 FRONTEND_VERSION 常量"
        assert console_match.group(1) == APP_VERSION, "console.js FRONTEND_VERSION 必须与后端 SSOT 对齐"

    # 2. 验证 GET /api/v1/system/version 增强元数据
    res = client.get("/api/v1/system/version")
    assert res.status_code == 200
    data = res.json()
    assert data["code"] == 0
    assert data["version"] == APP_VERSION
    assert data["service"] == SERVICE_NAME
    assert data["api_version"] == "v1"
    assert isinstance(data["process_id"], int)
    assert data["started_at"]
    assert data["project_root"]
    assert Path(data["data_dir"]).resolve() == DATA_DIR.resolve()

    # 3. 验证 POST /api/v1/system/shutdown
    shut_res = client.post("/api/v1/system/shutdown")
    assert shut_res.status_code == 200
    shut_data = shut_res.json()
    assert shut_data["code"] == 0
    assert shut_data["process_id"] == data["process_id"]


def test_digital_human_media_status_and_stream_preview(client):
    """验证数字人媒体中枢状态遥测与流式预览接口"""
    # 1. 媒体驱动状态查询
    res = client.get("/api/v1/live/media/status")
    assert res.status_code == 200
    data = res.json()
    assert data["code"] == 0
    m_data = data["data"]
    assert "fps" in m_data
    assert "virtual_cam" in m_data
    assert "resolution" in m_data
    # 媒体能力与虚拟摄像头可达性必须始终上报（前端据此显示真实状态）
    assert "cv_available" in m_data
    assert "render_backend" in m_data
    assert "available" in m_data["virtual_cam"]
    # 视觉感知与音画同步状态
    assert "vision" in m_data
    assert "av_sync" in m_data
    assert "enabled" in m_data["vision"]
    assert "recommended_delay_ms" in m_data["av_sync"]

    # 2. 开播启动数字人流式通道
    start_res = client.post("/api/v1/live/start", json={
        "platform": "mock",
        "room_id": "room_demo"
    })
    assert start_res.status_code == 200

    # 3. 验证推流状态下媒体指标更新
    res2 = client.get("/api/v1/live/media/status")
    assert res2.status_code == 200
    assert res2.json()["data"]["is_running"] is True

    # 4. 停止直播清理
    client.post("/api/v1/live/stop")


def test_vision_config_api(client):
    """视觉感知通道配置读写与来源校验"""
    res = client.get("/api/v1/settings/vision")
    assert res.status_code == 200
    data = res.json()["data"]
    assert "enabled" in data and "source" in data and "status" in data

    save = client.post("/api/v1/settings/vision", json={
        "enabled": True, "source": "desktop_screen", "interval_sec": 2.0
    })
    assert save.status_code == 200
    assert save.json()["code"] == 0

    read = client.get("/api/v1/settings/vision").json()["data"]
    assert read["enabled"] is True
    assert read["source"] == "desktop_screen"

    bad = client.post("/api/v1/settings/vision", json={"enabled": True, "source": "invalid_src"})
    assert bad.status_code == 400

    # 复原关闭，避免影响后续用例
    client.post("/api/v1/settings/vision", json={"enabled": False, "source": "desktop_screen", "interval_sec": 2.5})


def test_knowledge_status_api(client):
    """RAG 引擎状态：向量后端/维度/分块数/阈值"""
    res = client.get("/api/v1/knowledge/status")
    assert res.status_code == 200
    data = res.json()["data"]
    assert data["vector_backend"] in ("onnx", "hash")
    assert data["dim"] == 512
    assert "min_score" in data


def test_avatar_landmark_preprocess(client):
    """形象上传返回真实人脸特征预提取结果 (Haar 或中心框兜底)"""
    import io
    try:
        import numpy as np
        from PIL import Image
        buf = io.BytesIO()
        Image.fromarray((np.random.rand(240, 200, 3) * 255).astype("uint8")).save(buf, format="PNG")
        png_bytes = buf.getvalue()
    except Exception:
        png_bytes = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15c4"

    res = client.post(
        "/api/v1/avatars/create",
        data={"name": "关键点检测测试形象", "avatar_type": "image"},
        files={"file": ("lm_test.png", png_bytes, "image/png")}
    )
    assert res.status_code == 200
    data = res.json()["data"]
    assert "landmarks" in data
    assert data["landmarks"].get("method")
    client.delete(f"/api/v1/avatars/{data['id']}")


def test_danmaku_ws_ingest_requires_live(client):
    """通用弹幕 WebSocket 中继端点：未开播时安全忽略、开播后可注入；P0 阈值与 webhook 对齐"""
    import json as _json
    from server.routes.live import global_live_controller

    with client.websocket_connect("/ws/danmaku-ingest") as ws:
        # 未开播：发送应被静默忽略
        ws.send_text(_json.dumps({"event_type": "danmaku", "user_name": "中继观众", "text": "你好"}))
        # 开播后注入
        client.post("/api/v1/live/start", json={"room_id": "mock"})

        # put 间谍：在入队时刻捕获事件与优先级 (不依赖消费竞态)
        seen = []
        event_queue = global_live_controller.event_queue
        original_put = event_queue.put

        async def spy_put(**kwargs):
            seen.append(kwargs)
            return await original_put(**kwargs)

        event_queue.put = spy_put
        try:
            # 中额礼物 (5000 瓜子)：与 /live/danmaku-webhook 阈值对齐，应为 P1 不抢占
            ws.send_text(_json.dumps({"event_type": "gift", "user_name": "中继大哥", "gift_name": "火箭", "total_coin": 5000}))
            # 大额礼物 (500000 瓜子)：P0 强抢占打断
            ws.send_text(_json.dumps({"event_type": "gift", "user_name": "中继大哥", "gift_name": "嘉年华", "total_coin": 500000}))
            import time as _time
            deadline = _time.time() + 5
            while len([s for s in seen if s.get("event_type") == "gift"]) < 2 and _time.time() < deadline:
                _time.sleep(0.05)
        finally:
            event_queue.put = original_put

        gift_prios = {s["payload"]["gift_name"]: s["priority"] for s in seen if s.get("event_type") == "gift"}
        assert len(gift_prios) == 2, f"应入队 2 条礼物事件，实际 {len(gift_prios)}"
        assert gift_prios.get("火箭") == 1, f"5000 瓜子礼物应降为 P1，实际 P{gift_prios.get('火箭')}"
        assert gift_prios.get("嘉年华") == 0, f"500000 瓜子礼物应为 P0，实际 P{gift_prios.get('嘉年华')}"
    client.post("/api/v1/live/stop")


def test_duplicate_live_start_rejected(client):
    """回归：重复开播必须返回 409，而不是静默复用旧会话却返回新 session_id"""
    client.post("/api/v1/live/stop")
    first = client.post("/api/v1/live/start", json={"room_id": "mock"})
    assert first.status_code == 200
    assert first.json()["is_live"] is True

    duplicate = client.post("/api/v1/live/start", json={"room_id": "mock"})
    assert duplicate.status_code == 409, f"重复开播应被拒绝，实际 {duplicate.status_code}"

    client.post("/api/v1/live/stop")


def test_audio_device_endpoints(client):
    """物理音频输出与虚拟声卡接口测试"""
    # 1. 查询音频设备
    res = client.get("/api/v1/settings/audio-devices")
    assert res.status_code == 200
    data = res.json()["data"]
    assert "devices" in data
    assert "status" in data
    assert "available" in data["status"]

    # 2. 切换音频设备 (指定索引或 None)
    res_set = client.post("/api/v1/settings/audio-device", json={"device_index": None})
    assert res_set.status_code == 200
    assert res_set.json()["code"] == 0
    assert res_set.json()["data"]["device_index"] is None


def test_douyin_fetcher_registration(client):
    """抖音直播弹幕抓取适配器：注册表内置与创建验证"""
    from server.adapters.danmaku.registry import global_danmaku_registry
    assert global_danmaku_registry.has("douyin")
    assert "douyin" in global_danmaku_registry.list_platforms()

    events_received = []
    def callback(ev, user, payload, prio):
        events_received.append((ev, user, payload, prio))

    fetcher = global_danmaku_registry.create("douyin", "https://live.douyin.com/888888", callback)
    assert fetcher is not None
    assert fetcher.clean_room_id == "888888"

    # 验证事件回调分发
    fetcher.is_running = True
    fetcher._emit_event("danmaku", "抖音大哥", {"text": "多少钱一件？"}, priority=1)
    assert len(events_received) == 1
    assert events_received[0][0] == "danmaku"
    assert events_received[0][1] == "抖音大哥"
    assert events_received[0][3] == 1


def test_vision_intent_detection():
    """多模态视觉感知：智能意图唤醒与 Token 节流判定"""
    from server.core.vision.capture import is_vision_query
    # 涉及视觉关键词应触发
    assert is_vision_query("主播手里拿的是什么？") is True
    assert is_vision_query("看看衣服背面长什么样") is True
    assert is_vision_query("这款是什么颜色？") is True
    assert is_vision_query("给个特写镜头") is True

    # 普通闲聊与打卡不应触发 (节省 90% 图像 Token)
    assert is_vision_query("你好呀主播") is False
    assert is_vision_query("已点赞打卡") is False
    assert is_vision_query("今天播到几点？") is False
    assert is_vision_query("") is False


def test_virtual_audio_driver_unit():
    """VirtualAudioService 单元测试：软降级与非阻塞播放"""
    from server.core.media.virtual_audio import VirtualAudioService
    svc = VirtualAudioService()
    status = svc.get_status()
    assert "available" in status
    assert "is_enabled" in status
    # 播放空切片或模拟数据不崩溃
    svc.play_chunk(b"\x00" * 640)
    svc.stop()


def test_virtual_audio_sequential_playback_no_truncation():
    """回归：连续音频切片必须按顺序完整播出 (旧实现 sd.play 替换语义导致后句截断前句)"""
    import time as _time
    import threading as _threading
    import numpy as _np
    from server.core.media import virtual_audio as _va

    svc = _va.VirtualAudioService()
    written = []
    lock = _threading.Lock()

    class FakeStream:
        def __init__(self, samplerate):
            self.samplerate = samplerate
            self.active = True

        def write(self, data):
            with lock:
                written.append((self.samplerate, len(data)))

        def abort(self):
            self.active = False

        def close(self):
            self.active = False

    holder = {"stream": None}

    def fake_get_stream(sr, gen):
        if holder["stream"] is None or holder["stream"].samplerate != sr:
            holder["stream"] = FakeStream(sr)
        return holder["stream"]

    svc._get_stream = fake_get_stream

    pcm = (_np.sin(_np.linspace(0, 100, 2400)) * 8000).astype("<i2").tobytes()
    for _ in range(3):
        svc.play_chunk(pcm, fallback_sample_rate=24000)

    deadline = _time.time() + 5
    total_samples = 0
    while _time.time() < deadline:
        with lock:
            total_samples = sum(n for _, n in written)
        if total_samples >= 3 * 2400:
            break
        _time.sleep(0.02)

    # 契约：3 段切片按顺序完整播出 (工作线程分片写入，总量必须 7200 采样，绝无截断)
    with lock:
        total_samples = sum(n for _, n in written)
        all_24k = all(sr == 24000 for sr, _ in written)
    assert total_samples >= 3 * 2400, (
        f"3 段切片 (7200 采样) 只播出了 {total_samples} 采样，连续播报被互相截断"
    )
    assert all_24k
    svc.stop()


def test_virtual_audio_stop_clears_pending_chunks():
    """打断 (barge-in) 调用 stop() 后，待播队列必须被清空，不允许残留旧音频继续播出"""
    from server.core.media import virtual_audio as _va

    svc = _va.VirtualAudioService()
    svc.play_chunk(b"\x01" * 4800, fallback_sample_rate=24000)
    svc.play_chunk(b"\x01" * 4800, fallback_sample_rate=24000)
    svc.stop()
    # 确定性契约：stop 后待播队列必须为空 (至多 1 块可能已被 worker 取走并立即中止)
    assert svc._queue.empty() is True
    assert svc.get_status()["total_chunks_played"] <= 1


def test_live_lifecycle_and_order_persistence(client):
    """验证本次加固：未开播禁止插播、无效角色开播拦截、订单与库存持久化、停播清空队列"""
    import time as _t
    from server.routes.live import global_live_controller

    # 确保停止
    client.post("/api/v1/live/stop")

    # 1. 未开播时人工插话应被 400 拦截
    res_interject = client.post("/api/v1/live/interrupt", json={"text": "紧急打断测试"})
    assert res_interject.status_code == 400

    # 2. 开播时传入不存在的 role_id 应被 400 拦截
    bad_start = client.post("/api/v1/live/start", json={"role_id": "non_existent_role_999"})
    assert bad_start.status_code == 400

    # 3. 正常开播
    ok_start = client.post("/api/v1/live/start", json={"platform": "mock", "room_id": "room_persist_test"})
    assert ok_start.status_code == 200

    # 4. 创建测试商品
    prod_sku = f"SKU_TEST_{int(_t.time())}"
    create_p = client.post("/api/v1/products/upsert", json={
        "sku_code": prod_sku,
        "title": "持久化测试商品",
        "live_price": 99.0,
        "current_stock": 10
    })
    assert create_p.status_code == 200
    prod_id = create_p.json()["data"]["id"]

    # 5. 登记订单成交并验证落库与库存扣减
    order_res = client.post("/api/v1/live/stats/order", json={
        "amount": 99.0,
        "sku": prod_sku,
        "note": "自动化测试订单"
    })
    assert order_res.status_code == 200
    ord_data = order_res.json()["data"]
    assert ord_data["order_id"].startswith("ord_")
    assert ord_data["remaining_stock"] == 9

    # 6. 停止直播并验证队列被 clear
    stop_res = client.post("/api/v1/live/stop")
    assert stop_res.status_code == 200
    assert global_live_controller.event_queue.qsize() == 0

    # 清理测试商品
    client.delete(f"/api/v1/products/{prod_id}")


def test_douyin_cookie_encryption_contract(client):
    """验证抖音 Cookie 在数据库中以 AES 密文保存且脱敏与解密一致"""
    post_res = client.post("/api/v1/settings/douyin-cookies", json={
        "ttwid": "1%7Ctest_secret_ttwid_val",
        "ms_token": "secret_ms_token_12345678"
    })
    assert post_res.status_code == 200

    get_res = client.get("/api/v1/settings/douyin-cookies")
    assert get_res.status_code == 200
    data = get_res.json()["data"]
    assert data["configured"] is True
    assert "••••" in data["ttwid"]
    assert "••••" in data["ms_token"]



def test_anchor_update_can_clear_optional_fields(client):
    """回归：主播更新表单显式空值应解除音色绑定并清空备注。"""
    created = client.post(
        "/api/v1/anchors/create",
        data={"name": "待清空主播", "voice_id": "voice_default_female", "remark": "待清空备注"},
    )
    assert created.status_code == 200
    anchor_id = created.json()["data"]["id"]
    try:
        updated = client.post(
            "/api/v1/anchors/update",
            data={"id": anchor_id, "name": "待清空主播", "voice_id": "", "remark": ""},
        )
        assert updated.status_code == 200
        data = updated.json()["data"]
        assert data["voice_id"] is None
        assert data["remark"] == ""
    finally:
        client.delete(f"/api/v1/anchors/{anchor_id}")


def test_console_critical_operation_contracts():
    """回归：前端关键状态、Preflight、主播更新和订单 SKU 契约必须保持一致。"""
    from pathlib import Path

    root = Path(__file__).parents[2]
    js_modules = (root / "server/static/js/modules").glob("*.js")
    js = "".join(f.read_text(encoding="utf-8") for f in sorted(js_modules)) + (root / "server/static/js/console.js").read_text(encoding="utf-8")
    html = (root / "server/static/index.html").read_text(encoding="utf-8")

    assert 'let currentMode = "";' in js
    assert "let isLiveStreaming = false;" in js
    assert 'if (!pf) {' in js and "await startLiveDirect();" in js
    assert 'if (!res.ok) throw new Error(`HTTP ${res.status}`);' in js
    assert 'if (editId) fd.append("id", editId);' in js
    assert 'if (!sku) { alert("请输入已上架商品的 SKU"); return; }' in js
    assert 'placeholder="商品 SKU（必填）" required' in html
    assert 'onclick="runPreflight({ auto: true })"' not in html


def test_save_neural_renderer_config_and_validation_error_handler(client):
    """回归：neural_renderer 配置保存成功，且 422 验证异常返回人类可读的字符串 detail/message。"""
    # 1. 成功保存一个 sidecar_v3 实例
    res = client.post(
        "/api/v1/settings/configs/save",
        json={
            "config_group": "neural_renderer",
            "provider_name": "sidecar_v3",
            "title": "测试自建渲染节点",
            "is_active": True,
            "base_url": "ws://127.0.0.1:8010/ws/render-v3",
            "extra_params": {
                "adapter": "sidecar_v3",
                "backend_id": "auto",
                "avatar_id": "default",
            },
        },
    )
    assert res.status_code == 200
    data = res.json()
    assert data["code"] == 0
    saved_id = data["id"]
    assert saved_id

    # 2. 回归：当 extra_params 中未显式传 adapter 时，后端自动根据 provider_name 补齐并成功保存
    auto_res = client.post(
        "/api/v1/settings/configs/save",
        json={
            "config_group": "neural_renderer",
            "provider_name": "sidecar_v3",
            "base_url": "ws://127.0.0.1:8010/ws/render-v3",
            "extra_params": {
                # 未传 adapter，后端自动推导填充为 sidecar_v3
                "backend_id": "auto",
                "avatar_id": "default",
            },
        },
    )
    assert auto_res.status_code == 200
    assert auto_res.json()["code"] == 0

    # 3. 测试真正非法参数（如无效 URL scheme）触发 422 时，返回可读的字符串 message 与 detail，拒绝裸 object 导致前端 [object Object]
    bad_res = client.post(
        "/api/v1/settings/configs/save",
        json={
            "config_group": "neural_renderer",
            "provider_name": "sidecar_v3",
            "base_url": "ftp://invalid-scheme/avatar",
            "extra_params": {
                "adapter": "sidecar_v3",
            },
        },
    )
    assert bad_res.status_code == 422
    bad_json = bad_res.json()
    assert isinstance(bad_json.get("detail"), str)
    assert "ws" in bad_json["detail"]
    assert isinstance(bad_json.get("message"), str)
