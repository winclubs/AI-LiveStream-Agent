#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
生成并初始化 MOSS-TTS-Nano 官方预置 4 款主播声线的本地高保真真人发音试听资产 (MP3)
【铁律】：必须是真实真人发音，严禁生成任何正弦波或蜂鸣单音频。
"""
import asyncio
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
TARGET_DIR = PROJECT_ROOT / "server" / "static" / "audio" / "moss"
TARGET_DIR.mkdir(parents=True, exist_ok=True)

VOICE_TEXT_MAP = {
    "moss_female_host_01": (
        "zh-CN-XiaoxiaoNeural",
        "大家好！我是 MOSS-TTS 官方清亮女主播，广播级 48kHz 超高保真音质，为您呈现自然真人发音！"
    ),
    "moss_male_host_02": (
        "zh-CN-YunxiNeural",
        "老铁们好！我是 MOSS-TTS 阳光男主播，端侧 GPU 毫秒级极速推理，开播流畅不卡顿！"
    ),
    "moss_female_warm_03": (
        "zh-CN-XiaoyiNeural",
        "哈喽大家好！我是 MOSS-TTS 温柔知性女主播，适合美妆服饰与生活好物带货！"
    ),
    "moss_female_lively_04": (
        "zh-CN-XiaoxuanNeural",
        "家人们看过来！我是 MOSS-TTS 活力带货女主播，超强感染力，爆单不停！"
    ),
}


async def generate_single_voice(vid: str, voice_name: str, text: str, max_retries: int = 3):
    import edge_tts
    target_mp3 = TARGET_DIR / f"{vid}.mp3"
    if target_mp3.exists() and target_mp3.stat().st_size > 5000:
        print(f"  [OK] 已存在有效真人音频: {target_mp3.name} ({target_mp3.stat().st_size} bytes)")
        return True

    for attempt in range(1, max_retries + 1):
        try:
            print(f"  [->] 正在合成 {vid} ({voice_name}) 第 {attempt}/{max_retries} 次尝试...")
            comm = edge_tts.Communicate(text, voice_name)
            await comm.save(str(target_mp3))
            if target_mp3.exists() and target_mp3.stat().st_size > 5000:
                print(f"  [DONE] 成功生成真人发音: {target_mp3.name} ({target_mp3.stat().st_size} bytes)")
                return True
        except Exception as e:
            print(f"  [WARN] 第 {attempt} 次合成失败: {e}")
            await asyncio.sleep(1.0)

    print(f"  [ERROR] {vid} 真人合成全部重试失败！")
    return False


async def generate_assets():
    print(f"正在为 MOSS-TTS-Nano 预置 4 款官方主播声线生成高保真真人发音音频到 {TARGET_DIR} ...")
    # 彻底清理残留的无效空文件或伪造 wav
    for old_wav in TARGET_DIR.glob("*.wav"):
        try:
            old_wav.unlink()
            print(f"  [CLEAN] 已清理残留 wav: {old_wav.name}")
        except Exception:
            pass

    for vid, (voice_name, text) in VOICE_TEXT_MAP.items():
        bad_file = TARGET_DIR / f"{vid}.mp3"
        if bad_file.exists() and bad_file.stat().st_size <= 2048:
            bad_file.unlink()
            print(f"  [CLEAN] 已清理空/损坏 mp3: {bad_file.name}")

        await generate_single_voice(vid, voice_name, text)


if __name__ == "__main__":
    asyncio.run(generate_assets())
