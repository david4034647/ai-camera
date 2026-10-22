/* AI Camera dashboard – vanilla JS, no build step. */
const $ = (id) => document.getElementById(id);

const state = {
  running: false,
  lastEventId: 0,
  eventsSig: "",
  recordsSig: "",
  day: "",
  busy: false,
  streamKey: 0,
  streamRetry: null,
};

/* ------------------------------ helpers ------------------------------ */
function fmtBytes(n) {
  if (!n) return "0 B";
  const u = ["B", "KB", "MB", "GB", "TB"];
  let i = 0;
  while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
  return `${n >= 100 ? n.toFixed(0) : n.toFixed(1)} ${u[i]}`;
}

function fmtTime(iso) {
  if (!iso) return "—";
  const d = new Date(iso);
  if (isNaN(d)) return iso;
  const p = (x) => String(x).padStart(2, "0");
  return `${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
}

function fmtUptime(sec) {
  if (!sec) return "—";
  const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60);
  return h ? `${h}h ${m}m` : `${m}m ${sec % 60}s`;
}

function toast(msg, ms = 2600) {
  const t = $("toast");
  t.textContent = msg;
  t.classList.add("show");
  clearTimeout(t._timer);
  t._timer = setTimeout(() => t.classList.remove("show"), ms);
}

async function api(path, opts = {}) {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...opts,
  });
  if (!res.ok) {
    let detail = res.statusText;
    try { detail = (await res.json()).detail || detail; } catch (_) {}
    throw new Error(detail);
  }
  return res.headers.get("content-type")?.includes("json") ? res.json() : res.text();
}

/* ------------------------------ live view ------------------------------ */
function startStream() {
  const img = $("live");
  clearTimeout(state.streamRetry);
  state.streamKey += 1;
  img.src = `/stream.mjpg?k=${state.streamKey}&t=${Date.now()}`;
  $("videoWrap").classList.add("on");
}

function stopStream() {
  clearTimeout(state.streamRetry);
  const img = $("live");
  img.removeAttribute("src");
  $("videoWrap").classList.remove("on");
}

$("live").addEventListener("error", () => {
  if (!state.running) return;
  state.streamRetry = setTimeout(startStream, 2500);
});

/* ------------------------------ status ------------------------------ */
async function refreshStatus() {
  try {
    const s = await api("/api/status");
    const wasRunning = state.running;
    state.running = s.running;

    $("toggle").checked = s.running;
    $("statePill").classList.toggle("on", s.running);
    $("stateText").textContent = s.running ? "监控中" : "已停止";
    $("startBtn").textContent = s.running ? "监控运行中" : "开启监控";

    if (s.running && !wasRunning) startStream();
    if (!s.running && wasRunning) stopStream();

    const cam = s.camera || {};
    const det = s.detector || {};
    const rec = s.recording || {};
    const st = s.storage || {};

    $("mCamState").textContent = s.running
      ? (cam.status === "running" ? "正常" : cam.status)
      : "关闭";
    $("mRes").textContent = cam.width ? `${cam.width}×${cam.height}` : "—";
    $("mFps").textContent = s.running ? `${cam.fps || 0} fps` : "—";
    $("mLat").textContent = det.latency_ms ? `${det.latency_ms} ms` : "—";
    $("mUptime").textContent = s.running ? fmtUptime(s.uptime_seconds) : "—";
    $("mModel").textContent = det.model || "—";

    $("tPersons").textContent = s.running ? (det.persons || 0) : 0;
    $("tFaces").textContent = `人脸 ${s.running ? (det.faces || 0) : 0}`;
    $("tEvents").textContent = s.events_today || 0;
    $("tActive").textContent = rec.event_active ? `事件 #${rec.active_event_id} 进行中` : "无进行中事件";
    $("tRecSize").textContent = fmtBytes(rec.segment_bytes || 0);
    $("tRecFile").textContent = rec.file || "未录制";
    $("tDisk").textContent = fmtBytes(st.disk_free_bytes || 0);
    $("tRetention").textContent = `保留 ${st.retention_days || 7} 天 · 已用 ${fmtBytes(st.used_bytes || 0)}`;
    $("recHint").textContent = s.running ? "录制中" : "";

    if (!s.running) $("mUptime").textContent = "—";

    // refresh lists when a new event lands
    if ((s.events_today || 0) !== state.lastEventCount) {
      state.lastEventCount = s.events_today || 0;
      loadEvents();
      loadRecordings();
    }
  } catch (e) {
    $("statePill").classList.remove("on");
    $("stateText").textContent = "连接断开";
  }
}

/* ------------------------------ toggle ------------------------------ */
async function setMonitoring(on) {
  if (state.busy) return;
  state.busy = true;
  const t = $("toggle");
  t.disabled = true;
  try {
    await api(`/api/monitor/${on ? "start" : "stop"}`, { method: "POST" });
    toast(on ? "监控已开启，正在录像…" : "监控已关闭");
    if (!on) stopStream();
  } catch (e) {
    t.checked = !on;
    toast(`操作失败：${e.message}`, 4000);
  } finally {
    state.busy = false;
    t.disabled = false;
    refreshStatus();
  }
}

$("toggle").addEventListener("change", (e) => setMonitoring(e.target.checked));
$("startBtn").addEventListener("click", () => setMonitoring(true));

/* ------------------------------ events ------------------------------ */
async function loadEvents() {
  try {
    const q = state.day ? `?day=${encodeURIComponent(state.day)}&limit=120` : "?limit=120";
    const data = await api(`/api/events${q}`);
    const sig = JSON.stringify(data.events.map((e) => [e.id, e.clip, e.duration]));
    if (sig === state.eventsSig) return;
    state.eventsSig = sig;
    renderEvents(data.events);
    renderDays(data.days || []);
  } catch (e) { /* keep old list */ }
}

function renderEvents(events) {
  const box = $("events");
  if (!events.length) {
    box.innerHTML = `<div class="empty">暂无事件 · 开启监控后检测到人像会自动记录</div>`;
    return;
  }
  box.innerHTML = events.map((e) => {
    const img = e.has_snapshot ? `/api/events/${e.id}/snapshot` : "";
    const dur = e.duration ? `${e.duration}s` : (e.ended_at ? "—" : "进行中");
    return `<div class="event" data-id="${e.id}">
      ${img ? `<img loading="lazy" src="${img}" alt="事件 ${e.id}">`
            : `<div style="aspect-ratio:16/9;display:grid;place-items:center;color:#475569;font-size:12px">无截图</div>`}
      <div class="info">
        <span class="time">${fmtTime(e.started_at)}</span>
        <span class="tags">
          <span class="tag">人 ${e.persons || 1}</span>
          ${e.faces ? `<span class="tag face">脸 ${e.faces}</span>` : ""}
          ${e.has_clip ? `<span class="tag clip">${e.clip ? dur : "片段"}</span>` : ""}
        </span>
      </div>
    </div>`;
  }).join("");

  box.querySelectorAll(".event").forEach((el) => {
    el.addEventListener("click", () => openEvent(Number(el.dataset.id)));
  });
}

function renderDays(days) {
  const sel = $("dayFilter");
  const current = state.day;
  if (sel.dataset.sig === JSON.stringify(days.map((d) => [d.day, d.events]))) return;
  sel.dataset.sig = JSON.stringify(days.map((d) => [d.day, d.events]));
  sel.innerHTML = `<option value="">全部日期</option>` +
    days.map((d) => `<option value="${d.day}">${d.day} (${d.events})</option>`).join("");
  sel.value = current;
}

$("dayFilter").addEventListener("change", (e) => {
  state.day = e.target.value;
  loadEvents();
});

async function openEvent(id) {
  try {
    const ev = await api(`/api/events/${id}`);
    const html = `
      <h4>事件 #${ev.id} · ${fmtTime(ev.started_at)}</h4>
      <div class="row">
        <span>人数 ${ev.persons || 1}</span>
        <span>人脸 ${ev.faces || 0}</span>
        <span>持续 ${ev.duration ? ev.duration + " 秒" : "进行中"}</span>
        <span>置信度 ${ev.score ? ev.score.toFixed(2) : "—"}</span>
        <span>结束 ${fmtTime(ev.ended_at)}</span>
      </div>
      ${ev.clip ? `<video controls autoplay muted loop src="/api/events/${id}/clip"></video>` : ""}
      ${ev.snapshot ? `<img src="/api/events/${id}/snapshot" alt="snapshot" style="margin-top:${ev.clip ? "12px" : "0"}">` : ""}`;
    $("modalContent").innerHTML = html;
    $("modal").classList.add("open");
  } catch (e) {
    toast(`打开事件失败：${e.message}`);
  }
}

/* ------------------------------ recordings ------------------------------ */
async function loadRecordings() {
  try {
    const data = await api("/api/recordings");
    const sig = JSON.stringify(data.files.map((f) => [f.name, f.size]));
    if (sig === state.recordsSig) return;
    state.recordsSig = sig;
    renderRecords(data.files);
    renderStorage(data.storage);
  } catch (e) { /* ignore */ }
}

function renderRecords(files) {
  const box = $("records");
  if (!files.length) {
    box.innerHTML = `<div class="empty">暂无录像文件</div>`;
    return;
  }
  box.innerHTML = files.map((f) => `
    <div class="record">
      <span class="name">${f.name}</span>
      <span class="size">${fmtBytes(f.size)}</span>
      <button class="play" data-name="${f.name}">播放</button>
    </div>`).join("");
  box.querySelectorAll(".play").forEach((b) => {
    b.addEventListener("click", () => openRecording(b.dataset.name));
  });
}

function renderStorage(st) {
  if (!st) return;
  const days = (st.days || []).filter((d) => d.bytes > 0);
  const total = days.reduce((a, d) => a + d.bytes, 0) || 1;
  const colors = ["#22d3ee", "#38bdf8", "#818cf8", "#a78bfa", "#f472b6", "#fb923c", "#facc15"];
  const bar = days.map((d, i) => {
    const pct = (d.bytes / total) * 100;
    return `<span style="width:${pct}%;background:${colors[i % colors.length]}" title="${d.date}"></span>`;
  }).join("");
  const legend = days.map((d, i) =>
    `<span><i style="background:${colors[i % colors.length]}"></i>${d.date} · ${fmtBytes(d.bytes)} · ${d.events || 0} 事件</span>`
  ).join("");
  $("storage").innerHTML = `
    <div class="storage-bar">${bar}</div>
    <div class="storage-legend">${legend || `<span>还没有录像数据</span>`}</div>`;
}

async function openRecording(name) {
  $("modalContent").innerHTML = `
    <h4>${name}</h4>
    <video controls autoplay src="/api/recordings/${encodeURIComponent(name)}"></video>
    <div class="row"><a href="/api/recordings/${encodeURIComponent(name)}" download
      style="color:#67e8f9">下载文件</a></div>`;
  $("modal").classList.add("open");
}

/* ------------------------------ misc ------------------------------ */
$("modalClose").addEventListener("click", () => {
  $("modal").classList.remove("open");
  $("modalContent").innerHTML = "";
});
$("modal").addEventListener("click", (e) => {
  if (e.target === $("modal")) $("modalClose").click();
});
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") $("modalClose").click();
});

$("refreshBtn").addEventListener("click", () => {
  state.eventsSig = "";
  state.recordsSig = "";
  loadEvents();
  loadRecordings();
  refreshStatus();
  toast("已刷新");
});

async function loadConfig() {
  try {
    const c = await api("/api/config");
    $("cDetect").textContent = c.detect_fps;
    $("cClip").textContent = c.event_clip_seconds || "关闭";
  } catch (_) {}
}

/* boot */
loadConfig();
refreshStatus();
loadEvents();
loadRecordings();
setInterval(refreshStatus, 2000);
setInterval(loadEvents, 6000);
setInterval(loadRecordings, 8000);
