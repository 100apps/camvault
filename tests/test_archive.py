from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from camvault.archive import (
    ArchiveBatch,
    ArchiveManager,
    parse_archive_filename,
    scan_archive_records,
    write_archive_batch,
)
from camvault.buffer import LiveSegment
from camvault.config import StorageConfig


def _segment(
    sequence: int,
    data: bytes,
    created_at: datetime,
    duration: float = 2.0,
    stream_id: str | None = None,
) -> LiveSegment:
    return LiveSegment(
        sequence=sequence,
        name=f"s{sequence}.ts",
        data=data,
        created_at=created_at,
        duration=duration,
        stream_id=stream_id,
    )


def test_archive_is_atomic_hashed_and_partitioned_by_local_hour(tmp_path: Path) -> None:
    storage = StorageConfig(
        root=tmp_path / "recordings",
        timezone="Asia/Shanghai",
        archive_chunk_seconds=4,
        max_buffer_mb_per_camera=8,
        min_free_gb=0,
    )
    start = datetime(2026, 9, 4, 12, 34, 56, tzinfo=UTC)
    batch = ArchiveBatch(
        camera_id="front",
        segments=(
            _segment(7, b"abc", start, stream_id="run-a"),
            _segment(8, b"def", start + timedelta(seconds=2), stream_id="run-a"),
        ),
    )
    record = write_archive_batch(batch, storage)

    assert record.path.read_bytes() == b"abcdef"
    assert record.sha256 == hashlib.sha256(b"abcdef").hexdigest()
    assert record.relative_path.startswith("2026/09/04/20/")
    assert not list(storage.root.rglob("*.partial"))

    metadata = json.loads(record.path.with_suffix(".json").read_text(encoding="utf-8"))
    assert metadata["version"] == 2
    assert metadata["format"] == "mpegts"
    assert metadata["sequences"] == [7, 8]
    assert metadata["stream_id"] == "run-a"
    scanned = scan_archive_records(storage.root, "front")
    assert len(scanned) == 1
    assert scanned[0].sha256 == record.sha256
    assert scanned[0].stream_id == "run-a"


@pytest.mark.asyncio
async def test_archive_manager_flushes_and_releases_memory_budget(tmp_path: Path) -> None:
    storage = StorageConfig(
        root=tmp_path,
        timezone="UTC",
        archive_chunk_seconds=4,
        max_buffer_mb_per_camera=8,
        min_free_gb=0,
    )
    manager = ArchiveManager(camera_ids=["cam"], storage=storage)
    await manager.start()
    try:
        start = datetime.now(UTC)
        await manager.add("cam", _segment(0, b"a" * 1024, start))
        assert manager.memory_bytes("cam") == 1024
        await manager.add("cam", _segment(1, b"b" * 1024, start + timedelta(seconds=2)))
        await manager.flush_all()
        assert manager.memory_bytes("cam") == 0
        records = scan_archive_records(tmp_path, "cam")
        assert len(records) == 1
        assert records[0].size_bytes == 2048
    finally:
        await manager.stop()


@pytest.mark.asyncio
async def test_slow_disk_backpressures_without_exceeding_archive_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import threading

    import camvault.archive as archive_module

    storage = StorageConfig(
        root=tmp_path,
        timezone="UTC",
        archive_chunk_seconds=3600,
        max_buffer_mb_per_camera=8,
        min_free_gb=0,
    )
    started = threading.Event()
    release = threading.Event()

    def slow_write(batch: ArchiveBatch, _storage: StorageConfig):
        started.set()
        assert release.wait(timeout=5)
        path = tmp_path / f"{batch.segments[0].sequence}.ts"
        path.write_bytes(b"".join(item.data for item in batch.segments))
        from camvault.archive import ArchiveRecord

        return ArchiveRecord(
            camera_id=batch.camera_id,
            path=path,
            relative_path=path.name,
            start=batch.start,
            end=batch.end,
            duration=batch.duration,
            size_bytes=batch.size_bytes,
        )

    monkeypatch.setattr(archive_module, "write_archive_batch", slow_write)
    manager = ArchiveManager(camera_ids=["cam"], storage=storage)
    await manager.start()
    payload_size = 5 * 1024 * 1024
    start = datetime.now(UTC)
    try:
        await manager.add("cam", _segment(0, b"a" * payload_size, start))
        blocked_add = __import__("asyncio").create_task(
            manager.add(
                "cam",
                _segment(1, b"b" * payload_size, start + timedelta(seconds=2)),
            )
        )
        assert await __import__("asyncio").to_thread(started.wait, 2)
        await __import__("asyncio").sleep(0.05)
        assert not blocked_add.done()
        assert manager.memory_bytes("cam") == payload_size
        assert manager.memory_bytes("cam") <= 8 * 1024 * 1024
        release.set()
        await blocked_add
        assert manager.memory_bytes("cam") == payload_size
        await manager.flush_all()
        assert manager.memory_bytes("cam") == 0
    finally:
        release.set()
        await manager.stop()


@pytest.mark.asyncio
async def test_rotate_camera_seals_short_tail_without_waiting_for_target(
    tmp_path: Path,
) -> None:
    storage = StorageConfig(
        root=tmp_path,
        timezone="UTC",
        archive_chunk_seconds=300,
        max_buffer_mb_per_camera=8,
        min_free_gb=0,
    )
    manager = ArchiveManager(camera_ids=["cam"], storage=storage)
    await manager.start()
    try:
        await manager.add(
            "cam",
            _segment(0, b"tail", datetime.now(UTC), stream_id="run-tail"),
        )
        await manager.rotate_camera("cam")
        await manager.queues["cam"].join()
        records = scan_archive_records(tmp_path, "cam")
        assert len(records) == 1
        assert records[0].path.read_bytes() == b"tail"
        assert records[0].stream_id == "run-tail"
    finally:
        await manager.stop()


def test_parse_legacy_v01_archive_filename() -> None:
    parsed = parse_archive_filename("20260904T200000+0800_000000000120_000300000ms_a1b2c3.ts")
    assert parsed is not None
    assert parsed.sequence == 120
    assert parsed.duration == 300.0
    assert parsed.stream_id is None
    assert parsed.object_id == "a1b2c3"
