"""经授权 Wav2Lip 兼容插件的延迟加载 backend。"""

from __future__ import annotations

import asyncio
import concurrent.futures
import importlib
import os
import shutil
import stat
import sys
import tempfile
import threading
import time
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Mapping

from gpu_sidecar.contracts import (
    AvatarAsset,
    AvatarManifest,
    BackendDescriptor,
    InferenceActiveMarker,
    LicenseManifest,
    PluginEngine,
    PluginFrameResult,
    PluginOOMError,
    PluginSession,
)
from gpu_sidecar.manifests import load_avatar_manifest, load_license_manifest, path_sha256

_SNAPSHOT_EXCLUDED_NAMES = {".git", "__pycache__"}
_BRIDGE_END = object()


class BackendUnavailableError(RuntimeError):
    """GPU、许可、模型或 warmup 未达到可用门禁。"""


class BackendOOMError(BackendUnavailableError):
    """CUDA OOM 会永久撤销当前进程的 neural ready 状态。"""


class BackendTimeoutError(BackendUnavailableError):
    """不可终止的插件调用超时后要求外部 supervisor 重启进程。"""


class BackendContractError(BackendUnavailableError):
    """插件违反已声明的严格 activity/cancellation 契约。"""


class _LateCleanupError(RuntimeError):
    """被撤销所有权的 worker 无法证明其 late result 已清理。"""


class _BridgeFailure:
    def __init__(self, error: BaseException) -> None:
        self.error = error


class _ProducerOutcome:
    def __init__(self) -> None:
        self.error: BaseException | None = None
        self.delivered = False


@dataclass(frozen=True, slots=True)
class _PreparedBackend:
    torch: Any
    engine: PluginEngine
    descriptor: BackendDescriptor


@dataclass(slots=True)
class _StreamReservation:
    quiesced: asyncio.Event
    started: bool = False
    cancelled: bool = False
    producer: asyncio.Future[None] | None = None


class Wav2LipBackend:
    """只桥接用户提供且已获授权的插件，不携带模型实现、预处理或权重。"""

    def __init__(
        self,
        *,
        license_manifest_path: str | Path,
        avatar_manifest_path: str | Path,
        backend_id: str,
        model_version: str,
        device: str,
        plugin_config: Mapping[str, Any] | None = None,
        model_call_timeout_seconds: float = 30.0,
        cancel_timeout_seconds: float = 2.0,
        bridge_queue_size: int = 8,
    ) -> None:
        self._license_manifest_path = Path(license_manifest_path)
        self._avatar_manifest_path = Path(avatar_manifest_path)
        self._device = device
        self._plugin_config = dict(plugin_config or {})
        self._model_call_timeout_seconds = model_call_timeout_seconds
        self._cancel_timeout_seconds = cancel_timeout_seconds
        self._bridge_queue_size = bridge_queue_size
        self._license: LicenseManifest | None = None
        self._avatar: AvatarManifest | None = None
        self._torch: Any = None
        self._engine: PluginEngine | None = None
        self._startup_resource_stats: dict[str, Any] | None = None
        self._model_lock = asyncio.Lock()
        self._snapshot_root: Path | None = None
        self._stream_reservations: dict[int, _StreamReservation] = {}
        self._background_reapers: set[asyncio.Task[None]] = set()
        self._restart_required = False
        self._closing = False
        self._closed = False
        self._descriptor = BackendDescriptor(backend_id=backend_id, model_version=model_version)

    @property
    def descriptor(self) -> BackendDescriptor:
        return self._descriptor

    @property
    def avatar(self) -> AvatarManifest:
        if self._avatar is None:
            raise BackendUnavailableError("avatar manifest 尚未通过校验")
        return self._avatar

    @property
    def startup_resource_stats(self) -> dict[str, Any] | None:
        return dict(self._startup_resource_stats) if self._startup_resource_stats is not None else None

    def resource_snapshot(self) -> dict[str, Any]:
        """Best-effort process/CUDA counters; diagnostics must never break rendering."""
        snapshot: dict[str, Any] = {
            "device": None,
            "cuda_available": None,
            "memory_allocated_bytes": None,
            "memory_reserved_bytes": None,
            "max_memory_allocated_bytes": None,
            "max_memory_reserved_bytes": None,
            "process_rss_bytes": None,
            "unavailable": {},
        }
        unavailable: dict[str, str] = snapshot["unavailable"]
        try:
            import psutil

            snapshot["process_rss_bytes"] = int(psutil.Process().memory_info().rss)
        except Exception as exc:
            unavailable["process_rss_bytes"] = f"psutil unavailable: {exc}"

        torch = self._torch
        torch_fields = (
            "device",
            "cuda_available",
            "memory_allocated_bytes",
            "memory_reserved_bytes",
            "max_memory_allocated_bytes",
            "max_memory_reserved_bytes",
        )
        if torch is None:
            reason = "torch not loaded"
            unavailable.update({field: reason for field in torch_fields})
            return snapshot
        try:
            cuda = torch.cuda
            snapshot["cuda_available"] = cuda.is_available() is True
        except Exception as exc:
            reason = f"torch CUDA status unavailable: {exc}"
            unavailable["cuda_available"] = reason
            unavailable.update({field: reason for field in torch_fields if field != "cuda_available"})
            return snapshot
        if snapshot["cuda_available"] is not True:
            reason = "torch CUDA unavailable"
            unavailable.update({field: reason for field in torch_fields if field != "cuda_available"})
            return snapshot

        device = self._descriptor.gpu_device or self._device
        snapshot["device"] = str(device)
        counters = {
            "memory_allocated_bytes": "memory_allocated",
            "memory_reserved_bytes": "memory_reserved",
            "max_memory_allocated_bytes": "max_memory_allocated",
            "max_memory_reserved_bytes": "max_memory_reserved",
        }
        for field, function_name in counters.items():
            try:
                snapshot[field] = int(getattr(cuda, function_name)(device))
            except Exception as exc:
                unavailable[field] = f"torch.cuda.{function_name} unavailable: {exc}"
        return snapshot

    def _reset_peak_stats_sync(self) -> None:
        torch = self._torch
        if torch is None:
            return
        try:
            if torch.cuda.is_available() is True:
                torch.cuda.reset_peak_memory_stats(self._device)
        except Exception:
            pass

    async def prepare(self) -> BackendDescriptor:
        """校验 manifest、建立快照并 warmup；工作线程不得直接发布 backend 状态。"""
        deadline = time.monotonic() + self._model_call_timeout_seconds
        acquired = False
        release_lock = True
        try:
            await asyncio.wait_for(
                self._model_lock.acquire(),
                timeout=self._model_call_timeout_seconds,
            )
            acquired = True
            if self._closed or self._closing:
                raise BackendUnavailableError("backend 正在关闭或已经关闭")
            if self._restart_required:
                raise BackendUnavailableError(self._descriptor.unavailable_reason)
            if self._descriptor.available:
                return self._descriptor

            authorized_license = load_license_manifest(self._license_manifest_path)
            authorized_avatar = load_avatar_manifest(self._avatar_manifest_path)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("verified snapshot exceeded hard deadline")
            snapshot_task = asyncio.create_task(
                asyncio.to_thread(
                    self._create_verified_snapshot,
                    authorized_license,
                    authorized_avatar,
                ),
                name="wav2lip-verified-snapshot",
            )
            try:
                snapshot_license, snapshot_avatar = await asyncio.wait_for(
                    asyncio.shield(snapshot_task),
                    timeout=remaining,
                )
            except TimeoutError as exc:
                self._mark_restart_required("verified snapshot exceeded hard deadline")
                release_lock = await self._settle_prepare_task(snapshot_task)
                raise BackendTimeoutError(self._descriptor.unavailable_reason) from exc
            except asyncio.CancelledError:
                self._mark_restart_required("verified snapshot caller cancelled while thread was running")
                release_lock = await self._settle_prepare_task(snapshot_task)
                raise

            self._license = snapshot_license
            self._avatar = snapshot_avatar
            if self._closed or self._closing or self._restart_required:
                raise BackendUnavailableError("backend 在 snapshot 完成前进入关闭或不可恢复状态")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("plugin prepare/warmup exceeded hard deadline")
            warmup_ownership = threading.Event()
            warmup_ownership.set()
            warmup_task = asyncio.create_task(
                asyncio.to_thread(
                    self._load_and_warmup_owned_sync,
                    snapshot_license,
                    snapshot_avatar,
                    warmup_ownership,
                ),
                name="wav2lip-plugin-prepare-warmup",
            )
            try:
                prepared = await asyncio.wait_for(asyncio.shield(warmup_task), timeout=remaining)
            except TimeoutError as exc:
                warmup_ownership.clear()
                self._mark_restart_required("plugin prepare/warmup exceeded hard deadline")
                release_lock = await self._settle_prepare_task(warmup_task, close_prepared=True)
                raise BackendTimeoutError(self._descriptor.unavailable_reason) from exc
            except asyncio.CancelledError:
                warmup_ownership.clear()
                self._mark_restart_required("plugin prepare/warmup caller cancelled while thread was running")
                release_lock = await self._settle_prepare_task(warmup_task, close_prepared=True)
                raise

            if self._closed or self._closing or self._restart_required:
                closed = await self._close_engine_bounded(prepared.engine)
                if not closed:
                    release_lock = False
                    self._mark_restart_required("late prepared engine did not close before hard deadline")
                    raise BackendTimeoutError(self._descriptor.unavailable_reason)
                raise BackendUnavailableError("backend 在 warmup 完成前进入关闭或不可恢复状态")

            self._torch = prepared.torch
            self._engine = prepared.engine
            self._descriptor = prepared.descriptor
            self._startup_resource_stats = self.resource_snapshot()
            return self._descriptor
        except asyncio.CancelledError:
            raise
        except BackendTimeoutError:
            raise
        except TimeoutError as exc:
            self._mark_restart_required("plugin prepare/warmup exceeded hard deadline")
            raise BackendTimeoutError(self._descriptor.unavailable_reason) from exc
        except Exception as exc:
            if not self._restart_required:
                self._descriptor = replace(
                    self._descriptor,
                    available=False,
                    neural=False,
                    warmed=False,
                    license_approved=False,
                    unavailable_reason=str(exc),
                )
            if self._is_cuda_oom(exc):
                raise BackendOOMError(f"CUDA OOM，backend 已标记 unavailable: {exc}") from exc
            if isinstance(exc, BackendUnavailableError):
                raise
            raise BackendUnavailableError(str(exc)) from exc
        finally:
            if acquired and release_lock:
                self._model_lock.release()

    async def _settle_prepare_task(
        self,
        task: asyncio.Task[Any],
        *,
        close_prepared: bool = False,
    ) -> bool:
        """有界回收真实 to_thread task；false 表示必须永久隔离 model lock。"""
        try:
            result = await asyncio.wait_for(
                asyncio.shield(task),
                timeout=self._cancel_timeout_seconds,
            )
        except TimeoutError:
            self._schedule_prepare_reaper(task, close_prepared=close_prepared)
            return False
        except asyncio.CancelledError:
            self._schedule_prepare_reaper(task, close_prepared=close_prepared)
            return False
        except _LateCleanupError:
            return False
        except Exception:
            return True
        if close_prepared:
            return await self._close_engine_bounded(result.engine)
        return True

    def _schedule_prepare_reaper(self, task: asyncio.Task[Any], *, close_prepared: bool) -> None:
        reaper = asyncio.create_task(
            self._reap_late_prepare(task, close_prepared=close_prepared),
            name="wav2lip-late-prepare-reaper",
        )
        self._background_reapers.add(reaper)
        reaper.add_done_callback(self._background_reapers.discard)

    async def _reap_late_prepare(self, task: asyncio.Task[Any], *, close_prepared: bool) -> None:
        """隔离锁后仍回收 late result；绝不恢复 ready 或清理 snapshot。"""
        try:
            result = await asyncio.shield(task)
        except BaseException:
            return
        if close_prepared:
            try:
                await asyncio.to_thread(result.engine.close)
            except BaseException:
                pass

    async def _close_engine_bounded(self, engine: PluginEngine) -> bool:
        close_task = asyncio.create_task(
            asyncio.to_thread(engine.close),
            name="wav2lip-plugin-engine-close",
        )
        try:
            await asyncio.wait_for(
                asyncio.shield(close_task),
                timeout=self._model_call_timeout_seconds,
            )
        except BaseException:
            return False
        return True

    def _create_verified_snapshot(
        self,
        license_manifest: LicenseManifest,
        avatar_manifest: AvatarManifest,
    ) -> tuple[LicenseManifest, AvatarManifest]:
        if self._snapshot_root is not None:
            raise RuntimeError("私有 snapshot 已存在")
        root = Path(tempfile.mkdtemp(prefix="ai-livestream-wav2lip-"))
        self._snapshot_root = root
        try:
            implementation_path = self._snapshot_path(
                root, "implementation", license_manifest.implementation_path, license_manifest.implementation_sha256
            )
            weights_path = self._snapshot_path(
                root, "weights", license_manifest.weights_path, license_manifest.weights_sha256
            )
            source_path = self._snapshot_path(
                root, "avatar-source", avatar_manifest.source_path, avatar_manifest.source_sha256
            )
            assets = tuple(
                replace(
                    asset,
                    path=self._snapshot_path(root, f"avatar-asset-{index}", asset.path, asset.sha256),
                )
                for index, asset in enumerate(avatar_manifest.assets)
            )
            snapshot_license = replace(
                license_manifest,
                implementation_path=implementation_path,
                weights_path=weights_path,
            )
            snapshot_avatar = replace(avatar_manifest, source_path=source_path, assets=assets)
            self._make_snapshot_read_only(root)
            self._verify_snapshot(snapshot_license, snapshot_avatar)
            return snapshot_license, snapshot_avatar
        except Exception:
            self._cleanup_snapshot_sync()
            raise

    def _snapshot_path(self, root: Path, category: str, source: Path, expected: str) -> Path:
        container = root / category / expected
        destination = container / ("content" if source.is_dir() else source.name)
        container.mkdir(parents=True, exist_ok=False)
        if source.is_dir():
            self._copy_directory_strict(source, destination)
        else:
            self._copy_file_strict(source, destination)
        actual = path_sha256(destination)
        if actual != expected:
            raise ValueError(
                f"snapshot 摘要不匹配: category={category}, expected={expected}, actual={actual}"
            )
        return destination

    @classmethod
    def _copy_directory_strict(cls, source: Path, destination: Path) -> None:
        if cls._is_link_like(source) or not source.is_dir():
            raise ValueError(f"snapshot 源目录非法: {source}")
        destination.mkdir()
        for current, directory_names, file_names in os.walk(source, topdown=True, followlinks=False):
            current_path = Path(current)
            relative = current_path.relative_to(source)
            target_current = destination / relative
            kept: list[str] = []
            for name in sorted(directory_names):
                candidate = current_path / name
                if name in _SNAPSHOT_EXCLUDED_NAMES:
                    continue
                if cls._is_link_like(candidate) or not stat.S_ISDIR(candidate.stat().st_mode):
                    raise ValueError(f"snapshot 禁止符号链接或特殊目录: {candidate}")
                (target_current / name).mkdir()
                kept.append(name)
            directory_names[:] = kept
            for name in sorted(file_names):
                candidate = current_path / name
                if candidate.suffix.lower() == ".pyc":
                    continue
                cls._copy_file_strict(candidate, target_current / name)

    @classmethod
    def _copy_file_strict(cls, source: Path, destination: Path) -> None:
        if cls._is_link_like(source) or not source.is_file() or not stat.S_ISREG(source.stat().st_mode):
            raise ValueError(f"snapshot 禁止符号链接或特殊文件: {source}")
        shutil.copyfile(source, destination, follow_symlinks=False)

    @staticmethod
    def _is_link_like(path: Path) -> bool:
        is_junction = getattr(path, "is_junction", None)
        return path.is_symlink() or (callable(is_junction) and is_junction())

    @staticmethod
    def _make_snapshot_read_only(root: Path) -> None:
        for current, directory_names, file_names in os.walk(root, topdown=False, followlinks=False):
            current_path = Path(current)
            for name in file_names:
                try:
                    (current_path / name).chmod(stat.S_IREAD)
                except OSError:
                    pass
            for name in directory_names:
                try:
                    (current_path / name).chmod(stat.S_IREAD | stat.S_IEXEC)
                except OSError:
                    pass
        try:
            root.chmod(stat.S_IREAD | stat.S_IEXEC)
        except OSError:
            pass

    @staticmethod
    def _verify_snapshot(license_manifest: LicenseManifest, avatar_manifest: AvatarManifest) -> None:
        checks = (
            (license_manifest.implementation_path, license_manifest.implementation_sha256, "implementation"),
            (license_manifest.weights_path, license_manifest.weights_sha256, "weights"),
            (avatar_manifest.source_path, avatar_manifest.source_sha256, "avatar source"),
            *((asset.path, asset.sha256, f"avatar asset {asset.name}") for asset in avatar_manifest.assets),
        )
        for path, expected, label in checks:
            actual = path_sha256(path)
            if actual != expected:
                raise ValueError(f"{label} snapshot 二次摘要不匹配")

    def _load_and_warmup_owned_sync(
        self,
        license_manifest: LicenseManifest,
        authorized_avatar: AvatarManifest,
        ownership: threading.Event,
    ) -> _PreparedBackend:
        prepared = self._load_and_warmup_sync(license_manifest, authorized_avatar)
        if ownership.is_set():
            return prepared
        try:
            prepared.engine.close()
        except Exception as exc:
            raise _LateCleanupError("late prepared engine 无法关闭") from exc
        raise BackendUnavailableError("late prepared engine 已关闭且未发布")

    def _load_and_warmup_sync(
        self,
        license_manifest: LicenseManifest,
        authorized_avatar: AvatarManifest,
    ) -> _PreparedBackend:
        """在线程内构造局部结果；只有事件循环持锁路径可以 commit。"""
        try:
            torch = importlib.import_module("torch")
        except ImportError as exc:
            raise RuntimeError("缺少独立 GPU 环境中的 torch；sidecar 不会自动安装依赖") from exc
        if torch.cuda.is_available() is not True:
            raise RuntimeError("torch.cuda.is_available() 为 false，拒绝声明 neural ready")
        try:
            cuda_device = torch.device(self._device)
        except Exception as exc:
            raise RuntimeError(f"CUDA device 非法: {self._device}") from exc
        if getattr(cuda_device, "type", None) != "cuda":
            raise RuntimeError(f"device 不是 CUDA device: {self._device}")
        torch.cuda.set_device(cuda_device)
        device_index = cuda_device.index
        if device_index is None:
            device_index = torch.cuda.current_device()
        gpu_name = str(torch.cuda.get_device_name(device_index))
        capability = torch.cuda.get_device_capability(device_index)
        capability_text = ".".join(str(part) for part in capability)

        module = self._import_factory_module(license_manifest)
        factory = getattr(module, license_manifest.factory_callable, None)
        if not callable(factory):
            raise RuntimeError(
                f"授权 factory 不可调用: {license_manifest.factory_module}:{license_manifest.factory_callable}"
            )
        engine: Any = None
        try:
            engine = factory(
                implementation_path=str(license_manifest.implementation_path),
                weights_path=str(license_manifest.weights_path),
                device=str(cuda_device),
                plugin_config=dict(self._plugin_config),
            )
            self._validate_engine(engine)
            try:
                torch.cuda.reset_peak_memory_stats(self._device)
            except Exception:
                pass
            engine.warmup()
        except Exception as exc:
            close = getattr(engine, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:
                    pass
            oom_types = tuple(
                candidate
                for candidate in (
                    getattr(torch, "OutOfMemoryError", None),
                    getattr(getattr(torch, "cuda", None), "OutOfMemoryError", None),
                )
                if isinstance(candidate, type)
            )
            if oom_types and isinstance(exc, oom_types):
                raise PluginOOMError(str(exc)) from exc
            raise

        descriptor = BackendDescriptor(
            backend_id=self._descriptor.backend_id,
            model_version=self._descriptor.model_version,
            available=True,
            neural=True,
            warmed=True,
            license_approved=True,
            implementation_sha256=license_manifest.implementation_sha256,
            weights_sha256=license_manifest.weights_sha256,
            license_manifest_sha256=license_manifest.manifest_sha256,
            avatar_id=authorized_avatar.avatar_id,
            avatar_revision=authorized_avatar.revision,
            avatar_digest=authorized_avatar.manifest_sha256,
            gpu_device=str(cuda_device),
            gpu_name=gpu_name,
            gpu_compute_capability=capability_text,
            unavailable_reason="",
        )
        return _PreparedBackend(torch=torch, engine=engine, descriptor=descriptor)

    def _import_factory_module(self, manifest: LicenseManifest) -> Any:
        implementation_path = manifest.implementation_path.resolve()
        importlib.invalidate_caches()
        sys.path.insert(0, str(implementation_path))
        try:
            module = importlib.import_module(manifest.factory_module)
        finally:
            try:
                sys.path.remove(str(implementation_path))
            except ValueError:
                pass
        module_file = getattr(module, "__file__", None)
        if not module_file:
            raise RuntimeError("授权 factory module 必须来自 implementation snapshot 内的普通 Python 文件")
        resolved_module = Path(module_file).resolve()
        if not resolved_module.is_relative_to(implementation_path):
            raise RuntimeError(f"factory module 越过已校验 implementation snapshot: {resolved_module}")
        if resolved_module.suffix.lower() != ".py" or resolved_module.is_symlink() or not resolved_module.is_file():
            raise RuntimeError("factory module 必须是 implementation snapshot 内普通 .py 文件")
        return module

    @staticmethod
    def _validate_engine(engine: Any) -> None:
        for method_name in ("warmup", "open_session", "close"):
            if not callable(getattr(engine, method_name, None)):
                raise RuntimeError(f"factory engine 缺少可调用方法: {method_name}")
        if getattr(engine, "cancel_threadsafe", None) is not True:
            raise RuntimeError("plugin engine.cancel_threadsafe 必须严格为 true")
        if getattr(engine, "cancel_quiesces", None) is not True:
            raise RuntimeError("plugin engine.cancel_quiesces 必须严格为 true")

    @staticmethod
    def _validate_session(session: Any) -> None:
        for method_name in ("push_audio", "finish", "cancel"):
            if not callable(getattr(session, method_name, None)):
                raise RuntimeError(f"plugin session 缺少可调用方法: {method_name}")
        if getattr(session, "cancel_threadsafe", None) is not True:
            raise RuntimeError("plugin session.cancel_threadsafe 必须严格为 true")
        if getattr(session, "cancel_quiesces", None) is not True:
            raise RuntimeError("plugin session.cancel_quiesces 必须严格为 true")
        if not isinstance(getattr(session, "inference_active", None), threading.Event):
            raise RuntimeError("plugin session.inference_active 必须是标准 threading.Event")

    def _require_engine(self) -> PluginEngine:
        if self._closed or self._closing:
            raise BackendUnavailableError("backend 正在关闭或已经关闭")
        if self._restart_required or not self._descriptor.available or self._engine is None:
            raise BackendUnavailableError(self._descriptor.unavailable_reason or "backend unavailable")
        return self._engine

    async def open_session(self, request: Mapping[str, Any]) -> PluginSession:
        deadline = time.monotonic() + self._model_call_timeout_seconds
        acquired = False
        release_lock = True
        open_task: asyncio.Task[Any] | None = None
        try:
            await asyncio.wait_for(
                self._model_lock.acquire(),
                timeout=self._model_call_timeout_seconds,
            )
            acquired = True
            engine = self._require_engine()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("plugin open_session exceeded hard deadline")
            open_ownership = threading.Event()
            open_ownership.set()
            open_task = asyncio.create_task(
                asyncio.to_thread(
                    self._open_session_owned_sync,
                    engine,
                    dict(request),
                    self.avatar.to_plugin_dict(),
                    open_ownership,
                ),
                name="wav2lip-plugin-open-session",
            )
            try:
                session = await asyncio.wait_for(asyncio.shield(open_task), timeout=remaining)
            except TimeoutError as exc:
                open_ownership.clear()
                release_lock = False
                self._mark_restart_required("plugin open_session exceeded hard deadline")
                release_lock = await self._settle_late_open(open_task)
                raise BackendTimeoutError(self._descriptor.unavailable_reason) from exc
            except asyncio.CancelledError:
                open_ownership.clear()
                release_lock = False
                self._mark_restart_required("plugin open_session caller cancelled while thread was running")
                release_lock = await self._settle_late_open(open_task)
                raise
            try:
                self._validate_session(session)
            except Exception:
                release_lock = False
                self._mark_restart_required("plugin session contract validation failed")
                release_lock = await self._cancel_opened_session(session)
                raise
            if self._closed or self._closing or self._restart_required:
                release_lock = False
                release_lock = await self._cancel_opened_session(session)
                if not release_lock:
                    self._mark_restart_required("session opened during shutdown did not quiesce")
                    raise BackendTimeoutError(self._descriptor.unavailable_reason)
                raise BackendUnavailableError("backend 在 open_session 完成前进入关闭或不可恢复状态")
            return session
        except asyncio.CancelledError:
            raise
        except BackendTimeoutError:
            raise
        except TimeoutError as exc:
            self._mark_restart_required("plugin model lock wait exceeded hard deadline")
            raise BackendTimeoutError(self._descriptor.unavailable_reason) from exc
        except Exception as exc:
            self._raise_model_error(exc)
            raise
        finally:
            if acquired and release_lock:
                self._model_lock.release()

    @staticmethod
    def _open_session_owned_sync(
        engine: PluginEngine,
        request: Mapping[str, Any],
        avatar: Mapping[str, Any],
        ownership: threading.Event,
    ) -> PluginSession:
        session = engine.open_session(request=request, avatar=avatar)
        if ownership.is_set():
            return session
        try:
            session.cancel()
        except Exception as exc:
            raise _LateCleanupError("late opened session 无法取消") from exc
        raise BackendUnavailableError("late opened session 已取消且未发布")

    async def _settle_late_open(self, open_task: asyncio.Task[Any]) -> bool:
        """Boundedly reap a real to_thread task; false leaves the model lock permanently held."""
        try:
            session = await asyncio.wait_for(
                asyncio.shield(open_task),
                timeout=self._cancel_timeout_seconds,
            )
        except TimeoutError:
            self._schedule_late_open_reaper(open_task)
            return False
        except asyncio.CancelledError:
            self._schedule_late_open_reaper(open_task)
            return False
        except _LateCleanupError:
            return False
        except Exception:
            return True
        return await self._cancel_opened_session(session)

    def _schedule_late_open_reaper(self, task: asyncio.Task[Any]) -> None:
        reaper = asyncio.create_task(
            self._reap_late_open(task),
            name="wav2lip-late-open-reaper",
        )
        self._background_reapers.add(reaper)
        reaper.add_done_callback(self._background_reapers.discard)

    async def _reap_late_open(self, task: asyncio.Task[Any]) -> None:
        try:
            session = await asyncio.shield(task)
        except BaseException:
            return
        await self._cancel_opened_session(session)

    async def _cancel_opened_session(self, session: Any) -> bool:
        cancel = getattr(session, "cancel", None)
        if not callable(cancel):
            return False
        cancel_task = asyncio.create_task(
            asyncio.to_thread(cancel),
            name="wav2lip-plugin-late-open-cancel",
        )
        try:
            await asyncio.wait_for(
                asyncio.shield(cancel_task),
                timeout=self._cancel_timeout_seconds,
            )
        except BaseException:
            return False
        return True

    def push_audio(
        self,
        session: PluginSession,
        *,
        pcm: bytes,
        metadata: Mapping[str, Any],
        max_results: int,
        max_bytes: int,
        cancel_check: Callable[[], bool],
    ) -> AsyncIterator[PluginFrameResult | Mapping[str, Any] | InferenceActiveMarker]:
        return self._reserve_stream_call(
            session,
            session.push_audio,
            {"pcm": pcm, "metadata": dict(metadata)},
            max_results,
            max_bytes,
            cancel_check,
        )

    def finish_session(
        self,
        session: PluginSession,
        *,
        max_results: int,
        max_bytes: int,
        cancel_check: Callable[[], bool],
    ) -> AsyncIterator[PluginFrameResult | Mapping[str, Any] | InferenceActiveMarker]:
        return self._reserve_stream_call(
            session,
            session.finish,
            {},
            max_results,
            max_bytes,
            cancel_check,
        )

    def _reserve_stream_call(
        self,
        session: PluginSession,
        function: Any,
        kwargs: Mapping[str, Any],
        max_results: int,
        max_bytes: int,
        cancel_check: Callable[[], bool],
    ) -> AsyncIterator[Any]:
        session_key = id(session)
        if session_key in self._stream_reservations:
            raise BackendContractError("同一 plugin session 不允许并发模型调用")
        reservation = _StreamReservation(quiesced=asyncio.Event())
        self._stream_reservations[session_key] = reservation
        return self._run_reserved_stream(
            session,
            function,
            kwargs,
            max_results,
            max_bytes,
            cancel_check,
            reservation,
        )

    async def _run_reserved_stream(
        self,
        session: PluginSession,
        function: Any,
        kwargs: Mapping[str, Any],
        max_results: int,
        max_bytes: int,
        cancel_check: Callable[[], bool],
        reservation: _StreamReservation,
    ) -> AsyncIterator[Any]:
        reservation.started = True
        try:
            if reservation.cancelled:
                return
            async for item in self._stream_call(
                session,
                function,
                kwargs,
                max_results,
                max_bytes,
                cancel_check,
                reservation,
            ):
                yield item
        finally:
            reservation.quiesced.set()
            if self._stream_reservations.get(id(session)) is reservation:
                self._stream_reservations.pop(id(session), None)

    async def _stream_call(
        self,
        session: PluginSession,
        function: Any,
        kwargs: Mapping[str, Any],
        max_results: int,
        max_bytes: int,
        cancel_check: Callable[[], bool],
        reservation: _StreamReservation,
    ) -> AsyncIterator[Any]:
        deadline = time.monotonic() + self._model_call_timeout_seconds
        try:
            await asyncio.wait_for(
                self._model_lock.acquire(),
                timeout=self._model_call_timeout_seconds,
            )
        except TimeoutError as exc:
            self._mark_restart_required("plugin model lock wait exceeded hard deadline")
            raise BackendTimeoutError(self._descriptor.unavailable_reason) from exc
        producer: asyncio.Future[None] | None = None
        outcome = _ProducerOutcome()
        release_lock = False
        reached_bridge_end = False
        stop = threading.Event()
        try:
            if reservation.cancelled or cancel_check():
                return
            self._require_engine()
            activity = session.inference_active
            if not isinstance(activity, threading.Event):
                raise RuntimeError("plugin session.inference_active 必须是标准 threading.Event")
            activity.clear()
            queue: asyncio.Queue[Any] = asyncio.Queue(maxsize=self._bridge_queue_size)
            loop = asyncio.get_running_loop()
            producer = loop.run_in_executor(
                None,
                self._produce_results,
                loop,
                queue,
                stop,
                deadline,
                activity,
                outcome,
                function,
                dict(kwargs),
                max_results,
                max_bytes,
                cancel_check,
            )
            reservation.producer = producer
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("plugin model call exceeded hard deadline")
                item = await asyncio.wait_for(queue.get(), timeout=remaining)
                if item is _BRIDGE_END:
                    reached_bridge_end = True
                    break
                if isinstance(item, _BridgeFailure):
                    outcome.delivered = True
                    raise item.error
                if cancel_check():
                    break
                yield item
        except TimeoutError as exc:
            self._mark_restart_required("plugin model call exceeded hard deadline")
            raise BackendTimeoutError(self._descriptor.unavailable_reason) from exc
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if isinstance(exc, BackendContractError):
                self._descriptor = replace(
                    self._descriptor,
                    available=False,
                    neural=False,
                    warmed=False,
                    license_approved=False,
                    unavailable_reason=str(exc),
                )
            self._raise_model_error(exc)
            raise
        finally:
            active_exception = sys.exc_info()[1]
            stop.set()
            if producer is None:
                release_lock = True
            elif reached_bridge_end:
                release_lock = await self._await_producer(producer, self._cancel_timeout_seconds)
            else:
                release_lock = await self._settle_stream_producer(
                    producer,
                    session,
                    external_cancel_expected=cancel_check(),
                )
            if release_lock:
                self._model_lock.release()
                if outcome.error is not None and not outcome.delivered:
                    surfaced = self._latch_producer_error(outcome.error)
                    if active_exception is None or isinstance(active_exception, GeneratorExit):
                        raise surfaced
                    active_exception.add_note(f"plugin producer also failed: {surfaced!r}")
            else:
                self._mark_restart_required(
                    "plugin executor thread did not terminate; asyncio threads are not killable"
                )
                raise BackendTimeoutError(self._descriptor.unavailable_reason)

    async def _settle_stream_producer(
        self,
        producer: asyncio.Future[None],
        session: PluginSession,
        *,
        external_cancel_expected: bool,
    ) -> bool:
        if producer.done():
            return await self._await_producer(producer, 0.001)
        if external_cancel_expected and await self._await_producer(producer, self._cancel_timeout_seconds):
            return True
        cancel_task = asyncio.create_task(
            asyncio.to_thread(session.cancel),
            name="wav2lip-plugin-stream-cancel",
        )
        try:
            await asyncio.wait_for(
                asyncio.shield(cancel_task),
                timeout=self._cancel_timeout_seconds,
            )
        except BaseException:
            return False
        return await self._await_producer(producer, self._model_call_timeout_seconds)

    @staticmethod
    async def _await_producer(producer: asyncio.Future[None], timeout: float) -> bool:
        try:
            await asyncio.wait_for(asyncio.shield(producer), timeout=max(0.001, timeout))
        except TimeoutError:
            return False
        except asyncio.CancelledError:
            return False
        return True

    def _latch_producer_error(self, error: BaseException) -> BaseException:
        if isinstance(error, BackendContractError):
            self._descriptor = replace(
                self._descriptor,
                available=False,
                neural=False,
                warmed=False,
                license_approved=False,
                unavailable_reason=str(error),
            )
        if isinstance(error, TimeoutError):
            self._mark_restart_required("plugin producer raised timeout")
            return BackendTimeoutError(self._descriptor.unavailable_reason)
        try:
            self._raise_model_error(error)
        except BackendOOMError as exc:
            return exc
        return error

    @classmethod
    def _produce_results(
        cls,
        loop: asyncio.AbstractEventLoop,
        queue: asyncio.Queue[Any],
        stop: threading.Event,
        deadline: float,
        activity: threading.Event,
        outcome: _ProducerOutcome,
        function: Any,
        kwargs: Mapping[str, Any],
        max_results: int,
        max_bytes: int,
        cancel_check: Callable[[], bool],
    ) -> None:
        results: Any = None
        failure: BaseException | None = None
        activity_observed = threading.Event()
        producer_done = threading.Event()
        watcher = threading.Thread(
            target=cls._watch_inference_activity,
            args=(loop, queue, stop, deadline, activity, activity_observed, producer_done),
            name="wav2lip-inference-activity-watcher",
        )
        watcher.start()
        try:
            results = function(**dict(kwargs))
            if results is None:
                raise RuntimeError("插件 push_audio/finish 必须返回可迭代结果，零帧请返回空 iterable")
            count = 0
            byte_count = 0
            for result in results:
                if stop.is_set() or cancel_check():
                    break
                jpeg = result.get("jpeg") if isinstance(result, Mapping) else getattr(result, "jpeg", None)
                if not isinstance(jpeg, bytes) or not jpeg:
                    raise RuntimeError("插件结果 jpeg 必须是非空 bytes")
                if count >= max_results:
                    raise RuntimeError("插件单事务结果帧数超过 config 上限")
                byte_count += len(jpeg)
                if byte_count > max_bytes:
                    raise RuntimeError("插件单事务结果字节数超过 config 上限")
                cls._bridge_put(loop, queue, result, stop, deadline)
                count += 1
        except BaseException as exc:
            failure = exc
        finally:
            close = getattr(results, "close", None)
            if callable(close):
                try:
                    close()
                except Exception as exc:
                    if failure is None:
                        failure = exc
            producer_done.set()
            watcher.join(timeout=max(0.0, deadline - time.monotonic()))
            if watcher.is_alive() and failure is None:
                failure = RuntimeError("inference activity watcher 未在期限内退出")
            if not activity_observed.is_set() and failure is None:
                failure = BackendContractError(
                    "plugin inference_active 从未 set；拒绝未证明进入可取消推理区域的调用"
                )
            if activity.is_set() and failure is None:
                failure = BackendContractError("plugin inference_active 在推理调用退出后仍为 set")
            outcome.error = failure
            if failure is not None:
                cls._bridge_put(loop, queue, _BridgeFailure(failure), stop, deadline, suppress=True)
            cls._bridge_put(loop, queue, _BRIDGE_END, stop, deadline, suppress=True)

    @classmethod
    def _watch_inference_activity(
        cls,
        loop: asyncio.AbstractEventLoop,
        queue: asyncio.Queue[Any],
        stop: threading.Event,
        deadline: float,
        activity: threading.Event,
        activity_observed: threading.Event,
        producer_done: threading.Event,
    ) -> None:
        while not stop.is_set():
            if activity.wait(timeout=0.005):
                activity_observed.set()
                cls._bridge_put(
                    loop,
                    queue,
                    InferenceActiveMarker(),
                    stop,
                    deadline,
                    suppress=True,
                )
                return
            if producer_done.is_set() or time.monotonic() >= deadline:
                return

    @staticmethod
    def _bridge_put(
        loop: asyncio.AbstractEventLoop,
        queue: asyncio.Queue[Any],
        item: Any,
        stop: threading.Event,
        deadline: float,
        *,
        suppress: bool = False,
    ) -> None:
        future = asyncio.run_coroutine_threadsafe(queue.put(item), loop)
        try:
            while True:
                if stop.is_set():
                    future.cancel()
                    return
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    future.cancel()
                    if suppress:
                        return
                    raise TimeoutError("plugin bridge blocked past hard deadline")
                try:
                    future.result(timeout=min(0.1, remaining))
                    return
                except concurrent.futures.TimeoutError:
                    continue
        except Exception:
            if not suppress:
                raise

    async def cancel_session(self, session: PluginSession) -> None:
        """仅在 cancel、producer、activity 与 model lock 均收敛后返回。"""
        deadline = time.monotonic() + self._cancel_timeout_seconds
        reservation = self._stream_reservations.get(id(session))
        if reservation is not None:
            reservation.cancelled = True
            if not reservation.started:
                reservation.quiesced.set()
                if self._stream_reservations.get(id(session)) is reservation:
                    self._stream_reservations.pop(id(session), None)
        cancel_task = asyncio.create_task(
            asyncio.to_thread(session.cancel),
            name="wav2lip-plugin-session-cancel",
        )
        try:
            await asyncio.wait_for(
                asyncio.shield(cancel_task),
                timeout=max(0.001, deadline - time.monotonic()),
            )
            if reservation is not None:
                producer = reservation.producer
                if producer is not None:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError("plugin producer did not quiesce before cancel deadline")
                    await asyncio.wait_for(asyncio.shield(producer), timeout=remaining)
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("plugin model lock did not quiesce before cancel deadline")
                await asyncio.wait_for(reservation.quiesced.wait(), timeout=remaining)
            if session.inference_active.is_set():
                raise BackendContractError("plugin cancel 返回后 inference_active 仍为 set")
        except asyncio.CancelledError:
            self._mark_restart_required("plugin cancel caller cancelled while thread was running")
            try:
                await asyncio.wait_for(
                    asyncio.shield(cancel_task),
                    timeout=self._cancel_timeout_seconds,
                )
            except BaseException:
                pass
            raise
        except TimeoutError as exc:
            self._mark_restart_required("plugin cancel did not quiesce before hard deadline")
            raise BackendTimeoutError(self._descriptor.unavailable_reason) from exc
        except BackendContractError as exc:
            self._mark_restart_required(str(exc))
            raise BackendTimeoutError(self._descriptor.unavailable_reason) from exc
        except Exception as exc:
            self._raise_model_error(exc)
            raise

    async def close(self) -> None:
        """终止 backend；未证明所有线程静止时保留 snapshot 并永久隔离锁。"""
        if self._closed:
            return
        self._closing = True
        acquired = False
        release_lock = True
        cleanup_allowed = False
        try:
            await asyncio.wait_for(
                self._model_lock.acquire(),
                timeout=self._cancel_timeout_seconds,
            )
            acquired = True
            engine = self._engine
            self._engine = None
            if engine is not None:
                engine_closed = await self._close_engine_bounded(engine)
                if not engine_closed:
                    release_lock = False
                    self._mark_restart_required("backend engine.close did not settle before hard deadline")
            cleanup_allowed = release_lock and not self._restart_required
        except TimeoutError:
            self._mark_restart_required("backend close exceeded hard deadline")
        except asyncio.CancelledError:
            self._mark_restart_required("backend close cancelled before quiescence was proven")
        finally:
            if acquired and release_lock:
                self._model_lock.release()
            if acquired and cleanup_allowed:
                try:
                    await asyncio.shield(asyncio.to_thread(self._cleanup_snapshot_sync))
                except BaseException:
                    self._mark_restart_required("snapshot cleanup was interrupted")
                else:
                    self._torch = None
                    self._license = None
                    self._avatar = None
                    self._startup_resource_stats = None
            self._closed = True
            self._descriptor = replace(
                self._descriptor,
                available=False,
                neural=False,
                warmed=False,
                license_approved=False,
                unavailable_reason=(
                    self._descriptor.unavailable_reason if self._restart_required else "backend closed"
                ),
            )

    def _cleanup_snapshot_sync(self) -> None:
        root = self._snapshot_root
        self._snapshot_root = None
        if root is None:
            return
        for current, directory_names, file_names in os.walk(root, topdown=False):
            current_path = Path(current)
            for name in file_names:
                try:
                    (current_path / name).chmod(stat.S_IWRITE | stat.S_IREAD)
                except OSError:
                    pass
            for name in directory_names:
                try:
                    (current_path / name).chmod(stat.S_IWRITE | stat.S_IREAD | stat.S_IEXEC)
                except OSError:
                    pass
        try:
            root.chmod(stat.S_IWRITE | stat.S_IREAD | stat.S_IEXEC)
        except OSError:
            pass
        shutil.rmtree(root, ignore_errors=True)

    def _mark_restart_required(self, reason: str) -> None:
        if self._restart_required:
            return
        self._restart_required = True
        self._descriptor = replace(
            self._descriptor,
            available=False,
            neural=False,
            warmed=False,
            license_approved=False,
            unavailable_reason=(
                f"{reason}; asyncio executor threads cannot be killed; "
                "external supervisor must force-terminate and restart process"
            ),
        )

    def _raise_model_error(self, exc: BaseException) -> None:
        if not self._is_cuda_oom(exc):
            return
        self._descriptor = replace(
            self._descriptor,
            available=False,
            neural=False,
            warmed=False,
            license_approved=False,
            unavailable_reason=(
                f"CUDA OOM: {exc}; external supervisor must force-terminate and restart process"
            ),
        )
        self._restart_required = True
        raise BackendOOMError(f"CUDA OOM，backend 已标记 unavailable: {exc}") from exc

    def _is_cuda_oom(self, exc: BaseException) -> bool:
        torch_oom_types: tuple[type[BaseException], ...] = ()
        if self._torch is not None:
            candidates = (
                getattr(self._torch, "OutOfMemoryError", None),
                getattr(getattr(self._torch, "cuda", None), "OutOfMemoryError", None),
            )
            torch_oom_types = tuple(candidate for candidate in candidates if isinstance(candidate, type))
        current: BaseException | None = exc
        seen: set[int] = set()
        while current is not None and id(current) not in seen:
            seen.add(id(current))
            if isinstance(current, PluginOOMError):
                return True
            if torch_oom_types and isinstance(current, torch_oom_types):
                return True
            current = current.__cause__ or current.__context__
        return False
