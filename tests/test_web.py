from __future__ import annotations

import logging
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit

import httpx
import pytest

from camvault.archive import ArchiveRecord
from camvault.config import AppConfig, CameraConfig, RecordingConfig, ServerConfig, StorageConfig
from camvault.config_store import ConfigStore
from camvault.logging_setup import RecentLogHandler
from camvault.service import CamVaultService
from camvault.web import _anchor_download_records, _script_json, create_app


def test_download_uses_newest_overlapping_archive_as_seek_anchor() -> None:
    boundary = datetime(2026, 9, 4, 12, 1, tzinfo=UTC)
    previous = ArchiveRecord(
        camera_id="front",
        path=None,
        relative_path="previous.ts.enc",
        start=boundary - timedelta(seconds=60),
        end=boundary + timedelta(seconds=0.8),
        duration=60.8,
        size_bytes=100,
    )
    current = ArchiveRecord(
        camera_id="front",
        path=None,
        relative_path="current.ts.enc",
        start=boundary,
        end=boundary + timedelta(seconds=60),
        duration=60,
        size_bytes=100,
    )

    assert _anchor_download_records([previous, current], boundary) == [current]
    assert _anchor_download_records([previous, current], boundary - timedelta(seconds=1)) == [
        previous,
        current,
    ]


@pytest.mark.asyncio
async def test_ingest_auth_live_and_vod_playback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "camvault.web.build_download_command",
        lambda *_args, **_kwargs: [
            sys.executable,
            "-c",
            "import sys; sys.stdout.buffer.write(sys.stdin.buffer.read())",
        ],
    )
    config = AppConfig(
        server=ServerConfig(
            host="127.0.0.1",
            playback_token="play-token",
            playback_token_env=None,
        ),
        storage=StorageConfig(
            root=tmp_path,
            timezone="UTC",
            archive_chunk_seconds=4,
            max_buffer_mb_per_camera=8,
            retention_days=0,
            min_free_gb=0,
        ),
        recording=RecordingConfig(
            hls_segment_seconds=2,
            max_ingest_segment_mb=8,
            max_live_memory_mb_per_camera=4,
            include_audio=False,
            audio_codec="none",
        ),
        cameras=[CameraConfig(id="front", rtsp_url="rtsp://127.0.0.1/unused")],
    )
    service = CamVaultService(config)
    await service.start(start_supervisors=False)
    app = create_app(service, manage_service=False)
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 40000))
    headers = {"X-CamVault-Ingest": service.ingest_secret}
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            denied = await client.put("/_ingest/front/a.ts", content=b"x")
            assert denied.status_code == 403
            assert denied.headers["x-content-type-options"] == "nosniff"
            assert denied.headers["x-frame-options"] == "DENY"
            assert "default-src 'self'" in denied.headers["content-security-policy"]

            playlist_hint = "#EXTM3U\n#EXTINF:2.0,\na.ts\n#EXTINF:2.0,\nb.ts\n"
            assert (
                await client.put(
                    "/_ingest/front/index.m3u8", content=playlist_hint, headers=headers
                )
            ).status_code == 201
            service._on_audio_level("front", -20.0)
            assert (
                await client.put("/_ingest/front/a.ts", content=b"aaa", headers=headers)
            ).status_code == 201
            service._on_audio_level("front", -70.0)
            assert (
                await client.put("/_ingest/front/b.ts", content=b"bbb", headers=headers)
            ).status_code == 201

            assert (await client.get("/live/front/index.m3u8")).status_code == 401
            live = await client.get("/live/front/index.m3u8", params={"token": "play-token"})
            assert live.status_code == 200
            assert "a.ts?token=play-token" in live.text
            segment = await client.get("/live/front/a.ts", params={"token": "play-token"})
            assert segment.content == b"aaa"

            await service.flush_archives()
            vod = await client.get("/vod/front/index.m3u8", params={"token": "play-token"})
            assert vod.status_code == 200
            media_line = next(
                line for line in vod.text.splitlines() if line.startswith("/recordings/")
            )
            recording = await client.get(urlsplit(media_line).path, params={"token": "play-token"})
            assert recording.status_code == 200
            assert recording.content == b"aaabbb"

            records = await service.archive_records("front")
            seek_start = records[0].start + timedelta(seconds=1)
            seek_vod = await client.get(
                "/vod/front/index.m3u8",
                params={
                    "start": seek_start.isoformat(),
                    "end": records[0].end.isoformat(),
                    "token": "play-token",
                },
            )
            assert "#EXT-X-START:TIME-OFFSET=1.000,PRECISE=YES" in seek_vod.text

            download = await client.get(
                "/download/front",
                params={
                    "start": records[0].start.isoformat(),
                    "end": records[0].end.isoformat(),
                    "token": "play-token",
                },
            )
            assert download.status_code == 200
            assert download.content == b"aaabbb"
            assert download.headers["content-type"].startswith("video/mp4")
            assert download.headers["content-disposition"].endswith('.mp4"')

            oversized = await client.get(
                "/download/front",
                params={
                    "start": records[0].start.isoformat(),
                    "end": (records[0].start + timedelta(hours=25)).isoformat(),
                    "token": "play-token",
                },
            )
            assert oversized.status_code == 422

            status = await client.get("/api/status", params={"token": "play-token"})
            payload = status.json()
            assert payload["archive_buffer_bytes"] == 0
            assert payload["bounded_media_memory_bytes"] >= 6
            assert payload["write_bytes_last_minute"] == 6
            assert payload["storage"]["capacity"]["total_bytes"] > 0
            assert payload["archive_batching"]["adaptive"] is False
            assert payload["archive_batching"]["hard_max_bytes_per_camera"] == 8 * 1024 * 1024
            assert payload["archive_batching"]["cameras"]["front"]["target_seconds"] == 4

            records = await service.archive_records("front")
            timeline = await client.get(
                "/api/timeline",
                params={
                    "start": (records[0].start - timedelta(seconds=1)).isoformat(),
                    "end": (records[0].end + timedelta(seconds=1)).isoformat(),
                    "token": "play-token",
                },
            )
            assert timeline.status_code == 200
            assert timeline.json()["cameras"][0]["ranges"][0]["records"] == 1
            assert len(timeline.json()["cameras"][0]["sound_ranges"]) == 1
            assert timeline.json()["cameras"][0]["sound_ranges"][0]["level_db"] == -20.0
    finally:
        await service.stop()


def test_recording_path_blocks_traversal(tmp_path: Path) -> None:
    config = AppConfig(
        storage=StorageConfig(root=tmp_path, min_free_gb=0),
        recording=RecordingConfig(max_ingest_segment_mb=8),
        cameras=[CameraConfig(id="front", rtsp_url="rtsp://x")],
    )
    service = CamVaultService(config)
    with pytest.raises(ValueError, match="invalid recording path"):
        service.recording_path("front", "../../secret.ts")


def test_script_json_escapes_inline_script_delimiters() -> None:
    encoded = _script_json("</script>&<script>alert(1)</script>")
    assert "</script>" not in encoded
    assert "\\u003c/script\\u003e" in encoded
    assert "\\u0026" in encoded


@pytest.mark.asyncio
async def test_control_console_config_logs_and_manual_retention(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    original = """[server]
playback_token = "control-token"
playback_token_env = ""

[storage]
root = "./recordings"
retention_days = 0
min_free_gb = 0

[logging]
file = ""

[[cameras]]
id = "front"
rtsp_url = "rtsp://127.0.0.1/unused"
"""
    config_path.write_text(original, encoding="utf-8")
    from camvault.config import load_config

    config = load_config(config_path)
    recent = RecentLogHandler(100, secrets=("hidden-value",))
    recent.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
    recent.handle(
        logging.LogRecord("camvault.test", 20, __file__, 1, "token=hidden-value", (), None)
    )
    service = CamVaultService(config, config_path=config_path, recent_logs=recent)
    app = create_app(service, manage_service=False)
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 40000))
    headers = {"X-CamVault-Token": "control-token"}
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        assert (
            await client.get("/api/config", params={"token": "control-token"})
        ).status_code == 401
        loaded = await client.get("/api/config", headers=headers)
        assert loaded.status_code == 200
        revision = loaded.json()["revision"]

        invalid = await client.put(
            "/api/config",
            headers=headers,
            json={"content": "invalid = [", "revision": revision},
        )
        assert invalid.status_code == 422
        assert config_path.read_text(encoding="utf-8") == original

        updated = original.replace("retention_days = 0", "retention_days = 7")
        saved = await client.put(
            "/api/config",
            headers=headers,
            json={"content": updated, "revision": revision},
        )
        assert saved.status_code == 200
        assert saved.json()["restart_required"] is True
        assert ConfigStore(config_path).backup_path.read_text(encoding="utf-8") == original

        logs = await client.get("/api/logs", headers=headers)
        assert logs.status_code == 200
        assert "hidden-value" not in str(logs.json())

        cleanup = await client.post("/api/retention/run", headers=headers)
        assert cleanup.status_code == 200
        assert cleanup.json()["deleted_files"] == 0

        dashboard = await client.get("/", params={"token": "control-token"})
        assert dashboard.status_code == 200
        assert "CamVault · 监控中心" in dashboard.text
        assert "执行归档清理" in dashboard.text
        assert 'data-camera="front"' in dashboard.text
        assert 'id="video-front"' in dashboard.text
        assert "实时监控" in dashboard.text
        assert "历史回放" in dashboard.text
        assert "hls.js@1.7.2" in dashboard.text
        assert "/assets/dashboard.js?v=12" in dashboard.text
        assert 'id="playbackRate"' in dashboard.text
        assert 'id="nextSound"' in dashboard.text
        assert 'id="downloadCamera"' in dashboard.text
        assert 'id="downloadHistory"' in dashboard.text
        assert "原码直通" not in dashboard.text
        assert '"codecMode": "h264"' in dashboard.text


@pytest.mark.asyncio
async def test_browser_password_login_session_and_csrf(tmp_path: Path) -> None:
    config = AppConfig(
        server=ServerConfig(
            host="0.0.0.0",
            playback_token=None,
            playback_token_env=None,
            web_password="correct horse battery staple",
            web_password_env=None,
        ),
        storage=StorageConfig(root=tmp_path, min_free_gb=0),
        recording=RecordingConfig(max_ingest_segment_mb=8),
        cameras=[CameraConfig(id="front", rtsp_url="rtsp://127.0.0.1/unused")],
    )
    service = CamVaultService(config)
    app = create_app(service, manage_service=False)
    transport = httpx.ASGITransport(app=app, client=("192.168.1.20", 40000))
    async with httpx.AsyncClient(
        transport=transport, base_url="http://camvault", follow_redirects=False
    ) as client:
        redirect = await client.get("/")
        assert redirect.status_code == 303
        assert redirect.headers["location"].startswith("/login")

        wrong = await client.post("/login", data={"password": "wrong", "next": "/"})
        assert wrong.status_code == 401
        assert "camvault_session" not in wrong.cookies

        login = await client.post(
            "/login", data={"password": "correct horse battery staple", "next": "/"}
        )
        assert login.status_code == 303
        cookie = login.headers["set-cookie"]
        assert "HttpOnly" in cookie
        assert "SameSite=strict" in cookie

        dashboard = await client.get("/")
        assert dashboard.status_code == 200
        assert "监控中心" in dashboard.text
        assert (await client.get("/api/status")).status_code == 200
        assert (await client.get("/api/config")).status_code == 403
        assert (
            await client.get("/api/config", headers={"X-CamVault-CSRF": service.csrf_token})
        ).status_code == 409

        logout = await client.post("/logout", headers={"X-CamVault-CSRF": service.csrf_token})
        assert logout.status_code == 200
        assert (await client.get("/")).status_code == 303


@pytest.mark.asyncio
async def test_stream_restart_rotates_archive_and_marks_vod_discontinuity(
    tmp_path: Path,
) -> None:
    config = AppConfig(
        server=ServerConfig(host="127.0.0.1", playback_token="token", playback_token_env=None),
        storage=StorageConfig(
            root=tmp_path,
            timezone="UTC",
            archive_chunk_seconds=300,
            max_buffer_mb_per_camera=8,
            retention_days=0,
            min_free_gb=0,
        ),
        recording=RecordingConfig(
            hls_segment_seconds=2,
            max_ingest_segment_mb=8,
            max_live_memory_mb_per_camera=4,
            include_audio=False,
            audio_codec="none",
        ),
        cameras=[CameraConfig(id="front", rtsp_url="rtsp://127.0.0.1/unused")],
    )
    service = CamVaultService(config)
    await service.start(start_supervisors=False)
    app = create_app(service, manage_service=False)
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 40000))
    headers = {"X-CamVault-Ingest": service.ingest_secret}
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            names = [
                "segment_run1_20260904T120000_000000.ts",
                "segment_run2_20260904T120002_000000.ts",
            ]
            for name, payload in zip(names, (b"first", b"second"), strict=True):
                response = await client.put(
                    f"/_ingest/front/{name}", content=payload, headers=headers
                )
                assert response.status_code == 201
            await service.flush_archives()
            records = await service.archive_records("front")
            assert [record.stream_id for record in records] == ["run1", "run2"]

            live = await client.get("/live/front/index.m3u8", params={"token": "token"})
            assert "#EXT-X-DISCONTINUITY" in live.text
            vod = await client.get("/vod/front/index.m3u8", params={"token": "token"})
            assert vod.text.count("#EXT-X-DISCONTINUITY\n") == 1
    finally:
        await service.stop()
