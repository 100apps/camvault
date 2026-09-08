from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import signal
import socket
import sys
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import ClassVar
from urllib.parse import unquote

import pytest
import uvicorn
from test_webdav_socket import _SocketWebDAVHandler

from camvault.archive import ArchiveBatch, ArchiveRecord
from camvault.config import AppConfig, CameraConfig, RecordingConfig, ServerConfig, StorageConfig
from camvault.crypto import (
    ARCHIVE_ENCRYPTION_HEADER_BYTES,
    decrypt_archive_chunk,
    parse_encryption_header,
)
from camvault.ffmpeg import build_synthetic_command
from camvault.server import CamVaultServer
from camvault.service import CamVaultService
from camvault.web import create_app


@pytest.mark.skipif(os.name == "nt", reason="POSIX process signals")
@pytest.mark.parametrize("stop_signal", [signal.SIGTERM, signal.SIGINT])
@pytest.mark.asyncio
async def test_signal_commits_encrypted_ram_and_final_http_segment(
    tmp_path: Path, stop_signal: signal.Signals
) -> None:
    class Handler(_SocketWebDAVHandler):
        directories: ClassVar[set[str]] = {"/dav"}
        files: ClassVar[dict[str, bytes]] = {}
        failures = 0

        def do_PUT(self) -> None:
            if type(self).failures == 0:
                type(self).failures += 1
                self.rfile.read(int(self.headers["Content-Length"]))
                self._reply(503)
                return
            super().do_PUT()

    dav = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=dav.serve_forever, daemon=True)
    thread.start()
    env = os.environ.copy()
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
    process = None
    try:
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            str(Path(__file__).parent / "helpers" / "shutdown_server.py"),
            str(dav.server_address[1]),
            str(tmp_path / "unused-local-recordings"),
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        assert await asyncio.wait_for(process.stdout.readline(), timeout=10) == b"READY\n"
        assert not Handler.files
        process.send_signal(stop_signal)
        _stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=12)
        assert process.returncode in (0, -int(stop_signal)), stderr.decode()
        assert "shutdown drain complete" in stderr.decode()
        assert "shutdown incomplete" not in stderr.decode()
        assert Handler.failures == 1
        assert len(Handler.files) == 2  # one video + its audio-index sidecar
        assert not any("partial" in path for path in Handler.files)
        decoded = {}
        for path, payload in Handler.files.items():
            header = parse_encryption_header(payload[:ARCHIVE_ENCRYPTION_HEADER_BYTES])
            decoded[path] = decrypt_archive_chunk(
                payload[ARCHIVE_ENCRYPTION_HEADER_BYTES:],
                header=header,
                index=0,
                key=b"s" * 32,
                context=unquote(path).removeprefix("/dav/CamVault/"),
            )
        video = next(data for path, data in decoded.items() if path.endswith(".ts.enc"))
        assert video == b"initial-in-ramfinal-on-stop"
        metadata = json.loads(
            next(data for path, data in decoded.items() if path.endswith(".json.enc"))
        )
        assert metadata["segment_count"] == 2
        assert metadata["audio_index"][-1]["active"] is True
        assert not (tmp_path / "unused-local-recordings").exists()
        assert (tmp_path / "spool" / ".target.json").exists()
        assert not [path for path in (tmp_path / "spool").iterdir() if path.is_dir()]
    finally:
        if process is not None and process.returncode is None:
            process.kill()
            await process.communicate()
        await asyncio.to_thread(dav.shutdown)
        dav.server_close()
        thread.join(timeout=5)


@pytest.mark.asyncio
@pytest.mark.parametrize("hang_writer", [False, True])
async def test_shutdown_deadline_reports_uncommitted_bytes_and_releases_workers(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, hang_writer: bool
) -> None:
    config = AppConfig(
        server=ServerConfig(playback_token_env=None, web_password_env=None),
        storage=StorageConfig(root=tmp_path, archive_chunk_seconds=1800, min_free_gb=0),
        recording=RecordingConfig(max_ingest_segment_mb=8),
        cameras=[CameraConfig(id="front", rtsp_url="rtsp://127.0.0.1/unused")],
    )
    # Short virtual deployment deadline keeps this failure-path test inexpensive.
    config.server.shutdown_timeout_seconds = 0.05
    service = CamVaultService(config)

    async def unavailable(_batch: ArchiveBatch) -> ArchiveRecord:
        if hang_writer:
            await asyncio.Event().wait()
        raise OSError("injected offline storage")

    service.archive_manager.writer = unavailable
    await service.start(start_supervisors=False)
    await service.ingest_upload("front", "first.ts", b"uncommitted")
    with caplog.at_level(logging.INFO):
        await asyncio.wait_for(service.stop(), timeout=1)
    assert service.archive_manager.unwritten_bytes == len(b"uncommitted")
    assert service.archive_manager.memory_bytes() == 0
    assert not service.archive_manager.workers
    assert "shutdown incomplete" in caplog.text
    assert "shutdown drain complete" not in caplog.text
    await service.stop()  # lifespan / repeated stop must not start a second upload
    assert service.archive_manager.unwritten_bytes == len(b"uncommitted")


@pytest.mark.asyncio
async def test_ffmpeg_quit_preserves_last_http_segment(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    executable = os.environ.get("CAMVAULT_TEST_FFMPEG") or shutil.which("ffmpeg")
    if not executable:
        pytest.skip("set CAMVAULT_TEST_FFMPEG or install FFmpeg for real quit/HTTP test")
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen(128)
    sock.setblocking(False)
    port = sock.getsockname()[1]
    config = AppConfig(
        server=ServerConfig(port=port, playback_token_env=None, web_password_env=None),
        storage=StorageConfig(root=tmp_path, archive_chunk_seconds=1800, min_free_gb=0),
        recording=RecordingConfig(
            ffmpeg_path=executable, hls_segment_seconds=1, max_ingest_segment_mb=8
        ),
        cameras=[CameraConfig(id="front", rtsp_url="rtsp://127.0.0.1/unused")],
    )
    service = CamVaultService(config)
    app = create_app(service, start_recorders=False)
    server = CamVaultServer(uvicorn.Config(app, log_level="error"), service)
    task = asyncio.create_task(server.serve(sockets=[sock]))
    process = None
    try:
        async with asyncio.timeout(10):
            while not server.started:
                if task.done():
                    await task
                await asyncio.sleep(0.01)
        command = build_synthetic_command(
            camera_id="front",
            recording=config.recording,
            ingest_port=port,
            ingest_secret=service.ingest_secret,
            duration_seconds=30,
        )
        command[command.index("-nostdin")] = "-stdin"
        process = await asyncio.create_subprocess_exec(
            *command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        supervisor = service.supervisors.supervisors["front"]
        supervisor._process = process
        service._supervisors_started = True
        async with asyncio.timeout(10):
            while service.runtimes["front"].segments_ingested < 2:
                await asyncio.sleep(0.02)
        await asyncio.sleep(0.25)  # quit with an incomplete current HLS segment
        segments_before_stop = service.runtimes["front"].segments_ingested
        with caplog.at_level(logging.INFO, logger="camvault.supervisor"):
            # Simulate the watchdog and service requesting termination together.
            competing_stop = asyncio.create_task(supervisor._terminate(process))
            server.should_exit = True
            await asyncio.wait_for(asyncio.gather(task, competing_stop), timeout=10)
        _stdout, stderr = await process.communicate()
        assert process.returncode == 0, stderr.decode()
        assert b"Failed to open file" not in stderr
        assert service.runtimes["front"].segments_ingested > segments_before_stop
        assert caplog.text.count("FFmpeg flushed and exited") == 1
        assert service.archive_manager.memory_bytes() == 0
        assert service.archive_manager.unwritten_bytes == 0
    finally:
        if process is not None and process.returncode is None:
            process.kill()
            await process.communicate()
        server.should_exit = True
        await asyncio.wait_for(task, timeout=10)
