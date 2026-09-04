# CamVault 测试报告

测试日期：2026-09-04

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

真实 AList/WebDAV 写入与回放尚待使用部署账号完成，因此以上性能结论只覆盖摄像头、
CamVault、本地归档和浏览器拉流路径，不把空闲 AList 当作远端上传性能结论。

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
