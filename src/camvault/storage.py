from __future__ import annotations

import asyncio
import email.utils
import hashlib
import json
import logging
import re
import secrets
import shutil
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath
from urllib.parse import quote, unquote, urlsplit, urlunsplit
from zoneinfo import ZoneInfo

import httpx
from defusedxml import ElementTree

from camvault.archive import (
    ArchiveBatch,
    ArchiveRecord,
    archive_relative_path,
    ensure_storage_root,
    parse_archive_filename,
    scan_archive_records,
    write_archive_batch,
)
from camvault.config import StorageConfig, WebDAVConfig
from camvault.crypto import (
    ARCHIVE_ENCRYPTION_ALGORITHM,
    ARCHIVE_ENCRYPTION_HEADER_BYTES,
    ARCHIVE_ENCRYPTION_TAG_BYTES,
    ArchiveEncryptionError,
    ArchiveEncryptionHeader,
    decrypt_archive_chunk,
    encrypt_archive_chunks,
    encrypted_size,
    encrypted_span_for_plaintext_range,
    parse_encryption_header,
    plaintext_chunk_size,
)
from camvault.retention import RetentionResult, apply_retention

logger = logging.getLogger(__name__)

_DAV = "{DAV:}"
_PROPFIND_BODY = b"""<?xml version="1.0" encoding="utf-8"?>
<d:propfind xmlns:d="DAV:"><d:prop>
  <d:resourcetype/><d:getcontentlength/><d:getlastmodified/>
  <d:getetag/><d:quota-available-bytes/><d:quota-used-bytes/>
</d:prop></d:propfind>"""


def _archive_sidecar_path(media_path: str) -> str:
    if media_path.endswith(".ts.enc"):
        return f"{media_path[:-7]}.json.enc"
    return str(PurePosixPath(media_path).with_suffix(".json"))


def _resolve_byte_range(value: str | None, size: int) -> tuple[int, int, int] | None:
    if size <= 0:
        return None
    if value is None:
        return 200, 0, size - 1
    match = re.fullmatch(r"bytes=(\d*)-(\d*)", value.strip())
    if match is None or (not match.group(1) and not match.group(2)):
        return None
    if match.group(1):
        start = int(match.group(1))
        end = int(match.group(2)) if match.group(2) else size - 1
        if start >= size or end < start:
            return None
        return 206, start, min(end, size - 1)
    suffix_size = int(match.group(2))
    if suffix_size <= 0:
        return None
    return 206, max(0, size - suffix_size), size - 1


async def _empty_remote_body() -> AsyncIterator[bytes]:
    if False:
        yield b""


async def _no_remote_close() -> None:
    return None


async def _iter_http_response(
    response: httpx.Response, *, chunk_size: int = 128 * 1024
) -> AsyncIterator[bytes]:
    if response.is_stream_consumed:
        if response.content:
            yield response.content
        return
    async for chunk in response.aiter_raw(chunk_size=chunk_size):
        yield chunk


class StorageBackendError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class StorageHealth:
    backend: str
    location: str
    detail: str
    diskless_media_path: bool


@dataclass(slots=True)
class RemoteRead:
    status_code: int
    headers: dict[str, str]
    body: AsyncIterator[bytes]
    close_callback: Callable[[], Awaitable[None]]
    _closed: bool = False

    @classmethod
    def from_response(cls, response: httpx.Response) -> RemoteRead:
        allowed = {
            "content-length",
            "content-range",
            "accept-ranges",
            "etag",
            "last-modified",
            "content-type",
        }
        headers = {key: value for key, value in response.headers.items() if key in allowed}
        return cls(
            status_code=response.status_code,
            headers=headers,
            body=_iter_http_response(response),
            close_callback=response.aclose,
        )

    def iter_bytes(self) -> AsyncIterator[bytes]:
        return self.body

    async def close(self) -> None:
        if not self._closed:
            self._closed = True
            await self.close_callback()


@dataclass(frozen=True, slots=True)
class WebDAVEntry:
    relative_path: str
    is_dir: bool
    size_bytes: int
    modified: datetime | None
    quota_available_bytes: int | None = None
    quota_used_bytes: int | None = None


class StorageBackend:
    kind: str
    location: str
    diskless_media_path: bool
    storage: StorageConfig

    async def start(self) -> None:
        raise NotImplementedError

    async def close(self) -> None:
        raise NotImplementedError

    async def health_check(self) -> StorageHealth:
        raise NotImplementedError

    async def write_batch(self, batch: ArchiveBatch) -> ArchiveRecord:
        raise NotImplementedError

    async def list_records(
        self,
        camera_id: str,
        *,
        start: datetime | None = None,
        end: datetime | None = None,
        limit: int | None = None,
    ) -> list[ArchiveRecord]:
        raise NotImplementedError

    def local_recording_path(self, camera_id: str, relative_path: str) -> Path | None:
        return None

    async def open_remote_recording(
        self, camera_id: str, relative_path: str, range_header: str | None
    ) -> RemoteRead | None:
        return None

    async def retention(
        self,
        *,
        now_epoch: float | None = None,
        emergency_min_delete_bytes: int = 0,
        emergency_max_delete_files: int | None = None,
    ) -> RetentionResult:
        raise NotImplementedError

    def status(self) -> dict[str, object]:
        return {
            "backend": self.kind,
            "location": self.location,
            "diskless_media_path": self.diskless_media_path,
        }


class LocalStorageBackend(StorageBackend):
    kind = "local"
    diskless_media_path = False

    def __init__(self, storage: StorageConfig) -> None:
        self.storage = storage
        self.location = str(storage.root)
        self._managed_archive_bytes: int | None = None

    async def start(self) -> None:
        await asyncio.to_thread(ensure_storage_root, self.storage.root)

    async def close(self) -> None:
        return None

    async def health_check(self) -> StorageHealth:
        await self.start()

        def check() -> None:
            test_file = self.storage.root / f".camvault-write-test-{secrets.token_hex(4)}"
            test_file.write_bytes(b"CamVault local storage check\n")
            if test_file.read_bytes() != b"CamVault local storage check\n":
                raise StorageBackendError("local storage read-after-write verification failed")
            test_file.unlink()

        await asyncio.to_thread(check)
        return StorageHealth(
            backend=self.kind,
            location=self.location,
            detail="local create/write/read/delete succeeded",
            diskless_media_path=False,
        )

    async def write_batch(self, batch: ArchiveBatch) -> ArchiveRecord:
        record = await asyncio.to_thread(write_archive_batch, batch, self.storage)
        if self._managed_archive_bytes is not None:
            self._managed_archive_bytes += record.size_bytes
        return record

    async def list_records(
        self,
        camera_id: str,
        *,
        start: datetime | None = None,
        end: datetime | None = None,
        limit: int | None = None,
    ) -> list[ArchiveRecord]:
        records = await asyncio.to_thread(scan_archive_records, self.storage.root, camera_id)
        return _filter_records(records, start=start, end=end, limit=limit)

    def local_recording_path(self, camera_id: str, relative_path: str) -> Path | None:
        relative = _safe_recording_relative(relative_path)
        root = (self.storage.root / camera_id).resolve()
        candidate = root.joinpath(*relative.parts).resolve()
        try:
            candidate.relative_to(root)
        except ValueError as exc:
            raise ValueError("invalid recording path") from exc
        return candidate

    async def retention(
        self,
        *,
        now_epoch: float | None = None,
        emergency_min_delete_bytes: int = 0,
        emergency_max_delete_files: int | None = None,
    ) -> RetentionResult:
        result = await asyncio.to_thread(
            apply_retention,
            self.storage,
            now_epoch=now_epoch,
            emergency_min_delete_bytes=emergency_min_delete_bytes,
            emergency_max_delete_files=emergency_max_delete_files,
        )
        self._managed_archive_bytes = result.remaining_bytes
        return result

    def status(self) -> dict[str, object]:
        capacity: dict[str, object] = {
            "source": "filesystem",
            "managed_archive_bytes": self._managed_archive_bytes,
        }
        try:
            usage = shutil.disk_usage(self.storage.root)
        except OSError as exc:
            capacity["error"] = str(exc)
        else:
            capacity |= {
                "total_bytes": usage.total,
                "used_bytes": usage.used,
                "free_bytes": usage.free,
            }
        return super().status() | {"capacity": capacity}


class WebDAVStorageBackend(StorageBackend):
    kind = "webdav"
    diskless_media_path = True

    def __init__(
        self,
        storage: StorageConfig,
        camera_ids: Iterable[str],
        *,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.storage = storage
        self.config: WebDAVConfig = storage.webdav
        self.camera_ids = tuple(camera_ids)
        self.location = f"{self.config.url}{self.config.root}"
        self._client = client
        self._owns_client = client is None
        self._started = False
        self._ensured_collections: set[tuple[str, ...]] = set()
        self._records_cache: dict[
            tuple[str, int | None, int | None], tuple[float, list[ArchiveRecord]]
        ] = {}
        self._cache_seconds = 30.0
        self._last_media_upload_source_chunks = 0
        self._managed_archive_bytes: int | None = None
        self._quota_available_cache: int | None = None
        self._quota_used_cache: int | None = None
        self._root_parts = tuple(PurePosixPath(self.config.root).parts[1:])
        parsed = urlsplit(self.config.url)
        self._base_path = parsed.path.rstrip("/")

    def _new_client(self) -> httpx.AsyncClient:
        username = self.config.resolved_username()
        password = self.config.resolved_password()
        if username is None or password is None:
            raise StorageBackendError("WebDAV username/password are not available")
        timeout = httpx.Timeout(
            self.config.request_timeout_seconds,
            connect=self.config.connect_timeout_seconds,
        )
        return httpx.AsyncClient(
            auth=httpx.BasicAuth(username, password),
            timeout=timeout,
            limits=httpx.Limits(
                max_connections=self.config.max_connections,
                max_keepalive_connections=self.config.max_connections,
            ),
            verify=self.config.verify_tls,
            follow_redirects=True,
            headers={"User-Agent": "CamVault/0.9.1", "Accept-Encoding": "identity"},
        )

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = self._new_client()
        return self._client

    async def start(self) -> None:
        if self._started:
            return
        # OPTIONS verifies endpoint reachability/auth without touching local storage.
        response = await self._request("OPTIONS", self.config.url, expected={200, 204})
        await response.aclose()
        await self._ensure_collection_parts(self._root_parts)
        self._started = True

    async def close(self) -> None:
        if self._client is not None and self._owns_client:
            await self._client.aclose()
            self._client = None
        self._started = False

    async def health_check(self) -> StorageHealth:
        await self.start()
        # Avoid a leading dot: AList users need not be granted its separate
        # "see hidden files" permission merely to use CamVault transactions.
        directory = ("_camvault-health",)
        await self._ensure_collection_parts((*self._root_parts, *directory))
        token = secrets.token_hex(8)
        source = PurePosixPath(*directory, f"{token}.tmp").as_posix()
        destination = PurePosixPath(*directory, f"{token}.ok").as_posix()
        payload = b"CamVault WebDAV streaming health check\n"
        try:
            await self._put(source, (payload,), len(payload), "application/octet-stream")
            await self._move(source, destination)
            response = await self._request(
                "GET",
                self._url(destination),
                expected={200},
                headers={"Accept-Encoding": "identity"},
            )
            try:
                if response.content != payload:
                    raise StorageBackendError("WebDAV read-after-write verification failed")
            finally:
                await response.aclose()
        finally:
            # Cleanup must not hide the original health-check failure when the endpoint
            # disappears between operations.
            await self._best_effort_delete(source)
            await self._best_effort_delete(destination)
        return StorageHealth(
            backend=self.kind,
            location=self.location,
            detail="OPTIONS/MKCOL/PUT/MOVE/GET/DELETE succeeded using in-memory payloads",
            diskless_media_path=True,
        )

    async def write_batch(self, batch: ArchiveBatch) -> ArchiveRecord:
        await self.start()
        plain_relative = archive_relative_path(batch, self.storage)
        encryption_key = (
            self.config.resolved_encryption_key() if self.config.encryption_enabled else None
        )
        encrypted = encryption_key is not None
        relative = f"{plain_relative}.enc" if encrypted else plain_relative
        camera_relative = PurePosixPath(batch.camera_id, relative).as_posix()
        media_path = camera_relative
        metadata_path = _archive_sidecar_path(camera_relative)
        media_partial = _partial_name(media_path)
        metadata_partial = _partial_name(metadata_path)
        directory_parts = (*self._root_parts, *PurePosixPath(camera_relative).parent.parts)
        await self._ensure_collection_parts(directory_parts)

        digest = hashlib.sha256()
        for segment in batch.segments:
            digest.update(segment.data)
        record = ArchiveRecord(
            camera_id=batch.camera_id,
            path=None,
            relative_path=relative,
            start=batch.start.astimezone(UTC),
            end=batch.end.astimezone(UTC),
            duration=batch.duration,
            size_bytes=batch.size_bytes,
            sha256=digest.hexdigest(),
            segment_count=len(batch.segments),
            stream_id=batch.stream_id,
            audio_index=batch.audio_index,
            encrypted=encrypted,
        )
        metadata = record.as_dict() | {
            "format": "mpegts",
            "version": 3,
            "sequences": [batch.segments[0].sequence, batch.segments[-1].sequence],
            "object_id": batch.object_id,
            "backend": "webdav",
        }
        metadata_bytes = (json.dumps(metadata, ensure_ascii=False, indent=2) + "\n").encode("utf-8")

        if encrypted:
            assert encryption_key is not None
            encryption_chunk_size = self.config.encryption_chunk_kb * 1024
            media_chunks: Iterable[bytes] = encrypt_archive_chunks(
                (segment.data for segment in batch.segments),
                plaintext_size=batch.size_bytes,
                chunk_size=encryption_chunk_size,
                key=encryption_key,
                context=media_path,
            )
            media_content_length = encrypted_size(batch.size_bytes, encryption_chunk_size)
            metadata_chunks: Iterable[bytes] = encrypt_archive_chunks(
                (metadata_bytes,),
                plaintext_size=len(metadata_bytes),
                chunk_size=encryption_chunk_size,
                key=encryption_key,
                context=metadata_path,
            )
            metadata_content_length = encrypted_size(len(metadata_bytes), encryption_chunk_size)
            media_content_type = "application/octet-stream"
            metadata_content_type = "application/octet-stream"
        else:
            media_chunks = (segment.data for segment in batch.segments)
            media_content_length = batch.size_bytes
            metadata_chunks = (metadata_bytes,)
            metadata_content_length = len(metadata_bytes)
            media_content_type = "video/mp2t"
            metadata_content_type = "application/json; charset=utf-8"

        try:
            if self.config.atomic_upload:
                await self._put(
                    media_partial,
                    media_chunks,
                    media_content_length,
                    media_content_type,
                    track_media=True,
                )
                await self._move(media_partial, media_path)
                await self._put(
                    metadata_partial,
                    metadata_chunks,
                    metadata_content_length,
                    metadata_content_type,
                )
                await self._move(metadata_partial, metadata_path)
            else:
                await self._put(
                    media_path,
                    media_chunks,
                    media_content_length,
                    media_content_type,
                    track_media=True,
                )
                await self._put(
                    metadata_path,
                    metadata_chunks,
                    metadata_content_length,
                    metadata_content_type,
                )
        except Exception:
            # Never spool to local disk on retry. Only remove remote transaction objects;
            # committed final media is retained so a retry can finish its sidecar.
            await self._best_effort_delete(media_partial)
            await self._best_effort_delete(metadata_partial)
            raise

        self._invalidate_camera(batch.camera_id)
        if self._managed_archive_bytes is not None:
            self._managed_archive_bytes += record.size_bytes
        return record

    async def list_records(
        self,
        camera_id: str,
        *,
        start: datetime | None = None,
        end: datetime | None = None,
        limit: int | None = None,
    ) -> list[ArchiveRecord]:
        await self.start()
        start_utc = _utc_or_none(start)
        end_utc = _utc_or_none(end)
        cache_key = (
            camera_id,
            int(start_utc.timestamp() // 3600) if start_utc else None,
            int(end_utc.timestamp() // 3600) if end_utc else None,
        )
        cached = self._records_cache.get(cache_key)
        if cached is not None and time.monotonic() - cached[0] <= self._cache_seconds:
            return _filter_records(list(cached[1]), start=start_utc, end=end_utc, limit=limit)

        entries: list[WebDAVEntry] = []
        if (
            start_utc is not None
            and end_utc is not None
            and (end_utc - start_utc).total_seconds() <= self.config.targeted_scan_max_hours * 3600
        ):
            hour_paths = self._hour_paths(camera_id, start_utc, end_utc)
            for path in hour_paths:
                entries.extend(await self._propfind(path, depth="1", missing_ok=True))
        else:
            entries = await self._propfind(camera_id, depth="infinity", missing_ok=True)

        records = self._records_from_entries(camera_id, entries)
        while len(self._records_cache) >= self.config.index_cache_entries:
            self._records_cache.pop(next(iter(self._records_cache)))
        self._records_cache[cache_key] = (time.monotonic(), list(records))
        return _filter_records(records, start=start_utc, end=end_utc, limit=limit)

    async def open_remote_recording(
        self, camera_id: str, relative_path: str, range_header: str | None
    ) -> RemoteRead | None:
        await self.start()
        relative = _safe_recording_relative(relative_path)
        remote_path = PurePosixPath(camera_id, *relative.parts).as_posix()
        if relative_path.endswith(".ts.enc"):
            return await self._open_encrypted_recording(remote_path, range_header)
        response = await self._open_remote_response(remote_path, range_header)
        return RemoteRead.from_response(response)

    async def _open_remote_response(
        self, remote_path: str, range_header: str | None
    ) -> httpx.Response:
        headers = {"Accept-Encoding": "identity"}
        if range_header:
            headers["Range"] = range_header
        request = self.client.build_request("GET", self._url(remote_path), headers=headers)
        try:
            response = await self.client.send(request, stream=True, follow_redirects=True)
        except httpx.HTTPError as exc:
            raise StorageBackendError(f"WebDAV GET failed: {type(exc).__name__}: {exc}") from exc
        if response.status_code == 404:
            await response.aclose()
            raise FileNotFoundError(remote_path)
        # Preserve 416 so CamVault behaves like a transparent range proxy. Converting it
        # to 502 makes media players retry the wrong failure class.
        if response.status_code not in {200, 206, 416}:
            status = response.status_code
            await response.aclose()
            raise StorageBackendError(f"WebDAV GET returned HTTP {status}")
        return response

    async def _open_encrypted_recording(
        self, remote_path: str, range_header: str | None
    ) -> RemoteRead:
        try:
            key = self.config.resolved_encryption_key()
        except ValueError as exc:
            raise StorageBackendError(str(exc)) from exc
        if key is None:
            raise StorageBackendError(
                "encrypted WebDAV archive cannot be opened because its key is unavailable"
            )

        header_response = await self._open_remote_response(
            remote_path, f"bytes=0-{ARCHIVE_ENCRYPTION_HEADER_BYTES - 1}"
        )
        try:
            if header_response.status_code == 416:
                raise StorageBackendError("encrypted WebDAV archive is missing its header")
            header_bytes = bytearray()
            async for chunk in _iter_http_response(
                header_response, chunk_size=ARCHIVE_ENCRYPTION_HEADER_BYTES
            ):
                needed = ARCHIVE_ENCRYPTION_HEADER_BYTES - len(header_bytes)
                header_bytes.extend(chunk[:needed])
                if len(header_bytes) == ARCHIVE_ENCRYPTION_HEADER_BYTES:
                    break
            try:
                header = parse_encryption_header(bytes(header_bytes))
            except ArchiveEncryptionError as exc:
                raise StorageBackendError(str(exc)) from exc
        finally:
            await header_response.aclose()

        selected = _resolve_byte_range(range_header, header.plaintext_size)
        if selected is None:
            return RemoteRead(
                status_code=416,
                headers={
                    "content-range": f"bytes */{header.plaintext_size}",
                    "content-length": "0",
                    "accept-ranges": "bytes",
                },
                body=_empty_remote_body(),
                close_callback=_no_remote_close,
            )
        status_code, plain_start, plain_end = selected
        first_chunk, last_chunk, stored_start, stored_end = encrypted_span_for_plaintext_range(
            header, plain_start, plain_end
        )
        data_response = await self._open_remote_response(
            remote_path, f"bytes={stored_start}-{stored_end}"
        )
        if data_response.status_code == 416:
            await data_response.aclose()
            raise StorageBackendError("encrypted WebDAV archive is truncated")
        source_skip = stored_start if data_response.status_code == 200 else 0
        if data_response.status_code == 206:
            content_range = data_response.headers.get("content-range", "")
            match = re.fullmatch(r"bytes (\d+)-(\d+)/(?:\d+|\*)", content_range)
            if match is None or int(match.group(1)) != stored_start:
                await data_response.aclose()
                raise StorageBackendError("WebDAV returned an invalid encrypted byte range")

        body = self._decrypt_remote_range(
            response=data_response,
            header=header,
            key=key,
            context=remote_path,
            first_chunk=first_chunk,
            last_chunk=last_chunk,
            stored_bytes=stored_end - stored_start + 1,
            source_skip=source_skip,
            plain_start=plain_start,
            plain_end=plain_end,
        )
        response_headers = {
            "content-length": str(plain_end - plain_start + 1),
            "accept-ranges": "bytes",
            "content-type": "video/mp2t",
        }
        if status_code == 206:
            response_headers["content-range"] = (
                f"bytes {plain_start}-{plain_end}/{header.plaintext_size}"
            )
        if "last-modified" in data_response.headers:
            response_headers["last-modified"] = data_response.headers["last-modified"]
        return RemoteRead(
            status_code=status_code,
            headers=response_headers,
            body=body,
            close_callback=data_response.aclose,
        )

    async def _decrypt_remote_range(
        self,
        *,
        response: httpx.Response,
        header: ArchiveEncryptionHeader,
        key: bytes,
        context: str,
        first_chunk: int,
        last_chunk: int,
        stored_bytes: int,
        source_skip: int,
        plain_start: int,
        plain_end: int,
    ) -> AsyncIterator[bytes]:
        pending = bytearray()
        remaining = stored_bytes
        chunk_index = first_chunk
        try:
            async for source in _iter_http_response(response):
                if source_skip:
                    skipped = min(source_skip, len(source))
                    source_skip -= skipped
                    source = source[skipped:]
                    if not source:
                        continue
                accepted = source[:remaining]
                pending.extend(accepted)
                remaining -= len(accepted)
                while chunk_index <= last_chunk:
                    cipher_size = (
                        plaintext_chunk_size(header, chunk_index) + ARCHIVE_ENCRYPTION_TAG_BYTES
                    )
                    if len(pending) < cipher_size:
                        break
                    encrypted_chunk = bytes(pending[:cipher_size])
                    del pending[:cipher_size]
                    try:
                        plaintext = decrypt_archive_chunk(
                            encrypted_chunk,
                            header=header,
                            index=chunk_index,
                            key=key,
                            context=context,
                        )
                    except ArchiveEncryptionError as exc:
                        raise StorageBackendError(str(exc)) from exc
                    chunk_plain_start = chunk_index * header.chunk_size
                    selected_start = max(0, plain_start - chunk_plain_start)
                    selected_end = min(len(plaintext), plain_end - chunk_plain_start + 1)
                    if selected_start < selected_end:
                        yield plaintext[selected_start:selected_end]
                    chunk_index += 1
                if remaining == 0:
                    break
        except httpx.HTTPError as exc:
            raise StorageBackendError(
                f"WebDAV encrypted GET failed: {type(exc).__name__}: {exc}"
            ) from exc
        if source_skip or remaining or pending or chunk_index <= last_chunk:
            raise StorageBackendError("encrypted WebDAV archive is truncated")

    async def retention(
        self,
        *,
        now_epoch: float | None = None,
        emergency_min_delete_bytes: int = 0,
        emergency_max_delete_files: int | None = None,
    ) -> RetentionResult:
        await self.start()
        now_epoch = time.time() if now_epoch is None else now_epoch
        entries_by_camera: dict[str, list[WebDAVEntry]] = {}
        records: list[ArchiveRecord] = []
        for camera_id in self.camera_ids:
            entries = await self._propfind(camera_id, depth="infinity", missing_ok=True)
            entries_by_camera[camera_id] = entries
            records.extend(self._records_from_entries(camera_id, entries))

        deleted_files = 0
        deleted_bytes = 0
        deleted_paths: set[tuple[str, str]] = set()

        async def delete_record(record: ArchiveRecord) -> None:
            nonlocal deleted_files, deleted_bytes
            key = (record.camera_id, record.relative_path)
            if key in deleted_paths:
                return
            remote = PurePosixPath(record.camera_id, record.relative_path).as_posix()
            await self._delete(remote, ignore_missing=True)
            await self._delete(_archive_sidecar_path(remote), ignore_missing=True)
            deleted_paths.add(key)
            deleted_files += 1
            deleted_bytes += record.size_bytes
            self._invalidate_camera(record.camera_id)

        # Remove abandoned remote transaction files and old committed orphans. All are
        # identified by CamVault's deterministic naming convention under dedicated roots.
        stale_cutoff = datetime.fromtimestamp(
            now_epoch - self.storage.partial_max_age_hours * 3600, tz=UTC
        )
        for camera_id, entries in entries_by_camera.items():
            sidecars = {
                entry.relative_path
                for entry in entries
                if entry.relative_path.endswith((".json", ".json.enc"))
            }
            for entry in entries:
                if entry.is_dir or entry.modified is None or entry.modified >= stale_cutoff:
                    continue
                name = PurePosixPath(entry.relative_path).name
                remote = entry.relative_path
                if name.endswith(".camvault-partial"):
                    await self._delete(remote, ignore_missing=True)
                    continue
                if entry.relative_path.endswith((".ts", ".ts.enc")):
                    parsed = parse_archive_filename(name)
                    expected_sidecar = _archive_sidecar_path(entry.relative_path)
                    if parsed is not None and expected_sidecar not in sidecars:
                        await self._delete(remote, ignore_missing=True)
                        self._invalidate_camera(camera_id)

        records.sort(key=lambda item: item.start)
        retained = list(records)
        if self.storage.retention_days > 0:
            cutoff = datetime.fromtimestamp(now_epoch - self.storage.retention_days * 86400, tz=UTC)
            next_retained: list[ArchiveRecord] = []
            for record in retained:
                if record.start < cutoff:
                    await delete_record(record)
                else:
                    next_retained.append(record)
            retained = next_retained

        total = sum(record.size_bytes for record in retained)
        max_bytes = int(self.storage.max_storage_gb * 1024**3)
        if max_bytes > 0 and total > max_bytes:
            next_retained = []
            for record in retained:
                if total > max_bytes:
                    await delete_record(record)
                    total -= record.size_bytes
                else:
                    next_retained.append(record)
            retained = next_retained

        free_bytes = await self._quota_available_bytes()
        min_free_bytes = int(self.storage.min_free_gb * 1024**3)
        if min_free_bytes > 0 and free_bytes is not None and free_bytes < min_free_bytes:
            next_retained = []
            for record in retained:
                if free_bytes < min_free_bytes:
                    await delete_record(record)
                    total -= record.size_bytes
                    free_bytes += record.size_bytes
                else:
                    next_retained.append(record)
            retained = next_retained
        elif min_free_bytes > 0 and free_bytes is None:
            logger.warning(
                "WebDAV server does not expose DAV:quota-available-bytes; "
                "storage.min_free_gb cannot be enforced for this backend"
            )

        # WebDAV servers such as AList may not expose quota-available-bytes. A failed
        # archive transaction can therefore request a bounded oldest-first reclaim. Any
        # files already removed by the normal age/size/free-space policy count toward the
        # requested headroom, avoiding needless extra deletion.
        emergency_needed = max(0, emergency_min_delete_bytes - deleted_bytes)
        emergency_deleted = 0
        emergency_files = 0
        if emergency_needed > 0:
            next_retained = []
            for record in retained:
                reached_byte_target = emergency_deleted >= emergency_needed
                reached_file_limit = (
                    emergency_max_delete_files is not None
                    and emergency_files >= emergency_max_delete_files
                )
                if not reached_byte_target and not reached_file_limit:
                    await delete_record(record)
                    emergency_deleted += record.size_bytes
                    emergency_files += 1
                    total -= record.size_bytes
                else:
                    next_retained.append(record)
            retained = next_retained
            if free_bytes is not None:
                free_bytes += emergency_deleted

        result = RetentionResult(
            deleted_files=deleted_files,
            deleted_bytes=deleted_bytes,
            remaining_bytes=max(0, sum(record.size_bytes for record in retained)),
            free_bytes=free_bytes,
        )
        self._managed_archive_bytes = result.remaining_bytes
        return result

    def status(self) -> dict[str, object]:
        total_bytes = None
        if self._quota_available_cache is not None and self._quota_used_cache is not None:
            total_bytes = self._quota_available_cache + self._quota_used_cache
        return super().status() | {
            "transport": "HTTP WebDAV streaming",
            "atomic_upload": self.config.atomic_upload,
            "local_media_spool": False,
            "credential_source": "environment/config (not exposed)",
            "encryption": {
                "enabled": self.config.encryption_enabled,
                "algorithm": (
                    ARCHIVE_ENCRYPTION_ALGORITHM if self.config.encryption_enabled else None
                ),
                "chunk_bytes": (
                    self.config.encryption_chunk_kb * 1024
                    if self.config.encryption_enabled
                    else None
                ),
                "key_source": (
                    "environment (not exposed)" if self.config.encryption_enabled else None
                ),
            },
            "capacity": {
                "source": (
                    "webdav-quota" if self._quota_available_cache is not None else "camvault-index"
                ),
                "total_bytes": total_bytes,
                "used_bytes": self._quota_used_cache,
                "free_bytes": self._quota_available_cache,
                "managed_archive_bytes": self._managed_archive_bytes,
            },
        }

    def _hour_paths(self, camera_id: str, start: datetime, end: datetime) -> list[str]:
        # Include the previous archive interval because a chunk can begin in the prior hour
        # and still overlap the requested time range.
        cursor = (start - timedelta(seconds=self.storage.archive_chunk_seconds)).replace(
            minute=0, second=0, microsecond=0
        )
        final = end.replace(minute=0, second=0, microsecond=0)
        local_tz = ZoneInfo(self.storage.timezone)
        paths: set[str] = set()
        while cursor <= final:
            local = cursor.astimezone(local_tz)
            paths.add(
                PurePosixPath(
                    camera_id,
                    local.strftime("%Y"),
                    local.strftime("%m"),
                    local.strftime("%d"),
                    local.strftime("%H"),
                ).as_posix()
            )
            cursor += timedelta(hours=1)
        return sorted(paths)

    def _records_from_entries(
        self, camera_id: str, entries: list[WebDAVEntry]
    ) -> list[ArchiveRecord]:
        sidecars = {
            entry.relative_path
            for entry in entries
            if entry.relative_path.endswith((".json", ".json.enc"))
        }
        prefix = f"{camera_id}/"
        records: list[ArchiveRecord] = []
        for entry in entries:
            if entry.is_dir or not entry.relative_path.startswith(prefix):
                continue
            if not entry.relative_path.endswith((".ts", ".ts.enc")):
                continue
            sidecar = _archive_sidecar_path(entry.relative_path)
            if sidecar not in sidecars:
                continue
            relative = entry.relative_path[len(prefix) :]
            path = PurePosixPath(relative)
            if len(path.parts) != 5:
                continue
            parsed = parse_archive_filename(path.name)
            if parsed is None:
                continue
            records.append(
                ArchiveRecord(
                    camera_id=camera_id,
                    path=None,
                    relative_path=relative,
                    start=parsed.start,
                    end=parsed.start + timedelta(seconds=parsed.duration),
                    duration=parsed.duration,
                    size_bytes=entry.size_bytes,
                    stream_id=parsed.stream_id,
                    audio_index=parsed.audio_index,
                    encrypted=parsed.encrypted,
                )
            )
        records.sort(key=lambda record: record.start)
        return records

    async def _quota_available_bytes(self) -> int | None:
        entries = await self._propfind("", depth="0", missing_ok=False)
        for entry in entries:
            if entry.quota_used_bytes is not None:
                self._quota_used_cache = entry.quota_used_bytes
            if entry.quota_available_bytes is not None:
                self._quota_available_cache = entry.quota_available_bytes
                return self._quota_available_cache
        self._quota_available_cache = None
        self._quota_used_cache = None
        return None

    def _invalidate_camera(self, camera_id: str) -> None:
        for key in list(self._records_cache):
            if key[0] == camera_id:
                self._records_cache.pop(key, None)

    async def _ensure_collection_parts(self, absolute_parts: tuple[str, ...]) -> None:
        if not absolute_parts:
            return
        for index in range(1, len(absolute_parts) + 1):
            parts = absolute_parts[:index]
            if parts in self._ensured_collections:
                continue
            response = await self._request(
                "MKCOL", self._url_for_absolute_parts(parts), expected={200, 201, 204, 405}
            )
            await response.aclose()
            self._ensured_collections.add(parts)

    async def _put(
        self,
        relative_path: str,
        chunks: Iterable[bytes],
        content_length: int,
        content_type: str,
        *,
        track_media: bool = False,
    ) -> None:
        source_chunks = 0

        async def body() -> AsyncIterator[bytes]:
            nonlocal source_chunks
            sent = 0
            for chunk in chunks:
                if not chunk:
                    continue
                source_chunks += 1
                sent += len(chunk)
                if sent > content_length:
                    raise StorageBackendError("WebDAV upload exceeded declared Content-Length")
                yield chunk
                await asyncio.sleep(0)
            if sent != content_length:
                raise StorageBackendError(
                    f"WebDAV upload produced {sent} bytes, expected {content_length}"
                )

        response = await self._request(
            "PUT",
            self._url(relative_path),
            expected={200, 201, 204},
            headers={"Content-Type": content_type, "Content-Length": str(content_length)},
            content=body(),
        )
        if track_media:
            self._last_media_upload_source_chunks = source_chunks
        await response.aclose()

    async def _move(self, source: str, destination: str) -> None:
        response = await self._request(
            "MOVE",
            self._url(source),
            expected={200, 201, 204, 405, 409, 412},
            headers={"Destination": self._url(destination), "Overwrite": "F"},
        )
        status = response.status_code
        await response.aclose()
        if status in {200, 201, 204}:
            return
        # A retry can see the destination already committed. Treat that as idempotent and
        # remove only the fresh partial source.
        if await self._exists(destination):
            await self._delete(source, ignore_missing=True)
            return
        raise StorageBackendError(f"WebDAV MOVE returned HTTP {status}")

    async def _delete(self, relative_path: str, *, ignore_missing: bool) -> None:
        response = await self._request(
            "DELETE",
            self._url(relative_path),
            expected={200, 202, 204, 404} if ignore_missing else {200, 202, 204},
        )
        await response.aclose()

    async def _best_effort_delete(self, relative_path: str) -> None:
        try:
            await self._delete(relative_path, ignore_missing=True)
        except (StorageBackendError, ValueError) as exc:
            logger.warning("failed to clean remote transaction object %s: %s", relative_path, exc)

    async def _exists(self, relative_path: str) -> bool:
        entries = await self._propfind(relative_path, depth="0", missing_ok=True)
        return bool(entries)

    async def _propfind(
        self, relative_path: str, *, depth: str, missing_ok: bool
    ) -> list[WebDAVEntry]:
        response = await self._request(
            "PROPFIND",
            self._url(relative_path),
            expected={207, 404} if missing_ok else {207},
            headers={"Depth": depth, "Content-Type": "application/xml; charset=utf-8"},
            content=_PROPFIND_BODY,
        )
        try:
            if response.status_code == 404:
                return []
            max_bytes = self.config.max_index_response_mb * 1024 * 1024
            if len(response.content) > max_bytes:
                raise StorageBackendError(
                    f"WebDAV PROPFIND response exceeds {self.config.max_index_response_mb} MiB"
                )
            return self._parse_propfind(response.content)
        finally:
            await response.aclose()

    def _parse_propfind(self, payload: bytes) -> list[WebDAVEntry]:
        try:
            root = ElementTree.fromstring(payload)
        except (ElementTree.ParseError, ValueError) as exc:
            raise StorageBackendError(f"invalid WebDAV PROPFIND XML: {exc}") from exc
        entries: list[WebDAVEntry] = []
        root_url_path = unquote(urlsplit(self._url("")).path).rstrip("/")
        for response in root.findall(f"{_DAV}response"):
            href_node = response.find(f"{_DAV}href")
            if href_node is None or not href_node.text:
                continue
            href_path = unquote(urlsplit(href_node.text).path).rstrip("/")
            if href_path == root_url_path:
                relative = ""
            elif href_path.startswith(root_url_path + "/"):
                relative = href_path[len(root_url_path) + 1 :]
            else:
                continue
            prop = None
            for propstat in response.findall(f"{_DAV}propstat"):
                status = propstat.findtext(f"{_DAV}status", default="")
                if " 200 " in status:
                    prop = propstat.find(f"{_DAV}prop")
                    break
            if prop is None:
                continue
            resource_type = prop.find(f"{_DAV}resourcetype")
            is_dir = (
                resource_type is not None and resource_type.find(f"{_DAV}collection") is not None
            )
            size = _parse_int(prop.findtext(f"{_DAV}getcontentlength")) or 0
            modified = _parse_http_datetime(prop.findtext(f"{_DAV}getlastmodified"))
            entries.append(
                WebDAVEntry(
                    relative_path=relative,
                    is_dir=is_dir,
                    size_bytes=size,
                    modified=modified,
                    quota_available_bytes=_parse_int(prop.findtext(f"{_DAV}quota-available-bytes")),
                    quota_used_bytes=_parse_int(prop.findtext(f"{_DAV}quota-used-bytes")),
                )
            )
        return entries

    async def _request(
        self,
        method: str,
        url: str,
        *,
        expected: set[int],
        **kwargs: object,
    ) -> httpx.Response:
        try:
            response = await self.client.request(method, url, **kwargs)
        except httpx.HTTPError as exc:
            raise StorageBackendError(
                f"WebDAV {method} failed: {type(exc).__name__}: {exc}"
            ) from exc
        if response.status_code not in expected:
            status = response.status_code
            detail = response.text[:300].replace("\n", " ").strip()
            await response.aclose()
            suffix = f": {detail}" if detail else ""
            raise StorageBackendError(f"WebDAV {method} returned HTTP {status}{suffix}")
        return response

    def _url(self, relative_path: str) -> str:
        relative = _safe_webdav_relative(relative_path)
        return self._url_for_absolute_parts((*self._root_parts, *relative.parts))

    def _url_for_absolute_parts(self, parts: tuple[str, ...]) -> str:
        parsed = urlsplit(self.config.url)
        encoded = "/".join(quote(part, safe="") for part in parts)
        path = self._base_path
        if encoded:
            path = f"{path}/{encoded}" if path else f"/{encoded}"
        return urlunsplit((parsed.scheme, parsed.netloc, path or "/", "", ""))


def create_storage_backend(
    storage: StorageConfig,
    camera_ids: Iterable[str],
    *,
    client: httpx.AsyncClient | None = None,
) -> StorageBackend:
    if storage.backend == "local":
        if client is not None:
            raise ValueError("an HTTP client can only be injected for the WebDAV backend")
        return LocalStorageBackend(storage)
    return WebDAVStorageBackend(storage, camera_ids, client=client)


def _safe_webdav_relative(value: str) -> PurePosixPath:
    raw = value.replace("\\", "/").strip("/")
    if not raw:
        return PurePosixPath()
    path = PurePosixPath(raw)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise ValueError("invalid WebDAV relative path")
    return path


def _safe_recording_relative(value: str) -> PurePosixPath:
    try:
        path = _safe_webdav_relative(value)
    except ValueError as exc:
        raise ValueError("invalid recording path") from exc
    if not path.parts or not value.lower().endswith((".ts", ".ts.enc")) or len(path.parts) != 5:
        raise ValueError("only partitioned MPEG-TS recording files may be served")
    if parse_archive_filename(path.name) is None:
        raise ValueError("invalid CamVault recording filename")
    return path


def _partial_name(relative_path: str) -> str:
    path = PurePosixPath(relative_path)
    # Dot-prefixed objects are considered hidden by AList and may require an additional
    # user permission. A suffix is equally unambiguous while remaining operable by a
    # least-privilege WebDAV account.
    return str(path.with_name(f"{path.name}.camvault-partial"))


def _filter_records(
    records: list[ArchiveRecord],
    *,
    start: datetime | None,
    end: datetime | None,
    limit: int | None,
) -> list[ArchiveRecord]:
    start = _utc_or_none(start)
    end = _utc_or_none(end)
    if start is not None:
        records = [record for record in records if record.end > start]
    if end is not None:
        records = [record for record in records if record.start < end]
    records.sort(key=lambda record: record.start)
    if limit is not None and len(records) > limit:
        records = records[-limit:]
    return records


def _utc_or_none(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _parse_int(value: str | None) -> int | None:
    try:
        return int(value) if value is not None and value.strip() else None
    except ValueError:
        return None


def _parse_http_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = email.utils.parsedate_to_datetime(value)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC)
    except (TypeError, ValueError, OverflowError):
        return None
