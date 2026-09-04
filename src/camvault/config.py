from __future__ import annotations

import base64
import binascii
import ipaddress
import os
import re
import tomllib
from pathlib import Path, PurePosixPath
from typing import Literal
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, Field, field_validator, model_validator

_CAMERA_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")


class ServerConfig(BaseModel):
    host: str = "127.0.0.1"
    port: int = Field(default=8088, ge=1, le=65535)
    playback_token: str | None = None
    playback_token_env: str | None = "CAMVAULT_PLAYBACK_TOKEN"
    # Browser users authenticate with a normal password. API clients can continue to use
    # playback_token; keeping the two mechanisms separate avoids putting a reusable
    # browser password in media URLs.
    web_password: str | None = None
    web_password_env: str | None = "CAMVAULT_WEB_PASSWORD"
    session_hours: int = Field(default=24, ge=1, le=720)
    allow_unauthenticated_lan: bool = False
    access_log: bool = False

    def resolved_playback_token(self) -> str | None:
        if self.playback_token_env:
            value = os.getenv(self.playback_token_env)
            if value:
                return value
        return self.playback_token or None

    def resolved_web_password(self) -> str | None:
        if self.web_password_env:
            value = os.getenv(self.web_password_env)
            if value:
                return value
        return self.web_password or None

    def is_loopback_bind(self) -> bool:
        host = self.host.strip().strip("[]")
        if host.lower() == "localhost":
            return True
        try:
            return ipaddress.ip_address(host).is_loopback
        except ValueError:
            return False

    def supports_loopback_ingest(self) -> bool:
        host = self.host.strip().strip("[]")
        return host in {"0.0.0.0", "::"} or self.is_loopback_bind()

    def ingest_host(self) -> str:
        """Return a loopback address accepted by the configured Uvicorn bind."""

        host = self.host.strip().strip("[]")
        if host == "0.0.0.0":
            return "127.0.0.1"
        if host == "::":
            return "::1"
        return host


class WebDAVConfig(BaseModel):
    """WebDAV archive target.

    `url` is the WebDAV service root (for AList normally ``http://host:5244/dav``).
    `root` is a dedicated CamVault collection below that endpoint. Keeping these fields
    separate lets CamVault create every collection component without guessing where the
    WebDAV server root ends.
    """

    url: str = "http://127.0.0.1:5244/dav"
    root: str = "/CamVault"
    username: str | None = None
    username_env: str | None = "CAMVAULT_WEBDAV_USERNAME"
    password: str | None = None
    password_env: str | None = "CAMVAULT_WEBDAV_PASSWORD"
    # Archive encryption is intentionally WebDAV-only: local deployments can rely on
    # filesystem encryption, while cloud-bound objects need protection before upload.
    encryption_enabled: bool = False
    encryption_key_env: str = "CAMVAULT_ARCHIVE_KEY"
    encryption_chunk_kb: int = Field(default=1024, ge=64, le=8192)
    verify_tls: bool = True
    connect_timeout_seconds: float = Field(default=10.0, ge=1.0, le=300.0)
    request_timeout_seconds: float = Field(default=300.0, ge=5.0, le=7200.0)
    max_connections: int = Field(default=4, ge=1, le=64)
    atomic_upload: bool = True
    targeted_scan_max_hours: int = Field(default=168, ge=1, le=24 * 365)
    max_index_response_mb: int = Field(default=64, ge=1, le=1024)
    index_cache_entries: int = Field(default=128, ge=8, le=4096)

    @field_validator("url")
    @classmethod
    def validate_url(cls, value: str) -> str:
        parsed = urlsplit(value.strip())
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("webdav.url must be an absolute http:// or https:// URL")
        if parsed.username is not None or parsed.password is not None:
            raise ValueError(
                "webdav.url must not contain credentials; use username_env/password_env"
            )
        if parsed.query or parsed.fragment:
            raise ValueError("webdav.url must not contain a query string or fragment")
        return value.rstrip("/")

    @field_validator("root")
    @classmethod
    def validate_root(cls, value: str) -> str:
        raw = value.strip().replace("\\", "/")
        path = PurePosixPath("/" + raw.lstrip("/"))
        if str(path) == "/":
            raise ValueError("webdav.root must be a dedicated non-root collection")
        if any(part in {"", ".", ".."} for part in path.parts[1:]):
            raise ValueError("webdav.root contains an invalid path component")
        return str(path)

    def resolved_username(self) -> str | None:
        if self.username_env:
            value = os.getenv(self.username_env)
            if value is not None:
                return value
        return self.username

    def resolved_password(self) -> str | None:
        if self.password_env:
            value = os.getenv(self.password_env)
            if value is not None:
                return value
        return self.password

    def resolved_encryption_key_text(self) -> str | None:
        if not self.encryption_key_env:
            return None
        value = os.getenv(self.encryption_key_env)
        return value.strip() if value else None

    def resolved_encryption_key(self) -> bytes | None:
        encoded = self.resolved_encryption_key_text()
        if encoded is None:
            return None
        try:
            key = base64.b64decode(encoded, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ValueError(
                f"{self.encryption_key_env} must contain one Base64-encoded 32-byte key"
            ) from exc
        if len(key) != 32:
            raise ValueError(
                f"{self.encryption_key_env} must contain one Base64-encoded 32-byte key"
            )
        return key

    @field_validator("encryption_chunk_kb")
    @classmethod
    def validate_encryption_chunk_kb(cls, value: int) -> int:
        if value & (value - 1):
            raise ValueError("webdav.encryption_chunk_kb must be a power of two")
        return value


class StorageConfig(BaseModel):
    backend: Literal["local", "webdav"] = "local"
    root: Path = Path("./recordings")
    webdav: WebDAVConfig = Field(default_factory=WebDAVConfig)
    timezone: str = "Asia/Shanghai"
    # Archive objects are intentionally larger than live HLS segments. One minute keeps
    # remote-object counts reasonable while still giving history playback a quick seek.
    archive_chunk_seconds: float = Field(default=60.0, ge=4.0, le=3600.0)
    max_buffer_mb_per_camera: int = Field(default=128, ge=8, le=4096)
    retention_days: int = Field(default=30, ge=0, le=36500)
    max_storage_gb: float = Field(default=0.0, ge=0.0)
    min_free_gb: float = Field(default=10.0, ge=0.0)
    retention_check_seconds: int = Field(default=3600, ge=30, le=86400)
    partial_max_age_hours: int = Field(default=24, ge=1, le=720)
    write_failure_policy: Literal["retry", "delete_oldest"] = "delete_oldest"
    write_failure_reclaim_mb: int = Field(default=512, ge=1, le=1_048_576)
    write_failure_max_delete_files: int = Field(default=100, ge=1, le=100_000)
    fsync: bool = False

    @field_validator("timezone")
    @classmethod
    def validate_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(f"unknown IANA timezone: {value}") from exc
        return value


class RecordingConfig(BaseModel):
    ffmpeg_path: str = "ffmpeg"
    ffprobe_path: str = "ffprobe"
    hls_segment_seconds: float = Field(default=2.0, ge=0.5, le=30.0)
    live_window_segments: int = Field(default=8, ge=3, le=300)
    max_live_memory_mb_per_camera: int = Field(default=64, ge=4, le=2048)
    max_ingest_segment_mb: int = Field(default=64, ge=2, le=2048)
    rtsp_transport: Literal["tcp", "udp", "http", "https"] = "tcp"
    input_timeout_seconds: int = Field(default=20, ge=3, le=300)
    startup_timeout_seconds: int = Field(default=60, ge=5, le=600)
    no_segment_timeout_seconds: int = Field(default=45, ge=5, le=600)
    health_check_seconds: int = Field(default=5, ge=1, le=60)
    restart_min_seconds: float = Field(default=1.0, ge=0.1, le=60.0)
    restart_max_seconds: float = Field(default=60.0, ge=1.0, le=3600.0)
    include_audio: bool = True
    # H.264 is the safe browser-facing default. Cameras may keep producing H.265/HEVC;
    # CamVault normalizes the stream without requiring a camera-side configuration change.
    video_codec: Literal["copy", "h264"] = "h264"
    audio_codec: Literal["copy", "aac", "none"] = "aac"
    # 48 kbit/s avoids FFmpeg clamping for common 8 kHz mono G.711 camera audio.
    audio_bitrate: str = "48k"
    # Audio activity is measured inside the existing FFmpeg audio path. It adds no
    # second-pass media scan and stores only one small level value per HLS segment.
    # Filtering requires decoded audio, so indexing is active for the AAC mode.
    audio_index_enabled: bool = True
    audio_activity_threshold_db: float = Field(default=-35.0, ge=-120.0, le=0.0)
    # ultrafast trades some archive size for much lower CPU consumption. CRF 20 retains
    # substantially more source detail than FFmpeg's implicit CRF 23 default.
    h264_preset: str = "ultrafast"
    h264_crf: int = Field(default=20, ge=0, le=51)
    # Camera timestamps must not be expanded into duplicated frames. This matters most
    # for low-frame-rate 4K cameras where duplication wastes both CPU and WebDAV space.
    fps_mode: Literal["passthrough", "vfr", "cfr"] = "passthrough"
    # Error-only output avoids repeated timestamp warnings becoming a CPU and disk-log
    # workload on small always-on routers. Operators can temporarily select warning/info
    # when diagnosing a camera.
    ffmpeg_loglevel: Literal["quiet", "panic", "fatal", "error", "warning", "info"] = "error"
    extra_input_args: list[str] = Field(default_factory=list)
    extra_output_args: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_restart_window(self) -> RecordingConfig:
        if self.restart_max_seconds < self.restart_min_seconds:
            raise ValueError("recording.restart_max_seconds must be >= restart_min_seconds")
        return self


class LoggingConfig(BaseModel):
    """Buffered diagnostic logging settings.

    The in-memory ring feeds the web console. File logging is batched so routine log
    traffic does not turn into a stream of tiny writes on the system disk.
    """

    file: Path | None = Path("./logs/camvault.log")
    memory_records: int = Field(default=2000, ge=100, le=100_000)
    batch_records: int = Field(default=128, ge=1, le=10_000)
    flush_seconds: float = Field(default=30.0, ge=1.0, le=3600.0)
    max_file_mb: int = Field(default=20, ge=1, le=4096)
    backup_count: int = Field(default=5, ge=0, le=100)

    @field_validator("file", mode="before")
    @classmethod
    def empty_file_disables_persistence(cls, value: object) -> object:
        return None if value == "" else value


class CameraConfig(BaseModel):
    id: str
    name: str | None = None
    enabled: bool = True

    # Direct RTSP mode. Prefer rtsp_url_env if the URL itself contains credentials.
    rtsp_url: str | None = None
    rtsp_url_env: str | None = None

    # ONVIF mode.
    host: str | None = None
    onvif_port: int = Field(default=80, ge=1, le=65535)
    onvif_https: bool = False
    onvif_device_path: str = "/onvif/device_service"
    verify_tls: bool = False
    onvif_clock_offset_seconds: int = Field(default=0, ge=-86400, le=86400)

    username: str | None = None
    username_env: str | None = None
    password: str | None = None
    password_env: str | None = None

    profile_token: str | None = None
    profile_name: str | None = None
    profile_index: int | None = Field(default=None, ge=0)

    rtsp_transport: Literal["tcp", "udp", "http", "https"] | None = None
    include_audio: bool | None = None
    video_codec: Literal["copy", "h264"] | None = None
    audio_codec: Literal["copy", "aac", "none"] | None = None

    @model_validator(mode="after")
    def validate_camera(self) -> CameraConfig:
        if not _CAMERA_ID_RE.fullmatch(self.id):
            raise ValueError(
                "camera id must start with an alphanumeric character and contain only "
                "letters, digits, '_' or '-' (max 64 chars)"
            )
        if not self.name:
            self.name = self.id
        if not (self.rtsp_url or self.rtsp_url_env or self.host):
            raise ValueError(f"camera {self.id!r} needs rtsp_url/rtsp_url_env or an ONVIF host")
        if not self.onvif_device_path.startswith("/"):
            raise ValueError("onvif_device_path must begin with '/'")
        return self

    def resolved_username(self) -> str | None:
        if self.username_env:
            value = os.getenv(self.username_env)
            if value is not None:
                return value
        return self.username

    def resolved_password(self) -> str | None:
        if self.password_env:
            value = os.getenv(self.password_env)
            if value is not None:
                return value
        return self.password

    def resolved_direct_rtsp_url(self) -> str | None:
        if self.rtsp_url_env:
            value = os.getenv(self.rtsp_url_env)
            if value:
                return value
        return self.rtsp_url

    def device_service_url(self) -> str:
        if not self.host:
            raise ValueError(f"camera {self.id!r} has no ONVIF host")
        scheme = "https" if self.onvif_https else "http"
        host = self.host
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        return f"{scheme}://{host}:{self.onvif_port}{self.onvif_device_path}"


class AppConfig(BaseModel):
    server: ServerConfig = Field(default_factory=ServerConfig)
    storage: StorageConfig = Field(default_factory=StorageConfig)
    recording: RecordingConfig = Field(default_factory=RecordingConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)
    cameras: list[CameraConfig]

    @model_validator(mode="after")
    def validate_app(self) -> AppConfig:
        ids = [camera.id for camera in self.cameras]
        duplicates = sorted({camera_id for camera_id in ids if ids.count(camera_id) > 1})
        if duplicates:
            raise ValueError(f"duplicate camera ids: {', '.join(duplicates)}")
        if not self.cameras:
            raise ValueError("at least one camera is required")
        if self.recording.max_ingest_segment_mb > self.storage.max_buffer_mb_per_camera:
            raise ValueError(
                "recording.max_ingest_segment_mb must be <= "
                "storage.max_buffer_mb_per_camera so one accepted segment always fits "
                "inside the per-camera archive memory budget"
            )
        return self

    def validate_runtime_security(self) -> None:
        if not self.server.supports_loopback_ingest():
            raise ValueError(
                f"server.host={self.server.host!r} cannot accept the private loopback ingest "
                "connection. Use 127.0.0.1/localhost for local-only playback, or "
                "0.0.0.0 (IPv4) / :: (IPv6) for LAN playback."
            )
        if (
            not self.server.is_loopback_bind()
            and not self.server.allow_unauthenticated_lan
            and not self.server.resolved_playback_token()
            and not self.server.resolved_web_password()
        ):
            token_hint = self.server.playback_token_env or "CAMVAULT_PLAYBACK_TOKEN"
            password_hint = self.server.web_password_env or "CAMVAULT_WEB_PASSWORD"
            raise ValueError(
                f"server.host={self.server.host!r} accepts non-local connections, but no "
                f"authentication is set. Set {password_hint} for browser login, set "
                f"{token_hint} for API access, "
                "or explicitly set allow_unauthenticated_lan=true."
            )
        if self.storage.backend == "webdav":
            username = self.storage.webdav.resolved_username()
            password = self.storage.webdav.resolved_password()
            if username is None or password is None:
                raise ValueError(
                    "storage.backend='webdav' requires WebDAV credentials. Set "
                    "CAMVAULT_WEBDAV_USERNAME/CAMVAULT_WEBDAV_PASSWORD or configure the "
                    "corresponding storage.webdav username/password fields."
                )
            if self.storage.webdav.encryption_enabled:
                try:
                    encryption_key = self.storage.webdav.resolved_encryption_key()
                except ValueError as exc:
                    raise ValueError(str(exc)) from exc
                if encryption_key is None:
                    raise ValueError(
                        "WebDAV archive encryption is enabled, but "
                        f"{self.storage.webdav.encryption_key_env} is not set"
                    )


def parse_config_text(
    text: str, *, base_dir: str | Path, validate_runtime: bool = True
) -> AppConfig:
    """Validate TOML text and resolve paths relative to its configuration directory."""

    raw = tomllib.loads(text)
    config = AppConfig.model_validate(raw)
    config_dir = Path(base_dir).expanduser().resolve()
    if not config.storage.root.is_absolute():
        config.storage.root = (config_dir / config.storage.root).resolve()
    else:
        config.storage.root = config.storage.root.expanduser().resolve()
    if config.logging.file is not None:
        if not config.logging.file.is_absolute():
            config.logging.file = (config_dir / config.logging.file).resolve()
        else:
            config.logging.file = config.logging.file.expanduser().resolve()
    if validate_runtime:
        config.validate_runtime_security()
    return config


def load_config(path: str | Path, *, validate_runtime: bool = True) -> AppConfig:
    config_path = Path(path).expanduser().resolve()
    return parse_config_text(
        config_path.read_text(encoding="utf-8"),
        base_dir=config_path.parent,
        validate_runtime=validate_runtime,
    )
