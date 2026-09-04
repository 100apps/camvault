from __future__ import annotations

import re
from typing import Any
from urllib.parse import quote, urlsplit, urlunsplit

_USERINFO_RE = re.compile(r"(?P<scheme>[A-Za-z][A-Za-z0-9+.-]*://)[^/@\s]+@")


def inject_url_credentials(url: str, username: str | None, password: str | None) -> str:
    """Insert percent-encoded credentials into an RTSP URL.

    Existing userinfo is replaced when a username is supplied. If no username is supplied,
    the URL is returned unchanged.
    """

    if not username:
        return url
    parts = urlsplit(url)
    if parts.scheme.lower() not in {"rtsp", "rtsps"}:
        raise ValueError(f"expected an rtsp/rtsps URL, got {parts.scheme!r}")
    if not parts.hostname:
        raise ValueError("RTSP URL has no hostname")

    encoded_user = quote(username, safe="")
    encoded_password = quote(password or "", safe="")
    userinfo = f"{encoded_user}:{encoded_password}@"

    hostname = parts.hostname
    if ":" in hostname and not hostname.startswith("["):
        hostname = f"[{hostname}]"
    port = f":{parts.port}" if parts.port else ""
    netloc = f"{userinfo}{hostname}{port}"
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


def repair_url_host(url: str, configured_host: str | None) -> str:
    """Replace wildcard/loopback hosts while preserving already-encoded userinfo."""

    if not configured_host:
        return url
    parts = urlsplit(url)
    hostname = (parts.hostname or "").lower()
    if hostname not in {"", "0.0.0.0", "::", "localhost", "127.0.0.1", "::1"}:
        return url

    host = configured_host.strip("[]")
    if ":" in host:
        host = f"[{host}]"

    # Keep the raw substring so `%40`, `%2F`, and similar escapes are not encoded twice.
    userinfo = ""
    if "@" in parts.netloc:
        userinfo = parts.netloc.rsplit("@", 1)[0] + "@"
    port = f":{parts.port}" if parts.port else ""
    return urlunsplit(
        (parts.scheme, f"{userinfo}{host}{port}", parts.path, parts.query, parts.fragment)
    )


def repair_rtsp_host(url: str, configured_host: str | None) -> str:
    """Backward-compatible name for RTSP callers."""

    return repair_url_host(url, configured_host)


def redact_url(url: str) -> str:
    return _USERINFO_RE.sub(lambda match: f"{match.group('scheme')}***:***@", url)


def redact_text(text: str, secrets: list[str] | tuple[str, ...] = ()) -> str:
    redacted = _USERINFO_RE.sub(lambda match: f"{match.group('scheme')}***:***@", text)
    for secret in sorted((item for item in secrets if item), key=len, reverse=True):
        redacted = redacted.replace(secret, "***")
    return redacted


def redact_data(value: Any, secrets: list[str] | tuple[str, ...] = ()) -> Any:
    """Recursively redact URLs and known secrets from JSON-like diagnostic data."""

    if isinstance(value, str):
        return redact_text(value, secrets)
    if isinstance(value, list):
        return [redact_data(item, secrets) for item in value]
    if isinstance(value, tuple):
        return tuple(redact_data(item, secrets) for item in value)
    if isinstance(value, dict):
        return {key: redact_data(item, secrets) for key, item in value.items()}
    return value
