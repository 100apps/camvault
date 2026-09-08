from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import shutil
import time
from collections.abc import AsyncIterator, Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import BinaryIO

import httpx

from camvault.archive import ArchiveBatch, ArchiveRecord, parse_archive_filename
from camvault.config import StorageConfig
from camvault.crypto import (
    ARCHIVE_ENCRYPTION_HEADER_BYTES,
    ARCHIVE_ENCRYPTION_TAG_BYTES,
    decrypt_archive_chunk,
    encrypted_chunk_offset,
    parse_encryption_header,
    plaintext_chunk_size,
)
from camvault.retention import RetentionResult
from camvault.security import redact_text
from camvault.storage import (
    PreparedArchive,
    RemoteRead,
    StorageBackend,
    StorageBackendError,
    StorageHealth,
    WebDAVStorageBackend,
    _empty_remote_body,
    _filter_records,
    _no_remote_close,
    _resolve_byte_range,
    _safe_recording_relative,
    prepare_archive,
)

logger = logging.getLogger(__name__)
_ENTRY_ID = re.compile(r"^[0-9a-f]{64}$")
_CAMERA_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
_GIB = 1024**3


class SpoolWriteError(StorageBackendError):
    # Deleting cloud recordings cannot free a full/unwritable local outbox.
    allow_reclaim = False


class SpoolCorruptionError(SpoolWriteError):
    pass


def _sync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _write_chunks(path: Path, chunks: Iterable[bytes]) -> tuple[int, str]:
    count = 0
    digest = hashlib.sha256()
    with path.open("xb") as stream:
        os.chmod(path, 0o600)
        for chunk in chunks:
            stream.write(chunk)
            count += len(chunk)
            digest.update(chunk)
        stream.flush()
        os.fsync(stream.fileno())
    return count, digest.hexdigest()


async def _file_chunks(path: Path, expected_hash: str) -> AsyncIterator[bytes]:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := await asyncio.to_thread(stream.read, 128 * 1024):
            digest.update(chunk)
            yield chunk
    if digest.hexdigest() != expected_hash:
        raise SpoolCorruptionError(f"spool checksum mismatch: {path.name}")


@dataclass(frozen=True, slots=True)
class SpoolEntry:
    directory: Path
    camera_id: str
    relative_path: str
    plaintext_size: int
    media_size: int
    metadata_size: int
    media_hash: str
    metadata_hash: str

    @property
    def identifier(self) -> str:
        return hashlib.sha256(f"{self.camera_id}/{self.relative_path}".encode()).hexdigest()

    @property
    def disk_bytes(self) -> int:
        return (
            self.media_size
            + self.metadata_size
            + self.directory.joinpath("entry.json").stat().st_size
        )

    def record(self) -> ArchiveRecord:
        parsed = parse_archive_filename(Path(self.relative_path).name)
        assert parsed is not None
        return ArchiveRecord(
            camera_id=self.camera_id,
            path=None,
            relative_path=self.relative_path,
            start=parsed.start,
            end=parsed.start + timedelta(seconds=parsed.duration),
            duration=parsed.duration,
            size_bytes=self.plaintext_size,
            stream_id=parsed.stream_id,
            audio_index=parsed.audio_index,
            encrypted=parsed.encrypted,
        )


class SpoolingStorageBackend(StorageBackend):
    """Durable outbox for WebDAV: commit locally, replay unchanged bytes, then remove."""

    kind = "webdav"
    diskless_media_path = False

    def __init__(
        self,
        storage: StorageConfig,
        camera_ids: Iterable[str],
        *,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.storage = storage
        self.remote = WebDAVStorageBackend(storage, camera_ids, client=client)
        self.location = self.remote.location
        self.directory = (
            (storage.spool_directory or storage.root.expanduser().resolve().parent / "spool")
            .expanduser()
            .resolve()
        )
        if self.directory.parent == self.directory:
            raise ValueError("spool_directory must be a dedicated non-root directory")
        self._lock = asyncio.Lock()
        self._owner: BinaryIO | None = None
        self._worker: asyncio.Task[None] | None = None
        self._entries: dict[str, SpoolEntry] = {}
        self._blocked: set[str] = set()
        self._wake = asyncio.Event()
        self._empty = asyncio.Event()
        self._empty.set()
        self._used_bytes = 0
        self._orphan_bytes = 0
        self._last_error: str | None = None
        self._write_error: str | None = None
        self._last_success: float | None = None
        self._next_retry = 0.0
        self._draining = False
        self._uploaded = 0
        self._uploaded_bytes = 0

    def _target_fingerprint(self) -> str:
        config = self.storage.webdav
        key = config.resolved_encryption_key() if config.encryption_enabled else None
        if config.encryption_enabled and key is None:
            raise SpoolWriteError("archive encryption key is required for the disk outbox")
        # No password/key is persisted. Refuse to send old footage to a changed account,
        # destination or encryption key after a configuration edit.
        identity = [config.url, config.root, config.resolved_username(), (key or b"").hex()]
        return hashlib.sha256(json.dumps(identity).encode()).hexdigest()

    def _open(self) -> None:
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.directory, 0o700)
        lock_path = self.directory / ".lock"
        if lock_path.is_symlink() or (self.directory / ".target.json").is_symlink():
            raise SpoolWriteError("spool control files must not be symlinks")
        self._owner = lock_path.open("a+b")
        os.chmod(lock_path, 0o600)
        try:
            if os.name == "nt":
                import msvcrt

                self._owner.write(b"0")
                self._owner.flush()
                self._owner.seek(0)
                msvcrt.locking(self._owner.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(self._owner.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            target = self.directory / ".target.json"
            expected = self._target_fingerprint()
            if target.exists():
                if json.loads(target.read_text())["fingerprint"] != expected:
                    raise SpoolWriteError(
                        "spool destination/account/key changed; keep the original configuration or use a new spool_directory"
                    )
            else:
                if any(path.name != ".lock" for path in self.directory.iterdir()):
                    raise SpoolWriteError(
                        "refusing to adopt a non-empty, unrecognized spool_directory"
                    )
                _write_chunks(target, [json.dumps({"fingerprint": expected}).encode()])
                _sync_directory(self.directory)
            self._entries.clear()
            self._blocked.clear()
            self._used_bytes = self._orphan_bytes = 0
            for directory in self.directory.iterdir():
                if directory.name.endswith(".committed") and directory.name.startswith("."):
                    # Both remote objects were committed before this directory was renamed.
                    self._cleanup_committed(directory)
                    continue
                if directory.name.startswith(".") and not directory.name.endswith(".partial"):
                    continue
                if directory.is_symlink() or not directory.is_dir():
                    raise SpoolWriteError("unexpected entry in dedicated spool_directory")
                try:
                    entry = self._load_entry(directory)
                    if directory.name.endswith(".partial"):
                        destination = self.directory / entry.identifier
                        if destination.exists():
                            raise SpoolCorruptionError("duplicate incomplete spool transaction")
                        directory.rename(destination)
                        _sync_directory(self.directory)
                        entry = self._load_entry(destination)
                    elif directory.name != entry.identifier:
                        raise SpoolCorruptionError("spool identity mismatch")
                except (OSError, ValueError, KeyError, TypeError, SpoolCorruptionError) as exc:
                    # Keep damaged/incomplete files for recovery; never delete unuploaded footage.
                    self._last_error = (
                        f"incomplete/corrupt spool entry retained: {directory.name}: {exc}"
                    )
                    self._orphan_bytes += sum(
                        child.stat().st_size
                        for child in directory.iterdir()
                        if child.is_file() and not child.is_symlink()
                    )
                    logger.error(self._last_error)
                    continue
                self._entries[entry.identifier] = entry
                self._used_bytes += entry.disk_bytes
            self._used_bytes += self._orphan_bytes
        except BaseException:
            self._owner.close()
            self._owner = None
            raise

    def _load_entry(self, directory: Path) -> SpoolEntry:
        manifest = directory / "entry.json"
        if manifest.is_symlink() or manifest.stat().st_size > 16 * 1024:
            raise SpoolCorruptionError("invalid spool manifest")
        data = json.loads(manifest.read_text(encoding="utf-8"))
        if not _CAMERA_ID.fullmatch(data["camera_id"]):
            raise SpoolCorruptionError("invalid camera in spool manifest")
        _safe_recording_relative(data["relative_path"])
        for name in ("media_hash", "metadata_hash"):
            if not _ENTRY_ID.fullmatch(data[name]):
                raise SpoolCorruptionError("invalid spool checksum")
        for name, field in (("media", "media_size"), ("metadata", "metadata_size")):
            path = directory / name
            if path.is_symlink() or data[field] <= 0 or path.stat().st_size != data[field]:
                raise SpoolCorruptionError("incomplete spool file")
        if data["plaintext_size"] <= 0:
            raise SpoolCorruptionError("invalid spool plaintext size")
        return SpoolEntry(directory=directory, **data)

    async def start(self) -> None:
        async with self._lock:
            if self._owner is not None:
                return
            await asyncio.to_thread(self._open)
            self._draining = False
            self._next_retry = 0.0
            if self._entries:
                self._empty.clear()
            self._worker = asyncio.create_task(self._replay(), name="webdav-spool-replay")

    def _persist(self, batch: ArchiveBatch) -> tuple[SpoolEntry, ArchiveRecord]:
        prepared = prepare_archive(batch, self.storage)
        record = prepared.record
        identifier = hashlib.sha256(
            f"{record.camera_id}/{record.relative_path}".encode()
        ).hexdigest()
        destination = self.directory / identifier
        if destination.exists():
            _sync_directory(destination)
            _sync_directory(self.directory)
            return self._load_entry(destination), record
        needed = prepared.media_size + prepared.metadata_size + 16 * 1024
        free = shutil.disk_usage(self.directory).free
        if self._used_bytes + needed > self.storage.spool_max_gb * _GIB:
            raise SpoolWriteError(
                "disk outbox capacity reached; preserving unuploaded recordings and backpressuring ingest"
            )
        if free - needed < self.storage.spool_min_free_gb * _GIB:
            raise SpoolWriteError(
                "disk free-space reserve reached; preserving unuploaded recordings and backpressuring ingest"
            )
        partial = self.directory / f"{identifier}.partial"
        if partial.exists():
            # A previous interrupted write is retained; don't overwrite its evidence.
            raise SpoolWriteError("incomplete spool transaction requires recovery before retry")
        partial.mkdir(mode=0o700)
        try:
            media_size, media_hash = _write_chunks(partial / "media", prepared.media_chunks)
            metadata_size, metadata_hash = _write_chunks(
                partial / "metadata", prepared.metadata_chunks
            )
            if (media_size, metadata_size) != (prepared.media_size, prepared.metadata_size):
                raise SpoolWriteError("spool write produced an unexpected size")
            manifest = {
                "camera_id": record.camera_id,
                "relative_path": record.relative_path,
                "plaintext_size": record.size_bytes,
                "media_size": media_size,
                "metadata_size": metadata_size,
                "media_hash": media_hash,
                "metadata_hash": metadata_hash,
            }
            _write_chunks(partial / "entry.json", [json.dumps(manifest).encode()])
            _sync_directory(partial)
            partial.rename(destination)
            _sync_directory(self.directory)
            return SpoolEntry(directory=destination, **manifest), record
        except OSError as exc:
            # The complete source batch is still held by ArchiveManager. Remove only
            # this failed attempt's incomplete copies, so a freed disk can be retried.
            if partial.exists():
                for name in ("entry.json", "metadata", "media"):
                    (partial / name).unlink(missing_ok=True)
                partial.rmdir()
            raise SpoolWriteError(f"cannot persist disk outbox: {exc}") from exc

    async def write_batch(self, batch: ArchiveBatch) -> ArchiveRecord:
        try:
            await self.start()
            async with self._lock:
                task = asyncio.create_task(asyncio.to_thread(self._persist, batch))
                try:
                    entry, record = await asyncio.shield(task)
                except asyncio.CancelledError:
                    # A thread cannot be canceled safely mid-fsync. Finish registering its
                    # durable result; restart scanning also recovers a just-renamed entry.
                    entry, _record = await task
                    self._register(entry)
                    raise
                self._register(entry)
                self._write_error = None
                return record
        except (OSError, ValueError, StorageBackendError) as exc:
            self._write_error = f"cannot persist disk outbox: {exc}"
            raise SpoolWriteError(self._write_error) from exc

    def _register(self, entry: SpoolEntry) -> None:
        if entry.identifier not in self._entries:
            self._entries[entry.identifier] = entry
            self._used_bytes += entry.disk_bytes
        self._empty.clear()
        self._wake.set()

    def _remove_committed(self, entry: SpoolEntry) -> None:
        # Rename before deletion so a crash during cleanup cannot replay half a pair.
        committed = self.directory / f".{entry.identifier}.committed"
        entry.directory.rename(committed)
        try:
            _sync_directory(self.directory)
            self._cleanup_committed(committed)
        except OSError:
            # The remote pair is already durable. Keep the committed marker for startup
            # cleanup (e.g. Windows may still have a playback reader holding the file).
            logger.warning("uploaded outbox entry awaits local cleanup: %s", committed.name)

    def _cleanup_committed(self, committed: Path) -> None:
        if committed.is_symlink() or not _ENTRY_ID.fullmatch(committed.name[1:-10]):
            raise SpoolCorruptionError("invalid committed outbox directory")
        if any(
            child.name not in {"media", "metadata", "entry.json"} or child.is_symlink()
            for child in committed.iterdir()
        ):
            raise SpoolCorruptionError("unexpected file in committed outbox directory")
        for name in ("media", "metadata", "entry.json"):
            (committed / name).unlink(missing_ok=True)
        committed.rmdir()
        _sync_directory(self.directory)

    async def _replay(self) -> None:
        failures = 0
        while True:
            self._wake.clear()
            candidates = [entry for key, entry in self._entries.items() if key not in self._blocked]
            delay = self._next_retry - time.monotonic()
            if not candidates or delay > 0:
                try:
                    await asyncio.wait_for(
                        self._wake.wait(), timeout=max(0.01, delay) if candidates else None
                    )
                except TimeoutError:
                    pass
                continue
            entry = min(candidates, key=lambda item: (item.relative_path, item.camera_id))
            media = _file_chunks(entry.directory / "media", entry.media_hash)
            metadata = _file_chunks(entry.directory / "metadata", entry.metadata_hash)
            try:
                async with asyncio.timeout(self.storage.spool_upload_timeout_seconds):
                    await self.remote.write_prepared(
                        PreparedArchive(
                            entry.record(), media, metadata, entry.media_size, entry.metadata_size
                        )
                    )
                async with self._lock:
                    size = entry.disk_bytes
                    await asyncio.to_thread(self._remove_committed, entry)
                    self._entries.pop(entry.identifier)
                    self._used_bytes -= size
                self._uploaded += 1
                self._uploaded_bytes += entry.media_size + entry.metadata_size
                self._last_success = time.time()
                self._last_error = None
                self._next_retry = 0.0
                failures = 0
                if not self._entries:
                    self._empty.set()
                logger.info(
                    "disk outbox uploaded %s; %d pending", entry.camera_id, len(self._entries)
                )
            except SpoolCorruptionError as exc:
                self._blocked.add(entry.identifier)
                self._last_error = str(exc)
                logger.error("corrupt spool entry retained: %s", entry.identifier)
            except Exception as exc:  # noqa: BLE001 - keep recording through any remote I/O failure
                self.remote._started = False
                self.remote._ensured_collections.clear()
                failures += 1
                # Cloud capacity recovery remains available after uploads moved out of
                # ArchiveManager. Never delete history for local I/O, auth or outages.
                if (
                    failures == 1
                    and not self._draining
                    and self.storage.write_failure_policy == "delete_oldest"
                    and isinstance(exc, StorageBackendError)
                    and exc.status_code == 507
                ):
                    try:
                        async with asyncio.timeout(self.storage.spool_upload_timeout_seconds):
                            if self.on_reclaim is not None:
                                await self.on_reclaim(entry.camera_id, entry.plaintext_size)
                            else:
                                await self.retention(
                                    emergency_min_delete_bytes=max(
                                        entry.plaintext_size,
                                        self.storage.write_failure_reclaim_mb * 1024**2,
                                    ),
                                    emergency_max_delete_files=self.storage.write_failure_max_delete_files,
                                )
                    except Exception:  # noqa: BLE001 - preserve the original failed batch
                        logger.warning("WebDAV quota reclaim failed; keeping disk outbox")
                self._last_error = redact_text(
                    str(exc) or type(exc).__name__, (self.storage.webdav.resolved_password() or "",)
                )
                self._next_retry = time.monotonic() + (
                    2.0
                    if self._draining
                    else min(300.0, self.storage.spool_retry_seconds * 2 ** min(failures - 1, 4))
                )
                logger.warning(
                    "WebDAV unavailable; %d batches retained on disk: %s",
                    len(self._entries),
                    self._last_error,
                )
            finally:
                await media.aclose()
                await metadata.aclose()

    async def drain_uploads(self) -> None:
        if not self._entries:
            return
        self._draining = True
        self._next_retry = 0.0
        self._wake.set()
        await self._empty.wait()

    def pending_uploads(self) -> int:
        return len(self._entries)

    async def close(self) -> None:
        if self._worker is not None:
            self._worker.cancel()
            await asyncio.gather(self._worker, return_exceptions=True)
            self._worker = None
        await self.remote.close()
        if self._owner is not None:
            self._owner.close()
            self._owner = None

    async def health_check(self) -> StorageHealth:
        # Diagnostics may run beside the recorder. Do not acquire its exclusive lock,
        # start another replay worker, or take ownership of pending footage.
        parent = self.directory
        while not parent.exists():
            parent = parent.parent
        if not parent.is_dir() or not os.access(parent, os.W_OK | os.X_OK):
            raise SpoolWriteError("disk outbox parent directory is not writable")
        self._target_fingerprint()
        health = await self.remote.health_check()
        return StorageHealth(
            self.kind, self.location, health.detail + "; durable disk outbox configured", False
        )

    async def list_records(
        self,
        camera_id: str,
        *,
        start: datetime | None = None,
        end: datetime | None = None,
        limit: int | None = None,
    ) -> list[ArchiveRecord]:
        local = [entry.record() for entry in self._entries.values() if entry.camera_id == camera_id]
        remote: list[ArchiveRecord] = []
        if time.monotonic() >= self._next_retry:
            try:
                async with asyncio.timeout(min(5.0, self.storage.spool_upload_timeout_seconds)):
                    remote = await self.remote.list_records(
                        camera_id, start=start, end=end, limit=limit
                    )
            except (StorageBackendError, TimeoutError):
                if not local:
                    raise
        records = {record.relative_path: record for record in remote}
        records.update({record.relative_path: record for record in local})
        return _filter_records(list(records.values()), start=start, end=end, limit=limit)

    async def open_remote_recording(
        self, camera_id: str, relative_path: str, range_header: str | None
    ) -> RemoteRead | None:
        _safe_recording_relative(relative_path)
        identifier = hashlib.sha256(f"{camera_id}/{relative_path}".encode()).hexdigest()
        async with self._lock:
            entry = self._entries.get(identifier)
            if entry is not None:
                stream = (entry.directory / "media").open("rb")
            else:
                stream = None
        if stream is None:
            return await self.remote.open_remote_recording(camera_id, relative_path, range_header)
        return await self._local_read(stream, entry, range_header)

    async def _local_read(
        self, stream: BinaryIO, entry: SpoolEntry, range_header: str | None
    ) -> RemoteRead:
        try:
            encrypted = entry.relative_path.endswith(".enc")
            header = (
                parse_encryption_header(stream.read(ARCHIVE_ENCRYPTION_HEADER_BYTES))
                if encrypted
                else None
            )
            size = header.plaintext_size if header else entry.plaintext_size
            selected = _resolve_byte_range(range_header, size)
            if selected is None:
                stream.close()
                return RemoteRead(
                    416,
                    {"content-range": f"bytes */{size}", "content-length": "0"},
                    _empty_remote_body(),
                    _no_remote_close,
                )
            status, start, end = selected
            key = self.storage.webdav.resolved_encryption_key() if encrypted else None

            async def close() -> None:
                stream.close()

            async def body():
                try:
                    if header:
                        assert key is not None
                        first, last = start // header.chunk_size, end // header.chunk_size
                        for index in range(first, last + 1):
                            stream.seek(encrypted_chunk_offset(header, index))
                            cipher = await asyncio.to_thread(
                                stream.read,
                                plaintext_chunk_size(header, index) + ARCHIVE_ENCRYPTION_TAG_BYTES,
                            )
                            plain = decrypt_archive_chunk(
                                cipher,
                                header=header,
                                index=index,
                                key=key,
                                context=f"{entry.camera_id}/{entry.relative_path}",
                            )
                            offset = index * header.chunk_size
                            yield plain[max(0, start - offset) : min(len(plain), end - offset + 1)]
                    else:
                        stream.seek(start)
                        remaining = end - start + 1
                        while remaining:
                            chunk = await asyncio.to_thread(stream.read, min(128 * 1024, remaining))
                            if not chunk:
                                raise SpoolCorruptionError("truncated local spool recording")
                            remaining -= len(chunk)
                            yield chunk
                finally:
                    stream.close()

            headers = {
                "content-length": str(end - start + 1),
                "content-type": "video/mp2t",
                "accept-ranges": "bytes",
            }
            if status == 206:
                headers["content-range"] = f"bytes {start}-{end}/{size}"
            return RemoteRead(status, headers, body(), close)
        except BaseException:
            stream.close()
            raise

    async def retention(
        self,
        *,
        now_epoch: float | None = None,
        emergency_min_delete_bytes: int = 0,
        emergency_max_delete_files: int | None = None,
    ) -> RetentionResult:
        # Unuploaded local entries are never subject to the cloud's age/size deletion.
        return await self.remote.retention(
            now_epoch=now_epoch,
            emergency_min_delete_bytes=emergency_min_delete_bytes,
            emergency_max_delete_files=emergency_max_delete_files,
        )

    def status(self) -> dict[str, object]:
        try:
            usage = shutil.disk_usage(self.directory)
            disk = {"total_bytes": usage.total, "used_bytes": usage.used, "free_bytes": usage.free}
        except OSError:
            disk = {"total_bytes": None, "used_bytes": None, "free_bytes": None}
        return self.remote.status() | {
            "diskless_media_path": False,
            "local_media_spool": True,
            "spool": {
                "directory": str(self.directory),
                "pending_batches": len(self._entries),
                "pending_bytes": self._used_bytes,
                "orphan_bytes": self._orphan_bytes,
                "blocked_batches": len(self._blocked),
                "max_bytes": int(self.storage.spool_max_gb * _GIB),
                "min_free_bytes": int(self.storage.spool_min_free_gb * _GIB),
                "uploaded_batches": self._uploaded,
                "uploaded_bytes": self._uploaded_bytes,
                "last_error": self._last_error,
                "write_error": self._write_error,
                "last_upload_at": self._last_success,
                "retry_in_seconds": max(0, round(self._next_retry - time.monotonic(), 1)),
                "disk": disk,
            },
        }
