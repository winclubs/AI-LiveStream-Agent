# -*- coding: utf-8 -*-
"""Offline GPU/environment and authorized Wav2Lip backend preflight."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import shutil
import sys
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from gpu_sidecar.backend import BackendUnavailableError, Wav2LipBackend  # noqa: E402
from gpu_sidecar.config import SidecarConfig  # noqa: E402
from gpu_sidecar.manifests import load_avatar_manifest, load_license_manifest  # noqa: E402

logger = logging.getLogger("LiveAgent.Wav2LipGPUPreflight")
SCHEMA_VERSION = "wav2lip-gpu-preflight/v1"


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()


def _check(name: str, status: str, detail: str = "", **data: Any) -> dict[str, Any]:
    result: dict[str, Any] = {"name": name, "status": status, "detail": detail}
    result.update(data)
    return result


def _environment() -> tuple[dict[str, Any], list[dict[str, Any]], bool]:
    environment: dict[str, Any] = {
        "python": {
            "version": sys.version.split()[0],
            "implementation": sys.implementation.name,
            "executable": sys.executable,
        },
        "nvidia_smi": {"available": False, "path": None},
        "torch": {"imported": False},
    }
    checks = [
        _check(
            "python_runtime",
            "PASS",
            f"Python {sys.version.split()[0]}",
            python_3_12=sys.version_info[:2] == (3, 12),
        )
    ]
    nvidia_smi = shutil.which("nvidia-smi")
    environment["nvidia_smi"] = {"available": nvidia_smi is not None, "path": nvidia_smi}
    checks.append(
        _check(
            "nvidia_smi_available",
            "PASS" if nvidia_smi else "UNKNOWN",
            nvidia_smi or "nvidia-smi not found on PATH; no NVML data is inferred",
        )
    )
    try:
        import torch
    except Exception as exc:
        environment["torch"]["import_error"] = str(exc)
        checks.append(_check("torch_import", "BLOCKED", str(exc)))
        return environment, checks, True

    torch_environment: dict[str, Any] = {
        "imported": True,
        "version": str(getattr(torch, "__version__", "unknown")),
        "cuda_available": False,
        "cuda_version": getattr(getattr(torch, "version", None), "cuda", None),
        "device_count": 0,
        "devices": [],
    }
    environment["torch"] = torch_environment
    checks.append(_check("torch_import", "PASS", f"torch {torch_environment['version']}"))
    try:
        cuda_available = torch.cuda.is_available() is True
        torch_environment["cuda_available"] = cuda_available
        device_count = int(torch.cuda.device_count()) if cuda_available else 0
        torch_environment["device_count"] = device_count
        if cuda_available:
            for index in range(device_count):
                properties = torch.cuda.get_device_properties(index)
                capability = torch.cuda.get_device_capability(index)
                torch_environment["devices"].append(
                    {
                        "index": index,
                        "name": str(torch.cuda.get_device_name(index)),
                        "compute_capability": ".".join(str(part) for part in capability),
                        "total_vram_bytes": int(properties.total_memory),
                    }
                )
    except Exception as exc:
        torch_environment["cuda_error"] = str(exc)
        checks.append(_check("cuda_gpu", "BLOCKED", str(exc)))
        return environment, checks, True
    blocked = not torch_environment["cuda_available"] or torch_environment["device_count"] < 1
    checks.append(
        _check(
            "cuda_gpu",
            "BLOCKED" if blocked else "PASS",
            "CUDA/GPU unavailable" if blocked else f"{torch_environment['device_count']} CUDA device(s)",
        )
    )
    return environment, checks, blocked


def _base_report() -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "status": "FAIL",
        "timestamp": _timestamp(),
        "checks": [],
        "environment": {},
        "descriptor": None,
        "resource_stats": None,
    }


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="检查 Python/torch/CUDA，或执行授权 manifest + backend warmup 全门禁；绝不下载或安装依赖",
    )
    parser.add_argument("--environment-only", action="store_true", help="只检查本机 Python、torch、CUDA/GPU 和 nvidia-smi")
    parser.add_argument("--config", help="sidecar JSON 配置")
    parser.add_argument("--license", help="人工批准的 license manifest JSON")
    parser.add_argument("--avatar", help="avatar manifest JSON")
    args = parser.parse_args(argv)
    supplied = (args.config, args.license, args.avatar)
    if args.environment_only and any(supplied):
        parser.error("--environment-only 不能与 --config/--license/--avatar 同时使用")
    if not args.environment_only and not all(supplied):
        parser.error("全门禁需要同时提供 --config、--license 和 --avatar")
    return args


async def _full_preflight(args: argparse.Namespace, report: dict[str, Any], environment_blocked: bool) -> int:
    backend: Wav2LipBackend | None = None
    try:
        config = SidecarConfig.load(args.config)
        report["checks"].append(_check("config", "PASS", "sidecar config validated"))
        license_manifest = load_license_manifest(args.license)
        report["checks"].append(
            _check(
                "license_manifest",
                "PASS",
                "license and implementation/weights digests validated",
                manifest_sha256=license_manifest.manifest_sha256,
                implementation_sha256=license_manifest.implementation_sha256,
                weights_sha256=license_manifest.weights_sha256,
            )
        )
        avatar_manifest = load_avatar_manifest(args.avatar)
        report["checks"].append(
            _check(
                "avatar_manifest",
                "PASS",
                "avatar source/assets digests validated",
                manifest_sha256=avatar_manifest.manifest_sha256,
                avatar_id=avatar_manifest.avatar_id,
                avatar_revision=avatar_manifest.revision,
            )
        )
    except Exception as exc:
        logger.error("配置或 manifest 门禁失败: %s", exc)
        report["checks"].append(_check("authorized_inputs", "FAIL", str(exc)))
        report["status"] = "FAIL"
        return 1

    if environment_blocked:
        report["checks"].append(_check("backend_prepare_warmup", "BLOCKED", "torch/CUDA/GPU unavailable"))
        report["status"] = "SKIPPED_BLOCKED"
        return 2

    backend = Wav2LipBackend(
        license_manifest_path=args.license,
        avatar_manifest_path=args.avatar,
        backend_id=config.backend_id,
        model_version=config.model_version,
        device=config.device,
        plugin_config=config.plugin_config,
    )
    try:
        descriptor = await backend.prepare()
        report["descriptor"] = asdict(descriptor)
        report["resource_stats"] = await asyncio.to_thread(backend.resource_snapshot)
        ready = bool(
            descriptor.available
            and descriptor.neural
            and descriptor.warmed
            and descriptor.license_approved
        )
        report["checks"].append(
            _check(
                "backend_prepare_warmup",
                "PASS" if ready else "FAIL",
                "authorized backend prepared and warmed" if ready else descriptor.unavailable_reason,
            )
        )
        report["status"] = "PASS" if ready else "FAIL"
        return 0 if ready else 1
    except BackendUnavailableError as exc:
        logger.error("backend prepare/warmup 失败: %s", exc)
        report["descriptor"] = asdict(backend.descriptor)
        report["resource_stats"] = await asyncio.to_thread(backend.resource_snapshot)
        report["checks"].append(_check("backend_prepare_warmup", "FAIL", str(exc)))
        report["status"] = "FAIL"
        return 1
    except Exception as exc:
        logger.error("backend preflight 异常: %s", exc)
        report["checks"].append(_check("backend_prepare_warmup", "FAIL", str(exc)))
        report["status"] = "FAIL"
        return 1
    finally:
        if backend is not None:
            try:
                await backend.close()
            except Exception as exc:
                logger.warning("backend close 失败: %s", exc)


async def _run(args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    report = _base_report()
    environment, checks, blocked = await asyncio.to_thread(_environment)
    report["environment"] = environment
    report["checks"].extend(checks)
    if args.environment_only:
        report["status"] = "SKIPPED_BLOCKED" if blocked else "PASS"
        return report, 2 if blocked else 0
    exit_code = await _full_preflight(args, report, blocked)
    return report, exit_code


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", stream=sys.stderr)
    args = _parse_args(argv)
    report = _base_report()
    exit_code = 1
    try:
        report, exit_code = asyncio.run(_run(args))
    except KeyboardInterrupt:
        report["status"] = "FAIL"
        report["checks"].append(_check("execution", "FAIL", "interrupted"))
        exit_code = 130
    except Exception as exc:
        logger.error("preflight 未处理异常: %s", exc)
        report["status"] = "FAIL"
        report["checks"].append(_check("execution", "FAIL", str(exc)))
    print(json.dumps(report, ensure_ascii=True, separators=(",", ":")))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
