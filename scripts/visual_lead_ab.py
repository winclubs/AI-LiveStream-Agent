# -*- coding: utf-8 -*-
"""
Visual Lead (视听预动量) A/B 对比工具 (LIPSYNC_OPTIMIZATION_PLAN.md v3.1 §3.3)

背景
----
当前 `LIPSYNC_VISUAL_LEAD_SAMPLES` 默认 640（= 40ms = 25fps 下的 1 帧）。
这个值是**按「1 帧 = 40ms」推出来的，不是实测最优**。方案 §3.3 明确要求
「先做 A/B 再固化，不要凭直觉写死」。

本工具为同一段音频渲染多档偏移的唇形序列，供人工盲测选优。

用法
----
    # 用已有资产 + 指定音频，产出 4 档对比
    python scripts/visual_lead_ab.py \
        --anchor data/avatar_assets/task_e0b46ab5d3 \
        --audio  data/xxx/tts.wav \
        --outdir out/visual_lead_ab

    # 也可指定单档
    python scripts/visual_lead_ab.py --anchor ... --audio ... --offsets 0,320,640,960

产出
----
    out/visual_lead_ab/lead_0000/frames/*.jpg   逐帧唇形
    out/visual_lead_ab/lead_0640/frames/*.jpg
    out/visual_lead_ab/manifest.json            偏移量与实测信息

盲测建议：把各档目录重命名为 A/B/C/D（打乱顺序），让评审不知道哪档对应哪个
偏移量，避免锚定效应。选定后再把该值写入部署环境变量。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import wave
from pathlib import Path
from typing import List, Optional

import numpy as np

REPO_ROOT = Path(__file__).resolve().parents[1]

DEFAULT_OFFSETS = [0, 320, 640, 960]   # 0 / 20ms / 40ms / 60ms
DEFAULT_FPS = 25
MEL_CONTEXT = 3200                        # ±200ms，与生产一致
SR = 16000


def read_audio_16k(path: Path) -> np.ndarray:
    """读取音频并重采样到 16kHz 单声道 float32。

    优先 soundfile/librosa；都没有时对 wav 用标准库兜底（支持 PCM16）。
    """
    raw = None
    sr = None
    for loader in ("soundfile", "librosa"):
        try:
            if loader == "soundfile":
                import soundfile as sf

                raw, sr = sf.read(str(path), dtype="float32", always_2d=True)
                raw = raw.mean(axis=1)
            else:
                import librosa

                raw, sr = librosa.load(str(path), sr=SR, mono=True)
            return np.asarray(raw, dtype=np.float32)
        except ImportError:
            continue
        except Exception as e:
            print(f"警告: {loader} 读取失败({e})，尝试下一种")

    try:
        with wave.open(str(path), "rb") as w:
            sr = w.getframerate()
            n = w.getnframes()
            channels = w.getnchannels()
            width = w.getsampwidth()
            data = w.readframes(n)
        if width == 2:
            arr = np.frombuffer(data, dtype=np.int16).astype(np.float32) / 32768.0
        elif width == 4:
            arr = np.frombuffer(data, dtype=np.int32).astype(np.float32) / 2147483648.0
        elif width == 1:
            arr = (np.frombuffer(data, dtype=np.uint8).astype(np.float32) - 128.0) / 128.0
        else:
            raise ValueError(f"不支持的采样宽度: {width * 8} bit")
        if channels > 1:
            arr = arr.reshape(-1, channels).mean(axis=1)
        raw = arr
    except Exception as e:
        raise RuntimeError(f"无法读取音频 {path}: {e}")

    # 线性重采样到 16k
    if sr and sr != SR:
        t_out = np.linspace(0, 1, int(len(raw) * SR / sr), endpoint=False)
        t_in = np.linspace(0, 1, len(raw), endpoint=False)
        raw = np.interp(t_out, t_in, raw).astype(np.float32)
    return np.asarray(raw, dtype=np.float32)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Visual Lead A/B comparison renderer")
    p.add_argument("--anchor", required=True, help="主播资产目录（含 face_imgs/ 与 coords.pkl）")
    p.add_argument("--audio", required=True, help="测试音频（会自动重采样到 16kHz）")
    p.add_argument("--outdir", default=str(REPO_ROOT / "out" / "visual_lead_ab"))
    p.add_argument("--offsets", default=",".join(str(o) for o in DEFAULT_OFFSETS),
                   help="待对比的偏移量（采样点），逗号分隔")
    p.add_argument("--fps", type=int, default=DEFAULT_FPS)
    p.add_argument("--max-frames", type=int, default=250, help="最多渲染帧数")
    p.add_argument("--model-key", default="onnx_lipsync")
    p.add_argument("--blinded", action="store_true",
                   help="用打乱的匿名目录名产出，避免盲测时的锚定效应")
    return p


def main() -> int:
    args = build_parser().parse_args()
    offsets = [int(x) for x in args.offsets.split(",") if x.strip()]
    if not offsets:
        print("错误：--offsets 为空")
        return 1

    try:
        import cv2
    except ImportError:
        print("错误：需要 opencv (pip install opencv-python)")
        return 1

    anchor = Path(args.anchor)
    audio_path = Path(args.audio)
    if not anchor.exists():
        print(f"错误：资产目录不存在 {anchor}")
        return 1
    if not audio_path.exists():
        print(f"错误：音频不存在 {audio_path}")
        return 1

    sys.path.insert(0, str(REPO_ROOT))
    from server.core.avatar.neural_lip_renderer import NeuralLipRenderer

    print(f"加载资产: {anchor}")
    print(f"加载音频: {audio_path}")
    pcm = read_audio_16k(audio_path)
    print(f"音频时长: {len(pcm) / SR:.2f}s  ({len(pcm)} 采样 @16kHz)")

    renderer = NeuralLipRenderer(model_key=args.model_key)
    if not renderer.is_ready:
        print(f"错误：神经渲染引擎未就绪 (model_key={args.model_key})")
        return 1
    if not renderer.load_anchor_assets(anchor):
        print(f"错误：资产加载失败 {anchor}")
        return 1
    print(f"引擎就绪: provider={renderer.session.get_providers()[0]}  "
          f"时序={'T=' + str(renderer.temporal_frames) if renderer.supports_temporal else '单帧'}")

    n_frames = min(args.max_frames, max(1, int(len(pcm) / SR * args.fps)))
    pts_step = SR // args.fps
    out_root = Path(args.outdir)
    out_root.mkdir(parents=True, exist_ok=True)

    # 匿名化：打乱顺序并映射为 A/B/C/D
    rng = np.random.default_rng(20261002)
    order = list(range(len(offsets)))
    rng.shuffle(order)
    labels = [chr(ord("A") + i) for i in range(len(offsets))]
    alias = {order[i]: labels[i] for i in range(len(offsets))}

    manifest = {
        "audio": str(audio_path),
        "anchor": str(anchor),
        "fps": args.fps,
        "frames": n_frames,
        "model_key": args.model_key,
        "provider": renderer.session.get_providers()[0],
        "temporal": renderer.temporal_frames if renderer.supports_temporal else 1,
        "variants": [],
    }

    for idx in range(len(offsets)):
        lead = offsets[idx]
        label = alias[idx] if args.blinded else f"lead_{lead:04d}"
        vdir = out_root / label
        fdir = vdir / "frames"
        fdir.mkdir(parents=True, exist_ok=True)

        renderer.reset_temporal_state()
        written = 0
        for seq in range(n_frames):
            center = seq * pts_step + pts_step // 2 + lead
            win_start, win_end = center - MEL_CONTEXT, center + MEL_CONTEXT
            pad_l = max(0, -win_start)
            act_s, act_e = max(0, win_start), min(len(pcm), win_end)
            if act_s < act_e:
                chunk = pcm[act_s:act_e]
                if pad_l > 0 or win_end > len(pcm):
                    pad_r = max(0, win_end - len(pcm))
                    chunk = np.pad(chunk, (pad_l, pad_r), mode="constant")
            else:
                chunk = np.zeros(2 * MEL_CONTEXT, dtype=np.float32)

            idx_img = seq % max(1, len(renderer.face_imgs))
            base = renderer.full_imgs[idx_img] if renderer.full_imgs else None
            if base is None:
                h, w = 960, 720
                base = np.zeros((h, w, 3), dtype=np.uint8)
            out = renderer.render_lip_frame(base, seq, chunk)
            if out is None:
                out = base
            cv2.imwrite(str(fdir / f"{seq:05d}.jpg"), out)
            written += 1

        ms = lead / SR * 1000.0
        manifest["variants"].append({
            "label": label,
            "offset_samples": lead,
            "offset_ms": round(ms, 1),
            "frames": written,
        })
        print(f"  [{label}] offset={lead:4d} ({ms:4.1f}ms) -> {written} 帧  {fdir}")

    manifest["variants"].sort(key=lambda v: v["offset_samples"])
    (out_root / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"\n产出目录: {out_root}")
    print("\n盲测建议：")
    print(f"  1. 逐个打开 {out_root}/<标签>/frames/ 对比同一帧号的唇形")
    print("  2. 重点看发 /b/ /p/ /m/ 时唇缝是否收紧、爆破是否更跟手")
    print("  3. 选定后把对应偏移量写入部署环境变量：")
    print("     LIPSYNC_VISUAL_LEAD_SAMPLES=<偏移量>")
    print("\n注意：偏移过大会产生「抢拍」感；若 640 档并非最优，不要默认沿用。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
