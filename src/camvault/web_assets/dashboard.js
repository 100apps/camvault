(() => {
  "use strict";

  const boot = window.CAMVAULT_BOOTSTRAP;
  const cameras = boot.cameras;
  const players = new Map();
  const $ = (id) => document.getElementById(id);
  let mode = "live";
  let revision = null;
  let timelineData = null;
  let timelineTimer = null;
  let toastTimer = null;
  let statusTimer = null;
  let logTimer = null;
  let currentView = "monitor";
  let playbackRate = 1;

  const timeline = {
    canvas: $("timeline"),
    viewStart: new Date(Date.now() - 6 * 3600_000),
    viewEnd: new Date(),
    selectionStart: new Date(Date.now() - 15 * 60_000),
    selectionEnd: new Date(),
    drag: null,
    layout: null,
    frame: 0,
  };

  function authHeaders() {
    const headers = {};
    if (boot.token) headers["X-CamVault-Token"] = boot.token;
    if (boot.csrf) headers["X-CamVault-CSRF"] = boot.csrf;
    return headers;
  }

  async function api(path, options = {}) {
    options.headers = Object.assign({}, authHeaders(), options.headers || {});
    const response = await fetch(path, options);
    let payload;
    try {
      payload = await response.json();
    } catch (_error) {
      payload = { detail: await response.text() };
    }
    if (response.status === 401) {
      window.location.assign("/login");
      throw new Error("登录已失效");
    }
    if (!response.ok) throw new Error(payload.detail || `HTTP ${response.status}`);
    return payload;
  }

  function humanBytes(value) {
    if (value === null || value === undefined) return "—";
    const units = ["B", "KiB", "MiB", "GiB", "TiB", "PiB"];
    let index = 0;
    let number = Number(value);
    while (Math.abs(number) >= 1024 && index < units.length - 1) {
      number /= 1024;
      index += 1;
    }
    const digits = index === 0 ? 0 : number >= 100 ? 0 : number >= 10 ? 1 : 2;
    return `${number.toFixed(digits)} ${units[index]}`;
  }

  function showToast(message, error = false) {
    const box = $("toast");
    box.textContent = message;
    box.classList.toggle("error", error);
    box.classList.add("show");
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => box.classList.remove("show"), 3200);
  }

  function localInputValue(date) {
    const shifted = new Date(date.getTime() - date.getTimezoneOffset() * 60_000);
    return shifted.toISOString().slice(0, 19);
  }

  function syncInputs() {
    $("historyStart").value = localInputValue(timeline.selectionStart);
    $("historyEnd").value = localInputValue(timeline.selectionEnd);
    timeline.canvas.setAttribute(
      "aria-valuetext",
      `${timeline.selectionStart.toLocaleString()} 至 ${timeline.selectionEnd.toLocaleString()}`,
    );
  }

  function streamUrl(camera) {
    const base = mode === "live"
      ? `/live/${camera.id}/index.m3u8`
      : `/vod/${camera.id}/index.m3u8`;
    const params = new URLSearchParams();
    if (mode === "history") {
      params.set("start", timeline.selectionStart.toISOString());
      params.set("end", timeline.selectionEnd.toISOString());
    }
    if (boot.token) params.set("token", boot.token);
    const query = params.toString();
    return query ? `${base}?${query}` : base;
  }

  function setMessage(cameraId, value) {
    const element = $(`message-${cameraId}`);
    if (element) element.textContent = value;
  }

  function applyPlaybackRate(video) {
    const rate = mode === "history" ? playbackRate : 1;
    video.defaultPlaybackRate = rate;
    video.playbackRate = rate;
  }

  function destroyPlayer(cameraId) {
    const current = players.get(cameraId);
    if (current && current.hls) current.hls.destroy();
    players.delete(cameraId);
    const video = $(`video-${cameraId}`);
    if (video) {
      video.pause();
      video.removeAttribute("src");
      video.load();
    }
  }

  function attachCamera(camera) {
    if (!camera.enabled) return;
    destroyPlayer(camera.id);
    const video = $(`video-${camera.id}`);
    const source = streamUrl(camera);
    const label = mode === "live" ? "实时画面" : "历史录像";
    applyPlaybackRate(video);
    setMessage(camera.id, `正在连接${label}`);

    if (window.Hls && window.Hls.isSupported()) {
      const hls = new window.Hls({
        enableWorker: true,
        lowLatencyMode: mode === "live",
        liveSyncDurationCount: 2,
        liveMaxLatencyDurationCount: 5,
        maxLiveSyncPlaybackRate: 1.5,
        maxBufferLength: mode === "live" ? 10 : 90,
        backBufferLength: mode === "live" ? 0 : 45,
      });
      players.set(camera.id, { hls, source, mediaRecoveries: 0 });
      hls.loadSource(source);
      hls.attachMedia(video);
      hls.on(window.Hls.Events.MANIFEST_PARSED, () => {
        if (players.get(camera.id)?.hls !== hls) return;
        applyPlaybackRate(video);
        setMessage(camera.id, "");
        video.play().catch(() => setMessage(camera.id, `${label}已就绪，点击画面播放`));
      });
      hls.on(window.Hls.Events.ERROR, (_event, data) => {
        if (players.get(camera.id)?.hls !== hls || !data.fatal) return;
        if (data.type === window.Hls.ErrorTypes.NETWORK_ERROR) {
          if (mode === "history" && data.response?.code === 404) {
            setMessage(camera.id, "所选时段没有录像");
            return;
          }
          setMessage(camera.id, "连接中断，正在恢复");
          setTimeout(() => {
            if (players.get(camera.id)?.hls === hls) hls.startLoad();
          }, 1200);
        } else if (data.type === window.Hls.ErrorTypes.MEDIA_ERROR) {
          const current = players.get(camera.id);
          if (current) current.mediaRecoveries += 1;
          if (camera.codecMode === "copy" && current?.mediaRecoveries > 2) {
            setMessage(camera.id, "浏览器无法解码摄像头原码；可换支持 HEVC 的浏览器，或在配置中启用 H.264 兼容模式");
            hls.destroy();
            players.delete(camera.id);
            return;
          }
          setMessage(camera.id, "正在恢复媒体解码");
          hls.recoverMediaError();
        } else {
          setMessage(camera.id, `无法播放：${data.details}`);
          hls.destroy();
          players.delete(camera.id);
        }
      });
      return;
    }

    if (video.canPlayType("application/vnd.apple.mpegurl")) {
      video.src = source;
      players.set(camera.id, { hls: null, source });
      video.addEventListener("loadedmetadata", () => {
        applyPlaybackRate(video);
        setMessage(camera.id, "");
        video.play().catch(() => setMessage(camera.id, `${label}已就绪，点击画面播放`));
      }, { once: true });
      video.addEventListener("error", () => setMessage(camera.id, `无法加载${label}`), { once: true });
      return;
    }
    setMessage(camera.id, "当前浏览器不支持 HLS");
  }

  function attachAll() {
    cameras.forEach(attachCamera);
  }

  function stopAll(message) {
    cameras.forEach((camera) => {
      destroyPlayer(camera.id);
      if (camera.enabled) setMessage(camera.id, message);
    });
  }

  async function refreshStatus() {
    try {
      const data = await api("/api/status");
      $("overall").className = "health-chip ok";
      $("overall").lastChild.textContent = "服务正常";
      $("memory").textContent = humanBytes(data.bounded_media_memory_bytes);
      $("writeRate").textContent = `${humanBytes(data.write_bytes_last_minute)} / 分钟`;
      $("writeBitrate").textContent = `平均 ${Number(data.write_mbps_last_minute || 0).toFixed(2)} Mbit/s`;

      const capacity = data.storage.capacity || {};
      const managed = capacity.managed_archive_bytes;
      if (capacity.total_bytes !== null && capacity.total_bytes !== undefined) {
        const percent = capacity.total_bytes > 0
          ? Math.min(100, capacity.used_bytes / capacity.total_bytes * 100)
          : 0;
        $("storageUsage").textContent = `${percent.toFixed(1)}%`;
        $("storageDetail").textContent = `${humanBytes(capacity.used_bytes)} 已用 · ${humanBytes(capacity.free_bytes)} 可用 · ${data.storage.backend.toUpperCase()}`;
        $("storageMeter").style.width = `${percent}%`;
      } else {
        $("storageUsage").textContent = humanBytes(managed);
        $("storageDetail").textContent = `CamVault 归档 · ${data.storage.backend.toUpperCase()} 未提供总配额`;
        $("storageMeter").style.width = "0";
      }

      const policy = data.retention.policy;
      $("retention").textContent = policy.retention_days
        ? `保留 ${policy.retention_days} 天`
        : policy.max_storage_gb
          ? `上限 ${policy.max_storage_gb} GiB`
          : "按可用空间";
      $("retentionDetail").textContent = data.retention.last_error
        ? `上次清理失败：${data.retention.last_error}`
        : `写入失败时删除最老录像 · 已运行 ${data.retention.runs} 次`;

      data.cameras.forEach((camera) => {
        const dot = $(`dot-${camera.id}`);
        const state = $(`state-${camera.id}`);
        const profile = $(`profile-${camera.id}`);
        const rate = $(`rate-${camera.id}`);
        if (dot) dot.className = `status-dot ${camera.state === "recording" ? "ok" : camera.state === "disabled" ? "" : "bad"}`;
        if (state) state.textContent = camera.state === "recording" ? "录像中" : camera.detail || camera.state;
        if (profile) profile.textContent = camera.resolved_profile || "正在解析主码流";
        if (rate) rate.textContent = `${humanBytes(camera.write_bytes_last_minute)} / 分钟`;
      });
    } catch (_error) {
      $("overall").className = "health-chip bad";
      $("overall").lastChild.textContent = "连接异常";
    }
  }

  function setView(view) {
    currentView = view;
    $("monitorView").hidden = view !== "monitor";
    $("systemView").hidden = view !== "system";
    document.querySelectorAll(".nav-item").forEach((button) => {
      button.classList.toggle("active", button.dataset.view === view);
    });
    if (view === "system") {
      stopAll("切回监控中心后恢复画面");
      loadConfig();
      loadLogs();
      clearInterval(logTimer);
      logTimer = setInterval(loadLogs, 10_000);
    } else {
      clearInterval(logTimer);
      if (mode === "live") attachAll();
      requestTimelineDraw();
    }
  }

  function setMode(nextMode) {
    mode = nextMode;
    const history = mode === "history";
    $("modeLive").classList.toggle("active", !history);
    $("modeHistory").classList.toggle("active", history);
    $("timelinePanel").hidden = !history;
    $("playbackNotice").textContent = history
      ? "拖动时间轴框选回放范围，所有摄像头保持同一时间基准"
      : "低延迟转发，页面隐藏时自动暂停拉流";
    if (history) {
      stopAll("请在时间轴选择时段并开始回放");
      resizeTimeline();
      loadTimeline();
    } else {
      attachAll();
    }
  }

  function timeAtX(clientX) {
    const rect = timeline.canvas.getBoundingClientRect();
    const layout = timeline.layout;
    const x = Math.max(layout.left, Math.min(rect.width, clientX - rect.left));
    const ratio = (x - layout.left) / Math.max(1, layout.width);
    return new Date(timeline.viewStart.getTime() + ratio * (timeline.viewEnd - timeline.viewStart));
  }

  function formatTick(date, span) {
    const options = span > 2 * 86400_000
      ? { month: "2-digit", day: "2-digit", hour: "2-digit" }
      : { hour: "2-digit", minute: "2-digit" };
    return new Intl.DateTimeFormat("zh-CN", options).format(date);
  }

  function requestTimelineDraw() {
    if (timeline.frame) return;
    timeline.frame = requestAnimationFrame(() => {
      timeline.frame = 0;
      drawTimeline();
    });
  }

  function resizeTimeline() {
    const canvas = timeline.canvas;
    const rect = canvas.getBoundingClientRect();
    if (!rect.width || !rect.height) return;
    const dpr = Math.min(window.devicePixelRatio || 1, 2);
    const width = Math.round(rect.width * dpr);
    const height = Math.round(rect.height * dpr);
    if (canvas.width !== width || canvas.height !== height) {
      canvas.width = width;
      canvas.height = height;
    }
    requestTimelineDraw();
  }

  function drawTimeline() {
    const canvas = timeline.canvas;
    const rect = canvas.getBoundingClientRect();
    if (!rect.width || !rect.height) return;
    const dpr = canvas.width / rect.width;
    const ctx = canvas.getContext("2d");
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, rect.width, rect.height);
    const left = rect.width < 520 ? 62 : 92;
    const top = 29;
    const rows = Math.max(1, cameras.filter((camera) => camera.enabled).length);
    const rowHeight = (rect.height - top - 8) / rows;
    const plotWidth = Math.max(1, rect.width - left - 8);
    timeline.layout = { left, width: plotWidth, top, rowHeight };
    const span = timeline.viewEnd - timeline.viewStart;
    const xFor = (value) => left + (new Date(value) - timeline.viewStart) / span * plotWidth;

    ctx.font = "10px Inter, system-ui, sans-serif";
    ctx.textBaseline = "middle";
    const tickCount = Math.max(3, Math.floor(plotWidth / 150));
    for (let index = 0; index <= tickCount; index += 1) {
      const ratio = index / tickCount;
      const x = left + ratio * plotWidth;
      ctx.strokeStyle = "#1c232c";
      ctx.beginPath(); ctx.moveTo(x, top - 4); ctx.lineTo(x, rect.height); ctx.stroke();
      ctx.fillStyle = "#687484";
      ctx.textAlign = index === 0 ? "left" : index === tickCount ? "right" : "center";
      const date = new Date(timeline.viewStart.getTime() + ratio * span);
      ctx.fillText(formatTick(date, span), x, 13);
    }

    const enabled = cameras.filter((camera) => camera.enabled);
    enabled.forEach((camera, row) => {
      const y = top + row * rowHeight;
      ctx.fillStyle = "#697585";
      ctx.textAlign = "left";
      const name = camera.name.length > 10 ? `${camera.name.slice(0, 9)}…` : camera.name;
      ctx.fillText(name, 9, y + rowHeight / 2);
      ctx.fillStyle = "#121820";
      ctx.fillRect(left, y + 6, plotWidth, Math.max(7, rowHeight - 12));
      const info = timelineData?.cameras?.find((entry) => entry.id === camera.id);
      (info?.ranges || []).forEach((range) => {
        const x1 = Math.max(left, xFor(range.start));
        const x2 = Math.min(left + plotWidth, xFor(range.end));
        if (x2 > x1) {
          ctx.fillStyle = "#356fca";
          ctx.fillRect(x1, y + 6, Math.max(2, x2 - x1), Math.max(7, rowHeight - 12));
        }
      });
      (info?.sound_ranges || []).forEach((range) => {
        const x1 = Math.max(left, xFor(range.start));
        const x2 = Math.min(left + plotWidth, xFor(range.end));
        if (x2 <= x1) return;
        const level = range.level_db === null || range.level_db === undefined
          ? .72
          : Math.max(.28, Math.min(1, (Number(range.level_db) + 60) / 40));
        const height = Math.max(5, Math.min(11, (rowHeight - 12) * (.35 + level * .45)));
        ctx.fillStyle = `rgba(233, 174, 79, ${(.58 + level * .35).toFixed(2)})`;
        ctx.fillRect(x1, y + rowHeight - 6 - height, Math.max(2, x2 - x1), height);
      });
    });

    const selectionX1 = Math.max(left, xFor(timeline.selectionStart));
    const selectionX2 = Math.min(left + plotWidth, xFor(timeline.selectionEnd));
    if (selectionX2 > selectionX1) {
      ctx.fillStyle = "rgba(93, 157, 255, .15)";
      ctx.fillRect(selectionX1, top - 4, selectionX2 - selectionX1, rect.height - top + 4);
      ctx.strokeStyle = "#77aaff";
      ctx.lineWidth = 1;
      ctx.strokeRect(selectionX1 + .5, top - 3.5, Math.max(1, selectionX2 - selectionX1 - 1), rect.height - top + 2);
    }
    const nowX = xFor(Date.now());
    if (nowX >= left && nowX <= left + plotWidth) {
      ctx.strokeStyle = "#e96565";
      ctx.beginPath(); ctx.moveTo(nowX, top - 5); ctx.lineTo(nowX, rect.height); ctx.stroke();
    }
  }

  function scheduleTimelineLoad() {
    clearTimeout(timelineTimer);
    timelineTimer = setTimeout(loadTimeline, 240);
    requestTimelineDraw();
  }

  async function loadTimeline() {
    if (mode !== "history") return;
    $("timelineLoading").hidden = false;
    try {
      const params = new URLSearchParams({
        start: timeline.viewStart.toISOString(),
        end: timeline.viewEnd.toISOString(),
      });
      timelineData = await api(`/api/timeline?${params}`);
      requestTimelineDraw();
    } catch (error) {
      showToast(`录像索引读取失败：${error.message}`, true);
    } finally {
      $("timelineLoading").hidden = true;
    }
  }

  function clampView(start, end) {
    const minSpan = 5 * 60_000;
    const maxSpan = 31 * 86400_000;
    let span = end - start;
    if (span < minSpan) {
      const middle = (start.getTime() + end.getTime()) / 2;
      span = minSpan;
      start = new Date(middle - span / 2);
      end = new Date(middle + span / 2);
    } else if (span > maxSpan) {
      const middle = (start.getTime() + end.getTime()) / 2;
      span = maxSpan;
      start = new Date(middle - span / 2);
      end = new Date(middle + span / 2);
    }
    timeline.viewStart = start;
    timeline.viewEnd = end;
  }

  function zoomTimeline(factor, center = new Date((timeline.viewStart.getTime() + timeline.viewEnd.getTime()) / 2)) {
    const oldSpan = timeline.viewEnd - timeline.viewStart;
    const newSpan = Math.max(5 * 60_000, Math.min(31 * 86400_000, oldSpan * factor));
    const ratio = (center - timeline.viewStart) / oldSpan;
    clampView(
      new Date(center.getTime() - newSpan * ratio),
      new Date(center.getTime() + newSpan * (1 - ratio)),
    );
    scheduleTimelineLoad();
  }

  function allSoundRanges() {
    return (timelineData?.cameras || [])
      .flatMap((camera) => camera.sound_ranges || [])
      .sort((left, right) => new Date(left.start) - new Date(right.start));
  }

  function selectSoundRange(range) {
    const start = new Date(range.start);
    const end = new Date(range.end);
    timeline.selectionStart = new Date(start.getTime() - 5_000);
    timeline.selectionEnd = new Date(Math.max(end.getTime() + 15_000, start.getTime() + 30_000));
    syncInputs();
    requestTimelineDraw();
  }

  function soundRangeNear(point) {
    const tolerance = (timeline.viewEnd - timeline.viewStart) / Math.max(1, timeline.layout?.width || 1) * 6;
    const instant = point.getTime();
    return allSoundRanges().find((range) => (
      instant >= new Date(range.start).getTime() - tolerance
      && instant <= new Date(range.end).getTime() + tolerance
    ));
  }

  timeline.canvas.addEventListener("pointerdown", (event) => {
    if (!timeline.layout) return;
    timeline.canvas.setPointerCapture(event.pointerId);
    if (event.shiftKey || event.button === 1) {
      timeline.drag = {
        mode: "pan",
        x: event.clientX,
        start: timeline.viewStart.getTime(),
        end: timeline.viewEnd.getTime(),
      };
    } else {
      const point = timeAtX(event.clientX);
      timeline.drag = { mode: "select", x: event.clientX, anchor: point };
      timeline.selectionStart = point;
      timeline.selectionEnd = point;
      syncInputs();
      requestTimelineDraw();
    }
  });

  timeline.canvas.addEventListener("pointermove", (event) => {
    const drag = timeline.drag;
    if (!drag || !timeline.layout) return;
    if (drag.mode === "pan") {
      const delta = (event.clientX - drag.x) / timeline.layout.width * (drag.end - drag.start);
      timeline.viewStart = new Date(drag.start - delta);
      timeline.viewEnd = new Date(drag.end - delta);
    } else {
      const point = timeAtX(event.clientX);
      timeline.selectionStart = new Date(Math.min(drag.anchor, point));
      timeline.selectionEnd = new Date(Math.max(drag.anchor, point));
      syncInputs();
    }
    requestTimelineDraw();
  });

  function finishPointer(event) {
    const drag = timeline.drag;
    if (!drag) return;
    if (drag.mode === "select" && Math.abs(event.clientX - drag.x) < 4) {
      const center = timeAtX(event.clientX);
      const sound = soundRangeNear(center);
      if (sound) {
        selectSoundRange(sound);
      } else {
        const half = Math.min(5 * 60_000, (timeline.viewEnd - timeline.viewStart) / 20);
        timeline.selectionStart = new Date(center.getTime() - half);
        timeline.selectionEnd = new Date(center.getTime() + half);
      }
      syncInputs();
    }
    if (drag.mode === "pan") scheduleTimelineLoad();
    timeline.drag = null;
    requestTimelineDraw();
  }
  timeline.canvas.addEventListener("pointerup", finishPointer);
  timeline.canvas.addEventListener("pointercancel", finishPointer);
  timeline.canvas.addEventListener("wheel", (event) => {
    event.preventDefault();
    zoomTimeline(Math.exp(event.deltaY * 0.0015), timeAtX(event.clientX));
  }, { passive: false });
  timeline.canvas.addEventListener("keydown", (event) => {
    if (event.key === "+" || event.key === "=") zoomTimeline(.7);
    else if (event.key === "-") zoomTimeline(1.4);
    else if (event.key === "Enter") $("applyHistory").click();
    else if (event.key === "ArrowLeft" || event.key === "ArrowRight") {
      const delta = (timeline.viewEnd - timeline.viewStart) / 40 * (event.key === "ArrowLeft" ? -1 : 1);
      timeline.selectionStart = new Date(timeline.selectionStart.getTime() + delta);
      timeline.selectionEnd = new Date(timeline.selectionEnd.getTime() + delta);
      syncInputs(); requestTimelineDraw();
    } else return;
    event.preventDefault();
  });

  async function loadConfig() {
    try {
      const data = await api("/api/config");
      $("config").value = data.content;
      revision = data.revision;
      $("notice").textContent = "已读取当前配置";
    } catch (error) {
      $("notice").textContent = `读取失败：${error.message}`;
    }
  }

  async function saveConfig() {
    const button = $("save");
    button.disabled = true;
    try {
      const data = await api("/api/config", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ content: $("config").value, revision }),
      });
      revision = data.revision;
      $("notice").textContent = "保存成功，重启 CamVault 后应用";
      showToast("配置已校验并安全保存");
    } catch (error) {
      $("notice").textContent = `保存失败：${error.message}`;
      showToast(`保存失败：${error.message}`, true);
    } finally {
      button.disabled = false;
    }
  }

  async function loadLogs() {
    if (currentView !== "system") return;
    try {
      const data = await api("/api/logs?limit=500");
      const box = $("logs");
      box.textContent = data.entries.map((entry) => entry.message).join("\n") || "暂无日志";
      box.scrollTop = box.scrollHeight;
    } catch (error) {
      $("logs").textContent = `读取失败：${error.message}`;
    }
  }

  async function cleanup() {
    const button = $("cleanup");
    button.disabled = true;
    try {
      const result = await api("/api/retention/run", { method: "POST" });
      showToast(`清理完成：删除 ${result.deleted_files} 个文件，释放 ${humanBytes(result.deleted_bytes)}`);
      refreshStatus();
      loadLogs();
    } catch (error) {
      showToast(`清理失败：${error.message}`, true);
    } finally {
      button.disabled = false;
    }
  }

  document.querySelectorAll(".nav-item").forEach((button) => {
    button.addEventListener("click", () => setView(button.dataset.view));
  });
  $("modeLive").addEventListener("click", () => setMode("live"));
  $("modeHistory").addEventListener("click", () => setMode("history"));
  $("zoomIn").addEventListener("click", () => zoomTimeline(.6));
  $("zoomOut").addEventListener("click", () => zoomTimeline(1.7));
  $("jumpNow").addEventListener("click", () => {
    const span = timeline.viewEnd - timeline.viewStart;
    timeline.viewEnd = new Date();
    timeline.viewStart = new Date(timeline.viewEnd.getTime() - span);
    scheduleTimelineLoad();
  });
  $("nextSound").addEventListener("click", () => {
    const ranges = allSoundRanges();
    if (!ranges.length) {
      showToast("当前时间范围内没有检测到声音");
      return;
    }
    const after = timeline.selectionEnd.getTime() + 1_000;
    const range = ranges.find((item) => new Date(item.start).getTime() >= after) || ranges[0];
    selectSoundRange(range);
    showToast(`已定位到 ${new Date(range.start).toLocaleString()} 的声音片段`);
  });
  document.querySelectorAll("[data-span]").forEach((button) => {
    button.addEventListener("click", () => {
      const span = Number(button.dataset.span);
      timeline.viewEnd = new Date();
      timeline.viewStart = new Date(timeline.viewEnd.getTime() - span);
      document.querySelectorAll("[data-span]").forEach((item) => item.classList.toggle("active", item === button));
      scheduleTimelineLoad();
    });
  });
  $("historyStart").addEventListener("change", () => {
    const value = new Date($("historyStart").value);
    if (!Number.isNaN(value.getTime())) timeline.selectionStart = value;
    requestTimelineDraw();
  });
  $("historyEnd").addEventListener("change", () => {
    const value = new Date($("historyEnd").value);
    if (!Number.isNaN(value.getTime())) timeline.selectionEnd = value;
    requestTimelineDraw();
  });
  $("applyHistory").addEventListener("click", () => {
    if (timeline.selectionStart >= timeline.selectionEnd) {
      showToast("回放结束时间必须晚于开始时间", true);
      return;
    }
    attachAll();
  });
  $("playbackRate").addEventListener("change", () => {
    playbackRate = Number($("playbackRate").value) || 1;
    players.forEach((_player, cameraId) => {
      const video = $(`video-${cameraId}`);
      if (video) applyPlaybackRate(video);
    });
    showToast(`回放速度已设为 ${playbackRate}×`);
  });
  $("reload").addEventListener("click", loadConfig);
  $("save").addEventListener("click", saveConfig);
  $("refreshLogs").addEventListener("click", loadLogs);
  $("cleanup").addEventListener("click", cleanup);
  if (boot.hasLogin) {
    $("logout").hidden = false;
    $("logout").addEventListener("click", async () => {
      try { await api("/logout", { method: "POST" }); } catch (_error) { /* redirect anyway */ }
      window.location.assign("/login");
    });
  }

  document.addEventListener("visibilitychange", () => {
    players.forEach((player, cameraId) => {
      const video = $(`video-${cameraId}`);
      if (document.hidden) {
        video.pause();
        if (player.hls) player.hls.stopLoad();
      } else if (currentView === "monitor") {
        if (player.hls) player.hls.startLoad(mode === "live" ? -1 : video.currentTime);
        video.play().catch(() => {});
      }
    });
  });
  window.addEventListener("beforeunload", () => cameras.forEach((camera) => destroyPlayer(camera.id)));
  new ResizeObserver(resizeTimeline).observe(timeline.canvas);

  syncInputs();
  $("clock").textContent = new Intl.DateTimeFormat("zh-CN", { dateStyle: "long", timeStyle: "short" }).format(new Date());
  refreshStatus();
  attachAll();
  statusTimer = setInterval(refreshStatus, 5000);
  window.addEventListener("beforeunload", () => clearInterval(statusTimer));
})();
