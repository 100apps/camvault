from __future__ import annotations

import asyncio
import hashlib
import json
import shutil
import socket
import tempfile
from pathlib import Path
from typing import Any

import httpx
import uvicorn

from camvault.config import (
    AppConfig,
    CameraConfig,
    RecordingConfig,
    ServerConfig,
    StorageConfig,
)
from camvault.ffmpeg import build_synthetic_command, inspect_ffmpeg
from camvault.service import CamVaultService
from camvault.web import create_app


class SelfTestError(RuntimeError):
    pass


async def run_self_test(
    *,
    ffmpeg_path: str = "ffmpeg",
    ffprobe_path: str = "ffprobe",
) -> dict[str, Any]:
    ffmpeg_info = inspect_ffmpeg(ffmpeg_path)
    ffprobe_executable = shutil.which(ffprobe_path) or ffprobe_path

    with tempfile.TemporaryDirectory(prefix="camvault-selftest-") as temp_dir:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("127.0.0.1", 0))
        sock.listen(128)
        sock.setblocking(False)
        port = int(sock.getsockname()[1])

        config = AppConfig(
            server=ServerConfig(
                host="127.0.0.1",
                port=port,
                playback_token="selftest-token",
                playback_token_env=None,
            ),
            storage=StorageConfig(
                root=Path(temp_dir) / "recordings",
                timezone="UTC",
                archive_chunk_seconds=4.0,
                max_buffer_mb_per_camera=16,
                retention_days=0,
                min_free_gb=0,
                retention_check_seconds=3600,
            ),
            recording=RecordingConfig(
                ffmpeg_path=ffmpeg_info.executable,
                ffprobe_path=ffprobe_executable,
                hls_segment_seconds=1.0,
                live_window_segments=8,
                max_live_memory_mb_per_camera=16,
                max_ingest_segment_mb=16,
                include_audio=False,
                audio_codec="none",
                ffmpeg_loglevel="error",
            ),
            cameras=[
                CameraConfig(
                    id="selftest",
                    name="Synthetic camera",
                    rtsp_url="rtsp://127.0.0.1/unused",
                    enabled=True,
                )
            ],
        )
        service = CamVaultService(config)
        app = create_app(service, manage_service=True, start_recorders=False)
        server = uvicorn.Server(
            uvicorn.Config(
                app,
                host="127.0.0.1",
                port=port,
                log_level="error",
                access_log=False,
                lifespan="on",
            )
        )
        server_task = asyncio.create_task(server.serve(sockets=[sock]))
        try:
            for _ in range(100):
                if server.started:
                    break
                if server_task.done():
                    raise SelfTestError("embedded test server stopped during startup")
                await asyncio.sleep(0.03)
            else:
                raise SelfTestError("embedded test server did not start")

            async def run_synthetic_stream(run_id: str, duration_seconds: float) -> None:
                command = build_synthetic_command(
                    camera_id="selftest",
                    recording=config.recording,
                    ingest_port=port,
                    ingest_secret=service.ingest_secret,
                    duration_seconds=duration_seconds,
                    run_id=run_id,
                )
                process = await asyncio.create_subprocess_exec(
                    *command,
                    stdin=asyncio.subprocess.DEVNULL,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.PIPE,
                )
                _stdout, stderr = await process.communicate()
                if process.returncode != 0:
                    raise SelfTestError(
                        f"synthetic FFmpeg pipeline {run_id!r} failed: "
                        + stderr.decode("utf-8", errors="replace").strip()
                    )

            # Two independent FFmpeg runs exercise the 7x24 restart boundary: the short
            # first tail must be sealed, and HLS must advertise a discontinuity.
            await run_synthetic_stream("selftest_a", 3.1)
            await run_synthetic_stream("selftest_b", 2.6)

            await asyncio.sleep(0.25)
            await service.flush_archives()

            async with httpx.AsyncClient(timeout=5) as client:
                unauthorized = await client.get(f"http://127.0.0.1:{port}/live/selftest/index.m3u8")
                if unauthorized.status_code != 401:
                    raise SelfTestError("playback endpoint did not enforce its token")

                live_response = await client.get(
                    f"http://127.0.0.1:{port}/live/selftest/index.m3u8",
                    params={"token": "selftest-token"},
                )
                if live_response.status_code != 200:
                    raise SelfTestError(
                        f"live playlist returned HTTP {live_response.status_code}: "
                        f"{live_response.text}"
                    )
                if "#EXTINF:" not in live_response.text:
                    raise SelfTestError("live playlist contains no media segments")
                if "#EXT-X-DISCONTINUITY" not in live_response.text:
                    raise SelfTestError("live playlist did not mark the FFmpeg restart")
                segment_name = next(
                    (
                        line.split("?", 1)[0]
                        for line in live_response.text.splitlines()
                        if line and not line.startswith("#")
                    ),
                    None,
                )
                if not segment_name:
                    raise SelfTestError("live playlist has no segment URI")
                live_segment = await client.get(
                    f"http://127.0.0.1:{port}/live/selftest/{segment_name}",
                    params={"token": "selftest-token"},
                )
                if live_segment.status_code != 200 or not live_segment.content:
                    raise SelfTestError("live segment could not be downloaded")

                vod_response = await client.get(
                    f"http://127.0.0.1:{port}/vod/selftest/index.m3u8",
                    params={"token": "selftest-token"},
                )
                if vod_response.status_code != 200 or "#EXT-X-ENDLIST" not in vod_response.text:
                    raise SelfTestError(f"VOD playlist failed with HTTP {vod_response.status_code}")
                if "#EXT-X-DISCONTINUITY" not in vod_response.text:
                    raise SelfTestError("VOD playlist did not preserve the restart boundary")
                recording_uri = next(
                    (
                        line
                        for line in vod_response.text.splitlines()
                        if line.startswith("/recordings/")
                    ),
                    None,
                )
                if not recording_uri:
                    raise SelfTestError("VOD playlist has no recording URI")
                recording_response = await client.get(f"http://127.0.0.1:{port}{recording_uri}")
                if recording_response.status_code != 200 or not recording_response.content:
                    raise SelfTestError("archived recording could not be downloaded")

                status_response = await client.get(
                    f"http://127.0.0.1:{port}/api/status",
                    params={"token": "selftest-token"},
                )
                status_response.raise_for_status()
                status_payload = status_response.json()

            records = await service.archive_records("selftest")
            if not records:
                raise SelfTestError("no archive file was produced")
            if not all(
                record.path is not None and record.path.stat().st_size > 0 for record in records
            ):
                raise SelfTestError("an archive file is empty")
            stream_ids = {record.stream_id for record in records}
            if not {"selftest_a", "selftest_b"}.issubset(stream_ids):
                raise SelfTestError("archives did not retain both FFmpeg stream IDs")
            first = records[0]
            if len(Path(first.relative_path).parts) != 5:
                raise SelfTestError("archive path is not partitioned as YYYY/MM/DD/HH/file")
            if first.path is None:
                raise SelfTestError("local self-test archive has no filesystem path")
            digest = hashlib.sha256(first.path.read_bytes()).hexdigest()
            if digest != first.sha256:
                raise SelfTestError("archive SHA-256 sidecar does not match media")

            probe = await asyncio.create_subprocess_exec(
                ffprobe_executable,
                "-v",
                "error",
                "-show_entries",
                "format=duration,format_name",
                "-show_entries",
                "stream=codec_name,codec_type,width,height",
                "-of",
                "json",
                str(records[0].path),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            probe_stdout, probe_stderr = await probe.communicate()
            if probe.returncode != 0:
                raise SelfTestError(
                    "ffprobe could not read the generated archive: "
                    + probe_stderr.decode("utf-8", errors="replace").strip()
                )
            probe_payload = json.loads(probe_stdout)

            camera_status = next(
                item for item in status_payload["cameras"] if item["id"] == "selftest"
            )
            return {
                "result": "PASS",
                "ffmpeg": ffmpeg_info.version_line,
                "live_segments": camera_status["segments_ingested"],
                "archive_files": len(records),
                "archive_bytes": sum(record.size_bytes for record in records),
                "first_archive_probe": probe_payload,
                "checks": [
                    "FastAPI loopback PUT ingest",
                    "bounded live HLS memory window",
                    "RAM aggregation to atomic MPEG-TS archive",
                    "token-protected live playlist and segment",
                    "VOD playlist and archive download",
                    "FFmpeg restart tail rotation and HLS discontinuity",
                    "YYYY/MM/DD/HH partitioning and SHA-256 metadata",
                    "ffprobe readability",
                ],
            }
        finally:
            server.should_exit = True
            await asyncio.gather(server_task, return_exceptions=True)
            sock.close()
