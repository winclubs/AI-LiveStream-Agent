#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
动作切片一键补齐脚本 (Action Clip Gap Filler)

问题背景：
  系统默认 5 大带货动作 (0 待机 / 1 欢迎 / 2 点赞 / 3 促单指引 / 4 致谢) 中，
  若运营者只上传了部分动作切片视频，未上传的动作 (常见为 3 促单指引购物车)
  会在 get_status().missing_action_codes 中如实暴露，触发时只能保持待机画面。

本脚本的作用：
  对缺失的动作切片，以“已有切片 + 程序化指引箭头/倒计时贴片”合成一段合规
  的占位动作视频 (1080x1920 竖屏 25fps)，并经标准 AvatarTaskManager 管线
  抽帧入库，使缺失动作立即获得可用的画面资产，直播促单节奏不再断档。

  合成视频明确属于程序化占位画面，建议运营后续用真人实拍切片替换：
    python scripts/upload_action_clip.py  (或在控制台【数字人】页面上传)

用法：
  python scripts/generate_action_clip.py --action 3
  python scripts/generate_action_clip.py --all-missing
"""

import argparse
import asyncio
import os
import sys
from pathlib import Path

# 适配终端编码
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def _compose_placeholder_clip(
    src_clip_path: Path,
    out_path: Path,
    action_code: int,
    action_name: str,
    fps: int = 25,
    duration_sec: float = 4.0,
) -> bool:
    """以已有切片为底，叠加促单指引箭头与动作名生成占位动作视频"""
    import cv2
    import numpy as np

    cap = cv2.VideoCapture(str(src_clip_path))
    if not cap.isOpened():
        print(f"[错误] 无法读取底片视频: {src_clip_path}")
        return False

    src_frames = []
    while True:
        ret, frame = cap.read()
        if not ret or frame is None:
            break
        src_frames.append(frame)
    cap.release()

    if not src_frames:
        print(f"[错误] 底片视频无有效帧: {src_clip_path}")
        return False

    h, w = src_frames[0].shape[:2]
    total_frames = max(1, int(fps * duration_sec))

    fourcc = cv2.VideoWriter_fourcc(*"mp4v")
    writer = cv2.VideoWriter(str(out_path), fourcc, fps, (w, h))
    if not writer.isOpened():
        print(f"[错误] 无法创建输出视频: {out_path}")
        return False

    n_src = len(src_frames)

    # 动作名标注 (促单指引购物车 / 挥手欢迎 等)
    label = f"#{action_code} {action_name}"
    font = cv2.FONT_HERSHEY_SIMPLEX
    # 中文场景用兜底英文标签，避免无中文字体时乱码
    fallback_label = f"Action #{action_code}: {action_name}"

    for i in range(total_frames):
        base = src_frames[i % n_src].copy()
        t = i / float(fps)

        # 1. 底部渐变遮罩条 (提升文字可读性)
        overlay = base.copy()
        bar_h = int(h * 0.16)
        cv2.rectangle(overlay, (0, h - bar_h), (w, h), (0, 0, 0), -1)
        cv2.addWeighted(overlay, 0.55, base, 0.45, 0, base)

        # 2. 动作名文字 (居中)
        (tw, th), _ = cv2.getTextSize(fallback_label, font, 1.1, 2)
        tx = max(4, (w - tw) // 2)
        ty = h - bar_h // 2 + th // 2
        cv2.putText(base, fallback_label, (tx, ty), font, 1.1, (255, 255, 255), 2, cv2.LINE_AA)

        # 3. 促单指引动作 (code=3)：动态下箭头指引购物车
        if action_code == 3:
            # 箭头在右下购物车区域上下浮动
            bob = int(abs(np.sin(t * 2.5)) * 26)
            ax = int(w * 0.72)
            ay = int(h * 0.72) + bob
            arrow_len = int(h * 0.10)
            cv2.arrowedLine(
                base,
                (ax, ay - arrow_len),
                (ax, ay + arrow_len // 2),
                (52, 211, 153),
                10,
                tipLength=0.35,
            )
            # 倒计时进度条
            progress = min(1.0, t / duration_sec)
            bar_w = int(w * 0.5)
            bx0 = (w - bar_w) // 2
            by = int(h * 0.88)
            cv2.rectangle(base, (bx0, by), (bx0 + bar_w, by + 10), (60, 60, 60), -1)
            cv2.rectangle(
                base,
                (bx0, by),
                (bx0 + int(bar_w * (1.0 - progress)), by + 10),
                (52, 211, 153),
                -1,
            )

        # 4. 其他动作：轻柔扫光强化动作感知
        else:
            sweep_x = int((np.sin(t * 1.8) * 0.5 + 0.5) * w)
            cv2.line(base, (sweep_x, 0), (sweep_x, h), (255, 255, 255), 2, cv2.LINE_AA)

        writer.write(base)

    writer.release()
    print(f"[完成] 已合成占位动作视频: {out_path.name} ({total_frames} 帧, {w}x{h}@{fps}fps)")
    return True


async def _fill_action(action_code: int) -> bool:
    from server.core.avatar.action_state_machine import get_action_state_machine
    from server.core.avatar.task_manager import get_avatar_task_manager
    from server.database.db import AsyncSessionLocal
    from server.database.models import AvatarAction, Anchor
    from sqlalchemy import select

    asm = get_action_state_machine()
    await asm.load_configs_from_db()

    clip = asm.clips.get(action_code)
    if clip is None:
        print(f"[错误] 动作 {action_code} 未在数据库注册")
        return False
    if clip.total_frames > 0:
        print(f"[跳过] 动作 {action_code} ({clip.action_name}) 已有 {clip.total_frames} 帧切片，无需补齐")
        return True

    # 选取一个已就绪的同尺寸切片作为底片 (优先待机切片 0，其次任意已加载切片)
    src_clip = None
    for cand_code in (0, 1, 2, 4):
        cand = asm.clips.get(cand_code)
        if cand and cand.video_path and Path(cand.video_path).exists():
            src_clip = Path(cand.video_path)
            break
    if src_clip is None:
        print("[错误] 没有任何已就绪切片可作为底片，请先上传至少一个动作切片")
        return False

    action_dir = PROJECT_ROOT / "data" / "avatar_actions" / f"action_default_{action_code}"
    action_dir.mkdir(parents=True, exist_ok=True)
    out_video = action_dir / "clip.mp4"

    print(f"[进行] 正在为动作 {action_code} ({clip.action_name}) 合成占位切片，底片: {src_clip.name}")
    if not _compose_placeholder_clip(src_clip, out_video, action_code, clip.action_name):
        return False

    # 更新数据库记录，指向真实存在的视频与即将抽取的帧目录
    frames_dir = action_dir / "frames"
    frames_dir.mkdir(parents=True, exist_ok=True)
    async with AsyncSessionLocal() as db:
        rec = await db.get(AvatarAction, f"action_default_{action_code}")
        if rec is None:
            # 全局默认动作缺失记录时新建 (anchor_id = NULL 表示全局通用)
            rec = AvatarAction(
                id=f"action_default_{action_code}",
                anchor_id=None,
                action_code=action_code,
                action_name=clip.action_name,
                video_path=out_video.as_posix(),
                frames_dir=frames_dir.as_posix(),
                trigger_type="both",
                trigger_keywords=",".join(
                    r["keywords"] for r in asm.keyword_rules if r.get("action_code") == action_code
                ),
                trigger_events=",".join(
                    k for k, v in asm.event_rules.items() if v.get("action_code") == action_code
                ),
                duration_sec=clip.duration_sec,
                priority=clip.priority,
                mirror_loop=1,
                is_active=1,
            )
            db.add(rec)
        else:
            rec.video_path = out_video.as_posix()
            rec.frames_dir = frames_dir.as_posix()
        await db.commit()

    # 经标准管线抽帧入库 (若已有主播则挂接，否则仅落盘+更新动作表)
    mgr = get_avatar_task_manager()
    async with AsyncSessionLocal() as db:
        res = await db.execute(select(Anchor).where(Anchor.avatar_asset_dir.isnot(None)).limit(1))
        anchor = res.scalars().first()

    if anchor is not None:
        try:
            await mgr.process_action_slice(
                anchor_id=anchor.id,
                action_code=action_code,
                action_name=clip.action_name,
                video_path=out_video.as_posix(),
                duration_sec=clip.duration_sec,
                priority=clip.priority,
            )
            print(f"[进行] 已通过标准管线为主播 {anchor.id} 挂接动作 {action_code} 切片")
        except Exception as exc:
            print(f"[警告] 标准管线挂接失败，资产已落盘可供手动挂接: {exc}")

    # 重新加载并校验
    await asm.load_configs_from_db()
    new_clip = asm.clips.get(action_code)
    ok = new_clip is not None and new_clip.total_frames > 0
    if ok:
        print(f"[成功] 动作 {action_code} ({new_clip.action_name}) 现已就绪: {new_clip.total_frames} 帧")
    else:
        print(f"[失败] 动作 {action_code} 补齐后仍无可用帧")
    return ok


async def _main() -> int:
    parser = argparse.ArgumentParser(description="补齐缺失的数字人动作切片资产")
    parser.add_argument("--action", type=int, help="指定补齐的动作编号 (如 3)")
    parser.add_argument("--all-missing", action="store_true", help="自动补齐全部缺失动作")
    args = parser.parse_args()

    if args.action is None and not args.all_missing:
        parser.print_help()
        return 1

    from server.core.avatar.action_state_machine import get_action_state_machine

    asm = get_action_state_machine()
    await asm.load_configs_from_db()
    status = asm.get_status()
    missing = status.get("missing_action_codes", [])

    if not missing:
        print("[完成] 全部核心带货动作切片均已就绪，无需补齐")
        return 0

    print(f"[信息] 检测到缺失动作: {missing} ({status.get('missing_action_names')})")

    targets = [args.action] if args.action is not None else list(missing)
    results = []
    for code in targets:
        if code not in missing and code != args.action:
            print(f"[跳过] 动作 {code} 不在缺失清单中")
            continue
        results.append(await _fill_action(code))

    return 0 if all(results) else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
