from __future__ import annotations

import email.utils
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import unquote, urlsplit
from xml.sax.saxutils import escape

import httpx
import pytest

from camvault.archive import ArchiveBatch
from camvault.buffer import LiveSegment
from camvault.config import (
    AppConfig,
    CameraConfig,
    RecordingConfig,
    ServerConfig,
    StorageConfig,
    WebDAVConfig,
)
from camvault.service import CamVaultService
from camvault.storage import StorageBackendError, WebDAVStorageBackend
from camvault.web import create_app


class MemoryWebDAV:
    def __init__(self) -> None:
        self.directories = {"/dav"}
        self.files: dict[str, bytes] = {}
        self.modified: dict[str, datetime] = {}
        self.media_put_chunks: list[int] = []
        self.media_put_content_lengths: list[str | None] = []
        self.fail_next_media_put = False
        self.commit_then_fail_next_media_move = False
        self.quota_available = 10 * 1024**3

    @staticmethod
    def _norm(path: str) -> str:
        decoded = unquote(path)
        if decoded != "/":
            decoded = decoded.rstrip("/")
        return decoded

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        path = self._norm(request.url.path)
        method = request.method
        if method == "OPTIONS":
            return httpx.Response(
                200,
                headers={
                    "DAV": "1, 2",
                    "Allow": "OPTIONS, GET, PUT, MKCOL, MOVE, DELETE, PROPFIND",
                },
            )
        if method == "MKCOL":
            if path in self.directories:
                return httpx.Response(405)
            parent = self._norm(str(Path(path).parent).replace("\\", "/"))
            if parent not in self.directories:
                return httpx.Response(409)
            self.directories.add(path)
            self.modified[path] = datetime.now(UTC)
            return httpx.Response(201)
        if method == "PUT":
            parent = self._norm(str(Path(path).parent).replace("\\", "/"))
            if parent not in self.directories:
                return httpx.Response(409)
            chunks: list[bytes] = []
            async for chunk in request.stream:
                chunks.append(bytes(chunk))
            payload = b"".join(chunks)
            if path.endswith(".ts.camvault-partial"):
                self.media_put_chunks.append(len(chunks))
                self.media_put_content_lengths.append(request.headers.get("content-length"))
                if self.fail_next_media_put:
                    self.fail_next_media_put = False
                    return httpx.Response(503, text="injected cloud failure")
            expected_length = request.headers.get("content-length")
            if expected_length is not None and int(expected_length) != len(payload):
                return httpx.Response(400, text="content-length mismatch")
            self.files[path] = payload
            self.modified[path] = datetime.now(UTC)
            return httpx.Response(201)
        if method == "MOVE":
            destination = self._norm(urlsplit(request.headers["destination"]).path)
            if path not in self.files:
                return httpx.Response(404)
            if destination in self.files and request.headers.get("overwrite") == "F":
                return httpx.Response(412)
            parent = self._norm(str(Path(destination).parent).replace("\\", "/"))
            if parent not in self.directories:
                return httpx.Response(409)
            self.files[destination] = self.files.pop(path)
            self.modified[destination] = self.modified.pop(path, datetime.now(UTC))
            if path.endswith(".ts.camvault-partial") and self.commit_then_fail_next_media_move:
                self.commit_then_fail_next_media_move = False
                return httpx.Response(500, text="commit succeeded but response was lost")
            return httpx.Response(201)
        if method == "GET":
            if path not in self.files:
                return httpx.Response(404)
            payload = self.files[path]
            headers = {
                "Content-Type": "video/mp2t"
                if path.endswith(".ts")
                else "application/octet-stream",
                "Accept-Ranges": "bytes",
            }
            range_header = request.headers.get("range")
            if range_header:
                match = re.fullmatch(r"bytes=(\d+)-(\d*)", range_header)
                if not match:
                    return httpx.Response(416)
                start = int(match.group(1))
                end = int(match.group(2)) if match.group(2) else len(payload) - 1
                if start >= len(payload) or end < start:
                    return httpx.Response(416)
                end = min(end, len(payload) - 1)
                selected = payload[start : end + 1]
                headers["Content-Range"] = f"bytes {start}-{end}/{len(payload)}"
                headers["Content-Length"] = str(len(selected))
                return httpx.Response(206, content=selected, headers=headers)
            headers["Content-Length"] = str(len(payload))
            return httpx.Response(200, content=payload, headers=headers)
        if method == "DELETE":
            if path in self.files:
                self.files.pop(path, None)
                self.modified.pop(path, None)
                return httpx.Response(204)
            if path in self.directories:
                prefix = path + "/"
                if any(item.startswith(prefix) for item in self.files | self.directories):
                    return httpx.Response(409)
                self.directories.remove(path)
                return httpx.Response(204)
            return httpx.Response(404)
        if method == "PROPFIND":
            if path not in self.files and path not in self.directories:
                return httpx.Response(404)
            depth = request.headers.get("depth", "infinity")
            objects = [(path, path in self.directories)]
            prefix = path + "/"
            candidates = [(item, True) for item in self.directories if item.startswith(prefix)]
            candidates += [(item, False) for item in self.files if item.startswith(prefix)]
            if depth == "1":
                candidates = [item for item in candidates if "/" not in item[0][len(prefix) :]]
            if depth != "0":
                objects += sorted(candidates)
            body = ["<?xml version='1.0' encoding='utf-8'?><d:multistatus xmlns:d='DAV:'>"]
            for item_path, is_dir in objects:
                href = escape(item_path + ("/" if is_dir and item_path != "/" else ""))
                modified = self.modified.get(item_path, datetime.now(UTC))
                size = 0 if is_dir else len(self.files[item_path])
                resource = "<d:collection/>" if is_dir else ""
                quota = (
                    f"<d:quota-available-bytes>{self.quota_available}</d:quota-available-bytes>"
                    f"<d:quota-used-bytes>{sum(map(len, self.files.values()))}</d:quota-used-bytes>"
                    if item_path == "/dav/Cloud/CamVault"
                    else ""
                )
                body.append(
                    "<d:response>"
                    f"<d:href>{href}</d:href>"
                    "<d:propstat><d:prop>"
                    f"<d:resourcetype>{resource}</d:resourcetype>"
                    f"<d:getcontentlength>{size}</d:getcontentlength>"
                    f"<d:getlastmodified>{email.utils.format_datetime(modified, usegmt=True)}</d:getlastmodified>"
                    f"{quota}"
                    "</d:prop><d:status>HTTP/1.1 200 OK</d:status></d:propstat>"
                    "</d:response>"
                )
            body.append("</d:multistatus>")
            return httpx.Response(
                207,
                content="".join(body).encode(),
                headers={"Content-Type": "application/xml"},
            )
        return httpx.Response(405)


def _storage(tmp_path: Path, **updates: object) -> StorageConfig:
    values: dict[str, object] = {
        "backend": "webdav",
        "root": tmp_path / "must-not-be-created",
        "timezone": "UTC",
        "archive_chunk_seconds": 4,
        "max_buffer_mb_per_camera": 8,
        "retention_days": 0,
        "min_free_gb": 0,
        "webdav": WebDAVConfig(
            url="http://webdav.test/dav",
            root="/Cloud/CamVault",
            username="camvault",
            username_env=None,
            password="secret",
            password_env=None,
        ),
    }
    values.update(updates)
    return StorageConfig.model_validate(values)


def _segment(
    sequence: int,
    payload: bytes,
    created_at: datetime,
    *,
    stream_id: str = "run-a",
) -> LiveSegment:
    return LiveSegment(
        sequence=sequence,
        name=f"segment_{sequence}.ts",
        data=payload,
        created_at=created_at,
        duration=2.0,
        stream_id=stream_id,
    )


@pytest.mark.asyncio
async def test_webdav_streams_atomic_archive_without_creating_local_storage(
    tmp_path: Path,
) -> None:
    server = MemoryWebDAV()
    client = httpx.AsyncClient(transport=httpx.MockTransport(server))
    storage = _storage(tmp_path)
    backend = WebDAVStorageBackend(storage, ["front"], client=client)
    start = datetime(2026, 9, 4, 12, 0, tzinfo=UTC)
    batch = ArchiveBatch(
        camera_id="front",
        segments=(
            _segment(1, b"abc", start),
            _segment(2, b"defg", start + timedelta(seconds=2)),
        ),
    )
    try:
        health = await backend.health_check()
        assert health.diskless_media_path is True
        record = await backend.write_batch(batch)
        assert record.path is None
        assert record.size_bytes == 7
        assert not storage.root.exists()
        assert server.media_put_content_lengths[-1] == "7"
        assert server.media_put_chunks[-1] >= 1
        assert backend._last_media_upload_source_chunks == 2
        assert not any(path.endswith(".camvault-partial") for path in server.files)

        remote_media = next(path for path in server.files if path.endswith(".ts"))
        assert server.files[remote_media] == b"abcdefg"
        assert remote_media.replace(".ts", ".json") in server.files

        records = await backend.list_records(
            "front", start=start - timedelta(seconds=1), end=start + timedelta(minutes=1)
        )
        assert len(records) == 1
        assert records[0].relative_path == record.relative_path
        assert records[0].duration == 4.0

        remote = await backend.open_remote_recording("front", record.relative_path, "bytes=2-5")
        assert remote is not None
        try:
            assert remote.status_code == 206
            assert remote.headers["content-range"] == "bytes 2-5/7"
            assert b"".join([chunk async for chunk in remote.iter_bytes()]) == b"cdef"
        finally:
            await remote.close()
    finally:
        await backend.close()
        await client.aclose()


@pytest.mark.asyncio
async def test_webdav_failure_never_falls_back_to_local_disk(tmp_path: Path) -> None:
    server = MemoryWebDAV()
    server.fail_next_media_put = True
    client = httpx.AsyncClient(transport=httpx.MockTransport(server))
    storage = _storage(tmp_path)
    backend = WebDAVStorageBackend(storage, ["front"], client=client)
    start = datetime.now(UTC)
    batch = ArchiveBatch(camera_id="front", segments=(_segment(1, b"payload", start),))
    try:
        with pytest.raises(StorageBackendError, match="HTTP 503"):
            await backend.write_batch(batch)
        assert not storage.root.exists()
        assert not any(path.endswith(".camvault-partial") for path in server.files)

        record = await backend.write_batch(batch)
        assert record.size_bytes == len(b"payload")
        assert not storage.root.exists()
    finally:
        await backend.close()
        await client.aclose()


@pytest.mark.asyncio
async def test_webdav_retry_after_ambiguous_move_is_idempotent(tmp_path: Path) -> None:
    server = MemoryWebDAV()
    server.commit_then_fail_next_media_move = True
    client = httpx.AsyncClient(transport=httpx.MockTransport(server))
    storage = _storage(tmp_path)
    backend = WebDAVStorageBackend(storage, ["front"], client=client)
    start = datetime.now(UTC)
    batch = ArchiveBatch(camera_id="front", segments=(_segment(1, b"one-commit", start),))
    try:
        with pytest.raises(StorageBackendError, match="HTTP 500"):
            await backend.write_batch(batch)

        committed_media = [path for path in server.files if path.endswith(".ts")]
        assert len(committed_media) == 1
        assert server.files[committed_media[0]] == b"one-commit"
        assert not storage.root.exists()

        record = await backend.write_batch(batch)
        committed_media = [path for path in server.files if path.endswith(".ts")]
        assert len(committed_media) == 1
        assert committed_media[0].endswith(record.relative_path)
        assert committed_media[0].replace(".ts", ".json") in server.files
        assert not any(path.endswith(".camvault-partial") for path in server.files)
        assert not storage.root.exists()
    finally:
        await backend.close()
        await client.aclose()


@pytest.mark.asyncio
async def test_webdav_retention_deletes_old_media_and_sidecar(tmp_path: Path) -> None:
    server = MemoryWebDAV()
    client = httpx.AsyncClient(transport=httpx.MockTransport(server))
    now = datetime(2026, 9, 4, 12, 0, tzinfo=UTC)
    storage = _storage(tmp_path, retention_days=1)
    backend = WebDAVStorageBackend(storage, ["front"], client=client)
    old_batch = ArchiveBatch(
        camera_id="front",
        segments=(_segment(1, b"old", now - timedelta(days=2)),),
    )
    new_batch = ArchiveBatch(
        camera_id="front",
        segments=(_segment(2, b"new", now - timedelta(hours=1)),),
    )
    try:
        old = await backend.write_batch(old_batch)
        new = await backend.write_batch(new_batch)
        result = await backend.retention(now_epoch=now.timestamp())
        assert result.deleted_files == 1
        assert result.deleted_bytes == 3
        assert result.remaining_bytes == 3
        assert result.free_bytes == server.quota_available
        capacity = backend.status()["capacity"]
        assert capacity["source"] == "webdav-quota"
        assert capacity["managed_archive_bytes"] == 3
        assert capacity["total_bytes"] >= server.quota_available
        all_paths = set(server.files)
        old_remote = f"/dav/Cloud/CamVault/front/{old.relative_path}"
        new_remote = f"/dav/Cloud/CamVault/front/{new.relative_path}"
        assert old_remote not in all_paths
        assert old_remote.replace(".ts", ".json") not in all_paths
        assert new_remote in all_paths
        assert new_remote.replace(".ts", ".json") in all_paths
        assert not storage.root.exists()
    finally:
        await backend.close()
        await client.aclose()


@pytest.mark.asyncio
async def test_webdav_emergency_retention_deletes_oldest_first_with_cap(
    tmp_path: Path,
) -> None:
    server = MemoryWebDAV()
    client = httpx.AsyncClient(transport=httpx.MockTransport(server))
    storage = _storage(tmp_path)
    backend = WebDAVStorageBackend(storage, ["front"], client=client)
    start = datetime(2026, 9, 4, 12, 0, tzinfo=UTC)
    try:
        records = []
        for sequence, payload in enumerate((b"1111", b"2222", b"3333"), start=1):
            records.append(
                await backend.write_batch(
                    ArchiveBatch(
                        camera_id="front",
                        segments=(
                            _segment(
                                sequence,
                                payload,
                                start + timedelta(minutes=sequence),
                            ),
                        ),
                    )
                )
            )

        result = await backend.retention(
            now_epoch=start.timestamp(),
            emergency_min_delete_bytes=6,
            emergency_max_delete_files=2,
        )

        assert result.deleted_files == 2
        assert result.deleted_bytes == 8
        assert result.remaining_bytes == 4
        for record in records[:2]:
            remote = f"/dav/Cloud/CamVault/front/{record.relative_path}"
            assert remote not in server.files
            assert remote.replace(".ts", ".json") not in server.files
        newest_remote = f"/dav/Cloud/CamVault/front/{records[2].relative_path}"
        assert newest_remote in server.files
        assert newest_remote.replace(".ts", ".json") in server.files
    finally:
        await backend.close()
        await client.aclose()


@pytest.mark.asyncio
async def test_webdav_service_vod_proxies_range_without_exposing_credentials(
    tmp_path: Path,
) -> None:
    server = MemoryWebDAV()
    webdav_client = httpx.AsyncClient(transport=httpx.MockTransport(server))
    storage = _storage(tmp_path)
    config = AppConfig(
        server=ServerConfig(host="127.0.0.1", playback_token="token", playback_token_env=None),
        storage=storage,
        recording=RecordingConfig(
            hls_segment_seconds=2,
            max_ingest_segment_mb=8,
            max_live_memory_mb_per_camera=4,
            include_audio=False,
            audio_codec="none",
        ),
        cameras=[CameraConfig(id="front", rtsp_url="rtsp://127.0.0.1/unused")],
    )
    backend = WebDAVStorageBackend(storage, ["front"], client=webdav_client)
    service = CamVaultService(config, storage_backend=backend)
    await service.start(start_supervisors=False)
    app = create_app(service, manage_service=False)
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 40000))
    headers = {"X-CamVault-Ingest": service.ingest_secret}
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://camvault") as client:
            assert (
                await client.put(
                    "/_ingest/front/segment_run1_20260904T120000_000000.ts",
                    content=b"hello",
                    headers=headers,
                )
            ).status_code == 201
            assert (
                await client.put(
                    "/_ingest/front/segment_run1_20260904T120002_000000.ts",
                    content=b"world",
                    headers=headers,
                )
            ).status_code == 201
            await service.flush_archives()

            vod = await client.get("/vod/front/index.m3u8", params={"token": "token"})
            assert vod.status_code == 200
            media_uri = next(
                line for line in vod.text.splitlines() if line.startswith("/recordings/")
            )
            media_path = urlsplit(media_uri).path
            playback = await client.get(
                media_path,
                params={"token": "token"},
                headers={"Range": "bytes=1-7"},
            )
            assert playback.status_code == 206
            assert playback.content == b"ellowor"
            assert playback.headers["content-range"] == "bytes 1-7/10"

            unsatisfied = await client.get(
                media_path,
                params={"token": "token"},
                headers={"Range": "bytes=999-"},
            )
            assert unsatisfied.status_code == 416

            status = (await client.get("/api/status", params={"token": "token"})).json()
            assert status["storage"]["backend"] == "webdav"
            assert status["storage"]["diskless_media_path"] is True
            assert status["storage"]["local_media_spool"] is False
            assert status["storage_root"] is None
            assert "secret" not in str(status)
            assert not storage.root.exists()
    finally:
        await service.stop()
        await webdav_client.aclose()
