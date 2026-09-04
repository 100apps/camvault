# Security policy and operating notes

CamVault is intended for a trusted home LAN or a private overlay network.

## Secrets

- Prefer `password_env`, `username_env`, `rtsp_url_env`, `playback_token_env`, and the WebDAV credential environment variables.
- Do not commit `config.toml`, `.env`, shell history containing secrets, or service files with plaintext credentials.
- WebDAV URLs containing user information are rejected. CamVault keeps AList credentials server-side and never embeds them in browser playlists.
- RTSP credentials are passed to FFmpeg in its process arguments. Logs redact them, but a privileged local account may inspect process arguments.
- Rotate camera, AList and playback credentials if logs, shell history, task definitions, or backups may have exposed them.

## Network exposure

- Ingest accepts only loopback clients and requires a random process-local secret. For LAN playback, bind `0.0.0.0` or `::`; binding only a concrete LAN IP is intentionally rejected so the private ingest route cannot leave loopback.
- Playback endpoints require a token when bound to a non-loopback address, unless the operator explicitly overrides the safeguard.
- The built-in server does not terminate TLS. Do not expose it or AList WebDAV directly to the public Internet.
- Prefer WireGuard/Tailscale. Otherwise put services behind a maintained HTTPS reverse proxy with authentication and rate limits.
- Non-Safari browser playback loads pinned hls.js from jsDelivr and sends no referrer. Use Safari native HLS or VLC/IINA when the playback machine must make no CDN request.

## Local storage backend

- Run as a dedicated unprivileged account.
- Grant write access only to the configured recording root.
- Retention only deletes `.ts` files with a valid CamVault v1 or v2 JSON sidecar.
- Use full-disk encryption when physical theft is in scope.

## WebDAV / AList backend

- Create a dedicated least-privilege AList account restricted to the CamVault directory. It needs WebDAV read/manage plus create/upload, move/rename and delete capabilities.
- Keep `atomic_upload = true` only when `camvault storage-check` succeeds against the real configured AList storage. Disabling it weakens incomplete-upload visibility guarantees.
- `backend = "webdav"` means CamVault creates no local media spool. It does not guarantee that AList's selected cloud driver never uses `temp_dir`; place that directory on tmpfs/RAM disk when media must not touch SSD.
- AList's persistent SQLite/config data, logs, container runtime logs, Python bytecode and operating-system swap are separate write paths. Move, disable or constrain them according to the required SSD policy.
- Treat remote deletion as destructive. Use a dedicated root such as `/Cloud/CamVault`; do not point retention at a shared WebDAV directory.
- Do not disable TLS verification for a remote WebDAV endpoint. Plain HTTP is acceptable only over loopback or a separately trusted private tunnel.

## Failure model

WebDAV outage data remains in bounded RAM and is retried without falling back to disk. Once the memory budget is exhausted, ingest is backpressured and new footage may be lost. A local HDD/NVR or camera SD card is required for long offline retention.

## Reporting

This generated project has no upstream security contact. Treat it as code you own: review dependencies, run tests, and keep Python, FFmpeg, FastAPI, Uvicorn, HTTPX, AList and the operating system patched.
