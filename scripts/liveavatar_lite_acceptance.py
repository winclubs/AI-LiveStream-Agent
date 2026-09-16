"""LiveAvatar LITE 官方 Sandbox 控制面验收。

该脚本不会消费 LiveKit 视频轨；默认以退出码 2 标记完整媒体验收仍受阻。
使用 --control-only 时，只要生命周期、音频终态与中断验证通过则退出 0。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import struct
import sys
import wave
from pathlib import Path

from server.adapters.media.avatar_provider import (
    AvatarRenderMode,
    ProviderError,
    ProviderErrorCode,
)
from server.adapters.media.liveavatar_lite_provider import (
    LiveAvatarLiteAvatarProvider,
)
from server.core.media.audio_frame import AudioFormat, AudioFrame

SANDBOX_AVATAR_ID = "dd73ea75-1218-4ef3-92ce-606d5f7fbc0a"


def _load_wav(path: Path) -> bytes:
    with wave.open(str(path), "rb") as wav_file:
        if wav_file.getnchannels() != 1:
            raise ValueError("WAV 必须是 mono")
        if wav_file.getsampwidth() != 2:
            raise ValueError("WAV 必须是 PCM16")
        if wav_file.getframerate() != 24_000:
            raise ValueError("WAV 必须是 24kHz")
        return wav_file.readframes(wav_file.getnframes())


def _tone_pcm(seconds: float) -> bytes:
    samples = max(1, int(24_000 * seconds))
    return b"".join(
        struct.pack("<h", int(2500 * math.sin(2 * math.pi * 220 * index / 24_000)))
        for index in range(samples)
    )


def _audio_frames(pcm: bytes, audio_id: str) -> tuple[AudioFrame, ...]:
    if not pcm or len(pcm) % 2:
        raise ValueError("PCM 数据必须非空并按 16-bit 样本对齐")
    fmt = AudioFormat(codec="pcm_s16le", sample_rate=24_000, channels=1)
    chunk_bytes = 24_000 * 2
    chunks = [pcm[offset : offset + chunk_bytes] for offset in range(0, len(pcm), chunk_bytes)]
    frames = []
    pts_samples = 0
    for sequence, chunk in enumerate(chunks):
        frames.append(
            AudioFrame(
                data=chunk,
                format=fmt,
                audio_id=audio_id,
                sequence=sequence,
                pts_samples=pts_samples,
                audio_generation=1,
                session_generation=1,
                is_first=sequence == 0,
                is_final=sequence == len(chunks) - 1,
            )
        )
        pts_samples += len(chunk) // 2
    return tuple(frames)


async def _run(args: argparse.Namespace) -> tuple[int, dict]:
    api_key = os.environ.get(args.api_key_env, "").strip()
    report = {
        "provider": "liveavatar_lite",
        "sandbox": True,
        "avatar_id": args.avatar_id,
        "checks": {},
        "overall": "blocked",
    }
    if not api_key:
        report["checks"]["credentials"] = {
            "status": "blocked",
            "message": f"环境变量 {args.api_key_env} 未设置",
        }
        return 2, report

    source = "synthetic_tone_protocol_only"
    pcm = _tone_pcm(1.5)
    if args.wav:
        pcm = _load_wav(Path(args.wav))
        source = "user_supplied_pcm16_24khz_mono_wav"
    report["checks"]["audio_fixture"] = {
        "status": "pass",
        "source": source,
        "bytes": len(pcm),
        "note": "合成音只验证协议；口型质量需使用真实语音并人工观看视频轨",
    }

    provider = LiveAvatarLiteAvatarProvider(
        provider_id="sandbox:liveavatar_lite",
        display_name="LiveAvatar LITE Sandbox",
        api_base_url="https://api.liveavatar.com",
        api_key=api_key,
        avatar_id=args.avatar_id,
        sandbox_only=True,
        max_session_duration=60,
        keep_alive_seconds=30,
        connect_timeout=args.connect_timeout,
        message_timeout=args.message_timeout,
        request_timeout=args.request_timeout,
    )
    try:
        await provider.start()
        status = provider.get_status()
        report["checks"]["lifecycle"] = {
            "status": "pass" if status["control_ready"] else "fail",
            "livekit_metadata_received": status["livekit_metadata_received"],
        }

        result = await provider.render_sentence(
            _audio_frames(pcm, "sandbox-completion"),
            render_mode=AvatarRenderMode.REALTIME,
        )
        report["checks"]["audio_completion"] = {
            "status": "pass",
            "completion_scope": result.evidence["completion_scope"],
            "output_frames_observed": result.output_frames,
            "video_observed": result.evidence["video_observed"],
        }

        if not args.skip_interrupt:
            interrupt_task = asyncio.create_task(
                provider.render_sentence(
                    _audio_frames(_tone_pcm(4.0), "sandbox-interrupt"),
                    render_mode=AvatarRenderMode.REALTIME,
                )
            )
            await asyncio.sleep(0.35)
            await provider.interrupt("acceptance barge-in")
            try:
                await interrupt_task
            except ProviderError as exc:
                if exc.code is not ProviderErrorCode.STALE_GENERATION:
                    raise
            else:
                raise RuntimeError("中断后的 render_sentence 未以 stale_generation 收敛")
            report["checks"]["interrupt"] = {
                "status": "pass",
                "control_acknowledged": True,
                "video_quiescence_validated": False,
            }

        report["checks"]["video_track"] = {
            "status": "blocked",
            "message": "仓库未安装 LiveKit/aiortc，未消费或解码 LiveKit 视频轨",
        }
        report["checks"]["video_timestamps"] = {
            "status": "blocked",
            "message": "未观察视频帧，不能验证视频 timebase 或 sample PTS",
        }
        control_passed = all(
            item.get("status") == "pass"
            for key, item in report["checks"].items()
            if key not in {"video_track", "video_timestamps"}
        )
        report["overall"] = "control_passed_media_blocked" if control_passed else "failed"
        if control_passed and args.control_only:
            return 0, report
        return (2 if control_passed else 1), report
    except Exception as exc:
        report["checks"]["runtime"] = {
            "status": "fail",
            "error_type": type(exc).__name__,
            "message": str(exc),
        }
        report["overall"] = "failed"
        return 1, report
    finally:
        await provider.stop()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api-key-env", default="LIVEAVATAR_API_KEY")
    parser.add_argument("--avatar-id", default=SANDBOX_AVATAR_ID)
    parser.add_argument("--wav", help="可选的 PCM16/24kHz/mono WAV 语音样本")
    parser.add_argument("--skip-interrupt", action="store_true")
    parser.add_argument("--control-only", action="store_true")
    parser.add_argument("--connect-timeout", type=float, default=10.0)
    parser.add_argument("--message-timeout", type=float, default=20.0)
    parser.add_argument("--request-timeout", type=float, default=120.0)
    args = parser.parse_args()
    code, report = asyncio.run(_run(args))
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return code


if __name__ == "__main__":
    sys.exit(main())
