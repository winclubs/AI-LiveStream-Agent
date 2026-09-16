"""授权插件与 sidecar 之间的最小接口契约。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from threading import Event
from typing import Any, Iterable, Mapping, Protocol, runtime_checkable


@dataclass(frozen=True, slots=True)
class PluginFrameResult:
    """插件产出的单帧；PTS 使用输入音频的每声道 sample clock。"""

    jpeg: bytes
    pts_samples: int


@dataclass(frozen=True, slots=True)
class InferenceActiveMarker:
    """backend 仅在观察到插件 inference_active.set() 后产生的内部桥接标记。"""


@runtime_checkable
class PluginSession(Protocol):
    """插件 session 必须支持并发取消，并公开标准推理活跃事件。

    ``cancel()`` 返回前必须停止该 session 的全部结果生产并 clear
    ``inference_active``；backend 还会独立等待 producer 与模型锁收敛。
    """

    cancel_threadsafe: bool
    cancel_quiesces: bool
    inference_active: Event

    def push_audio(
        self,
        *,
        pcm: bytes,
        metadata: Mapping[str, Any],
    ) -> Iterable[PluginFrameResult | Mapping[str, Any]]: ...

    def finish(self) -> Iterable[PluginFrameResult | Mapping[str, Any]]: ...

    def cancel(self) -> None: ...


@runtime_checkable
class PluginEngine(Protocol):
    """factory 返回的进程级引擎接口；取消语义必须在创建 session 前声明。"""

    cancel_threadsafe: bool
    cancel_quiesces: bool

    def warmup(self) -> None: ...

    def open_session(
        self,
        *,
        request: Mapping[str, Any],
        avatar: Mapping[str, Any],
    ) -> PluginSession: ...

    def close(self) -> None: ...


@runtime_checkable
class PluginFactory(Protocol):
    """manifest 中 module:callable 必须实现的 keyword-only factory 契约。"""

    def __call__(
        self,
        *,
        implementation_path: str,
        weights_path: str,
        device: str,
        plugin_config: Mapping[str, Any],
    ) -> PluginEngine: ...


class PluginOOMError(RuntimeError):
    """插件包装 CUDA 内存不足时应保留 cause，或显式抛出此异常。"""


@dataclass(frozen=True, slots=True)
class LicenseManifest:
    manifest_path: Path
    implementation_path: Path
    implementation_source: str
    implementation_license: str
    implementation_authorization_reference: str
    human_approval_reference: str
    implementation_sha256: str
    weights_path: Path
    weights_source: str
    weights_license: str
    commercial_authorization: str
    weights_sha256: str
    factory_module: str
    factory_callable: str
    manifest_sha256: str


@dataclass(frozen=True, slots=True)
class AvatarAsset:
    name: str
    path: Path
    sha256: str
    config: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class AvatarManifest:
    manifest_path: Path
    avatar_id: str
    revision: str
    source_path: Path
    source_sha256: str
    preprocessing_profile: Mapping[str, Any]
    assets: tuple[AvatarAsset, ...]
    plugin_config: Mapping[str, Any]
    manifest_sha256: str

    def to_plugin_dict(self) -> dict[str, Any]:
        """只把已验证的绝对路径和声明配置交给授权插件。"""
        return {
            "avatar_id": self.avatar_id,
            "revision": self.revision,
            "source_path": str(self.source_path),
            "source_sha256": self.source_sha256,
            "preprocessing_profile": dict(self.preprocessing_profile),
            "assets": {
                asset.name: {
                    "path": str(asset.path),
                    "sha256": asset.sha256,
                    "config": dict(asset.config),
                }
                for asset in self.assets
            },
            "plugin_config": dict(self.plugin_config),
            "manifest_sha256": self.manifest_sha256,
        }


@dataclass(frozen=True, slots=True)
class BackendDescriptor:
    backend_id: str
    model_version: str
    available: bool = False
    neural: bool = False
    warmed: bool = False
    license_approved: bool = False
    implementation_sha256: str = ""
    weights_sha256: str = ""
    license_manifest_sha256: str = ""
    avatar_id: str = ""
    avatar_revision: str = ""
    avatar_digest: str = ""
    gpu_device: str = ""
    gpu_name: str = ""
    gpu_compute_capability: str = ""
    unavailable_reason: str = "not initialized"

    def evidence(self) -> dict[str, Any]:
        return {
            "backend_id": self.backend_id,
            "model_version": self.model_version,
            "implementation_sha256": self.implementation_sha256,
            "weights_sha256": self.weights_sha256,
            "license_manifest_sha256": self.license_manifest_sha256,
            "license_approved": self.license_approved,
            "avatar_id": self.avatar_id,
            "avatar_revision": self.avatar_revision,
            "avatar_digest": self.avatar_digest,
            "gpu_device": self.gpu_device,
            "gpu_name": self.gpu_name,
            "gpu_compute_capability": self.gpu_compute_capability,
        }
