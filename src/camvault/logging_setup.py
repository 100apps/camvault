from __future__ import annotations

import logging
import os
import re
import threading
from collections import deque
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

from camvault.config import AppConfig, LoggingConfig
from camvault.security import redact_text

_KEY_VALUE_SECRET_RE = re.compile(r"(?i)(password|passwd|token|secret)(\s*[=:]\s*)([^\s,;]+)")


def _sanitize(text: str, secrets: tuple[str, ...]) -> str:
    redacted = redact_text(text, secrets)
    return _KEY_VALUE_SECRET_RE.sub(r"\1\2***", redacted)


@dataclass(frozen=True, slots=True)
class LogEntry:
    sequence: int
    timestamp: str
    level: str
    logger: str
    message: str

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


class RecentLogHandler(logging.Handler):
    """Bounded, thread-safe RAM log used by the diagnostic console."""

    def __init__(self, max_records: int, *, secrets: tuple[str, ...] = ()) -> None:
        super().__init__()
        self._entries: deque[LogEntry] = deque(maxlen=max_records)
        self._sequence = 0
        self._secrets = secrets

    def emit(self, record: logging.LogRecord) -> None:
        try:
            message = _sanitize(self.format(record), self._secrets)
            self._sequence += 1
            timestamp = datetime.fromtimestamp(record.created, tz=UTC).isoformat(
                timespec="milliseconds"
            )
            self._entries.append(
                LogEntry(
                    sequence=self._sequence,
                    timestamp=timestamp,
                    level=record.levelname,
                    logger=record.name,
                    message=message,
                )
            )
        except Exception:  # noqa: BLE001 - logging must isolate arbitrary formatter errors
            self.handleError(record)

    def snapshot(self, *, after: int = 0, limit: int = 500) -> list[dict[str, object]]:
        self.acquire()
        try:
            entries = [entry for entry in self._entries if entry.sequence > after]
            return [entry.as_dict() for entry in entries[-limit:]]
        finally:
            self.release()


class BufferedFileHandler(logging.Handler):
    """Batch writes formatted log lines and rotates without per-record disk I/O."""

    def __init__(
        self,
        path: Path,
        *,
        batch_records: int,
        flush_seconds: float,
        max_bytes: int,
        backup_count: int,
        secrets: tuple[str, ...] = (),
    ) -> None:
        super().__init__()
        self.path = path
        self.batch_records = batch_records
        self.flush_seconds = flush_seconds
        self.max_bytes = max_bytes
        self.backup_count = backup_count
        self._secrets = secrets
        self._pending: list[str] = []
        self.last_error: str | None = None
        self._stopped = threading.Event()
        self._worker = threading.Thread(
            target=self._flush_loop,
            name="camvault-log-flush",
            daemon=True,
        )
        self._worker.start()

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self._pending.append(_sanitize(self.format(record), self._secrets))
            if len(self._pending) >= self.batch_records or record.levelno >= logging.ERROR:
                self.flush()
        except Exception:  # noqa: BLE001 - logging must never crash the recorder
            self.handleError(record)

    def flush(self) -> None:
        self.acquire()
        try:
            if not self._pending:
                return
            payload = ("\n".join(self._pending) + "\n").encode("utf-8")
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._rotate_if_needed(len(payload))
            with self.path.open("ab") as destination:
                destination.write(payload)
                destination.flush()
            self._pending.clear()
            self.last_error = None
        finally:
            self.release()

    def close(self) -> None:
        if not self._stopped.is_set():
            self._stopped.set()
            if threading.current_thread() is not self._worker:
                self._worker.join(timeout=max(1.0, min(self.flush_seconds + 1.0, 5.0)))
            try:
                self.flush()
            except OSError as exc:
                self.last_error = str(exc)
        super().close()

    def _flush_loop(self) -> None:
        while not self._stopped.wait(self.flush_seconds):
            try:
                self.flush()
            except OSError as exc:
                # A later batch retries; keep the failure inspectable without recursively
                # logging from inside a log handler.
                self.last_error = str(exc)

    def _rotate_if_needed(self, incoming_bytes: int) -> None:
        try:
            current_bytes = self.path.stat().st_size
        except FileNotFoundError:
            return
        if current_bytes + incoming_bytes <= self.max_bytes:
            return
        if self.backup_count <= 0:
            self.path.unlink(missing_ok=True)
            return
        oldest = self.path.with_name(f"{self.path.name}.{self.backup_count}")
        oldest.unlink(missing_ok=True)
        for index in range(self.backup_count - 1, 0, -1):
            source = self.path.with_name(f"{self.path.name}.{index}")
            if source.exists():
                os.replace(source, self.path.with_name(f"{self.path.name}.{index + 1}"))
        os.replace(self.path, self.path.with_name(f"{self.path.name}.1"))


@dataclass(slots=True)
class LogManager:
    recent: RecentLogHandler
    file_handler: BufferedFileHandler | None
    handlers: tuple[logging.Handler, ...]

    def close(self) -> None:
        root = logging.getLogger()
        for handler in self.handlers:
            root.removeHandler(handler)
            handler.close()


def config_secrets(config: AppConfig) -> tuple[str, ...]:
    values = [
        config.server.resolved_playback_token(),
        config.server.resolved_web_password(),
        config.storage.webdav.resolved_password(),
        config.storage.webdav.resolved_encryption_key_text(),
    ]
    for camera in config.cameras:
        values.extend(
            (
                camera.resolved_password(),
                camera.resolved_direct_rtsp_url(),
            )
        )
    return tuple(value for value in values if value)


def configure_logging(
    level: str,
    settings: LoggingConfig,
    *,
    secrets: tuple[str, ...] = (),
) -> LogManager:
    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper()))
    for handler in list(root.handlers):
        if getattr(handler, "_camvault_owned", False):
            root.removeHandler(handler)
            handler.close()

    formatter = logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S%z",
    )
    console = logging.StreamHandler()
    console.setFormatter(formatter)
    console._camvault_owned = True  # type: ignore[attr-defined]
    root.addHandler(console)

    recent = RecentLogHandler(settings.memory_records, secrets=secrets)
    recent.setFormatter(formatter)
    recent._camvault_owned = True  # type: ignore[attr-defined]
    root.addHandler(recent)

    file_handler = None
    if settings.file is not None:
        file_handler = BufferedFileHandler(
            settings.file,
            batch_records=settings.batch_records,
            flush_seconds=settings.flush_seconds,
            max_bytes=settings.max_file_mb * 1024 * 1024,
            backup_count=settings.backup_count,
            secrets=secrets,
        )
        file_handler.setFormatter(formatter)
        file_handler._camvault_owned = True  # type: ignore[attr-defined]
        root.addHandler(file_handler)

    # With Uvicorn's built-in logging config disabled these records flow into the same
    # redacted RAM/file pipeline as CamVault records.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        child = logging.getLogger(name)
        child.handlers.clear()
        child.propagate = True

    handlers = (console, recent) if file_handler is None else (console, recent, file_handler)
    return LogManager(recent=recent, file_handler=file_handler, handlers=handlers)
