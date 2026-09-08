# AList no-SSD media temporary path

This directory contains only an override fragment. Keep the existing AList image, ports,
user and persistent `data` volume from your deployment, then merge the `alist` service fields
from `compose.override.example.yml`.

This applies only to AList's temporary files. CamVault 0.10+ always keeps a separate durable
outbox on persistent disk for outage/restart recovery; never put that outbox in this tmpfs.

After startup, verify inside the container:

```sh
printenv ALIST_TEMP_DIR
mount | grep -E 'alist-temp|/tmp'
df -T /run/alist-temp
```

AList uses the `ALIST_` environment prefix by default, so the variable is `ALIST_TEMP_DIR`.
Only use `TEMP_DIR` when AList is explicitly launched with `--no-prefix`. AList applies the
environment override after loading and rewriting `config.json`, so the effective environment
value is not automatically persisted back to that file. Do not treat an old `temp_dir` value in
`config.json` as proof that the runtime override failed; verify the process environment, tmpfs
mount and actual upload activity. If the AList configuration has `force: true`, edit `temp_dir`
in `config.json` directly because environment overrides are skipped. Keep AList's persistent
`data` volume on an HDD if even
small SQLite/config writes must avoid the system SSD. Do not put the persistent database in
tmpfs unless loss on every reboot is acceptable.

See `docs/ALIST_WEBDAV.md` for sizing, permissions and failure semantics.
