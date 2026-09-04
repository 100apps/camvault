from __future__ import annotations

import math
from collections import OrderedDict, deque
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import PurePosixPath
from urllib.parse import urlsplit


@dataclass(slots=True)
class LiveSegment:
    sequence: int
    name: str
    data: bytes
    created_at: datetime
    duration: float
    stream_id: str | None = None
    discontinuity_sequence: int = 0
    audio_rms_db: float | None = None
    audio_active: bool = False


class LiveBuffer:
    def __init__(
        self,
        *,
        window_segments: int,
        max_bytes: int,
        default_duration: float,
    ) -> None:
        self.window_segments = window_segments
        self.max_bytes = max_bytes
        self.default_duration = default_duration
        self._segments: OrderedDict[str, LiveSegment] = OrderedDict()
        self._bytes = 0
        self._next_sequence = 0
        self._duration_hints: OrderedDict[str, float] = OrderedDict()
        self._recent_names: deque[str] = deque(maxlen=max(64, window_segments * 8))
        self._recent_name_set: set[str] = set()
        self._current_stream_id: str | None = None
        self._discontinuity_count = 0

    @property
    def total_bytes(self) -> int:
        return self._bytes

    def add_segment(
        self,
        *,
        name: str,
        data: bytes,
        created_at: datetime | None = None,
        stream_id: str | None = None,
    ) -> LiveSegment | None:
        if name in self._segments or name in self._recent_name_set:
            return None
        created_at = created_at or datetime.now(UTC)
        duration = self._duration_hints.pop(name, self.default_duration)
        if stream_id is not None:
            if self._current_stream_id is not None and stream_id != self._current_stream_id:
                self._discontinuity_count += 1
            self._current_stream_id = stream_id
        segment = LiveSegment(
            sequence=self._next_sequence,
            name=name,
            data=data,
            created_at=created_at,
            duration=duration,
            stream_id=stream_id,
            discontinuity_sequence=self._discontinuity_count,
        )
        self._next_sequence += 1
        self._segments[name] = segment
        self._bytes += len(data)
        self._remember_name(name)
        self._evict()
        return segment

    def _remember_name(self, name: str) -> None:
        if len(self._recent_names) == self._recent_names.maxlen:
            old = self._recent_names.popleft()
            self._recent_name_set.discard(old)
        self._recent_names.append(name)
        self._recent_name_set.add(name)

    def _evict(self) -> None:
        while self._segments and (
            len(self._segments) > self.window_segments or self._bytes > self.max_bytes
        ):
            _name, segment = self._segments.popitem(last=False)
            self._bytes -= len(segment.data)

    def apply_playlist(self, playlist: str) -> None:
        pending_duration: float | None = None
        for raw_line in playlist.splitlines():
            line = raw_line.strip()
            if not line:
                continue
            if line.startswith("#EXTINF:"):
                value = line.split(":", 1)[1].split(",", 1)[0]
                try:
                    pending_duration = max(0.001, float(value))
                except ValueError:
                    pending_duration = None
                continue
            if line.startswith("#"):
                continue
            if pending_duration is None:
                continue
            path = urlsplit(line).path
            name = PurePosixPath(path).name
            if not name:
                pending_duration = None
                continue
            segment = self._segments.get(name)
            if segment is not None:
                segment.duration = pending_duration
            else:
                self._duration_hints[name] = pending_duration
                while len(self._duration_hints) > self.window_segments * 4:
                    self._duration_hints.popitem(last=False)
            pending_duration = None

    def get(self, name: str) -> LiveSegment | None:
        return self._segments.get(name)

    def snapshot(self) -> list[LiveSegment]:
        return list(self._segments.values())

    def render_playlist(self, *, token: str | None = None) -> str:
        segments = self.snapshot()
        if not segments:
            return ""
        target_duration = max(1, math.ceil(max(segment.duration for segment in segments)))
        lines = [
            "#EXTM3U",
            "#EXT-X-VERSION:3",
            f"#EXT-X-TARGETDURATION:{target_duration}",
            f"#EXT-X-MEDIA-SEQUENCE:{segments[0].sequence}",
        ]
        first_discontinuity = segments[0].discontinuity_sequence
        if first_discontinuity:
            lines.append(f"#EXT-X-DISCONTINUITY-SEQUENCE:{first_discontinuity}")
        suffix = f"?token={token}" if token else ""
        previous_discontinuity = first_discontinuity
        for segment in segments:
            if segment.discontinuity_sequence > previous_discontinuity:
                lines.append("#EXT-X-DISCONTINUITY")
            previous_discontinuity = segment.discontinuity_sequence
            timestamp = (
                segment.created_at.astimezone(UTC)
                .isoformat(timespec="milliseconds")
                .replace("+00:00", "Z")
            )
            lines.append(f"#EXT-X-PROGRAM-DATE-TIME:{timestamp}")
            lines.append(f"#EXTINF:{segment.duration:.3f},")
            lines.append(f"{segment.name}{suffix}")
        return "\n".join(lines) + "\n"
