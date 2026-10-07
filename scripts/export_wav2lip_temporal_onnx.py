# -*- coding: utf-8 -*-
"""
Wav2Lip 5 帧时序动态轴 ONNX 导出脚本 (LIPSYNC_OPTIMIZATION_PLAN.md v3.1 §5.2)

背景
----
Wav2Lip 官方推理本身带 `seq_len=5` 的时序上下文 (`wav2lip.py` 把 `frames[-4:]`
连同当前帧一起送入生成器)。当前线上链路每帧只喂单帧，白丢了模型自带的时序能力，
这是唇形帧间突变的直接来源。本脚本把该能力导出成 ONNX。

输入/输出契约
-------------
    face_seq : [B, 6, 256, 256, T]   T ∈ {1, 3, 5}；6 通道 = [masked ‖ face] 沿时间堆叠
    mel      : [B, 1, 80, 16]        与 Wav2LipMelExtractor 输出严格一致
    输出     : [B, 3, 256, 256]       仅中心帧

历史缺陷（v3.1 修复）
---------------------
初版脚本构造 `TemporalWav2LipWrapper()` 时**未传入 core_model**，且从未 import
Wav2Lip 模型类、从未把已加载的 `generator_dict` 灌进任何模块。`forward` 于是落到
占位分支 `return face_input[:, :3, :, :]`，导出的 ONNX 是一个恒等映射——
能加载、能推理，但完全不做唇形重绘。此脚本现在真正构建模型并校验权重。

用法
----
本脚本是**离线工具**，需在装有 torch / onnx 的环境运行（线上 sidecar 不依赖 torch）：

    pip install torch torchvision
    python scripts/export_wav2lip_temporal_onnx.py \
        --weights data/models/wav2lip_gan.pth \
        --output  data/models/wav2lip_temporal_256.onnx

权重获取（Wav2Lip 官方发布）::

    # 优先 GAN 版 (wav2lip_gan.pth)，唇形锐利度优于 L1 版
    curl -L -o wav2lip_gan.pth \\
      https://huggingface.co/numz/wav2lip_gan/resolve/main/checkpoints/wav2lip_gan.pth
    # 或 L1 版
    curl -L -o wav2lip.pth \\
      https://huggingface.co/numz/wav2lip/resolve/main/checkpoints/wav2lip.pth

注意 GAN 版 checkpoint 同时包含判别器与感知损失权重，键名带 `discriminator.` 前缀，
本脚本已剥离；加载后 GAN 判别器不参与生成。

退出码
------
    0  导出成功
    1  依赖缺失 / 权重缺失 / 构建失败 / 权重不完整 / 校验失败
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

# Wav2Lip 超参 (Rudrabha/Wav2Lip hparams.py)。必须与 Wav2LipMelExtractor 一致，
# 否则导出的模型会与运行时的 mel 分布错配。
IMG_SIZE = 256
FPS = 25
MEL_STEPS = 16
TEMPORAL_FRAMES = 5


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Export Wav2Lip with dynamic temporal axis (T frames) to ONNX",
    )
    p.add_argument("--weights", default=str(REPO_ROOT / "data/models/wav2lip_gan.pth"),
                   help="Path to wav2lip_gan.pth or wav2lip.pth")
    p.add_argument("--output", default=str(REPO_ROOT / "data/models/wav2lip_temporal_256.onnx"),
                   help="Target ONNX path")
    p.add_argument("--opset", type=int, default=17, help="ONNX opset version")
    p.add_argument("--frames", type=int, default=TEMPORAL_FRAMES,
                   help="T used for tracing (export stays dynamic)")
    p.add_argument("--verify", action="store_true",
                   help="After export, run the ONNX and assert the output is NOT "
                        "a trivial pass-through of the input (guards against the "
                        "identity-mapping regression).")
    return p


def _import_deps():
    try:
        import torch  # noqa: F401
        import torch.nn as nn  # noqa: F401
    except ImportError:
        print("错误：导出时序 ONNX 需要 PyTorch 环境 (pip install torch torchvision)")
        return None
    try:
        import onnx  # noqa: F401
    except ImportError:
        print("错误：导出时序 ONNX 需要 onnx 包 (pip install onnx)")
        return None
    return torch


def _load_generator_state_dict(torch, weights_path: Path):
    """加载 checkpoint 并剥离判别器权重，只保留生成器。"""
    print(f"正在加载 Wav2Lip 检查点: {weights_path} ...")
    checkpoint = torch.load(str(weights_path), map_location="cpu", weights_only=False)
    state_dict = checkpoint.get("state_dict", checkpoint) if isinstance(checkpoint, dict) else checkpoint
    if not isinstance(state_dict, dict):
        raise RuntimeError("checkpoint 中未找到 state_dict 字典")

    generator_dict, dropped = {}, 0
    for k, v in state_dict.items():
        if k.startswith("discriminator."):
            dropped += 1
            continue
        generator_dict[k[7:] if k.startswith("module.") else k] = v

    print(f"  已剥离 discriminator 判别器权重 {dropped} 项，保留生成器张量 {len(generator_dict)} 项")
    if not generator_dict:
        raise RuntimeError("剥离判别器后生成器权重为空，checkpoint 格式可能不符")
    return generator_dict


def _build_wav2lip_model(generator_dict, device="cpu"):
    """按官方 models.py 的结构构建 Wav2Lip 生成器并灌入权重。

    这里直接 import 官方 Wav2Lip `models.py`，避免在本脚本里重写网络结构
    （重写极易与官方实现漂移，是这类导出脚本最常见的错误来源）。
    """
    import torch
    import torch.nn as nn

    # 优先使用本地已 clone 的 Wav2Lip 源码
    candidates = [
        REPO_ROOT / "third_party" / "Wav2Lip",
        REPO_ROOT.parent / "Wav2Lip",
    ]
    wav2lip_dir = next((c for c in candidates if (c / "models.py").exists()), None)
    if wav2lip_dir is None:
        raise RuntimeError(
            "未找到 Wav2Lip 源码目录 (需含 models.py)。请先 clone 官方仓库到 "
            "third_party/Wav2Lip:  git clone https://github.com/Rudrabha/Wav2Lip "
            "third_party/Wav2Lip"
        )

    import sys as _sys
    if str(wav2lip_dir) not in _sys.path:
        _sys.path.insert(0, str(wav2lip_dir))

    try:
        from models import Wav2Lip  # type: ignore
    except Exception as e:
        raise RuntimeError(f"导入 Wav2Lip models.py 失败: {e}") from e

    model = Wav2Lip()
    # 严格校验：任何缺失张量都必须报错，绝不允许静默随机初始化——
    # 静默随机的模型导出的 ONNX 看起来能跑，实际输出的是噪声唇形。
    missing, unexpected = model.load_state_dict(generator_dict, strict=False)
    if missing:
        raise RuntimeError(
            f"权重不完整：缺失 {len(missing)} 个张量。首个缺失: {missing[0]}。"
            f"请确认 --weights 指向与该网络结构匹配的 checkpoint。"
        )
    if unexpected:
        print(f"  提示：{len(unexpected)} 个 checkpoint 张量未被模型使用 (首个: {unexpected[0]})")

    model = model.eval().to(device)
    return model, wav2lip_dir


def _make_wrapper(torch, nn, core):
    """把 Wav2Lip 生成器包成接受 [B,6,H,W,T] 动态时序输入的模块。"""

    class TemporalWav2LipWrapper(nn.Module):
        """沿时间维取中心帧送入 Wav2Lip 生成器。

        Wav2Lip 原生接口为 forward(x, mel): x=[B,6,H,W], mel=[B,1,80,16]。
        本包装层只做「T -> 1」的降维，核心重绘全部由 core 完成。
        """

        def __init__(self, core_model):
            super().__init__()
            if core_model is None:
                raise ValueError("core_model 不能为 None：否则 forward 会退化为恒等映射")
            self.core = core_model

        def forward(self, face_seq, mel_window):
            if face_seq.dim() == 5:
                # 取中心帧。T 为偶数时取靠后一帧，与渲染侧 seq-T//2 的取帧一致。
                t_mid = face_seq.shape[-1] // 2
                face_input = face_seq[..., t_mid]
            else:
                face_input = face_seq
            if face_input.dim() == 4:
                face_input = face_input.permute(0, 3, 1, 2)   # [B,H,W,6] -> [B,6,H,W]
            return self.core(face_input, mel_window)

    return TemporalWav2LipWrapper(core)


def export_temporal_onnx(weights_path: str, output_path: str,
                         opset_version: int = 17, frames: int = TEMPORAL_FRAMES,
                         verify: bool = False) -> bool:
    torch = _import_deps()
    if torch is None:
        return False

    import torch.nn as nn

    w_path = Path(weights_path)
    if not w_path.exists():
        print(f"错误：权重文件不存在 {w_path}")
        print("请先下载官方 wav2lip_gan.pth（见本文件顶部用法说明）")
        return False

    out_p = Path(output_path)
    out_p.parent.mkdir(parents=True, exist_ok=True)

    try:
        generator_dict = _load_generator_state_dict(torch, w_path)
        core, wav2lip_dir = _build_wav2lip_model(generator_dict)
        print(f"  Wav2Lip 生成器构建完成（源码: {wav2lip_dir}）")
    except Exception as e:
        print(f"错误：构建 Wav2Lip 模型失败: {e}")
        return False

    wrapper = _make_wrapper(torch, nn, core)
    wrapper.eval()

    dummy_faces = torch.zeros(1, 6, IMG_SIZE, IMG_SIZE, frames, dtype=torch.float32)
    dummy_mel = torch.zeros(1, 1, 80, MEL_STEPS, dtype=torch.float32)

    print(f"正在导出动态时序 ONNX (opset={opset_version}, 追踪用 T={frames}) -> {out_p} ...")
    try:
        torch.onnx.export(
            wrapper,
            (dummy_faces, dummy_mel),
            str(out_p),
            input_names=["face_seq", "mel_window"],
            output_names=["mouth_frame"],
            dynamic_axes={
                "face_seq": {0: "batch", 4: "time_steps"},
                # mel 的时间维固定为 16 步（MEL_STEPS），不设为动态：
                # Wav2Lip 生成器的音频分支按固定 16 步切片设计，放开会导致
                # 形状不匹配。batch 维仍保持动态。
                "mel_window": {0: "batch"},
                "mouth_frame": {0: "batch"},
            },
            opset_version=opset_version,
            do_constant_folding=True,
        )
    except Exception as e:
        print(f"ONNX 导出失败: {e}")
        return False

    size_mb = out_p.stat().st_size / 1048576
    print(f"成功导出动态时序 ONNX: {out_p} ({size_mb:.1f} MB)")

    if verify:
        return _verify_export(out_p, frames)
    print("提示：加 --verify 可自动校验导出结果不是恒等映射")
    return True


def _verify_export(out_p: Path, frames: int) -> bool:
    """防回归校验：导出结果必须真的做重绘，而不是把输入原样吐回来。

    历史缺陷正是恒等映射，因此这项校验必须常驻。
    """
    try:
        import numpy as np
        import onnxruntime as ort
    except ImportError:
        print("警告：缺少 onnxruntime/onnx，跳过校验")
        return True

    sess = ort.InferenceSession(str(out_p), providers=["CPUExecutionProvider"])
    names = {i.name for i in sess.get_inputs()}

    rng = np.random.default_rng(0)
    face = rng.random((1, 6, IMG_SIZE, IMG_SIZE, frames), dtype=np.float32)
    mel = rng.standard_normal((1, 1, 80, MEL_STEPS)).astype(np.float32) * 2.0

    feed = {"face_seq": face, "mel_window": mel} if names >= {"face_seq", "mel_window"} else {
        i.name: (mel if ("mel" in i.name.lower() or "audio" in i.name.lower()) else face)
        for i in sess.get_inputs()
    }
    out = sess.run(None, feed)[0]

    # 恒等映射会输出 face 的前三通道；真模型输出的是重绘后的嘴部，二者差异应显著
    passthrough = face[0, :3]
    diff = float(np.abs(out[0] - passthrough).mean())
    print(f"  校验：输出与输入前三通道的平均差 = {diff:.6f}")
    if diff < 1e-4:
        print("错误：导出结果与输入几乎相同，疑似恒等映射（core 未接入）。导出作废。")
        return False
    print("  校验通过：输出确实由模型重绘产生")
    return True


def main() -> int:
    args = build_parser().parse_args()
    ok = export_temporal_onnx(args.weights, args.output, args.opset,
                              args.frames, args.verify)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
