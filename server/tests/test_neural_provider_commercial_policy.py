import asyncio
from unittest.mock import AsyncMock, MagicMock
import pytest

from server.adapters.media.media_router import MediaDriverRouter
from server.adapters.media.avatar_orchestrator import AvatarDispatchOutcome
from server.core.media.audio_frame import AudioFormat, AudioFrame


def _make_dummy_frame():
    fmt = AudioFormat(codec="pcm_s16le", sample_rate=16000, channels=1)
    return AudioFrame(
        data=b"\x00\x00" * 320,
        format=fmt,
        audio_id="test_audio_001",
        sequence=0,
        pts_samples=0,
        audio_generation=1,
        session_generation=1,
        is_first=True,
        is_final=True,
        text="测试话术",
    )


def test_media_router_blocks_mock_mode_in_commercial_modes():
    """在 PRODUCTION 与 PILOT 商业档位下，禁止进入 Mock/模式D，必须 Fail-Closed"""
    router = MediaDriverRouter()

    # PREVIEW 档位允许模式 D (仅音频联调)
    router.set_execution_mode("PREVIEW")
    drv = router.select_driver(mode="D")
    assert drv is not None
    assert router.driver_type == "mock"

    # PRODUCTION 档位严禁模式 D
    router.set_execution_mode("PRODUCTION")
    with pytest.raises(RuntimeError, match="严禁使用轻量仿真媒体驱动"):
        router.select_driver(mode="D")

    # PILOT 档位严禁模式 D
    router.set_execution_mode("PILOT")
    with pytest.raises(RuntimeError, match="严禁使用轻量仿真媒体驱动"):
        router.select_driver(mode="D")


def test_media_router_blocks_unconfigured_neural_asset_in_commercial_modes():
    """商业档位下必须提供切片资产以驱动真实神经唇形，禁止缺失资产时无声回退假唇形"""
    router = MediaDriverRouter()
    router.set_execution_mode("PRODUCTION")
    with pytest.raises(RuntimeError, match="必须提供主播切片资产目录以驱动神经唇形"):
        router.select_driver(mode="B", avatar_asset_dir="")


def test_media_router_marks_degraded_on_orchestrator_failure():
    """在 PRODUCTION 档位下，远端神经 Provider 调度失败不得静默使用程序化假动作，必须触发 MEDIA_DEGRADED"""
    async def _run():
        router = MediaDriverRouter()
        router.set_execution_mode("PRODUCTION")

        degraded_reasons = []
        def on_degraded(reason):
            degraded_reasons.append(reason)
        router.set_on_degraded(on_degraded)

        mock_orchestrator = MagicMock()
        # 模拟编排器无可用 Provider 导致 fallback
        mock_orchestrator.dispatch_sentence = AsyncMock(
            return_value=AvatarDispatchOutcome(
                audio_id="test_audio_001",
                fallback_reason="no_eligible_provider",
            )
        )
        mock_orchestrator.provider_count = 1
        mock_orchestrator.batch_providers = MagicMock(return_value=[])
        mock_orchestrator.get_last_selected_provider = MagicMock(return_value=None)
        mock_orchestrator.get_snapshot = MagicMock(return_value={
            "running": True,
            "strategy": "auto",
            "provider_count": 1,
            "providers": [],
        })

        router.attach_avatar_orchestrator(mock_orchestrator)

        frame = _make_dummy_frame()
        await router._submit_sidecar_frames([frame])

        assert router.is_degraded is True
        assert "no_eligible_provider" in router.degrade_reason
        assert len(degraded_reasons) == 1
        assert "no_eligible_provider" in degraded_reasons[0]

        # 验证 preview_status 暴露 MEDIA_DEGRADED 阻断状态
        status = router.get_preview_status()
        assert status.get("media_degraded") is True
        assert status.get("state_detail") == "MEDIA_DEGRADED"

        # 验证在降级状态下，get_latest_jpeg 绝不向外输出虚假合成帧
        assert router.get_latest_jpeg() == b""

    asyncio.run(_run())


def test_media_router_allows_procedural_fallback_in_preview_mode():
    """在 PREVIEW 预览/测试档位下，允许程序化降级并记录警告，不阻断本地研发体验"""
    async def _run():
        router = MediaDriverRouter()
        router.set_execution_mode("PREVIEW")

        mock_orchestrator = MagicMock()
        mock_orchestrator.dispatch_sentence = AsyncMock(
            return_value=AvatarDispatchOutcome(
                audio_id="test_audio_001",
                fallback_reason="sidecar_timeout",
            )
        )
        mock_orchestrator.batch_providers = MagicMock(return_value=[])
        router.attach_avatar_orchestrator(mock_orchestrator)

        frame = _make_dummy_frame()
        await router._submit_sidecar_frames([frame])

        # 预览档位不标记为 MEDIA_DEGRADED
        assert router.is_degraded is False

    asyncio.run(_run())
