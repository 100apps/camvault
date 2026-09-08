from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from test_webdav_storage import MemoryWebDAV, _encrypted_storage, _segment

from camvault.archive import ArchiveBatch
from camvault.config import (
    AppConfig,
    CameraConfig,
    RecordingConfig,
    ServerConfig,
    parse_config_text,
)
from camvault.crypto import ARCHIVE_ENCRYPTION_MAGIC
from camvault.service import CamVaultService
from camvault.spool import SpoolingStorageBackend, SpoolWriteError
from camvault.storage import create_storage_backend


def _batch(sequence: int, payload: bytes = b"private-video") -> ArchiveBatch:
    segment = _segment(
        sequence, payload, datetime(2026, 9, 8, tzinfo=UTC) + timedelta(seconds=sequence * 2)
    )
    segment.audio_rms_db = -12
    segment.audio_active = True
    return ArchiveBatch(camera_id="front", segments=(segment,))


async def _wait_until(predicate) -> None:
    async with asyncio.timeout(3):
        while not predicate():
            await asyncio.sleep(0.01)


def test_old_config_gets_persistent_outbox_beside_config_without_options(tmp_path: Path) -> None:
    config = parse_config_text(
        '[storage]\nbackend="webdav"\n[[cameras]]\nid="front"\nrtsp_url="rtsp://camera/stream"',
        base_dir=tmp_path,
        validate_runtime=False,
    )
    assert config.storage.spool_directory == tmp_path / "spool"
    assert isinstance(create_storage_backend(config.storage, ["front"]), SpoolingStorageBackend)


@pytest.mark.asyncio
async def test_storage_check_does_not_take_over_running_outbox(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    storage = _encrypted_storage(tmp_path, monkeypatch)
    client = httpx.AsyncClient(transport=httpx.MockTransport(MemoryWebDAV()))
    backend = create_storage_backend(storage, ["front"], client=client)
    diagnostic = create_storage_backend(storage, ["front"], client=client)
    try:
        await backend.start()
        health = await diagnostic.health_check()
        assert health.diskless_media_path is False
        assert diagnostic._owner is None and diagnostic._worker is None
        assert backend._owner is not None
    finally:
        await diagnostic.close()
        await backend.close()
        await client.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["reserve", "stat", "write"])
async def test_local_io_failure_is_retriable_without_deleting_cloud_history(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    async def offline(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("offline", request=request)

    storage = _encrypted_storage(tmp_path, monkeypatch)
    client = httpx.AsyncClient(transport=httpx.MockTransport(offline))
    backend = create_storage_backend(storage, ["front"], client=client)
    try:
        await backend.start()
        with monkeypatch.context() as patch:
            if failure == "reserve":
                usage = shutil.disk_usage(tmp_path)
                patch.setattr(
                    "camvault.spool.shutil.disk_usage", lambda _: type(usage)(100, 90, 10)
                )
            else:

                def fail(*args, **kwargs):
                    raise OSError("injected disk failure")

                patch.setattr(
                    "camvault.spool.shutil.disk_usage"
                    if failure == "stat"
                    else "camvault.spool._write_chunks",
                    fail,
                )
            with pytest.raises(SpoolWriteError) as error:
                await backend.write_batch(_batch(0))
            assert error.value.allow_reclaim is False
            assert backend.status()["spool"]["write_error"]
            assert not list(backend.directory.glob("*.partial"))
        storage.spool_min_free_gb = 0
        await backend.write_batch(_batch(0))
        assert backend.pending_uploads() == 1
        assert backend.status()["spool"]["write_error"] is None
    finally:
        await backend.close()
        await client.aclose()


@pytest.mark.skipif(os.name == "nt", reason="POSIX SIGKILL crash model")
@pytest.mark.asyncio
async def test_forced_process_kill_releases_lock_and_replays_durable_batch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    storage = _encrypted_storage(tmp_path, monkeypatch)
    storage.spool_min_free_gb = 0
    config = tmp_path / "test-storage.json"
    config.write_text(storage.model_dump_json())
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        str(Path(__file__).parent / "helpers" / "spool_crash.py"),
        str(config),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    cloud = MemoryWebDAV()
    client = httpx.AsyncClient(transport=httpx.MockTransport(cloud))
    recovered = create_storage_backend(storage, ["front"], client=client)
    try:
        assert await asyncio.wait_for(process.stdout.readline(), 5) == b"DURABLE\n"
        process.kill()
        await asyncio.wait_for(process.communicate(), 3)
        await recovered.start()
        assert recovered.pending_uploads() == 1
        await asyncio.wait_for(recovered.drain_uploads(), 3)
        assert len(cloud.files) == 2
        assert recovered.pending_uploads() == 0
    finally:
        if process.returncode is None:
            process.kill()
            await process.communicate()
        await recovered.close()
        await client.aclose()


@pytest.mark.asyncio
async def test_startup_keeps_incomplete_orphan_and_cleans_only_committed_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def offline(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("offline", request=request)

    storage = _encrypted_storage(tmp_path, monkeypatch)
    storage.spool_min_free_gb = 0
    client = httpx.AsyncClient(transport=httpx.MockTransport(offline))
    backend = create_storage_backend(storage, ["front"], client=client)
    try:
        await backend.write_batch(_batch(0))
        entry = next(iter(backend._entries.values()))
        await backend.close()
        committed = entry.directory.with_name(f".{entry.identifier}.committed")
        entry.directory.rename(committed)
        orphan = backend.directory / ("0" * 64 + ".partial")
        orphan.mkdir()
        (orphan / "media").write_bytes(b"interrupted-never-uploaded")
        backend = create_storage_backend(storage, ["front"], client=client)
        await backend.start()
        assert not committed.exists()
        assert (orphan / "media").read_bytes() == b"interrupted-never-uploaded"
        assert backend.status()["spool"]["orphan_bytes"] == len(b"interrupted-never-uploaded")
    finally:
        await backend.close()
        await client.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [503, 507])
async def test_cloud_reclaim_only_on_explicit_quota_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, status: int
) -> None:
    cloud = MemoryWebDAV()

    async def transport(request: httpx.Request) -> httpx.Response:
        if request.method == "PUT":
            # An error body mentioning 507 must not masquerade as a quota response.
            return httpx.Response(status, text="upstream diagnostic: HTTP 507")
        return await cloud(request)

    reclaimed = []

    async def reclaim(camera, size):
        reclaimed.append((camera, size))

    storage = _encrypted_storage(tmp_path, monkeypatch)
    storage.spool_min_free_gb = 0
    client = httpx.AsyncClient(transport=httpx.MockTransport(transport))
    backend = create_storage_backend(storage, ["front"], client=client)
    backend.on_reclaim = reclaim
    try:
        await backend.write_batch(_batch(0))
        await _wait_until(lambda: backend.status()["spool"]["last_error"])
        assert bool(reclaimed) is (status == 507)
        assert backend.pending_uploads() == 1
    finally:
        await backend.close()
        await client.aclose()


@pytest.mark.asyncio
async def test_offline_start_disk_range_playback_restart_and_exact_ciphertext_replay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cloud = MemoryWebDAV()
    offline = [True]

    async def transport(request: httpx.Request) -> httpx.Response:
        if offline[0]:
            raise httpx.ConnectError("injected outage", request=request)
        return await cloud(request)

    storage = _encrypted_storage(tmp_path, monkeypatch)
    storage.spool_min_free_gb = 0
    client = httpx.AsyncClient(transport=httpx.MockTransport(transport))
    backend = create_storage_backend(storage, ["front"], client=client)
    try:
        await backend.start()  # no dependency on AList being online
        record = await backend.write_batch(_batch(0, b"A" * 70000 + b"B" * 70000))
        await backend.write_batch(_batch(1, b"second-video"))
        await _wait_until(lambda: backend.status()["spool"]["last_error"])
        assert backend.pending_uploads() == 2
        assert not cloud.files
        entries = dict(backend._entries)
        ciphertexts = {
            f"/dav/Cloud/CamVault/{entry.camera_id}/{entry.relative_path}": (
                entry.directory / "media"
            ).read_bytes()
            for entry in entries.values()
        }
        for entry in entries.values():
            assert (entry.directory / "media").read_bytes().startswith(ARCHIVE_ENCRYPTION_MAGIC)
            assert (entry.directory / "metadata").read_bytes().startswith(ARCHIVE_ENCRYPTION_MAGIC)
            assert b'"audio_index"' not in (entry.directory / "metadata").read_bytes()
        records = await backend.list_records("front")
        assert len(records) == 2 and records[0].audio_index[0].active
        read = await backend.open_remote_recording(
            "front", record.relative_path, "bytes=69998-70002"
        )
        assert read.status_code == 206
        assert b"".join([chunk async for chunk in read.iter_bytes()]) == b"AABBB"
        await read.close()
        await backend.close()

        backend = create_storage_backend(storage, ["front"], client=client)
        await backend.start()
        assert backend.pending_uploads() == 2
        offline[0] = False
        await asyncio.wait_for(backend.drain_uploads(), timeout=3)
        assert backend.pending_uploads() == 0
        assert backend.status()["spool"]["pending_bytes"] == 0
        assert len(cloud.files) == 4
        for path, ciphertext in ciphertexts.items():
            assert cloud.files[path] == ciphertext  # no re-encryption or new filename
        assert all(not entry.directory.exists() for entry in entries.values())
    finally:
        await backend.close()
        await client.aclose()


@pytest.mark.asyncio
async def test_partial_remote_commit_keeps_local_pair_until_sidecar_succeeds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cloud = MemoryWebDAV()
    fail_metadata = [True]

    async def transport(request: httpx.Request) -> httpx.Response:
        if fail_metadata[0] and request.method == "PUT" and ".json.enc" in request.url.path:
            return httpx.Response(503)
        return await cloud(request)

    storage = _encrypted_storage(tmp_path, monkeypatch)
    storage.spool_min_free_gb = 0
    client = httpx.AsyncClient(transport=httpx.MockTransport(transport))
    backend = create_storage_backend(storage, ["front"], client=client)
    try:
        record = await backend.write_batch(_batch(0))
        await _wait_until(lambda: backend.status()["spool"]["last_error"])
        assert len([path for path in cloud.files if path.endswith(".ts.enc")]) == 1
        assert backend.pending_uploads() == 1
        entry = next(iter(backend._entries.values()))
        assert (entry.directory / "metadata").is_file()
        await backend.close()
        backend = create_storage_backend(storage, ["front"], client=client)
        await backend.start()
        fail_metadata[0] = False
        await asyncio.wait_for(backend.drain_uploads(), timeout=3)
        assert len(cloud.files) == 2
        assert f"/dav/Cloud/CamVault/front/{record.relative_path}" in cloud.files
        assert not entry.directory.exists()
    finally:
        await backend.close()
        await client.aclose()


@pytest.mark.asyncio
async def test_spool_full_preserves_pending_and_never_reclaims_remote(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def offline(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("offline", request=request)

    storage = _encrypted_storage(tmp_path, monkeypatch)
    storage.spool_min_free_gb = 0
    storage.spool_max_gb = 0.00012
    client = httpx.AsyncClient(transport=httpx.MockTransport(offline))
    backend = create_storage_backend(storage, ["front"], client=client)
    try:
        await backend.write_batch(_batch(0, b"a" * 100000))
        with pytest.raises(SpoolWriteError, match="capacity reached") as error:
            await backend.write_batch(_batch(1, b"b" * 100000))
        assert error.value.allow_reclaim is False
        assert backend.pending_uploads() == 1
        assert all(entry.directory.exists() for entry in backend._entries.values())
    finally:
        await backend.close()
        await client.aclose()


@pytest.mark.asyncio
async def test_shutdown_with_offline_webdav_preserves_sealed_tail_for_next_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    async def offline(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("offline", request=request)

    storage = _encrypted_storage(tmp_path, monkeypatch)
    storage.archive_chunk_seconds = 1800
    storage.spool_min_free_gb = 0
    client = httpx.AsyncClient(transport=httpx.MockTransport(offline))
    backend = create_storage_backend(storage, ["front"], client=client)
    config = AppConfig(
        server=ServerConfig(playback_token_env=None, web_password_env=None),
        storage=storage,
        recording=RecordingConfig(max_ingest_segment_mb=8),
        cameras=[CameraConfig(id="front", rtsp_url="rtsp://camera/unused")],
    )
    config.server.shutdown_timeout_seconds = 0.2
    service = CamVaultService(config, storage_backend=backend)
    try:
        await service.start(start_supervisors=False)
        await service.ingest_upload("front", "a.ts", b"tail-still-in-ram")
        assert service.archive_manager.memory_bytes() > 0
        await asyncio.wait_for(service.stop(), timeout=2)
        assert service.archive_manager.unwritten_bytes == 0
        assert backend.pending_uploads() == 1
        assert "remain on disk" in caplog.text
        recovered = create_storage_backend(storage, ["front"], client=client)
        try:
            await recovered.start()
            assert recovered.pending_uploads() == 1
        finally:
            await recovered.close()
    finally:
        await service.stop()
        await client.aclose()


@pytest.mark.asyncio
async def test_spool_lock_destination_binding_and_atomic_rename_recovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def offline(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("offline", request=request)

    storage = _encrypted_storage(tmp_path, monkeypatch)
    storage.spool_min_free_gb = 0
    client = httpx.AsyncClient(transport=httpx.MockTransport(offline))
    backend = create_storage_backend(storage, ["front"], client=client)
    try:
        await backend.write_batch(_batch(0))
        duplicate = create_storage_backend(storage, ["front"], client=client)
        with pytest.raises(OSError):
            await duplicate.start()
        entry = next(iter(backend._entries.values()))
        await backend.close()
        partial = entry.directory.with_name(entry.identifier + ".partial")
        entry.directory.rename(partial)
        backend = create_storage_backend(storage, ["front"], client=client)
        await backend.start()
        assert backend.pending_uploads() == 1
        assert entry.directory.exists() and not partial.exists()
        await backend.close()
        changed = storage.model_copy(deep=True)
        changed.webdav.root = "/DifferentAccount"
        wrong = create_storage_backend(changed, ["front"], client=client)
        with pytest.raises(SpoolWriteError, match="destination/account/key changed"):
            await wrong.start()
        assert entry.directory.exists()
    finally:
        await backend.close()
        await client.aclose()


@pytest.mark.asyncio
async def test_corrupt_spool_is_kept_and_not_committed_to_cloud(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cloud = MemoryWebDAV()
    offline = [True]

    async def transport(request: httpx.Request) -> httpx.Response:
        if offline[0]:
            raise httpx.ConnectError("offline", request=request)
        return await cloud(request)

    storage = _encrypted_storage(tmp_path, monkeypatch)
    storage.spool_min_free_gb = 0
    client = httpx.AsyncClient(transport=httpx.MockTransport(transport))
    backend = create_storage_backend(storage, ["front"], client=client)
    try:
        await backend.write_batch(_batch(0))
        await _wait_until(lambda: backend.status()["spool"]["last_error"])
        entry = next(iter(backend._entries.values()))
        media = entry.directory / "media"
        payload = bytearray(media.read_bytes())
        payload[-1] ^= 1
        media.write_bytes(payload)
        offline[0] = False
        backend._next_retry = 0
        backend._wake.set()
        await _wait_until(lambda: backend.status()["spool"]["blocked_batches"] == 1)
        assert backend.pending_uploads() == 1
        assert media.exists()
        assert not any(path.endswith(".ts.enc") for path in cloud.files)
        assert (
            json.loads((entry.directory / "entry.json").read_text())["relative_path"]
            == entry.relative_path
        )
    finally:
        await backend.close()
        await client.aclose()
