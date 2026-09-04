from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from camvault.archive import ensure_storage_root
from camvault.config import (
    AppConfig,
    CameraConfig,
    RecordingConfig,
    ServerConfig,
    StorageConfig,
    WebDAVConfig,
    load_config,
)


def test_load_config_resolves_storage_relative_to_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CAMVAULT_PLAYBACK_TOKEN", "secret")
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        """
[server]
host = "0.0.0.0"
playback_token_env = "CAMVAULT_PLAYBACK_TOKEN"
[storage]
root = "./media"
[[cameras]]
id = "front"
rtsp_url = "rtsp://192.0.2.10/stream"
""",
        encoding="utf-8",
    )
    config = load_config(config_path)
    assert config.storage.root == (tmp_path / "media").resolve()
    assert config.server.resolved_playback_token() == "secret"


def test_concrete_lan_bind_is_rejected_for_private_ingest() -> None:
    config = AppConfig(
        server=ServerConfig(
            host="192.168.1.50",
            playback_token="a-long-enough-token",
            playback_token_env=None,
        ),
        cameras=[CameraConfig(id="one", rtsp_url="rtsp://192.0.2.1/x")],
    )
    with pytest.raises(ValueError, match="private loopback ingest"):
        config.validate_runtime_security()


def test_ipv6_wildcard_uses_ipv6_loopback_for_ingest() -> None:
    server = ServerConfig(host="::")
    assert server.ingest_host() == "::1"
    assert server.supports_loopback_ingest()


def test_lan_bind_requires_authentication() -> None:
    config = AppConfig(
        server=ServerConfig(
            host="0.0.0.0",
            playback_token=None,
            playback_token_env=None,
            allow_unauthenticated_lan=False,
        ),
        cameras=[CameraConfig(id="one", rtsp_url="rtsp://192.0.2.1/x")],
    )
    with pytest.raises(ValueError, match="no playback token"):
        config.validate_runtime_security()


def test_invalid_timezone_is_rejected() -> None:
    with pytest.raises(ValidationError, match="unknown IANA timezone"):
        StorageConfig(timezone="Mars/Olympus_Mons")


def test_duplicate_camera_ids_are_rejected() -> None:
    with pytest.raises(ValidationError, match="duplicate camera ids"):
        AppConfig(
            cameras=[
                CameraConfig(id="same", rtsp_url="rtsp://192.0.2.1/a"),
                CameraConfig(id="same", rtsp_url="rtsp://192.0.2.2/b"),
            ]
        )


def test_ingest_object_must_fit_archive_memory_budget() -> None:
    with pytest.raises(ValidationError, match="max_ingest_segment_mb"):
        AppConfig(
            storage=StorageConfig(max_buffer_mb_per_camera=8),
            recording=RecordingConfig(max_ingest_segment_mb=16),
            cameras=[CameraConfig(id="one", rtsp_url="rtsp://192.0.2.1/x")],
        )


def test_storage_root_cannot_be_filesystem_root() -> None:
    with pytest.raises(ValueError, match="filesystem root"):
        ensure_storage_root(Path(Path.cwd().anchor))


def test_webdav_url_rejects_embedded_credentials() -> None:
    with pytest.raises(ValidationError, match="must not contain credentials"):
        WebDAVConfig(url="http://user:secret@127.0.0.1:5244/dav")
