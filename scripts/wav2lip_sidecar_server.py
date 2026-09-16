# -*- coding: utf-8 -*-
"""经授权 Wav2Lip 兼容插件 sidecar 的薄启动入口。"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from gpu_sidecar.backend import Wav2LipBackend  # noqa: E402
from gpu_sidecar.config import SidecarConfig  # noqa: E402
from gpu_sidecar.server import serve  # noqa: E402

logger = logging.getLogger("LiveAgent.Wav2LipSidecarCLI")


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="加载用户提供且已获授权的 Wav2Lip 兼容插件；不会下载或内置源码/权重",
    )
    parser.add_argument("--config", required=True, help="sidecar JSON 配置")
    parser.add_argument("--license", required=True, help="人工批准的 license manifest JSON")
    parser.add_argument("--avatar", required=True, help="avatar manifest JSON")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8890)
    parser.add_argument("--token", default="")
    parser.add_argument("--tls-cert", default="", help="server certificate PEM; must pair with --tls-key")
    parser.add_argument("--tls-key", default="", help="server private key PEM; must pair with --tls-cert")
    parser.add_argument(
        "--trusted-proxy",
        action="store_true",
        help="mark loopback-only plaintext transport as TLS terminated by a trusted local reverse proxy",
    )
    return parser.parse_args(argv)


async def _run(args: argparse.Namespace) -> None:
    if not 1 <= args.port <= 65535:
        raise ValueError("port 必须位于 1~65535")
    if bool(args.tls_cert) != bool(args.tls_key):
        raise ValueError("--tls-cert/--tls-key 必须成对提供")
    config = SidecarConfig.load(args.config)
    backend = Wav2LipBackend(
        license_manifest_path=args.license,
        avatar_manifest_path=args.avatar,
        backend_id=config.backend_id,
        model_version=config.model_version,
        device=config.device,
        plugin_config=config.plugin_config,
        model_call_timeout_seconds=config.model_call_timeout_seconds,
        cancel_timeout_seconds=config.cancel_timeout_seconds,
        bridge_queue_size=config.bridge_queue_size,
    )
    try:
        descriptor = await backend.prepare()
        logger.info(
            "启动门禁通过: backend=%s model=%s weights_sha256=%s avatar=%s@%s gpu=%s",
            descriptor.backend_id,
            descriptor.model_version,
            descriptor.weights_sha256,
            descriptor.avatar_id,
            descriptor.avatar_revision,
            descriptor.gpu_name,
        )
        await serve(
            backend=backend,
            config=config,
            host=args.host,
            port=args.port,
            token=args.token,
            tls_cert=args.tls_cert,
            tls_key=args.tls_key,
            trusted_proxy=args.trusted_proxy,
        )
    finally:
        await backend.close()


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    args = _parse_args(argv)
    try:
        asyncio.run(_run(args))
    except KeyboardInterrupt:
        logger.info("sidecar 已停止")
        return 130
    except Exception as exc:
        logger.error("sidecar 启动失败，未进入 neural ready: %s", exc)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
