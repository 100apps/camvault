from __future__ import annotations

import secrets
import shlex
import shutil
import subprocess
from dataclasses import dataclass

from camvault.config import CameraConfig, RecordingConfig
from camvault.security import redact_text, redact_url


class FFmpegError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class FFmpegInfo:
    executable: str
    version_line: str


def inspect_ffmpeg(executable: str) -> FFmpegInfo:
    resolved = shutil.which(executable) or executable
    try:
        result = subprocess.run(
            [resolved, "-version"],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise FFmpegError(f"cannot execute {executable!r}: {exc}") from exc
    first_line = (result.stdout or result.stderr).splitlines()[0]
    return FFmpegInfo(executable=resolved, version_line=first_line)


def _ffmpeg_has_component(executable: str, table: str, component: str) -> bool:
    try:
        result = subprocess.run(
            [executable, "-hide_banner", table],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    for line in result.stdout.splitlines():
        parts = line.split()
        # FFmpeg codec table rows are: capability-flags, component-name, description.
        if len(parts) >= 2 and parts[1] == component:
            return True
    return False


def ffmpeg_has_encoder(executable: str, encoder: str) -> bool:
    return _ffmpeg_has_component(executable, "-encoders", encoder)


def ffmpeg_has_decoder(executable: str, decoder: str) -> bool:
    return _ffmpeg_has_component(executable, "-decoders", decoder)


def _effective(camera: CameraConfig, recording: RecordingConfig) -> tuple[bool, str, str, str]:
    include_audio = (
        recording.include_audio if camera.include_audio is None else camera.include_audio
    )
    video_codec = camera.video_codec or recording.video_codec
    audio_codec = camera.audio_codec or recording.audio_codec
    transport = camera.rtsp_transport or recording.rtsp_transport
    if not include_audio:
        audio_codec = "none"
    return include_audio, video_codec, audio_codec, transport


def _hls_output_args(
    *,
    camera_id: str,
    recording: RecordingConfig,
    ingest_host: str,
    ingest_port: int,
    ingest_secret: str,
    run_id: str,
) -> list[str]:
    url_host = ingest_host.strip().strip("[]")
    if ":" in url_host:
        url_host = f"[{url_host}]"
    segment_url = (
        f"http://{url_host}:{ingest_port}/_ingest/{camera_id}/"
        f"segment_{run_id}_%Y%m%dT%H%M%S_%%06d.ts"
    )
    playlist_url = f"http://{url_host}:{ingest_port}/_ingest/{camera_id}/index.m3u8"
    return [
        "-f",
        "hls",
        "-hls_segment_type",
        "mpegts",
        "-hls_time",
        f"{recording.hls_segment_seconds:g}",
        "-hls_list_size",
        str(recording.live_window_segments),
        "-hls_allow_cache",
        "0",
        "-hls_flags",
        "program_date_time+omit_endlist+second_level_segment_index",
        "-strftime",
        "1",
        "-hls_segment_filename",
        segment_url,
        "-method",
        "PUT",
        "-http_persistent",
        "0",
        "-ignore_io_errors",
        "0",
        "-headers",
        f"X-CamVault-Ingest: {ingest_secret}\r\n",
        playlist_url,
    ]


def build_camera_command(
    *,
    camera: CameraConfig,
    recording: RecordingConfig,
    rtsp_url: str,
    ingest_port: int,
    ingest_secret: str,
    run_id: str | None = None,
    ingest_host: str = "127.0.0.1",
) -> list[str]:
    run_id = run_id or secrets.token_hex(5)
    include_audio, video_codec, audio_codec, transport = _effective(camera, recording)
    args = [
        recording.ffmpeg_path,
        "-hide_banner",
        "-nostdin",
        "-loglevel",
        recording.ffmpeg_loglevel,
        "-rtsp_transport",
        transport,
        "-timeout",
        str(recording.input_timeout_seconds * 1_000_000),
        "-fflags",
        "+genpts+discardcorrupt",
        *recording.extra_input_args,
        "-i",
        rtsp_url,
        "-map",
        "0:v:0",
    ]
    if include_audio and audio_codec != "none":
        args += ["-map", "0:a:0?"]

    if video_codec == "copy":
        args += ["-c:v", "copy"]
    elif video_codec == "h264":
        args += [
            "-c:v",
            "libx264",
            "-preset",
            recording.h264_preset,
            "-crf",
            str(recording.h264_crf),
            "-tune",
            "zerolatency",
            "-pix_fmt",
            "yuv420p",
            "-force_key_frames",
            f"expr:gte(t,n_forced*{recording.hls_segment_seconds:g})",
        ]
    else:  # protected by Pydantic, retained as a defensive check
        raise FFmpegError(f"unsupported video codec mode: {video_codec}")

    if audio_codec == "aac":
        if recording.audio_index_enabled:
            # astats annotates audio frames already being decoded for AAC output;
            # ametadata writes only tiny text measurements to the supervisor pipe.
            args += [
                "-af",
                (
                    "astats=metadata=1:reset=1:measure_perchannel=none:"
                    "measure_overall=RMS_level,"
                    # FFmpeg's filter parser and ametadata's file option each consume
                    # one escaping layer before the pipe protocol sees its colon.
                    "ametadata=print:key=lavfi.astats.Overall.RMS_level:"
                    "file=pipe\\\\:1:direct=1"
                ),
            ]
        args += ["-c:a", "aac", "-b:a", recording.audio_bitrate]
    elif audio_codec == "copy":
        args += ["-c:a", "copy"]
    elif audio_codec == "none":
        args += ["-an"]
    else:
        raise FFmpegError(f"unsupported audio codec mode: {audio_codec}")

    args += [
        "-max_muxing_queue_size",
        "2048",
        "-avoid_negative_ts",
        "make_zero",
        "-fps_mode:v",
        recording.fps_mode,
        *recording.extra_output_args,
    ]
    args += _hls_output_args(
        camera_id=camera.id,
        recording=recording,
        ingest_host=ingest_host,
        ingest_port=ingest_port,
        ingest_secret=ingest_secret,
        run_id=run_id,
    )
    return args


def build_synthetic_command(
    *,
    camera_id: str,
    recording: RecordingConfig,
    ingest_port: int,
    ingest_secret: str,
    duration_seconds: float = 6.0,
    run_id: str = "selftest",
    ingest_host: str = "127.0.0.1",
) -> list[str]:
    encoder = "libx264" if ffmpeg_has_encoder(recording.ffmpeg_path, "libx264") else "mpeg2video"
    args = [
        recording.ffmpeg_path,
        "-hide_banner",
        "-nostdin",
        "-loglevel",
        "error",
        "-re",
        "-f",
        "lavfi",
        "-i",
        "testsrc2=size=320x180:rate=10",
        "-t",
        f"{duration_seconds:g}",
        "-map",
        "0:v:0",
        "-an",
        "-c:v",
        encoder,
    ]
    if encoder == "libx264":
        args += [
            "-preset",
            "ultrafast",
            "-tune",
            "zerolatency",
            "-pix_fmt",
            "yuv420p",
            "-g",
            "10",
            "-keyint_min",
            "10",
            "-sc_threshold",
            "0",
        ]
    else:
        args += ["-g", "10"]
    args += _hls_output_args(
        camera_id=camera_id,
        recording=recording,
        ingest_host=ingest_host,
        ingest_port=ingest_port,
        ingest_secret=ingest_secret,
        run_id=run_id,
    )
    return args


def redacted_command(command: list[str], *, secrets_to_hide: tuple[str, ...] = ()) -> str:
    redacted: list[str] = []
    for argument in command:
        if argument.startswith(("rtsp://", "rtsps://")):
            redacted.append(redact_url(argument))
        else:
            redacted.append(redact_text(argument, secrets_to_hide))
    return shlex.join(redacted)
