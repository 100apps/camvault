from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import shutil
import sys
from importlib.resources import files
from pathlib import Path
from typing import Any

import uvicorn

from camvault.config import AppConfig, CameraConfig, load_config
from camvault.discovery import discover_onvif
from camvault.ffmpeg import FFmpegError, ffmpeg_has_decoder, ffmpeg_has_encoder, inspect_ffmpeg
from camvault.logging_setup import config_secrets, configure_logging
from camvault.onvif import OnvifError, resolve_camera_rtsp
from camvault.security import redact_data, redact_text, redact_url
from camvault.selftest import SelfTestError, run_self_test
from camvault.service import CamVaultService
from camvault.storage import StorageBackendError, create_storage_backend
from camvault.web import create_app


def _configure_logging(level: str) -> None:
    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper()))
    if any(getattr(handler, "_camvault_owned", False) for handler in root.handlers):
        return
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    handler._camvault_owned = True  # type: ignore[attr-defined]
    root.addHandler(handler)


def _camera_by_id(config: AppConfig, camera_id: str) -> CameraConfig:
    for camera in config.cameras:
        if camera.id == camera_id:
            return camera
    raise ValueError(f"unknown camera id: {camera_id}")


def _json_print(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2))


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("value must be > 0")
    return parsed


def _positive_float(value: str) -> float:
    parsed = float(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("value must be > 0")
    return parsed


async def _storage_health(config: AppConfig) -> dict[str, object]:
    backend = create_storage_backend(config.storage, [camera.id for camera in config.cameras])
    try:
        health = await backend.health_check()
        return {
            "backend": health.backend,
            "location": health.location,
            "detail": health.detail,
            "diskless_media_path": health.diskless_media_path,
        }
    finally:
        await backend.close()


async def _run_retention_once(config: AppConfig) -> dict[str, object]:
    backend = create_storage_backend(config.storage, [camera.id for camera in config.cameras])
    try:
        await backend.start()
        result = await backend.retention()
        return {
            "backend": config.storage.backend,
            "deleted_files": result.deleted_files,
            "deleted_bytes": result.deleted_bytes,
            "remaining_bytes": result.remaining_bytes,
            "free_bytes": result.free_bytes,
        }
    finally:
        await backend.close()


def command_init(args: argparse.Namespace) -> int:
    destination = Path(args.output).expanduser()
    if destination.exists() and not args.force:
        print(f"Refusing to overwrite {destination}; pass --force to replace it.", file=sys.stderr)
        return 2
    content = files("camvault").joinpath("example_config.toml").read_text(encoding="utf-8")
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(content, encoding="utf-8")
    print(f"Created {destination}")
    return 0


def command_discover(args: argparse.Namespace) -> int:
    devices = discover_onvif(args.timeout)
    _json_print(
        [{"xaddrs": list(device.xaddrs), "scopes": list(device.scopes)} for device in devices]
    )
    return 0 if devices else 1


def command_doctor(args: argparse.Namespace) -> int:
    findings: list[dict[str, str]] = []
    try:
        config = load_config(args.config)
        findings.append({"check": "config", "result": "PASS", "detail": str(args.config)})
    except (OSError, ValueError) as exc:
        findings.append({"check": "config", "result": "FAIL", "detail": str(exc)})
        _json_print(findings)
        return 1

    for name, executable in (
        ("ffmpeg", config.recording.ffmpeg_path),
        ("ffprobe", config.recording.ffprobe_path),
    ):
        try:
            info = inspect_ffmpeg(executable)
            findings.append({"check": name, "result": "PASS", "detail": info.version_line})
        except FFmpegError as exc:
            findings.append({"check": name, "result": "FAIL", "detail": str(exc)})

    requires_libx264 = any(
        camera.enabled and (camera.video_codec or config.recording.video_codec) == "h264"
        for camera in config.cameras
    )
    if requires_libx264:
        if ffmpeg_has_encoder(config.recording.ffmpeg_path, "libx264"):
            findings.append(
                {
                    "check": "ffmpeg:libx264",
                    "result": "PASS",
                    "detail": "required H.264 encoder is available",
                }
            )
        else:
            findings.append(
                {
                    "check": "ffmpeg:libx264",
                    "result": "FAIL",
                    "detail": "video_codec=h264 requires an FFmpeg build with libx264",
                }
            )

    for decoder in ("h264", "hevc"):
        available = ffmpeg_has_decoder(config.recording.ffmpeg_path, decoder)
        findings.append(
            {
                "check": f"ffmpeg:decoder:{decoder}",
                "result": "PASS" if available else "WARN",
                "detail": (
                    f"software {decoder.upper()} decoder is available"
                    if available
                    else (
                        f"software {decoder.upper()} decoder is missing; cameras using this "
                        "codec cannot be normalized for browser playback"
                    )
                ),
            }
        )

    try:
        health = asyncio.run(_storage_health(config))
        findings.append(
            {
                "check": "storage",
                "result": "PASS",
                "detail": (
                    f"{health['backend']} · {health['location']} · {health['detail']} · "
                    f"diskless_media_path={health['diskless_media_path']}"
                ),
            }
        )
        if config.storage.backend == "webdav" and config.storage.min_free_gb > 0:
            findings.append(
                {
                    "check": "storage:webdav-quota",
                    "result": "WARN",
                    "detail": (
                        "min_free_gb is enforced only when the WebDAV server exposes "
                        "DAV:quota-available-bytes; max_storage_gb and retention_days "
                        "remain enforceable"
                    ),
                }
            )
    except (OSError, ValueError, StorageBackendError) as exc:
        findings.append({"check": "storage", "result": "FAIL", "detail": str(exc)})

    for camera in config.cameras:
        if not camera.enabled:
            findings.append(
                {
                    "check": f"camera:{camera.id}",
                    "result": "PASS",
                    "detail": "camera is disabled",
                }
            )
            continue
        if (
            camera.rtsp_url_env
            and not os.getenv(camera.rtsp_url_env)
            and not (camera.rtsp_url or camera.host)
        ):
            findings.append(
                {
                    "check": f"camera:{camera.id}:rtsp_url",
                    "result": "FAIL",
                    "detail": f"environment variable {camera.rtsp_url_env} is not set",
                }
            )
        if (
            camera.password_env
            and os.getenv(camera.password_env) is None
            and camera.password is None
        ):
            findings.append(
                {
                    "check": f"camera:{camera.id}:password",
                    "result": "FAIL",
                    "detail": f"environment variable {camera.password_env} is not set",
                }
            )
        elif camera.password:
            findings.append(
                {
                    "check": f"camera:{camera.id}:password",
                    "result": "WARN",
                    "detail": "plain-text password is in config; use password_env",
                }
            )
        else:
            findings.append(
                {
                    "check": f"camera:{camera.id}:credentials",
                    "result": "PASS",
                    "detail": "credential source is available or camera is anonymous",
                }
            )

    if config.server.is_loopback_bind():
        findings.append(
            {
                "check": "playback exposure",
                "result": "PASS",
                "detail": "server binds only to loopback",
            }
        )
    elif config.server.resolved_web_password():
        findings.append(
            {
                "check": "playback exposure",
                "result": "PASS",
                "detail": "LAN bind protected by browser password",
            }
        )
    elif config.server.resolved_playback_token():
        findings.append(
            {
                "check": "playback exposure",
                "result": "PASS",
                "detail": "LAN bind protected by playback token",
            }
        )
    else:
        findings.append(
            {
                "check": "playback exposure",
                "result": "WARN",
                "detail": "LAN bind is explicitly unauthenticated",
            }
        )

    _json_print(findings)
    return 1 if any(item["result"] == "FAIL" for item in findings) else 0


async def _camera_check(
    config: AppConfig, camera: CameraConfig, probe_seconds: int
) -> dict[str, Any]:
    url, profile = await resolve_camera_rtsp(camera)
    result: dict[str, Any] = {
        "camera": camera.id,
        "stream": redact_url(url),
        "profile": (
            {
                "token": profile.token,
                "name": profile.name,
                "width": profile.width,
                "height": profile.height,
            }
            if profile
            else None
        ),
    }
    executable = shutil.which(config.recording.ffprobe_path) or config.recording.ffprobe_path
    transport = camera.rtsp_transport or config.recording.rtsp_transport
    command = [
        executable,
        "-v",
        "error",
        "-rtsp_transport",
        transport,
        "-timeout",
        str(config.recording.input_timeout_seconds * 1_000_000),
        "-read_intervals",
        f"%+{probe_seconds}",
        "-show_streams",
        "-show_format",
        "-of",
        "json",
        url,
    ]
    process = await asyncio.create_subprocess_exec(
        *command,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(
            process.communicate(),
            timeout=config.recording.input_timeout_seconds + probe_seconds + 10,
        )
    except TimeoutError as exc:
        process.kill()
        await process.wait()
        raise RuntimeError("ffprobe timed out while reading the camera stream") from exc
    if process.returncode != 0:
        password = camera.resolved_password() or ""
        raise RuntimeError(redact_text(stderr.decode(errors="replace"), (password,)).strip())
    probe_payload = json.loads(stdout)
    video_stream = next(
        (
            stream
            for stream in probe_payload.get("streams", [])
            if stream.get("codec_type") == "video"
        ),
        None,
    )
    source_codec = video_stream.get("codec_name") if isinstance(video_stream, dict) else None
    target_codec = camera.video_codec or config.recording.video_codec
    if target_codec == "h264" and isinstance(source_codec, str):
        if not ffmpeg_has_decoder(config.recording.ffmpeg_path, source_codec):
            raise RuntimeError(
                f"configured FFmpeg cannot decode camera codec {source_codec!r}; "
                "use a complete FFmpeg build and set recording.ffmpeg_path/ffprobe_path"
            )
        if not ffmpeg_has_encoder(config.recording.ffmpeg_path, "libx264"):
            raise RuntimeError(
                "video_codec=h264 requires a complete FFmpeg build with libx264; "
                "set recording.ffmpeg_path/ffprobe_path to that build"
            )
    result["playback_pipeline"] = {
        "source_video_codec": source_codec,
        "output_video_codec": target_codec,
        "camera_change_required": False,
        "browser_compatible": target_codec == "h264" or source_codec == "h264",
    }
    result["ffprobe"] = redact_data(
        probe_payload,
        (camera.resolved_password() or "",),
    )
    return result


def command_camera_check(args: argparse.Namespace) -> int:
    try:
        config = load_config(args.config)
        camera = _camera_by_id(config, args.camera)
        _json_print(asyncio.run(_camera_check(config, camera, args.seconds)))
        return 0
    except (ValueError, OnvifError, OSError, RuntimeError, json.JSONDecodeError) as exc:
        print(f"Camera check failed: {exc}", file=sys.stderr)
        return 1


def command_self_test(args: argparse.Namespace) -> int:
    try:
        report = asyncio.run(run_self_test(ffmpeg_path=args.ffmpeg, ffprobe_path=args.ffprobe))
        _json_print(report)
        return 0
    except (SelfTestError, FFmpegError, OSError) as exc:
        print(f"Self-test failed: {exc}", file=sys.stderr)
        return 1


def command_storage_check(args: argparse.Namespace) -> int:
    try:
        config = load_config(args.config)
        _json_print(asyncio.run(_storage_health(config)))
        return 0
    except (OSError, ValueError, StorageBackendError) as exc:
        print(f"Storage check failed: {exc}", file=sys.stderr)
        return 1


def command_retention(args: argparse.Namespace) -> int:
    try:
        config = load_config(args.config)
        _json_print(asyncio.run(_run_retention_once(config)))
        return 0
    except (OSError, ValueError, StorageBackendError) as exc:
        print(f"Retention run failed: {exc}", file=sys.stderr)
        return 1


def command_estimate(args: argparse.Namespace) -> int:
    bytes_total = args.bitrate_mbps * 1_000_000 / 8 * 86400 * args.days * args.cameras
    _json_print(
        {
            "cameras": args.cameras,
            "bitrate_mbps_each": args.bitrate_mbps,
            "days": args.days,
            "decimal_gb": round(bytes_total / 1_000_000_000, 2),
            "decimal_tb": round(bytes_total / 1_000_000_000_000, 3),
            "note": (
                "This is payload volume before filesystem overhead. RAM buffering cannot "
                "remove these necessary archive bytes."
            ),
        }
    )
    return 0


def command_serve(args: argparse.Namespace) -> int:
    # Keep startup failures visible before the full buffered logger can be configured.
    _configure_logging(args.log_level)
    try:
        config = load_config(args.config, validate_runtime=False)
        if args.web_password is not None:
            config.server.web_password = args.web_password
            config.server.web_password_env = None
        elif args.web_password_env is not None:
            value = os.getenv(args.web_password_env)
            if not value:
                raise ValueError(
                    f"web password environment variable {args.web_password_env} is not set"
                )
            config.server.web_password = value
            config.server.web_password_env = None
        config.validate_runtime_security()
    except (OSError, ValueError) as exc:
        print(f"Cannot load configuration: {exc}", file=sys.stderr)
        return 2
    log_manager = configure_logging(
        args.log_level,
        config.logging,
        secrets=config_secrets(config),
    )
    try:
        inspect_ffmpeg(config.recording.ffmpeg_path)
    except FFmpegError as exc:
        print(str(exc), file=sys.stderr)
        log_manager.close()
        return 2

    try:
        service = CamVaultService(
            config,
            config_path=args.config,
            recent_logs=log_manager.recent,
        )
    except (OSError, ValueError) as exc:
        print(f"Cannot initialize CamVault: {exc}", file=sys.stderr)
        log_manager.close()
        return 2
    app = create_app(service, manage_service=True, start_recorders=True)
    try:
        uvicorn.run(
            app,
            host=config.server.host,
            port=config.server.port,
            log_level=args.log_level.lower(),
            access_log=config.server.access_log,
            log_config=None,
        )
    finally:
        log_manager.close()
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="camvault",
        description="Cross-platform ONVIF/RTSP recorder with RAM-buffered HLS playback",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    init_parser = subparsers.add_parser("init", help="write an example configuration")
    init_parser.add_argument("-o", "--output", default="config.toml")
    init_parser.add_argument("--force", action="store_true")
    init_parser.set_defaults(func=command_init)

    discover_parser = subparsers.add_parser("discover", help="discover ONVIF devices on LAN")
    discover_parser.add_argument("--timeout", type=_positive_float, default=3.0)
    discover_parser.set_defaults(func=command_discover)

    doctor_parser = subparsers.add_parser("doctor", help="validate local setup and secrets")
    doctor_parser.add_argument("-c", "--config", default="config.toml")
    doctor_parser.set_defaults(func=command_doctor)

    check_parser = subparsers.add_parser(
        "camera-check", help="resolve one camera and probe its media stream"
    )
    check_parser.add_argument("-c", "--config", default="config.toml")
    check_parser.add_argument("--camera", required=True)
    check_parser.add_argument("--seconds", type=_positive_int, default=3)
    check_parser.set_defaults(func=command_camera_check)

    selftest_parser = subparsers.add_parser(
        "self-test", help="run a synthetic FFmpeg → HTTP → RAM → local-backend integration test"
    )
    selftest_parser.add_argument("--ffmpeg", default="ffmpeg")
    selftest_parser.add_argument("--ffprobe", default="ffprobe")
    selftest_parser.set_defaults(func=command_self_test)

    storage_check_parser = subparsers.add_parser(
        "storage-check",
        help="verify local or WebDAV create/write/move/read/delete without local spooling",
    )
    storage_check_parser.add_argument("-c", "--config", default="config.toml")
    storage_check_parser.set_defaults(func=command_storage_check)

    retention_parser = subparsers.add_parser(
        "retention", help="run retention cleanup once and print the result"
    )
    retention_parser.add_argument("-c", "--config", default="config.toml")
    retention_parser.set_defaults(func=command_retention)

    estimate_parser = subparsers.add_parser("estimate", help="estimate archive storage volume")
    estimate_parser.add_argument("--bitrate-mbps", type=_positive_float, required=True)
    estimate_parser.add_argument("--cameras", type=_positive_int, required=True)
    estimate_parser.add_argument("--days", type=_positive_float, default=30)
    estimate_parser.set_defaults(func=command_estimate)

    serve_parser = subparsers.add_parser("serve", help="start recorder and playback service")
    serve_parser.add_argument("-c", "--config", default="config.toml")
    serve_parser.add_argument(
        "--log-level", choices=["debug", "info", "warning", "error"], default="info"
    )
    password_group = serve_parser.add_mutually_exclusive_group()
    password_group.add_argument(
        "--web-password",
        help="browser login password (visible in process listings; prefer --web-password-env)",
    )
    password_group.add_argument(
        "--web-password-env",
        help="read the browser login password from this environment variable",
    )
    serve_parser.set_defaults(func=command_serve)

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    try:
        code = args.func(args)
    except KeyboardInterrupt:
        code = 130
    raise SystemExit(code)
