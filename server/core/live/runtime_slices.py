# -*- coding: utf-8 -*-
"""切片转场/播放、视觉巡检、优雅关闭与停播。

由 server/routes/live.py 的 LiveSessionController 拆出。
缝在**函数体内**从 server.routes.live 解析，见 test_live_seam_late_binding.py。
"""
import logging
import asyncio
import inspect
import time
import uuid
import numpy as np
from server.adapters.media.media_router import global_media_router
from server.core.media.hybrid_stitcher import CROSSFADE_SAMPLES_16K, HybridSliceHandle, apply_audio_crossfade
from server.core.vision.capture import global_vision
from typing import Optional
logger = logging.getLogger("LiveAgent.LiveController")


class RuntimeSlicesMixin:
    """切片转场/播放、视觉巡检、优雅关闭与停播"""
    def _on_slice_transition_filler(
        self,
        old_slice: Optional[HybridSliceHandle],
        next_slice: Optional[HybridSliceHandle],
        filler_text: str,
    ) -> None:
        """
        切片转场过场词事件钩子：
        1. 登记当前直播上下文，并通过 WebSocket 广播给前端中控大屏；
        2. 启动异步任务调用 synthesize_preview_audio 合成该语气过场词音频；
        3. 向 global_virtual_audio 投递播放 (带 audio_generation 栅栏，支持打断)。
        """
        from server.routes.live import global_virtual_audio
        try:
            from server.routes.ws_live import ws_manager
            old_slot = old_slice.slot_code if old_slice else ""
            next_slot = next_slice.slot_code if next_slice else ""
            logger.info(
                f"[LiveSessionController] 切片转场过场词触发: '{filler_text}' "
                f"({old_slot} -> {next_slot})"
            )
            self.live_context["last_transition_filler"] = filler_text

            async def _synthesize_and_play():
                try:
                    # 1. 广播信令给中控大屏
                    await ws_manager.broadcast(
                        "TRANSITION_FILLER",
                        {
                            "filler_text": filler_text,
                            "old_slice_id": old_slice.slice_id if old_slice else None,
                            "old_slot_code": old_slot,
                            "next_slice_id": next_slice.slice_id if next_slice else None,
                            "next_slot_code": next_slot,
                            "timestamp": time.time(),
                        },
                    )

                    # 2. TTS 合成过场词语音
                    from server.core.audio.tts_preview_service import (
                        PreviewSpeechParams,
                        synthesize_preview_audio,
                    )
                    gen_before = self._audio_generation
                    tts_params = PreviewSpeechParams(
                        provider_name="edge_tts",
                        voice_name="zh-CN-XiaoxiaoNeural",
                        text=filler_text,
                    )
                    audio_bytes, _media_type = await synthesize_preview_audio(tts_params)

                    # 3. 栅栏检查：若在合成期间发生了打断或已停播，则丢弃
                    if (
                        not self.is_live
                        or self._audio_generation != gen_before
                        or not audio_bytes
                    ):
                        return

                    # 4. 投递到 global_virtual_audio 播放
                    global_virtual_audio.play_chunk(
                        audio_bytes,
                        fallback_sample_rate=24000,
                        codec="wav",
                        audio_generation=self._audio_generation,
                        audio_id=f"filler_{uuid.uuid4().hex[:8]}_{self._audio_generation}",
                    )
                    logger.info(
                        f"[LiveSessionController] 过场词音频已送入虚拟声卡: '{filler_text}', gen={self._audio_generation}"
                    )
                except Exception as ex:
                    logger.debug(f"[LiveSessionController] 过场词合成/播放异常: {ex}")

            try:
                loop = asyncio.get_running_loop()
                loop.create_task(_synthesize_and_play())
            except RuntimeError:
                pass
        except Exception as e:
            logger.debug(f"[LiveSessionController] 切片转场回调处理异常: {e}")


    def _on_slice_audio_playback(
        self,
        slice_handle: HybridSliceHandle,
        start_frame: int = 0,
        use_crossfade: bool = True,
    ) -> None:
        """
        向 virtual_audio 投递切片音频，实现动静分离中静轨的声音真实播出。
        支持每轮播动态微变速、WAV头自动剥离、断点重入截取与 50ms 平滑 Cross-fade。
        """
        from server.routes.live import global_virtual_audio
        if not self.is_live or slice_handle is None:
            return

        try:
            pcm_bytes = slice_handle.get_audio_pcm(dynamic_varispeed=True, start_frame=start_frame)
            if not pcm_bytes:
                return

            pcm_float = np.frombuffer(pcm_bytes, dtype=np.int16).astype(np.float32) / 32768.0

            if use_crossfade and len(pcm_float) > CROSSFADE_SAMPLES_16K:
                prev_tail = getattr(self, "_last_slice_audio_tail", None)
                if prev_tail is None:
                    prev_tail = np.zeros(CROSSFADE_SAMPLES_16K, dtype=np.float32)
                faded_pcm = apply_audio_crossfade(
                    prev_tail,
                    pcm_float,
                    crossfade_samples=CROSSFADE_SAMPLES_16K,
                )
                self._last_slice_audio_tail = pcm_float[-CROSSFADE_SAMPLES_16K:].copy()
                pcm_bytes = (np.clip(faded_pcm, -1.0, 1.0) * 32767.0).astype(np.int16).tobytes()
            else:
                self._last_slice_audio_tail = (
                    pcm_float[-CROSSFADE_SAMPLES_16K:].copy()
                    if len(pcm_float) >= CROSSFADE_SAMPLES_16K
                    else None
                )

            audio_id = f"slice_{slice_handle.slice_id}_{self._audio_generation}_{start_frame}"
            global_virtual_audio.play_chunk(
                pcm_bytes,
                fallback_sample_rate=16000,
                codec="pcm_s16le",
                allow_raw_pcm=True,
                audio_generation=self._audio_generation,
                audio_id=audio_id,
            )
            logger.info(
                f"[LiveSessionController] 投递切片音频至虚拟声卡: {slice_handle.slice_id} "
                f"(帧偏移={start_frame}/{slice_handle.total_frames}, 字节数={len(pcm_bytes)}, "
                f"比率={slice_handle.current_loop_speed_ratio:.4f}, gen={self._audio_generation})"
            )
        except Exception as e:
            logger.warning(f"[LiveSessionController] 投递切片音频失败: {e}")


    async def _vision_loop(self):
        """视觉感知采集：按配置频次抓取画面并写入 live_context 供多模态大脑感知"""
        from server.database.db import AsyncSessionLocal
        from server.routes.settings import _load_vision_config
        while self.is_live:
            try:
                async with AsyncSessionLocal() as session:
                    cfg = await _load_vision_config(session)
                if cfg.get("enabled"):
                    b64 = await asyncio.to_thread(global_vision.capture_b64)
                    if b64:
                        self.live_context["vision_image_b64"] = b64
                await asyncio.sleep(max(0.5, float(cfg.get("interval_sec", 2.5))))
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.debug(f"视觉采集巡检异常: {e}")
                await asyncio.sleep(2.0)


    async def _safe_close(
        self,
        name: str,
        closer,
        timeout: float = 3.0,
    ) -> None:
        """Fail-Safe 清理闭包：接收清理函数，严格超时控制与异常隔离，记录清理错误"""
        try:
            result = closer()
            if inspect.isawaitable(result):
                await asyncio.wait_for(result, timeout=timeout)
        except asyncio.TimeoutError:
            err_msg = f"清理资源超时: {name}"
            logger.error(err_msg)
            self.cleanup_errors.append(err_msg)
        except BaseException as e:
            err_msg = f"清理资源失败: {name} ({e})"
            logger.exception(err_msg)
            self.cleanup_errors.append(err_msg)


    async def stop(self) -> list:
        """Fail-Safe 幂等停止并隔离等待所有会话资源退出，严格按 8 步安全顺序清理，并返回清理错误列表。"""
        from server.routes.live import _call_sync_compat, global_virtual_audio
        self.is_live = False
        self._session_generation += 1
        self._audio_generation += 1
        self.cleanup_errors = []

        try:
            # 1. 立即停止接收新弹幕
            if self.fetcher:
                await self._safe_close("danmaku_fetcher", self.fetcher.stop, timeout=3.0)
                self.fetcher = None

            # 2. 取消主消费、场观、视觉与缝合器任务
            session_tasks = [
                task for task in (self.worker_task, self.viewer_task, self.vision_task, self.stitcher_task)
                if task and not task.done()
            ]
            for task in session_tasks:
                task.cancel()
            if session_tasks:
                await asyncio.gather(*session_tasks, return_exceptions=True)
            self.worker_task = self.viewer_task = self.vision_task = self.stitcher_task = None
            try:
                self.stitcher.clear_playlist()
                self._last_slice_audio_tail = None
            except Exception:
                pass

            # 3. 收敛后台广播/数据库任务
            pending = [task for task in self._background_tasks if not task.done()]
            if pending:
                try:
                    await asyncio.wait_for(asyncio.gather(*pending, return_exceptions=True), timeout=2.0)
                except asyncio.TimeoutError:
                    for task in pending:
                        if not task.done():
                            task.cancel()
                    await asyncio.gather(*pending, return_exceptions=True)
            self._background_tasks.clear()

            # 4. 停止音频和 TTS
            current_tts_task = self.current_tts_task
            self.current_tts_task = None
            if current_tts_task and not current_tts_task.done():
                current_tts_task.cancel()
                await asyncio.gather(current_tts_task, return_exceptions=True)
            if hasattr(self, "tts_driver") and self.tts_driver:
                await self._safe_close("tts_driver", self.tts_driver.stop, timeout=3.0)
            await self._safe_close(
                "virtual_audio",
                lambda: _call_sync_compat(global_virtual_audio.stop, next_generation=self._audio_generation),
                timeout=2.0,
            )

            # 5. 停止远程 GPU 和本地媒体
            await self._safe_close("detach_remote", global_media_router.detach_remote, timeout=2.0)
            await self._safe_close("media_router", global_media_router.stop, timeout=3.0)
            await self._safe_close("detach_sidecar", global_media_router.detach_sidecar, timeout=2.0)
            global_media_router.reset_media_degraded()
            global_media_router.set_on_degraded(None)

            # 6. 停止摄像头、声卡和视觉感知设备
            await self._safe_close("vision", global_vision.close, timeout=2.0)
            self.live_context.pop("vision_image_b64", None)
            if hasattr(self, "vram_watchdog") and self.vram_watchdog:
                await self._safe_close("vram_watchdog", self.vram_watchdog.stop, timeout=2.0)
            from server.core.monitoring.system_resource_watchdog import global_resource_watchdog
            await self._safe_close("resource_watchdog", global_resource_watchdog.stop, timeout=2.0)
            from server.core.monitoring.media_quality_watchdog import global_media_watchdog
            await self._safe_close("media_watchdog", global_media_watchdog.stop, timeout=2.0)

            # 7. 清空事件队列
            await self._safe_close("event_queue", self.event_queue.clear, timeout=1.0)

            # 7.1 关闭残留的 WebRTC/WHEP 会话，避免跨场次泄漏 ICE/DTLS 连接
            try:
                from server.core.media.webrtc_streamer import get_webrtc_stream_manager
                webrtc_manager = get_webrtc_stream_manager()
                if webrtc_manager is not None:
                    await self._safe_close("webrtc_sessions", webrtc_manager.close_all, timeout=3.0)
            except Exception as webrtc_close_exc:
                logger.debug(f"WebRTC 会话收敛跳过（可选依赖缺失）: {webrtc_close_exc}")

        finally:
            # 8. 最终复位所有身份和引用
            self.is_live = False
            self.session_id = None
            self.obs_owner_session_id = None
            self.obs_owner_connection_epoch = None
            self.live_context = {"products": []}
            self.fetcher = None
            self.worker_task = self.viewer_task = self.vision_task = None
            logger.info("直播会话已安全释放所有硬件与内存资源 (清理错误数: %d)", len(self.cleanup_errors))

        return list(self.cleanup_errors)
