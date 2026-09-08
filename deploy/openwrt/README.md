# OpenWrt / procd

Install the built wheel and its dependencies into a persistent virtual environment under
`/data`, then keep configuration and secrets outside the source tree:

```sh
uv venv /data/camvault/venv
uv pip install --python /data/camvault/venv/bin/python camvault-0.10.0-py3-none-any.whl

cp config.toml /data/camvault/config.toml
cp deploy/openwrt/run-camvault.sh.example /data/camvault/run.sh
cp deploy/openwrt/camvault.init.example /etc/init.d/camvault
chmod 600 /data/camvault/config.toml /data/camvault/secrets.env
chmod 700 /data/camvault/run.sh
chmod 755 /etc/init.d/camvault

/etc/init.d/camvault enable
/etc/init.d/camvault start
/etc/init.d/camvault status
```

`/data/camvault/secrets.env` uses POSIX shell assignments and should contain only the
variables selected by `password_env`/`username_env`, for example:

```sh
CAMVAULT_CAMERA_PASSWORD='replace-me'
CAMVAULT_WEBDAV_USERNAME='replace-me'
CAMVAULT_WEBDAV_PASSWORD='replace-me'
CAMVAULT_WEB_PASSWORD='replace-me'
CAMVAULT_ARCHIVE_KEY='one-Base64-encoded-32-byte-random-key'
```

Generate `CAMVAULT_ARCHIVE_KEY` once with `openssl rand -base64 32`, keep the file at mode
`600`, and make a separate offline backup. Never upload the key to the same WebDAV storage;
encrypted archives are unrecoverable if it is lost.

Use `logread -e camvault` for service logs. With `storage.backend="webdav"`, CamVault always
uses a durable encrypted outbox: this config path defaults to `/data/camvault/spool` even
without new settings. Ensure `/data` is mounted and persistent before starting. The queue
defaults to 10 GiB total, keeping 1 GiB disk free. Override `storage.spool_directory`,
`spool_max_gb` and `spool_min_free_gb` when needed. AList's own database, logs and temporary
files are separate; do not place the CamVault outbox in AList's disposable temp directory.

## Shutdown / reboot uploads

Set this deployment-specific value before enabling the init script:

```toml
[server]
shutdown_timeout_seconds = 10
```

On SIGTERM (normal reboot, shutdown, service stop/restart) or SIGINT, CamVault stops
recorders while its HTTP ingest is still listening, commits their final segments and
persists pending video/audio-index archives to the disk outbox before trying WebDAV.
It retries within this deadline; if the cloud is still unavailable, durable batches remain
for automatic replay after startup/recovery. RAM that could not be persisted is reported.
No additional monitoring process is needed.
FFmpeg receives a private stdin quit command first; TERM/KILL are only timeout fallbacks.

The init script uses `STOP=09`, before this deployment's `K10alist` and `K90network`.
It also waits for the captured process PID to exit: procd's delete request alone is
asynchronous and would let AList stop too early. procd may force termination after 14 seconds.
[OpenWrt rcS](https://github.com/openwrt/procd/blob/main/rcS.c) normally gives a shutdown hook
15 seconds before starting forced cancellation, so simply increasing `term_timeout` to
several minutes does **not** preserve AList/network availability during a system reboot.
The generic 120-second application default is suitable only when the service manager grants
that much time (for example the systemd template with `TimeoutStopSec=150`).

When upgrading an existing init script, remove the old stop-order link and regenerate it:

```sh
/etc/init.d/camvault disable
/etc/init.d/camvault enable
```

Check `logread -e 'shutdown drain'`. A normal service restart can verify the drain without
rebooting the router. Sudden power loss/SIGKILL can still lose the current unsealed RAM batch;
already-fsynced disk entries survive and replay. A full/unwritable disk or a batch too large
to persist within the shutdown window can also lose RAM data. A UPS/camera SD card adds protection.
