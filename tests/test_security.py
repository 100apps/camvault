from __future__ import annotations

from camvault.security import (
    inject_url_credentials,
    redact_data,
    redact_text,
    redact_url,
    repair_url_host,
)


def test_inject_credentials_percent_encodes_and_replaces_userinfo() -> None:
    result = inject_url_credentials(
        "rtsp://old:old@[2001:db8::1]:8554/live?channel=1",
        "a@b",
        "p/a:ss",
    )
    assert result == "rtsp://a%40b:p%2Fa%3Ass@[2001:db8::1]:8554/live?channel=1"


def test_repair_host_preserves_raw_encoded_credentials() -> None:
    result = repair_url_host(
        "rtsp://a%40b:p%2Fx@0.0.0.0:554/stream",
        "192.168.1.8",
    )
    assert result == "rtsp://a%40b:p%2Fx@192.168.1.8:554/stream"


def test_redaction_covers_urls_and_explicit_secrets() -> None:
    url = "rtsp://admin:verysecret@camera.local/live"
    assert redact_url(url) == "rtsp://***:***@camera.local/live"
    text = redact_text(f"open {url}; token=abc123", ("abc123", "verysecret"))
    assert "verysecret" not in text
    assert "abc123" not in text
    assert "rtsp://***:***@" in text


def test_redact_data_recursively_removes_probe_credentials() -> None:
    payload = {
        "format": {
            "filename": "rtsp://admin:p%40ss@camera/live",
            "comment": "password=p@ss",
        },
        "streams": [{"codec_name": "h264"}],
    }
    redacted = redact_data(payload, ("p@ss",))
    assert redacted["format"]["filename"] == "rtsp://***:***@camera/live"
    assert redacted["format"]["comment"] == "password=***"
