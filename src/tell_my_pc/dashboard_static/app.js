"use strict";
/* tell-my-pc dashboard. Plain JS, no build step. Every API call carries the local key. */

const KEY_STORE = "tmp-dashboard-key";
let KEY = null;

const S = {
  overview: null,
  events: [],          // newest last, capped
  lastId: 0,
  older: [],           // paged in from "Show older", oldest first
  tasks: null,
  approvals: null,
  files: null,
  config: null,
  task: null,          // current task detail
  route: { name: "overview", arg: null },
  filter: "all",
  query: "",
  open: new Set(),     // expanded activity rows
};
const MAX_EVENTS = 3000;

/* ------------------------------------------------------------------ helpers */

const $ = (sel, el = document) => el.querySelector(sel);
const ESC = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" };
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ESC[c]);
const pad = (n) => String(n).padStart(2, "0");

function fmtTime(ts) {
  const d = new Date(ts * 1000);
  return `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
}
function fmtClock(ts) {
  const d = new Date(ts * 1000);
  return `${pad(d.getHours())}:${pad(d.getMinutes())}`;
}
function fmtDay(ts) {
  return new Date(ts * 1000).toLocaleDateString(undefined, { weekday: "short", day: "numeric", month: "short" });
}
function fmtDur(s) {
  s = Math.max(0, Math.round(s));
  if (s < 60) return `${s} s`;
  if (s < 3600) return `${Math.floor(s / 60)} min ${pad(s % 60)} s`;
  const h = Math.floor(s / 3600);
  return h < 48 ? `${h} h ${pad(Math.floor((s % 3600) / 60))} min` : `${Math.floor(h / 24)} days`;
}
function ago(ts) {
  const s = Date.now() / 1000 - ts;
  if (s < 45) return "just now";
  if (s < 3600) return `${Math.round(s / 60)} min ago`;
  if (s < 86400) return `${Math.round(s / 3600)} h ago`;
  return fmtDay(ts);
}
function fmtSize(n) {
  if (n < 1024) return `${n} B`;
  const units = ["KB", "MB", "GB"];
  let v = n / 1024, i = 0;
  while (v >= 1024 && i < 2) { v /= 1024; i++; }
  return `${v.toFixed(1)} ${units[i]}`;
}
const plural = (n, word) => `${n} ${word}${n === 1 ? "" : "s"}`;

/* lucide icons, 16px, 1.5 stroke */
const ICON = {
  overview: '<rect width="7" height="7" x="3" y="3" rx="1"/><rect width="7" height="7" x="14" y="3" rx="1"/><rect width="7" height="7" x="14" y="14" rx="1"/><rect width="7" height="7" x="3" y="14" rx="1"/>',
  activity: '<path d="M22 12h-4l-3 9L9 3l-3 9H2"/>',
  tasks: '<path d="M3 12h.01"/><path d="M3 18h.01"/><path d="M3 6h.01"/><path d="M8 12h13"/><path d="M8 18h13"/><path d="M8 6h13"/>',
  approvals: '<path d="M20 13c0 5-3.5 7.5-7.66 8.95a1 1 0 0 1-.67-.01C7.5 20.5 4 18 4 13V6a1 1 0 0 1 1-1c2 0 4.5-1.2 6.24-2.72a1.17 1.17 0 0 1 1.52 0C14.51 3.81 17 5 19 5a1 1 0 0 1 1 1z"/><path d="m9 12 2 2 4-4"/>',
  files: '<path d="M15 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V7Z"/><path d="M14 2v4a2 2 0 0 0 2 2h4"/>',
  settings: '<path d="M21 4h-7"/><path d="M10 4H3"/><path d="M21 12h-9"/><path d="M8 12H3"/><path d="M21 20h-5"/><path d="M12 20H3"/><path d="M14 2v4"/><path d="M8 10v4"/><path d="M16 18v4"/>',
  in: '<path d="M17 7 7 17"/><path d="M17 17H7V7"/>',
  out: '<path d="M7 7h10v10"/><path d="M7 17 17 7"/>',
  lock: '<rect width="18" height="11" x="3" y="11" rx="2"/><path d="M7 11V7a5 5 0 0 1 10 0v4"/>',
  unlock: '<rect width="18" height="11" x="3" y="11" rx="2"/><path d="M7 11V7a5 5 0 0 1 9.9-1"/>',
  stop: '<rect width="14" height="14" x="5" y="5" rx="2"/>',
  search: '<circle cx="11" cy="11" r="8"/><path d="m21 21-4.3-4.3"/>',
  back: '<path d="m15 18-6-6 6-6"/>',
  check: '<path d="M20 6 9 17l-5-5"/>',
  x: '<path d="M18 6 6 18"/><path d="m6 6 12 12"/>',
};
const icon = (name, size = 16) =>
  `<svg width="${size}" height="${size}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${ICON[name]}</svg>`;

/* the mark: a phone on the left, the line, the computer on the right */
const MARK = '<svg width="20" height="20" viewBox="0 0 20 20" aria-hidden="true"><rect x="1.5" y="5" width="5" height="10" rx="1.5" fill="none" stroke="currentColor" stroke-width="1.5"/><path d="M6.5 10h5" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/><rect x="11.5" y="4" width="7" height="9" rx="1.5" fill="currentColor"/><path d="M13.5 16h3" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/></svg>';

const KIND = {
  request: "Phone request",
  task_received: "Task queued",
  task_finished: "Task finished",
  notification_sent: "Sent to phone",
  notify_failed: "Send failed",
  tool_allowed: "Tool call",
  tool_denied: "Tool call",
  approval_requested: "Approval asked",
  approval_answer: "Approval answer",
  file_queued: "File queued",
  file_sent: "File sent",
  file_downloaded: "File downloaded",
  file_link_refused: "Link refused",
  auth_failed: "Refused request",
  locked: "Locked",
  unlocked: "Unlocked",
  project_asked: "Asked which project",
  project_answered: "Project answered",
  task_held: "Waiting for limit",
  task_released: "Released",
  task_stop_requested: "Stop requested",
  fallback_used: "Fallback model",
  inbox_rejected: "Inbox refused",
};
const kindLabel = (k) => KIND[k] || k.replace(/_/g, " ").replace(/^./, (c) => c.toUpperCase());
const vmark = (v) => (v ? `<span class="vm ${esc(v)}" title="${esc(VERDICT[v] || v)}"></span>` : "");
const VERDICT = {
  allow: "Allowed by the policy",
  asked: "Asked you, you approved",
  deny: "Denied by the policy",
  "asked-deny": "Asked you, denied or no answer",
};

/* ------------------------------------------------------------------ API */

async function api(path, opts = {}) {
  const headers = { "X-TMP-Key": KEY || "" };
  if (opts.body) headers["Content-Type"] = "application/json";
  const r = await fetch(path, {
    method: opts.method || "GET",
    headers,
    body: opts.body ? JSON.stringify(opts.body) : undefined,
    cache: "no-store",
  });
  if (r.status === 401) {
    keyless();
    throw new Error("no key");
  }
  const data = await r.json().catch(() => ({}));
  return { ok: r.ok, status: r.status, data };
}

function readKey() {
  const m = location.hash.match(/key=([A-Za-z0-9_-]{32,})/);
  if (m) {
    KEY = m[1];
    try { sessionStorage.setItem(KEY_STORE, KEY); } catch { /* private window */ }
    history.replaceState(null, "", location.pathname + "#/overview");
    return;
  }
  try { KEY = sessionStorage.getItem(KEY_STORE); } catch { KEY = null; }
}

function keyless() {
  $("#app").innerHTML = `<div class="keyless enter">
    <h1>Open the dashboard from the computer</h1>
    <p>For your safety this page needs the private link that only the computer can make. Run this in a terminal:</p>
    <p><span class="kbd">tell-my-pc dashboard</span></p>
    <p class="muted">The link only works on this computer, in this browser tab.</p></div>`;
}

/* ------------------------------------------------------------------ data */

async function loadEvents() {
  const { data } = await api(`/api/events?after=${S.lastId}&limit=${S.lastId ? 1000 : 400}`);
  const fresh = data.events || [];
  if (!fresh.length) return false;
  S.events.push(...fresh);
  if (S.events.length > MAX_EVENTS) S.events.splice(0, S.events.length - MAX_EVENTS);
  S.lastId = fresh[fresh.length - 1].id;
  return true;
}
async function loadOverview() {
  const { data } = await api("/api/overview");
  S.overview = data;
}
const loaders = {
  tasks: async () => { S.tasks = (await api("/api/tasks?limit=200")).data.tasks || []; },
  approvals: async () => { S.approvals = (await api("/api/approvals")).data; },
  files: async () => { S.files = (await api("/api/files")).data; },
  settings: async () => { S.config = (await api("/api/config")).data; },
  task: async (id) => {
    const r = await api(`/api/tasks/${encodeURIComponent(id)}`);
    S.task = r.ok ? r.data : { error: r.data.error || "Not found" };
  },
};

async function act(path, body, confirmText) {
  if (confirmText && !window.confirm(confirmText)) return null;
  const r = await api(path, { method: "POST", body: body || {} });
  await Promise.all([loadOverview(), loadEvents()]);
  if (S.route.name !== "overview" && loaders[S.route.name]) await loaders[S.route.name](S.route.arg);
  render();
  if (!r.ok && r.data.error) window.alert(r.data.error);
  return r;
}

/* ------------------------------------------------------------------ shell */

const NAV = [
  ["overview", "Overview"],
  ["activity", "Activity"],
  ["tasks", "Tasks"],
  ["approvals", "Approvals"],
  ["files", "Files"],
  ["settings", "Settings"],
];

function parseRoute() {
  const h = location.hash.replace(/^#\/?/, "");
  const [name, arg] = h.split("/");
  if (name === "tasks" && arg) return { name: "task", arg };
  return { name: NAV.some(([n]) => n === name) ? name : "overview", arg: null };
}

function shell() {
  const ov = S.overview || {};
  const pending = (ov.pending_approvals || []).length;
  const locked = ov.locked;
  const running = (ov.running || []).length;
  let lamp = "", state = "Listening";
  if (locked) { lamp = "locked"; state = "Locked"; }
  else if (pending) { lamp = "needs"; state = `${plural(pending, "approval")} waiting`; }
  else if (running) { state = "Working on a task"; }
  const active = S.route.name === "task" ? "tasks" : S.route.name;
  const nav = NAV.map(([n, label]) => {
    let count = "";
    if (n === "approvals" && pending) count = `<span class="count mono needs">${pending}</span>`;
    if (n === "tasks" && running) count = `<span class="count mono">${running} running</span>`;
    return `<a href="#/${n}" class="${n === active ? "on" : ""}">${icon(n)}<span>${label}</span>${count}</a>`;
  }).join("");
  return `<div class="shell">
    <aside class="side">
      <div class="brand">${MARK}<b>tell-my-pc</b></div>
      <div class="line-status">
        <div class="row"><span class="lamp ${lamp}"></span><span>${esc(state)}</span></div>
        <div class="where mono muted">${esc(ov.listening || "")}</div>
      </div>
      <div class="nav-label mono muted">Monitor</div>
      <nav class="nav">${nav}</nav>
      <div class="side-foot">
        <div class="mono muted">v${esc(ov.version || "")}${ov.started_at ? ` · up ${esc(fmtDur(ov.now - ov.started_at))}` : ""}</div>
      </div>
    </aside>
    <main class="main">
      <header class="top">
        <div class="crumb">${icon(active)}<b>${esc(NAV.find(([n]) => n === active)?.[1] || "")}</b></div>
        <div class="right">${locked
          ? `<button class="btn" data-act="unlock">${icon("unlock")}Unlock</button>`
          : `<button class="btn" data-act="lock">${icon("lock")}Lock</button>`}</div>
      </header>
      <div class="page" id="page"></div>
    </main>
  </div>`;
}

function lockedBanner() {
  const l = S.overview && S.overview.locked;
  if (!l) return "";
  return `<div class="banner">${icon("lock")}<div><b>Locked</b><span class="sec">${l.reason ? ` · ${esc(l.reason)}` : ""}. New tasks and every tool call are refused.</span></div>
    <button class="btn right" data-act="unlock">${icon("unlock")}Unlock</button></div>`;
}

/* ------------------------------------------------------------------ overview */

function pageOverview() {
  const ov = S.overview;
  if (!ov) return `<div class="empty">Loading</div>`;
  const d = ov.day;
  const t = d.tools;
  const now = new Date(ov.now * 1000);
  const title = `${now.toLocaleDateString(undefined, { weekday: "long" })} <span>${now.toLocaleDateString(undefined, { day: "numeric", month: "long" })}</span>`;
  const sub = (n, word, cls) => (n ? `<span class="${cls}">${plural(n, word)}</span>` : `no ${word}s`);
  const ins = ov.wire.filter((w) => w.dir === "in").length;
  const outs = ov.wire.filter((w) => w.dir === "out").length;
  return `${lockedBanner()}
  <h1 class="page-title">${title}</h1>
  <section class="stats">
    <div class="card stat enter"><div class="label">Tasks</div><div class="fig">${d.tasks}</div>
      <div class="sub mono">${d.done} done · ${sub(d.failed, "failure", "bad")}</div></div>
    <div class="card stat enter"><div class="label">From your phone</div><div class="fig">${d.received}</div>
      <div class="sub mono">${d.auth_failed ? `<span class="bad">${plural(d.auth_failed, "refused request")}</span>` : "all signed in"}</div></div>
    <div class="card stat enter"><div class="label">To your phone</div><div class="fig">${d.sent}</div>
      <div class="sub mono">${d.send_failed ? `<span class="bad">${d.send_failed} failed to send</span>` : "none failed"}</div></div>
    <div class="card stat enter"><div class="label">Tool calls</div><div class="fig">${t.allow + t.asked + t.deny}</div>
      <div class="sub mono"><span title="${VERDICT.allow}">${vmark("allow")} ${t.allow}</span>&nbsp;&nbsp;<span title="Asked you">${vmark("asked")} ${t.asked}</span>&nbsp;&nbsp;<span title="${VERDICT.deny}">${vmark("deny")} ${t.deny}</span></div></div>
  </section>
  <section class="card wire-card enter">
    <div class="card-head"><h2>The line, last 24 hours</h2>
      <div class="wire-meta mono muted">
        <span class="k"><i class="key-in"></i>from your phone</span>
        <span class="k"><i class="key-out"></i>to your phone</span>
        <span class="k"><i class="key-need"></i>needed you</span>
      </div>
      <div class="right mono muted">${ins} in · ${outs} out</div></div>
    <div class="wire" id="wire"><div class="tip" id="tip"></div></div>
  </section>
  <section class="grid-2">
    <div class="card enter">${nowCard(ov)}</div>
    <div class="card enter">${needsCard(ov)}</div>
  </section>
  <section class="card enter" style="margin-top:12px">
    <div class="card-head"><h2>Recent tasks</h2><a class="right muted" href="#/tasks">All tasks</a></div>
    <div class="rows">${taskRows((S.tasks || []).slice(0, 6))}</div>
  </section>`;
}

function nowCard(ov) {
  const run = (ov.running || [])[0];
  if (!run) {
    const last = (S.tasks || []).find((x) => !["queued", "running", "held"].includes(x.status));
    const queued = ov.queued ? ` ${plural(ov.queued, "task")} queued.` : "";
    const held = ov.held ? ` ${plural(ov.held, "task")} waiting for Claude's limit to reset.` : "";
    return `<div class="card-head"><h2>Now</h2><span class="right status">idle</span></div>
      <div class="sec">Nothing running.${queued}${held}</div>
      ${last ? `<a class="row-item task-row" style="margin-top:12px" href="#/tasks/${esc(last.id)}">
        <span class="mono muted">${esc(last.id)}</span>
        <span class="ask">${esc(last.text)}<small>${esc(last.summary)}</small></span>
        <span class="sec">${esc(last.project)}</span><span class="status ${esc(last.status)}">${esc(last.status)}</span>
        <span class="mono muted">${esc(ago(last.updated_at))}</span></a>` : ""}`;
  }
  const tape = S.events.filter((e) => e.task === run.id && (e.dir === "tool" || e.dir === "out")).slice(-10).reverse();
  return `<div class="card-head"><h2>Now</h2>
      <span class="mono muted">${esc(run.id)} · ${esc(run.project)} · ${esc(fmtDur(ov.now - run.created_at))}</span>
      <button class="btn right" data-act="stop" data-id="${esc(run.id)}">${icon("stop")}Stop</button></div>
    <a href="#/tasks/${esc(run.id)}" style="text-decoration:none"><div class="summary">“${esc(run.text)}”</div></a>
    <div class="tape" style="margin-top:12px">${tapeRows(tape) || `<div class="empty">Starting</div>`}</div>`;
}

function needsCard(ov) {
  const list = ov.pending_approvals || [];
  if (!list.length) {
    return `<div class="card-head"><h2>Needs you</h2></div>
      <div class="sec">Nothing waiting. When a task wants to do something outside its tier, it shows up here and on your phone.</div>`;
  }
  return `<div class="card-head"><h2>Needs you</h2><span class="right mono" style="color:var(--accent-ink)">${list.length}</span></div>
    ${list.map((a) => approvalCard(a, ov.now)).join("")}`;
}

function approvalCard(a, now) {
  return `<div class="need" style="margin-top:8px">
    <div class="need-head"><span class="lamp needs"></span><b style="font-weight:500">${esc(a.tool)}</b>
      <a class="mono muted" href="#/tasks/${esc(a.task_id)}" style="margin-left:auto;text-decoration:none">${esc(a.task_id)}</a></div>
    <div class="cmd">${esc(a.detail)}</div>
    <div class="why">${esc(a.reason)}</div>
    <div class="acts">
      <button class="btn primary" data-act="approve" data-id="${esc(a.id)}">${icon("check")}Approve</button>
      <button class="btn" data-act="deny" data-id="${esc(a.id)}">${icon("x")}Deny</button>
      <span class="left mono muted">counts as Deny in ${esc(fmtDur(a.expires_at - now))}</span>
    </div></div>`;
}

function tapeRows(evs) {
  return evs.map((e) => {
    if (e.dir === "out") {
      return `<div class="row-item"><span class="mono muted">${fmtTime(e.ts)}</span>${icon("out", 12)}
        <span class="line sec">${esc(e.text)}</span></div>`;
    }
    const det = e.detail || {};
    return `<div class="row-item"><span class="mono muted">${fmtTime(e.ts)}</span>${vmark(e.verdict)}
      <span><span class="line"><span class="tool">${esc(e.tool || "")}</span>&nbsp; ${esc(e.brief || "")}</span>
      ${e.verdict !== "allow" ? `<div class="why">${esc(det.reason || "")}</div>` : ""}</span></div>`;
  }).join("");
}

/* ------------------------------------------------------------------ the wire */

const WIRE_H = 300, MID = 150;
function markHeight(w) {
  if (w.dir === "in") {
    if (w.kind === "request") return /\/(task|continue)/.test(w.text) ? 104 : 52;
    if (w.kind === "approval_answer") return 72;
    if (w.kind === "auth_failed") return 44;
    return 40;
  }
  if (w.kind === "notify_failed") return 44;
  return { report: 104, approval: 88, file: 68, question: 68 }[w.notekind] || 44;
}
function markClass(w) {
  if (w.kind === "auth_failed" || w.kind === "notify_failed") return "bad";
  if (w.notekind === "approval" || w.kind === "approval_answer") return "need";
  return w.dir;
}

function drawWire() {
  const host = $("#wire");
  const ov = S.overview;
  if (!host || !ov) return;
  const W = Math.max(320, host.clientWidth);
  const start = ov.now - 86400;
  const x = (ts) => ((ts - start) / 86400) * W;
  const css = getComputedStyle(document.documentElement);
  const c = (v) => css.getPropertyValue(v).trim();
  const color = { in: c("--mark-in"), out: c("--mark-out"), need: c("--accent"), bad: c("--bad") };
  let svg = "";
  // hour grid, every 3 hours
  const first = new Date(start * 1000);
  first.setMinutes(0, 0, 0);
  for (let h = first.getTime() / 1000 + 3600; h < ov.now; h += 3600) {
    const hr = new Date(h * 1000).getHours();
    if (hr % 3) continue;
    const xx = x(h).toFixed(1);
    svg += `<line x1="${xx}" x2="${xx}" y1="20" y2="${WIRE_H - 20}" stroke="${c("--line")}" />`;
    svg += `<text x="${xx}" y="${WIRE_H - 2}" text-anchor="middle" font-family="${c("--mono")}" font-size="11" fill="${c("--muted")}">${pad(hr)}:00</text>`;
  }
  // the line, thicker where the computer worked
  svg += `<line x1="0" x2="${W}" y1="${MID}" y2="${MID}" stroke="${c("--line-2")}" />`;
  for (const s of ov.spans || []) {
    const a = Math.max(0, x(s.start)), b = Math.max(a + 3, x(s.end));
    const fill = s.status === "running" ? c("--text") : c("--span");
    svg += `<rect x="${a.toFixed(1)}" y="${MID - 3}" width="${(b - a).toFixed(1)}" height="6" rx="3" fill="${fill}"><title>${esc(s.id)}</title></rect>`;
  }
  // traffic: above from the phone, below to the phone
  for (const w of ov.wire) {
    const xx = x(w.ts).toFixed(1);
    const h = markHeight(w);
    const [y1, y2] = w.dir === "in" ? [MID - 6, MID - 6 - h] : [MID + 6, MID + 6 + h];
    svg += `<line x1="${xx}" x2="${xx}" y1="${y1}" y2="${y2}" stroke="${color[markClass(w)]}" stroke-width="2" stroke-linecap="round" />`;
  }
  // now, with the operator's lamp when something waits for you
  svg += `<line x1="${W}" x2="${W}" y1="20" y2="${WIRE_H - 20}" stroke="${c("--line-2")}" stroke-dasharray="2 4" />`;
  if ((ov.pending_approvals || []).length) {
    svg += `<circle cx="${W}" cy="${MID}" r="5" fill="${c("--accent")}" />`;
  }
  const tip = $("#tip");
  host.querySelector("svg")?.remove();
  host.insertAdjacentHTML("afterbegin", `<svg viewBox="0 0 ${W} ${WIRE_H}" role="img" aria-label="Messages from and to the phone over the last 24 hours">${svg}</svg>`);
  const el = host.querySelector("svg");
  el.addEventListener("mousemove", (ev) => {
    const r = el.getBoundingClientRect();
    const mx = ((ev.clientX - r.left) / r.width) * W;
    const my = ((ev.clientY - r.top) / r.height) * WIRE_H;
    const dir = my < MID ? "in" : "out";
    let best = null, bd = 9;
    for (const w of ov.wire) {
      if (w.dir !== dir) continue;
      const dd = Math.abs(x(w.ts) - mx);
      if (dd < bd) { bd = dd; best = w; }
    }
    if (!best) { tip.classList.remove("show"); return; }
    tip.innerHTML = `<div class="t mono">${fmtTime(best.ts)} · ${esc(kindLabel(best.kind))}${best.task ? ` · ${esc(best.task)}` : ""}</div><div class="what">${esc(best.text)}</div>`;
    const left = Math.min(r.width - 340, Math.max(0, (x(best.ts) / W) * r.width - 20));
    tip.style.left = `${left}px`;
    tip.style.top = dir === "in" ? `${(MID / WIRE_H) * r.height + 12}px` : "8px";
    tip.classList.add("show");
  });
  el.addEventListener("mouseenter", () => { S.hover = true; });
  el.addEventListener("mouseleave", () => { S.hover = false; tip.classList.remove("show"); });
}

/* ------------------------------------------------------------------ activity */

const FILTERS = [
  ["all", "All"],
  ["in", "From your phone"],
  ["out", "To your phone"],
  ["tool", "Tool calls"],
  ["system", "Tasks and system"],
];

function pageActivity() {
  return `${lockedBanner()}
  <div class="toolbar">
    <div class="chips" id="chips">${FILTERS.map(([k, l]) => `<button class="chip ${S.filter === k ? "on" : ""}" data-filter="${k}">${l}</button>`).join("")}</div>
    <div class="search-wrap right">${icon("search")}<input class="search" id="q" placeholder="Search" value="${esc(S.query)}" aria-label="Search events"></div>
  </div>
  <section class="card"><div class="rows" id="evrows"></div>
    <div style="padding-top:12px"><button class="btn" data-act="older">Show older</button></div></section>`;
}

function eventRows() {
  const q = S.query.trim().toLowerCase();
  const all = [...S.older, ...S.events];
  const rows = [];
  let day = "";
  for (let i = all.length - 1; i >= 0 && rows.length < 400; i--) {
    const e = all[i];
    if (S.filter !== "all" && e.dir !== S.filter) continue;
    if (q && !`${e.text} ${e.kind} ${e.task || ""}`.toLowerCase().includes(q)) continue;
    const d = fmtDay(e.ts);
    if (d !== day) {
      day = d;
      rows.push(`<div class="day-split mono muted">${esc(d)}</div>`);
    }
    const dirCell = e.dir === "in" || e.dir === "out"
      ? `<span class="dir ${e.dir}" title="${e.dir === "in" ? "From your phone" : "To your phone"}">${icon(e.dir)}</span>`
      : `<span class="dir">${e.verdict ? vmark(e.verdict) : ""}</span>`;
    const open = S.open.has(e.id);
    const detail = open
      ? `<div style="grid-column:1/-1;padding:4px 0 8px 104px"><pre class="config">${esc(JSON.stringify(e.detail, null, 2))}</pre></div>`
      : "";
    rows.push(`<div class="row-item ev" data-ev="${e.id}" style="cursor:pointer">
      <span class="mono muted" title="${esc(new Date(e.ts * 1000).toLocaleString())}">${fmtTime(e.ts)}</span>${dirCell}
      <span class="kind sec">${esc(kindLabel(e.kind))}</span>
      <span class="what ${open ? "wrap" : ""}">${esc(e.text)}</span>
      <span class="task mono">${e.task ? `<a href="#/tasks/${esc(e.task)}">${esc(e.task)}</a>` : ""}</span>${detail}</div>`);
  }
  return rows.join("") || `<div class="empty">Nothing matches.</div>`;
}

/* ------------------------------------------------------------------ tasks */

function taskRows(list) {
  if (!list.length) return `<div class="empty">No tasks yet. Say “Hey Siri, Tell Claude” and dictate one.</div>`;
  return list.map((t) => `<a class="row-item task-row" href="#/tasks/${esc(t.id)}">
    <span class="id mono muted">${esc(t.id)}</span>
    <span class="ask">${esc(t.text)}<small>${esc(t.summary || "")}</small></span>
    <span class="proj sec">${esc(t.project)}</span>
    <span class="status ${esc(t.status)}">${esc(t.status)}</span>
    <span class="when mono muted">${esc(ago(t.created_at))}</span></a>`).join("");
}

function pageTasks() {
  return `${lockedBanner()}<h1 class="page-title">Tasks <span>${S.tasks ? S.tasks.length : ""}</span></h1>
    <section class="card"><div class="rows">${S.tasks ? taskRows(S.tasks) : `<div class="empty">Loading</div>`}</div></section>`;
}

function pageTask() {
  const d = S.task;
  if (!d) return `<div class="empty">Loading</div>`;
  if (d.error) return `<a href="#/tasks" class="muted">${icon("back", 14)} Tasks</a><div class="empty">${esc(d.error)}</div>`;
  const t = d.task, r = d.result || {};
  const running = ["running", "queued", "held"].includes(t.status);
  const end = running ? Date.now() / 1000 : t.updated_at;
  const files = r.files_changed || [];
  const tools = d.events.filter((e) => e.dir === "tool" || e.dir === "out" || e.dir === "in");
  const list = (items, map) => (items && items.length ? `<ul class="list">${items.map((x) => `<li>${esc(map ? map(x) : x)}</li>`).join("")}</ul>` : `<div class="muted">None</div>`);
  return `${lockedBanner()}
  <a href="#/tasks" class="muted" style="display:inline-flex;align-items:center;gap:4px;text-decoration:none;margin-bottom:16px">${icon("back", 14)}Tasks</a>
  <h1 class="ask-quote">“${esc(t.text)}”</h1>
  <div class="meta mono">
    <span class="status ${esc(t.status)}">${esc(t.status)}</span>
    <span>${esc(t.id)}</span><span>${esc(t.project)}</span><span>${esc(t.tier)} tier</span>
    <span>${esc(new Date(t.created_at * 1000).toLocaleString())}</span><span>${esc(fmtDur(end - t.created_at))}</span>
    ${t.branch ? `<span>${esc(t.branch)}</span>` : ""}
    ${running ? `<button class="btn" data-act="stop" data-id="${esc(t.id)}">${icon("stop")}Stop</button>` : ""}
  </div>
  <section class="grid-2" style="margin-top:0">
    <div class="card">
      <div class="card-head"><h2>Report</h2></div>
      <div class="summary">${esc(r.summary || (running ? "Still working." : t.error || "No summary."))}</div>
      <div class="card-head" style="margin:20px 0 8px"><h2>Done</h2></div>${list(r.done)}
      <div class="card-head" style="margin:20px 0 8px"><h2>Not done</h2></div>${list(r.not_done, (x) => `${x.item}: ${x.reason}`)}
      <div class="card-head" style="margin:20px 0 8px"><h2>Needs from you</h2></div>${list(r.needs_from_you)}
    </div>
    <div class="card">
      <div class="card-head"><h2>Files changed</h2><span class="right mono muted">${r.target === "computer" ? "folder snapshot" : "git diff"}</span></div>
      ${files.length ? `<div class="rows">${files.map((f) => `<div class="row-item" style="grid-template-columns:20px minmax(0,1fr)"><span class="mono muted">${esc(f.status)}</span><span class="mono" style="overflow-wrap:anywhere">${esc(f.path)}</span></div>`).join("")}</div>` : `<div class="muted">None</div>`}
    </div>
  </section>
  <section class="card" style="margin-top:12px">
    <div class="card-head"><h2>Everything this task did</h2><span class="right mono muted">${tools.length} events</span></div>
    <div class="tape">${tools.length ? tapeRows(tools) : `<div class="empty">No tool calls.</div>`}</div>
  </section>
  ${d.report ? `<section class="card" style="margin-top:12px"><div class="report">${md(d.report)}</div></section>` : ""}`;
}

/* tiny markdown for reports: headings, lists, tables, code */
function md(src) {
  const inline = (s) => esc(s).replace(/`([^`]+)`/g, "<code>$1</code>").replace(/\*\*([^*]+)\*\*/g, "<b>$1</b>");
  const out = [];
  let code = null, list = false, table = null;
  const flush = () => {
    if (list) { out.push("</ul>"); list = false; }
    if (table) { out.push(`<table>${table.join("")}</table>`); table = null; }
  };
  for (const line of src.split(/\r?\n/)) {
    if (line.startsWith("```")) {
      if (code) { out.push(`<pre>${esc(code.join("\n"))}</pre>`); code = null; } else { flush(); code = []; }
      continue;
    }
    if (code) { code.push(line); continue; }
    if (/^\|/.test(line)) {
      if (/^\|[\s|-]+\|$/.test(line)) continue;
      if (!table) { flush(); table = []; }
      const cells = line.split("|").slice(1, -1).map((c) => `<td>${inline(c.trim())}</td>`).join("");
      table.push(`<tr>${cells}</tr>`);
      continue;
    }
    const m = line.match(/^(#{1,3})\s+(.*)/);
    if (m) { flush(); out.push(`<h${m[1].length === 1 ? 1 : 2}>${inline(m[2])}</h${m[1].length === 1 ? 1 : 2}>`); continue; }
    if (/^\s*-\s+/.test(line)) {
      if (table) flush();
      if (!list) { out.push("<ul>"); list = true; }
      out.push(`<li>${inline(line.replace(/^\s*-\s+/, ""))}</li>`);
      continue;
    }
    flush();
    if (line.trim()) out.push(`<p>${inline(line)}</p>`);
  }
  flush();
  if (code) out.push(`<pre>${esc(code.join("\n"))}</pre>`);
  return out.join("");
}

/* ------------------------------------------------------------------ approvals, files, settings */

function pageApprovals() {
  const a = S.approvals;
  if (!a) return `<div class="empty">Loading</div>`;
  const pending = a.approvals.filter((x) => x.status === "pending" && x.expires_at > a.now);
  const done = a.approvals.filter((x) => !pending.includes(x));
  return `${lockedBanner()}<h1 class="page-title">Approvals</h1>
  <div class="grid-2" style="margin-top:0">
    <section class="card"><div class="card-head"><h2>Waiting</h2></div>
      ${pending.length ? pending.map((x) => approvalCard(x, a.now)).join("") : `<div class="sec">Nothing waiting.</div>`}</section>
    <section class="card"><div class="card-head"><h2>Earlier</h2></div><div class="rows">
      ${done.map((x) => `<div class="row-item" style="grid-template-columns:72px minmax(0,1fr) 80px">
        <span class="mono muted">${esc(fmtClock(x.created_at))}</span>
        <span style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${esc(x.detail)}"><span class="sec">${esc(x.tool)}</span>&nbsp; ${esc(x.detail)}</span>
        <span class="status ${x.status === "approved" ? "done" : x.status === "denied" ? "failed" : ""}">${esc(x.status)}</span></div>`).join("") || `<div class="empty">None yet.</div>`}
    </div></section>
  </div>`;
}

function pageFiles() {
  const f = S.files;
  if (!f) return `<div class="empty">Loading</div>`;
  return `${lockedBanner()}<h1 class="page-title">Files sent to your phone</h1>
  <section class="card"><div class="rows">
    ${f.files.map((x) => `<div class="row-item" style="grid-template-columns:minmax(0,1fr) 88px 96px 120px 120px">
      <span style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${esc(x.path)}">${esc(x.name)}<small class="muted" style="display:block">${esc(x.note || x.path)}</small></span>
      <a class="mono muted" href="#/tasks/${esc(x.task)}" style="text-decoration:none">${esc(x.task)}</a>
      <span class="mono sec num">${esc(fmtSize(x.size))}</span>
      <span class="status ${x.sent ? "done" : "queued"}">${x.sent ? "sent" : "queued"}</span>
      <span class="mono muted">${x.expires_at > f.now ? `link for ${esc(fmtDur(x.expires_at - f.now))}` : "link expired"}</span></div>`).join("") || `<div class="empty">No files sent yet. Ask “send me the latest screenshot”.</div>`}
  </div></section>`;
}

function pageSettings() {
  const ov = S.overview || {};
  const c = S.config;
  if (!c) return `<div class="empty">Loading</div>`;
  const comp = c.computer;
  const folders = (xs) => (xs && xs.length ? `<div class="folders mono">${xs.map((x) => `<span>${esc(x)}</span>`).join("")}</div>` : `<span class="muted">None</span>`);
  return `${lockedBanner()}<h1 class="page-title">Settings</h1>
  <div class="grid-2" style="margin-top:0">
    <section class="card"><div class="card-head"><h2>Kill switch</h2></div>
      <div class="sec">${ov.locked ? "Locked. Tasks and tool calls are refused until you unlock here or with tell-my-pc unlock." : "Lock refuses new tasks, denies waiting approvals and stops what is running. Your phone can lock too, but only this computer can unlock."}</div>
      <div style="margin-top:16px">${ov.locked
        ? `<button class="btn primary" data-act="unlock">${icon("unlock")}Unlock</button>`
        : `<button class="btn danger" data-act="lock">${icon("lock")}Lock now</button>`}</div></section>
    <section class="card"><div class="card-head"><h2>Daemon</h2></div><dl class="kv">
      <dt>Version</dt><dd class="mono">${esc(ov.version)}</dd>
      <dt>Running since</dt><dd class="mono">${ov.started_at ? esc(new Date(ov.started_at * 1000).toLocaleString()) : ""}</dd>
      <dt>Phone reaches it at</dt><dd class="mono">${esc(ov.listening)}</dd>
      <dt>Runs tasks with</dt><dd class="mono">${esc(ov.executor)}</dd>
      <dt>Notifies through</dt><dd class="mono">${esc(ov.notifier || "nothing")}</dd>
      <dt>Targets</dt><dd>${esc((ov.targets || []).join(", "))}</dd>
      ${ov.claude_limited_until && ov.claude_limited_until > ov.now ? `<dt>Claude limit</dt><dd>resets ${esc(fmtClock(ov.claude_limited_until))}</dd>` : ""}
    </dl></section>
  </div>
  ${comp.enabled ? `<section class="card" style="margin-top:12px"><div class="card-head"><h2>Tasks on the computer</h2></div><dl class="kv">
    <dt>Workspace</dt><dd class="mono">${esc(comp.workspace)}</dd>
    <dt>May change</dt><dd>${folders(comp.write_roots)}</dd>
    <dt>May read</dt><dd>${folders(comp.read_roots)}</dd>
    <dt>Blocked</dt><dd>${folders(comp.blocked)}</dd></dl>
    <div class="muted" style="margin-top:12px">Change these in the computer section of the config file, then restart the daemon.</div></section>` : ""}
  <section class="card" style="margin-top:12px"><div class="card-head"><h2>Config</h2><span class="right mono muted">secrets hidden</span></div>
    <pre class="config">${esc(JSON.stringify(c.config, null, 2))}</pre></section>`;
}

/* ------------------------------------------------------------------ render */

const PAGES = {
  overview: pageOverview, activity: pageActivity, tasks: pageTasks, task: pageTask,
  approvals: pageApprovals, files: pageFiles, settings: pageSettings,
};

let mountedRoute = "";
function render(full = false) {
  if (!KEY) return keyless();
  const key = `${S.route.name}/${S.route.arg || ""}`;
  if (!$(".shell") || full) {
    $("#app").innerHTML = shell();
  } else {
    // refresh the chrome without touching the page (keeps focus and scroll)
    const fresh = document.createElement("div");
    fresh.innerHTML = shell();
    $(".side").replaceWith(fresh.querySelector(".side"));
    $(".top").replaceWith(fresh.querySelector(".top"));
  }
  const page = $("#page");
  // the entrance plays once when a page opens, not on every live refresh
  document.body.classList.toggle("settled", mountedRoute === key);
  if (S.route.name === "activity") {
    if (mountedRoute !== key || !$("#evrows")) page.innerHTML = pageActivity();
    $("#evrows").innerHTML = eventRows();
  } else {
    page.innerHTML = PAGES[S.route.name]();
    if (S.route.name === "overview") drawWire();
  }
  if (mountedRoute !== key) window.scrollTo(0, 0);
  mountedRoute = key;
}

async function go() {
  S.route = parseRoute();
  S.open.clear();
  render();
  const loader = loaders[S.route.name];
  if (loader) await loader(S.route.arg);
  if (S.route.name === "overview" && !S.tasks) await loaders.tasks();
  render();
}

/* ------------------------------------------------------------------ events */

document.addEventListener("click", async (ev) => {
  const b = ev.target.closest("[data-act]");
  if (b) {
    const id = b.dataset.id;
    const a = b.dataset.act;
    b.disabled = true;
    try {
      if (a === "approve" || a === "deny") await act(`/api/approvals/${id}`, { decision: a });
      else if (a === "stop") await act("/api/stop", { id }, "Stop this task?");
      else if (a === "lock") await act("/api/lock", { reason: "locked from the dashboard" }, "Lock tell-my-pc? Running tasks stop and new ones are refused until you unlock.");
      else if (a === "unlock") await act("/api/unlock", {});
      else if (a === "older") {
        const first = (S.older[0] || S.events[0] || {}).id;
        if (first) {
          const { data } = await api(`/api/events?before=${first}&limit=300`);
          S.older = [...(data.events || []), ...S.older];
          render();
        }
      }
    } finally { b.disabled = false; }
    return;
  }
  const chip = ev.target.closest("[data-filter]");
  if (chip) { S.filter = chip.dataset.filter; $("#chips").innerHTML = FILTERS.map(([k, l]) => `<button class="chip ${S.filter === k ? "on" : ""}" data-filter="${k}">${l}</button>`).join(""); $("#evrows").innerHTML = eventRows(); return; }
  const row = ev.target.closest("[data-ev]");
  if (row && !ev.target.closest("a")) {
    const id = Number(row.dataset.ev);
    S.open.has(id) ? S.open.delete(id) : S.open.add(id);
    $("#evrows").innerHTML = eventRows();
  }
});
document.addEventListener("input", (ev) => {
  if (ev.target.id === "q") { S.query = ev.target.value; $("#evrows").innerHTML = eventRows(); }
});
window.addEventListener("hashchange", go);
let resizeT = 0;
window.addEventListener("resize", () => { clearTimeout(resizeT); resizeT = setTimeout(drawWire, 120); });
window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => drawWire());

/* ------------------------------------------------------------------ live */

let tick = 0;
async function poll() {
  try {
    const changed = await loadEvents();
    const slow = tick % 2 === 0;
    if (slow) await loadOverview();
    const r = S.route.name;
    if (r === "task" && S.task && S.task.task && ["running", "queued", "held"].includes(S.task.task.status)) await loaders.task(S.route.arg);
    else if (slow && (r === "tasks" || r === "overview")) await loaders.tasks();
    else if (slow && (r === "approvals" || r === "files")) await loaders[r]();
    // the activity list only re-renders when something arrived, so reading isn't disturbed
    if ((changed || slow) && !(r === "overview" && S.hover)) {
      if (r === "activity" && !changed) { const f = document.createElement("div"); f.innerHTML = shell(); $(".side").replaceWith(f.querySelector(".side")); }
      else render();
    }
  } catch (e) {
    if (String(e.message) === "no key") return;
  }
  tick++;
  setTimeout(poll, 2000);
}

(async function start() {
  readKey();
  if (!KEY) return keyless();
  try {
    await Promise.all([loadOverview(), loadEvents()]);
  } catch {
    return;
  }
  await go();
  setTimeout(poll, 2000);
})();
