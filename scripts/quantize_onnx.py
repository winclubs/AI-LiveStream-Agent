# -*- coding: utf-8 -*-
"""
LatentSync / ONNX 神经唇形量化脚本 (INT8 / UINT8)，用于本地 CPU 实时化。

背景
----
实测（本机 12 核 CPU，无 CUDA）::

    单帧 ONNX 推理   176.4 ms   (FP32, 204.5 MB)
    25fps 帧预算      40.0 ms   -> 超 4.4 倍

量化是唯一能在不动模型结构的前提下压低延迟的手段。本脚本产出量化模型并
**实测对比延迟与画质差异**，由数据决定是否值得启用，而不是盲信"量化=快"。

权衡（务必实测后再决定）
------------------------
INT8 通常带来 2~4x 加速，但代价是：
  * 唇部细节与牙齿边缘出现轻微块状伪影；
  * 动态范围收窄，闭唇音（b/p/m）的唇缝可能变得不如 FP32 干净。

**因此本脚本默认只产出候选模型 + 报告，不自动替换生产权重。**

用法
----
    pip install onnx onnxruntime
    python scripts/quantize_onnx.py \
        --input  data/models/onnx_lipsync.onnx \
        --output data/models/onnx_lipsync_int8.onnx \
        --compare

    # 量化时序模型（T=5 导出版）
    python scripts/quantize_onnx.py \
        --input  data/models/wav2lip_temporal_256.onnx \
        --output data/models/wav2lip_temporal_256_int8.onnx \
        --compare

启用方式：把量化后的路径填到 NeuralLipRenderer(custom_onnx_path=...)，或
在设置页把模型 key 指向量化文件。**务必先看 --compare 的画质差异再决定。**
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path
from typing import Optional

REPO_ROOT = Path(__file__).resolve().parents[1]

# 对比用的合成输入（与生产链路一致的形状）
PROBE_FRAMES = 10


def _import_quant_deps():
    try:
        import onnx  # noqa: F401
        from onnxruntime.quantization import (
            QuantType,
            quantize_dynamic,
        )
    except ImportError as e:
        print(f"错误：缺少量化依赖 ({e})。请先安装: pip install onnx onnxruntime")
        return None
    return onnx, quantize_dynamic, QuantType


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Quantize LatentSync / Lipsync ONNX to INT8/UINT8")
    p.add_argument("--input", default=str(REPO_ROOT / "data/models/onnx_lipsync.onnx"))
    p.add_argument("--output", default=str(REPO_ROOT / "data/models/onnx_lipsync_int8.onnx"))
    p.add_argument("--type", choices=["int8", "uint8"], default="int8",
                   help="动态量化权重类型。int8 体积/速度通常更优；"
                        "uint8 在部分 CPU 上精度更稳")
    p.add_argument("--per-channel", action="store_true",
                   help="逐通道量化，精度通常优于 per-tensor")
    p.add_argument("--reduce-range", action="store_true",
                   help="降低 7-bit 权重精度换取更小体积（精度下降更明显）")
    p.add_argument("--compare", action="store_true",
                   help="量化后实测延迟与画质差异（强烈建议开启）")
    p.add_argument("--threads", type=int, default=0,
                   help="推理线程数，0 = CPU 核数的一半")
    return p


def _probe_inputs(session, names):
    """按模型实际输入名构造探测输入（单帧 / 时序自适应）。"""
    import numpy as np

    feed = {}
    for inp in session.get_inputs():
        shape = [d if isinstance(d, int) else None for d in inp.shape]
        name = inp.name.lower()
        is_audio = ("audio" in name or "mel" in name)
        if is_audio:
            feed[inp.name] = np.zeros((1, 1, 80, 16), dtype=np.float32)
        elif len(shape) == 5:
            t = shape[4] if isinstance(shape[4], int) and shape[4] > 1 else 5
            feed[inp.name] = np.zeros((1, 6, 256, 256, t), dtype=np.float32)
        else:
            feed[inp.name] = np.zeros((1, 6, 256, 256), dtype=np.float32)
    return feed


def benchmark(model_path: Path, threads: int):
    """实测单帧推理延迟与输出统计"""
    import numpy as np
    import onnxruntime as ort

    opts = ort.SessionOptions()
    opts.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
    opts.intra_op_num_threads = threads
    sess = ort.InferenceSession(str(model_path), sess_options=opts,
                                providers=["CPUExecutionProvider"])
    feed = _probe_inputs(sess, sess.get_inputs())
    out_names = [o.name for o in sess.get_outputs()]

    sess.run(out_names, feed)                      # 预热
    samples = []
    for _ in range(PROBE_FRAMES):
        t0 = time.perf_counter()
        out = sess.run(out_names, feed)
        samples.append((time.perf_counter() - t0) * 1000.0)

    samples.sort()
    median_ms = samples[len(samples) // 2]
    arr = np.asarray(out[0], dtype=np.float32)
    return {
        "median_ms": median_ms,
        "min_ms": samples[0],
        "out_shape": tuple(arr.shape),
        "out_mean": float(arr.mean()),
        "out_std": float(arr.std()),
        "out_min": float(arr.min()),
        "out_max": float(arr.max()),
        "raw": arr,
    }


def compare_quality(fp_stats, int_stats) -> dict:
    """画质差异：全图 MAE + 嘴部区域 MAE（嘴部才是关键）"""
    import numpy as np

    a, b = fp_stats["raw"], int_stats["raw"]
    if a.shape != b.shape:
        return {"comparable": False, "note": f"形状不同 {a.shape} vs {b.shape}"}
    diff = np.abs(a - b)
    # 嘴部区域：256x256 中约 y 128~230, x 64~192
    mouth = diff[:, 128:230, 64:192] if diff.ndim == 4 else diff
    full = diff
    return {
        "comparable": True,
        "mae_full": float(full.mean()),
        "mae_mouth": float(np.asarray(mouth).mean()),
        "max_abs": float(diff.max()),
        "p99": float(np.percentile(diff, 99)),
    }


def main() -> int:
    args = build_parser().parse_args()
    deps = _import_quant_deps()
    if deps is None:
        return 1
    _onnx, quantize_dynamic, QuantType = deps

    src = Path(args.input)
    dst = Path(args.output)
    if not src.exists():
        print(f"错误：输入模型不存在 {src}")
        return 1
    if not src.suffix == ".onnx":
        print(f"错误：输入必须是 .onnx 文件（收到 {src.name}）")
        print("提示：wav2lip.pth 是 PyTorch 权重，无法直接量化；"
              "需先用 scripts/export_wav2lip_temporal_onnx.py 导出 ONNX")
        return 1

    threads = args.threads or max(1, (os.cpu_count() or 4) // 2)

    print(f"输入: {src}  ({src.stat().st_size / 1048576:.1f} MB)")
    print(f"线程: {threads}")

    if args.compare:
        print("\n[1/3] 基线 (FP32) 实测...")
        fp = benchmark(src, threads)
        print(f"      单帧中位延迟 {fp['median_ms']:.1f} ms  "
              f"(25fps 预算 40.0 ms, 可达 {1000 / fp['median_ms']:.1f} fps)")

    print("\n[2/3] 量化中...")
    dst.parent.mkdir(parents=True, exist_ok=True)
    qt = QuantType.QInt8 if args.type == "int8" else QuantType.QUInt8
    try:
        quantize_dynamic(
            model_input=str(src),
            model_output=str(dst),
            weight_type=qt,
            per_channel=args.per_channel,
            reduce_range=args.reduce_range,
        )
    except Exception as e:
        print(f"错误：量化失败: {e}")
        return 1
    print(f"      产出 {dst}  ({dst.stat().st_size / 1048576:.1f} MB)")

    if not args.compare:
        print("\n提示：加 --compare 可实测延迟与画质差异后再决定是否启用")
        return 0

    print("\n[3/3] 量化后实测...")
    try:
        iq = benchmark(dst, threads)
    except Exception as e:
        print(f"错误：量化模型无法推理: {e}")
        return 1

    speedup = fp["median_ms"] / iq["median_ms"] if iq["median_ms"] > 0 else 0.0
    print(f"      单帧中位延迟 {iq['median_ms']:.1f} ms  "
          f"(25fps 预算 40.0 ms, 可达 {1000 / iq['median_ms']:.1f} fps)")
    print(f"      加速比: {speedup:.2f}x")

    print("\n" + "=" * 72)
    print("对比结论")
    print("=" * 72)
    print(f"  体积      {src.stat().st_size / 1048576:7.1f} MB -> "
          f"{dst.stat().st_size / 1048576:7.1f} MB")
    print(f"  延迟      {fp['median_ms']:7.1f} ms -> {iq['median_ms']:7.1f} ms"
          f"   ({speedup:.2f}x)")
    print(f"  25fps     {'达标' if fp['median_ms'] <= 40 else '不达标':>7s}"
          f"    -> {'达标' if iq['median_ms'] <= 40 else '不达标'}")

    q = compare_quality(fp, iq)
    if q.get("comparable"):
        print(f"  画质 MAE  全图 {q['mae_full']:.4f} / 嘴部 {q['mae_mouth']:.4f}"
              f" / 最大 {q['max_abs']:.3f} / P99 {q['p99']:.3f}")
        verdict = ""
        if q["mae_mouth"] > 0.05:
            verdict = ("\n  ⚠️ 嘴部差异偏大，量化伪影可能损害唇形细节。"
                       "建议改用 --type uint8 或放弃量化。")
        elif speedup < 1.5:
            verdict = "\n  ⚠️ 加速不足 1.5x，不值得为此牺牲画质。建议放弃量化。"
        else:
            verdict = "\n  ✅ 加速与画质均可接受，可启用。"
        print(verdict)
    else:
        print(f"  画质对比: {q.get('note')}")

    print("\n启用方式：NeuralLipRenderer(custom_onnx_path=...) 或在设置页指向量化模型。")
    print("建议先在试听预览中对比实际唇形效果，再决定是否用于正式直播。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
