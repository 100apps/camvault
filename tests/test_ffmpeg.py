from __future__ import annotations

from camvault.config import CameraConfig, RecordingConfig
from camvault.ffmpeg import build_camera_command, redacted_command


def test_camera_command_uses_http_put_without_temporary_segment_files() -> None:
    command = build_camera_command(
        camera=CameraConfig(id="front", rtsp_url="rtsp://camera/live"),
        recording=RecordingConfig(include_audio=False, audio_codec="none"),
        rtsp_url="rtsp://admin:password@camera/live",
        ingest_port=8088,
        ingest_secret="ingest-secret",
        run_id="abc",
    )
    joined = " ".join(command)
    assert "-method PUT" in joined
    assert "http://127.0.0.1:8088/_ingest/front/" in joined
    assert "segment_abc_" in joined
    assert ".partial" not in joined
    redacted = redacted_command(command, secrets_to_hide=("ingest-secret",))
    assert "password" not in redacted
    assert "ingest-secret" not in redacted
    assert "rtsp://***:***@camera/live" in redacted


def test_camera_command_formats_ipv6_loopback_url() -> None:
    command = build_camera_command(
        camera=CameraConfig(id="front", rtsp_url="rtsp://camera/live"),
        recording=RecordingConfig(include_audio=False, audio_codec="none"),
        rtsp_url="rtsp://camera/live",
        ingest_host="::1",
        ingest_port=8088,
        ingest_secret="secret",
        run_id="ipv6",
    )
    assert any(item.startswith("http://[::1]:8088/_ingest/front/") for item in command)
