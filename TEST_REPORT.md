# CamVault 测试报告

最近测试日期：2026-09-08

## 0.10.0（2026-09-08）

新增默认持久磁盘队列。回归 **89 项 pytest 通过**，另以部署目录的 FFmpeg/FFprobe
执行合成视频端到端自测，通过。新增覆盖：

- 旧配置未填写任何 spool 字段时，自动使用配置文件旁的持久 `spool` 目录；
- AList 离线启动、多个加密批次持久化、关闭/重新启动后按时间补传；
- 独立子进程完成 fsync 后被真实 SIGKILL，下一进程能取得锁并恢复上传；
- 视频已提交而声音索引返回 503 时，本地整对文件保留，重启重试不产生重复最终文件；
- 密文跨加密块的本地 Range 回放，补传前后密文逐字节不变；
- 容量上限、最低剩余空间、磁盘查询/写入故障，不触发云端删除；
- 正常停止时 AList 仍离线，最后一批 RAM 录像落盘后保留至下次启动；
- 并发队列互斥、网盘账户/目录/密钥绑定、封口 rename 恢复、未完成/损坏文件保留；
- 已提交但清理中断的本地目录在启动时清理；独立 storage-check 不抢正在运行的队列锁；
- 仅明确 HTTP 507 可触发配置的云端容量回收，503 正文提及 507 不会误触发。

真实 N5105 / AList 链路使用独立 UUID 测试目录与 **1 MiB 合成数据**，未停止 AList：

- 通过隔离传输注入断网，确认视频与声音索引均先保存为 AES-GCM 密文；
- 销毁旧后端，再以真实 AList 连接启动，恢复并补传耗时 **9.335 秒**；
- 从真实网盘读回的媒体密文 SHA-256 与落盘原件一致，侧车也为加密格式；
- 本地暂存和网盘回放的同一 Range 均还原为原始测试字节；两个提交成功后本地队列清空；
- 测试完成后仅删除本次独立云端测试目录及合成本地文件，未改动家庭录像。

同一持久 SSD 的落盘微基准：三个 20 MiB 批次，共 **60 MiB**，包含现有加密、哈希、
媒体/元数据/清单写入和 fsync。耗时 **0.432 秒**，进程 CPU 时间 **0.337 秒**，
约 **138.8 MiB/s**；本次峰值 RSS 未超过基准开始前的已有峰值（不代表零内存开销）。
此为短时本地写入基准，不是网盘持续吞吐保证。补传只有一个后台任务，以 128 KiB 块
读取原密文，不重新加密，不增加视频转码。

正式部署验证：

- 已安装 0.10.0，沿用原配置，默认自动使用 `/data/camvault/spool`，权限 0700/0600；
- 升级后 45 秒采样，CamVault + 两路 FFmpeg 占整机 CPU **1.072%**、合计 RSS
  **126.71 MiB**。该窗口未到动态封口时间，未覆盖媒体落盘；落盘成本见上面的微基准。
  升级前 30 秒参考值为 1.124%/274.79 MiB，但 RAM 批次阶段和进程运行时间不同，
  不能把两者差值当作本功能的 CPU/RSS 优化收益；
- 新版本正常服务重启：信号到达时 RAM 待归档 **3,821,288 字节**，两路 FFmpeg 都正常
  封口退出；日志记录两路磁盘队列依次补传，**6.66 秒**完成，待传目录清空；
- 重启后两路均为 `recording`、重启计数 0、最后错误为空；页面 v12 和队列状态正常；
- 升级前旧 0.9.1 的一次退出曾因最终 MOVE 响应超过 10 秒而报告 22,002,956 字节未提交。
  随后只读核查确认两路的最终视频及侧车实际均已提交；完整逐块解密读取分别为
  **22,002,956 / 11,029,584 字节**，SHA-256 均匹配，声音索引分别 849/130 条，
  无需修复或覆盖。新队列在这种“服务端提交成功但响应丢失”的情形会保留本地副本并幂等重试。

Ruff、格式检查、JavaScript/shell 语法、`git diff --check` 和 0.10.0 wheel/sdist 构建通过。

断电边界：保证的是已经完成 fsync/原子封口的批次恢复；尚未封口的 RAM 批次或中断
的本地写入仍可能丢失，磁盘满后也无法无限接收新录像。本轮未重启或关闭路由器。

## 0.9.1（2026-09-07 UTC / 路由器本地时间 2026-09-08）

正常停止信号触发最后一批上传，验证包含三层：

- 隔离进程分别接收真实 `SIGTERM`、`SIGINT`：信号到达前 RAM 录像尚未上传；停止摄像头
  时再通过 HTTP 补送一个分片。模拟 WebDAV 首次 PUT 返回 503，重试后视频及声音索引
  均完成加密 PUT/MOVE；解密验证前后分片字节完全一致，声音活动标记保留，无本地 spool；
- 分别注入持续报错和永久等待的上传端，验证退出时限生效、未提交字节数准确、工作任务
  和缓冲被释放，日志不会将超时误报为成功；重复停止不产生第二次上传；
- 真实 FFmpeg 测试在不完整的 HLS 分片中途退出，同时触发两条停止路径；专用 stdin `q`
  指令和互斥保护让进程正常退出（退出码 0），最后一个 HTTP 分片到达且没有
  `Failed to open file`。该测试可通过 `CAMVAULT_TEST_FFMPEG` 指定二进制路径。

正式 N5105 两摄像头服务重启实测：

- 收到 SIGTERM 时待归档媒体为 **2,804,208 字节**；两路均记录 `FFmpeg flushed and exited`；
- 两路 `.ts.enc` 和包含声音索引的 `.json.enc` 均成功 PUT/MOVE（HTTP 201），退出上传
  完成耗时 **3.04 秒**，服务 restart 命令耗时 **4.03 秒**；
- 摄像头恢复 `recording`，重启计数 0、最后错误为空；WebDAV 加密和动态批次继续启用；
- 正式配置 `server.shutdown_timeout_seconds=10`，procd `term_timeout=14`；关机链接为
  `K09camvault`，早于 `K10alist` 和 `K90network`，停止钩子等待旧进程退出后才返回；
- 未实际重启或关闭路由器。OpenWrt rcS 的关机钩子时限较短，因此只承诺在可用时间内
  尝试上传，不能保证断网、超慢网盘、突然断电或 SIGKILL 时的 RAM 数据安全。

回归：**74 项 pytest 通过（含真实 FFmpeg 退出测试）**；另以部署目录 FFmpeg/FFprobe
绝对路径执行合成媒体端到端自测，通过。Ruff、格式、shell 语法、`git diff --check`
通过，0.9.1 wheel/sdist 构建通过。

## 0.9.0（2026-09-07）

针对低码率家庭摄像头减少 WebDAV 对象和 API 请求，并在码率或可用内存变化时自动调参：

- 自适应模式用约五分钟指数平滑码率实时计算每路 `n = 目标字节 / 字节每秒`，并通过
  120～1,800 秒范围限制异常值；高码率也可先达到字节目标而更早提交；
- 目标字节实时取配置目标、每路硬上限三分之一、可用物理内存动态份额三者的最小值；
  Linux 使用 `MemAvailable`，最多每五秒采样一次，不扫描媒体或增加编解码；
- 自动化测试分别注入 1 GiB 和 512 MiB 可用内存，验证内存下降会把目标从约 12.8 MiB
  缩到 1 MiB 安全下限并立即封存；同一内存下高码率摄像头的 `n` 小于低码率摄像头；
- 正式 N5105 状态读取到 4.80 GiB `MemAvailable`；在 64 MiB 每路硬上限下，两路目标
  均自动限制为 21.3 MiB。`camera_234` 平滑码率 17,245.8 B/s、目标 1,297.1 秒，
  `camera_237` 28,787.1 B/s、目标 777.1 秒，证明不同码率得到不同 `n`；
- 实际等待首批动态归档，`camera_237` 聚合了 739.846 秒后生成 `.ts.enc`，AList/WebDAV
  的临时对象 `PUT` 和原子 `MOVE` 均返回 HTTP 201；两路保持 `recording`、无重启和错误；
- 按这次目标估算，两路媒体与侧车 PUT/MOVE 主操作从固定一分钟约 11,520 次/天下降到
  约 711 次/天，减少约 93.8%；实际值会随画面码率和系统空闲内存持续变化；
- 状态 API 暴露每路平滑码率、自动 `n`、目标字节和当前缓冲，控制台显示自动范围；
- 摄像头重连或服务正常退出仍立即封存短尾；异常断电只会影响仍在 RAM 的当前批次；
- 69 项 pytest 全部通过；第 70 项使用路由器实际 FFmpeg/FFprobe 绝对路径独立执行，端到端
  自测通过；Ruff、Node JavaScript 语法和 `git diff --check` 通过；0.9.0 wheel/sdist
  离线构建通过，正式服务升级后健康且开机自启保持启用。

发布判定：**通过**。

## 0.8.0

在 Intel Celeron N5105 路由器、两路真实主码流和正式 AList/百度网盘 WebDAV 后端上完成
上传前加密验收：

- CPU flags 实测包含 `aes`、`pclmulqdq`，采用分块 AES-256-GCM；OpenSSL 1 MiB 块吞吐
  2,582,250.8 kB/s，CamVault 使用的 Python `cryptography` 实现处理 256 MiB 时达到
  1,134.5 MiB/s；
- 新媒体与 JSON 侧车均以 `.ts.enc` / `.json.enc` 上传，原始 MPEG-TS 和 JSON 内容不在
  远端对象中明文出现；每个对象使用随机 96 位 nonce 基值，每个 1 MiB 块带独立 128 位
  GCM 认证标签，并把对象路径、块序号、块长度和文件头绑定为 AAD；
- 正式两路摄像头均连续提交多批加密归档；直接读取远端文件头确认是 CamVault 密文格式，
  透明 Range 实测 `bytes 1000-4095` 返回 206、3,096 字节和正确明文 Content-Range；
- 真实 4K 密文归档经透明解密和下载接口封装后保持 HEVC 3840×2160 + AAC；10 秒导出
  含 121 个视频包、79 个音频包，媒体 PTS 分别覆盖 0–9.999 秒和 0–9.991 秒；
- 正式密钥只存在权限 `600` 的 `/data/camvault/secrets.env`，配置只保存环境变量名；状态
  API 只显示算法、块大小和非敏感密钥来源，不返回密钥或指纹；
- 55 秒 `/proc` 采样覆盖两路 `.enc` 媒体及侧车上传：CamVault 0.316% / 70.7 MiB，
  两路 FFmpeg 1.016% / 36.2 MiB，AList 0.027% / 54.6 MiB。CamVault + FFmpeg 合计
  1.332% 整机 CPU 容量，与 0.6.0 的 1.27% 基线接近；
- `pytest -q`：67 项全部通过；Ruff、Node JavaScript 语法、格式、`git diff --check` 和
  0.8.0 wheel/sdist 离线构建通过；两路摄像头保持 `recording`、0 次重启、无归档错误。

旧 `.ts` 明文归档保留只读兼容，不自动重写；它们会按现有保留策略逐步淘汰。远端仍可见
摄像头 ID、目录时间、时长、大小和紧凑声音活动位图，加密保护的是媒体/侧车内容及完整性，
不提供文件名匿名化。

发布判定：**通过**。

## 0.7.0

在正式 AList/百度网盘 WebDAV 后端和两路真实摄像头上完成时间段下载验收：

- `pytest -q`：60 项全部通过；Ruff、Node JavaScript 语法和 `git diff --check` 通过；
- 下载沿用回放页的开始/结束时间，并可明确选择摄像头；单次最多 24 小时且同一时刻只允许
  一个导出任务，避免低性能路由器被并发任务拖垮；
- 服务端按顺序读取本地或 WebDAV 分片，以管道交给 FFmpeg 使用 `-c copy` 封装为碎片化
  MP4，不转码、不生成临时媒体文件，下载中断后会关闭远程读取并回收 FFmpeg 进程；
- WebDAV 实测摄像头 `camera_237` 的 30 秒区间下载为 824,010 字节，ffprobe 时长
  29.998 秒，保留 HEVC 1280×720 视频和 AAC 声音；
- 4K 摄像头 `camera_234` 的 10 秒区间下载为 110,149 字节，ffprobe 时长
  9.987 秒，保留 HEVC 3840×2160 视频和 AAC 声音；
- 两次导出后均无遗留 FFmpeg 进程，两路录像仍为 `recording`、重启次数为 0，未记录新错误。

发布判定：**通过**。

## 0.6.0

在 0.5.0 的同一台 Intel Celeron N5105 路由器、两路真实主码流和正式
AList/百度网盘 WebDAV 后端上完成声音索引与倍速回放验收：

- `pytest -q`：59 项全部通过；Ruff、Node JavaScript 语法、`git diff --check` 通过；
- 离线构建成功生成 0.6.0 wheel 与 sdist，并安装到 `/data/camvault` 持久虚拟环境；
- FFmpeg 音量分析复用现有 AAC 解码链路，只启用 `Overall.RMS_level`，关闭默认的全部
  逐声道及无关统计，不进行录像二次读取或二次解码；
- WebDAV 归档 JSON v3 实测 `segment_count=30`、`audio_points=30`，一分钟内每个视频
  分片均有 dB 索引；完整索引与视频一起提交，紧凑活动位图包含在远端文件名中；
- 真实时间线 API 从 WebDAV 目录索引读出声音活动区间，不需要逐个 GET JSON 侧车；
- 共享 Chromium 实测 0.5×–8× 选择器、下一段声音、点击框选、Canvas 时间轴及移除
  播放器码流徽标均正常；CSS/JS 本地载入约 19 ms / 26 ms。

最终优化版 45 秒 `/proc` 差分采样：整机 CPU 5.74%，CamVault + 两路 FFmpeg
1.27% / 95.1 MiB RSS，AList 0.01% / 84.3 MiB RSS。对比 0.5.0 同一原码直通 + WebDAV
基线约 1.01%，声音索引增加约 0.26 个整机 CPU 百分点，RSS 未出现可测的持续增长。
采样期间两路摄像头均无重启。

发布判定：**通过**。

## 0.5.0

当前发布候选在 Intel Celeron N5105（4 个逻辑核心、约 8 GiB RAM、无 swap）的实际路由器
上完成两台局域网摄像头主码流验证：

- 摄像头 A：HEVC 3840×2160，12 fps，G.711 A-law；
- 摄像头 B：HEVC 1280×720，20 fps，G.711 A-law；
- H.264 兼容模式和 HEVC 原码直通模式均连续产生直播与归档，两路均为 0 次重启；
- HEVC 原码直通持续约 17 分钟，每路接收约 505 个分片，最终媒体缓冲约 1 MiB；
- ffprobe 确认两种模式的输出分辨率/帧率未降级，4K 路保持 3840×2160@12 fps；
- 两个模拟 HLS 播放器共完整读取 70 个新直播分片、2,960,206 字节，无 HTTP 错误；
- 页面状态、登录 Cookie、实时清单、归档、时间线和滚动写入指标均可用。
- `pytest -q`：58 项全部通过；Ruff、Node JavaScript 语法检查均通过。
- `camvault self-test`：`PASS`；离线构建成功生成 0.5.0 wheel 与 sdist，wheel 包含全部
  控制台静态资源。

45 秒 `/proc` 差分采样结果如下。进程 CPU 和整机 CPU 都按 4 个逻辑核心的总容量计算；
RSS 为采样期间均值。AList 当时没有真实 WebDAV 上传，保持空闲约 75.6 MiB RSS。

| 场景 | 整机 CPU | CamVault CPU / RSS | 两路 FFmpeg CPU / RSS | AList CPU / RSS |
|---|---:|---:|---:|---:|
| H.264 软件转码 | 26.32% | 0.24% / 67.3 MiB | 22.91% / 546.0 MiB | 0% / 75.6 MiB |
| HEVC 原码直通 | 5.03% | 0.12% / 53.9 MiB | 0.87% / 37.9 MiB | 0.01% / 75.6 MiB |
| 原码直通 + 两个实时播放器 | 4.92% | 0.16% / 54.1 MiB | 0.87% / 38.0 MiB | 0% / 75.6 MiB |

同一时段的滚动写入量从 H.264 `ultrafast` 的 30,711,304 B/min 降至原码直通的
2,439,676 B/min。该值取决于摄像头码率和画面内容，不是固定容量承诺。原码直通是本机的
低占用推荐配置；播放端必须具备相应 HEVC 支持。

实际 AList（本机 5244 端口）与百度网盘挂载随后完成正式链路验收：

- `storage-check` 的 `OPTIONS/MKCOL/PUT/MOVE/GET/DELETE` 全部成功，测试对象已删除；
- 两路摄像头分别向 WebDAV 提交首个约 60 秒归档，合计 2,470,508 字节，无本地媒体 spool；
- 时间线从远端索引发现两路录像，分别返回一个连续范围；
- 历史播放清单指向远端归档，代理 Range 请求返回 `206`、准确的 `Content-Range` 和
  1024 字节响应体；
- 完整取回的 4K 远端归档经 ffprobe 确认为 HEVC 3840×2160@12 fps、61.716 秒；
- 正式服务使用 `/data/camvault` 持久虚拟环境、root-only secrets 文件和 OpenWrt procd，
  默认 `storage.backend="webdav"`。

因此 0.5.0 的实际验收覆盖摄像头、CamVault、AList、百度网盘上传、远端索引和 Range 回放
完整路径。切换为正式 WebDAV 服务并由 procd 重启恢复后，又完成一次 30 秒 `/proc` 差分
采样：整机 CPU 6.97%，CamVault 0.11% / 57.9 MiB，两路 FFmpeg 0.90% / 37.9 MiB，
AList 低于 0.01% / 81.8 MiB。CamVault + FFmpeg 仍约占整机 1.01% CPU；本次约
0.33 Mbit/s 的真实上传未给 AList 造成可测的持续 CPU 压力。瞬时峰值与长期网盘行为仍应
由控制台/系统监控持续观察。

## 0.4.0

当前工作区使用 Python 3.13.11 和持久目录中的完整静态 FFmpeg/ffprobe 7.0.2 实测：

- `pytest -q`：55 项全部通过；
- `camvault self-test`：`PASS`，覆盖 FFmpeg → HTTP PUT → 有界 RAM → 原子归档 →
  HLS 直播/回放 → ffprobe 可读性；
- `ruff check src tests`、`ruff format src tests`、`git diff --check`：通过；
- 生成后的多摄像头控制台 JavaScript 通过 Node `--check` 语法验证；
- `uv build`：生成 `camvault-0.4.0` wheel 与 sdist。

0.4.0 新增测试覆盖本地/WebDAV 写失败紧急回收、最旧优先及文件数上限、同一事务立即重试、
多摄像头页面入口、分钟分片中间的精确历史起播，以及 FFmpeg 软件解码器识别。

设备级验证结果：

- 两台只读 ONVIF/RTSP 探测均通过，源视频为 HEVC 640×360，音频为 G.711 A-law；
- 两路同时试录无重启，CamVault 实时和历史输出均由 ffprobe 确认为 H.264 640×360 + AAC；
- 真机媒体只写临时本地目录，验证后已删除，未上传到 AList/远程网盘；
- AList/WebDAV 使用合成字节完成 OPTIONS/MKCOL/PUT/MOVE/GET Range/DELETE 与清理检查，
  未使用摄像头画面。

发布判定：**通过**。真实家庭画面写入远程网盘仍需用户另行明确授权后再做试录。

## 0.2.0 历史基线

### 1. 结论

CamVault 0.2.0 在原有本地归档后端上新增了 WebDAV/AList 后端。当前发布候选版本在本环境完成：

- Python 源码编译检查；
- 43 项自动化测试；
- 本地 FFmpeg 合成视频端到端自测；
- 真实 TCP WebDAV 协议链路测试；
- WebDAV 故障、幂等重试、Range 回放和保留策略测试；
- `uv build --offline` 构建；
- wheel 独立安装与自测；
- 最终源码 ZIP 解压后的复测。

发布判定：**通过**。尚未在用户实际 AList、网盘和摄像头上做设备级验收，部署前必须执行
`camvault storage-check` 和逐台 `camvault camera-check`。

### 2. 测试环境

| 项目 | 版本 |
|---|---|
| 操作系统 | Linux 6.18.35 x86_64, glibc 2.41 |
| Python | 3.13.5 |
| uv | 0.10.0 |
| FFmpeg / ffprobe | 7.1.5-0+deb13u1 |
| FastAPI | 0.128.2 |
| Uvicorn | 0.48.0 |
| HTTPX | 0.28.1 |
| Pydantic | 2.13.4 |
| defusedxml | 0.7.1 |
| pytest / pytest-asyncio | 9.0.2 / 1.3.0 |

项目声明支持 Python 3.11+；本报告只代表上述 Python 3.13.5 环境的实际执行结果。

### 3. AList 源码研究基线

检查对象：

- AList 仓库：`AlistGo/alist`；
- `main` 提交：`e1c022a9d920559078e5a906d7e1499901857006`；
- 检查当日最新发布版：v3.64.0（2026-09-03 发布）。

源码检查得到的实现依据：

1. AList WebDAV `PUT` 处理器把请求 `Body` 与 `ContentLength` 包装为上传流，再交给
   `PutDirectly`，WebDAV 接入层没有无条件先把完整文件写到临时盘；
2. 底层网盘驱动仍可能因为哈希、分片、Range/Seek 等要求触发完整缓存；
3. AList 的临时文件路径最终由 `temp_dir` 控制，默认位于 `data/temp`；默认环境变量名是 `ALIST_TEMP_DIR`（仅 `--no-prefix` 模式才是 `TEMP_DIR`）；环境覆盖在配置文件加载/改写之后应用，不会自动回写 `config.json`；
4. 因此 CamVault 只能保证自己的媒体路径不落盘。要约束 AList 下游临时媒体，还必须把
   AList `temp_dir` 放到 tmpfs/RAM disk；
5. AList 的 SQLite、配置、日志、容器日志和系统 swap 是独立写入路径，不能把“媒体零
   spool”错误表述为整机绝对零 SSD I/O。

详细源码位置和部署方案见 `docs/ALIST_WEBDAV.md`。

### 4. 自动化测试

执行：

```bash
PYTHONPATH=src python -m compileall -q src tests
PYTHONPATH=src python -m pytest -q
```

结果：

```text
43 passed, 2 warnings
```

两条 warning 来自 Uvicorn 当前 WebSocket 适配层对 `websockets.legacy` API 的弃用提示；
CamVault 不使用 WebSocket，不影响本次 HTTP/HLS 测试结果。

### 4.1 原有能力回归

覆盖：

- 配置校验、局域网绑定安全约束、IPv4/IPv6 回环上传地址；
- RTSP 凭据编码和输出脱敏；
- ONVIF WS-Security PasswordDigest、Media1/Media2、Profile 选择和 `GetStreamUri`；
- WS-Discovery 报文构造与解析；
- FFmpeg HLS HTTP PUT 命令和敏感参数脱敏；
- 直播内存窗口的数量/字节硬上限、去重和淘汰；
- FFmpeg 重连后的 HLS discontinuity；
- 归档内存预算、慢存储反压和短尾部强制封存；
- 本地 `.partial` 写入、原子重命名、目录分区、SHA-256 与元数据；
- 本地保留策略只清理受管录像；
- Token 鉴权、直播/VOD/下载、路径穿越防护和安全响应头；
- CamVault 0.1 旧录像文件名兼容。

### 4.2 WebDAV/AList 后端专项覆盖

覆盖：

- `OPTIONS`、`MKCOL`、`PUT`、`MOVE`、`PROPFIND`、`GET`、`DELETE`；
- Basic Auth，不把 WebDAV 凭据暴露给浏览器或状态接口；
- 媒体以原 RAM 分片迭代器上传，显式保留 `Content-Length`；
- `*.camvault-partial` 事务对象和 `MOVE` 提交；
- 上传成功后远端无事务残留；
- 网络/网盘失败时不创建本地 `storage.root`，不回退到 SSD；
- `MOVE` 已提交但响应丢失后的同一批次幂等重试，不产生第二份最终录像；
- 远端分区扫描、短时间范围按小时定向扫描和索引缓存；
- 远端 VOD 代理、`Range`、`206 Partial Content`、`Content-Range` 与 `416`；
- `retention_days`、`max_storage_gb`、WebDAV quota 与事务/孤儿清理；
- 远程状态中 `diskless_media_path=true`、`local_media_spool=false`。

### 5. 真实 TCP WebDAV 测试

除 HTTPX MockTransport 外，测试还启动了监听 `127.0.0.1` 随机端口的 WebDAV 测试服务，
通过真实 TCP/HTTP 完成：

```text
Basic Auth
  -> OPTIONS
  -> MKCOL 多级目录
  -> PUT（必须有 Content-Length）
  -> MOVE
  -> GET 校验
  -> DELETE 清理
```

结果：通过。测试前后指定的本地录像目录均不存在，远端测试对象全部清理。

该测试验证 HTTP 客户端、认证、请求头和方法链路，但它不是 AList 二进制或某个真实网盘
驱动的替代品。

### 6. 本地 FFmpeg 合成视频端到端自测

执行：

```bash
PYTHONPATH=src python -m camvault self-test
```

实际链路：

```text
两次独立 FFmpeg 合成流
  -> HTTP PUT 到本机 FastAPI
  -> 有界 RAM 直播窗口
  -> 按 stream_id 切断重连边界
  -> RAM 聚合并原子写入本地 MPEG-TS
  -> HLS 直播/VOD 下载
  -> ffprobe 解析最终归档
```

结果：`PASS`，标准错误输出为 0 字节。

| 指标 | 结果 |
|---|---:|
| 接收直播分片 | 7 |
| 生成归档文件 | 2 |
| 归档总字节 | 265,080 |
| 首个归档编码 | H.264 |
| 首个归档分辨率 | 320×180 |
| 首个归档容器 | MPEG-TS |
| 首个归档可解析时长 | 3.1 秒 |
| 重连边界 | 直播与 VOD 均检测到 discontinuity |

### 7. uv 构建与 wheel 独立复测

执行：

```bash
uv build --offline
```

成功生成：

- `camvault-0.2.0-py3-none-any.whl`；
- `camvault-0.2.0.tar.gz`。

随后将 wheel 以 `--no-deps` 安装到独立目录，并显式设置 `PYTHONPATH` 指向该目录，确认：

1. 导入路径来自独立安装目录，不是工作区 `src/`；
2. `camvault.__version__ == "0.2.0"`；
3. `python -m camvault --help` 可执行；
4. `python -m camvault init` 能读取 wheel 内配置模板；
5. 生成的配置与仓库模板字节一致；
6. 从 wheel 运行 `python -m camvault self-test` 得到 `PASS`；
7. wheel 自测标准错误输出为 0 字节。

### 8. 配置与发布文件检查

完成：

- 所有源码和测试文件 AST/compileall 解析；
- `pyproject.toml`、两个配置模板的 TOML 解析；
- 仓库配置模板与 wheel 内模板一致性比较；
- launchd plist XML 解析；
- launchd shell 脚本 `sh -n`；
- AList Compose override YAML 解析；
- wheel 内容检查，确认包含全部运行模块和内置配置模板；sdist/源码 ZIP 内容检查，确认另含 AList 文档、部署模板和测试；
- 最终 ZIP 白名单式打包，不含 `.venv`、缓存、字节码、`.env`、运行配置和录像。

`ruff` 没有在本环境执行：离线依赖缓存中缺少 `defusedxml`，`uv run --offline ruff` 无法
完成依赖解析，系统环境也未预装 Ruff。该限制没有被包装成“静态检查通过”；源码编译、
43 项测试和发布包复测均已实际执行。

### 9. 最终源码 ZIP 解压复测

最终 ZIP 解压到全新目录后执行：

```bash
PYTHONPATH=src python -m compileall -q src tests
PYTHONPATH=src python -m pytest -q
PYTHONPATH=src python -m camvault self-test
uv build --offline
```

结果：

- 自动化测试：`43 passed, 2 warnings`；
- 合成视频自测：`PASS`，标准错误输出 0 字节；
- 离线构建：成功；
- 解压目录中不存在预带的缓存、虚拟环境或用户密钥。

### 10. 尚未在本环境验证的部分

- **未运行真实 AList 二进制。** 沙箱不能从 GitHub 下载 release asset，且没有预装 AList；
  因此采用官方源码审计、内存 WebDAV 语义测试和真实 TCP WebDAV 方法链路测试。不能把这些
  结果表述为“已经验证你家的 AList + 目标网盘”。
- 未连接用户家中的真实 ONVIF 摄像头；ONVIF 协议流程使用 Mock 响应验证。部署后必须逐台
  执行 `camvault camera-check`。
- 未验证具体网盘驱动是否会使用 AList `temp_dir`、是否真正支持原子 MOVE、单文件限制、
  限速和风控；必须针对实际挂载运行 `camvault storage-check`，并做至少数小时试录。
- 未在 Windows 和 macOS 主机上实际运行服务模板；核心代码不依赖平台专属 API，但任务
  计划、launchd、RAM disk、权限和 FFmpeg 安装仍需在目标机确认。
- 执行环境禁止访问 PyPI，因此没有运行联网的 `uv sync`，也没有伪造 `uv.lock`。首次联网
  同步后应保留生成的锁文件；7×24 服务使用 `uv run --no-sync --no-dev`。
- 未做数周级 soak test、主机掉电、真实 WAN 抖动、多路 4K、低上行带宽或网盘限流压力测试。
- 网页播放器的 hls.js CDN 加载未做 GUI 自动化；HLS/API、M3U8、Range 和归档下载路径已做
  HTTP 集成测试。

### 11. 部署验收门槛

在实际环境中同时满足以下条件后，才能把 WebDAV 后端视为上线：

```text
camvault doctor             PASS
camvault storage-check      PASS
每台 camera-check           PASS
AList temp_dir 位于 RAM      已确认
CamVault storage.root        未创建（WebDAV 模式）
连续试录与回放               无缺口/无 .camvault-partial 残留
AList/网盘限速和配额          符合预期
```
