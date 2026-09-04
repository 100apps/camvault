from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path

import pytest

from camvault.config import AppConfig, CameraConfig, RecordingConfig, StorageConfig
from camvault.retention import apply_retention
from camvault.service import CamVaultService


def _managed_file(root: Path, name: str, payload: bytes, mtime: float) -> Path:
    media = root / "cam" / "2026" / "01" / "01" / "00" / f"{name}.ts"
    media.parent.mkdir(parents=True, exist_ok=True)
    media.write_bytes(payload)
    media.with_suffix(".json").write_text(
        json.dumps({"version": 1, "format": "mpegts"}), encoding="utf-8"
    )
    os.utime(media, (mtime, mtime))
    return media


def test_retention_deletes_only_managed_old_media(tmp_path: Path) -> None:
    now = time.time()
    old = _managed_file(tmp_path, "old", b"old", now - 3 * 86400)
    fresh = _managed_file(tmp_path, "fresh", b"fresh", now)
    unrelated = tmp_path / "unrelated.ts"
    unrelated.write_bytes(b"do not delete")

    storage = StorageConfig(
        root=tmp_path,
        retention_days=1,
        max_storage_gb=0,
        min_free_gb=0,
    )
    result = apply_retention(storage, now_epoch=now)
    assert result.deleted_files == 1
    assert not old.exists()
    assert fresh.exists()
    assert unrelated.exists()


def test_stale_partial_cleanup_ignores_unrelated_partial_files(tmp_path: Path) -> None:
    old_time = time.time() - 48 * 3600
    managed_partial = tmp_path / "cam" / ".record.ts.partial"
    unrelated_partial = tmp_path / "notes.partial"
    managed_partial.parent.mkdir(parents=True, exist_ok=True)
    managed_partial.write_bytes(b"incomplete")
    unrelated_partial.write_bytes(b"keep")
    os.utime(managed_partial, (old_time, old_time))
    os.utime(unrelated_partial, (old_time, old_time))

    apply_retention(
        StorageConfig(
            root=tmp_path,
            retention_days=0,
            max_storage_gb=0,
            min_free_gb=0,
            partial_max_age_hours=24,
        )
    )
    assert not managed_partial.exists()
    assert unrelated_partial.exists()


def test_retention_deletes_oldest_until_total_size_is_below_limit(tmp_path: Path) -> None:
    now = time.time()
    oldest = _managed_file(tmp_path, "oldest", b"1234", now - 60)
    newest = _managed_file(tmp_path, "newest", b"5678", now)
    storage = StorageConfig(
        root=tmp_path,
        retention_days=0,
        max_storage_gb=5 / 1024**3,
        min_free_gb=0,
    )

    result = apply_retention(storage, now_epoch=now)

    assert result.deleted_files == 1
    assert result.deleted_bytes == 4
    assert result.remaining_bytes == 4
    assert not oldest.exists()
    assert newest.exists()


@pytest.mark.asyncio
async def test_archive_error_wakes_retention_without_waiting_for_schedule(tmp_path: Path) -> None:
    config = AppConfig(
        storage=StorageConfig(
            root=tmp_path,
            retention_days=0,
            min_free_gb=0,
            retention_check_seconds=60,
        ),
        recording=RecordingConfig(max_ingest_segment_mb=8),
        cameras=[CameraConfig(id="front", rtsp_url="rtsp://127.0.0.1/unused")],
    )
    service = CamVaultService(config)
    await service.start(start_supervisors=False)
    try:
        for _ in range(100):
            if service.retention_runs >= 1:
                break
            await asyncio.sleep(0.01)
        assert service.retention_runs == 1

        service._on_archive_error("front", "archive write failed: no space left")
        for _ in range(100):
            if service.retention_runs >= 2:
                break
            await asyncio.sleep(0.01)

        assert service.retention_runs == 2
        assert service.last_retention_reason == "archive-write-failure"
        assert service.status()["retention"]["last_error"] is None
    finally:
        await service.stop()
