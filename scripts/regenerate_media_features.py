#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
媒体特征一次性重算脚本 (幂等)
- 重新提取所有音色档案的声学特征向量 (.npy)，替换历史占位文件
- 重新检测所有形象/主播照片的人脸特征 (.json)，替换历史占位缓存
影响范围：仅覆盖 data/voices/*.npy 与 data/avatars/*_landmarks.json，不改动数据库结构。
用法：python scripts/regenerate_media_features.py --yes
"""
import argparse
import asyncio
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass


async def regenerate_voices():
    from sqlalchemy import select
    from server.database.db import AsyncSessionLocal
    from server.database.models import VoiceProfile
    from server.core.audio.features import extract_voice_features, save_embedding

    fixed, total = 0, 0
    async with AsyncSessionLocal() as session:
        rows = (await session.execute(select(VoiceProfile))).scalars().all()
        total = len(rows)
        for v in rows:
            wav = v.sample_wav_path
            if not wav or not Path(wav).exists():
                continue
            feat = extract_voice_features(wav)
            if feat.get("vector"):
                save_embedding(feat["vector"], v.embedding_npy_path or (Path(wav).with_suffix(".npy")).as_posix())
                fixed += 1
    print(f"[音色] 重算完成: {fixed}/{total}")


async def regenerate_avatars():
    from sqlalchemy import select
    from server.database.db import AsyncSessionLocal
    from server.database.models import Avatar
    from server.core.vision.face_landmarks import detect_face_landmarks

    fixed, total = 0, 0
    async with AsyncSessionLocal() as session:
        rows = (await session.execute(select(Avatar))).scalars().all()
        total = len(rows)
        for a in rows:
            src = a.source_file_path
            if not src or not Path(src).exists():
                continue
            if Path(src).suffix.lower() not in (".png", ".jpg", ".jpeg", ".bmp", ".webp"):
                continue
            cache = a.preprocessed_cache_path or (Path(src).parent / f"{a.id}_landmarks.json").as_posix()
            detect_face_landmarks(src, cache)
            a.preprocessed_cache_path = cache
            fixed += 1
        await session.commit()
    print(f"[形象] 重算完成: {fixed}/{total}")


async def main():
    parser = argparse.ArgumentParser(description="重算媒体特征 (声学/人脸)")
    parser.add_argument("--yes", action="store_true", help="确认执行 (否则仅提示)")
    args = parser.parse_args()
    if not args.yes:
        print("本脚本将覆盖 data/ 下的音色特征与形象人脸缓存。确认请加 --yes")
        return
    await regenerate_voices()
    await regenerate_avatars()
    print("全部完成。建议重启服务以加载新特征。")


if __name__ == "__main__":
    asyncio.run(main())
