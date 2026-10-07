# -*- coding: utf-8 -*-
"""
时序 ONNX 导出脚本 (LIPSYNC_OPTIMIZATION_PLAN.md v3.1 §5.2) 回归测试

守护一个已经发生过的严重缺陷：初版 `export_wav2lip_temporal_onnx.py` 构造
`TemporalWav2LipWrapper()` 时未传 core_model，且从未 import Wav2Lip 模型类、
从未把已加载的 generator_dict 灌进任何模块。forward 于是落到占位分支
`return face_input[:, :3, :, :]`，导出的是一个**恒等映射** ONNX——
能加载、能推理、能出图，但完全不做唇形重绘。这类缺陷不会抛异常，
只能靠结构断言 + 导出后数值校验拦住。

运行时部分需要 torch（离线工具环境），未安装时自动跳过；
静态结构断言在无 torch 环境下同样生效。
"""
import ast
import importlib.util
import inspect
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "export_wav2lip_temporal_onnx.py"


def _load_module():
    """加载导出脚本模块（纯源码解析，不需要 torch）"""
    spec = importlib.util.spec_from_file_location("wav2lip_temporal_export", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def export_mod():
    return _load_module()


@pytest.fixture(scope="module")
def torch_nn():
    torch = pytest.importorskip(
        "torch", reason="导出脚本为离线工具，运行时断言需 torch 环境")
    return torch, torch.nn


# ---------------------------------------------------------------- 静态结构断言

def test_script_parses_and_exposes_api():
    """脚本本身必须是合法 Python 且导出入口齐备"""
    tree = ast.parse(SCRIPT.read_text(encoding="utf-8"))
    funcs = {n.name for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}
    for required in ("export_temporal_onnx", "_make_wrapper",
                     "_load_generator_state_dict", "_build_wav2lip_model"):
        assert required in funcs, f"导出脚本缺少 {required}"


def test_wrapper_rejects_none_core(export_mod, torch_nn):
    """核心回归：core_model=None 必须直接报错，绝不能静默导出恒等映射"""
    torch, nn = torch_nn
    with pytest.raises(ValueError, match="core_model"):
        export_mod._make_wrapper(torch, nn, None)


def test_wrapper_has_no_passthrough_fallback():
    """wrapper 的 forward 中不得存在「返回输入切片」这类占位分支

    用 AST 判定「是否真的存在该return 语句」，而不是全文匹配——
    否则本文件里用于说明该缺陷的注释/文档字符串会造成假阳性。
    """
    tree = ast.parse(SCRIPT.read_text(encoding="utf-8"))
    offenders = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Return) or node.value is None:
            continue
        seg = ast.unparse(node.value)
        # 直接把 face 输入切片吐回来的恒等映射特征
        if "face_input[:, :3" in seg or "face_seq[:, :3" in seg:
            offenders.append(seg)
    assert not offenders, (
        f"导出脚本仍存在恒等映射占位 return: {offenders}"
    )


def test_generator_state_dict_is_actually_consumed():
    """剥离判别器后得到的 generator_dict 必须真的被灌进模型"""
    src = SCRIPT.read_text(encoding="utf-8")
    tree = ast.parse(src)
    build = next(n for n in ast.walk(tree)
                 if isinstance(n, ast.FunctionDef) and n.name == "_build_wav2lip_model")
    body = ast.unparse(build)
    assert "generator_dict" in body, "_build_wav2lip_model 未使用 generator_dict"
    assert "load_state_dict" in body, (
        "generator_dict 未通过 load_state_dict 灌入模型 —— 权重会被丢弃"
    )


def test_weight_loading_is_strict():
    """权重不完整必须报错，禁止静默随机初始化（否则导出的是噪声唇形）"""
    src = SCRIPT.read_text(encoding="utf-8")
    tree = ast.parse(src)
    build = next(n for n in ast.walk(tree)
                 if isinstance(n, ast.FunctionDef) and n.name == "_build_wav2lip_model")
    body = ast.unparse(build)
    assert "if missing" in body, "缺少对 load_state_dict 缺失张量的检查"
    assert "RuntimeError" in body, "权重不完整时应抛 RuntimeError 而非继续"


def test_dynamic_axes_do_not_open_mel_time_axis():
    """mel 时间维必须固定 16 步；只有 face_seq 的时间轴是动态的"""
    mod = _load_module()
    src = inspect.getsource(mod.export_temporal_onnx)
    assert '"face_seq": {0: "batch", 4: "time_steps"}' in src
    assert '"mel_window": {0: "batch"}' in src
    assert '"mel_window": {0: "batch", 3: "time_steps"}' not in src, (
        "mel 时间维不可设为动态：Wav2Lip 音频分支按固定 16 步设计"
    )


# ---------------------------------------------------------------- 运行时行为

def test_forward_invokes_core(export_mod, torch_nn):
    """有 core 时 forward 必须调用 core，而不是把输入原样返回"""
    torch, nn = torch_nn

    class Core(torch.nn.Module):
        def forward(self, x, mel):
            return x[:, :3] * 0 + 7.0

    wrapper = export_mod._make_wrapper(torch, nn, Core())
    out = wrapper(torch.zeros(1, 6, 256, 256, 5), torch.zeros(1, 1, 80, 16))
    assert abs(float(out.mean()) - 7.0) < 1e-6, (
        "forward 未调用 core，输出为恒等映射"
    )


def test_forward_permutes_bhw6_to_b6hw(export_mod, torch_nn):
    """[B,H,W,6] 输入必须被置换为 [B,6,H,W] 再送入 Wav2Lip"""
    torch, nn = torch_nn

    class ShapeProbe(torch.nn.Module):
        def forward(self, x, mel):
            return torch.tensor([float(x.shape[1])])

    wrapper = export_mod._make_wrapper(torch, nn, ShapeProbe())
    out = wrapper(torch.zeros(1, 256, 256, 6), torch.zeros(1, 1, 80, 16))
    assert float(out[0]) == 6.0, "通道维未正确置换到 axis=1"


def test_missing_weights_fails_cleanly(export_mod, tmp_path):
    """权重文件缺失时必须返回 False 并给出可操作提示，不得抛栈"""
    ok = export_mod.export_temporal_onnx(
        str(tmp_path / "nope.pth"), str(tmp_path / "out.onnx"))
    assert ok is False
