from __future__ import annotations

import pytest

from camvault.selftest import run_self_test


@pytest.mark.asyncio
async def test_synthetic_end_to_end_pipeline() -> None:
    report = await run_self_test()
    assert report["result"] == "PASS"
    assert report["live_segments"] >= 4
    assert report["archive_files"] >= 1
    assert report["first_archive_probe"]["format"]["format_name"] == "mpegts"
