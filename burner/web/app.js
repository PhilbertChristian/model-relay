"use strict";

const LANES = ["claude", "agent37", "orca", "mock"];
const STATES = {
  idle: "Idle",
  burning: "Burning",
  paused: "Paused",
  limited: "Limited",
  stopped: "Stopped",
};
const RUN_STATUS = new Set(["queued", "running", "succeeded", "failed", "limited", "cancelled"]);
const LEVELS = new Set(["info", "success", "warn"]);
const IDEA_STATUS = new Set(["new", "planned", "done"]);
const SSE_TYPES = [
  "project", "task", "run", "runner", "budget", "status",
  "concurrency", "integration", "idea", "feed", "event",
];

const $ = (sel) => document.querySelector(sel);

let snapshot = null;
let deadline = null;
let refreshTimer = 0;
let refreshing = false;
let refreshAgain = false;

function esc(value) {
  return String(value ?? "").replace(/[&<>"']/g, (ch) => ({
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    '"': "&quot;",
    "'": "&#39;",
  }[ch]));
}

function clip(value, max) {
  const text = String(value ?? "").replace(/\s+/g, " ").trim();
  if (text.length <= max) return text;
  return text.slice(0, max - 1) + "…";
}

function fmtTokens(n) {
  const v = Number(n);
  if (!Number.isFinite(v)) return "—";
  const sign = v < 0 ? "-" : "";
  const a = Math.abs(v);
  if (a >= 1e9) return sign + (a / 1e9).toFixed(2) + "B";
  if (a >= 1e6) return sign + (a / 1e6).toFixed(2) + "M";
  if (a >= 1e4) return sign + (a / 1e3).toFixed(1) + "k";
  if (a >= 1e3) return sign + (a / 1e3).toFixed(2) + "k";
  return sign + String(Math.round(a));
}

function pad(n) {
  return String(n).padStart(2, "0");
}

function formatRemain(ms) {
  if (!Number.isFinite(ms)) return "—:—:—";
  const s = Math.floor(Math.max(0, ms) / 1000);
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const sec = s % 60;
  return `${pad(h)}:${pad(m)}:${pad(sec)}`;
}

function fmtClock(iso) {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "--:--:--";
  return new Intl.DateTimeFormat(undefined, {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
  }).format(d);
}

function fmtWhen(iso) {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "";
  return new Intl.DateTimeFormat(undefined, {
    weekday: "short",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  }).format(d);
}

function statusClass(status) {
  return RUN_STATUS.has(status) ? status : "queued";
}

function setHTML(el, html) {
  if (!el || el._html === html) return;
  const top = el.scrollTop;
  el._html = html;
  el.innerHTML = html;
  el.scrollTop = top;
}

function setLink(ok) {
  const el = $("#link");
  if (!el) return;
  document.body.dataset.link = ok ? "up" : "down";
  el.textContent = ok ? "LIVE" : "RETRY";
  el.classList.toggle("is-down", !ok);
}

function notice(text) {
  const el = $("#notice");
  if (!el) return;
  el.textContent = text || "";
  el.hidden = !text;
}

function syncDeadline(budget) {
  if (!budget) {
    deadline = null;
    return;
  }
  if (budget.windowEnd) {
    const t = Date.parse(budget.windowEnd);
    if (!Number.isNaN(t)) {
      deadline = t;
      return;
    }
  }
  deadline = Number.isFinite(Number(budget.hoursLeft))
    ? Date.now() + Number(budget.hoursLeft) * 3600000
    : null;
}

function tick() {
  const clock = $("#countdown");
  if (clock) clock.textContent = deadline ? formatRemain(deadline - Date.now()) : "—:—:—";
  const session = $("#session");
  const uptime = $("#uptime");
  const started = snapshot?.startedAt ? Date.parse(snapshot.startedAt) : NaN;
  if (session && uptime) {
    if (Number.isNaN(started)) session.hidden = true;
    else {
      session.hidden = false;
      uptime.textContent = formatRemain(Date.now() - started);
    }
  }
}

function paintMeter(budget) {
  const meter = $("#meter");
  const pctEl = $("#pct");
  const has = budget && Number.isFinite(Number(budget.pctUsed));
  const pct = has ? Number(budget.pctUsed) : 0;
  const clamped = Math.max(0, Math.min(100, pct));
  meter.style.setProperty("--pct", String(clamped));
  meter.classList.toggle("is-limited", Boolean(budget?.limited));
  if (!has) {
    pctEl.textContent = "—";
    meter.setAttribute("aria-label", "Weekly quota unavailable");
    return;
  }
  const shown = pct >= 100 ? pct.toFixed(0) : pct.toFixed(1);
  pctEl.innerHTML = `${shown}<span class="unit">%</span>`;
  meter.setAttribute("aria-label", `${shown} percent of weekly quota`);
}

function paintStats(state) {
  const budget = state.budget;
  const behind = Boolean(
    budget && Number(budget.burnRatePerHour) < Number(budget.neededRatePerHour)
  );
  const cells = [
    ["Used", budget ? fmtTokens(budget.tokensUsed) : "—", ""],
    ["Target", budget ? fmtTokens(budget.tokensTarget) : "—", ""],
    ["Agents", String(state.concurrency ?? 0), ""],
    ["Recommend", budget && budget.recommendedConcurrency != null ? String(budget.recommendedConcurrency) : "—", ""],
    ["Burn/h", budget ? fmtTokens(budget.burnRatePerHour) : "—", ""],
    ["Need/h", budget ? fmtTokens(budget.neededRatePerHour) : "—", behind ? "is-behind" : ""],
  ];
  setHTML($("#stats"), cells.map(([label, value, cls]) => `
    <div class="stat ${cls}">
      <div class="stat-label">${esc(label)}</div>
      <div class="stat-value">${esc(value)}</div>
    </div>
  `).join(""));

  const burnN = Number(budget?.burnRatePerHour);
  const needN = Number(budget?.neededRatePerHour);
  const burn = Number.isFinite(burnN) ? burnN : 0;
  const need = Number.isFinite(needN) ? needN : 0;
  const max = Math.max(burn, need, 1);
  const width = (n) => `${Math.max(0, Math.min(100, (n / max) * 100)).toFixed(1)}%`;
  setHTML($("#rates"), `
    <div class="rate">
      <div class="rate-top"><span>Burn</span><span>${esc(budget ? fmtTokens(burn) + "/h" : "—")}</span></div>
      <div class="track"><div class="fill fill-burn" style="width:${width(burn)}"></div></div>
    </div>
    <div class="rate ${behind ? "is-behind" : ""}">
      <div class="rate-top"><span>Need</span><span>${esc(budget ? fmtTokens(need) + "/h" : "—")}</span></div>
      <div class="track"><div class="fill fill-need" style="width:${width(need)}"></div></div>
    </div>
  `);

  const flag = $("#limit-flag");
  if (budget?.limited) {
    const when = budget.limitResetsAt ? ` · resets ${fmtWhen(budget.limitResetsAt)}` : "";
    flag.hidden = false;
    flag.textContent = `Lane limited${when}`;
  } else if (flag) {
    flag.hidden = true;
    flag.textContent = "";
  }
  const windowEnd = $("#window-end");
  if (windowEnd) windowEnd.textContent = budget?.windowEnd ? fmtWhen(budget.windowEnd) : "";
}

function paintControls(stateName) {
  const hold = $("#btn-hold");
  if (!hold) return;
  const paused = stateName === "paused";
  hold.dataset.action = paused ? "resume" : "pause";
  hold.textContent = paused ? "Resume" : "Pause";
  for (const btn of document.querySelectorAll("#controls [data-action]")) {
    const action = btn.dataset.action;
    const live =
      (action === "start" && (stateName === "idle" || stateName === "stopped")) ||
      (action === "pause" && (stateName === "burning" || stateName === "limited")) ||
      (action === "resume" && stateName === "paused");
    btn.classList.toggle("is-live", live);
  }
}

function paintLamps(integrations) {
  const names = ["agent37", "monid", "orca"];
  const html = names.map((name) => {
    const info = integrations?.[name];
    const cls = !info ? "lamp-unk" : info.ok ? "lamp-ok" : "lamp-bad";
    const title = info?.message ? ` title="${esc(info.message)}"` : "";
    return `<span class="lamp ${cls}"${title}><i></i>${esc(name)}</span>`;
  }).join("");
  setHTML($("#lamps"), html);
}

function ensureLanes() {
  const root = $("#lanes");
  for (const lane of LANES) {
    if (root.querySelector(`#lane-${lane}`)) continue;
    const sec = document.createElement("section");
    sec.className = `lane lane-${lane} panel`;
    sec.id = `lane-${lane}`;
    sec.innerHTML = `
      <header class="lane-h">
        <span class="pip"></span>
        <span class="lane-name">${lane}</span>
        <span class="lane-count" data-count>00</span>
      </header>
      <div class="lane-body" data-lane="${lane}"></div>`;
    root.appendChild(sec);
  }
}

function runCard(run, projectName) {
  const usage = run.usage || {};
  const blurb = clip(run.summary || run.error || "", 220);
  const bits = [`${fmtTokens(usage.input)} in`, `${fmtTokens(usage.output)} out`];
  if (usage.cacheRead) bits.push(`${fmtTokens(usage.cacheRead)} cr`);
  if (usage.cacheWrite) bits.push(`${fmtTokens(usage.cacheWrite)} cw`);
  if (typeof run.filesChanged === "number") bits.push(`${run.filesChanged} files`);
  if (typeof run.insertions === "number" || typeof run.deletions === "number") {
    bits.push(`+${run.insertions || 0} -${run.deletions || 0}`);
  }
  const title = run.title || run.id || "run";
  const proj = projectName && projectName !== title
    ? `<div class="proj">${esc(projectName)}</div>`
    : "";
  const summary = blurb
    ? `<p class="summary${run.summary ? "" : " is-error"}">${esc(blurb)}</p>`
    : "";
  const branch = run.branch ? `<div class="branch">${esc(run.branch)}</div>` : "";
  return `
    <div class="run-top">
      <h3>${esc(title)}</h3>
      <span class="status">${esc(statusClass(run.status))}</span>
    </div>
    ${proj}
    ${summary}
    <p class="meta">${bits.map(esc).join(" · ")}</p>
    ${branch}`;
}

function syncLane(lane, runs, names) {
  const body = $(`#lane-${lane} .lane-body`);
  if (!runs.length) {
    const html = `<div class="empty">standby</div>`;
    if (body._html !== html) {
      body._html = html;
      body.dataset.order = "";
      body.innerHTML = html;
    }
    return;
  }
  const keep = new Set(runs.map((run) => String(run.id)));
  for (const node of [...body.querySelectorAll("[data-run]")]) {
    if (!keep.has(node.dataset.run)) node.remove();
  }
  body.querySelector(".empty")?.remove();
  for (const run of runs) {
    const id = String(run.id);
    const cls = `run run-${statusClass(run.status)}`;
    const html = runCard(run, names.get(run.projectId));
    let card = body.querySelector(`[data-run="${CSS.escape(id)}"]`);
    if (!card) {
      card = document.createElement("article");
      card.dataset.run = id;
      body.appendChild(card);
    }
    if (card.className !== cls) card.className = cls;
    if (card._html !== html) {
      card._html = html;
      card.innerHTML = html;
    }
  }
  const order = runs.map((run) => run.id).join("|");
  if (body.dataset.order !== order) {
    body.dataset.order = order;
    for (const run of runs) {
      const card = body.querySelector(`[data-run="${CSS.escape(String(run.id))}"]`);
      if (card) body.appendChild(card);
    }
  }
  body._html = "runs";
}

function stamp(run) {
  const t = Date.parse(run.endedAt || run.startedAt || "");
  return Number.isNaN(t) ? 0 : t;
}

function paintLanes(runs, names) {
  ensureLanes();
  const grouped = Object.fromEntries(LANES.map((lane) => [lane, []]));
  for (const run of runs) {
    if (grouped[run.lane]) grouped[run.lane].push(run);
  }
  for (const lane of LANES) {
    const all = grouped[lane].slice().sort((a, b) => stamp(b) - stamp(a));
    const sec = $(`#lane-${lane}`);
    sec.classList.toggle("is-live", all.some((run) => run.status === "running"));
    const n = all.length;
    sec.querySelector("[data-count]").textContent = n < 100 ? pad(n) : String(n);
    syncLane(lane, all.slice(0, 6), names);
  }
}

function paintProjects(projects) {
  const list = projects.slice().sort((a, b) => (b.score || 0) - (a.score || 0));
  const count = $("#project-count");
  if (count) count.textContent = list.length < 100 ? pad(list.length) : String(list.length);
  if (!list.length) {
    setHTML($("#projects"), `<div class="empty">no repos in view</div>`);
    return;
  }
  const max = Math.max(...list.map((p) => Number(p.score) || 0), 1);
  const html = list.map((project) => {
    const bits = [];
    if (project.languages?.length) bits.push(project.languages.slice(0, 3).join(" · "));
    if (project.packageManager) bits.push(project.packageManager);
    if (project.todoCount) bits.push(`${project.todoCount} todos`);
    if (project.dirty) bits.push("dirty");
    if (project.hasTests) bits.push("tests");
    const width = Math.max(0, Math.min(100, ((Number(project.score) || 0) / max) * 100));
    return `
      <article class="item" title="${esc(project.path || "")}">
        <div class="item-top">
          <div class="item-title">${esc(project.name || project.id)}</div>
          <div class="item-score">${esc(Math.round(Number(project.score) || 0))}</div>
        </div>
        <div class="sub">${esc(bits.join(" · ") || "unscored")}</div>
        <div class="bar"><span style="width:${width.toFixed(1)}%"></span></div>
      </article>`;
  }).join("");
  setHTML($("#projects"), html);
}

function paintTasks(tasks, names) {
  const list = tasks.slice().sort((a, b) => (b.priority || 0) - (a.priority || 0));
  const count = $("#task-count");
  if (count) count.textContent = list.length < 100 ? pad(list.length) : String(list.length);
  if (!list.length) {
    setHTML($("#tasks"), `<div class="empty">queue empty</div>`);
    return;
  }
  const max = Math.max(...list.map((task) => Number(task.priority) || 0), 1);
  const html = list.map((task) => {
    const bits = [
      names.get(task.projectId) || task.projectId || "project",
      `p${task.priority ?? 0}`,
    ];
    if (task.estTokens) bits.push(`~${fmtTokens(task.estTokens)}`);
    if (task.lane) bits.push(task.lane);
    const width = Math.max(0, Math.min(100, ((Number(task.priority) || 0) / max) * 100));
    return `
      <article class="item">
        <div class="item-top">
          <div class="item-title">${esc(task.title || task.id)}</div>
          <div class="item-score">${esc(task.kind || "")}</div>
        </div>
        <div class="sub">${esc(bits.join(" · "))}</div>
        <div class="bar"><span style="width:${width.toFixed(1)}%"></span></div>
      </article>`;
  }).join("");
  setHTML($("#tasks"), html);
}

function paintFeed(feed) {
  const rows = (feed || []).slice().sort((a, b) => String(b.at).localeCompare(String(a.at)));
  const count = $("#feed-count");
  if (count) count.textContent = rows.length < 100 ? pad(rows.length) : String(rows.length);
  const el = $("#feed");
  if (!rows.length) {
    setHTML(el, `<li class="empty">standing by</li>`);
    return;
  }
  const html = rows.map((item) => {
    const level = LEVELS.has(item.level) ? item.level : "info";
    return `
      <li class="feed-item level-${level}">
        <time datetime="${esc(item.at)}">${esc(fmtClock(item.at))}</time>
        <span class="src">${esc(item.source || "burner")}</span>
        <span class="msg">${esc(item.message || "")}</span>
      </li>`;
  }).join("");
  if (el._html === html) return;
  const pin = el.scrollTop < 12;
  const top = el.scrollTop;
  el._html = html;
  el.innerHTML = html;
  el.scrollTop = pin ? 0 : top;
}

let reviewPlan = null;
let searchHits = null;
let searchTimer = 0;

function paintIdeas(ideas) {
  const scroller = $("#idea-scroller");
  const query = ($("#search")?.value || "").trim();
  let html = "";
  if (query.length >= 2) {
    const hits = Array.isArray(searchHits) ? searchHits : [];
    html = hits.length
      ? hits.map((hit) => `
      <article class="idea">
        <span class="idea-score">${esc(hit.role === "assistant" ? "AI" : "YOU")}</span>
        <p class="idea-text">${esc(clip(hit.text || "", 180))}</p>
        <span class="idea-status">${esc(hit.role || "hit")}</span>
      </article>`).join("")
      : `<p class="idea-text">No matches</p>`;
  } else if (reviewPlan && Array.isArray(reviewPlan.checks) && reviewPlan.checks.length && !(ideas || []).length) {
    html = reviewPlan.checks.slice(0, 8).map((check) => `
      <article class="idea">
        <span class="idea-score">REV</span>
        <p class="idea-text">${esc(clip(check, 180))}</p>
        <span class="idea-status">review</span>
      </article>`).join("");
  } else {
    const list = (ideas || []).slice().sort((a, b) => (b.score || 0) - (a.score || 0));
    html = list.map((idea) => {
      const status = IDEA_STATUS.has(idea.status) ? idea.status : "new";
      return `
      <article class="idea">
        <span class="idea-score">${esc(Math.round(Number(idea.score) || 0))}</span>
        <p class="idea-text">${esc(clip(idea.text || idea.summary || "", 160))}</p>
        <span class="idea-status st-${status}">${esc(status)}</span>
      </article>`;
    }).join("");
  }
  setHTML(scroller, html);
}

function paintReviewLine() {
  const line = $("#review-line");
  if (!line) return;
  line.textContent = reviewPlan && reviewPlan.summary ? reviewPlan.summary : "Review plan";
}

function paint(state) {
  snapshot = state;
  const known = Object.hasOwn(STATES, state.state);
  const key = known ? state.state : "idle";
  document.body.dataset.state = key;
  const pill = $("#state-pill");
  pill.className = `pill pill-${known ? key : "idle"}`;
  const label = known ? STATES[key] : String(state.state || "Idle");
  pill.querySelector("span").textContent = label;
  document.title = `BURNER · ${label}`;
  $("#demo-badge").hidden = !state.demo;
  syncDeadline(state.budget);
  tick();
  paintMeter(state.budget);
  paintStats(state);
  paintControls(state.state);
  paintLamps(state.integrations || {});
  const names = new Map((state.projects || []).map((project) => [project.id, project.name]));
  paintLanes(state.runs || [], names);
  paintProjects(state.projects || []);
  paintTasks(state.tasks || [], names);
  paintFeed(state.feed || []);
  paintReviewLine();
  paintIdeas(state.ideas || []);
}

function emptyState() {
  return {
    state: "idle",
    concurrency: 0,
    projects: [],
    tasks: [],
    runs: [],
    logs: {},
    budget: null,
    integrations: {},
    ideas: [],
    feed: [],
    startedAt: null,
    demo: false,
  };
}

function refresh() {
  refreshAgain = true;
  if (refreshing || refreshTimer) return;
  refreshTimer = setTimeout(flush, 40);
}

async function flush() {
  refreshTimer = 0;
  if (refreshing) return;
  refreshAgain = false;
  refreshing = true;
  try {
    const res = await fetch("/api/state", { cache: "no-store" });
    if (!res.ok) throw new Error(String(res.status));
    paint(await res.json());
    setLink(true);
  } catch {
    setLink(false);
  } finally {
    refreshing = false;
    if (refreshAgain) refresh();
  }
}

async function control(action, btn) {
  if (!action || btn.disabled) return;
  btn.disabled = true;
  btn.classList.add("is-ack");
  try {
    const res = await fetch("/api/control", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ action }),
    });
    if (!res.ok) throw new Error(String(res.status));
    notice("");
    refresh();
  } catch {
    notice("CONTROL REJECTED");
  } finally {
    btn.disabled = false;
    setTimeout(() => btn.classList.remove("is-ack"), 420);
  }
}

function openEvents() {
  const es = new EventSource("/api/events");
  const onEvent = () => refresh();
  es.onmessage = onEvent;
  es.onopen = () => {
    setLink(true);
    refresh();
  };
  es.onerror = () => setLink(false);
  for (const type of SSE_TYPES) es.addEventListener(type, onEvent);
}

async function runSearch(raw) {
  const query = String(raw || "").trim();
  if (query.length < 2) {
    searchHits = null;
    if (snapshot) paintIdeas(snapshot.ideas || []);
    return;
  }
  try {
    const res = await fetch(`/api/search?q=${encodeURIComponent(query)}`, { cache: "no-store" });
    if (!res.ok) throw new Error(String(res.status));
    const body = await res.json();
    searchHits = Array.isArray(body.hits) ? body.hits : [];
  } catch {
    searchHits = [];
  }
  if (snapshot) paintIdeas(snapshot.ideas || []);
}

async function loadReview() {
  try {
    const res = await fetch("/api/review", { cache: "no-store" });
    if (!res.ok) return;
    const body = await res.json();
    reviewPlan = body && body.plan ? body.plan : null;
    paintReviewLine();
    if (snapshot) paintIdeas(snapshot.ideas || []);
  } catch {
    /* review plan stays blank until the next load */
  }
}

function boot() {
  paint(emptyState());
  const form = $("#search-form");
  const input = $("#search");
  form.addEventListener("submit", (event) => {
    event.preventDefault();
    runSearch(input.value);
  });
  input.addEventListener("input", () => {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(() => runSearch(input.value), 180);
  });
  $("#controls").addEventListener("click", (event) => {
    const btn = event.target.closest("[data-action]");
    if (!btn) return;
    control(btn.dataset.action, btn);
  });
  refresh();
  loadReview();
  openEvents();
  setInterval(tick, 1000);
  setInterval(() => {
    if (document.body.dataset.link === "down") refresh();
  }, 4000);
}

boot();
