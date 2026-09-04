# CamVault

CamVault 是一个面向家庭多摄像头、7×24 运行的 ONVIF/RTSP 录像工具。

- Python 3.11+，使用 `uv` 管理项目；Windows、macOS、Linux 共用一套代码。
- 带用户名/密码的 ONVIF Media1/Media2 自动取流，也支持直接填写 RTSP URL。
- 每台摄像头独立 FFmpeg、断流检测、指数退避重连和流边界标记。
- HLS 小分片经回环 HTTP PUT 进入有界 RAM，不先落大量临时小文件。
- 可选两种归档后端：
  - `local`：内存聚合后，大文件顺序写 HDD/SSD/NAS 挂载目录；
  - `webdav`：内存聚合后直接流式 PUT 到 AList/WebDAV，不建立本地媒体 spool。
- 录像按 `摄像头/年/月/日/小时` 分区，包含 SHA-256 JSON 侧车。
- 提供带 Token 的 HTML 控制台、直播 HLS、时间范围回放、配置编辑、诊断日志和状态 API。
- 支持保留天数、总容量、最低剩余空间及残留事务清理。
- 日志先进入有界 RAM 环并批量刷入滚动文件，减少高频小写入。

## 1. 架构

```text
ONVIF GetProfiles / GetStreamUri
              │
              ▼
       RTSP（每台摄像头一个 FFmpeg）
              │
              │  约 2 秒一个 MPEG-TS，HTTP PUT 到回环地址
              ▼
┌──────────────────── CamVault / FastAPI ─────────────────────┐
│  每摄像头上传锁                                              │
│       ├── LiveBuffer：有界 RAM 直播窗口 ──> HLS 播放          │
│       └── ArchiveManager：有界 RAM 聚合、后端慢时反压         │
└───────────────────────────┬─────────────────────────────────┘
                            │ StorageBackend
                  ┌─────────┴─────────┐
                  ▼                   ▼
            local 后端           webdav 后端
       .partial -> rename     PUT transaction -> MOVE
                  │                   │
                  ▼                   ▼
        本地 HDD/SSD/NAS       AList -> 远程网盘
```

### 写入边界

`local` 后端减少小文件、目录元数据和临时写放大，但录像内容本身仍必须写入目标盘。

`webdav` 后端保证 CamVault 的**媒体数据路径**不创建本地录像、临时录像或失败回退文件；
录像数据从 FFmpeg 进入 RAM 后直接成为 WebDAV 请求体。要让 AList 下游驱动也不把临时媒体
写到 SSD，还必须把 AList `temp_dir` 指向 tmpfs/RAM disk。AList 的 SQLite、配置、日志和
操作系统 swap 是另一类写入，不能与录像媒体路径混为一谈。

详见 [`docs/ALIST_WEBDAV.md`](docs/ALIST_WEBDAV.md)。

## 2. 前置条件

1. Python 3.11 或更高版本；
2. `uv`；
3. FFmpeg/ffprobe，并位于 `PATH`；
4. 摄像头和运行主机网络互通；
5. WebDAV 模式下，已有可写的 AList/WebDAV 服务。

检查：

```bash
python --version
uv --version
ffmpeg -version
ffprobe -version
```

## 3. 快速开始

### Linux / macOS

```bash
unzip camvault-0.3.0.zip
cd camvault
uv sync
uv run camvault init

export CAMVAULT_PLAYBACK_TOKEN='换成至少32位随机字符串'
export CAMVAULT_LIVING_ROOM_PASSWORD='摄像头密码'
export CAMVAULT_DOOR_PASSWORD='摄像头密码'

uv run camvault doctor -c config.toml
uv run camvault camera-check -c config.toml --camera living_room
uv run camvault self-test
uv run camvault serve -c config.toml
```

### Windows PowerShell

```powershell
Expand-Archive .\camvault-0.3.0.zip -DestinationPath .
Set-Location .\camvault
uv sync
uv run camvault init

$env:CAMVAULT_PLAYBACK_TOKEN = '换成至少32位随机字符串'
$env:CAMVAULT_LIVING_ROOM_PASSWORD = '摄像头密码'
$env:CAMVAULT_DOOR_PASSWORD = '摄像头密码'

uv run camvault doctor -c .\config.toml
uv run camvault camera-check -c .\config.toml --camera living_room
uv run camvault self-test
uv run camvault serve -c .\config.toml
```

`uv sync` 会生成/更新 `uv.lock`。首次同步完成后，7×24 服务模板使用
`uv run --no-sync --no-dev`，避免重启时依赖外网。

`server.host` 只支持以下绑定方式，因为 FFmpeg 私有上传端点必须能通过回环地址访问：

- 仅本机播放：`127.0.0.1`、`localhost` 或 `::1`；
- 局域网播放：`0.0.0.0` 或 `::`，并配置播放 Token；
- 不要只绑定 `192.168.x.x` 一类具体网卡地址；来源限制应放在防火墙/反向代理层。

## 4. 配置摄像头

生成配置：

```bash
uv run camvault init -o config.toml
```

### 4.1 ONVIF 自动获取 RTSP

```toml
[[cameras]]
id = "living_room"
name = "客厅"
enabled = true
host = "192.168.1.101"
onvif_port = 80
username = "admin"
password_env = "CAMVAULT_LIVING_ROOM_PASSWORD"
```

默认优先尝试 Media2，再回退 Media1；没有指定 Profile 时选择声明分辨率最高的 Profile。
也可显式设置：

```toml
profile_name = "main"
# profile_token = "Profile_1"
# profile_index = 0
```

摄像头时钟不准导致 WS-Security 失败时，可按实际偏差设置：

```toml
onvif_clock_offset_seconds = 120
```

### 4.2 直接 RTSP

```toml
[[cameras]]
id = "garage"
name = "车库"
rtsp_url = "rtsp://192.168.1.103:554/Streaming/Channels/101"
username = "admin"
password_env = "CAMVAULT_GARAGE_PASSWORD"
```

完整 RTSP URL 含凭据时，应从环境变量读取：

```toml
rtsp_url_env = "CAMVAULT_GARAGE_RTSP_URL"
```

### 4.3 查找和验证

```bash
uv run camvault discover --timeout 5
uv run camvault camera-check -c config.toml --camera living_room --seconds 3
```

WS-Discovery 依赖组播。发现不到设备不代表 ONVIF 不可用；VLAN、AP 客户端隔离、VPN、容器
网络和防火墙都可能阻断组播，已知 IP 时直接配置更可靠。检查输出中的密码会被脱敏。

## 5. 本地存储后端

```toml
[storage]
backend = "local"
root = "/surveillance/camvault"
timezone = "Asia/Shanghai"
archive_chunk_seconds = 300
max_buffer_mb_per_camera = 128
fsync = false
```

Windows 示例：

```toml
root = "D:/CamVault/recordings"
```

目录结构：

```text
recordings/
├── .camvault-root
├── living_room/
│   └── 2026/09/04/20/
│       ├── 20260904T200000+0800_000000000120_000300000ms_s12ab..._a1b2....ts
│       └── 同名.json
└── door/...
```

写入流程：

1. RAM 聚合 2 秒分片；
2. 顺序写一个 `.partial` 文件；
3. 生成 JSON 元数据；
4. 原子重命名媒体和元数据；
5. 异常时清理事务残留。

保留任务只删除带合法 CamVault 元数据侧车的媒体，不会把根目录中任意 `.ts` 当作受管文件。
CamVault 0.2 能继续识别和播放 0.1 生成的旧文件名。

## 6. AList / WebDAV 存储后端

### 6.1 AList 准备

1. 在 AList 中把目标网盘挂载到例如 `/Cloud`；
2. 创建专用目录 `/Cloud/CamVault`；
3. 创建最小权限用户，开放 WebDAV 读取、WebDAV 管理、创建/上传、重命名/移动和删除；
4. AList 与 CamVault 同机时使用 `127.0.0.1:5244`；
5. 把 AList `temp_dir` 指向 tmpfs/RAM disk，避免某些网盘驱动内部缓存落 SSD。

### 6.2 CamVault 配置

```bash
export CAMVAULT_WEBDAV_USERNAME='camvault'
export CAMVAULT_WEBDAV_PASSWORD='强密码'
```

```toml
[storage]
backend = "webdav"
timezone = "Asia/Shanghai"
archive_chunk_seconds = 300
max_buffer_mb_per_camera = 256
retention_days = 30
max_storage_gb = 0
# AList/网盘不暴露 DAV quota 时必须设 0。
min_free_gb = 0

[storage.webdav]
url = "http://127.0.0.1:5244/dav"
root = "/Cloud/CamVault"
username_env = "CAMVAULT_WEBDAV_USERNAME"
password_env = "CAMVAULT_WEBDAV_PASSWORD"
verify_tls = true
connect_timeout_seconds = 10
request_timeout_seconds = 900
max_connections = 8
atomic_upload = true
targeted_scan_max_hours = 168
max_index_response_mb = 64
```

WebDAV URL 禁止内嵌账号密码，避免凭据进入日志或异常信息。

### 6.3 上线前强制检查

```bash
uv run camvault storage-check -c config.toml
```

它会使用小型内存负载实际执行：

```text
OPTIONS -> MKCOL -> PUT -> MOVE -> GET -> DELETE
```

这能发现“可以登录但不能重命名/删除”“AList 用户权限不足”“具体网盘驱动不支持 MOVE”等
问题。`atomic_upload=true` 时，媒体和 JSON 先写 `*.camvault-partial`，再通过 MOVE 提交；
播放索引只接纳媒体与侧车同时存在的记录。

Docker tmpfs 示例位于：

```text
deploy/alist-no-ssd/compose.override.example.yml
```

完整权限、RAM disk、AList SQLite/日志边界和故障语义见
[`docs/ALIST_WEBDAV.md`](docs/ALIST_WEBDAV.md)。

## 7. 内存、磁盘寿命和断网取舍

关键配置：

```toml
[storage]
archive_chunk_seconds = 300
max_buffer_mb_per_camera = 256

[recording]
hls_segment_seconds = 2
live_window_segments = 8
max_live_memory_mb_per_camera = 64
max_ingest_segment_mb = 64
```

| 参数 | 作用 |
|---|---|
| `hls_segment_seconds` | FFmpeg 小分片目标时长，影响直播延迟 |
| `live_window_segments` | 浏览器可见直播窗口 |
| `archive_chunk_seconds` | 大归档目标时长，越大则网盘对象/API 越少 |
| `max_buffer_mb_per_camera` | 每摄像头归档数据硬预算；满后反压，不无限增内存 |
| `max_live_memory_mb_per_camera` | 直播窗口字节硬上限 |
| `max_ingest_segment_mb` | 单个 HTTP 分片上限，必须不大于归档预算 |
| `fsync` | 仅本地后端；强制刷盘增强断电一致性但增加同步写 |

实际批次在“达到时间”或“达到字节预算”时结束，以先到者为准。单摄像头 4 Mbit/s：

| 目标时长 | 约媒体大小 | 媒体文件/天 | 媒体+元数据 PUT/MOVE 主请求/天 |
|---:|---:|---:|---:|
| 300 秒 | 143 MiB | 288 | 1152 |
| 600 秒 | 286 MiB | 144 | 576 |
| 900 秒 | 429 MiB | 96 | 384 |

普通消费级网盘优先考虑 5～10 分钟、每摄像头 256～512 MiB 的归档预算，避免大量小对象和
API 调用；仍需根据上行带宽、网盘单文件限制、内存总量和风控策略实测。

媒体 RAM 的稳态预算约为：

```text
摄像头数 × (max_buffer_mb_per_camera + max_live_memory_mb_per_camera)
```

接收一个新 HTTP 分片时还有瞬时内存。Python 不写本地文件不代表 OS 一定不换页；对
“物理上绝不写 SSD”有硬要求时还需评估 swap。

### 断网/网盘故障

WebDAV 写入失败后，已封存批次留在 RAM 中指数退避重试；不会悄悄切换到 SSD。达到内存
硬预算后，后续 FFmpeg 上传被反压。网络恢复后当前批次可继续提交，但故障持续超过内存
能力时，新录像无法无限缓存，可能形成缺口。

“零本地 spool”和“任意时长断网不丢录像”不可同时满足。长时间离线容灾必须至少采用：
摄像头 SD 卡、本地 NVR/HDD spool、更大 RAM 中的一种。

进程或机器突然退出时，尚未提交的 RAM 批次会丢失。风险窗口约为：

```text
min(archive_chunk_seconds, max_buffer_mb_per_camera / 实际码率)
```

## 8. 录像容量

```bash
uv run camvault estimate --bitrate-mbps 4 --cameras 4 --days 30
```

十进制估算：

```text
GB = 码率(Mbit/s) × 86400 × 天数 × 摄像头数 ÷ 8 ÷ 1000
```

单台 4 Mbit/s 约 43.2 GB/天；4 台连续 30 天约 5.184 TB，尚未包含音频、容器和文件系统
开销。RAM 聚合只能优化写入形态，不能消除这些必要媒体字节。

## 9. 自动清理

```toml
[storage]
retention_days = 30
max_storage_gb = 0
min_free_gb = 10
retention_check_seconds = 3600
partial_max_age_hours = 24
```

规则：

1. 删除超过 `retention_days` 的录像；
2. 总量超过 `max_storage_gb` 时从最旧开始删除；
3. 本地可用空间，或 WebDAV 暴露的 DAV quota，低于 `min_free_gb` 时继续删除；
4. 清理超时事务对象和无元数据的受管孤儿媒体。

服务启动后立即执行一次，之后按 `retention_check_seconds` 周期执行；归档写入失败时还会
唤醒一次紧急清理。每次运行的原因、时间、删除量和错误都会出现在控制台状态与日志中。

WebDAV 服务不提供 `DAV:quota-available-bytes` 时，`min_free_gb` 无法可靠执行，应设为 0，
以时间或总容量规则为主。远端总量清理需要扫描对象树，目录非常大时会增加 AList/网盘 API
压力。

手工执行一次：

```bash
uv run camvault retention -c config.toml
```

## 10. 播放和 API

假设 CamVault 为 `192.168.1.50:8088`：

```text
HTML 控制台（状态、配置、日志、清理、播放器入口）
http://192.168.1.50:8088/?token=TOKEN

直播
http://192.168.1.50:8088/live/living_room/index.m3u8?token=TOKEN

最近一小时历史录像
http://192.168.1.50:8088/vod/living_room/index.m3u8?token=TOKEN

指定时间（无时区时按 storage.timezone）
http://192.168.1.50:8088/vod/living_room/index.m3u8?start=2026-09-04T20:00:00&end=2026-09-04T21:00:00&token=TOKEN

状态
http://192.168.1.50:8088/api/status?token=TOKEN
```

控制台可以原子保存 `config.toml`；保存前会完整校验 TOML 和运行时安全规则，用 SHA-256
修订号阻止覆盖他人的新改动，并保留一个 `config.toml.bak`。配置保存后需重启服务生效。
配置、日志和手动清理 API 不接受 URL 查询参数里的 Token，只接受 `Authorization: Bearer`
或 `X-CamVault-Token` 请求头，避免把管理凭据带入 URL。

### 缓冲日志

```toml
[logging]
file = "./logs/camvault.log" # 设为 "" 可关闭文件日志
memory_records = 2000        # 控制台 RAM 环
batch_records = 128          # 满批次时刷盘
flush_seconds = 30           # 最长驻留时间；ERROR 会立即刷盘
max_file_mb = 20
backup_count = 5
```

日志文件路径相对配置文件解析。若录像位于机械盘/NAS，可将日志路径也放到那里；敏感 URL、
Token 和已解析的密码会在进入 RAM 与文件前脱敏。关闭文件日志可实现 CamVault 日志零落盘，
代价是进程退出后仅保留控制台/服务管理器捕获的输出。

VLC、IINA、ffplay 可直接打开 M3U8。Safari 使用原生 HLS；其他现代浏览器通过固定版本的
hls.js CDN。录像/API 不依赖该 CDN。

WebDAV 模式下，浏览器不会获得 AList 凭据。CamVault 在服务端代理远端 GET，并透传 Range、
206、Content-Range 和 416；媒体仍不落本地回放缓存。链路变为：

```text
网盘 -> AList -> CamVault -> 播放器
```

历史播放只包含已提交的归档。不同 FFmpeg `stream_id` 或明显时间缺口之间会插入
`#EXT-X-DISCONTINUITY`。

### 编码兼容性

- `video_codec="copy"`：CPU 最低、无画质损失；摄像头输出 H.265 时多数浏览器不兼容；
- `video_codec="h264"`：浏览器兼容更好，但显著增加 CPU/GPU 功耗；
- `audio_codec="aac"`：把常见 G.711 等转成更兼容的 AAC；
- `audio_codec="copy"`：最低 CPU，但浏览器可能无声；
- `audio_codec="none"`：完全不录音。

视频直拷贝只能在关键帧附近切片。摄像头 GOP 很长时，实际分片和直播延迟会大于配置值，
建议关键帧间隔 1～2 秒。

## 11. 安全

1. 摄像头密码、WebDAV 密码和播放 Token 使用环境变量，不提交 `.env`/`config.toml`；
2. WebDAV 采用独立 AList 最小权限用户，不使用管理员账号；
3. AList 同机时只走回环地址；跨机使用 HTTPS 且不要关闭证书验证；
4. 不把 CamVault 8088 或 AList 5244 直接暴露公网；使用 WireGuard/Tailscale 或 HTTPS 反代；
5. 默认关闭 Uvicorn access log，避免查询参数 Token 进入普通访问日志；
6. FFmpeg 通过进程参数接收 RTSP URL，本机管理员仍可能查看凭据；
7. 录像本身不加密；本地盘使用 BitLocker/FileVault/LUKS，远端依赖网盘加密模型；
8. 播放 Token 不是 TLS，同网段明文 HTTP 仍可能被窃听。

详见 [`SECURITY.md`](SECURITY.md)。

## 12. 7×24 服务

- Linux：`deploy/systemd/camvault.service.example`
- macOS：`deploy/launchd/com.camvault.recorder.plist.example`
- Windows：`deploy/windows/install-task.ps1`

先手工执行 `uv sync --no-dev`，再用 `uv run --no-sync --no-dev` 启动。服务环境中需要同时
提供摄像头密码、播放 Token，以及 WebDAV 模式下的账号密码。

本地后端必须给服务账号目标录像目录写权限。WebDAV 后端不需要本地录像目录，但服务日志、
AList 日志、AList SQLite 和容器日志仍应按“是否允许写 SSD”的目标单独配置。

## 13. 自测

快速合成视频端到端测试：

```bash
uv run camvault self-test
```

覆盖：FFmpeg HLS HTTP PUT、回环鉴权、有界直播 RAM、RAM 聚合、本地原子归档、带 Token
播放列表、断流 discontinuity，以及 ffprobe 可解析性。

完整测试：

```bash
uv run pytest -q
uv build --offline
```

自动化测试还覆盖：

- 配置/凭据/路径安全；
- ONVIF WS-Security、Media1/Media2、Profile 选择和 WS-Discovery；
- 本地分区、哈希、事务写入、保留策略；
- WebDAV OPTIONS/MKCOL/PUT/MOVE/PROPFIND/GET/DELETE；
- HTTP `Content-Length` 和分片流式请求体；
- 失败时不创建本地 fallback；
- WebDAV Range 206/416 代理；
- 远端保留策略与事务清理；
- CamVault 0.1 文件名兼容。

真实 AList/网盘组合仍必须在目标机器上执行 `storage-check`：不同网盘驱动在分片、哈希、
MOVE、配额和限速方面并不等价。

## 14. 已知边界

- 连续录像，不含移动侦测、AI 事件识别或事件索引；
- 不做多机高可用；
- 不是完整 NVR/VMS，不实现 PTZ、ONVIF Profile G、双向语音或厂商私有告警；
- WebDAV 大范围历史/容量清理依赖 PROPFIND，极大对象树需要进一步做远端索引；
- `atomic_upload` 是基于临时对象和 MOVE 的可见性提交，不等于所有网盘都提供数据库级事务；
- 远端模式没有磁盘队列，这是避免 SSD 写入的主动取舍；
- 核心代码跨平台，但三种操作系统的服务模板没有在本次环境逐一安装验证。

## 15. 常用命令

```bash
uv run camvault init [-o config.toml]
uv run camvault discover [--timeout 5]
uv run camvault doctor -c config.toml
uv run camvault storage-check -c config.toml
uv run camvault camera-check -c config.toml --camera CAMERA_ID [--seconds 3]
uv run camvault self-test
uv run camvault retention -c config.toml
uv run camvault estimate --bitrate-mbps 4 --cameras 4 --days 30
uv run camvault serve -c config.toml [--log-level info]
```

## License

MIT
