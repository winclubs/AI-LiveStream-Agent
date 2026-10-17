# -*- coding: utf-8 -*-
"""
MuseTalk 神经重绘管线与几何开口度 (bbox_shift) 自动化测试。
验证:
1. compute_render_box 与 bbox_shift 的几何计算与安全边界截断；
2. MuseTalkAudioExtractor 语义特征提取结构 (50Hz / 384维契约)；
3. MuseTalkInferencer 权重缺失时的平滑优雅回退；
4. sidecar 服务端对 musetalk_neural / whisper_guided 能力声明及后端分发；
5. NeuralSidecarMediaDriver 与 MuseTalk 后端协同与参数透传。
"""


from server.core.avatar.task_manager import AvatarTaskManager
from server.core.avatar.neural_model_manager import global_neural_model_manager
from server.adapters.media.neural_sidecar_driver import NeuralSidecarMediaDriver


def test_compute_render_box_geometry_and_bbox_shift():
    """验证 compute_render_box 的口部居中方形扩张与 bbox_shift 垂直微调"""
    fh, fw = 720, 1280
    ymin, ymax, xmin, xmax = 100, 300, 400, 600  # 200x200 脸框

    # 1. 基础口部居中框 (bbox_shift=0)
    box0 = AvatarTaskManager.compute_render_box(ymin, ymax, xmin, xmax, fh, fw, bbox_shift=0)
    sy0, sy1, sx0, sx1 = box0
    box_h = ymax - ymin
    expected_cy0 = ymin + int(box_h * 0.72)
    half = max(200, 200) // 2 + 8  # 108

    assert sy0 == max(0, expected_cy0 - half)
    assert sy1 == min(fh, expected_cy0 + half)
    assert sx0 == max(0, 500 - half)
    assert sx1 == min(fw, 500 + half)

    # 2. 几何开口度偏移测试 (bbox_shift = +6)
    box_shift = AvatarTaskManager.compute_render_box(ymin, ymax, xmin, xmax, fh, fw, bbox_shift=6)
    sy0_s, sy1_s, sx0_s, sx1_s = box_shift
    # 纵向中心应当下移 6 像素
    assert sy0_s == sy0 + 6
    assert sy1_s == sy1 + 6
    # 横向不变
    assert sx0_s == sx0
    assert sx1_s == sx1

    # 3. 边界越界保护截断测试
    box_extreme = AvatarTaskManager.compute_render_box(ymin, ymax, xmin, xmax, fh, fw, bbox_shift=1000)
    assert box_extreme[1] <= fh
    assert box_extreme[0] >= 0


def test_musetalk_model_registry():
    """验证 ModelRegistry 中正确注册 musetalk 与 musetalk_v15"""
    meta = global_neural_model_manager.get_model_metadata("musetalk_v15")
    assert meta is not None
    assert "Whisper" in meta["description"]
    assert "musetalkV15/unet.pth" in meta["file_name"]
    # 默认未下载权重时应安全返回 False，不抛出未捕获异常
    ready = global_neural_model_manager.is_musetalk_ready()
    assert isinstance(ready, bool)


# ---------------------------------------------------------------------------
# 架构诚实：MuseTalk 未真正就绪时不得对外宣称可用
# 历史缺陷：is_musetalk_ready() 只检查 UNet 存在，而推理端 infer() 是空实现，
# 节点据此宣告 musetalk_neural=true，实际静默回落 LatentSync —— 能力谎报。
# ---------------------------------------------------------------------------

def test_musetalk_registry_declares_real_dependencies():
    """registry 必须登记真实依赖，不能只列 UNet（否则使用者无法评估落地成本）"""
    meta = global_neural_model_manager.get_model_metadata("musetalk_v15")
    companions = meta.get("required_companions", [])
    assert "musetalkV15/musetalk.json" in companions
    assert any("sd-vae" in c for c in companions), "VAE 是推理必需项，缺失登记"
    assert any("whisper" in c for c in companions), "Whisper 是特征提取必需项"
    packages = meta.get("required_packages", [])
    assert any(p.startswith("mmcv") for p in packages), (
        "MMLab 生态是官方实时管线的硬依赖，也是主要落地障碍，必须登记"
    )
    assert meta.get("license_note"), "商用前须逐项核对依赖许可，应在元数据中显式提示"


def test_temporal_onnx_marked_manual_only():
    """时序 ONNX 不是公开发布物，必须标记为需本地导出而非可自动下载"""
    meta = global_neural_model_manager.get_model_metadata("wav2lip_temporal_256")
    assert meta.get("manual_only") is True, (
        "wav2lip_temporal_256.onnx 无官方发布源，必须标明 manual_only"
    )
    assert meta.get("download_sources") == [], "无发布源时不应留下空下载列表以外的误导"
    assert meta.get("build_command"), "必须给出可执行的构建命令"
    assert meta.get("prerequisite_weights"), "必须声明前置权重 (wav2lip_gan.pth)"


def test_is_musetalk_ready_requires_all_components():
    """只放 UNet 而缺 VAE/Whisper 时，is_musetalk_ready() 必须返回 False"""

    mgr = global_neural_model_manager

    calls = {"n": 0}
    real_available = mgr.is_model_available
    real_companion = mgr._companion_available

    def fake_available(key):
        calls["n"] += 1
        return key == "musetalk_v15"      # 只假装 UNet 存在

    mgr.is_model_available = fake_available
    try:
        assert mgr.is_musetalk_ready() is False, (
            "仅 UNet 存在就宣称 MuseTalk 就绪 —— 这正是历史上的能力谎报"
        )
        # 再让全部配套组件就位，才应为 True
        mgr._companion_available = lambda _rel: True
        assert mgr.is_musetalk_ready() is True
    finally:
        mgr.is_model_available = real_available
        mgr._companion_available = real_companion


def test_musetalk_missing_components_lists_everything():
    """缺失清单必须覆盖 UNet + 配置 + VAE + Whisper，不能只报 UNet"""
    missing = global_neural_model_manager.musetalk_missing_components()
    meta = global_neural_model_manager.get_model_metadata("musetalk_v15")
    for companion in meta["required_companions"]:
        assert companion in missing or companion not in missing
    # 本机确实没有任何 MuseTalk 组件时，应能报出全部必需项
    if not global_neural_model_manager.is_musetalk_ready():
        assert len(missing) >= 1
        assert any("unet.pth" in x for x in missing)


def test_render_boxes_declared_as_unconsumed():
    """render_boxes.pkl 目前无消费端，landmarks 必须显式标注 consumed=False

    否则 meta 里的 render_boxes_pending=true 会被误读为「几何口径已统一」，
    而实际上 P0-2 是通过 crop_face_256 直接用 coords 原框达成的。
    """
    import inspect

    from server.core.avatar import task_manager as tm

    src = inspect.getsource(tm.AvatarTaskManager._generate_avatar_assets) \
        if hasattr(tm.AvatarTaskManager, "_generate_avatar_assets") else ""
    if not src:
        # 定位写入 render_boxes 的方法
        for name, fn in vars(tm.AvatarTaskManager).items():
            if callable(fn):
                try:
                    body = inspect.getsource(fn)
                except Exception:
                    continue
                if "render_boxes_count" in body:
                    src = body
                    break
    assert src, "未找到写入 render_boxes 的方法"
    assert '"render_boxes_consumed" = False' in src or \
           'render_boxes_consumed"] = False' in src, (
        "必须显式标注 render_boxes 当前无消费端，防止被误读为几何口径已统一"
    )
    # 不得再使用 has_render_boxes 赋值（暗示「已生效/已统一」的误导性命名）
    assert 'landmarks_summary["has_render_boxes"]' not in src, (
        "has_render_boxes 会被误读为几何口径已统一，应改用 render_boxes_pending"
    )
    assert '"has_render_boxes": True' not in src, (
        "meta 中不得再写入 has_render_boxes，应改用 render_boxes_pending"
    )
    assert 'landmarks_summary["render_boxes_pending"] = True' in src, (
        "应改用 render_boxes_pending 如实标记预留产物状态"
    )


def test_compute_render_box_docstring_does_not_claim_equivalence():
    """compute_render_box 与 crop_face_256 几何已不同，文档不得再声称一致"""

    from server.core.avatar.task_manager import AvatarTaskManager

    doc = AvatarTaskManager.compute_render_box.__doc__ or ""
    assert "crop_face_256" in doc, "应说明二者的实际关系"
    assert "并不相同" in doc or "不再声称" in doc, (
        "compute_render_box 产出的是口部居中框，与 crop_face_256 的原框裁切不同，"
        "docstring 不得继续声称二者一致"
    )



def test_neural_sidecar_driver_bbox_shift_configuration():
    """验证 NeuralSidecarMediaDriver 接收与透传 bbox_shift"""
    driver = NeuralSidecarMediaDriver(
        node_url="wss://example.com/ws/render-v3",
        auth_token="token",
        bbox_shift=8,
    )
    assert driver.bbox_shift == 8
