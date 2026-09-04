from __future__ import annotations

import asyncio
import logging
import re
import secrets
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from camvault.archive import ArchiveManager, ArchiveRecord
from camvault.buffer import LiveBuffer
from camvault.config import AppConfig, CameraConfig
from camvault.config_store import ConfigSnapshot, ConfigStore
from camvault.logging_setup import RecentLogHandler
from camvault.retention import RetentionResult
from camvault.runtime import CameraRuntime
from camvault.storage import (
    RemoteRead,
    StorageBackend,
    StorageBackendError,
    create_storage_backend,
)
from camvault.supervisor import SupervisorManager

logger = logging.getLogger(__name__)

_SEGMENT_STREAM_RE = re.compile(r"^segment_(?P<stream>.+)_[0-9]{8}T[0-9]{6}_[0-9]{6}\.ts$")


def _segment_stream_id(filename: str) -> str | None:
    match = _SEGMENT_STREAM_RE.fullmatch(filename)
    return match.group("stream") if match else None


class CamVaultService:
    def __init__(
        self,
        config: AppConfig,
        *,
        storage_backend: StorageBackend | None = None,
        config_path: str | Path | None = None,
        recent_logs: RecentLogHandler | None = None,
    ) -> None:
        config.validate_runtime_security()
        self.config = config
        self.camera_map: dict[str, CameraConfig] = {camera.id: camera for camera in config.cameras}
        self.runtimes: dict[str, CameraRuntime] = {
            camera.id: CameraRuntime(
                camera_id=camera.id,
                name=camera.name or camera.id,
                state="disabled" if not camera.enabled else "idle",
                detail="camera disabled" if not camera.enabled else "not started",
            )
            for camera in config.cameras
        }
        live_max_bytes = config.recording.max_live_memory_mb_per_camera * 1024 * 1024
        self.buffers: dict[str, LiveBuffer] = {
            camera.id: LiveBuffer(
                window_segments=config.recording.live_window_segments,
                max_bytes=live_max_bytes,
                default_duration=config.recording.hls_segment_seconds,
            )
            for camera in config.cameras
            if camera.enabled
        }
        enabled_ids = [camera.id for camera in config.cameras if camera.enabled]
        self.storage_backend = storage_backend or create_storage_backend(
            config.storage, [camera.id for camera in config.cameras]
        )
        # Acquired by the HTTP route before reading a request body. This prevents stale
        # FFmpeg connections from accumulating multiple full segment payloads per camera.
        self.upload_locks = {camera_id: asyncio.Lock() for camera_id in enabled_ids}
        self.archive_manager = ArchiveManager(
            camera_ids=enabled_ids,
            storage=config.storage,
            writer=self.storage_backend.write_batch,
            on_written=self._on_archive_written,
            on_error=self._on_archive_error,
        )
        self.ingest_secret = secrets.token_urlsafe(32)
        self.playback_token = config.server.resolved_playback_token()
        self._last_stream_ids: dict[str, str] = {}
        self.supervisors = SupervisorManager(
            config=config,
            runtimes=self.runtimes,
            ingest_secret=self.ingest_secret,
            on_stream_end=self._on_stream_end,
        )
        self._retention_task: asyncio.Task[None] | None = None
        self._retention_lock = asyncio.Lock()
        self._retention_wakeup = asyncio.Event()
        self._started = False
        self._supervisors_started = False
        self.config_store = ConfigStore(config_path) if config_path is not None else None
        self.recent_logs = recent_logs
        self.last_retention: RetentionResult | None = None
        self.last_retention_started_at: datetime | None = None
        self.last_retention_completed_at: datetime | None = None
        self.last_retention_reason: str | None = None
        self.last_retention_error: str | None = None
        self.retention_runs = 0

    async def start(self, *, start_supervisors: bool = True) -> None:
        if not self._started:
            backend_started = False
            manager_started = False
            try:
                await self.storage_backend.start()
                backend_started = True
                await self.archive_manager.start()
                manager_started = True
                self._retention_task = asyncio.create_task(self._retention_loop(), name="retention")
                self._started = True
            except Exception:
                if self._retention_task is not None:
                    self._retention_task.cancel()
                    await asyncio.gather(self._retention_task, return_exceptions=True)
                    self._retention_task = None
                if manager_started:
                    await self.archive_manager.stop()
                if backend_started:
                    await self.storage_backend.close()
                raise
        if start_supervisors and not self._supervisors_started:
            self.supervisors.start()
            self._supervisors_started = True

    async def start_supervisors_after(self, delay_seconds: float = 0.25) -> None:
        await asyncio.sleep(delay_seconds)
        if self._started and not self._supervisors_started:
            self.supervisors.start()
            self._supervisors_started = True

    async def stop(self) -> None:
        if not self._started:
            return
        if self._supervisors_started:
            await self.supervisors.stop()
            self._supervisors_started = False
        if self._retention_task is not None:
            self._retention_task.cancel()
            await asyncio.gather(self._retention_task, return_exceptions=True)
            self._retention_task = None
        await self.archive_manager.stop()
        await self.storage_backend.close()
        self._started = False

    async def ingest_upload(self, camera_id: str, filename: str, payload: bytes) -> str:
        camera = self.camera_map.get(camera_id)
        if camera is None:
            raise KeyError(camera_id)
        if not camera.enabled:
            raise ValueError("camera is disabled")
        buffer = self.buffers[camera_id]
        if filename.endswith(".m3u8"):
            buffer.apply_playlist(payload.decode("utf-8", errors="replace"))
            return "playlist"
        if not filename.endswith(".ts"):
            raise ValueError("only .ts segments and .m3u8 playlists are accepted")

        stream_id = _segment_stream_id(filename)
        previous_stream_id = self._last_stream_ids.get(camera_id)
        if (
            stream_id is not None
            and previous_stream_id is not None
            and stream_id != previous_stream_id
        ):
            await self.archive_manager.rotate_camera(camera_id)

        segment = buffer.add_segment(
            name=filename,
            data=payload,
            created_at=datetime.now(UTC),
            stream_id=stream_id,
        )
        if segment is None:
            return "duplicate"
        if stream_id is not None:
            self._last_stream_ids[camera_id] = stream_id
        runtime = self.runtimes[camera_id]
        runtime.bytes_ingested += len(payload)
        runtime.segments_ingested += 1
        runtime.last_segment_at = segment.created_at
        runtime.state = "recording"
        runtime.detail = "receiving media"
        runtime.last_error = None
        runtime.touch()
        await self.archive_manager.add(camera_id, segment)
        return "segment"

    def upload_lock(self, camera_id: str) -> asyncio.Lock:
        if camera_id not in self.camera_map:
            raise KeyError(camera_id)
        try:
            return self.upload_locks[camera_id]
        except KeyError as exc:
            raise ValueError("camera is disabled") from exc

    def live_buffer(self, camera_id: str) -> LiveBuffer:
        try:
            return self.buffers[camera_id]
        except KeyError as exc:
            if camera_id not in self.camera_map:
                raise KeyError(camera_id) from exc
            raise ValueError("camera is disabled") from exc

    async def archive_records(
        self,
        camera_id: str,
        *,
        start: datetime | None = None,
        end: datetime | None = None,
        limit: int | None = None,
    ) -> list[ArchiveRecord]:
        if camera_id not in self.camera_map:
            raise KeyError(camera_id)
        return await self.storage_backend.list_records(camera_id, start=start, end=end, limit=limit)

    def recording_path(self, camera_id: str, relative_path: str) -> Path:
        """Return a local path; retained for API compatibility with local deployments."""

        if camera_id not in self.camera_map:
            raise KeyError(camera_id)
        path = self.storage_backend.local_recording_path(camera_id, relative_path)
        if path is None:
            raise ValueError("recording is stored on a remote backend")
        return path

    def local_recording_path(self, camera_id: str, relative_path: str) -> Path | None:
        if camera_id not in self.camera_map:
            raise KeyError(camera_id)
        return self.storage_backend.local_recording_path(camera_id, relative_path)

    async def open_remote_recording(
        self, camera_id: str, relative_path: str, range_header: str | None
    ) -> RemoteRead | None:
        if camera_id not in self.camera_map:
            raise KeyError(camera_id)
        return await self.storage_backend.open_remote_recording(
            camera_id, relative_path, range_header
        )

    async def flush_archives(self) -> None:
        await self.archive_manager.flush_all()

    async def read_config(self) -> ConfigSnapshot:
        if self.config_store is None:
            raise ValueError("configuration file management is unavailable")
        return await asyncio.to_thread(self.config_store.read)

    async def write_config(self, content: str, *, expected_revision: str | None) -> ConfigSnapshot:
        if self.config_store is None:
            raise ValueError("configuration file management is unavailable")
        snapshot = await asyncio.to_thread(
            self.config_store.write,
            content,
            expected_revision=expected_revision,
        )
        logger.warning(
            "configuration updated through the control console; restart required to apply it"
        )
        return snapshot

    def log_entries(self, *, after: int = 0, limit: int = 500) -> list[dict[str, object]]:
        if self.recent_logs is None:
            return []
        return self.recent_logs.snapshot(after=after, limit=limit)

    async def run_retention(self, *, reason: str = "manual") -> RetentionResult:
        async with self._retention_lock:
            self.last_retention_started_at = datetime.now(UTC)
            self.last_retention_reason = reason
            self.last_retention_error = None
            try:
                result = await self.storage_backend.retention()
            except asyncio.CancelledError:
                raise
            except (OSError, ValueError, StorageBackendError) as exc:
                self.last_retention_error = str(exc)
                logger.exception("retention run failed (reason=%s)", reason)
                raise
            self.last_retention = result
            self.last_retention_completed_at = datetime.now(UTC)
            self.retention_runs += 1
            log = logger.warning if result.deleted_files else logger.info
            log(
                "retention completed (reason=%s, deleted_files=%d, deleted_bytes=%d, "
                "remaining_bytes=%d, free_bytes=%s)",
                reason,
                result.deleted_files,
                result.deleted_bytes,
                result.remaining_bytes,
                result.free_bytes,
            )
            return result

    def status(self) -> dict[str, Any]:
        live_bytes = sum(buffer.total_bytes for buffer in self.buffers.values())
        archive_buffer_bytes = self.archive_manager.memory_bytes()
        storage_status = self.storage_backend.status()
        return {
            "status": "ok",
            "storage": storage_status,
            # Kept for 0.1 clients. Remote mode intentionally has no local storage root.
            "storage_root": (
                str(self.config.storage.root) if self.config.storage.backend == "local" else None
            ),
            "live_memory_bytes": live_bytes,
            "archive_buffer_bytes": archive_buffer_bytes,
            "bounded_media_memory_bytes": live_bytes + archive_buffer_bytes,
            "retention": {
                "policy": {
                    "retention_days": self.config.storage.retention_days,
                    "max_storage_gb": self.config.storage.max_storage_gb,
                    "min_free_gb": self.config.storage.min_free_gb,
                    "check_seconds": self.config.storage.retention_check_seconds,
                },
                "runs": self.retention_runs,
                "last_reason": self.last_retention_reason,
                "last_started_at": (
                    self.last_retention_started_at.isoformat()
                    if self.last_retention_started_at
                    else None
                ),
                "last_completed_at": (
                    self.last_retention_completed_at.isoformat()
                    if self.last_retention_completed_at
                    else None
                ),
                "last_error": self.last_retention_error,
                "deleted_files": self.last_retention.deleted_files if self.last_retention else 0,
                "deleted_bytes": self.last_retention.deleted_bytes if self.last_retention else 0,
                "remaining_bytes": (
                    self.last_retention.remaining_bytes if self.last_retention else None
                ),
                "free_bytes": self.last_retention.free_bytes if self.last_retention else None,
            },
            "cameras": [runtime.as_dict() for runtime in self.runtimes.values()],
        }

    async def _on_stream_end(self, camera_id: str) -> None:
        # Seal a short tail immediately. It is enqueued but not synchronously persisted, so
        # reconnect remains responsive while the archive worker preserves ordering.
        await self.archive_manager.rotate_camera(camera_id)

    def _on_archive_written(self, record: ArchiveRecord) -> None:
        runtime = self.runtimes[record.camera_id]
        runtime.archive_files += 1
        runtime.archive_bytes += record.size_bytes
        runtime.last_archive_at = datetime.now(UTC)
        runtime.touch()

    def _on_archive_error(self, camera_id: str, message: str) -> None:
        runtime = self.runtimes[camera_id]
        runtime.last_error = message
        runtime.detail = message
        runtime.touch()
        # A full local disk or exhausted remote quota often surfaces first as an archive
        # write error. Wake cleanup immediately instead of waiting for the hourly timer.
        self._retention_wakeup.set()

    async def _retention_loop(self) -> None:
        reason = "startup"
        while True:
            self._retention_wakeup.clear()
            try:
                await self.run_retention(reason=reason)
            except asyncio.CancelledError:
                raise
            except (OSError, ValueError, StorageBackendError):
                # run_retention records the error. Keep the scheduler alive so a temporary
                # WebDAV or filesystem failure cannot permanently disable cleanup.
                reason = "retry-after-error"
            try:
                await asyncio.wait_for(
                    self._retention_wakeup.wait(),
                    timeout=self.config.storage.retention_check_seconds,
                )
                reason = "archive-write-failure"
            except TimeoutError:
                reason = "scheduled"
