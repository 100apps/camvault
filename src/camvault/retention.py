from __future__ import annotations

import json
import shutil
import time
from dataclasses import dataclass
from pathlib import Path

from camvault.archive import ensure_storage_root
from camvault.config import StorageConfig


@dataclass(frozen=True, slots=True)
class RetentionResult:
    deleted_files: int
    deleted_bytes: int
    remaining_bytes: int
    free_bytes: int | None


def _is_managed_media(path: Path) -> bool:
    metadata_path = path.with_suffix(".json")
    try:
        payload = json.loads(metadata_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return False
    return payload.get("version") in {1, 2} and payload.get("format") == "mpegts"


def _media_files(root: Path) -> list[Path]:
    candidates: list[tuple[float, Path]] = []
    for path in root.rglob("*.ts"):
        try:
            if path.is_file() and _is_managed_media(path):
                candidates.append((path.stat().st_mtime, path))
        except OSError:
            continue
    candidates.sort(key=lambda item: item[0])
    return [path for _mtime, path in candidates]


def _delete_media(path: Path) -> int:
    try:
        size = path.stat().st_size
        path.unlink()
    except OSError:
        return 0
    try:
        path.with_suffix(".json").unlink(missing_ok=True)
    except OSError:
        # The media bytes were removed successfully. A harmless orphan sidecar may remain
        # and can be cleaned manually; do not under-report reclaimed media space.
        pass
    return size


def _clean_stale_partials(root: Path, max_age_hours: int) -> None:
    cutoff = time.time() - max_age_hours * 3600
    for path in root.rglob("*.partial"):
        is_camvault_transaction = path.name.startswith(".") and (
            path.name.endswith(".ts.partial") or path.name.endswith(".json.partial")
        )
        if not is_camvault_transaction:
            continue
        try:
            if path.is_file() and path.stat().st_mtime < cutoff:
                path.unlink(missing_ok=True)
        except OSError:
            continue


def _prune_empty_parents(directory: Path, root: Path) -> None:
    current = directory
    while current != root and current.is_relative_to(root):
        try:
            current.rmdir()
        except OSError:
            return
        current = current.parent


def apply_retention(storage: StorageConfig, *, now_epoch: float | None = None) -> RetentionResult:
    ensure_storage_root(storage.root)
    root = storage.root.expanduser().resolve()
    now_epoch = now_epoch if now_epoch is not None else time.time()
    _clean_stale_partials(root, storage.partial_max_age_hours)

    deleted_files = 0
    deleted_bytes = 0
    files = _media_files(root)
    deleted_parents: list[Path] = []

    def delete(path: Path) -> int:
        nonlocal deleted_files, deleted_bytes
        size = _delete_media(path)
        if size:
            deleted_files += 1
            deleted_bytes += size
            deleted_parents.append(path.parent)
        return size

    if storage.retention_days > 0:
        cutoff = now_epoch - storage.retention_days * 86400
        retained: list[Path] = []
        for path in files:
            try:
                is_old = path.stat().st_mtime < cutoff
            except OSError:
                continue
            if is_old:
                delete(path)
            else:
                retained.append(path)
        files = retained

    sizes: dict[Path, int] = {}
    total = 0
    for path in files:
        try:
            size = path.stat().st_size
        except OSError:
            continue
        sizes[path] = size
        total += size

    max_bytes = int(storage.max_storage_gb * 1024**3)
    if max_bytes > 0 and total > max_bytes:
        for path in list(files):
            if total <= max_bytes:
                break
            size = delete(path)
            if size:
                total -= size
                sizes.pop(path, None)
                files.remove(path)

    min_free_bytes = int(storage.min_free_gb * 1024**3)
    try:
        free = shutil.disk_usage(root).free
    except OSError:
        free = 0
    if min_free_bytes > 0 and free < min_free_bytes:
        for path in list(files):
            if free >= min_free_bytes:
                break
            size = delete(path)
            if size:
                total -= size
                free += size
                files.remove(path)

    for directory in sorted(set(deleted_parents), key=lambda item: len(item.parts), reverse=True):
        _prune_empty_parents(directory, root)

    try:
        free = shutil.disk_usage(root).free
    except OSError:
        free = 0
    return RetentionResult(
        deleted_files=deleted_files,
        deleted_bytes=deleted_bytes,
        remaining_bytes=max(total, 0),
        free_bytes=free,
    )
