# -*- coding: utf-8 -*-
"""
存量数字人资产健康审计 (LIPSYNC_OPTIMIZATION_PLAN.md v3.1 §3.1)

v3.1 的 P0 资产守卫只在**新切片**时生效；存量资产是修复前生成的，
仍带着「静默回退」时期的损坏帧。此脚本对全部存量资产复检并给出处置建议。

用法：
    python scripts/audit_avatar_assets.py                # 报告
    python scripts/audit_avatar_assets.py --json out.json  # 机器可读

判定标准与 task_manager 的守卫保持一致：
    人脸检出率 < 90%  或  最长连续丢失 > 5 帧  →  判定为「须重切」
本脚本**只读不删**，处置动作需人工确认。
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import pickle
import sys

import cv2
import numpy as np

MIN_DETECT_RATIO = 0.90
MAX_CARRY_FRAMES = 5
SAMPLE_EVERY = 1          # 1 = 全量复检；调大可加速但会降低精度


def audit_asset(asset_dir: str, cascade, sample_every: int = 1):
    coords_path = os.path.join(asset_dir, "coords.pkl")
    if not os.path.exists(coords_path):
        return None
    try:
        coords = pickle.load(open(coords_path, "rb"))
    except Exception as e:
        return {
            "asset": os.path.basename(asset_dir),
            "verdict": "CORRUPT",
            "reason": f"coords.pkl 无法解析: {e}",
            "frames": 0,
        }

    faces = sorted(
        glob.glob(os.path.join(asset_dir, "face_imgs", "*.jpg")),
        key=lambda s: int(os.path.splitext(os.path.basename(s))[0])
        if os.path.splitext(os.path.basename(s))[0].isdigit() else 0,
    )
    if not faces:
        return {
            "asset": os.path.basename(asset_dir),
            "verdict": "EMPTY",
            "reason": "face_imgs 为空",
            "frames": len(coords),
        }

    detected = 0
    tried = 0
    max_run = 0
    run = 0
    for i, f in enumerate(faces):
        if sample_every > 1 and i % sample_every:
            continue
        im = cv2.imread(f)
        tried += 1
        ok = False
        if im is not None:
            g = cv2.equalizeHist(cv2.cvtColor(im, cv2.COLOR_BGR2GRAY))
            ok = len(cascade.detectMultiScale(
                g, scaleFactor=1.1, minNeighbors=4, minSize=(60, 60))) > 0
        if ok:
            detected += 1
            run = 0
        else:
            run += 1
            max_run = max(max_run, run)

    ratio = detected / max(1, tried)
    reasons = []
    if ratio < MIN_DETECT_RATIO:
        reasons.append(f"检出率 {ratio:.1%} < {MIN_DETECT_RATIO:.0%}")
    if max_run > MAX_CARRY_FRAMES:
        reasons.append(f"最长连续丢失 {max_run} 帧 > {MAX_CARRY_FRAMES}")
    if not faces[: min(len(faces), 20)]:
        reasons.append("前 20 帧切片缺失")

    meta_path = os.path.join(asset_dir, "meta.json")
    meta = {}
    if os.path.exists(meta_path):
        try:
            meta = json.load(open(meta_path, encoding="utf-8"))
        except Exception:
            pass

    return {
        "asset": os.path.basename(asset_dir),
        "anchor_id": meta.get("anchor_id"),
        "name": meta.get("name"),
        "frames": len(faces),
        "coords": len(coords),
        "detect_ratio": round(ratio, 4),
        "max_consecutive_miss": max_run,
        "meta_detection_rate": meta.get("detection_rate"),
        "has_render_boxes": os.path.exists(os.path.join(asset_dir, "render_boxes.pkl")),
        "verdict": "RETAKE" if reasons else "OK",
        "reason": "; ".join(reasons),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="Audit stored avatar assets for P0 guard compliance")
    ap.add_argument("--root", default=os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "data", "avatar_assets"))
    ap.add_argument("--sample-every", type=int, default=SAMPLE_EVERY,
                    help="每隔 N 帧抽检一次（1=全量）")
    ap.add_argument("--json", help="同时写出 JSON 报告")
    args = ap.parse_args()

    cascade = cv2.CascadeClassifier(
        cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
    if cascade.empty():
        print("错误：Haar 分类器加载失败")
        return 1

    dirs = sorted(glob.glob(os.path.join(args.root, "task_*")))
    dirs = [d for d in dirs if os.path.isdir(d)]
    if not dirs:
        print(f"未在 {args.root} 找到任何资产")
        return 0

    results = []
    for d in dirs:
        r = audit_asset(d, cascade, args.sample_every)
        if r:
            results.append(r)

    reta = [r for r in results if r["verdict"] == "RETAKE"]
    broken = [r for r in results if r["verdict"] in ("CORRUPT", "EMPTY")]
    ok = [r for r in results if r["verdict"] == "OK"]

    print("=" * 92)
    print(f"存量资产审计：共 {len(results)} 个  |  OK {len(ok)}  "
          f"须重切 {len(reta)}  损坏/空 {len(broken)}")
    print("=" * 92)
    print(f"{'资产':22s} {'帧数':>6s} {'检出率':>8s} {'最长丢失':>9s}  判定")
    print("-" * 92)
    for r in sorted(results, key=lambda x: (x["verdict"] != "RETAKE",
                                            x.get("detect_ratio") or 0)):
        ratio = f"{r.get('detect_ratio', 0):.1%}" if "detect_ratio" in r else "-"
        run = r.get("max_consecutive_miss", "-")
        print(f"{r['asset']:22s} {r.get('frames', 0):6d} {ratio:>8s} "
              f"{str(run):>9s}  {r['verdict']}"
              + (f"  ({r['reason']})" if r.get("reason") else ""))

    if reta:
        print()
        print("!! 以下资产须重切（人脸切片中存在无人脸/无脸框污染）：")
        for r in reta:
            print(f"   - {r['asset']}  {r.get('frames', 0)} 帧  {r['reason']}")
        print()
        print("处置建议：")
        print("  1. 这些资产是 v3.1 资产守卫上线【之前】生成的，守卫不会追溯校验它们。")
        print("  2. 需在设置页重新上传原始素材触发重切；新守卫会在检出率不足时直接拒绝。")
        print("  3. 在重切完成前，这些资产不应投入直播使用。")

    if args.json:
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump({
                "min_detect_ratio": MIN_DETECT_RATIO,
                "max_carry_frames": MAX_CARRY_FRAMES,
                "total": len(results),
                "ok": len(ok), "retake": len(reta), "broken": len(broken),
                "assets": results,
            }, f, ensure_ascii=False, indent=2)
        print(f"\nJSON 报告已写入 {args.json}")

    return 1 if (reta or broken) else 0


if __name__ == "__main__":
    sys.exit(main())
