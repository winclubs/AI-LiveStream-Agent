"""许可证与 avatar manifest 的严格加载和摘要校验。"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from pathlib import Path
from typing import Any, Mapping

from gpu_sidecar.contracts import AvatarAsset, AvatarManifest, LicenseManifest

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_MODULE_RE = re.compile(r"^[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*$")
_CALLABLE_RE = re.compile(r"^[A-Za-z_]\w*$")
_EXCLUDED_DIRECTORY_NAMES = {".git", "__pycache__"}
_MAX_MANIFEST_BYTES = 1024 * 1024


def _read_json_object(path: Path) -> tuple[dict[str, Any], str]:
    path = path.expanduser().resolve()
    if not path.is_file() or path.is_symlink():
        raise ValueError(f"manifest 必须是存在的普通文件: {path}")
    raw = path.read_bytes()
    if not raw or len(raw) > _MAX_MANIFEST_BYTES:
        raise ValueError(f"manifest 为空或超过 {_MAX_MANIFEST_BYTES} bytes: {path}")
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"manifest 不是合法 UTF-8 JSON: {path}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"manifest 顶层必须是 object: {path}")
    return value, hashlib.sha256(raw).hexdigest()


def _object(value: Any, field: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{field} 必须是 object")
    return value


def _text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} 必须是非空字符串")
    return value.strip()


def _sha256(value: Any, field: str) -> str:
    digest = _text(value, field).lower()
    if not _SHA256_RE.fullmatch(digest):
        raise ValueError(f"{field} 必须是 64 位小写 SHA-256")
    return digest


def _resolve_path(base: Path, value: Any, field: str) -> Path:
    raw = Path(_text(value, field)).expanduser()
    return (raw if raw.is_absolute() else base / raw).resolve()


def _hash_regular_file(path: Path, hasher: Any) -> None:
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            hasher.update(chunk)


def deterministic_directory_sha256(root: Path) -> str:
    """按相对 POSIX 路径排序，哈希路径长度、文件长度和内容，避免平台遍历顺序影响。"""
    root = root.expanduser().resolve()
    if not root.is_dir() or root.is_symlink():
        raise ValueError(f"实现路径必须是存在且非符号链接的目录: {root}")
    hasher = hashlib.sha256(b"gpu-sidecar-directory-sha256-v1\0")
    file_count = 0
    for current, directory_names, file_names in os.walk(root, topdown=True, followlinks=False):
        current_path = Path(current)
        kept_directories: list[str] = []
        for name in sorted(directory_names):
            candidate = current_path / name
            if name in _EXCLUDED_DIRECTORY_NAMES:
                continue
            if candidate.is_symlink():
                raise ValueError(f"实现目录不允许符号链接目录: {candidate}")
            kept_directories.append(name)
        directory_names[:] = kept_directories
        for name in sorted(file_names):
            candidate = current_path / name
            if candidate.suffix.lower() == ".pyc":
                continue
            if candidate.is_symlink():
                raise ValueError(f"实现目录不允许符号链接文件: {candidate}")
            mode = candidate.stat().st_mode
            if not stat.S_ISREG(mode):
                continue
            relative = candidate.relative_to(root).as_posix().encode("utf-8")
            size = candidate.stat().st_size
            hasher.update(len(relative).to_bytes(8, "big"))
            hasher.update(relative)
            hasher.update(size.to_bytes(8, "big"))
            _hash_regular_file(candidate, hasher)
            file_count += 1
    if file_count == 0:
        raise ValueError(f"实现目录不含可摘要的普通文件: {root}")
    return hasher.hexdigest()


def path_sha256(path: Path) -> str:
    """文件使用标准 SHA-256，目录使用 deterministic_directory_sha256。"""
    path = path.expanduser().resolve()
    if path.is_symlink():
        raise ValueError(f"摘要路径不允许符号链接: {path}")
    if path.is_dir():
        return deterministic_directory_sha256(path)
    if not path.is_file():
        raise ValueError(f"摘要路径不存在或不是普通文件: {path}")
    hasher = hashlib.sha256()
    _hash_regular_file(path, hasher)
    return hasher.hexdigest()


def _verify_digest(path: Path, expected: str, field: str) -> None:
    actual = path_sha256(path)
    if actual != expected:
        raise ValueError(f"{field} 摘要不匹配: expected={expected}, actual={actual}, path={path}")


def _reject_executable_ignored_bytecode(root: Path) -> None:
    """摘要仍跳过 pyc，但拒绝 Python 可直接导入的顶层 sourceless bytecode。"""
    for candidate in root.rglob("*.pyc"):
        relative_parts = candidate.relative_to(root).parts
        if not any(part in _EXCLUDED_DIRECTORY_NAMES for part in relative_parts):
            raise ValueError(f"实现目录不允许摘要外可执行 pyc: {candidate}")


def load_license_manifest(path: str | Path) -> LicenseManifest:
    """在任何 torch 或插件 import 前完成授权声明与实现/权重摘要门禁。"""
    manifest_path = Path(path).expanduser().resolve()
    data, manifest_sha256 = _read_json_object(manifest_path)
    if type(data.get("schema_version")) is not int or data["schema_version"] != 1:
        raise ValueError("license manifest.schema_version 必须为整数 1")
    if data.get("accepted") is not True:
        raise ValueError("license manifest 必须由人工审核后显式设置 accepted=true")
    implementation = _object(data.get("implementation"), "implementation")
    weights = _object(data.get("weights"), "weights")
    if implementation.get("human_approved") is not True:
        raise ValueError("implementation.human_approved 必须显式为 true")
    if weights.get("commercial_use_authorized") is not True:
        raise ValueError("weights.commercial_use_authorized 必须显式为 true")

    base = manifest_path.parent
    implementation_path = _resolve_path(base, implementation.get("path"), "implementation.path")
    weights_path = _resolve_path(base, weights.get("path"), "weights.path")
    implementation_sha256 = _sha256(implementation.get("sha256"), "implementation.sha256")
    weights_sha256 = _sha256(weights.get("sha256"), "weights.sha256")
    factory = _text(data.get("factory"), "factory")
    if factory.count(":") != 1:
        raise ValueError("factory 必须使用 module:callable 格式")
    factory_module, factory_callable = factory.split(":", 1)
    if not _MODULE_RE.fullmatch(factory_module) or not _CALLABLE_RE.fullmatch(factory_callable):
        raise ValueError("factory module:callable 含非法 Python 标识符")

    result = LicenseManifest(
        manifest_path=manifest_path,
        implementation_path=implementation_path,
        implementation_source=_text(implementation.get("source"), "implementation.source"),
        implementation_license=_text(implementation.get("license"), "implementation.license"),
        implementation_authorization_reference=_text(
            implementation.get("usage_authorization_reference"),
            "implementation.usage_authorization_reference",
        ),
        human_approval_reference=_text(
            implementation.get("human_approval_reference"),
            "implementation.human_approval_reference",
        ),
        implementation_sha256=implementation_sha256,
        weights_path=weights_path,
        weights_source=_text(weights.get("source"), "weights.source"),
        weights_license=_text(weights.get("license"), "weights.license"),
        commercial_authorization=_text(
            weights.get("commercial_authorization"),
            "weights.commercial_authorization",
        ),
        weights_sha256=weights_sha256,
        factory_module=factory_module,
        factory_callable=factory_callable,
        manifest_sha256=manifest_sha256,
    )
    if not implementation_path.is_dir():
        raise ValueError(f"implementation.path 必须是存在的目录: {implementation_path}")
    if not weights_path.is_file() or weights_path.is_symlink():
        raise ValueError(f"weights.path 必须是存在的普通文件: {weights_path}")
    _reject_executable_ignored_bytecode(implementation_path)
    _verify_digest(implementation_path, implementation_sha256, "implementation.sha256")
    _verify_digest(weights_path, weights_sha256, "weights.sha256")
    return result


def load_avatar_manifest(path: str | Path) -> AvatarManifest:
    """校验 avatar 原始来源及插件声明的每项资产，不接受隐式或缺失路径。"""
    manifest_path = Path(path).expanduser().resolve()
    data, manifest_sha256 = _read_json_object(manifest_path)
    if type(data.get("schema_version")) is not int or data["schema_version"] != 1:
        raise ValueError("avatar manifest.schema_version 必须为整数 1")
    base = manifest_path.parent
    source_path = _resolve_path(base, data.get("source_path"), "source_path")
    source_sha256 = _sha256(data.get("source_sha256"), "source_sha256")
    profile = _object(data.get("preprocessing_profile"), "preprocessing_profile")
    if not profile:
        raise ValueError("preprocessing_profile 不能为空")
    raw_assets = _object(data.get("assets"), "assets")
    if not raw_assets:
        raise ValueError("assets 至少需要一项插件资产")

    assets: list[AvatarAsset] = []
    for name in sorted(raw_assets):
        asset_data = _object(raw_assets[name], f"assets.{name}")
        asset_path = _resolve_path(base, asset_data.get("path"), f"assets.{name}.path")
        asset_sha256 = _sha256(asset_data.get("sha256"), f"assets.{name}.sha256")
        asset_config = asset_data.get("config", {})
        if not isinstance(asset_config, dict):
            raise ValueError(f"assets.{name}.config 必须是 object")
        _verify_digest(asset_path, asset_sha256, f"assets.{name}.sha256")
        assets.append(AvatarAsset(name=name, path=asset_path, sha256=asset_sha256, config=asset_config))

    _verify_digest(source_path, source_sha256, "source_sha256")
    plugin_config = data.get("plugin_config", {})
    if not isinstance(plugin_config, dict):
        raise ValueError("plugin_config 必须是 object")
    return AvatarManifest(
        manifest_path=manifest_path,
        avatar_id=_text(data.get("avatar_id"), "avatar_id"),
        revision=_text(data.get("revision"), "revision"),
        source_path=source_path,
        source_sha256=source_sha256,
        preprocessing_profile=profile,
        assets=tuple(assets),
        plugin_config=plugin_config,
        manifest_sha256=manifest_sha256,
    )
