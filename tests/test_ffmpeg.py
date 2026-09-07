from __future__ import annotations

from types import SimpleNamespace

from camvault.config import CameraConfig, RecordingConfig
from camvault.ffmpeg import (
    build_camera_command,
    build_download_command,
    ffmpeg_has_decoder,
    redacted_command,
)


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
    assert f"-timeout {20 * 1_000_000}" in joined
    assert "-rw_timeout" not in command
    assert "-stdin" in command
    assert "-nostdin" not in command
    assert "-method PUT" in joined
    assert "http://127.0.0.1:8088/_ingest/front/" in joined
    assert "segment_abc_" in joined
    assert ".partial" not in joined
    assert "-preset ultrafast" in joined
    assert "-crf 20" in joined
    assert "-fps_mode:v passthrough" in joined
    redacted = redacted_command(command, secrets_to_hide=("ingest-secret",))
    assert "password" not in redacted
    assert "ingest-secret" not in redacted
    assert "rtsp://***:***@camera/live" in redacted


def test_camera_command_indexes_audio_in_existing_aac_pipeline_only() -> None:
    camera = CameraConfig(id="front", rtsp_url="rtsp://camera/live")
    aac = build_camera_command(
        camera=camera,
        recording=RecordingConfig(video_codec="copy", audio_codec="aac"),
        rtsp_url="rtsp://camera/live",
        ingest_port=8088,
        ingest_secret="secret",
    )
    copied = build_camera_command(
        camera=camera,
        recording=RecordingConfig(video_codec="copy", audio_codec="copy"),
        rtsp_url="rtsp://camera/live",
        ingest_port=8088,
        ingest_secret="secret",
    )
    disabled = build_camera_command(
        camera=camera,
        recording=RecordingConfig(video_codec="copy", audio_codec="aac", audio_index_enabled=False),
        rtsp_url="rtsp://camera/live",
        ingest_port=8088,
        ingest_secret="secret",
    )

    assert "-af" in aac
    assert any("Overall.RMS_level" in item for item in aac)
    assert any("measure_perchannel=none" in item for item in aac)
    assert any("measure_overall=RMS_level" in item for item in aac)
    assert any("file=pipe\\\\:1" in item for item in aac)
    assert any("direct=1" in item for item in aac)
    assert "-af" not in copied
    assert "-af" not in disabled


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


def test_download_command_stream_copies_fragmented_mp4_without_temp_files() -> None:
    command = build_download_command(
        RecordingConfig(ffmpeg_path="/opt/ffmpeg"),
        start_offset_seconds=12.3456,
        duration_seconds=45.6789,
    )
    joined = " ".join(command)
    assert command[0] == "/opt/ffmpeg"
    assert "-ss 12.346" in joined
    assert "-t 45.679" in joined
    assert "-c copy" in joined
    assert "-bsf:a aac_adtstoasc" in joined
    assert "-f mp4 pipe:1" in joined
    assert "frag_keyframe+empty_moov+default_base_moof" in joined
    assert not any(".mp4" in item or ".ts" in item for item in command)
    assert command.index("-ss") > command.index("-i")


def test_decoder_check_requires_exact_software_decoder_name(monkeypatch) -> None:
    output = """
 V..... h264_v4l2m2m         V4L2 mem2mem H.264 decoder wrapper
 VFS..D h264                 H.264 / AVC / MPEG-4 AVC
"""
    monkeypatch.setattr(
        "camvault.ffmpeg.subprocess.run",
        lambda *_args, **_kwargs: SimpleNamespace(stdout=output),
    )

    assert ffmpeg_has_decoder("ffmpeg", "h264") is True
    assert ffmpeg_has_decoder("ffmpeg", "hevc") is False
