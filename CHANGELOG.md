# Changelog

## 0.3.0 — 2026-09-04

- Expanded the HTML console with storage/RAM/retention status, configuration editing,
  diagnostic logs, manual cleanup, live viewing and time-range playback.
- Added validated, revision-checked atomic configuration writes with a known-good backup.
- Added redacted in-memory logs and buffered, size-rotated file logs to reduce small writes.
- Added observable scheduled retention runs and immediate cleanup wakeups after archive
  write failures, for both local and WebDAV backends.

## 0.2.0 — 2026-09-04

- Added pluggable `local` and `webdav` archive backends.
- Added direct RAM-to-WebDAV streaming with a declared `Content-Length`; no CamVault local media spool or disk fallback in WebDAV mode.
- Added transaction-object upload plus WebDAV `MOVE` commit and JSON sidecars.
- Added AList-compatible non-hidden transaction names and a destructive tiny-object `storage-check` command.
- Added remote `PROPFIND` indexing, time-targeted VOD scans, Range playback proxying and remote retention.
- Added bounded backpressure during slow or unavailable remote storage.
- Added AList tmpfs/RAM-disk deployment guidance and Compose override.
- Preserved playback compatibility with CamVault 0.1 archive filenames.

## 0.1.0 — 2026-09-04

- Initial ONVIF/RTSP recorder with bounded RAM HLS, local sequential archive writes, retention and playback service.
