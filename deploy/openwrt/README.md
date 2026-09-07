# OpenWrt / procd

Install the built wheel and its dependencies into a persistent virtual environment under
`/data`, then keep configuration and secrets outside the source tree:

```sh
uv venv /data/camvault/venv
uv pip install --python /data/camvault/venv/bin/python camvault-0.9.0-py3-none-any.whl

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

Use `logread -e camvault` for service logs. With `storage.backend="webdav"`, CamVault does
not create a local media spool; AList's own database, logs and temporary-file policy remain
separate.
