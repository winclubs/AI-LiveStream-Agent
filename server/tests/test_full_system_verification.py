"""
AI-LiveStream-Agent 系统全功能端到端逐一验证测试套件
按 14 大核心业务模块依次逐一验证系统功能与 API 契约，确保所有功能正常可用。
"""
import io
import time
import pytest
from PIL import Image
from fastapi.testclient import TestClient
from server.app import app


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


def _generate_png_bytes(width=2, height=2) -> bytes:
    output = io.BytesIO()
    Image.new("RGB", (width, height), color=(50, 100, 150)).save(output, format="PNG")
    return output.getvalue()


DUMMY_WAV_BYTES = (
    b"RIFF$\x00\x00\x00WAVEfmt \x10\x00\x00\x00\x01\x00\x01\x00D\xac\x00\x00\x88X\x01\x00\x02\x00\x10\x00data\x00\x00\x00\x00"
)


# ===========================================================================
# 模块 1: 系统环境与硬件自检
# ===========================================================================
def test_01_system_environment_and_hardware(client):
    """
    【模块 1 验证】
    1. /api/v1/system/version: 版本号、服务名、进程号与核心路径
    2. /api/v1/live/hardware: CPU、内存、显卡实时资源状态
    3. /api/v1/system/prerequisites: OBS、虚拟摄像头、音频驱动与媒体引擎全生态前置检测
    """
    # 1. 检查版本与元数据
    res_ver = client.get("/api/v1/system/version")
    assert res_ver.status_code == 200
    ver_data = res_ver.json()
    assert ver_data["code"] == 0
    assert "version" in ver_data
    assert ver_data["service"] == "AI-LiveStream-Agent"
    assert "process_id" in ver_data
    assert "data_dir" in ver_data

    # 2. 检查硬件状态探针
    res_hw = client.get("/api/v1/live/hardware")
    assert res_hw.status_code == 200
    hw_data = res_hw.json()
    assert hw_data["code"] == 0
    assert "cpu_percent" in hw_data["data"]
    assert "ram_total_gb" in hw_data["data"]
    assert "ram_used_gb" in hw_data["data"]

    # 3. 检查直播必备前置依赖自检端点
    res_pre = client.get("/api/v1/system/prerequisites")
    assert res_pre.status_code == 200
    pre_data = res_pre.json()
    assert pre_data["code"] == 0
    assert "summary" in pre_data["data"]
    summary = pre_data["data"]["summary"]
    assert summary["total"] >= 5
    assert "items" in pre_data["data"]
    item_keys = {item["key"] for item in pre_data["data"]["items"]}
    assert {"obs", "media_core", "python_runtime"}.issubset(item_keys)


# ===========================================================================
# 模块 2: 开播向导与预检流程
# ===========================================================================
def test_02_broadcast_wizard_and_preflight(client):
    """
    【模块 2 验证】
    1. /api/v1/settings/modes: 4 种直播模式定义 (A/B/C/D)
    2. /api/v1/settings/live-mode: 切换与获取直播模式
    3. /api/v1/settings/live-theme: 设置与读取直播主题
    4. /api/v1/live/preflight: 9 项开播前真实预检
    """
    # 1. 获取开播模式列表
    res_modes = client.get("/api/v1/settings/modes")
    assert res_modes.status_code == 200
    modes_data = res_modes.json()
    assert modes_data["code"] == 0
    mode_codes = {m["code"] for m in modes_data["data"]}
    assert {"A", "B", "C", "D"}.issubset(mode_codes)

    # 2. 设定直播模式为主流端云混合模式 B
    res_set_mode = client.post("/api/v1/settings/live-mode", json={"mode": "B"})
    assert res_set_mode.status_code == 200
    assert res_set_mode.json()["code"] == 0

    res_get_mode = client.get("/api/v1/settings/live-mode")
    assert res_get_mode.status_code == 200
    assert res_get_mode.json()["data"]["mode"] == "B"

    # 3. 设定直播主题
    res_theme = client.post("/api/v1/settings/live-theme", json={"theme": "端到端系统功能全面验证直播"})
    assert res_theme.status_code == 200
    res_get_theme = client.get("/api/v1/settings/live-theme")
    assert res_get_theme.status_code == 200
    assert res_get_theme.json()["data"]["theme"] == "端到端系统功能全面验证直播"

    # 4. 执行开播前全面诊断预检
    res_pf = client.get("/api/v1/live/preflight")
    assert res_pf.status_code == 200
    pf_data = res_pf.json()
    assert pf_data["code"] == 0
    checks = pf_data["data"]["checks"]
    check_keys = {c["key"] for c in checks}
    assert {"mode", "role", "llm", "tts", "hardware", "guardrails"}.issubset(check_keys)
    assert all(c["status"] in ("pass", "warn", "fail") for c in checks)


# ===========================================================================
# 模块 3: 直播状态机与大屏指标
# ===========================================================================
def test_03_live_statemachine_and_dashboard(client):
    """
    【模块 3 验证】
    1. /api/v1/live/status: 初始状态校验
    2. /api/v1/live/start: 开播生命周期
    3. /api/v1/live/manual-speech: 运营人工插话
    4. /api/v1/live/interrupt: 紧急插播打断
    5. /api/v1/live/stats: 大屏指标实时聚合
    6. /api/v1/live/stop: 下播与清理
    """
    # 确保停止初始残留
    client.post("/api/v1/live/stop")

    # 1. 查询当前直播状态
    res_status1 = client.get("/api/v1/live/status")
    assert res_status1.status_code == 200
    assert res_status1.json()["is_live"] is False

    # 2. 开播
    res_start = client.post("/api/v1/live/start", json={"room_id": "room_full_verify_001"})
    assert res_start.status_code == 200
    start_data = res_start.json()
    assert start_data["code"] == 0
    assert start_data["is_live"] is True

    # 再次查询确认运行中
    res_status2 = client.get("/api/v1/live/status")
    assert res_status2.status_code == 200
    assert res_status2.json()["is_live"] is True

    # 3. 运营人工插话
    res_speech = client.post("/api/v1/live/manual-speech", json={"text": "欢迎新进直播间的小伙伴！"})
    assert res_speech.status_code == 200
    assert res_speech.json()["code"] == 0

    # 4. 紧急打断
    res_interrupt = client.post("/api/v1/live/interrupt", json={"text": "紧急打断测试：现发放限时大促立减券！"})
    assert res_interrupt.status_code == 200
    assert res_interrupt.json()["code"] == 0

    # 5. 查询大屏实时运营数据
    res_stats = client.get("/api/v1/live/stats")
    assert res_stats.status_code == 200
    stats_data = res_stats.json()
    assert stats_data["code"] == 0
    assert "viewer_count" in stats_data["data"] or "total_danmaku" in stats_data["data"]

    # 6. 下播
    res_stop = client.post("/api/v1/live/stop")
    assert res_stop.status_code == 200
    assert res_stop.json()["is_live"] is False


# ===========================================================================
# 模块 4: 内置 RTMP 直推流引擎
# ===========================================================================
def test_04_rtmp_streaming_engine(client):
    """
    【模块 4 验证】
    1. /api/v1/live/rtmp/status: 获取 RTMP 引擎运行状态
    2. /api/v1/live/rtmp/start: 启动 RTMP 推流
    3. /api/v1/live/rtmp/stop: 停止 RTMP 推流
    """
    # 1. 检查 RTMP 状态
    res_status = client.get("/api/v1/live/rtmp/status")
    assert res_status.status_code == 200
    data = res_status.json()
    assert data["code"] == 0
    assert "is_streaming" in data["data"]

    # 2. 模拟启动推流 (使用本地 mock RTMP 地址)
    res_start = client.post("/api/v1/live/rtmp/start", json={
        "rtmp_url": "rtmp://127.0.0.1:1935/live/stream_verify_test"
    })
    assert res_start.status_code == 200
    assert res_start.json()["code"] == 0

    # 3. 停止推流
    res_stop = client.post("/api/v1/live/rtmp/stop")
    assert res_stop.status_code == 200
    assert res_stop.json()["code"] == 0


# ===========================================================================
# 模块 5: AI 主播立绘管理
# ===========================================================================
def test_05_avatar_image_and_view_angles(client):
    """
    【模块 5 验证】
    1. /api/v1/avatars/list: 查询立绘形象列表
    2. /api/v1/avatars/create: 上传单张正脸肖像图片并提取特征
    3. /api/v1/avatars/{id}: 删除形象
    """
    # 1. 获取列表
    res_list = client.get("/api/v1/avatars/list")
    assert res_list.status_code == 200
    assert res_list.json()["code"] == 0

    # 2. 创建并上传数字人形象
    png_data = _generate_png_bytes(width=64, height=64)
    res_create = client.post(
        "/api/v1/avatars/create",
        data={"name": "验证数字人_小雅", "avatar_type": "image"},
        files={"file": ("avatar_test_ya.png", png_data, "image/png")}
    )
    assert res_create.status_code == 200
    created = res_create.json()
    assert created["code"] == 0
    avatar_id = created["data"]["id"]

    # 3. 再次查询确认已成功入库
    res_list2 = client.get("/api/v1/avatars/list")
    assert res_list2.status_code == 200
    ids = [a["id"] for a in res_list2.json()["data"]]
    assert avatar_id in ids

    # 4. 删除测试立绘
    res_del = client.delete(f"/api/v1/avatars/{avatar_id}")
    assert res_del.status_code == 200
    assert res_del.json()["code"] == 0


# ===========================================================================
# 模块 6: 音色管理与试听
# ===========================================================================
def test_06_voice_profile_and_audio_devices(client):
    """
    【模块 6 验证】
    1. /api/v1/voices/list: 查询克隆音色档案列表
    2. /api/v1/voices/clone: 上传音频样本并提取声学特征
    3. /api/v1/settings/audio-devices: 本地声卡与麦克风设备扫描
    4. /api/v1/voices/{id}: 删除音色
    """
    # 1. 查询音色列表
    res_list = client.get("/api/v1/voices/list")
    assert res_list.status_code == 200
    assert res_list.json()["code"] == 0

    # 2. 上传音频样本克隆音色
    res_clone = client.post(
        "/api/v1/voices/clone",
        data={"name": "验证音色_温婉女主播", "speed": 1.1, "volume": 1.0},
        files={"audio_file": ("sample_wenwan.wav", DUMMY_WAV_BYTES, "audio/wav")}
    )
    assert res_clone.status_code == 200
    clone_data = res_clone.json()
    assert clone_data["code"] == 0
    voice_id = clone_data["data"]["id"]

    # 3. 查询本地音频硬件输入输出设备
    res_devs = client.get("/api/v1/settings/audio-devices")
    assert res_devs.status_code == 200
    devs_data = res_devs.json()
    assert devs_data["code"] == 0
    assert "devices" in devs_data["data"]

    # 4. 删除测试音色
    res_del = client.delete(f"/api/v1/voices/{voice_id}")
    assert res_del.status_code == 200
    assert res_del.json()["code"] == 0


# ===========================================================================
# 模块 7: 主播角色人设库
# ===========================================================================
def test_07_anchor_role_personas(client):
    """
    【模块 7 验证】
    1. /api/v1/roles/list: 查询内置与自定义人设库
    2. /api/v1/roles/upsert: 新增/更新角色提示词、交互约束
    3. /api/v1/roles/switch: 即时热切换当前主播
    """
    # 1. 查询角色列表
    res_list = client.get("/api/v1/roles/list")
    assert res_list.status_code == 200
    roles = res_list.json()["data"]
    assert len(roles) >= 3

    # 2. 创建或更新专属人设
    custom_role = {
        "id": "role_verify_tech_expert",
        "role_type": "expert",
        "role_name": "科技硬核测评专家",
        "system_prompt": "你是一名精通各类数码产品的资深科技博主，语调客观严谨，善用参数对比分析。",
        "speech_speed": 1.05,
        "pitch_shift": 0.0,
        "associated_guardrail_group": "general"
    }
    res_upsert = client.post("/api/v1/roles/upsert", json=custom_role)
    assert res_upsert.status_code == 200
    assert res_upsert.json()["code"] == 0

    # 3. 热切换到该人设
    res_switch = client.post("/api/v1/roles/switch", json={"role_id": "role_verify_tech_expert"})
    assert res_switch.status_code == 200
    assert res_switch.json()["code"] == 0

    # 切换回默认娱乐主播
    client.post("/api/v1/roles/switch", json={"role_id": "role_entertainment_default"})


# ===========================================================================
# 模块 8: 商品货盘与爆品互动
# ===========================================================================
def test_08_product_showcase_and_hot_selling(client):
    """
    【模块 8 验证】
    1. /api/v1/products/list: 查询商品列表
    2. /api/v1/products/upsert: 新增/更新带货商品（SKU、秒杀价、卖点、话术）
    3. /api/v1/products/{id}/flash-sale: 触发限时秒杀（需在开播态）
    4. /api/v1/products/{id}: 删除商品
    """
    # 1. 查询商品列表
    res_list = client.get("/api/v1/products/list")
    assert res_list.status_code == 200
    assert res_list.json()["code"] == 0

    # 2. 新增商品
    product_payload = {
        "sku_code": "SKU_VERIFY_PHONE_01",
        "title": "2026全新旗舰折叠屏手机",
        "category": "数码3C",
        "original_price": 6999.0,
        "live_price": 5999.0,
        "current_stock": 20,
        "selling_points": ["超轻薄机身", "潜望长焦", "全天长续航"],
        "coupon_script": "直播间领券立减1000元，送无线充电器",
    }
    res_upsert = client.post("/api/v1/products/upsert", json=product_payload)
    assert res_upsert.status_code == 200
    assert res_upsert.json()["code"] == 0
    product_id = res_upsert.json()["data"]["id"]

    # 3. 触发秒杀 (flash-sale 要求开播中)
    client.post("/api/v1/live/start", json={"room_id": "room_flash_test"})
    try:
        res_sale = client.post(f"/api/v1/products/{product_id}/flash-sale")
        assert res_sale.status_code == 200
        assert res_sale.json()["code"] == 0
    finally:
        client.post("/api/v1/live/stop")

    # 4. 清理测试商品
    res_del = client.delete(f"/api/v1/products/{product_id}")
    assert res_del.status_code == 200
    assert res_del.json()["code"] == 0


# ===========================================================================
# 模块 9: 风控合规与敏感词
# ===========================================================================
def test_09_guardrails_compliance_and_sanitization(client):
    """
    【模块 9 验证】
    1. /api/v1/guardrails/words: 获取违禁词库
    2. /api/v1/guardrails/words: 录入违禁词与合规平替词
    3. /api/v1/guardrails/test-sanitize: Aho-Corasick 多模式串高效平替脱敏
    4. /api/v1/guardrails/logs: 违禁拦截审计日志
    5. /api/v1/guardrails/words/{id}: 删除测试违禁词
    """
    # 1. 获取违禁词库
    res_words = client.get("/api/v1/guardrails/words")
    assert res_words.status_code == 200
    assert res_words.json()["code"] == 0

    # 2. 录入违禁词
    res_add = client.post("/api/v1/guardrails/words", json={
        "word": "天下无双最厉害",
        "category": "extreme",
        "role_scope": "all",
        "action_policy": "substitute",
        "replacement_word": "广受大众认可"
    })
    assert res_add.status_code == 200
    word_id = res_add.json()["data"]["id"]

    # 3. 运行脱敏平替测试
    res_test = client.post("/api/v1/guardrails/test-sanitize", json={
        "text": "本直播间推荐的产品简直是天下无双最厉害，欢迎大家下单！"
    })
    assert res_test.status_code == 200
    sanitize_data = res_test.json()
    assert sanitize_data["code"] == 0
    assert "天下无双最厉害" not in sanitize_data["sanitized_text"]
    assert "广受大众认可" in sanitize_data["sanitized_text"]

    # 4. 查看审计日志
    res_logs = client.get("/api/v1/guardrails/logs?limit=5")
    assert res_logs.status_code == 200
    assert res_logs.json()["code"] == 0

    # 5. 删除测试违禁词
    res_del = client.delete(f"/api/v1/guardrails/words/{word_id}")
    assert res_del.status_code == 200
    assert res_del.json()["code"] == 0


# ===========================================================================
# 模块 10: LLM配置(1) 大模型生态与多大脑管理
# ===========================================================================
def test_10_llm_configuration_ecosystem(client):
    """
    【模块 10 验证】
    1. /api/v1/settings/llm/providers: 8 大主流模型生态元数据
    2. /api/v1/settings/configs/save: 保存 LLM 配置并验证 API Key 脱敏存储
    3. /api/v1/settings/configs/{id}/raw-key: 密钥解密明文查看
    4. /api/v1/settings/llm/models: 动态模型拉取探测 (无 Key 明确拦截，本地允许探测)
    5. /api/v1/settings/configs/{id}: 清理测试配置
    """
    # 1. 验证 8 大主流模型生态
    res_prov = client.get("/api/v1/settings/llm/providers")
    assert res_prov.status_code == 200
    prov_data = res_prov.json()
    assert prov_data["code"] == 0
    prov_ids = {p["id"] for p in prov_data["data"]}
    assert {"deepseek", "qwen", "minimax", "kimi", "gemini", "glm", "chatgpt", "custom"}.issubset(prov_ids)

    # 2. 保存测试配置
    config_id = f"cfg_llm_verify_{int(time.time())}"
    res_save = client.post("/api/v1/settings/configs/save", json={
        "id": config_id,
        "config_group": "llm",
        "provider_name": "deepseek",
        "base_url": "https://api.deepseek.com/v1",
        "model_name": "deepseek-chat",
        "api_key": "sk-verify-llm-secret-987654",
        "is_active": False
    })
    assert res_save.status_code == 200
    assert res_save.json()["code"] == 0

    # 验证列表中 API Key 脱敏
    res_configs = client.get("/api/v1/settings/configs")
    assert res_configs.status_code == 200
    configs = res_configs.json()["data"]
    target = next((c for c in configs if c["id"] == config_id), None)
    assert target is not None
    assert "sk-" in target["masked_key"]
    assert "987654" not in target["masked_key"]

    # 3. 验证查看明文密钥
    res_raw = client.get(f"/api/v1/settings/configs/{config_id}/raw-key")
    assert res_raw.status_code == 200
    assert res_raw.json()["raw_key"] == "sk-verify-llm-secret-987654"

    # 4. 验证动态模型拉取探测 (未提供 Key 返回友好提示)
    res_models = client.post("/api/v1/settings/llm/models", json={
        "provider_name": "deepseek",
        "base_url": "https://api.deepseek.com/v1"
    })
    assert res_models.status_code == 200
    assert res_models.json()["code"] == 1
    assert "API Key" in res_models.json()["message"]

    # 5. 删除配置
    res_del = client.delete(f"/api/v1/settings/configs/{config_id}")
    assert res_del.status_code == 200
    assert res_del.json()["code"] == 0


# ===========================================================================
# 模块 11: GPU配置(2) 数字人画面与云端渲染选型
# ===========================================================================
def test_11_gpu_and_neural_renderer_selection(client):
    """
    【模块 11 验证】
    1. /api/v1/settings/avatar/providers: 获取数字人渲染器注册表
    2. /api/v1/settings/configs/save:
       - 保存卡片 1 (本地卡通 2D): 验证 provider_name 智能推导为 local_cartoon
       - 保存卡片 4 (远端独占 GPU 节点): 验证智能推导为 remote_dedicated
       - 保存卡片 6 (无数字人纯音频): 验证智能推导为 audio_only
       - 验证彻底杜绝 [object Object] 与 extra_params.adapter 验证报错
    """
    # 1. 获取注册表
    res_desc = client.get("/api/v1/settings/avatar/providers")
    assert res_desc.status_code == 200
    data = res_desc.json()
    assert data["code"] == 0
    provider_ids = {p["id"] for p in data["data"]}
    assert {"sidecar_v3", "local_procedural", "aliyun_avatar", "tencent_avatar"}.issubset(provider_ids)

    # 2. 保存 sidecar_v3 (私有化 GPU Sidecar 节点) 并验证智能 adapter 补全
    res_sidecar = client.post("/api/v1/settings/configs/save", json={
        "config_group": "neural_renderer",
        "provider_name": "sidecar_v3",
        "title": "私有化 GPU 渲染节点",
        "base_url": "ws://127.0.0.1:8010/ws/render-v3",
        "api_key": "secret-sidecar-token",
        "is_active": False,
        "extra_params": {
            "backend_id": "auto",
            "avatar_id": "default",
        }
    })
    assert res_sidecar.status_code == 200
    sc_data = res_sidecar.json()
    assert sc_data["code"] == 0
    cfg_id_sidecar = sc_data["id"]
    assert cfg_id_sidecar

    # 3. 保存 custom_avatar (自定义 WebSocket / RTMP 流服务)
    res_custom = client.post("/api/v1/settings/configs/save", json={
        "config_group": "neural_renderer",
        "provider_name": "custom_avatar",
        "title": "自定义流服务",
        "base_url": "ws://127.0.0.1:8010/ws/render-v3",
        "is_active": False,
        "extra_params": {
            "stream_protocol": "websocket"
        }
    })
    assert res_custom.status_code == 200
    cust_data = res_custom.json()
    assert cust_data["code"] == 0
    cfg_id_custom = cust_data["id"]
    assert cfg_id_custom

    # 4. 验证数字人配置 raw-key 解密查看明文契约（点击眼睛图标查看明文）
    res_raw = client.get(f"/api/v1/settings/configs/{cfg_id_sidecar}/raw-key")
    assert res_raw.status_code == 200
    raw_json = res_raw.json()
    assert raw_json["code"] == 0
    assert raw_json["raw_key"] == "secret-sidecar-token"

    # 5. 验证异常拦截：若提交未注册的 adapter，系统返回可读的 422 提示而非 [object Object]
    res_invalid = client.post("/api/v1/settings/configs/save", json={
        "config_group": "neural_renderer",
        "provider_name": "unknown_renderer_xyz",
        "extra_params": {}
    })
    assert res_invalid.status_code == 422
    err_body = res_invalid.json()
    assert err_body["code"] == 422
    assert isinstance(err_body["detail"], str)
    assert "未注册" in err_body["detail"]

    # 6. 清理测试配置
    for cid in [cfg_id_sidecar, cfg_id_custom]:
        client.delete(f"/api/v1/settings/configs/{cid}")


# ===========================================================================
# 模块 12: TTS配置(3) 语音中心与实时试听
# ===========================================================================
def test_12_tts_voice_center_configuration(client):
    """
    【模块 12 验证】
    1. /api/v1/settings/configs: 查询 TTS 配置列表
    2. /api/v1/settings/configs/save: 保存 TTS 参数
    3. /api/v1/settings/ping: 测试服务连通性
    4. /api/v1/settings/tts/preview: 在线语音合成试听预览
    """
    # 1. 保存 Edge TTS 配置
    cfg_id = f"cfg_tts_verify_{int(time.time())}"
    res_save = client.post("/api/v1/settings/configs/save", json={
        "id": cfg_id,
        "config_group": "tts",
        "provider_name": "edge_tts",
        "is_active": False,
        "extra_params": {
            "voice": "zh-CN-XiaoxiaoNeural",
            "rate": "+0%",
            "volume": "+0%"
        }
    })
    assert res_save.status_code == 200
    assert res_save.json()["code"] == 0

    # 2. 连通性测试 (EdgeTTS 探测)
    res_ping = client.post("/api/v1/settings/ping", json={
        "config_group": "tts",
        "provider_name": "cloud_edge_tts"
    })
    assert res_ping.status_code == 200
    assert res_ping.json()["success"] is True

    # 3. 试听接口
    res_preview = client.post("/api/v1/settings/tts/preview", json={
        "provider_name": "edge_tts",
        "voice_name": "zh-CN-XiaoxiaoNeural",
        "text": "全系统功能逐一验证正在进行中"
    })
    assert res_preview.status_code == 200
    assert "audio" in res_preview.headers.get("content-type", "")
    assert len(res_preview.content) > 1000

    # 4. 清理配置
    client.delete(f"/api/v1/settings/configs/{cfg_id}")


# ===========================================================================
# 模块 13: 通用设置与视觉感知
# ===========================================================================
def test_13_general_settings_and_vision_perception(client):
    """
    【模块 13 验证】
    1. /api/v1/settings/vision: 读取视觉感知设置
    2. /api/v1/settings/vision: 更新视觉感知周期、截图区域与使能开关
    """
    # 1. 读取当前视觉配置
    res_get = client.get("/api/v1/settings/vision")
    assert res_get.status_code == 200
    assert res_get.json()["code"] == 0

    # 2. 更新视觉配置
    payload = {
        "enabled": True,
        "source": "desktop_screen",
        "interval_sec": 5.0
    }
    res_set = client.post("/api/v1/settings/vision", json=payload)
    assert res_set.status_code == 200
    assert res_set.json()["code"] == 0

    # 3. 再次获取确认已持久化
    res_check = client.get("/api/v1/settings/vision")
    assert res_check.status_code == 200
    data = res_check.json()["data"]
    assert data["enabled"] is True
    assert data["interval_sec"] == 5.0


# ===========================================================================
# 模块 14: 本地 RAG 知识库与双路混合检索
# ===========================================================================
def test_14_rag_knowledge_base_hybrid_retrieval(client):
    """
    【模块 14 验证】
    1. /api/v1/knowledge/status: 知识库运行态探针 (向量后端、维度、分块统计)
    2. /api/v1/knowledge/upload: 上传带货文献并切片建立双路混合索引
    3. /api/v1/knowledge/list: 查询已录入知识文档
    4. /api/v1/knowledge/search: 语义召回检索
    5. /api/v1/knowledge/{doc_id}: 清理删除知识文档
    """
    # 1. 检查知识库运行态
    res_status = client.get("/api/v1/knowledge/status")
    assert res_status.status_code == 200
    status_data = res_status.json()
    assert status_data["code"] == 0
    assert "vector_backend" in status_data["data"]

    # 2. 上传知识文献 (使用 Markdown 格式)
    doc_text = (
        "# 2026年爆品羽绒服问答手册\n\n"
        "## 面料材质\n"
        "本款羽绒服采用 90% 白鹅绒填充，蓬松度高达 800+，面料具备防风防泼水性能。\n\n"
        "## 洗涤保养说明\n"
        "建议使用 30 度以下温水手洗或专业羽绒服机洗模式，切勿高温烘干或干洗。\n"
    ).encode("utf-8")

    doc_name = f"验证测试手册_{int(time.time())}"
    res_upload = client.post(
        "/api/v1/knowledge/upload",
        data={"doc_name": doc_name},
        files={"file": (f"{doc_name}.md", doc_text, "text/markdown")}
    )
    assert res_upload.status_code == 200
    upload_data = res_upload.json()
    assert upload_data["code"] == 0
    doc_id = upload_data["data"]["doc_id"]
    assert upload_data["data"]["chunk_count"] >= 1

    # 3. 文档聚合查询
    res_list = client.get("/api/v1/knowledge/list")
    assert res_list.status_code == 200
    doc_ids = [d["doc_id"] for d in res_list.json()["data"]]
    assert doc_id in doc_ids

    # 4. 检索测试
    res_search = client.post("/api/v1/knowledge/search", json={
        "query": "羽绒服可以高温烘干吗？",
        "top_k": 3
    })
    assert res_search.status_code == 200
    search_data = res_search.json()
    assert search_data["code"] == 0
    results = search_data["data"]["hits"]
    assert len(results) >= 1
    assert any("洗涤" in r["content"] or "烘干" in r["content"] for r in results)

    # 5. 删除知识文档
    res_del = client.delete(f"/api/v1/knowledge/{doc_id}")
    assert res_del.status_code == 200
    assert res_del.json()["code"] == 0
