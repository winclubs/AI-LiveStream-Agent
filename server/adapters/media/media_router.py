"""
本地媒体驱动路由中枢：管理程序化头像、远程帧与 mock 降级，不负责外部平台发布。
"""
import asyncio
import inspect
import logging
from typing import Optional
from server.adapters.media.avatar_orchestrator import AvatarProviderOrchestrator
from server.adapters.media.avatar_provider import AvatarPreviewProvider
from server.adapters.media.base_driver import BaseMediaDriver
from server.adapters.media.musetalk_driver import (
    global_procedural_avatar_driver,
    CV_AVAILABLE,
)
from server.adapters.media.mock_driver import global_media_driver as global_mock_media_driver
from server.core.media.audio_frame import validate_audio_frame_batch
from server.core.media.shared_playback_clock import global_shared_playback_clock
from server.core.media.virtual_cam import global_virtual_cam

logger = logging.getLogger("LiveAgent.MediaRouter")

class MediaDriverRouter(BaseMediaDriver):
    def __init__(self):
        super().__init__()
        self.is_running = False
        # 依赖能力检测：无 numpy/opencv 时回退仿真驱动，保证服务可用
        if CV_AVAILABLE:
            self.active_driver: BaseMediaDriver = global_procedural_avatar_driver
            self.driver_type = "procedural_avatar"
        else:
            self.active_driver = global_mock_media_driver
            self.driver_type = "mock"
        # 旧 Remote GPU（TTS+视频 v1/v2）与新 renderer-only sidecar v3 分离挂载。
        self.remote_driver = None
        self.sidecar_driver = None
        self.avatar_orchestrator: Optional[AvatarProviderOrchestrator] = None
        self._sidecar_tasks: set[asyncio.Task] = set()
        self._orchestrator_start_task: Optional[asyncio.Task] = None
        self._batch_prefetcher = None

    def attach_remote(self, driver):
        """挂载端云分离远程渲染驱动 (帧回传优先显示)"""
        self.remote_driver = driver
        logger.info("已挂载端云分离远程渲染帧通道 (Tier C)")

    def detach_remote(self):
        self.remote_driver = None

    def attach_sidecar(self, driver):
        """挂载 renderer-only v3 sidecar；本地 renderer 始终保留为 shadow fallback。"""
        self.avatar_orchestrator = None
        self.sidecar_driver = driver
        self._detach_batch_prefetcher()
        logger.info("已挂载神经渲染 sidecar v3，程序化 renderer 保持 shadow 模式")

    def detach_sidecar(self):
        self.sidecar_driver = None
        self.avatar_orchestrator = None
        self._detach_batch_prefetcher()

    def attach_avatar_orchestrator(
        self,
        orchestrator: AvatarProviderOrchestrator,
        *,
        preview_driver=None,
    ) -> None:
        """挂载多 Provider 编排器；sidecar_driver 保留为旧预览/诊断兼容视图。"""
        self.avatar_orchestrator = orchestrator
        self.sidecar_driver = preview_driver
        self._attach_batch_prefetcher(orchestrator)
        logger.info(
            "已挂载 %d 个远端 Avatar Provider，程序化 renderer 保持热 shadow",
            orchestrator.provider_count,
        )

    def detach_avatar_orchestrator(self) -> None:
        self.avatar_orchestrator = None
        self.sidecar_driver = None
        self._detach_batch_prefetcher()

    def _attach_batch_prefetcher(self, orchestrator: AvatarProviderOrchestrator) -> None:
        """为支持 BATCH 的 Provider 挂载句级前瞻预取器 (P0-1)。

        扩散模型 (LatentSync UNet3D) 单句渲染耗时通常超过音频时长，
        必须靠「提前渲染下一句」把批处理延迟藏进播放窗口，否则必然音画失步。
        无 BATCH Provider 时保持空转，不引入任何额外开销。
        """
        self._detach_batch_prefetcher()
        providers = orchestrator.batch_providers()
        if not providers:
            return
        from server.core.avatar.slice_prefetcher import SlicePrefetcher

        # 单 Provider 时直接使用其自身；多 Provider 时以首个 BATCH Provider 作为预取目标，
        # 实际派发仍由编排器按策略选择，预取命中与否只影响是否需要现算。
        prefetcher = SlicePrefetcher(providers[0], lookahead_depth=2)
        self._batch_prefetcher = prefetcher
        orchestrator.attach_prefetcher(prefetcher)
        logger.info(
            "已为 %d 个批处理 Avatar Provider 挂载句级前瞻预取器", len(providers)
        )

    def _detach_batch_prefetcher(self) -> None:
        if self._batch_prefetcher is not None:
            try:
                self._batch_prefetcher.close()
            except Exception:
                logger.debug("预取器关闭失败", exc_info=True)
            self._batch_prefetcher = None
        if self.avatar_orchestrator is not None:
            self.avatar_orchestrator.detach_prefetcher()

    async def prefetch_upcoming_sentence(self, frames) -> None:
        """对外暴露的句级前瞻预取入口 (供 live.py 在播报当前句时提前渲染下一句)。"""
        prefetcher = self._batch_prefetcher
        if prefetcher is None or not frames:
            return
        try:
            await prefetcher.prefetch_sentence(
                frames[0].audio_id, frames, getattr(frames[0], "text", "") or ""
            )
        except Exception:
            logger.debug("句级前瞻预取失败，保持本地 shadow", exc_info=True)


    def _track_sidecar_task(self, coroutine) -> asyncio.Task:
        task = asyncio.create_task(coroutine)
        self._sidecar_tasks.add(task)
        task.add_done_callback(self._sidecar_tasks.discard)
        return task

    def _orchestrator_started(self, task: asyncio.Task) -> None:
        if self._orchestrator_start_task is task:
            self._orchestrator_start_task = None
        try:
            task.result()
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.exception("远端 Avatar Provider 编排器启动失败")

    async def _submit_sidecar_frames(self, frame_list) -> None:
        try:
            if self.avatar_orchestrator is not None:
                outcome = await self.avatar_orchestrator.dispatch_sentence(frame_list)
                if outcome.fallback_reason:
                    logger.warning(
                        "远端 Avatar 当前句未接管，继续使用程序化 shadow: %s",
                        outcome.fallback_reason,
                    )
                return
            if self.sidecar_driver is not None:
                await self.sidecar_driver.feed_audio_frames(frame_list)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("神经渲染 Provider 事务失败，继续使用程序化画面: %s", exc)

    async def _cancel_sidecar_tasks(self, timeout: float = 2.0) -> None:
        tasks = [task for task in self._sidecar_tasks if not task.done()]
        for task in tasks:
            task.cancel()
        if tasks:
            done, pending = await asyncio.wait(tasks, timeout=timeout)
            for task in done:
                self._sidecar_tasks.discard(task)
                try:
                    task.result()
                except asyncio.CancelledError:
                    pass
                except Exception:
                    logger.debug("远端 Avatar 后台任务收敛时返回异常", exc_info=True)
            if pending:
                logger.error(
                    "%s 个远端 Avatar 后台任务未在 %.1fs 内收敛，继续执行本地清理",
                    len(pending),
                    timeout,
                )
        else:
            self._sidecar_tasks.clear()

    def _selected_avatar_driver(self):
        if self.avatar_orchestrator is not None:
            provider = self.avatar_orchestrator.get_last_selected_provider()
            if isinstance(provider, AvatarPreviewProvider):
                return provider
        if isinstance(self.sidecar_driver, AvatarPreviewProvider):
            return self.sidecar_driver
        return None

    def select_driver(
        self,
        mode: str = "B",
        avatar_path: str = "",
        landmarks_path: str = "",
        avatar_asset_dir: str = "",
    ) -> BaseMediaDriver:
        """根据当前直播模式与媒体依赖能力选择渲染驱动 (主播底图与人脸关键点同步下发)

        参数职责严格区分：
        - avatar_path: 主播**肖像照片**，用于底图与关键点定位
        - avatar_asset_dir: 主播**切片资产目录**(face_imgs/ + coords.pkl)，神经唇形必需

        保留边界（审计结论）：
        - MockMediaDriver 不产出任何视频帧，只有音频链路，用于无 GPU 联调。
          它不会生成"假唇形"，故保留，但**仅允许显式选择**。
        - 历史缺陷一：`or not CV_AVAILABLE` 会在缺少 numpy/opencv 时静默降级到
          mock，用户按 local/hybrid（要求真人画面）开播却"成功"得到无画面直播，
          属于谎报可用。改为如实失败。
        - 历史缺陷二：切片资产目录从未传入，神经渲染被静默旁路，唇形退化为
          mouth_open 模拟驱动（表现为"只会微笑"）。此处显式挂载。
        """
        if mode == "D":
            # 显式联调模式：仅音频链路，不产出数字人画面
            self.active_driver = global_mock_media_driver
            self.driver_type = "mock"
            logger.warning("已切换至轻量仿真媒体驱动 (MockMediaDriver)：仅音频链路，不产出数字人画面")
            return self.active_driver

        if not CV_AVAILABLE:
            raise RuntimeError(
                "未安装 numpy/opencv-python，无法执行真实数字人渲染。"
                "仿真媒体驱动只提供音频链路、不会产出画面，故不再静默降级（如需仅音频联调请显式使用 D 模式）。"
                "请执行: pip install numpy opencv-python"
            )

        self.active_driver = global_procedural_avatar_driver
        self.driver_type = "procedural_avatar"
        if avatar_path:
            global_procedural_avatar_driver.set_avatar(avatar_path, landmarks_path or None)
        # 神经唇形切片资产（与肖像底图分离）：缺失时如实上报，由 start() 前置校验拦截
        if avatar_asset_dir:
            global_procedural_avatar_driver.set_avatar_asset(avatar_asset_dir)
        else:
            logger.warning(
                "未提供主播切片资产目录 (avatar.avatar_asset_dir)，"
                "神经唇形无法工作；唇形必须由真实神经渲染产生，不提供模拟驱动回退"
            )
        logger.info(f"已切换至程序化数字人渲染驱动 (Procedural Avatar Renderer - 模式 {mode})")
        return self.active_driver

    @staticmethod
    def _supported_metadata(callable_obj, metadata: dict) -> dict:
        try:
            parameters = inspect.signature(callable_obj).parameters
        except (TypeError, ValueError):
            return {}
        if any(param.kind == inspect.Parameter.VAR_KEYWORD for param in parameters.values()):
            return metadata
        return {key: value for key, value in metadata.items() if key in parameters}

    async def feed_audio_chunk(self, audio_bytes: bytes, text_snippet: str, **metadata):
        self.is_speaking = True
        target = self.active_driver.feed_audio_chunk
        await target(
            audio_bytes,
            text_snippet,
            **self._supported_metadata(target, metadata),
        )

    def supports_progressive_audio(self) -> bool:
        """仅本地神经渲染驱动支持未知总时长的 PCM 增量提交。"""
        return bool(
            self.avatar_orchestrator is None
            and self.sidecar_driver is None
            and getattr(self.active_driver, "supports_progressive_audio", False)
        )

    async def feed_audio_frames(self, frames, *, is_final: bool = True) -> None:
        """转发标准 PCM 帧；支持同一句非零 sequence 的增量批次。"""
        candidate_frames = tuple(frames or ())
        if not candidate_frames:
            return
        progressive = (not is_final) or candidate_frames[0].sequence != 0
        frame_list = validate_audio_frame_batch(
            candidate_frames,
            require_complete=bool(is_final),
            allow_offset=progressive,
        )
        self.is_speaking = True
        batch_target = getattr(self.active_driver, "feed_audio_frames", None)
        if batch_target is not None:
            await batch_target(
                frame_list,
                **self._supported_metadata(batch_target, {"is_final": bool(is_final)}),
            )
        else:
            first = frame_list[0]
            target = self.active_driver.feed_audio_chunk
            metadata = first.metadata()
            await target(
                b"".join(frame.data for frame in frame_list),
                first.text,
                **self._supported_metadata(target, metadata),
            )

        # 增量本地路径不启动 strict_totals 的 Sidecar v3 整句事务；
        # 云端节点继续由原有完整事务路径处理，避免未知总时长破坏协议。
        if progressive:
            return

        # 编排器自身执行无排队并发保护；旧单 sidecar 继续沿用单任务兼容限制。
        if self.avatar_orchestrator is not None:
            self._track_sidecar_task(self._submit_sidecar_frames(frame_list))
        elif self.sidecar_driver is not None:
            active_tasks = [task for task in self._sidecar_tasks if not task.done()]
            if active_tasks:
                logger.warning("神经渲染 sidecar 忙，当前句保持本地渲染")
            else:
                self._track_sidecar_task(self._submit_sidecar_frames(frame_list))

    def get_media_capabilities(self) -> dict:
        """返回当前实际预览驱动的标准媒体契约。"""
        selected = self.active_driver
        selected_name = self.driver_type
        if self.remote_driver is not None and getattr(self.remote_driver, "has_frames", False):
            selected = self.remote_driver
            selected_name = "remote_gpu"
        avatar_driver = self._selected_avatar_driver()
        if avatar_driver is not None and getattr(avatar_driver, "has_frames", False):
            selected = avatar_driver
            selected_name = "neural_sidecar"
        getter = getattr(selected, "get_media_capabilities", None)
        capabilities = dict(getter() if getter is not None else super().get_media_capabilities())
        capabilities["router_accepts_audio_frames"] = True
        capabilities["progressive_audio"] = self.supports_progressive_audio()
        capabilities["selected_driver"] = selected_name
        capabilities["shadow_driver"] = self.driver_type
        return capabilities

    async def interrupt(self, reason: str = "Barge-in", **metadata):
        self.is_speaking = False
        # 打断必须先清空预取缓存：已废弃的旧画面绝不允许在后续播放中复活。
        if self._batch_prefetcher is not None:
            try:
                await self._batch_prefetcher.interrupt_and_clear()
            except Exception:
                logger.exception("前瞻预取缓存打断清理失败")
        target = self.active_driver.interrupt
        try:
            await target(reason, **self._supported_metadata(target, metadata))
        except Exception:
            logger.exception("本地 shadow renderer 打断失败")
        if self.avatar_orchestrator is not None:
            try:
                # 先让 render owner 独占读取 cancel ACK，再清理仍未收敛的后台 task。
                await asyncio.wait_for(
                    self.avatar_orchestrator.interrupt(
                        reason,
                        next_generation=metadata.get("next_generation"),
                    ),
                    timeout=2.5,
                )
            except Exception:
                logger.exception("远端 Avatar Provider 打断失败，已保留本地 generation fence")
        elif self.sidecar_driver is not None:
            try:
                sidecar_interrupt = self.sidecar_driver.interrupt
                await asyncio.wait_for(
                    sidecar_interrupt(
                        reason,
                        **self._supported_metadata(sidecar_interrupt, metadata),
                    ),
                    timeout=2.5,
                )
            except Exception:
                logger.exception("神经渲染 sidecar 打断失败，已保留本地 generation fence")
        await self._cancel_sidecar_tasks(timeout=0.5)

    async def start(self):
        await self.active_driver.start()
        self.is_running = True
        if self.avatar_orchestrator is not None:
            task = asyncio.create_task(self.avatar_orchestrator.start())
            self._orchestrator_start_task = task
            task.add_done_callback(self._orchestrator_started)
        elif self.sidecar_driver is not None:
            self._track_sidecar_task(self.sidecar_driver.start())

    async def stop(self):
        self.is_speaking = False
        self._detach_batch_prefetcher()
        try:
            start_task = self._orchestrator_start_task
            if start_task is not None and not start_task.done():
                start_task.cancel()
                await asyncio.gather(start_task, return_exceptions=True)
            if self.avatar_orchestrator is not None:
                try:
                    await asyncio.wait_for(self.avatar_orchestrator.stop(), timeout=4.0)
                except Exception:
                    logger.exception("远端 Avatar Provider 编排器停止失败")
            elif self.sidecar_driver is not None:
                try:
                    await asyncio.wait_for(self.sidecar_driver.stop(), timeout=2.5)
                except Exception:
                    logger.exception("神经渲染 sidecar 停止失败")
            await self._cancel_sidecar_tasks(timeout=0.5)
        finally:
            # 无论 sidecar 如何收敛，本地 renderer/设备停止都必须执行。
            try:
                await self.active_driver.stop()
            finally:
                self.is_running = False

    def _with_capability_boundary(self, status: dict) -> dict:
        media_contract = self.get_media_capabilities()
        # 共享单调时钟实况 (升级 heuristic_uniform -> shared_monotonic_pts)
        try:
            clock_status = global_shared_playback_clock.get_alignment_status()
        except Exception:
            clock_status = {
                "shared_playback_clock": False,
                "hardware_dac_clock": False,
                "clock_source": None,
                "clock_precision": "none",
                "alignment_mode": "heuristic_uniform",
            }
        # 帧发布总线实况：当前赢得租约的帧源 (云端/shadow)，用于诊断输出通道画面来源一致性
        try:
            from server.core.media.frame_bus import global_frame_bus
            frame_bus_status = global_frame_bus.get_status()
        except Exception:
            frame_bus_status = {}
        caps = {
            "procedural_avatar": status.get("render_backend") == "procedural",
            "neural_lipsync": bool(status.get("neural_lipsync", False)),
            "viseme_lipsync": status.get("viseme_lipsync", True if status.get("render_backend") == "procedural" else False),
            "g2p_aligned": status.get("g2p_aligned", False),
            "alignment_mode": clock_status.get("alignment_mode", "heuristic_uniform"),
            "shared_playback_clock": bool(clock_status.get("shared_playback_clock")),
            "hardware_dac_clock": bool(clock_status.get("hardware_dac_clock", False)),
            "clock_source": clock_status.get("clock_source"),
            "clock_precision": clock_status.get("clock_precision", "none"),
            "av_drift_ms": clock_status.get("drift_ms", 0.0),
            "local_preview": bool(status.get("is_running")),
            "virtual_camera": bool((status.get("virtual_cam") or {}).get("is_active")),
            "audio_frame_input": bool(media_contract.get("accepts_audio_frames")),
            "router_accepts_audio_frames": True,
        }
        if "capabilities" in status and isinstance(status["capabilities"], dict):
            caps.update(status["capabilities"])
        status["capabilities"] = caps
        status["media_contract"] = media_contract
        status["external_publish"] = {
            "status": "not_managed",
            "platform_live": None,
            "validation": "pending_external_acceptance",
        }
        if self.avatar_orchestrator is not None:
            provider_snapshot = self.avatar_orchestrator.get_snapshot()
        else:
            provider_snapshot = {
                "running": False,
                "strategy": "sentence_pinned_auto",
                "procedural_shadow": True,
                "same_sentence_failover": False,
                "provider_count": 0,
                "last_selected_provider_id": "",
                "requests_total": 0,
                "outcomes": {},
                "providers": [],
            }
        status["avatar_provider"] = {
            key: value
            for key, value in provider_snapshot.items()
            if key != "providers"
        }
        status["avatar_providers"] = provider_snapshot["providers"]
        status["frame_bus"] = frame_bus_status
        # 前瞻预取健康度：扩散模型批处理路径能否跟上实时直播的关键指标 (P0-1)
        prefetcher = self._batch_prefetcher
        orchestrator = self.avatar_orchestrator
        status["slice_prefetch"] = {
            "enabled": prefetcher is not None,
            "ready_slices": prefetcher.ready_count if prefetcher else 0,
            "rendering_slices": prefetcher.rendering_count if prefetcher else 0,
            "prefetch_hits": orchestrator.prefetch_hits if orchestrator else 0,
        }
        return status

    def get_preview_status(self) -> dict:
        avatar_driver = self._selected_avatar_driver()
        if avatar_driver is not None and getattr(avatar_driver, "has_frames", False):
            status = avatar_driver.get_preview_status()
            status["driver_type"] = "neural_sidecar"
            status["shadow_driver"] = self.driver_type
            status["virtual_cam"] = global_virtual_cam.get_status()
            status["cv_available"] = CV_AVAILABLE
            return self._with_capability_boundary(status)
        if self.remote_driver is not None and getattr(self.remote_driver, "has_frames", False):
            status = self.remote_driver.get_preview_status()
            status["driver_type"] = "remote_gpu"
            status["virtual_cam"] = global_virtual_cam.get_status()
            status["cv_available"] = CV_AVAILABLE
            return self._with_capability_boundary(status)
        if hasattr(self.active_driver, "get_preview_status"):
            status = self.active_driver.get_preview_status()
            status["driver_type"] = self.driver_type
            if avatar_driver is not None:
                status["neural_sidecar"] = avatar_driver.get_preview_status()
            return self._with_capability_boundary(status)
        return self._with_capability_boundary({
            "driver_type": self.driver_type,
            "is_speaking": self.is_speaking,
            "fps": 25,
            "cv_available": CV_AVAILABLE,
            "render_backend": "mock",
            "virtual_cam": global_virtual_cam.get_status()
        })

    def get_latest_jpeg(self) -> bytes:
        # v3 Provider 首帧就绪后优先；不可用/断帧时依次回退旧 Remote 与本地 shadow。
        avatar_driver = self._selected_avatar_driver()
        if avatar_driver is not None and getattr(avatar_driver, "has_frames", False):
            return avatar_driver.get_latest_jpeg()
        if self.remote_driver is not None and getattr(self.remote_driver, "has_frames", False):
            return self.remote_driver.get_latest_jpeg()
        if hasattr(self.active_driver, "get_latest_jpeg"):
            return self.active_driver.get_latest_jpeg()
        if hasattr(global_procedural_avatar_driver, "get_latest_jpeg"):
            return global_procedural_avatar_driver.get_latest_jpeg()
        return b""

    def set_render_fps(self, fps: int) -> int:
        """动态调节活动渲染驱动的目标帧率 (支持 CPU 过载自适应降频)"""
        clamped = max(10, min(60, int(fps)))
        if hasattr(self.active_driver, "set_target_fps"):
            return self.active_driver.set_target_fps(clamped)
        if hasattr(global_procedural_avatar_driver, "set_target_fps"):
            return global_procedural_avatar_driver.set_target_fps(clamped)
        return clamped

# 全局媒体中枢单例
global_media_router = MediaDriverRouter()
