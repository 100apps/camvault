# AList / WebDAV 后端部署说明

> 研究与验证基线：2026-09-04；AList `main` 提交
> `e1c022a9d920559078e5a906d7e1499901857006`，发布版 v3.64.0。

## 1. 结论

CamVault 的 WebDAV 后端按以下媒体路径工作：

```text
摄像头 RTSP
  -> FFmpeg 产生约 2 秒 MPEG-TS 分片
  -> 回环 HTTP PUT
  -> CamVault 有界 RAM
  -> RAM 中聚合成较大归档批次
  -> 分块 AES-256-GCM 加密并认证
  -> WebDAV PUT 事务对象
  -> WebDAV MOVE 提交
  -> AList 存储驱动
  -> 远程网盘
```

在 `storage.backend = "webdav"` 时，CamVault 不创建 `storage.root`，不生成本地录像、
临时录像或失败回退文件。媒体字节从 FFmpeg 进入内存后直接作为 HTTP 请求体发送给
AList。

但是“整个系统绝对不写 SSD”不能只靠 CamVault 一个开关保证，必须同时处理：

1. AList 某些网盘驱动可能为了哈希、分片或随机读取使用自己的 `temp_dir`；
2. AList 默认 SQLite、配置和文件日志位于 `data` 目录；
3. 操作系统可能把匿名内存换出到 swap；
4. Docker 日志、systemd journal、Python/uv 缓存也可能有少量非媒体写入。

因此应区分两个目标：

| 目标 | 可达到程度 | 条件 |
|---|---|---|
| 录像媒体不落本机文件系统 | 可以，由 CamVault 保证 | `backend="webdav"`，无本地 fallback |
| AList 上传临时媒体不落 SSD | 可以，部署层保证 | `temp_dir` 指向 tmpfs/RAM disk |
| 所有程序和系统元数据绝对零 SSD I/O | 通常不值得承诺 | AList data/log、服务日志、swap、容器层都需迁移或关闭 |

## 2. 为什么 AList 可以流式接收，但仍需配置 RAM 临时目录

对 AList 当前源码的检查结果：

- WebDAV `PUT` 处理器把 HTTP `r.Body`、`r.ContentLength` 包装成 `FileStream`，再调用
  `PutDirectly`，WebDAV 层本身没有先把整个上传写成本地文件；
- 底层存储驱动仍可根据能力要求完整缓存或进行 Range/Seek；
- AList 的完整缓存路径最终通过 `CreateTempFile` 落到配置项 `temp_dir`；
- AList 官方配置文档也说明 `temp_dir` 默认是 `data/temp`，启动时会清空。

所以正确架构是“双层零落盘”：

```text
CamVault：禁止本地媒体 spool
AList：把 temp_dir 放到 RAM
```

只做第一层，无法约束某个网盘驱动内部的临时文件；只做第二层，CamVault 自己若先写文件
再上传，仍会磨损 SSD。

源码位置：

- `server/webdav/webdav.go`：WebDAV PUT 请求体进入 FileStream；
- `internal/fs/put.go`、`internal/op/fs.go`：上传转交底层驱动；
- `internal/stream/stream.go`：完整缓存/Range 缓存逻辑；
- `pkg/utils/file.go`：临时文件使用 `conf.Conf.TempDir`；
- `internal/conf/config.go`：`temp_dir` / `TEMP_DIR` 字段；默认环境前缀使实际变量为 `ALIST_TEMP_DIR`。

## 3. AList 侧配置

### 3.1 挂载网盘并准备专用目录

推荐在 AList 中把目标网盘挂载为一个明确路径，例如：

```text
/Cloud
```

先在 AList 中创建：

```text
/Cloud/CamVault
```

不要让摄像头录像写到 AList 虚拟根目录。独立目录可以降低误删风险，也方便单独配置配额、
权限和保留策略。

### 3.2 创建最小权限用户

建议创建专用用户，例如 `camvault`，并把可访问范围限制到目标目录。CamVault 使用的
WebDAV 方法如下：

| 方法 | 用途 |
|---|---|
| `OPTIONS` | 连通性和协议检测 |
| `MKCOL` | 创建日期分区目录 |
| `PUT` | 上传媒体和 JSON 元数据 |
| `MOVE` | 从事务对象提交到最终名称 |
| `PROPFIND` | 查询录像和容量信息 |
| `GET` + `Range` | 历史录像播放 |
| `DELETE` | 保留策略和事务垃圾清理 |

AList 用户至少需要：

- WebDAV 读取；
- WebDAV 管理；
- 创建目录或上传；
- 重命名/移动；
- 删除。

CamVault 0.2 的远端事务对象不是点号开头，因此不要求额外开放“查看隐藏文件”。

### 3.3 WebDAV 地址

AList 通常是：

```text
http://127.0.0.1:5244/dav
```

AList 与 CamVault 在同一台机器时，应使用回环地址，不要绕外网域名。跨机器时使用 HTTPS，
并保持 `verify_tls = true`。

## 4. CamVault 配置

环境变量：

```bash
export CAMVAULT_WEBDAV_USERNAME='camvault'
export CAMVAULT_WEBDAV_PASSWORD='替换成强密码'
export CAMVAULT_ARCHIVE_KEY="$(openssl rand -base64 32)"
```

Windows PowerShell：

```powershell
$env:CAMVAULT_WEBDAV_USERNAME = 'camvault'
$env:CAMVAULT_WEBDAV_PASSWORD = '替换成强密码'
```

`config.toml`：

```toml
[storage]
backend = "webdav"
timezone = "Asia/Shanghai"
archive_chunk_seconds = 600
adaptive_archive_enabled = true
adaptive_archive_min_seconds = 120
adaptive_archive_max_seconds = 1800
adaptive_archive_target_mb = 32
adaptive_memory_percent = 5
adaptive_memory_reserve_mb = 512
max_buffer_mb_per_camera = 128
retention_days = 30
max_storage_gb = 0
# AList/网盘若不提供 DAV quota，设为 0。
min_free_gb = 0
partial_max_age_hours = 24
write_failure_policy = "delete_oldest"
write_failure_reclaim_mb = 512
write_failure_max_delete_files = 100

[storage.webdav]
url = "http://127.0.0.1:5244/dav"
# /Cloud 是 AList 挂载路径，/CamVault 是已准备好的专用目录。
root = "/Cloud/CamVault"
username_env = "CAMVAULT_WEBDAV_USERNAME"
password_env = "CAMVAULT_WEBDAV_PASSWORD"
encryption_enabled = true
encryption_key_env = "CAMVAULT_ARCHIVE_KEY"
encryption_chunk_kb = 1024
verify_tls = true
connect_timeout_seconds = 10
request_timeout_seconds = 900
max_connections = 8
atomic_upload = true
targeted_scan_max_hours = 168
max_index_response_mb = 64
```

凭据不要写入 URL。CamVault 会拒绝：

```text
http://user:password@127.0.0.1:5244/dav
```

`CAMVAULT_ARCHIVE_KEY` 只生成一次，放在 root-only secrets 文件并另做离线备份，不得上传
到同一网盘。新媒体和 JSON 侧车分别以 `.ts.enc` 和 `.json.enc` 保存；每个 1 MiB 块独立
认证，播放器发起 Range 请求时 CamVault 只读取、验证并解密覆盖该范围的密文块。旧 `.ts`
文件仍兼容播放但不会自动迁移。目录、摄像头 ID、时间、时长、大小和声音活动位图仍是可见
元数据；该功能保护文件内容与完整性，不是文件名匿名化。

### 4.1 必须先运行存储协议检查

```bash
uv run camvault storage-check -c config.toml
```

该命令会用很小的内存负载实际执行：

```text
OPTIONS -> MKCOL -> PUT -> MOVE -> GET -> DELETE
```

成功输出应包含：

```json
{
  "backend": "webdav",
  "diskless_media_path": true
}
```

这比只测试“能登录 AList”严格得多：有些网盘能上传但不支持可靠 MOVE，或用户缺少删除、
重命名权限。协议检查失败时不要直接开始 7×24 录像。

### 4.2 原子提交的含义

默认 `atomic_upload = true`：

```text
PUT  xxx.ts.enc.camvault-partial
MOVE xxx.ts.enc.camvault-partial -> xxx.ts.enc
PUT  xxx.json.enc.camvault-partial
MOVE xxx.json.enc.camvault-partial -> xxx.json.enc
```

播放列表只接受同时存在媒体和 JSON 侧车的对象，因此半成品不会进入回放索引。

这是一种“可见性两阶段提交”。WebDAV/AList 下游驱动是否能做到存储系统意义上的原子
rename，由具体网盘能力决定。`storage-check` 会验证实际方法链路，但不能证明远端云服务在
断电级故障下具有数据库事务语义。

只有在底层驱动不支持 MOVE 且接受降低一致性时，才设置：

```toml
atomic_upload = false
```

此时中断上传可能在远端短暂留下最终文件名，不推荐。

## 5. 把 AList 临时目录放入 RAM

### 5.1 Docker / Docker Compose

把 `deploy/alist-no-ssd/compose.override.example.yml` 合并到现有 AList Compose 服务。核心是：

```yaml
services:
  alist:
    environment:
      ALIST_TEMP_DIR: /run/alist-temp
    tmpfs:
      - /run/alist-temp:size=2g,mode=1770
      - /tmp:size=256m,mode=1777
    logging:
      driver: "none"
```

注意：

- AList 默认给环境变量加 `ALIST_` 前缀，因此应使用 `ALIST_TEMP_DIR`；只有显式以 `--no-prefix` 启动时才使用 `TEMP_DIR`；
- 只有配置中的 `force=false` 时才读取环境变量；`force=true` 会跳过环境变量覆盖；
- AList 在读取/改写 `config.json` 之后才应用环境变量覆盖，覆盖值不会自动回写到文件；
  因此使用 `ALIST_TEMP_DIR` 时，不能仅凭 `config.json` 里的旧值判断运行时是否生效；
- 应同时核对容器进程环境、`/run/alist-temp` 的 tmpfs 挂载，并执行一次实际上传时观察该
  tmpfs；若希望配置文件本身也明确可审计，可直接把 `temp_dir` 写成 `/run/alist-temp`；
- `data` 卷仍包含配置和 SQLite。要保护系统 SSD，应把这个小型持久卷放在 HDD，而不是
  放进 tmpfs；否则重启会丢失 AList 配置；
- `logging.driver: none` 会牺牲容器日志。也可以保留限额很小的内存/远端日志方案。

### 5.2 Linux 非容器

多数 systemd Linux 的 `/run` 是 tmpfs。先创建专用目录并限制容量，或显式挂载：

```bash
sudo mkdir -p /run/alist-temp
sudo mount -t tmpfs -o size=2G,mode=1770 tmpfs /run/alist-temp
```

修改 AList `data/config.json`：

```json
{
  "temp_dir": "/run/alist-temp",
  "log": {
    "enable": false
  }
}
```

不要把完整 `data` 目录放到 `/run`，除非你明确接受重启后丢配置和数据库。

### 5.3 macOS

macOS 没有通用的内置 tmpfs 挂载接口，可建立易失 RAM disk。例如 2 GiB：

```bash
DEVICE=$(hdiutil attach -nomount ram://4194304)
diskutil erasevolume APFS ALIST_TMP "$DEVICE"
```

然后将 AList 的 `temp_dir` 指向：

```text
/Volumes/ALIST_TMP/alist-temp
```

RAM disk 重启后消失，需要在 AList 启动前通过 launchd 重新创建。AList 的持久 `data` 目录
仍应放在 HDD 或保留在系统盘并接受少量元数据写入。

### 5.4 Windows

Windows 没有可直接依赖的系统级 RAM disk。需要使用受信任的 RAM-disk 驱动，创建例如
`R:\alist-temp`，再修改 AList：

```json
{
  "temp_dir": "R:\\alist-temp",
  "log": {
    "enable": false
  }
}
```

也可以把 AList 和 CamVault 运行在 WSL2/Linux 中，在 Linux tmpfs 内放置 AList 临时目录。

## 6. 内存与网盘请求数量

远端模式不是“边收到 2 秒分片边立刻上传”。它先在 RAM 中聚合，目的是减少网盘小文件和
API 调用。实际批次结束条件为：

```text
达到 archive_chunk_seconds
或
达到 max_buffer_mb_per_camera
```

以单摄像头 4 Mbit/s 为例：

| 目标批次 | 约媒体大小 | 每天媒体文件数 | PUT+MOVE 主变更请求/天 |
|---:|---:|---:|---:|
| 60 秒 | 29 MiB | 1440 | 5760 |
| 300 秒 | 143 MiB | 288 | 1152 |
| 600 秒 | 286 MiB | 144 | 576 |
| 900 秒 | 429 MiB | 96 | 384 |

每个批次包含媒体和 JSON，各做一次 PUT 和 MOVE，因此主变更请求约为 4 次；目录创建在进程
内有缓存，查询和清理请求另计。

推荐：

- 推荐启用 `adaptive_archive_enabled`，由平滑后的每路码率和 `MemAvailable` 自动计算批次；
- 交互式历史回放优先：降低 `adaptive_archive_max_seconds`，最新归档更快可见；
- 网盘 API 次数/风控优先：提高 `adaptive_archive_max_seconds`，但要接受更长的 RAM 风险窗口；
- 目标字节还受可用内存配额和每路硬上限三分之一约束，内存吃紧时会提前上传；
- 每路达到 `max_buffer_mb_per_camera` 时继续反压，不会为了等待时间目标而无限占用 RAM；
- 4 Mbit/s、5 分钟：`max_buffer_mb_per_camera >= 192`，建议 256；
- 4 Mbit/s、10 分钟：建议 384 或 512；
- `max_connections` 至少覆盖并发上传摄像头数，再留 2~4 个连接给播放和 PROPFIND；
- 不要为了减少对象数量把单文件做得过大，部分网盘会有单次上传超时、分片或限速问题。

粗略公式：

```text
批次 MiB ≈ 码率(Mbit/s) × 秒数 ÷ 8 ÷ 1.048576
自动目标字节 = max(当前分片, 1 MiB 安全下限,
                     min(配置目标, 每路硬上限 / 3, (MemAvailable - 预留) × 比例 / 摄像头数))
自动 n 秒 = clamp(自动目标字节 / 每路平滑字节率, 最短秒数, 最长秒数)
```

CamVault 每台摄像头的长期媒体 RAM 预算约为：

```text
max_buffer_mb_per_camera + max_live_memory_mb_per_camera
```

接收 HTTP 分片时还有最多一个分片的瞬时缓冲。多个摄像头按台数线性增加。

如果 PUT/MOVE 因远端容量不足而首次失败，默认策略会扫描已提交记录、按时间删除最旧录像，
至少尝试回收 `write_failure_reclaim_mb`，但不超过
`write_failure_max_delete_files`，随后立即重试同一个确定性事务。把
`write_failure_policy` 设为 `retry` 可禁用错误触发的删除，仅保留退避重试。

AList 的 tmpfs 容量应覆盖底层驱动最坏情况下同时缓存的完整上传。保守估算：

```text
AList tmpfs >= 最大单批次 × 可能并发上传数 × 1.25
```

若驱动本身真正流式上传，实际占用会低很多；不能只凭平均值缩小 tmpfs，应通过监控确认。

## 7. 故障与数据完整性

### AList 或网盘短时不可用

- 已封存批次保留在 RAM 并指数退避重试；
- CamVault 不切换到本地 SSD；
- 达到每摄像头 RAM 上限后，后续 FFmpeg 上传被反压；
- 网络恢复后，当前保留批次继续提交；
- 故障期间超过 RAM 能力的新录像无法被无限保存，可能形成录像缺口。

这是“零本地 spool”与“任意长离线容灾”之间不可消除的矛盾。要保证长时间断网也不丢录像，
必须增加 HDD spool、摄像头 SD 卡/NVR，或允许更大的内存；三者至少选一个。

### 进程或机器突然退出

尚未提交的 RAM 批次会丢失。风险窗口由以下较小值决定：

```text
archive_chunk_seconds
max_buffer_mb_per_camera / 实际码率
```

### MOVE 成功但客户端超时

批次文件名包含稳定 object id。重试时不会创建另一个随机最终文件；如果目标已存在，后端会
按幂等提交处理并继续补齐元数据侧车。

## 8. 远端回放

CamVault 不把 AList 用户名和密码交给浏览器。浏览器仍访问 CamVault：

```text
/vod/<camera>/index.m3u8
/recordings/<camera>/<partitioned-file>.ts
```

CamVault 在服务端向 AList 发起带凭据的 `GET`，并透传 `Range`、`206 Partial Content`、
`Content-Range` 和 `416 Range Not Satisfiable`。媒体响应以流方式返回，不建立本地回放缓存。

这意味着播放会占用：

```text
网盘 -> AList -> CamVault -> 播放器
```

的带宽和连接。并发回放较多时，需要提高 `storage.webdav.max_connections`，并确认 AList
本地代理并发限制和网盘限速。

## 9. 远端保留策略

支持：

- `retention_days`；
- `max_storage_gb`；
- `partial_max_age_hours`；
- `min_free_gb`，仅当 WebDAV 服务提供 `DAV:quota-available-bytes` 时。

AList/网盘通常未必通过 WebDAV 暴露准确可用容量。此时：

```toml
min_free_gb = 0
```

以 `retention_days` 或 `max_storage_gb` 为主。远端全量容量清理需要扫描对象树；目录非常大时
会增加 AList 和网盘 API 压力，建议优先用时间保留，并把录像按 5~15 分钟聚合。

## 10. 零 SSD 媒体路径核对清单

- [ ] CamVault 使用 `storage.backend = "webdav"`；
- [ ] `uv run camvault storage-check` 成功；
- [ ] CamVault 状态接口显示 `diskless_media_path=true`、`local_media_spool=false`；
- [ ] 配置中的本地 `storage.root` 路径没有被创建；
- [ ] AList `temp_dir` 位于 tmpfs/RAM disk；
- [ ] AList 持久 `data` 在 HDD，或明确接受 SSD 上少量 SQLite/config 写入；
- [ ] AList 文件日志关闭或放到非 SSD；
- [ ] 容器日志有限额或关闭；
- [ ] 主机 RAM 足够，已评估 swap 导致的潜在 SSD 写入；
- [ ] 摄像头自身 SD 卡/NVR承担长时间断网容灾，或接受录像缺口；
- [ ] 远端网盘条款允许持续自动上传，不会因高频请求/大流量触发风控。

## 11. 参考

- AList 仓库：https://github.com/AlistGo/alist
- AList WebDAV 文档：https://alistgo.com/zh/guide/webdav.html
- AList 配置文档：https://alistgo.com/zh/config/configuration.html
