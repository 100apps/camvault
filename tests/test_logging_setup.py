from __future__ import annotations

import logging
from pathlib import Path

from camvault.logging_setup import BufferedFileHandler, RecentLogHandler


def _record(message: str, level: int = logging.INFO) -> logging.LogRecord:
    return logging.LogRecord("camvault.test", level, __file__, 1, message, (), None)


def test_recent_log_is_bounded_and_redacted() -> None:
    handler = RecentLogHandler(2, secrets=("known-secret",))
    handler.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
    handler.handle(_record("discarded"))
    handler.handle(_record("token=abc known-secret"))
    handler.handle(_record("rtsp://admin:camera-pass@example.test/live"))

    entries = handler.snapshot(limit=10)
    assert len(entries) == 2
    assert entries[0]["sequence"] == 2
    rendered = str(entries)
    assert "known-secret" not in rendered
    assert "camera-pass" not in rendered
    assert "token=***" in rendered


def test_buffered_file_log_flushes_in_batches_and_rotates(tmp_path: Path) -> None:
    path = tmp_path / "camvault.log"
    handler = BufferedFileHandler(
        path,
        batch_records=2,
        flush_seconds=60,
        max_bytes=24,
        backup_count=1,
        secrets=("private",),
    )
    handler.setFormatter(logging.Formatter("%(message)s"))
    try:
        handler.handle(_record("first private"))
        assert not path.exists()
        handler.handle(_record("second"))
        assert "private" not in path.read_text(encoding="utf-8")
        handler.handle(_record("a line long enough to rotate immediately"))
        handler.flush()
        assert path.with_name("camvault.log.1").exists()
    finally:
        handler.close()
