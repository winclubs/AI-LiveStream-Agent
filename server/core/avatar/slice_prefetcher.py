"""话术前瞻预取与切片缓冲管理器 (路径二专用)。

为解决扩散模型 (LatentSync) 无法纯逐帧推流的问题，
本模块在后台异步先行将下一段/下两段话术提交云端渲染，
生成高清切片并存入本地缓冲环，播放时即时命中，实现零延迟、顶尖口型同步。
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Callable, Coroutine, Sequence

from server.adapters.media.avatar_provider import AvatarRenderMode, ProviderRenderResult, RemoteAvatarProvider
from server.core.media.audio_frame import AudioFrame

logger = logging.getLogger("LiveAgent.SlicePrefetcher")


def _declares_slice_cache(provider: Any) -> bool:
    """判断 provider 是否**在类上**实现了切片缓存接口。

    必须看类而不是实例属性：`MagicMock` 会为任意属性自动生成可调用对象，
    用 `hasattr`/实例 getattr 判定会把测试替身误判成具备缓存能力的真实 Provider。
    """
    return callable(getattr(type(provider), "get_cached_slice", None)) and callable(
        getattr(type(provider), "play_cached_slice", None)
    )


class SliceState(StrEnum):
    PENDING = "pending"
    RENDERING = "rendering"
    READY = "ready"
    CONSUMED = "consumed"
    CANCELLED = "cancelled"
    FAILED = "failed"


@dataclass
class PrefetchedSlice:
    audio_id: str
    text_content: str
    frames: Sequence[AudioFrame]
    state: SliceState = SliceState.PENDING
    created_at: float = field(default_factory=time.monotonic)
    rendered_at: float = 0.0
    result: ProviderRenderResult | None = None
    slice_data: Any = None
    error: str = ""


class SlicePrefetcher:
    """双轨制话术切片预取与生命周期管理器。"""

    def __init__(
        self,
        provider: RemoteAvatarProvider,
        lookahead_depth: int = 2,
        slice_ttl_seconds: float = 300.0,
    ) -> None:
        self.provider = provider
        self.lookahead_depth = max(1, int(lookahead_depth))
        self.slice_ttl_seconds = float(slice_ttl_seconds)

        self._slices: dict[str, PrefetchedSlice] = {}
        self._active_tasks: dict[str, asyncio.Task] = {}
        self._lock = asyncio.Lock()
        self._is_running = True

    @property
    def ready_count(self) -> int:
        return sum(1 for s in self._slices.values() if s.state is SliceState.READY)

    @property
    def rendering_count(self) -> int:
        return sum(1 for s in self._slices.values() if s.state is SliceState.RENDERING)

    async def prefetch_sentence(
        self,
        audio_id: str,
        frames: Sequence[AudioFrame],
        text_content: str = "",
    ) -> None:
        """异步触发某一句段的高清预渲染。"""
        if not self._is_running:
            return

        async with self._lock:
            # 清理过期切片
            now = time.monotonic()
            expired_keys = [
                k for k, v in self._slices.items()
                if now - v.created_at > self.slice_ttl_seconds
            ]
            for k in expired_keys:
                self._slices.pop(k, None)

            # 若已在缓冲或渲染中则跳过
            if audio_id in self._slices:
                return

            slice_obj = PrefetchedSlice(
                audio_id=audio_id,
                text_content=text_content,
                frames=frames,
                state=SliceState.PENDING,
            )
            self._slices[audio_id] = slice_obj

            # 启动后台预渲染任务
            task = asyncio.create_task(self._do_render_slice(slice_obj))
            self._active_tasks[audio_id] = task
            task.add_done_callback(lambda t, aid=audio_id: self._active_tasks.pop(aid, None))

    async def _do_render_slice(self, slice_obj: PrefetchedSlice) -> None:
        slice_obj.state = SliceState.RENDERING
        try:
            logger.info("开始前瞻预渲染话术切片 audio_id=%s", slice_obj.audio_id)
            # publish=False：预取阶段只落切片缓存，绝不上屏。
            # 否则预取与正式播放会各上屏一次，造成重复发布与画面回跳。
            result = await self._call_render_publish_off(slice_obj.frames)
            slice_obj.result = result
            slice_obj.rendered_at = time.monotonic()
            slice_obj.state = SliceState.READY
            # 具备切片缓存的 Provider (扩散批处理) 必须校验真的拿到可播放帧：
            # 预取"成功"却无帧可放行，会让正式播放阶段空上屏。
            # 不具备缓存的 Provider (如流式 sidecar，渲染期间已实时出帧) 则
            # 沿用「渲染成功即就绪」语义，预取仅贡献提前量。
            if _declares_slice_cache(self.provider):
                slice_obj.slice_data = self._resolve_cached_frames(
                    slice_obj.audio_id, result
                )
                if not slice_obj.slice_data:
                    slice_obj.state = SliceState.FAILED
                    slice_obj.error = "预取完成但未取得可播放帧序列"
                    logger.warning(
                        "话术切片预取无可播放帧 audio_id=%s", slice_obj.audio_id
                    )
                    return
            logger.info(
                "话术切片预渲染就绪 audio_id=%s, 耗时=%.1fms, 帧数=%d",
                slice_obj.audio_id,
                result.latency_ms,
                result.output_frames,
            )
        except asyncio.CancelledError:
            slice_obj.state = SliceState.CANCELLED
            raise
        except Exception as exc:
            slice_obj.state = SliceState.FAILED
            slice_obj.error = str(exc)
            logger.warning("话术切片预渲染失败 audio_id=%s: %s", slice_obj.audio_id, exc)

    async def _call_render_publish_off(self, frames: Sequence[AudioFrame]):
        """以 publish=False 调用 provider 渲染。

        兼容未实现 publish 关键字的 Provider（例如旧 sidecar 适配器）：
        此时退化为默认 publish 语义，但预取本身仍只用于提前量，不改变正确性。
        """
        render = getattr(self.provider, "render_sentence", None)
        if render is None:
            raise RuntimeError("Avatar Provider 未实现 render_sentence，无法预取")
        try:
            return await render(frames, render_mode=AvatarRenderMode.BATCH, publish=False)
        except TypeError as exc:
            if "publish" not in str(exc):
                raise
            logger.debug(
                "Provider %s 不支持 publish 关键字，预取退化为默认语义",
                getattr(self.provider, "provider_id", "?"),
            )
            return await render(frames, render_mode=AvatarRenderMode.BATCH)

    def _resolve_cached_frames(self, audio_id: str, result) -> list:
        """从 provider 缓存取回预取帧序列。

        切片帧的所有权始终在 provider 侧（渲染结果由 provider 解码与持有），
        预取器只负责生命周期与「提前量」，不复制大块字节数组。
        """
        getter = getattr(self.provider, "get_cached_slice", None)
        if callable(getter):
            try:
                frames = list(getter(audio_id) or ())
                if frames:
                    return frames
            except Exception:
                logger.debug("读取 provider 预取缓存失败", exc_info=True)
        data = getattr(result, "slice_data", None)
        if isinstance(data, (list, tuple)) and data:
            return list(data)
        return []

    async def acquire_slice(self, audio_id: str) -> PrefetchedSlice | None:
        """播放时获取预渲染完成的高精切片，并立即启动实时上屏。

        命中即消费：同一切片不会被播放两次。provider 不支持缓存回放时
        （例如旧 sidecar 适配器），仅返回切片对象，由调用方走常规派发。
        """
        async with self._lock:
            slice_obj = self._slices.get(audio_id)
            if slice_obj is None or slice_obj.state is not SliceState.READY:
                return None
            slice_obj.state = SliceState.CONSUMED
            frames = slice_obj.slice_data or []
            playable = bool(frames)

        if playable:
            player = getattr(self.provider, "play_cached_slice", None)
            if callable(player):
                try:
                    started = await player(audio_id)
                    if started:
                        logger.info(
                            "预取切片命中并已上屏 audio_id=%s, 帧数=%d",
                            audio_id, len(frames),
                        )
                except Exception:
                    logger.warning(
                        "预取切片上屏失败 audio_id=%s，回退常规派发", audio_id,
                        exc_info=True,
                    )
        return slice_obj

    async def interrupt_and_clear(self) -> None:
        """突发实时互动/弹幕打断时调用：瞬间取消后台任务并清空切片。"""
        logger.info("突发打断：正在取消后台预渲染并清空切片队列")
        async with self._lock:
            for task in self._active_tasks.values():
                if not task.done():
                    task.cancel()
            self._active_tasks.clear()
            self._slices.clear()

        # 发送远程取消信号
        try:
            await self.provider.interrupt("User interrupt")
        except Exception:
            pass

        # 打断后必须连 provider 侧切片缓存一起清空：预取完成的整句切片若残留，
        # 会在后续同 audio_id 的播放中「复活」已被打断的旧画面，造成拖尾与错位。
        drop = getattr(self.provider, "clear_slice_cache", None)
        if callable(drop):
            try:
                drop()
            except Exception:
                logger.debug("清空 provider 切片缓存失败", exc_info=True)


    async def close(self) -> None:
        self._is_running = False
        await self.interrupt_and_clear()
