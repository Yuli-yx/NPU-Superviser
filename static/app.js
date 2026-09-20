"use strict";
/* NPU 资源看板前端逻辑:轮询 /api/dashboard,按 key 增量更新 DOM(避免重播入场动画)。 */

const $ = (sel, root) => (root || document).querySelector(sel);
const $$ = (sel, root) => Array.from((root || document).querySelectorAll(sel));

const REFRESH_SECS = 30 * 60;

const state = {
  data: null,
  models: [],
  config: {},
  filters: { q: "", model: "", tag: "", network: "", st: "" },
  revealed: new Set(),   // 已显示密码的服务器 id
  fetching: false,
  dragging: null,
  sorting: false,
  lastRefresh: 0,
  lastRefreshAttempt: 0,
  collecting: new Set(),
};
const pendingWrites = new Set();
const requestCooldowns = new Map();

/* ---------------- 工具 ---------------- */

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, c => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

async function api(path, method, body) {
  if (Date.now() < (requestCooldowns.get(path) || 0)) throw new Error("操作过于频繁，请稍后再试");
  const write = method && method !== "GET";
  const key = `${method}:${path}`;
  if (write && pendingWrites.has(key)) throw new Error("操作正在处理中，请勿重复提交");
  if (write) pendingWrites.add(key);
  try {
  const opts = { method: method || "GET", headers: {} };
  if (body !== undefined) {
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(body);
  }
  const res = await fetch(path, opts);
  let data = null;
  try { data = await res.json(); } catch (_) { /* 空响应 */ }
  if (!res.ok) {
    if (res.status === 429) requestCooldowns.set(path, Date.now() + (Number(res.headers.get("Retry-After")) || 5) * 1000);
    throw new Error((data && data.error) || `请求失败 (${res.status})`);
  }
  return data;
  } finally { if (write) pendingWrites.delete(key); }
}

function fmtGB(mb) {
  if (mb == null) return "—";
  const gb = mb / 1024;
  return gb >= 100 ? gb.toFixed(0) : gb >= 10 ? gb.toFixed(1) : gb.toFixed(2);
}
function fmtPct(v) { return v == null ? "—" : `${Math.round(v)}%`; }
function fmtDT(ts) {
  if (!ts) return "—";
  return new Date(ts * 1000).toLocaleString("zh-CN", { hour12: false });
}
function ago(ts) {
  if (!ts) return "未采集";
  const s = Math.max(0, Date.now() / 1000 - ts);
  if (s < 10) return "刚刚";
  if (s < 60) return `${Math.floor(s)} 秒前`;
  if (s < 3600) return `${Math.floor(s / 60)} 分钟前`;
  if (s < 86400) return `${Math.floor(s / 3600)} 小时前`;
  return `${Math.floor(s / 86400)} 天前`;
}
function toLocalInput(ts) {
  const d = ts ? new Date(ts * 1000) : new Date();
  const p = n => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}T${p(d.getHours())}:${p(d.getMinutes())}`;
}

function toast(msg, isErr) {
  if ($$(".toast:not(.out)").some(el => el.textContent === msg)) return;
  const existing = $$(".toast");
  if (existing.length >= 3) existing[0].remove();
  const el = document.createElement("div");
  el.className = "toast" + (isErr ? " err" : "");
  el.textContent = msg;
  $("#toasts").appendChild(el);
  setTimeout(() => {
    el.classList.add("out");
    el.addEventListener("animationend", () => el.remove(), { once: true });
  }, 3200);
}

async function copyText(text, tip) {
  try {
    await navigator.clipboard.writeText(text);
  } catch (_) {
    const ta = document.createElement("textarea");
    ta.value = text;
    ta.style.position = "fixed";
    ta.style.opacity = "0";
    document.body.appendChild(ta);
    ta.select();
    document.execCommand("copy");
    ta.remove();
  }
  toast(tip || "已复制");
}

function confirmBox(text, okLabel) {
  return new Promise(resolve => {
    const overlay = $("#confirmModal");
    $("#cfText").textContent = text;
    $("#cfOk").textContent = okLabel || "确认";
    overlay.hidden = false;
    let settled = false;
    const onKey = e => { if (e.key === "Escape") { e.stopImmediatePropagation(); done(false); } };
    const done = val => {
      if (settled) return;
      settled = true;
      document.removeEventListener("keydown", onKey, true);
      overlay.hidden = true;
      resolve(val);
    };
    document.addEventListener("keydown", onKey, true);
    $("#cfOk").onclick = () => done(true);
    $("#cfCancel").onclick = () => done(false);
    overlay.onclick = e => { if (e.target === overlay) done(false); };
  });
}

/* ---------------- 数据获取 ---------------- */

async function refresh(options = {}) {
  if (state.fetching || state.dragging !== null || state.sorting) return;
  const manual = options instanceof Event;
  if (manual && Date.now() - state.lastRefreshAttempt < 5000) {
    toast("刷新过于频繁，请间隔 5 秒再试", true);
    return;
  }
  state.lastRefreshAttempt = Date.now();
  state.fetching = true;
  $("#btnRefresh").disabled = true;
  try {
    state.data = await api("/api/dashboard");
    render();
    state.lastRefresh = Date.now();
  } catch (e) {
    toast(`刷新失败:${e.message}`, true);
  } finally {
    state.fetching = false;
    $("#btnRefresh").disabled = false;
  }
}

async function loadConfig() {
  try {
    const res = await api("/api/config");
    state.config = res.config || {};
    state.models = res.models || [];
    buildModelOptions();
    buildTagOptions(res.tags || []);
  } catch (_) { /* 配置加载失败不阻塞看板 */ }
}

/* ---------------- 渲染:统计条 ---------------- */

function renderStats() {
  const sum = state.data.summary;
  const items = [
    { key: "cards_total", label: "NPU 卡", cls: "all", st: null },
    { key: "idle", label: "空闲", cls: "idle", st: "idle" },
    { key: "occupied", label: "已占用", cls: "occupied", st: "occupied" },
    { key: "busy", label: "在跑 · 未登记", cls: "busy", st: "busy" },
    { key: "offline", label: "离线", cls: "offline", st: "offline" },
  ];
  const box = $("#stats");
  box.innerHTML = items.map(it => `
    <div class="stat ${it.st ? "clickable" : ""}" ${it.st ? `data-st="${it.st}"` : ""}
         title="${it.st ? "点击按此状态筛选" : ""}">
      <i class="dot ${it.cls}"></i>
      <span class="num">${sum[it.key] ?? 0}</span>
      <span class="lbl">${it.label}</span>
    </div>`).join("");
}

/* ---------------- 渲染:看板 ---------------- */

function filteredServers() {
  const { q, model, tag, network } = state.filters;
  const kw = q.trim().toLowerCase();
  return state.data.servers.filter(s =>
    (!kw || s.name.toLowerCase().includes(kw) || s.ip.toLowerCase().includes(kw))
    && (!model || s.model === model)
    && (!tag || (s.tags || []).includes(tag))
    && (!network || Object.entries(s.network_groups || {}).some(([kind, group]) => `${kind}:${group}` === network)));
}

function serverFamily(s) {
  const model = (s.model || "").trim().toUpperCase();
  return model.startsWith("A3") ? "A3" : model.startsWith("A5") ? "A5" : "其他";
}

function matchesServerState(s) {
  const filter = state.filters.st;
  return !filter || s.cards.some(c => c.state === filter)
    || (filter === "offline" && s.status !== "online" && !s.cards.length);
}

function renderBoard() {
  const board = $("#board");
  const servers = filteredServers();
  const seen = new Set();
  const positions = new Map();
  const families = ["A5", "A3", "其他"];
  families.forEach(family => {
    let section = $(`[data-family="${family}"]`, board);
    if (!section) {
      section = document.createElement("section");
      section.className = "machine-section";
      section.dataset.family = family;
      section.innerHTML = `<header class="machine-heading"><h2>${family === "其他" ? "其他 / 未标注型号" : family + " 服务器"}</h2><span class="machine-count"></span></header><div class="machine-grid"></div>`;
      board.appendChild(section);
    }
  });

  servers.forEach((s, i) => {
    seen.add(s.id);
    let el = board.querySelector(`.server-card[data-sid="${s.id}"]`);
    if (!el) el = buildServerCard(s);
    const family = serverFamily(s);
    const grid = $(`[data-family="${family}"] .machine-grid`, board);
    const index = positions.get(family) || 0;
    if (grid.children[index] !== el) grid.insertBefore(el, grid.children[index] || null);
    positions.set(family, index + 1);
    updateServerCard(el, s, i);
  });
  $$(".server-card", board).forEach(el => {
    if (!seen.has(+el.dataset.sid)) el.remove();
  });
  families.forEach(family => {
    const section = $(`[data-family="${family}"]`, board);
    const visible = $$(".server-card", section).filter(el => el.style.display !== "none");
    section.hidden = !visible.length;
    $(".machine-count", section).textContent = `${visible.length} 台 · 拖动 ⠿ 调整顺序`;
  });

  // 空状态
  let empty = $(".empty", board);
  if (servers.some(matchesServerState)) {
    if (empty) empty.remove();
  } else {
    const hasAny = state.data.servers.length > 0;
    if (!empty) {
      empty = document.createElement("div");
      empty.className = "empty";
      board.appendChild(empty);
    }
    empty.innerHTML = hasAny
      ? `<div class="big">没有符合筛选条件的服务器</div>
         <div class="hint">调整搜索词或筛选条件试试</div>`
      : `<div class="big">还没有服务器</div>
         <div class="hint">录入第一台 NPU 服务器的 SSH 信息,看板会自动采集每张卡的
         AI Core 利用率、HBM 显存、功耗与温度;同事可以在卡格上登记占用。</div>
         <button class="btn primary" id="emptyAdd">添加第一台服务器</button>`;
    const addBtn = $("#emptyAdd");
    if (addBtn) addBtn.onclick = () => openServerModal(null);
  }
}

const ACTION_SVG = {
  collect: `<svg viewBox="0 0 16 16"><path d="M13.5 8a5.5 5.5 0 1 1-1.6-3.9" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round"/><path d="M13.7 1.8v3h-3" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"/></svg>`,
  edit: `<svg viewBox="0 0 16 16"><path d="M11.3 2.2l2.5 2.5L5.5 13H3v-2.5z" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linejoin="round"/></svg>`,
  delete: `<svg viewBox="0 0 16 16"><path d="M3 4.5h10M6.5 4.5V3h3v1.5M4.5 4.5l.7 8.5h5.6l.7-8.5" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round"/></svg>`,
};

function buildServerCard(s) {
  const el = document.createElement("article");
  el.className = "server-card";
  el.dataset.sid = s.id;
  el.innerHTML = `
    <div class="sc-head">
      <span class="drag-handle" tabindex="0" role="button" aria-label="拖动排序，或按方向键移动" title="拖动调整位置；方向键也可调整">⠿</span>
      <span class="sc-dot"></span>
      <h3 class="sc-name"></h3>
      <span class="sc-model mono"></span>
      <div class="sc-tags"></div>
      <div class="sc-actions">
        <button class="icon-btn" data-act="collect" title="立即采集"></button>
        <button class="icon-btn" data-act="toggle" title="启用 / 停用定时采集"></button>
        <button class="icon-btn" data-act="edit" title="编辑台账"></button>
        <button class="icon-btn danger" data-act="delete" title="删除服务器"></button>
      </div>
    </div>
    <div class="sc-meta">
      <span class="mono ip"></span>
      <span class="ssh-box">
        <span class="user mono"></span>
        <span class="pw"></span>
        <button class="mini-btn" data-act="eye" title="显示 / 隐藏密码">
          <svg viewBox="0 0 16 16"><path d="M1.5 8S4 3.5 8 3.5 14.5 8 14.5 8 12 12.5 8 12.5 1.5 8 1.5 8z" fill="none" stroke="currentColor" stroke-width="1.4"/><circle cx="8" cy="8" r="2" fill="currentColor"/></svg>
        </button>
        <button class="mini-btn" data-act="copypw" title="复制密码">
          <svg viewBox="0 0 16 16"><rect x="5.5" y="5.5" width="8" height="8" rx="1.5" fill="none" stroke="currentColor" stroke-width="1.4"/><path d="M10.5 3.5h-7a1 1 0 0 0-1 1v7" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round"/></svg>
        </button>
      </span>
    </div>
    <div class="sc-sub"></div>
    <div class="sc-networks"></div>
    <div class="sc-error" hidden></div>
    <div class="npu-wall"></div>`;
  const wall = $(".npu-wall", el);
  wall.addEventListener("click", e => {
    const tile = e.target.closest(".tile");
    if (!tile) return;
    openTileModal(s.id, +tile.dataset.npu);
  });
  wall.addEventListener("mouseover", e => {
    const tile = e.target.closest(".tile");
    if (tile) showTooltip(tile, s.id, +tile.dataset.npu);
  });
  wall.addEventListener("mouseout", e => {
    if (e.target.closest(".tile")) hideTooltip();
  });
  return el;
}

function updateServerCard(el, s, i) {
  el.style.setProperty("--i", i);
  const dot = $(".sc-dot", el);
  const dotCls = s.status === "online" ? "online"
    : s.status === "offline" ? "offline" : "pending";
  dot.className = "sc-dot " + dotCls;
  dot.title = { online: "采集正常", offline: "采集失败", pending: "等待首次采集" }[dotCls];

  $(".sc-name", el).textContent = s.name;
  const modelEl = $(".sc-model", el);
  modelEl.textContent = s.model || "未标注型号";
  modelEl.hidden = !s.model;

  const tagsEl = $(".sc-tags", el);
  const tagsHtml = (s.tags || []).map(t => `<span class="tag-chip">${esc(t)}</span>`).join("");
  if (tagsEl.dataset.raw !== tagsHtml) { tagsEl.innerHTML = tagsHtml; tagsEl.dataset.raw = tagsHtml; }
  const groups = Object.entries(s.network_groups || {});
  $(".sc-networks", el).innerHTML = groups.length
    ? groups.map(([kind, group]) => `<span class="network-chip" title="人工标记：同网络、同互通组才表示互通；不代表实时连通性检测">${esc(({uboe: "UBoE", roce: "RoCE", ubg: "UBG"})[kind] || kind)} <b>${esc(group)}</b></span>`).join("")
    : `<span class="network-unknown">互通组未标注 · 相同网络类型不代表互通</span>`;

  $(".ip", el).textContent = s.collect_enabled ? `${s.ip}:${s.ssh_port}` : `${s.ip}:${s.ssh_port}(已停采)`;
  $(".user", el).textContent = `${s.username} /`;
  updatePassword(el, s);

  // 采集状态行
  const sub = $(".sc-sub", el);
  const parts = [];
  if (!s.collect_enabled) parts.push(`<span class="paused-badge">已停采</span>`);
  parts.push(`<span class="${s.status === "online" ? "ok" : ""}">${s.status === "online" ? "●" : "○"} 采集于 ${ago(s.last_collect_ts)}</span>`);
  if (s.npu_smi_version) parts.push(`<span class="ver">npu-smi ${esc(s.npu_smi_version)}</span>`);
  parts.push(`<span>${s.cards.length} 卡</span>`);
  sub.innerHTML = parts.join("");

  const errEl = $(".sc-error", el);
  const hasErr = s.status === "offline" && s.last_error;
  errEl.hidden = !hasErr;
  if (hasErr) errEl.textContent = `采集失败:${s.last_error}`;

  // 操作按钮
  const btnCollect = $('[data-act="collect"]', el);
  btnCollect.innerHTML = ACTION_SVG.collect;
  btnCollect.disabled = state.collecting.has(s.id);
  const btnToggle = $('[data-act="toggle"]', el);
  btnToggle.innerHTML = s.collect_enabled
    ? `<svg viewBox="0 0 16 16"><rect x="4" y="3.5" width="2.6" height="9" rx="1" fill="currentColor"/><rect x="9.4" y="3.5" width="2.6" height="9" rx="1" fill="currentColor"/></svg>`
    : `<svg viewBox="0 0 16 16"><path d="M5.5 3.5v9l7-4.5z" fill="currentColor"/></svg>`;
  btnToggle.title = s.collect_enabled ? "停用定时采集" : "启用定时采集";
  const btnEdit = $('[data-act="edit"]', el); btnEdit.innerHTML = ACTION_SVG.edit;
  const btnDel = $('[data-act="delete"]', el); btnDel.innerHTML = ACTION_SVG.delete;

  // NPU 芯片墙(按 npu_id 增量更新)
  const wall = $(".npu-wall", el);
  const seen = new Set();
  const stFilter = state.filters.st;
  let visibleCount = 0;
  s.cards.forEach((c, j) => {
    seen.add(c.npu_id);
    let tile = wall.querySelector(`.tile[data-npu="${c.npu_id}"]`);
    if (!tile) { tile = buildTile(c); wall.appendChild(tile); }
    updateTile(tile, c, j);
    const show = !stFilter || c.state === stFilter;
    tile.classList.toggle("hidden-by-filter", !show);
    if (show) visibleCount++;
  });
  $$(".tile", wall).forEach(t => { if (!seen.has(+t.dataset.npu)) t.remove(); });
  el.style.display = matchesServerState(s) ? "" : "none";
}

function updatePassword(el, s) {
  const pwEl = $(".pw", el);
  const shown = state.revealed.has(s.id);
  const html = shown ? `<span class="pw-real mono">${esc(s.password || "(空)")}</span>` : `<span class="pw-mask">······</span>`;
  if (pwEl.innerHTML !== html) pwEl.innerHTML = html;
  const button = $('[data-act="eye"]', el);
  button.setAttribute("aria-pressed", String(shown));
  button.title = shown ? "隐藏密码" : "查看密码";
}

function buildTile(c) {
  const tile = document.createElement("button");
  tile.type = "button";
  tile.className = "tile";
  tile.dataset.npu = c.npu_id;
  tile.innerHTML = `
    <div class="t-top"><span class="t-id"></span><span class="t-chip"></span></div>
    <div class="t-core"><span class="v"></span><span class="u">%</span><span class="t-core-label">AI CORE</span></div>
    <div class="t-hbm"><div class="t-hbm-bar"><div class="t-hbm-fill"></div></div><div class="t-hbm-txt"></div></div>
    <div class="t-meta"><span class="t-temp"></span><span class="t-power"></span></div>
    <div class="t-occ" hidden><i class="occ-dot"></i><span class="who"></span></div>`;
  return tile;
}

function updateTile(tile, c, j) {
  tile.style.setProperty("--j", j);
  tile.className = `tile st-${c.state}` + (c.health && c.health !== "OK" && c.health !== "NA" ? " health-bad" : "");
  tile.dataset.npu = c.npu_id;

  $(".t-id", tile).textContent = `NPU ${c.npu_id}`;
  $(".t-chip", tile).textContent = c.chip_name || "";
  const core = $(".t-core", tile);
  if (c.aicore_pct == null) {
    core.innerHTML = `<span class="dash">—</span><span class="t-core-label">AI CORE</span>`;
  } else {
    core.innerHTML = `<span class="v">${Math.round(c.aicore_pct)}</span><span class="u">%</span><span class="t-core-label">AI CORE</span>`;
  }

  const fill = $(".t-hbm-fill", tile);
  const pct = c.hbm_total_mb ? Math.min(100, (c.hbm_used_mb || 0) / c.hbm_total_mb * 100) : 0;
  fill.style.width = (c.hbm_total_mb ? Math.max(pct, 2) : 0) + "%";
  $(".t-hbm-txt", tile).textContent = c.hbm_total_mb
    ? `${fmtGB(c.hbm_used_mb)} / ${fmtGB(c.hbm_total_mb)} GB` : "HBM —";

  $(".t-temp", tile).textContent = c.temp_c != null ? `${Math.round(c.temp_c)}°C` : "";
  $(".t-power", tile).textContent = c.power_w != null ? `${Math.round(c.power_w)}W` : "";

  const occEl = $(".t-occ", tile);
  if (c.occupancy) {
    occEl.hidden = false;
    $(".who", occEl).textContent = c.occupancy.user + (c.occupancy.purpose ? ` · ${c.occupancy.purpose}` : "");
  } else {
    occEl.hidden = true;
  }

  // 数值明显变化时闪一圈
  const prevCore = tile.dataset.prevCore, prevHbm = tile.dataset.prevHbm;
  const changed = (prevCore !== undefined && c.aicore_pct != null
      && Math.abs(c.aicore_pct - +prevCore) >= 5)
    || (prevHbm !== undefined && c.hbm_used_mb != null
      && Math.abs(c.hbm_used_mb - +prevHbm) >= 1024);
  if (changed && tile.isConnected) {
    tile.classList.remove("flash");
    void tile.offsetWidth; // 重启动画
    tile.classList.add("flash");
  }
  tile.dataset.prevCore = c.aicore_pct;
  tile.dataset.prevHbm = c.hbm_used_mb;
}

/* ---------------- Tooltip ---------------- */

let tooltipTimer = null;
function showTooltip(tile, sid, npu) {
  clearTimeout(tooltipTimer);
  tooltipTimer = setTimeout(() => {
    const s = state.data.servers.find(x => x.id === sid);
    if (!s) return;
    const c = s.cards.find(x => x.npu_id === npu);
    if (!c) return;
    const occ = c.occupancy;
    const tip = $("#tooltip");
    tip.innerHTML = [
      `<b>${esc(s.name)} · NPU ${c.npu_id}</b>`,
      `芯片 ${esc(c.chip_name || "—")} · Health ${esc(c.health || "—")}`,
      `AI Core ${fmtPct(c.aicore_pct)} · HBM ${c.hbm_total_mb ? `${fmtGB(c.hbm_used_mb)}/${fmtGB(c.hbm_total_mb)}GB` : "—"}`,
      `${c.temp_c != null ? Math.round(c.temp_c) + "°C" : "—"} · ${c.power_w != null ? Math.round(c.power_w) + "W" : "—"}`,
      occ ? `占用:${esc(occ.user)}${occ.purpose ? " · " + esc(occ.purpose) : ""} · 自 ${fmtDT(occ.start_ts)}`
          : `未登记占用(${c.state === "busy" ? "检测到负载" : c.state === "idle" ? "空闲" : "离线"})`,
    ].join("<br>");
    tip.hidden = false;
    const r = tile.getBoundingClientRect();
    const tw = tip.offsetWidth, th = tip.offsetHeight;
    let x = r.left + r.width / 2 - tw / 2;
    let y = r.top - th - 8;
    if (y < 8) y = r.bottom + 8;
    x = Math.max(8, Math.min(x, innerWidth - tw - 8));
    tip.style.left = x + "px";
    tip.style.top = y + "px";
  }, 260);
}
function hideTooltip() {
  clearTimeout(tooltipTimer);
  $("#tooltip").hidden = true;
}

/* ---------------- 弹窗通用 ---------------- */

function openOverlay(id) { $(id).hidden = false; }
function closeOverlay(el) {
  const overlay = el.closest(".overlay");
  if (overlay) overlay.hidden = true;
}

/* ---------------- 卡片详情 / 登记弹窗 ---------------- */

function openTileModal(sid, npu) {
  const s = state.data.servers.find(x => x.id === sid);
  if (!s) return;
  const c = s.cards.find(x => x.npu_id === npu);
  if (!c) return;

  $("#tmTitle").textContent = `${s.name} · NPU ${c.npu_id}`;
  $("#tmMetrics").innerHTML = [
    ["芯片型号", esc(c.chip_name || "—")],
    ["Health", esc(c.health || "—")],
    ["AI Core", fmtPct(c.aicore_pct)],
    ["HBM 显存", c.hbm_total_mb ? `${fmtGB(c.hbm_used_mb)}<small> / ${fmtGB(c.hbm_total_mb)} GB</small>` : "—"],
    ["温度", c.temp_c != null ? `${Math.round(c.temp_c)}<small> °C</small>` : "—"],
    ["功耗", c.power_w != null ? `${Math.round(c.power_w)}<small> W</small>` : "—"],
  ].map(([k, v]) => `<div class="metric"><div class="k">${k}</div><div class="v">${v}</div></div>`).join("");

  const view = $("#tmOccView"), form = $("#occForm");
  if (c.occupancy) {
    view.hidden = false;
    form.hidden = true;
    view.innerHTML = `
      <div>
        <div class="who">${esc(c.occupancy.user)}</div>
        <div class="info">${esc(c.occupancy.purpose || "未填用途")} · 自 ${fmtDT(c.occupancy.start_ts)}</div>
      </div>
      <button class="btn danger" id="btnRelease">释放占用</button>`;
    $("#btnRelease").onclick = async () => {
      try {
        await api(`/api/occupancy/${c.occupancy.id}`, "DELETE");
        toast(`已释放 ${s.name} NPU ${c.npu_id}`);
        closeOverlay(view);
        refresh();
      } catch (e) { toast(e.message, true); }
    };
  } else {
    view.hidden = true;
    form.hidden = false;
    form.reset();
    form.elements.start.value = toLocalInput();
    form.elements.user.focus();
  }
  form.onsubmit = async e => {
    e.preventDefault();
    const fd = new FormData(form);
    const startMs = new Date(fd.get("start")).getTime();
    try {
      await api("/api/occupancy", "POST", {
        server_id: sid, npu_id: npu,
        user: fd.get("user"), purpose: fd.get("purpose"),
        start_ts: isNaN(startMs) ? null : Math.floor(startMs / 1000),
      });
      toast(`已登记占用:${s.name} NPU ${c.npu_id}`);
      closeOverlay(form);
      refresh();
    } catch (err) { toast(err.message, true); }
  };
  openOverlay("#tileModal");
  if (!c.occupancy) form.elements.user.focus();
}

/* ---------------- 服务器弹窗 ---------------- */

function openServerModal(server) {
  const form = $("#serverForm");
  form.reset();
  const sel = $("#smModel");
  const names = state.models.map(m => m.name);
  const extra = server && server.model && !names.includes(server.model) ? [server.model] : [];
  sel.innerHTML = [...names, ...extra, "自定义…"]
    .map(n => `<option value="${esc(n)}">${esc(n)}</option>`).join("");
  if (server) {
    $("#smTitle").textContent = "编辑服务器";
    form.elements.name.value = server.name;
    form.elements.ip.value = server.ip;
    form.elements.ssh_port.value = server.ssh_port;
    sel.value = names.includes(server.model) || extra.includes(server.model)
      ? server.model : "自定义…";
    form.elements.expected_cards.value = server.expected_cards ?? "";
    form.elements.username.value = server.username;
    form.elements.password.value = server.password;
    form.elements.tags.value = (server.tags || []).join(", ");
    ["uboe", "roce", "ubg"].forEach(kind => { form.elements[kind].value = (server.network_groups || {})[kind] || ""; });
    form.elements.note.value = server.note || "";
    form.elements.collect_enabled.checked = server.collect_enabled;
    form.dataset.sid = server.id;
  } else {
    $("#smTitle").textContent = "添加服务器";
    sel.value = names[0] || "自定义…";
    form.elements.expected_cards.value = defaultCardsOf(sel.value);
    form.elements.collect_enabled.checked = true;
    delete form.dataset.sid;
  }
  if (!server) syncExpectedCards();
  openOverlay("#serverModal");
  form.elements.name.focus();
}

function defaultCardsOf(modelName) {
  const m = state.models.find(m => m.name === modelName);
  return m ? m.cards : 8;
}
function syncExpectedCards() {
  const sel = $("#smModel");
  const input = $("#serverForm").elements.expected_cards;
  if (sel.value !== "自定义…") input.value = defaultCardsOf(sel.value);
}

$("#serverForm").addEventListener("submit", async e => {
  e.preventDefault();
  const form = e.target;
  const fd = new FormData(form);
  const body = {
    name: fd.get("name"), ip: fd.get("ip"),
    ssh_port: fd.get("ssh_port") || 22,
    model: $("#smModel").value === "自定义…" ? "" : $("#smModel").value,
    expected_cards: fd.get("expected_cards") === "" ? null : +fd.get("expected_cards"),
    username: fd.get("username") || "root",
    password: fd.get("password") || "",
    tags: String(fd.get("tags") || "").split(",").map(t => t.trim()).filter(Boolean),
    note: fd.get("note") || "",
    network_groups: Object.fromEntries(["uboe", "roce", "ubg"].map(kind => [kind, String(fd.get(kind) || "").trim()]).filter(([,group]) => group)),
    collect_enabled: form.elements.collect_enabled.checked,
  };
  try {
    if (form.dataset.sid) {
      await api(`/api/servers/${form.dataset.sid}`, "PUT", body);
      toast("已保存更改");
    } else {
      await api("/api/servers", "POST", body);
      toast("已添加,等待首轮采集");
    }
    closeOverlay(form);
    refresh();
  } catch (err) { toast(err.message, true); }
});

/* ---------------- 设置弹窗 ---------------- */

function openSettingsModal() {
  const form = $("#settingsForm");
  const c = state.config;
  ["interval_seconds", "collect_workers", "ssh_connect_timeout", "ssh_exec_timeout",
   "aicore_threshold", "hbm_threshold_pct"].forEach(k => {
    if (c[k] != null && form.elements[k]) form.elements[k].value = c[k];
  });
  renderModelsEditor(state.models);
  openOverlay("#settingsModal");
}

function renderModelsEditor(models) {
  const box = $("#modelsEditor");
  box.innerHTML = "";
  const addRow = (name, cards) => {
    const row = document.createElement("div");
    row.className = "model-row";
    row.innerHTML = `
      <input class="m-name" placeholder="型号名,如 A5-960(950DT)" value="${esc(name || "")}">
      <input class="m-cards type" type="number" min="0" max="64" value="${cards ?? 8}" title="默认卡数">
      <button type="button" class="icon-btn danger" title="删除此型号">
        <svg viewBox="0 0 16 16"><path d="M4 4l8 8M12 4l-8 8" stroke="currentColor" stroke-width="1.7" stroke-linecap="round"/></svg>
      </button>`;
    row.querySelector(".icon-btn").onclick = () => row.remove();
    box.appendChild(row);
  };
  models.forEach(m => addRow(m.name, m.cards));
  const addBtn = document.createElement("button");
  addBtn.type = "button";
  addBtn.className = "btn ghost models-add";
  addBtn.textContent = "+ 添加型号";
  addBtn.onclick = () => addRow("", 8);
  box.appendChild(addBtn);
}

$("#settingsForm").addEventListener("submit", async e => {
  e.preventDefault();
  const form = e.target;
  const config = {};
  ["interval_seconds", "collect_workers", "ssh_connect_timeout",
   "ssh_exec_timeout", "aicore_threshold", "hbm_threshold_pct"]
    .forEach(k => { config[k] = String(form.elements[k].value); });
  const models = $$(".model-row", $("#modelsEditor")).map(row => ({
    name: $(".m-name", row).value.trim(),
    cards: +$(".m-cards", row).value || 0,
  })).filter(m => m.name);
  try {
    await api("/api/config", "PUT", { config, models });
    toast("设置已保存");
    closeOverlay(form);
    loadConfig();
    refresh();
  } catch (err) { toast(err.message, true); }
});

/* ---------------- 备份 ---------------- */

$("#importFile").addEventListener("change", async e => {
  const file = e.target.files[0];
  if (!file) return;
  e.target.disabled = true;
  try {
    const payload = new FormData();
    payload.append("file", file);
    const response = await fetch("/api/ledger/import", { method: "POST", body: payload });
    const res = await response.json();
    if (!response.ok) throw new Error(res.error || "导入失败");
    toast(`导入完成:新增 ${res.added} 台,更新 ${res.updated} 台`);
    closeOverlay($("#backupModal"));
    loadConfig();
    refresh();
  } catch (err) {
    toast(`导入失败:${err.message}`, true);
  } finally {
    e.target.value = "";
    e.target.disabled = false;
  }
});

/* ---------------- 看板事件(委托) ---------------- */

$("#board").addEventListener("click", async e => {
  const actBtn = e.target.closest("[data-act]");
  const card = e.target.closest(".server-card");
  if (!actBtn || !card) return;
  const sid = +card.dataset.sid;
  const s = state.data.servers.find(x => x.id === sid);
  if (!s) return;
  const act = actBtn.dataset.act;
  let startedCollect = false;
  try {
    if (act === "collect") {
      if (state.collecting.has(sid)) return;
      state.collecting.add(sid);
      startedCollect = true;
      actBtn.disabled = true;
      toast(`正在采集 ${s.name} …`);
      const res = await api(`/api/servers/${sid}/collect`, "POST");
      toast(`采集完成:${res.cards} 张卡${res.version ? " · npu-smi " + res.version : ""}`);
      refresh();
    } else if (act === "toggle") {
      const next = !s.collect_enabled;
      await api(`/api/servers/${sid}`, "PATCH", { collect_enabled: next });
      toast(next ? "已启用定时采集" : "已停用定时采集(保留最后一次数据)");
      refresh();
    } else if (act === "edit") {
      openServerModal(s);
    } else if (act === "delete") {
      if (await confirmBox(`删除服务器「${s.name}」?其卡数据与占用登记会一并删除。`, "删除")) {
        await api(`/api/servers/${sid}`, "DELETE");
        toast("已删除");
        refresh();
      }
    } else if (act === "eye") {
      state.revealed.has(sid) ? state.revealed.delete(sid) : state.revealed.add(sid);
      updatePassword(card, s);
    } else if (act === "copypw") {
      await copyText(s.password || "", s.password ? "密码已复制" : "该服务器未填密码");
    }
  } catch (err) {
    toast(err.message, true);
  } finally {
    if (startedCollect) {
      state.collecting.delete(sid);
      actBtn.disabled = false;
    }
  }
});

/* ---------------- 筛选 ---------------- */

function bindFilters() {
  $("#fSearch").addEventListener("input", e => { state.filters.q = e.target.value; renderBoard(); });
  $("#fModel").addEventListener("change", e => { state.filters.model = e.target.value; renderBoard(); });
  $("#fTag").addEventListener("change", e => { state.filters.tag = e.target.value; renderBoard(); });
  $("#fNetwork").addEventListener("change", e => { state.filters.network = e.target.value; renderBoard(); });
  $("#fState").addEventListener("change", e => {
    state.filters.st = e.target.value;
    renderBoard();
    renderStats();
  });
  $("#stats").addEventListener("click", e => {
    const stat = e.target.closest(".stat[data-st]");
    if (!stat) return;
    const st = stat.dataset.st;
    const sel = $("#fState");
    sel.value = sel.value === st ? "" : st;
    sel.dispatchEvent(new Event("change"));
  });
}

function buildModelOptions() {
  const sel = $("#fModel");
  const cur = sel.value;
  const models = new Set(state.data ? state.data.servers.map(s => s.model).filter(Boolean) : []);
  (state.models || []).forEach(m => models.add(m.name));
  sel.innerHTML = `<option value="">全部型号</option>` +
    [...models].map(m => `<option value="${esc(m)}">${esc(m)}</option>`).join("");
  if ([...models].includes(cur)) sel.value = cur;
  else state.filters.model = "";
}

function buildTagOptions(tags) {
  const sel = $("#fTag");
  const cur = sel.value;
  const all = new Set([...(tags || [])]);
  if (state.data) state.data.servers.forEach(s => (s.tags || []).forEach(t => all.add(t)));
  sel.innerHTML = `<option value="">全部组网</option>` +
    [...all].sort().map(t => `<option value="${esc(t)}">${esc(t)}</option>`).join("");
  if ([...all].includes(cur)) sel.value = cur;
  else state.filters.tag = "";
}

function buildNetworkOptions() {
  const sel = $("#fNetwork");
  const all = new Map();
  (state.data?.servers || []).forEach(s => Object.entries(s.network_groups || {}).forEach(([kind, group]) => {
    all.set(`${kind}:${group}`, `${({uboe: "UBoE", roce: "RoCE", ubg: "UBG"})[kind] || kind} / ${group}`);
  }));
  sel.innerHTML = `<option value="">全部互通组</option>` + [...all].sort((a,b) => a[1].localeCompare(b[1])).map(([key,label]) => `<option value="${esc(key)}">${esc(label)}</option>`).join("");
  if (all.has(state.filters.network)) sel.value = state.filters.network;
  else state.filters.network = "";
}

/* ---------------- 排序：全局保存，筛选时不丢失隐藏机器 ---------------- */

async function moveServer(sid, targetId, after = false) {
  if (state.sorting || sid === targetId) return;
  const source = state.data.servers.find(s => s.id === sid);
  const target = state.data.servers.find(s => s.id === targetId);
  if (!source || !target || serverFamily(source) !== serverFamily(target)) return;
  const ids = state.data.servers.map(s => s.id).filter(id => id !== sid);
  ids.splice(ids.indexOf(targetId) + (after ? 1 : 0), 0, sid);
  state.sorting = true;
  try {
    await api("/api/servers/order", "PUT", { ids });
    const byId = new Map(state.data.servers.map(s => [s.id, s]));
    state.data.servers = ids.map(id => byId.get(id));
    renderBoard();
    toast("机器顺序已保存");
  } catch (e) { toast(e.message, true); }
  finally { state.sorting = false; }
}

function bindSorting() {
  const board = $("#board");
  const clear = () => $$(".drop-before, .drop-after, .is-dragging", board).forEach(el => el.classList.remove("drop-before", "drop-after", "is-dragging"));
  let pointer = null;
  board.addEventListener("pointerdown", e => {
    const handle = e.target.closest(".drag-handle");
    if (!handle || state.sorting || e.button !== 0 || pointer) return;
    const card = handle.closest(".server-card");
    pointer = { id: e.pointerId, handle, card, x: e.clientX, y: e.clientY, target: null, after: false };
    handle.setPointerCapture(e.pointerId);
    handle.focus();
    e.preventDefault();
  });
  board.addEventListener("pointermove", e => {
    if (!pointer || e.pointerId !== pointer.id) return;
    if (state.dragging === null && Math.hypot(e.clientX - pointer.x, e.clientY - pointer.y) < 6) return;
    state.dragging = +pointer.card.dataset.sid;
    pointer.card.classList.add("is-dragging");
    e.preventDefault();
    $$(".drop-before, .drop-after", board).forEach(el => el.classList.remove("drop-before", "drop-after"));
    const card = document.elementFromPoint(e.clientX, e.clientY)?.closest(".server-card");
    pointer.target = null;
    if (!card || card === pointer.card || pointer.card.closest(".machine-section") !== card.closest(".machine-section")) return;
    const box = card.getBoundingClientRect();
    pointer.after = e.clientX > box.left + box.width / 2;
    pointer.target = +card.dataset.sid;
    card.classList.add(pointer.after ? "drop-after" : "drop-before");
  });
  const finish = (e, cancelled = false) => {
    if (!pointer || e.pointerId !== pointer.id) return;
    const { handle, target, after, id } = pointer;
    const sid = state.dragging;
    pointer = null;
    state.dragging = null;
    if (handle.hasPointerCapture(id)) handle.releasePointerCapture(id);
    clear();
    if (!cancelled && sid !== null && target !== null) moveServer(sid, target, after);
  };
  board.addEventListener("pointerup", e => finish(e));
  board.addEventListener("pointercancel", e => finish(e, true));
  board.addEventListener("lostpointercapture", e => finish(e, true));
  board.addEventListener("keydown", async e => {
    const handle = e.target.closest(".drag-handle");
    if (!handle || !["ArrowUp", "ArrowLeft", "ArrowDown", "ArrowRight"].includes(e.key)) return;
    e.preventDefault();
    const sid = +handle.closest(".server-card").dataset.sid;
    const cards = $$(".server-card", handle.closest(".machine-section")).filter(el => el.style.display !== "none");
    const next = ["ArrowDown", "ArrowRight"].includes(e.key);
    const neighbor = cards[cards.findIndex(el => +el.dataset.sid === sid) + (next ? 1 : -1)];
    if (neighbor) {
      await moveServer(sid, +neighbor.dataset.sid, next);
      $(`.server-card[data-sid="${sid}"] .drag-handle`, board)?.focus();
    }
  });
}

/* ---------------- 轮询 ---------------- */

function startTicker() {
  setInterval(() => {
    if (!document.hidden) refresh();
  }, REFRESH_SECS * 1000);
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden && Date.now() - state.lastRefresh >= REFRESH_SECS * 1000) refresh();
  });
}

/* ---------------- 全局按钮 / 弹窗关闭 ---------------- */

function bindGlobal() {
  $("#btnAddServer").addEventListener("click", () => openServerModal(null));
  $("#btnSettings").addEventListener("click", openSettingsModal);
  $("#btnRefresh").addEventListener("click", refresh);
  $("#btnBackup").addEventListener("click", () => openOverlay("#backupModal"));
  $("#btnCollect").addEventListener("click", async () => {
    const button = $("#btnCollect");
    button.disabled = true;
    try {
      await api("/api/collect", "POST");
      toast("已触发全量采集，完成后可点击刷新看板");
      setTimeout(refresh, 10000);
      setTimeout(refresh, 30000);
    } catch (e) { toast(e.message, true); }
    finally { setTimeout(() => { button.disabled = false; }, 30000); }
  });
  $("#smModel").addEventListener("change", syncExpectedCards);

  document.addEventListener("click", e => {
    if (e.target.closest("[data-close]")) closeOverlay(e.target);
  });
  document.addEventListener("keydown", e => {
    if (e.key === "Escape") {
      const opened = $$(".overlay").filter(o => !o.hidden);
      if (opened.length) opened[opened.length - 1].hidden = true;
      hideTooltip();
    }
  });
  $$(".overlay").forEach(o => {
    o.addEventListener("mousedown", e => { if (e.target === o) o.hidden = true; });
  });
}

/* ---------------- 入口 ---------------- */

function render() {
  buildNetworkOptions();
  buildModelOptions();
  buildTagOptions();
  renderStats();
  renderBoard();
}

async function init() {
  bindFilters();
  bindGlobal();
  bindSorting();
  await loadConfig();
  await refresh();
  startTicker();
}

init();
