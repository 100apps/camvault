# Security policy and operating notes

CamVault is intended for a trusted home LAN or a private overlay network.

## Secrets

- Prefer `password_env`, `username_env`, `rtsp_url_env`, `web_password_env`, `playback_token_env`, and the WebDAV credential/archive-key environment variables.
- Do not commit `config.toml`, `.env`, shell history containing secrets, or service files with plaintext credentials.
- WebDAV URLs containing user information are rejected. CamVault keeps AList credentials server-side and never embeds them in browser playlists.
- RTSP credentials are passed to FFmpeg in its process arguments. Logs redact them, but a privileged local account may inspect process arguments.
- Rotate camera, AList and playback credentials if logs, shell history, task definitions, or backups may have exposed them.
- `CAMVAULT_ARCHIVE_KEY` must contain 32 random bytes encoded as Base64. Keep an offline backup separate from the WebDAV provider; losing it permanently loses access to encrypted archives.

## Network exposure

- Ingest accepts only loopback clients and requires a random process-local secret. For LAN playback, bind `0.0.0.0` or `::`; binding only a concrete LAN IP is intentionally rejected so the private ingest route cannot leave loopback.
- Playback endpoints require a browser password or API token when bound to a non-loopback address, unless the operator explicitly overrides the safeguard.
- Browser sessions use an expiring HttpOnly, SameSite=Strict cookie; state-changing and control-console requests also require a per-process CSRF token.
- The built-in server does not terminate TLS. Do not expose it or AList WebDAV directly to the public Internet.
- Prefer WireGuard/Tailscale. Otherwise put services behind a maintained HTTPS reverse proxy with authentication and rate limits.
- Non-Safari browser playback loads pinned hls.js from jsDelivr and sends no referrer. Use Safari native HLS or VLC/IINA when the playback machine must make no CDN request.

## Local storage backend

- Run as a dedicated unprivileged account.
- Grant write access only to the configured recording root.
- Retention only deletes `.ts` files with a valid CamVault v1 or v2 JSON sidecar.
- Use full-disk encryption when physical theft is in scope.

## WebDAV / AList backend

- Enable `encryption_enabled = true`. New video and metadata objects are encrypted before upload with independently nonced, chunked AES-256-GCM; every decrypted chunk is authenticated before it is released to a player or export.
- Encryption does not hide directory names, camera IDs, timestamps, durations, sizes or the compact audio-activity bitmap. It also does not protect a running CamVault host after root compromise, because that host necessarily holds the decryption key.
- Existing plaintext `.ts` archives remain readable for compatibility and are not rewritten automatically. Treat them as plaintext until retention deletes them or they are migrated separately.
- Create a dedicated least-privilege AList account restricted to the CamVault directory. It needs WebDAV read/manage plus create/upload, move/rename and delete capabilities.
- Keep `atomic_upload = true` only when `camvault storage-check` succeeds against the real configured AList storage. Disabling it weakens incomplete-upload visibility guarantees.
- `backend = "webdav"` always uses a durable local outbox (default: `spool` beside the config). Each sealed video/index pair is encrypted before it is fsynced locally when encryption is enabled. Directory mode is 0700; file mode is 0600. Metadata manifests contain timing/size/camera information, not credentials or encryption keys. This intentionally replaces the pre-0.10 no-disk policy.
- Keep the outbox on persistent writable storage, not tmpfs. It binds the destination, account and encryption key; use a new dedicated outbox when changing these and keep the original key/configuration to recover old pending recordings. AList's separate `temp_dir` can still use tmpfs to avoid duplicate SSD writes.
- AList's persistent SQLite/config data, logs, container runtime logs, Python bytecode and operating-system swap are separate write paths. Move, disable or constrain them according to the required SSD policy.
- Treat remote deletion as destructive. Use a dedicated root such as `/Cloud/CamVault`; do not point retention at a shared WebDAV directory.
- Do not disable TLS verification for a remote WebDAV endpoint. Plain HTTP is acceptable only over loopback or a separately trusted private tunnel.

## Failure model

Sealed WebDAV batches survive outages and process restarts in the encrypted disk outbox; they are removed only after video and sidecar commits both succeed. The default queue limit is 10 GiB with 1 GiB disk free-space reserve. Unuploaded footage is never automatically evicted: a full or unwritable disk eventually backpressures ingest, so new footage can still be lost. Sudden power loss/SIGKILL can lose the unsealed RAM batch or an interrupted local write; normal stop first attempts to seal it. Filesystem/device durability guarantees still apply. A UPS and camera SD card provide additional protection.

## Reporting

This generated project has no upstream security contact. Treat it as code you own: review dependencies, run tests, and keep Python, FFmpeg, FastAPI, Uvicorn, HTTPX, AList and the operating system patched.
