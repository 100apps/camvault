# Changelog

## 0.7.0 — 2026-09-04

- Added per-camera MP4 download for the time range selected on the history timeline.
- Exports local and WebDAV archives through a bounded streaming pipeline: one concurrent
  export, no temporary media file and FFmpeg stream copy instead of video transcoding.
- Limited one export to 24 hours and added cancellation cleanup so abandoned browser
  downloads promptly close remote reads and the FFmpeg process.

## 0.6.0 — 2026-09-04

- Added synchronized 0.5×–8× history playback and removed the redundant codec badge
  overlay from camera video.
- Added configurable low-overhead audio activity indexing in the existing FFmpeg AAC
  path, with per-segment RMS levels stored in each local/WebDAV JSON sidecar.
- Added orange sound activity marks, click-to-select and next-sound navigation to the
  zoomable timeline. WebDAV timelines read a bounded bitmap from archive filenames, so
  rendering sound marks does not download every sidecar or rescan media.

## 0.5.0 — 2026-09-04

- Switched browser playback to the highest-resolution camera profile and added explicit
  low-CPU H.264 controls (`ultrafast`, CRF 20 and passthrough frame timing) so 4K input is
  not silently reduced or expanded into duplicate frames.
- Rebuilt the dashboard with password sessions, CSRF-protected controls, storage capacity,
  rolling per-camera write volume and a zoomable, draggable multi-camera archive timeline.
- Made the generated deployment configuration WebDAV-first while retaining the local
  backend, added a startup password option and exposed WebDAV quota/managed archive metrics.
- Added an error-only 7x24 FFmpeg logging default, original-stream badges and actionable
  browser codec errors. Documented real N5105 benchmarks showing HEVC passthrough at about
  1% whole-machine CPU for two camera feeds, versus about 23% for software H.264 encoding.
- Added an OpenWrt procd deployment template with a persistent `/data` virtual environment
  and a root-only external secrets file.

## 0.4.0 — 2026-09-04

- Added configurable day/size/free-space limits and bounded oldest-first emergency
  reclamation before retrying a failed archive write.
- Added an efficient multi-camera live/history grid; history playlists hide minute-sized,
  date/hour-partitioned local or WebDAV objects behind one continuous timeline.
- Made H.264 normalization the browser-compatible default so fixed-function H.265 cameras
  do not need to be reconfigured, with explicit FFmpeg decoder/encoder diagnostics.

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
