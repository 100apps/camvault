"""Isolated signal-test process; only the loopback fixture's WebDAV is reachable."""

from __future__ import annotations

import asyncio
import base64
import logging
import os
import socket
import sys
from pathlib import Path

import httpx
import uvicorn

from camvault.config import (
    AppConfig,
    CameraConfig,
    RecordingConfig,
    ServerConfig,
    StorageConfig,
    WebDAVConfig,
)
from camvault.server import CamVaultServer
from camvault.service import CamVaultService
from camvault.web import create_app


async def main() -> None:
    logging.basicConfig(level=logging.INFO)
    os.environ["CAMVAULT_TEST_SHUTDOWN_KEY"] = base64.b64encode(b"s" * 32).decode()
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen(128)
    sock.setblocking(False)
    port = sock.getsockname()[1]
    config = AppConfig(
        server=ServerConfig(
            port=port,
            playback_token_env=None,
            web_password_env=None,
            shutdown_timeout_seconds=5,
        ),
        storage=StorageConfig(
            backend="webdav",
            root=Path(sys.argv[2]),
            archive_chunk_seconds=1800,
            adaptive_archive_enabled=True,
            max_buffer_mb_per_camera=64,
            min_free_gb=0,
            webdav=WebDAVConfig(
                url=f"http://127.0.0.1:{int(sys.argv[1])}/dav",
                username="camvault",
                password="secret",
                username_env=None,
                password_env=None,
                encryption_enabled=True,
                encryption_key_env="CAMVAULT_TEST_SHUTDOWN_KEY",
            ),
        ),
        recording=RecordingConfig(max_ingest_segment_mb=8),
        cameras=[CameraConfig(id="front", rtsp_url="rtsp://127.0.0.1/unused")],
    )
    service = CamVaultService(config)

    async def final_segment_on_stop() -> None:
        # Reproduce FFmpeg's last PUT emitted only after receiving its stop signal.
        service._on_audio_level("front", -12.0)
        async with httpx.AsyncClient(trust_env=False) as client:
            response = await client.put(
                f"http://127.0.0.1:{port}/_ingest/front/final.ts",
                headers={"X-CamVault-Ingest": service.ingest_secret},
                content=b"final-on-stop",
            )
            response.raise_for_status()

    service.supervisors.stop = final_segment_on_stop
    app = create_app(service, start_recorders=False)
    server = CamVaultServer(
        uvicorn.Config(app, log_config=None, timeout_graceful_shutdown=1), service
    )
    task = asyncio.create_task(server.serve(sockets=[sock]))
    while not server.started:
        if task.done():
            await task
            raise RuntimeError("test server failed to start")
        await asyncio.sleep(0.01)
    service._supervisors_started = True
    await service.ingest_upload("front", "first.ts", b"initial-in-ram")
    assert service.archive_manager.memory_bytes() == len(b"initial-in-ram")
    print("READY", flush=True)
    await task


if __name__ == "__main__":
    asyncio.run(main())
