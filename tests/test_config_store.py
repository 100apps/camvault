from __future__ import annotations

from pathlib import Path

import pytest

from camvault.config_store import ConfigConflictError, ConfigStore, ConfigStoreError


def _config(root: str = "./recordings") -> str:
    return f'''[storage]
root = "{root}"
min_free_gb = 0

[logging]
file = "./logs/test.log"

[[cameras]]
id = "front"
rtsp_url = "rtsp://127.0.0.1/stream"
'''


def test_config_store_validates_backs_up_and_revisions(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    original = _config()
    updated = _config("./archive")
    path.write_text(original, encoding="utf-8")
    store = ConfigStore(path)

    first = store.read()
    parsed = store.validate(updated)
    assert parsed.storage.root == (tmp_path / "archive").resolve()
    assert parsed.logging.file == (tmp_path / "logs/test.log").resolve()

    saved = store.write(updated, expected_revision=first.revision)
    assert saved.revision != first.revision
    assert path.read_text(encoding="utf-8") == updated
    assert store.backup_path.read_text(encoding="utf-8") == original

    with pytest.raises(ConfigConflictError, match="changed since"):
        store.write(original, expected_revision=first.revision)


def test_invalid_update_never_changes_config_or_backup(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    original = _config()
    path.write_text(original, encoding="utf-8")
    store = ConfigStore(path)
    revision = store.read().revision

    with pytest.raises(ConfigStoreError, match="invalid configuration"):
        store.write("not = [valid", expected_revision=revision)

    assert path.read_text(encoding="utf-8") == original
    assert not store.backup_path.exists()


def test_config_write_requires_revision(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(_config(), encoding="utf-8")
    with pytest.raises(ConfigConflictError, match="revision is required"):
        ConfigStore(path).write(_config("./new"), expected_revision=None)


def test_runtime_password_allows_safe_edit_without_persisting_secret(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    content = (
        '[server]\nhost = "0.0.0.0"\nplayback_token_env = ""\nweb_password_env = ""\n\n' + _config()
    )
    path.write_text(content, encoding="utf-8")

    with pytest.raises(ConfigStoreError, match="no authentication"):
        ConfigStore(path).validate(content)

    store = ConfigStore(path, runtime_web_password="runtime-only-secret")
    parsed = store.validate(content)
    assert parsed.server.resolved_web_password() == "runtime-only-secret"
    saved = store.write(content, expected_revision=store.read().revision)
    assert saved.content == content
    assert "runtime-only-secret" not in path.read_text(encoding="utf-8")
