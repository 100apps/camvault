from __future__ import annotations

from datetime import UTC, datetime

from camvault.buffer import LiveBuffer


def test_live_buffer_is_bounded_and_deduplicates() -> None:
    buffer = LiveBuffer(window_segments=2, max_bytes=7, default_duration=2.0)
    first = buffer.add_segment(name="a.ts", data=b"111")
    second = buffer.add_segment(name="b.ts", data=b"222")
    third = buffer.add_segment(name="c.ts", data=b"333")
    assert first is not None and second is not None and third is not None
    assert buffer.get("a.ts") is None
    assert [item.name for item in buffer.snapshot()] == ["b.ts", "c.ts"]
    assert buffer.total_bytes == 6
    assert buffer.add_segment(name="a.ts", data=b"new") is None


def test_playlist_duration_hint_applies_before_and_after_segment() -> None:
    buffer = LiveBuffer(window_segments=4, max_bytes=1000, default_duration=2.0)
    buffer.apply_playlist("#EXTM3U\n#EXTINF:3.25,\nfuture.ts\n")
    future = buffer.add_segment(name="future.ts", data=b"x")
    assert future is not None and future.duration == 3.25

    current = buffer.add_segment(name="current.ts", data=b"y")
    buffer.apply_playlist("#EXTM3U\n#EXTINF:1.5,\ncurrent.ts\n")
    assert current is not None and current.duration == 1.5


def test_render_playlist_contains_sequence_timestamp_and_token() -> None:
    buffer = LiveBuffer(window_segments=4, max_bytes=1000, default_duration=2.0)
    buffer.add_segment(
        name="seg.ts",
        data=b"x",
        created_at=datetime(2026, 9, 4, 12, 0, tzinfo=UTC),
    )
    playlist = buffer.render_playlist(token="a%20b")
    assert "#EXT-X-MEDIA-SEQUENCE:0" in playlist
    assert "#EXT-X-PROGRAM-DATE-TIME:2026-09-04T12:00:00.000Z" in playlist
    assert "seg.ts?token=a%20b" in playlist


def test_live_buffer_stays_bounded_after_long_sequence() -> None:
    buffer = LiveBuffer(window_segments=8, max_bytes=8 * 1024, default_duration=2.0)
    for index in range(10_000):
        buffer.add_segment(name=f"run_{index:06d}.ts", data=b"x" * 1024)
    assert len(buffer.snapshot()) == 8
    assert buffer.total_bytes == 8 * 1024
    assert buffer.snapshot()[0].sequence == 9_992


def test_live_playlist_marks_stream_restart_discontinuity() -> None:
    buffer = LiveBuffer(window_segments=8, max_bytes=1000, default_duration=2.0)
    first = buffer.add_segment(name="one.ts", data=b"a", stream_id="run-one")
    second = buffer.add_segment(name="two.ts", data=b"b", stream_id="run-two")
    assert first is not None and second is not None
    playlist = buffer.render_playlist()
    assert playlist.count("#EXT-X-DISCONTINUITY\n") == 1
    assert second.discontinuity_sequence == 1

    # Once the old segment slides out, the sequence preserves the prior transition count.
    tiny = LiveBuffer(window_segments=1, max_bytes=1000, default_duration=2.0)
    tiny.add_segment(name="old.ts", data=b"a", stream_id="old")
    tiny.add_segment(name="new.ts", data=b"b", stream_id="new")
    assert "#EXT-X-DISCONTINUITY-SEQUENCE:1" in tiny.render_playlist()
