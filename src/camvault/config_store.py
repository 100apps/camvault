from __future__ import annotations

import hashlib
import os
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path

from camvault.config import AppConfig, parse_config_text


class ConfigStoreError(ValueError):
    """A configuration update could not be completed safely."""


class ConfigConflictError(ConfigStoreError):
    """The file changed after a caller read it."""


@dataclass(frozen=True, slots=True)
class ConfigSnapshot:
    content: str
    revision: str


def _revision(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


class ConfigStore:
    """Read, validate and atomically update one CamVault TOML file."""

    def __init__(self, path: str | Path, *, max_bytes: int = 1024 * 1024) -> None:
        self.path = Path(path).expanduser().resolve()
        self.max_bytes = max_bytes
        self._lock = threading.RLock()

    @property
    def backup_path(self) -> Path:
        return self.path.with_name(f"{self.path.name}.bak")

    def read(self) -> ConfigSnapshot:
        with self._lock:
            try:
                payload = self.path.read_bytes()
            except OSError as exc:
                raise ConfigStoreError(f"cannot read configuration: {exc}") from exc
            if len(payload) > self.max_bytes:
                raise ConfigStoreError(
                    f"configuration is larger than the {self.max_bytes}-byte safety limit"
                )
            try:
                content = payload.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise ConfigStoreError("configuration must be UTF-8") from exc
            return ConfigSnapshot(content=content, revision=_revision(payload))

    def validate(self, content: str) -> AppConfig:
        payload = content.encode("utf-8")
        if len(payload) > self.max_bytes:
            raise ConfigStoreError(
                f"configuration is larger than the {self.max_bytes}-byte safety limit"
            )
        if "\x00" in content:
            raise ConfigStoreError("configuration contains a NUL byte")
        try:
            return parse_config_text(content, base_dir=self.path.parent)
        except (OSError, ValueError) as exc:
            raise ConfigStoreError(f"invalid configuration: {exc}") from exc

    def write(self, content: str, *, expected_revision: str | None) -> ConfigSnapshot:
        """Validate then replace the file atomically, retaining one known-good backup."""

        payload = content.encode("utf-8")
        self.validate(content)
        with self._lock:
            current = self.read()
            if expected_revision is None:
                raise ConfigConflictError("a configuration revision is required")
            if not _constant_time_equal(expected_revision, current.revision):
                raise ConfigConflictError(
                    "configuration changed since it was opened; reload before saving"
                )

            mode = self.path.stat().st_mode & 0o777
            self._atomic_replace(self.backup_path, current.content.encode("utf-8"), mode)
            self._atomic_replace(self.path, payload, mode)
            return ConfigSnapshot(content=content, revision=_revision(payload))

    @staticmethod
    def _atomic_replace(destination: Path, payload: bytes, mode: int) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary_name: str | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="wb",
                prefix=f".{destination.name}.",
                suffix=".tmp",
                dir=destination.parent,
                delete=False,
            ) as temporary:
                temporary_name = temporary.name
                temporary.write(payload)
                temporary.flush()
                os.fsync(temporary.fileno())
            os.chmod(temporary_name, mode)
            os.replace(temporary_name, destination)
        except OSError as exc:
            if temporary_name is not None:
                try:
                    Path(temporary_name).unlink(missing_ok=True)
                except OSError:
                    pass
            raise ConfigStoreError(f"cannot update {destination.name}: {exc}") from exc


def _constant_time_equal(left: str, right: str) -> bool:
    # Revisions are not secrets, but using compare_digest keeps malformed or very long
    # attacker-supplied values from influencing a custom comparison implementation.
    import secrets

    try:
        return secrets.compare_digest(left, right)
    except TypeError:
        return False
