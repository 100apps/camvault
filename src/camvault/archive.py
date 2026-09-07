from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import os
import re
import secrets
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath
from zoneinfo import ZoneInfo

from camvault.buffer import LiveSegment
from camvault.config import StorageConfig

logger = logging.getLogger(__name__)

_MIB = 1024 * 1024


def available_memory_bytes() -> int | None:
    """Return an inexpensive cross-platform estimate of available physical memory."""

    meminfo = Path("/proc/meminfo")
    try:
        if meminfo.is_file():
            for line in meminfo.read_text(encoding="ascii").splitlines():
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) * 1024
    except (OSError, ValueError, IndexError):
        pass

    if os.name == "nt":
        try:
            import ctypes

            class _MemoryStatusEx(ctypes.Structure):
                _fields_ = [
                    ("length", ctypes.c_ulong),
                    ("memory_load", ctypes.c_ulong),
                    ("total_physical", ctypes.c_ulonglong),
                    ("available_physical", ctypes.c_ulonglong),
                    ("total_page_file", ctypes.c_ulonglong),
                    ("available_page_file", ctypes.c_ulonglong),
                    ("total_virtual", ctypes.c_ulonglong),
                    ("available_virtual", ctypes.c_ulonglong),
                    ("available_extended_virtual", ctypes.c_ulonglong),
                ]

            status = _MemoryStatusEx()
            status.length = ctypes.sizeof(status)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
                return int(status.available_physical)
        except (AttributeError, OSError, ValueError):
            pass

    try:
        pages = os.sysconf("SC_AVPHYS_PAGES")
        page_size = os.sysconf("SC_PAGE_SIZE")
        if pages > 0 and page_size > 0:
            return int(pages * page_size)
    except (AttributeError, OSError, ValueError):
        pass
    return None


_ARCHIVE_FILENAME_RE = re.compile(
    r"^(?P<stamp>\d{8}T\d{6}[+-]\d{4})_"
    r"(?P<sequence>\d{12})_"
    r"(?P<duration>\d{9})ms_"
    r"(?P<stream>s(?:none|[0-9a-f]{12}))_"
    r"(?:(?P<audio_step>a\d{1,4})x(?P<audio_bits>[0-9a-f]{1,64})_)?"
    r"(?P<object>[0-9a-f]{12})\.ts(?P<encrypted>\.enc)?$"
)

# CamVault 0.1 filenames did not include a stream tag or stable object id. Keep parsing
# them so an in-place 0.1 -> 0.2 upgrade does not make existing local recordings
# inaccessible through the playback route.
_ARCHIVE_FILENAME_V1_RE = re.compile(
    r"^(?P<stamp>\d{8}T\d{6}[+-]\d{4})_"
    r"(?P<sequence>\d{12})_"
    r"(?P<duration>\d{9})ms_"
    r"(?P<object>[0-9a-f]{6})\.ts$"
)


@dataclass(frozen=True, slots=True)
class ArchiveBatch:
    camera_id: str
    segments: tuple[LiveSegment, ...]
    # Stable across retries, so a timeout after a successful remote commit does not create
    # a second object under a different name.
    object_id: str = field(default_factory=lambda: secrets.token_hex(6))

    @property
    def duration(self) -> float:
        return sum(segment.duration for segment in self.segments)

    @property
    def size_bytes(self) -> int:
        return sum(len(segment.data) for segment in self.segments)

    @property
    def start(self) -> datetime:
        return self.segments[0].created_at

    @property
    def end(self) -> datetime:
        return self.start + timedelta(seconds=self.duration)

    @property
    def stream_id(self) -> str | None:
        values = {segment.stream_id for segment in self.segments}
        if len(values) == 1:
            return next(iter(values))
        return None

    @property
    def audio_index(self) -> tuple[AudioIndexPoint, ...]:
        points: list[AudioIndexPoint] = []
        offset = 0.0
        for segment in self.segments:
            if segment.audio_rms_db is not None:
                points.append(
                    AudioIndexPoint(
                        offset=round(offset, 3),
                        duration=round(segment.duration, 3),
                        rms_db=round(segment.audio_rms_db, 2),
                        active=segment.audio_active,
                    )
                )
            offset += segment.duration
        return tuple(points)


@dataclass(frozen=True, slots=True)
class AudioIndexPoint:
    offset: float
    duration: float
    rms_db: float | None
    active: bool

    def as_dict(self) -> dict[str, object]:
        return {
            "offset": self.offset,
            "duration": self.duration,
            "rms_db": self.rms_db,
            "active": self.active,
        }


@dataclass(frozen=True, slots=True)
class ArchiveRecord:
    camera_id: str
    path: Path | None
    relative_path: str
    start: datetime
    end: datetime
    duration: float
    size_bytes: int
    sha256: str | None = None
    segment_count: int | None = None
    stream_id: str | None = None
    audio_index: tuple[AudioIndexPoint, ...] = ()
    encrypted: bool = False

    def as_dict(self) -> dict[str, object]:
        return {
            "camera_id": self.camera_id,
            "relative_path": self.relative_path,
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "duration": self.duration,
            "size_bytes": self.size_bytes,
            "sha256": self.sha256,
            "segment_count": self.segment_count,
            "stream_id": self.stream_id,
            "audio_index": [point.as_dict() for point in self.audio_index],
            "encrypted": self.encrypted,
        }


@dataclass(frozen=True, slots=True)
class ParsedArchiveName:
    start: datetime
    duration: float
    sequence: int
    stream_id: str | None
    object_id: str
    audio_index: tuple[AudioIndexPoint, ...] = ()
    encrypted: bool = False


def _stream_tag(stream_id: str | None) -> str:
    if stream_id is None:
        return "snone"
    digest = hashlib.blake2s(stream_id.encode("utf-8"), digest_size=6).hexdigest()
    return f"s{digest}"


def archive_relative_path(batch: ArchiveBatch, storage: StorageConfig) -> str:
    """Return the deterministic camera-relative object path for a batch."""

    if not batch.segments:
        raise ValueError("cannot name an empty archive batch")
    local_start = batch.start.astimezone(ZoneInfo(storage.timezone))
    stamp = local_start.strftime("%Y%m%dT%H%M%S%z")
    first_sequence = batch.segments[0].sequence
    duration_ms = round(batch.duration * 1000)
    audio_tag = _audio_filename_tag(batch)
    filename = (
        f"{stamp}_{first_sequence:012d}_{duration_ms:09d}ms_"
        f"{_stream_tag(batch.stream_id)}_{audio_tag}{batch.object_id}.ts"
    )
    return PurePosixPath(
        local_start.strftime("%Y"),
        local_start.strftime("%m"),
        local_start.strftime("%d"),
        local_start.strftime("%H"),
        filename,
    ).as_posix()


def parse_archive_filename(filename: str) -> ParsedArchiveName | None:
    match = _ARCHIVE_FILENAME_RE.fullmatch(filename)
    legacy = False
    if match is None:
        match = _ARCHIVE_FILENAME_V1_RE.fullmatch(filename)
        legacy = match is not None
    if match is None:
        return None
    try:
        start = datetime.strptime(match.group("stamp"), "%Y%m%dT%H%M%S%z").astimezone(UTC)
        duration = int(match.group("duration")) / 1000.0
        stream_tag = None if legacy else match.group("stream")
        stream_id = None if stream_tag is None or stream_tag == "snone" else f"tag:{stream_tag[1:]}"
        audio_index: tuple[AudioIndexPoint, ...] = ()
        if not legacy and match.group("audio_step") and match.group("audio_bits"):
            step = int(match.group("audio_step")[1:])
            slot_count = max(1, math.ceil(duration / step))
            bits = bin(int(match.group("audio_bits"), 16))[2:].zfill(slot_count)[-slot_count:]
            audio_index = tuple(
                AudioIndexPoint(
                    offset=float(index * step),
                    duration=min(float(step), max(0.001, duration - index * step)),
                    rms_db=None,
                    active=True,
                )
                for index, active in enumerate(bits)
                if active == "1" and index * step < duration
            )
        return ParsedArchiveName(
            start=start,
            duration=duration,
            sequence=int(match.group("sequence")),
            stream_id=stream_id,
            object_id=match.group("object"),
            audio_index=audio_index,
            encrypted=bool(not legacy and match.group("encrypted")),
        )
    except (ValueError, OverflowError):
        return None


def _audio_filename_tag(batch: ArchiveBatch) -> str:
    """Encode a bounded activity bitmap for zero-request WebDAV timelines.

    The detailed dB values live in the JSON sidecar. The filename carries at most 128
    activity buckets, which lets a PROPFIND directory listing render sound marks without
    downloading every sidecar from remote storage.
    """

    if not any(segment.audio_rms_db is not None for segment in batch.segments):
        return ""
    step = max(1, math.ceil(batch.duration / 128))
    slot_count = max(1, math.ceil(batch.duration / step))
    active = [False] * slot_count
    offset = 0.0
    for segment in batch.segments:
        if segment.audio_active:
            first = max(0, math.floor(offset / step))
            final = min(slot_count - 1, math.ceil((offset + segment.duration) / step) - 1)
            for index in range(first, final + 1):
                active[index] = True
        offset += segment.duration
    bits = "".join("1" if value else "0" for value in active)
    encoded = f"{int(bits, 2):0{math.ceil(slot_count / 4)}x}"
    return f"a{step}x{encoded}_"


class ArchiveAccumulator:
    def __init__(self, *, camera_id: str, target_seconds: float, max_bytes: int) -> None:
        self.camera_id = camera_id
        self.target_seconds = target_seconds
        self.max_bytes = max_bytes
        self._segments: list[LiveSegment] = []
        self._duration = 0.0
        self._bytes = 0

    @property
    def duration(self) -> float:
        return self._duration

    @property
    def size_bytes(self) -> int:
        return self._bytes

    def tune(self, *, target_seconds: float, max_bytes: int) -> ArchiveBatch | None:
        """Apply live batching targets and seal data that already crossed a new target."""

        self.target_seconds = max(0.001, target_seconds)
        self.max_bytes = max(1, max_bytes)
        if self._segments and (
            self._duration >= self.target_seconds or self._bytes >= self.max_bytes
        ):
            return self.pop()
        return None

    def add(self, segment: LiveSegment) -> ArchiveBatch | None:
        self._segments.append(segment)
        self._duration += segment.duration
        self._bytes += len(segment.data)
        if self._duration >= self.target_seconds or self._bytes >= self.max_bytes:
            return self.pop()
        return None

    def pop(self) -> ArchiveBatch | None:
        if not self._segments:
            return None
        batch = ArchiveBatch(camera_id=self.camera_id, segments=tuple(self._segments))
        self._segments = []
        self._duration = 0.0
        self._bytes = 0
        return batch


ArchiveWriter = Callable[[ArchiveBatch], Awaitable[ArchiveRecord]]
ArchiveReclaimer = Callable[[str, int], Awaitable[object]]
AvailableMemoryProvider = Callable[[], int | None]


class ArchiveManager:
    """Aggregate small HLS segments and send larger chunks to a bounded backend writer.

    `max_buffer_mb_per_camera` is enforced across the active writer, queued batch and
    current accumulator. When a local disk or remote WebDAV target falls behind, HTTP
    ingest is backpressured rather than allowing unbounded Python memory growth.
    """

    def __init__(
        self,
        *,
        camera_ids: list[str],
        storage: StorageConfig,
        writer: ArchiveWriter | None = None,
        on_written: Callable[[ArchiveRecord], None] | None = None,
        on_error: Callable[[str, str], None] | None = None,
        on_reclaim: ArchiveReclaimer | None = None,
        available_memory_provider: AvailableMemoryProvider = available_memory_bytes,
    ) -> None:
        self.storage = storage
        self.writer = writer
        self.max_bytes_per_camera = storage.max_buffer_mb_per_camera * _MIB
        self.adaptive_enabled = storage.backend == "webdav" and storage.adaptive_archive_enabled
        self._camera_count = max(1, len(camera_ids))
        self._available_memory_provider = available_memory_provider
        self._available_memory_bytes: int | None = None
        self._memory_sampled_at = 0.0
        self._estimated_bytes_per_second = {camera_id: 0.0 for camera_id in camera_ids}
        initial_target_bytes = (
            min(
                storage.adaptive_archive_target_mb * _MIB,
                max(1, self.max_bytes_per_camera // 3),
            )
            if self.adaptive_enabled
            else self.max_bytes_per_camera
        )
        initial_target_seconds = storage.archive_chunk_seconds
        self._target_bytes = {camera_id: initial_target_bytes for camera_id in camera_ids}
        self._target_seconds = {camera_id: initial_target_seconds for camera_id in camera_ids}
        self.accumulators = {
            camera_id: ArchiveAccumulator(
                camera_id=camera_id,
                target_seconds=initial_target_seconds,
                max_bytes=(
                    initial_target_bytes if self.adaptive_enabled else self.max_bytes_per_camera
                ),
            )
            for camera_id in camera_ids
        }
        # One queued batch is sufficient. The separate byte budget is the actual hard bound.
        self.queues: dict[str, asyncio.Queue[ArchiveBatch | None]] = {
            camera_id: asyncio.Queue(maxsize=1) for camera_id in camera_ids
        }
        self.workers: dict[str, asyncio.Task[None]] = {}
        self.on_written = on_written
        self.on_error = on_error
        self.on_reclaim = on_reclaim
        self._closing = False
        self._shutdown_requested = asyncio.Event()
        self.unwritten_bytes = 0
        self._retained_bytes = {camera_id: 0 for camera_id in camera_ids}
        self._budget_conditions = {camera_id: asyncio.Condition() for camera_id in camera_ids}
        self._ingest_locks = {camera_id: asyncio.Lock() for camera_id in camera_ids}

    def memory_bytes(self, camera_id: str | None = None) -> int:
        if camera_id is not None:
            return self._retained_bytes[camera_id]
        return sum(self._retained_bytes.values())

    def batching_status(self) -> dict[str, object]:
        return {
            "adaptive": self.adaptive_enabled,
            "available_memory_bytes": self._available_memory_bytes,
            "hard_max_bytes_per_camera": self.max_bytes_per_camera,
            "memory_percent": self.storage.adaptive_memory_percent,
            "memory_reserve_bytes": self.storage.adaptive_memory_reserve_mb * _MIB,
            "cameras": {
                camera_id: {
                    "estimated_bytes_per_second": round(
                        self._estimated_bytes_per_second[camera_id], 1
                    ),
                    "target_seconds": round(self._target_seconds[camera_id], 1),
                    "target_bytes": self._target_bytes[camera_id],
                    "buffer_seconds": round(accumulator.duration, 1),
                    "buffer_bytes": accumulator.size_bytes,
                }
                for camera_id, accumulator in self.accumulators.items()
            },
        }

    async def start(self) -> None:
        self._closing = False
        self._shutdown_requested.clear()
        self.unwritten_bytes = 0
        for camera_id in self.accumulators:
            if camera_id not in self.workers:
                self.workers[camera_id] = asyncio.create_task(
                    self._worker(camera_id), name=f"archive-{camera_id}"
                )

    async def add(self, camera_id: str, segment: LiveSegment) -> None:
        if camera_id not in self.accumulators:
            raise KeyError(camera_id)
        segment_bytes = len(segment.data)
        if segment_bytes > self.max_bytes_per_camera:
            raise ValueError(
                f"one media segment is {segment_bytes} bytes, larger than the per-camera "
                f"archive memory limit ({self.max_bytes_per_camera} bytes); raise "
                "storage.max_buffer_mb_per_camera or shorten recording.hls_segment_seconds"
            )

        # A per-camera lock covers all accumulator mutations. It also prevents multiple
        # disconnected/restarted FFmpeg requests from piling payloads into the archive path.
        async with self._ingest_locks[camera_id]:
            if self._closing:
                raise ValueError("archive manager is stopping")
            accumulator = self.accumulators[camera_id]
            if self.adaptive_enabled:
                batch = self._tune_accumulator(camera_id, accumulator, segment)
                if batch is not None:
                    await self.queues[camera_id].put(batch)
            if (
                accumulator.size_bytes
                and accumulator.size_bytes + segment_bytes > self.max_bytes_per_camera
            ):
                batch = accumulator.pop()
                if batch is not None:
                    await self.queues[camera_id].put(batch)

            await self._reserve(camera_id, segment_bytes)
            batch = accumulator.add(segment)
            if batch is not None:
                await self.queues[camera_id].put(batch)

    def _tune_accumulator(
        self,
        camera_id: str,
        accumulator: ArchiveAccumulator,
        segment: LiveSegment,
    ) -> ArchiveBatch | None:
        duration = max(0.001, segment.duration)
        instant_rate = len(segment.data) / duration
        previous_rate = self._estimated_bytes_per_second[camera_id]
        # Five-minute exponential smoothing avoids oscillating object sizes with every
        # keyframe-heavy segment while still following a sustained bitrate change.
        alpha = min(1.0, max(0.01, duration / 300.0))
        estimated_rate = (
            instant_rate
            if previous_rate <= 0
            else previous_rate + alpha * (instant_rate - previous_rate)
        )
        self._estimated_bytes_per_second[camera_id] = estimated_rate

        now = time.monotonic()
        if now - self._memory_sampled_at >= 5.0:
            try:
                self._available_memory_bytes = self._available_memory_provider()
            except (OSError, ValueError):
                self._available_memory_bytes = None
            self._memory_sampled_at = now

        candidates = [
            self.storage.adaptive_archive_target_mb * _MIB,
            max(1, self.max_bytes_per_camera // 3),
        ]
        if self._available_memory_bytes is not None:
            reserve = self.storage.adaptive_memory_reserve_mb * _MIB
            free_after_reserve = max(0, self._available_memory_bytes - reserve)
            adaptive_pool = int(free_after_reserve * self.storage.adaptive_memory_percent / 100)
            candidates.append(adaptive_pool // self._camera_count)
        # A small floor prevents a brief low-memory sample from degenerating into one
        # remote object per two-second HLS segment. A segment larger than the floor always
        # remains indivisible and therefore becomes the target itself.
        target_bytes = max(len(segment.data), min(_MIB, self.max_bytes_per_camera), min(candidates))
        target_bytes = min(self.max_bytes_per_camera, max(1, target_bytes))
        target_seconds = target_bytes / max(1.0, estimated_rate)
        target_seconds = min(
            self.storage.adaptive_archive_max_seconds,
            max(self.storage.adaptive_archive_min_seconds, target_seconds),
        )
        self._target_bytes[camera_id] = target_bytes
        self._target_seconds[camera_id] = target_seconds
        return accumulator.tune(target_seconds=target_seconds, max_bytes=target_bytes)

    async def rotate_camera(self, camera_id: str) -> None:
        """Seal the current tail without waiting for backend I/O to finish."""

        async with self._ingest_locks[camera_id]:
            batch = self.accumulators[camera_id].pop()
            if batch is not None:
                await self.queues[camera_id].put(batch)

    async def flush_camera(self, camera_id: str) -> None:
        await self.rotate_camera(camera_id)
        await self.queues[camera_id].join()

    async def flush_all(self) -> None:
        await asyncio.gather(*(self.flush_camera(camera_id) for camera_id in self.accumulators))

    async def stop(self) -> None:
        if not self.workers:
            return
        self._closing = True
        self._shutdown_requested.set()
        try:
            await self.flush_all()
            for queue in self.queues.values():
                await queue.put(None)
            await asyncio.gather(*self.workers.values(), return_exceptions=True)
            self.workers.clear()
        except BaseException:
            await self.abort()
            raise

    async def abort(self) -> None:
        """Release RAM after the service's shutdown deadline; report uncommitted bytes."""

        self._closing = True
        self._shutdown_requested.set()
        pending = self.memory_bytes()
        self.unwritten_bytes += pending
        if pending:
            logger.error("archive shutdown incomplete: %d uncommitted bytes in RAM", pending)
        for worker in self.workers.values():
            worker.cancel()
        await asyncio.gather(*self.workers.values(), return_exceptions=True)
        self.workers.clear()
        for camera_id, queue in self.queues.items():
            while not queue.empty():
                queue.get_nowait()
                queue.task_done()
            self.accumulators[camera_id].pop()
            await self._release(camera_id, self._retained_bytes[camera_id])

    async def _reserve(self, camera_id: str, size_bytes: int) -> None:
        condition = self._budget_conditions[camera_id]
        async with condition:
            await condition.wait_for(
                lambda: (
                    self._closing
                    or self._retained_bytes[camera_id] + size_bytes <= self.max_bytes_per_camera
                )
            )
            if self._closing:
                raise ValueError("archive manager is stopping")
            self._retained_bytes[camera_id] += size_bytes

    async def _release(self, camera_id: str, size_bytes: int) -> None:
        condition = self._budget_conditions[camera_id]
        async with condition:
            self._retained_bytes[camera_id] = max(0, self._retained_bytes[camera_id] - size_bytes)
            condition.notify_all()

    async def _write(self, batch: ArchiveBatch) -> ArchiveRecord:
        if self.writer is not None:
            return await self.writer(batch)
        # Resolve the module-level function at call time so tests and deployments can
        # instrument it without bypassing the archive manager's memory accounting.
        return await asyncio.to_thread(write_archive_batch, batch, self.storage)

    async def _worker(self, camera_id: str) -> None:
        queue = self.queues[camera_id]
        while True:
            batch = await queue.get()
            if batch is None:
                queue.task_done()
                return
            attempt = 0
            try:
                while True:
                    try:
                        record = await self._write(batch)
                        if self.on_written:
                            self.on_written(record)
                        break
                    except Exception as exc:  # noqa: BLE001 - backend contract permits any I/O error
                        attempt += 1
                        message = f"archive write failed: {exc}"
                        logger.error("camera %s: %s", camera_id, message)
                        if self.on_error:
                            self.on_error(camera_id, message)
                        if (
                            not self._closing
                            and attempt == 1
                            and self.storage.write_failure_policy == "delete_oldest"
                            and self.on_reclaim is not None
                        ):
                            try:
                                await self.on_reclaim(camera_id, batch.size_bytes)
                            except Exception as reclaim_exc:  # noqa: BLE001
                                logger.error(
                                    "camera %s: emergency oldest-first reclaim failed: %s",
                                    camera_id,
                                    reclaim_exc,
                                )
                            else:
                                # Retry the identical deterministic transaction immediately.
                                # If the failure was not capacity-related, later attempts use
                                # the normal bounded exponential backoff without more deletion.
                                continue
                        if self._closing:
                            # Retry shutdown failures until the service-wide deadline,
                            # but don't hammer a failing cloud provider in a tight loop.
                            await asyncio.sleep(2.0)
                        else:
                            try:
                                await asyncio.wait_for(
                                    self._shutdown_requested.wait(),
                                    timeout=min(60.0, 2.0 ** min(attempt, 6)),
                                )
                            except TimeoutError:
                                pass
            finally:
                await self._release(camera_id, batch.size_bytes)
                queue.task_done()


def ensure_storage_root(root: Path) -> None:
    resolved = root.expanduser().resolve()
    if resolved.parent == resolved:
        raise ValueError("refusing to use a filesystem root as storage.root")
    resolved.mkdir(parents=True, exist_ok=True)
    sentinel = resolved / ".camvault-root"
    if not sentinel.exists():
        sentinel.write_text("CamVault managed storage root\n", encoding="utf-8")


def _best_effort_fsync_directory(directory: Path) -> None:
    if os.name == "nt":
        return
    try:
        descriptor = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)


def _record_from_metadata(
    *, camera_id: str, media_path: Path, camera_root: Path, payload: dict[str, object]
) -> ArchiveRecord:
    start = datetime.fromisoformat(str(payload["start"]))
    end = datetime.fromisoformat(str(payload["end"]))
    if start.tzinfo is None:
        start = start.replace(tzinfo=UTC)
    if end.tzinfo is None:
        end = end.replace(tzinfo=UTC)
    raw_audio_index = payload.get("audio_index", [])
    audio_index: list[AudioIndexPoint] = []
    if isinstance(raw_audio_index, list):
        for raw in raw_audio_index:
            if not isinstance(raw, dict):
                continue
            try:
                rms_value = raw.get("rms_db")
                audio_index.append(
                    AudioIndexPoint(
                        offset=max(0.0, float(raw["offset"])),
                        duration=max(0.001, float(raw["duration"])),
                        rms_db=float(rms_value) if rms_value is not None else None,
                        active=bool(raw.get("active", False)),
                    )
                )
            except (KeyError, TypeError, ValueError, OverflowError):
                continue
    return ArchiveRecord(
        camera_id=camera_id,
        path=media_path,
        relative_path=media_path.relative_to(camera_root).as_posix(),
        start=start.astimezone(UTC),
        end=end.astimezone(UTC),
        duration=float(payload["duration"]),
        size_bytes=int(payload.get("size_bytes", media_path.stat().st_size)),
        sha256=str(payload["sha256"]) if payload.get("sha256") else None,
        segment_count=(
            int(payload["segment_count"]) if payload.get("segment_count") is not None else None
        ),
        stream_id=str(payload["stream_id"]) if payload.get("stream_id") is not None else None,
        audio_index=tuple(audio_index),
        encrypted=bool(payload.get("encrypted", False)),
    )


def write_archive_batch(batch: ArchiveBatch, storage: StorageConfig) -> ArchiveRecord:
    if not batch.segments:
        raise ValueError("cannot write an empty archive batch")
    ensure_storage_root(storage.root)
    relative = archive_relative_path(batch, storage)
    camera_root = storage.root / batch.camera_id
    final_path = camera_root.joinpath(*PurePosixPath(relative).parts)
    directory = final_path.parent
    directory.mkdir(parents=True, exist_ok=True)

    stem = final_path.stem
    partial_path = directory / f".{stem}.ts.partial"
    metadata_path = final_path.with_suffix(".json")
    metadata_partial = metadata_path.with_name(f".{metadata_path.name}.partial")

    # Idempotent retry: a previous request may have committed remotely/locally and then
    # timed out before the archive manager received the success result.
    if final_path.is_file() and metadata_path.is_file():
        try:
            payload = json.loads(metadata_path.read_text(encoding="utf-8"))
            if payload.get("version") in {1, 2, 3} and payload.get("format") == "mpegts":
                return _record_from_metadata(
                    camera_id=batch.camera_id,
                    media_path=final_path,
                    camera_root=camera_root,
                    payload=payload,
                )
        except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
            pass

    partial_path.unlink(missing_ok=True)
    metadata_partial.unlink(missing_ok=True)
    if final_path.exists() and not metadata_path.exists():
        final_path.unlink(missing_ok=True)

    digest = hashlib.sha256()
    try:
        with partial_path.open("xb") as file:
            for segment in batch.segments:
                file.write(segment.data)
                digest.update(segment.data)
            file.flush()
            if storage.fsync:
                os.fsync(file.fileno())

        record = ArchiveRecord(
            camera_id=batch.camera_id,
            path=final_path,
            relative_path=relative,
            start=batch.start.astimezone(UTC),
            end=batch.end.astimezone(UTC),
            duration=batch.duration,
            size_bytes=batch.size_bytes,
            sha256=digest.hexdigest(),
            segment_count=len(batch.segments),
            stream_id=batch.stream_id,
            audio_index=batch.audio_index,
        )
        metadata = record.as_dict() | {
            "format": "mpegts",
            "version": 3,
            "sequences": [batch.segments[0].sequence, batch.segments[-1].sequence],
            "object_id": batch.object_id,
        }
        with metadata_partial.open("x", encoding="utf-8") as file:
            json.dump(metadata, file, ensure_ascii=False, indent=2)
            file.write("\n")
            file.flush()
            if storage.fsync:
                os.fsync(file.fileno())

        # The media file becomes visible first; the sidecar follows immediately. On any
        # failure below, both are removed so a retry cannot leave a silent orphan.
        os.replace(partial_path, final_path)
        os.replace(metadata_partial, metadata_path)
        if storage.fsync:
            _best_effort_fsync_directory(directory)
        return record
    except Exception:
        partial_path.unlink(missing_ok=True)
        metadata_partial.unlink(missing_ok=True)
        if final_path.exists() and not metadata_path.exists():
            final_path.unlink(missing_ok=True)
        raise


def scan_archive_records(root: Path, camera_id: str) -> list[ArchiveRecord]:
    camera_root = root / camera_id
    if not camera_root.exists():
        return []
    records: list[ArchiveRecord] = []
    for metadata_path in camera_root.rglob("*.json"):
        try:
            payload = json.loads(metadata_path.read_text(encoding="utf-8"))
            if payload.get("version") not in {1, 2, 3} or payload.get("format") != "mpegts":
                continue
            media_path = metadata_path.with_suffix(".ts")
            if not media_path.is_file():
                continue
            records.append(
                _record_from_metadata(
                    camera_id=camera_id,
                    media_path=media_path,
                    camera_root=camera_root,
                    payload=payload,
                )
            )
        except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
            continue
    records.sort(key=lambda record: record.start)
    return records
