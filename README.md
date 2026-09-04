# CamVault

CamVault 是一个面向家庭多摄像头、7×24 运行的 ONVIF/RTSP 录像工具。

- Python 3.11+，使用 `uv` 管理项目；Windows、macOS、Linux 共用一套代码。
- 带用户名/密码的 ONVIF Media1/Media2 自动取流，也支持直接填写 RTSP URL。
- 每台摄像头独立 FFmpeg、断流检测、指数退避重连和流边界标记。
- HLS 小分片经回环 HTTP PUT 进入有界 RAM，不先落大量临时小文件。
- 可选两种归档后端：
  - `local`：内存聚合后，大文件顺序写 HDD/SSD/NAS 挂载目录；
  - `webdav`：内存聚合后直接流式 PUT 到 AList/WebDAV，不建立本地媒体 spool。
- 录像按 `摄像头/年/月/日/小时` 分区，包含 SHA-256 和分片音量索引 JSON 侧车。
- 提供密码登录的专业多摄像头控制台、主码流直播、倍速回放、按时间与摄像头导出 MP4、带声音活动标记的连续历史时间轴、配置编辑、诊断日志和状态 API。
- 控制台展示本地/WebDAV 容量、CamVault 归档量、最近 60 秒写入量及每路码率。
- 支持保留天数、总容量、最低剩余空间、写失败按最旧录像回收及残留事务清理。
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
3. 完整的 FFmpeg/ffprobe（H.265 摄像头转浏览器 H.264 时需 HEVC 解码器和 libx264）；
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
unzip camvault-0.7.0.zip
cd camvault
uv sync
uv run camvault init

export CAMVAULT_WEB_PASSWORD='浏览器登录强密码'
export CAMVAULT_WEBDAV_USERNAME='alist账号'
export CAMVAULT_WEBDAV_PASSWORD='alist密码'
export CAMVAULT_LIVING_ROOM_PASSWORD='摄像头密码'
export CAMVAULT_DOOR_PASSWORD='摄像头密码'

uv run camvault doctor -c config.toml
uv run camvault camera-check -c config.toml --camera living_room
uv run camvault self-test
uv run camvault serve -c config.toml
```

### Windows PowerShell

```powershell
Expand-Archive .\camvault-0.7.0.zip -DestinationPath .
Set-Location .\camvault
uv sync
uv run camvault init

$env:CAMVAULT_WEB_PASSWORD = '浏览器登录强密码'
$env:CAMVAULT_WEBDAV_USERNAME = 'alist账号'
$env:CAMVAULT_WEBDAV_PASSWORD = 'alist密码'
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
- 局域网播放：`0.0.0.0` 或 `::`，并配置网页登录密码或 API Token；
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
archive_chunk_seconds = 60
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
archive_chunk_seconds = 60
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
index_cache_entries = 128
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
archive_chunk_seconds = 60
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
| `write_failure_policy` | `delete_oldest` 在首次写失败后按最旧录像回收并立即重试；`retry` 只重试 |
| `write_failure_reclaim_mb` | 写失败时至少尝试回收的空间 |
| `write_failure_max_delete_files` | 单次写失败最多删除的录像文件数 |
| `fsync` | 仅本地后端；强制刷盘增强断电一致性但增加同步写 |

实际批次在“达到时间”或“达到字节预算”时结束，以先到者为准。单摄像头 4 Mbit/s：

| 目标时长 | 约媒体大小 | 媒体文件/天 | 媒体+元数据 PUT/MOVE 主请求/天 |
|---:|---:|---:|---:|
| 60 秒 | 29 MiB | 1440 | 5760 |
| 300 秒 | 143 MiB | 288 | 1152 |
| 600 秒 | 286 MiB | 144 | 576 |
| 900 秒 | 429 MiB | 96 | 384 |

默认 1 分钟偏向流畅历史定位。若网盘 API 次数或风控比定位速度更重要，可改为 5～10 分钟、
每摄像头 256～512 MiB 的归档预算；仍需根据上行带宽、网盘单文件限制和内存总量实测。

媒体 RAM 的稳态预算约为：

```text
摄像头数 × (max_buffer_mb_per_camera + max_live_memory_mb_per_camera)
```

接收一个新 HTTP 分片时还有瞬时内存。Python 不写本地文件不代表 OS 一定不换页；对
“物理上绝不写 SSD”有硬要求时还需评估 swap。

### 断网/网盘故障

默认在 WebDAV 或本地写入第一次失败后，先按时间删除最旧的受管录像并立即重试同一批次；
删除量和文件数均受配置限制。若仍失败，已封存批次留在 RAM 中指数退避重试，且不会悄悄切换到 SSD。达到内存
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
write_failure_policy = "delete_oldest"
write_failure_reclaim_mb = 512
write_failure_max_delete_files = 100
```

规则：

1. 删除超过 `retention_days` 的录像；
2. 总量超过 `max_storage_gb` 时从最旧开始删除；
3. 本地可用空间，或 WebDAV 暴露的 DAV quota，低于 `min_free_gb` 时继续删除；
4. 首次归档写失败时，按最旧优先额外回收受配置限制的空间，再立即重试同一事务；
5. 清理超时事务对象和无元数据的受管孤儿媒体。

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
HTML 控制台（密码登录、多摄像头主码流、同步历史时间轴、存储状态）
http://192.168.1.50:8088/

API/外部播放器直播（可选 API Token）
http://192.168.1.50:8088/live/living_room/index.m3u8?token=TOKEN

最近一小时历史录像
http://192.168.1.50:8088/vod/living_room/index.m3u8?token=TOKEN

指定时间（无时区时按 storage.timezone）
http://192.168.1.50:8088/vod/living_room/index.m3u8?start=2026-09-04T20:00:00&end=2026-09-04T21:00:00&token=TOKEN

状态
http://192.168.1.50:8088/api/status?token=TOKEN
```

浏览器认证默认从 `CAMVAULT_WEB_PASSWORD` 读取，也可写在配置中，或仅在启动时提供：

```toml
[server]
web_password_env = "CAMVAULT_WEB_PASSWORD"
# 或 web_password = "仅适合已限制文件权限的配置；环境变量更安全"
session_hours = 24
# API、VLC 或自动化客户端可另设：
playback_token_env = "CAMVAULT_PLAYBACK_TOKEN"
```

```bash
# 推荐：密码不出现在进程列表
CAMVAULT_UI_SECRET='强密码' uv run camvault serve -c config.toml \
  --web-password-env CAMVAULT_UI_SECRET

# 也支持 --web-password，但命令行参数可能被同机用户看到。
```

密码登录后只设置带 `HttpOnly`、`SameSite=Strict` 和有效期的会话 Cookie；密码和会话凭据
不会写入 URL 或 Web Storage。管理请求还必须带页面生成的 CSRF 头。使用明文 HTTP 时无法
阻止同网段窃听，跨不可信网络仍须使用 HTTPS、WireGuard 或 Tailscale。

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

历史播放只包含已提交的归档。页面把本地/WebDAV 上按日期、小时保存的分钟分片组合成一条
连续时间线，并精确跳到用户选择的开始时间；用户可拖动框选、滚轮缩放、按住 Shift 拖动
平移，也可用 1 小时/6 小时/24 小时/7 天快捷范围。橙色区间表示达到阈值的声音活动，
可点击橙色片段或使用“下一段声音”快速框选，并以 0.5×–8× 同步回放。不同 FFmpeg
`stream_id` 或明显时间缺口
之间会插入 `#EXT-X-DISCONTINUITY`。

声音索引复用本来就用于 AAC 输出的音频解码，实时聚合为每个 HLS 分片一个 RMS 峰值，
不会二次读取或解码录像。完整 dB 索引跟随媒体写入本地/WebDAV JSON 侧车；归档文件名另带
最多 128 个时间桶的紧凑活动位图，因此远端时间轴只使用原有 PROPFIND 列表，不会为了画
声音标记逐个下载每分钟侧车。默认阈值可按环境噪音调整：

```toml
[recording]
audio_codec = "aac"
audio_index_enabled = true
audio_activity_threshold_db = -35 # 越接近 0，越不容易被环境底噪触发
```

`audio_codec="copy"` 保持完全直拷贝，CamVault 不会为索引强制解码，此时不生成声音索引；
也可用 `audio_index_enabled=false` 完全关闭。

在时间轴选择开始和结束时间后，可选择一路摄像头并点击“导出 MP4”。导出链路顺序读取
本地或 WebDAV 归档，通过 FFmpeg `-c copy` 重新封装为流式 MP4：不重编码、不降低画质、
不创建临时视频文件。为避免低性能路由器被多个大任务占满，同一时间只允许一个导出，
单个文件最长 24 小时；起点按视频最近关键帧对齐。

### 编码兼容性

- `video_codec="h264"`（默认）：在 CamVault 主机转码，摄像头继续输出 H.265 也能由浏览器播放；
- `video_codec="copy"`：CPU 最低、无重编码画质损失；H.265/HEVC 能否播放取决于浏览器、
  操作系统与硬件解码支持；
- `audio_codec="aac"`：把常见 G.711 等转成更兼容的 AAC；
- `audio_codec="copy"`：最低 CPU，但浏览器可能无声；
- `audio_codec="none"`：完全不录音。

画质与 CPU 的推荐设置：

```toml
[recording]
video_codec = "h264"
h264_preset = "ultrafast" # 显著降低实时 4K 软件转码 CPU
h264_crf = 20             # 数字越小画质越高、文件越大
fps_mode = "passthrough" # 保留摄像头帧率，不复制帧
ffmpeg_loglevel = "error" # 7x24 默认只记录错误，避免时间戳警告刷日志
```

CamVault 不会主动缩放视频；最终分辨率就是所选 ONVIF Profile 的分辨率。未指定 Profile 时
选择摄像头声明的最高分辨率，亦可设置 `profile_index = 0` 锁定主码流。`ultrafast` 会牺牲
压缩率来换取低 CPU，不会把 4K 降成 360p；容量应以控制台实际“每分钟写入”指标估算。

视频直拷贝只能在关键帧附近切片。摄像头 GOP 很长时，实际分片和直播延迟会大于配置值，
建议关键帧间隔 1～2 秒。

#### 低性能路由器建议

若观看端支持摄像头的 HEVC 原码，优先使用下列配置。视频不解码、不缩放、不重编码，
4K 细节原样保留；只有 G.711 等摄像头音频转为 AAC，音频转码占用很小：

```toml
[recording]
video_codec = "copy"
audio_codec = "aac"
audio_bitrate = "48k"
fps_mode = "passthrough"
ffmpeg_loglevel = "error"
```

在本项目实际部署的 4 核 Intel Celeron N5105 上，两台摄像头主码流（HEVC
3840×2160@12 fps + HEVC 1280×720@20 fps）同时录像的 45 秒采样如下。CPU 均为占整机
4 个逻辑核心的比例；写入量是当时画面的滚动 60 秒观测值，场景变化后会波动。

| 模式 | CamVault + 两路 FFmpeg CPU | CamVault + 两路 FFmpeg RSS | 整机 CPU | 最近一分钟写入 |
|---|---:|---:|---:|---:|
| H.264 软件转码（ultrafast/CRF 20） | 23.15% | 613.3 MiB | 26.32% | 30.71 MB |
| HEVC 原码直通 | 0.99% | 91.8 MiB | 5.03% | 2.44 MB |
| HEVC 原码直通 + 两个实时播放器 | 1.03% | 92.1 MiB | 4.92% | 同上 |

原码直通把录像链路自身 CPU 降低约 95.7%，RSS 降低约 85.0%；两个播放器持续拉取全部
新分片时，CamVault 仅由整机 0.12% CPU 增至 0.16%。两路 FFmpeg 全程无重启，归档经
ffprobe 确认为源分辨率和源帧率。若页面提示浏览器不能解码原码，再改用
`video_codec="h264"`；软件转码兼容面更大，但不适合这类低功耗路由器长期运行。

OpenWrt 软件源中的精简 FFmpeg 可能显式禁用 `h264`/`hevc` 解码器、解析器或 `libx264`。
这不是摄像头配置问题。把完整静态版 `ffmpeg`、`ffprobe` 放到持久化目录（例如
`/data/camvault/bin/`），配置绝对路径后运行 `camvault doctor` 和 `camera-check`：

```toml
[recording]
ffmpeg_path = "/data/camvault/bin/ffmpeg"
ffprobe_path = "/data/camvault/bin/ffprobe"
video_codec = "h264"
audio_codec = "aac"
```

`camera-check` 会报告摄像头源编码、浏览器输出编码以及是否需要改摄像头；正常结果中的
`camera_change_required` 为 `false`。

## 11. 安全

1. 摄像头密码、WebDAV 密码、网页登录密码和 API Token 使用环境变量，不提交 `.env`/`config.toml`；
2. WebDAV 采用独立 AList 最小权限用户，不使用管理员账号；
3. AList 同机时只走回环地址；跨机使用 HTTPS 且不要关闭证书验证；
4. 不把 CamVault 8088 或 AList 5244 直接暴露公网；使用 WireGuard/Tailscale 或 HTTPS 反代；
5. 默认关闭 Uvicorn access log；浏览器登录不把凭据放在查询参数中；
6. FFmpeg 通过进程参数接收 RTSP URL，本机管理员仍可能查看凭据；
7. 录像本身不加密；本地盘使用 BitLocker/FileVault/LUKS，远端依赖网盘加密模型；
8. 密码登录和 API Token 都不是 TLS，同网段明文 HTTP 仍可能被窃听。

详见 [`SECURITY.md`](SECURITY.md)。

## 12. 7×24 服务

- OpenWrt：`deploy/openwrt/`（procd，配置/虚拟环境放在持久化 `/data`）
- Linux：`deploy/systemd/camvault.service.example`
- macOS：`deploy/launchd/com.camvault.recorder.plist.example`
- Windows：`deploy/windows/install-task.ps1`

先手工执行 `uv sync --no-dev`，再用 `uv run --no-sync --no-dev` 启动。服务环境中需要同时
提供摄像头密码、网页登录密码，以及 WebDAV 模式下的账号密码；外部播放器/API 如需使用
再额外配置播放 Token。

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

- 连续录像，含轻量声音活动索引；不含画面移动侦测或 AI 事件识别；
- 不做多机高可用；
- 不是完整 NVR/VMS，不实现 PTZ、ONVIF Profile G、双向语音或厂商私有告警；
- WebDAV 大范围历史/容量清理依赖 PROPFIND，极大对象树需要进一步做远端索引；
- `atomic_upload` 是基于临时对象和 MOVE 的可见性提交，不等于所有网盘都提供数据库级事务；
- 远端模式没有磁盘队列，这是避免 SSD 写入的主动取舍；
- 核心代码跨平台；OpenWrt procd 模板已在本次 N5105 设备安装验证，其他操作系统的服务
  模板没有在本次环境逐一安装验证。

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
uv run camvault serve -c config.toml [--log-level info] [--web-password-env ENV]
```

## License

MIT
