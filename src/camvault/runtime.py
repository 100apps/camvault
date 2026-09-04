from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any


def utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass(slots=True)
class CameraRuntime:
    camera_id: str
    name: str
    state: str = "idle"
    detail: str = ""
    process_pid: int | None = None
    restarts: int = 0
    bytes_ingested: int = 0
    segments_ingested: int = 0
    archive_files: int = 0
    archive_bytes: int = 0
    last_segment_at: datetime | None = None
    last_archive_at: datetime | None = None
    started_at: datetime | None = None
    resolved_profile: str | None = None
    resolved_stream: str | None = None
    last_error: str | None = None
    updated_at: datetime = field(default_factory=utc_now)

    def touch(self) -> None:
        self.updated_at = utc_now()

    def as_dict(self) -> dict[str, Any]:
        def iso(value: datetime | None) -> str | None:
            return value.isoformat() if value else None

        return {
            "id": self.camera_id,
            "name": self.name,
            "state": self.state,
            "detail": self.detail,
            "process_pid": self.process_pid,
            "restarts": self.restarts,
            "bytes_ingested": self.bytes_ingested,
            "segments_ingested": self.segments_ingested,
            "archive_files": self.archive_files,
            "archive_bytes": self.archive_bytes,
            "last_segment_at": iso(self.last_segment_at),
            "last_archive_at": iso(self.last_archive_at),
            "started_at": iso(self.started_at),
            "resolved_profile": self.resolved_profile,
            "resolved_stream": self.resolved_stream,
            "last_error": self.last_error,
            "updated_at": iso(self.updated_at),
        }
