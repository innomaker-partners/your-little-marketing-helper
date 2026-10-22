/* Marketing Viewer — frontend application logic.
   Vanilla JS, no build step, no CDN (offline). Renders the marketing-scraping
   run directory served by viewer_server.py. Read + annotate only.

   Data contract (see LOCAL_VIEWER_DESIGN.md):
     GET  /api/registry            -> [{run_id, tool, subject, generated_at, counts, indexed}]
     GET  /api/index?run=<run_id>  -> full frontend_index.json {records[], synthesis, reports[], ...}
     GET  /api/annotations?run=..  -> {} | {favorites:{uid:true}, notes:{uid:"..."}, trashed:{uid:true}}
     POST /api/annotations?run=..  {kind, uid, value} -> ONE mark set/cleared; returns {annotations, stamp}
     GET  /api/views?run=..        -> {views:[{id,title,note,uids,created_by,created_at}]}
     GET  /api/changes?run=..      -> {annotations, views, index} change stamps (polled)
     POST /api/view                -> what the user has open, for the agent (current_view.json)
     GET  /files?path=<rel>        -> creative / report bytes, path-guarded under root
*/

/* ============================================================================
   NAME & CHROME COPY. Deliberately a plain, descriptive label (not a brand):
   it names what is on screen, the results of the marketing-research runs this
   tool reads. These two lines are the only authored name strings in the app;
   change them here and the header, rail, and tab title all follow.
   ========================================================================== */
const APP_NAME = "Scraped marketing data";
const APP_SUBTITLE = "local mode";

/* ---- tiny helpers -------------------------------------------------------- */
const $ = (sel, root = document) => root.querySelector(sel);
const esc = (s) => String(s == null ? "" : s)
  .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
  .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
const num = (n) => (typeof n === "number" && isFinite(n)) ? n.toLocaleString("en-US") : esc(n);
const initials = (s) => String(s || "?").trim().split(/\s+/).slice(0, 2).map(w => w[0] || "").join("").toUpperCase() || "?";
const has = (v) => v !== null && v !== undefined && v !== "" && v !== "unknown" && v !== "n/a";

// Meta dynamic ads carry Mustache-style tokens ({{product.brand}}) that Meta fills per
// viewer from the advertiser's catalog. Left raw they render as broken template code
// ("wtf are these dynamic tags left in?"). hasTokens flags them; stripTokens
// removes them and collapses the gap -- a mixed body keeps its real words and loses only
// the tokens; a token-ONLY body becomes "" so the caller can show a dynamic-ad note instead.
// Note: no shared /g regex for the test -- a global regex's lastIndex is stateful across
// .test() calls, so hasTokens uses its own non-global pattern; stripTokens uses a fresh /g.
const hasTokens = (s) => typeof s === "string" && /\{\{\s*[^}]+\}\}/.test(s);
const stripTokens = (s) => typeof s === "string" ? s.replace(/\{\{\s*[^}]+\}\}/g, " ").replace(/\s+/g, " ").trim() : s;

// Presentation of the ISO dates emit normalizes (emit does epoch->ISO + duration; the view
// does the human phrasing, so the two never re-derive each other).
const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
function fmtDate(iso) {
  if (!has(iso) || String(iso).length < 10) return "";
  const [y, m, d] = String(iso).slice(0, 10).split("-").map(Number);
  if (!y || !m || !d) return "";
  return `${MONTHS[m - 1]} ${d}, ${y}`;
}
function durLabel(days) {
  if (days == null || days < 0) return "";
  if (days < 1) return "under a day";
  if (days < 14) return days + (days === 1 ? " day" : " days");
  if (days < 60) return Math.round(days / 7) + " weeks";
  return Math.round(days / 30) + " months";
}
// Coarse run-duration bands for the ad filter. Ordered shortest -> longest ->
// unknown. "unknown" catches ads whose duration the scrape does not report
// (LinkedIn Ad Library reports none), so a duration filter never silently drops
// them into an invisible gap — they are filterable under an honest "Not reported".
const DUR_BANDS = [
  ["u1w", "Under a week"],
  ["1to4w", "1 to 4 weeks"],
  ["1to3m", "1 to 3 months"],
  ["3mplus", "Over 3 months"],
  ["unknown", "Not reported"],
];
const DUR_LABEL = Object.fromEntries(DUR_BANDS);
function durationBand(r) {
  const d = (r.data || {}).duration_days;
  if (typeof d !== "number" || d < 0) return "unknown";
  if (d < 7) return "u1w";
  if (d < 30) return "1to4w";
  if (d < 90) return "1to3m";
  return "3mplus";
}
// One honest line about when an ad ran, from whatever dates exist (no "to" en-dash: copy rules).
function runLine(d) {
  const first = fmtDate(d.first_shown), last = fmtDate(d.last_shown), dur = durLabel(d.duration_days);
  if (first && last) return `ran ${first} to ${last}${dur ? " · " + dur : ""}`;
  if (first) return `first shown ${first}`;
  if (last) return `last shown ${last}`;
  return "";
}

// Best-effort number from a value that may be a numeric string ("8.25", "1,200"). null if not.
function toNum(v) {
  if (typeof v === "number") return isFinite(v) ? v : null;
  if (v == null) return null;
  const n = parseFloat(String(v).replace(/[,$%\s]/g, ""));
  return isFinite(n) ? n : null;
}
// localStorage, wrapped so a private window / blocked storage never throws (per-viewer only).
function lsGet(k, fallback) {
  try { const v = localStorage.getItem(k); return v == null ? fallback : JSON.parse(v); }
  catch (_) { return fallback; }
}
function lsSet(k, v) { try { localStorage.setItem(k, JSON.stringify(v)); } catch (_) {} }
// Show/hide an already-rendered block (used for expandable panels); flips a caret if given.
function toggleEl(id, btn) {
  const el = document.getElementById(id);
  if (!el) return;
  const open = el.style.display === "none";
  el.style.display = open ? "" : "none";
  if (btn) {
    btn.setAttribute("aria-expanded", String(open));
    btn.classList.toggle("open", open);
    const w = btn.querySelector(".disc-word"); if (w) w.textContent = open ? "Hide" : "Show";
  }
}
// One disclosure vocabulary for every expander: a chevron that flips plus a Show/Hide
// word, so a reader sees that the control opens a panel AND that it closes it again
// (the old 16px triangle was too small and never said "close").
function discState() {
  return `<span class="disc-state"><span class="disc-word">Show</span><svg class="disc-chev" width="12" height="12" viewBox="0 0 12 12" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M3 4.5l3 3 3-3"/></svg></span>`;
}

/* ============================================================================
   TOOLTIP LAYER — global, delegated, works in embedded Chromium/Cowork
   where native title bubbles never paint. One <div id="tip"> appended to body;
   mouseover/focusin delegation; lazy title->data-tip move on every hover so
   button-state re-sets (paintFav, toc toggle) always surface the newest text.
   ========================================================================== */
function initTooltip() {
  const tip = document.createElement("div");
  tip.id = "tip";
  tip.setAttribute("role", "tooltip");
  tip.hidden = true;
  document.body.appendChild(tip);

  let anchorEl = null;
  let pendingEl = null;   // element whose tip is scheduled but not yet shown
  let showTimer = null;

  function hide() {
    clearTimeout(showTimer);
    showTimer = null;
    pendingEl = null;
    if (anchorEl) { anchorEl.removeAttribute("aria-describedby"); anchorEl = null; }
    tip.hidden = true;
  }

  function positionTip(el) {
    const r = el.getBoundingClientRect();
    const vw = window.innerWidth, vh = window.innerHeight;
    const gap = 8;
    // A tip is never taller than the pane: the top clamp below keeps a tall tip's top on
    // screen, so without this cap its bottom ran off the screen (an ad's whole text made
    // a tip 19,630 px tall). Measured after the cap is set.
    tip.style.maxHeight = Math.max(0, vh - 2 * gap) + "px";
    tip.style.overflow = "hidden";
    const tw = tip.offsetWidth, th = tip.offsetHeight;
    let left = r.left + r.width / 2 - tw / 2;
    left = Math.max(gap, Math.min(left, vw - tw - gap));
    let top = r.bottom + gap;
    if (top + th > vh - gap) top = r.top - th - gap;
    top = Math.max(gap, top);   // a very short pane: never above the top edge
    tip.style.left = left + "px";
    tip.style.top  = top  + "px";
  }

  function showFor(el, text, delay) {
    clearTimeout(showTimer);
    pendingEl = el;
    showTimer = setTimeout(() => {
      pendingEl = null;
      if (!document.contains(el)) return;
      if (!lsGet("mv.tips", true)) return;
      tip.textContent = text;
      tip.hidden = false;
      anchorEl = el;
      el.setAttribute("aria-describedby", "tip");
      positionTip(el);
    }, delay);
  }

  function activate(e) {
    const el = e.target && e.target.closest ? e.target.closest("[title],[data-tip]") : null;
    if (!el) { hide(); return; }
    // Lazy move: transfer title -> data-tip now (overwrites any stale data-tip from a
    // prior hover, so button-state re-sets — paintFav, toc toggle — always win).
    const rawTitle = el.getAttribute("title");
    if (rawTitle !== null) {
      const text = rawTitle.trim();
      el.setAttribute("data-tip", text);
      el.removeAttribute("title");
      // Accessibility: icon-only elements (SVG only, no visible text) get aria-label
      // A single symbol (★, ‹, ›) is not a name either: a screen reader would announce the
      // glyph. Labels we set are marked so a later title change (paintFav) keeps them current.
      const vis = el.textContent.trim();
      const noName = vis === "" || (vis.length <= 2 && !/[\p{L}\p{N}]/u.test(vis));
      if (text && noName && (!el.hasAttribute("aria-label") || el.hasAttribute("data-tip-aria"))) {
        el.setAttribute("aria-label", text);
        el.setAttribute("data-tip-aria", "");
      }
    }
    const text = (el.getAttribute("data-tip") || "").trim();
    if (!text) { hide(); return; }
    // Re-anchoring: moving from one tipped element to another
    if (anchorEl && anchorEl !== el) hide();
    showFor(el, text, e.type === "focusin" ? 0 : 300);
  }

  document.addEventListener("mouseover", activate);
  document.addEventListener("focusin",   activate);
  // Leaving the element cancels a PENDING tip too, not only a shown one: otherwise a
  // quick pass over a label pops its tip 300 ms later, after the pointer has moved on.
  document.addEventListener("mouseout", e => {
    const cur = anchorEl || pendingEl;
    if (!cur) return;
    if (!document.contains(cur)) { hide(); return; }
    const rel = e.relatedTarget;
    if (!rel || !cur.contains(rel)) hide();
  });
  document.addEventListener("focusout",    hide);
  document.addEventListener("click",       hide);
  document.addEventListener("pointerdown", hide);
  document.addEventListener("keydown", e => { if (e.key === "Escape") hide(); });
  // Scroll follows the anchor instead of cancelling: focus() scrolls its target into
  // view, so cancelling here killed every keyboard tip for an off-screen control. A
  // mouse user who wheels away gets a mouseout, which hides it.
  document.addEventListener("scroll", () => {
    if (!anchorEl) return;
    const r = anchorEl.getBoundingClientRect();
    if (!document.contains(anchorEl) || r.bottom < 0 || r.top > window.innerHeight) hide();
    else positionTip(anchorEl);
  }, { capture: true, passive: true });
  window.addEventListener("blur", hide);
}

function toggleTips() {
  const on = !lsGet("mv.tips", true);
  lsSet("mv.tips", on);
  if (!on) { const tip = document.getElementById("tip"); if (tip) tip.hidden = true; }
  // Update the switch in place rather than re-rendering the rail: a rebuild destroys the
  // focused button (keyboard users lose their place) and resets the rail's scroll.
  document.querySelectorAll(".tip-switch").forEach(sw => {
    sw.setAttribute("aria-checked", String(on));
    const lbl = sw.querySelector(".tip-label");
    if (lbl) lbl.textContent = on ? "Tooltips on" : "Tooltips off";
  });
}
// Stable sort of records by an accessor; num=true compares numerically (nulls last).
function sortBy(list, get, num, dir) {
  const s = list.map((r, i) => [r, i]).sort((A, B) => {
    let x = get(A[0]), y = get(B[0]);
    if (num) {
      const xn = x == null, yn = y == null;
      if (xn && yn) return A[1] - B[1];
      if (xn) return 1; if (yn) return -1;         // nulls always last, regardless of dir
      return (x - y) * (dir === "desc" ? -1 : 1);
    }
    x = String(x == null ? "" : x).toLowerCase();
    y = String(y == null ? "" : y).toLowerCase();
    if (x === y) return A[1] - B[1];
    return (x < y ? -1 : 1) * (dir === "desc" ? -1 : 1);
  });
  return s.map(p => p[0]);
}

const PLATFORM = {
  meta: "Meta", google: "Google", linkedin: "LinkedIn",
  trustpilot: "Trustpilot", g2: "G2", capterra: "Capterra", reddit: "Reddit",
  maps: "Google Maps", appstore: "App Store", googleplay: "Google Play",
  semrush: "Semrush", google_serp: "Google",
};
const platLabel = (p) => PLATFORM[p] || (p ? p[0].toUpperCase() + p.slice(1) : "-");
const TOOL_LABEL = { ad: "Ads", pain: "Reviews", search: "Search", unknown: "Run" };
// The order the dossier renders its sections (synthesis/Reviews, then Search, then Ads,
// see openCampaign). The header run chips are sorted by this so the tags read in the same
// order as the sections below them. Unknown tools sort last.
const TOOL_ORDER = { pain: 0, search: 1, ad: 2 };
const toolOrder = (t) => (t in TOOL_ORDER ? TOOL_ORDER[t] : 9);
const POLARITY_LABEL = { low: "negative", high: "positive", neutral: "neutral" };
const polLabel = (p) => POLARITY_LABEL[p] || p;
// Canonical display order for the Sentiment facet: positive first, then negative, then neutral.
const POLARITY_ORDER = { high: 0, low: 1, neutral: 2 };
const sortPolarities = (arr) =>
  [...arr].sort((a, b) => {
    const oa = a in POLARITY_ORDER ? POLARITY_ORDER[a] : 9;
    const ob = b in POLARITY_ORDER ? POLARITY_ORDER[b] : 9;
    return oa - ob;
  });

/* ---- API layer ----------------------------------------------------------- */
const api = {
  async registry() { return fetchJSON("/api/registry"); },
  async index(runId) { return fetchJSON("/api/index?run=" + encodeURIComponent(runId)); },
  async annotations(runId) { return fetchJSON("/api/annotations?run=" + encodeURIComponent(runId)); },
  // One mark on one record (kind: favorites | notes | trashed; value null clears it). The
  // server applies it under a lock and answers with every mark of the run, so a mark the
  // agent added in the meantime is kept, never overwritten by this tab's older copy.
  async mark(runId, kind, uid, value) {
    const r = await fetch("/api/annotations?run=" + encodeURIComponent(runId), {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ kind, uid, value: value === undefined ? null : value }),
    });
    if (!r.ok) throw new Error("save failed: " + r.status);
    return r.json();
  },
  async views(runId) { return fetchJSON("/api/views?run=" + encodeURIComponent(runId)); },
  async carried(runId) { return fetchJSON("/api/carried?run=" + encodeURIComponent(runId)); },
  async changes(runId) { return fetchJSON("/api/changes?run=" + encodeURIComponent(runId)); },
  // The one compare call. The app never recomputes comparison in JS.
  async compare(runId, againstId) {
    let url = "/api/compare?run=" + encodeURIComponent(runId);
    if (againstId) url += "&against=" + encodeURIComponent(againstId);
    return fetchJSON(url);
  },
  fileURL(runId, relPath) {
    const rel = (runId === "." || !runId) ? relPath : runId + "/" + relPath;
    return "/files?path=" + encodeURIComponent(rel);
  },
};
async function fetchJSON(url) {
  const r = await fetch(url);
  const body = await r.json().catch(() => ({}));
  if (!r.ok) { const e = new Error(body.error || ("HTTP " + r.status)); e.info = body; throw e; }
  return body;
}

/* ---- application state --------------------------------------------------- */
const state = {
  registry: [],
  campaigns: [],           // [{subject, runs:[row]}]
  index: null,             // current run's frontend_index.json
  runId: null,
  ann: { favorites: {}, notes: {}, trashed: {} },
  trash: {},               // { runId: { uid: true, ... } } — all runs, loaded at boot
  filters: {},             // per-surface, reset on navigation
  views: [],               // current run's saved views (.viewer/views.json)
  visible: null,           // { uids, total } of the list on screen, for the agent's view report
  stamps: null,            // { run, annotations, views, index } last seen change stamps
  compare: null,           // compare_runs result for the current run (loaded in openRun)
  compareNote: null,       // Why a change link could not be honoured (compareProblem), or null
  carried: null,           // carried-marks marker from /api/carried (loaded in openRun)
};

// When true, renderAdLibrary / renderReviewWall skip their state.filters reset
// so collapsing or expanding the filter rail preserves the active filters.
let _skipFilterReset = false;

// Re-mark facet checkboxes as selected to match state.filters after a re-render
// that used _skipFilterReset = true (the DOM is fresh but the state is preserved).
function restoreFilterUI() {
  for (const [key, val] of Object.entries(state.filters)) {
    if (val instanceof Set) {
      val.forEach(v => {
        const el = document.querySelector(`.facet[data-facet="${cssEsc(key)}"][data-val="${cssEsc(v)}"]`);
        if (el) el.classList.add("sel");
      });
    }
  }
  if (state.filters.favOnly) {
    const ft = document.querySelector("[data-favtoggle]");
    if (ft) ft.classList.add("sel");
  }
  document.querySelectorAll("[data-viewsel]").forEach(el =>
    el.classList.toggle("sel", el.dataset.viewsel === state.filters.view));
  const q = $("#fq");
  if (q && state.filters.q) q.value = state.filters.q;
}

/* ============================================================================
   BOOT
   ========================================================================== */
/* Outbound links: every link the app opens in a new tab (target=_blank: ads, reviews,
   posts, search results, report sources, the #/innomaker page) plus a[data-ext] (the
   contact email). Embedded browsers such as the Claude desktop app's browser pane silently
   drop target=_blank, so a plain click asks the local server to open the link in the
   user's default browser (POST /api/open, which checks the link server-side). If the server cannot (no browser on that machine), the browser opens it
   itself: https in a new tab, mailto in place (that hands it to the email app). Modifier
   clicks keep the browser's own behaviour. */
function initExternalLinks() {
  document.addEventListener("click", async (e) => {
    const a = e.target.closest && e.target.closest('a[target="_blank"], a[data-ext]');
    if (!a || e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
    e.preventDefault();
    const url = a.href;
    try {
      const r = await fetch("/api/open", { method: "POST",
        headers: { "Content-Type": "application/json" }, body: JSON.stringify({ url }) });
      if (r.ok && (await r.json()).opened) return;
    } catch (_) { /* fall through to the browser's own handling */ }
    if (url.startsWith("mailto:")) { location.href = url; return; }
    // Anything else the server refused (javascript:, data:, ...) is never opened here:
    // scraped data fills these hrefs, and esc() escapes HTML but does not check schemes.
    if (!/^https?:/i.test(url)) return;
    const w = window.open(url, "_blank");
    if (w) w.opener = null; else location.href = url;
  });
}

async function boot() {
  buildFrame();
  initTooltip();
  initExternalLinks();
  try {
    state.registry = await api.registry();
  } catch (e) {
    return renderFatal("Could not reach the viewer server.", e.message);
  }
  state.campaigns = groupCampaigns(state.registry);
  // Pre-load annotations for every indexed run to build the trash map.
  // The Trash rail count and the Trash view both need the full cross-run picture.
  // Failures per-run are tolerated (become {}).
  const indexed = state.registry.filter(r => r.indexed);
  const annResults = await Promise.all(
    indexed.map(r => api.annotations(r.run_id).catch(() => ({})))
  );
  indexed.forEach((r, i) => {
    state.trash[r.run_id] = (annResults[i] || {}).trashed || {};
  });
  renderRail();
  window.addEventListener("hashchange", route);
  route();
  setInterval(checkChanges, 2500);
}

function groupCampaigns(rows) {
  const bySubject = new Map();
  for (const r of rows) {
    const key = r.subject || "(unlabelled)";
    if (!bySubject.has(key)) bySubject.set(key, []);
    bySubject.get(key).push(r);
  }
  return [...bySubject.entries()]
    .map(([subject, runs]) => ({ subject, runs: runs.sort((a, b) => {
      // Primary: tool (ascending) so all Reviews runs group together, then Search, then Ads.
      // Secondary: run_seq descending so the newest run of each tool sits first in the group.
      // run_seq is the server's authoritative "newest" marker; never re-derive order here.
      const t = a.tool.localeCompare(b.tool);
      if (t !== 0) return t;
      return (b.run_seq ?? -1) - (a.run_seq ?? -1);
    })}))
    .sort((a, b) => a.subject.localeCompare(b.subject));
}

/* ============================================================================
   FRAME — persistent ink rail + swappable region
   ========================================================================== */
function buildFrame() {
  document.body.innerHTML = `
    <div id="app">
      <nav class="rail" id="rail"></nav>
      <div class="region" id="region"></div>
    </div>`;
}

function grainSVG() {
  // inline film grain (no external asset). feTurbulence dusts the ink surfaces.
  return `<svg class="grain" xmlns="http://www.w3.org/2000/svg" aria-hidden="true">
    <filter id="mvGrain"><feTurbulence type="fractalNoise" baseFrequency="0.9" numOctaves="2" stitchTiles="stitch"/></filter>
    <rect width="100%" height="100%" filter="url(#mvGrain)"/></svg>`;
}

function renderRail() {
  const railEl = $("#rail");
  const collapsed = lsGet("mv.rail", false);

  if (collapsed) {
    railEl.className = "rail rail-collapsed";
    railEl.innerHTML = `
      <div style="padding:12px 0;display:flex;flex-direction:column;align-items:center">
        <button class="iconbtn" onclick="toggleRail()" aria-expanded="false"
                title="Show the projects panel" style="width:36px;height:36px;font-size:16px">›</button>
      </div>`;
    return;
  }

  railEl.className = "rail";
  const totalRuns = state.registry.length;
  const trashTotal = Object.values(state.trash).reduce((n, m) => n + Object.keys(m).length, 0);
  const trashActive = location.hash === "#/trash";
  const tipsOn = lsGet("mv.tips", true);
  const trashCanSVG = `<svg width="12" height="12" viewBox="0 0 14 14" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M2 3.5h10M5 3.5V2h4v1.5M3.5 3.5l.7 8h5.6l.7-8"/></svg>`;
  const camps = state.campaigns.map(c => {
    const active = c.subject === decodeURIComponent((location.hash.match(/#\/campaign\/(.+)$/) || [])[1] || "");
    const runsHTML = c.runs.map(r => railRunRow(r)).join("");
    return `
      <div style="margin-bottom:4px">
        <a href="#/campaign/${encodeURIComponent(c.subject)}" title="${esc(c.subject)}"
           class="rail-camp${active ? " on" : ""}"
           style="display:flex;align-items:flex-start;gap:8px;padding:9px 20px;text-decoration:none">
          <span style="width:6px;height:6px;border-radius:2px;background:var(--ember);flex-shrink:0;margin-top:6px"></span>
          <span style="font-family:var(--font-head);font-weight:600;font-size:14px;color:var(--ink);letter-spacing:-.01em;min-width:0;line-height:1.3;overflow-wrap:anywhere">${esc(c.subject)}</span>
          <span class="num" style="margin-left:auto;font-size:11px;color:var(--muted);flex-shrink:0;margin-top:2px">${c.runs.length}</span>
        </a>
        <div>${runsHTML}</div>
      </div>`;
  }).join("");

  railEl.innerHTML = `
    <div style="padding:22px 20px 18px;border-bottom:1px solid var(--line);display:flex;align-items:flex-start;gap:8px">
      <a href="#/" title="Back to all projects" style="display:block;text-decoration:none;min-width:0">
        <div class="eb" style="color:var(--ember);margin-bottom:6px">${esc(APP_SUBTITLE)}</div>
        <div class="hl" style="font-size:22px;font-weight:700;color:var(--ink)">${esc(APP_NAME)}</div>
      </a>
      <button class="iconbtn" onclick="toggleRail()" aria-expanded="true"
              title="Hide the projects panel" style="margin-left:auto;flex-shrink:0;font-size:16px">‹</button>
    </div>
    <div style="flex-grow:1;overflow-y:auto;padding:14px 0 0">
      <div class="eb" style="padding:0 20px 8px" title="Each project groups the runs made for one subject">Projects · ${state.campaigns.length}</div>
      ${camps || `<div style="padding:0 20px;color:var(--muted);font-size:13px">No runs discovered.</div>`}
      <div style="margin-top:10px;border-bottom:1px solid var(--line)">
        <div style="border-top:1px solid var(--line);padding:6px 8px">
          <a href="#/trash"
             title="Records you moved out of the working view. Nothing is ever deleted; restore any item from here."
             class="rail-camp${trashActive ? " on" : ""}"
             style="display:flex;align-items:center;gap:9px;padding:9px 12px;text-decoration:none">
            <span style="color:var(--muted-2);flex-shrink:0;display:flex">${trashCanSVG}</span>
            <span style="font-family:var(--font-head);font-weight:600;font-size:13.5px;color:var(--ink);letter-spacing:-.01em">Trash</span>
            ${trashTotal ? `<span class="num" style="margin-left:auto;font-size:11px;color:var(--muted);flex-shrink:0">${num(trashTotal)}</span>` : ""}
          </a>
        </div>
        <div style="padding:10px 14px;border-top:1px solid var(--line);display:flex;align-items:center;gap:10px">
          <span class="num" style="font-size:11px;color:var(--muted)" title="Every run folder found in the working folder, indexed or not, including scheduled months that did not run">${num(totalRuns)} run${totalRuns === 1 ? "" : "s"} on disk</span>
          <span style="flex-grow:1"></span>
          <button role="switch" aria-checked="${tipsOn ? "true" : "false"}"
                  class="pbtn tip-switch" onclick="toggleTips()"
                  title="Explanations when you hover over labels and numbers. Turn off once you know the app."
                  ><span class="tip-track" aria-hidden="true"><span class="tip-knob"></span></span><span class="tip-label">${tipsOn ? "Tooltips on" : "Tooltips off"}</span></button>
        </div>
      </div>
    </div>
    ${promoBlock()}`;
}

function toggleRail() {
  lsSet("mv.rail", !lsGet("mv.rail", false));
  renderRail();
}

function railRunRow(r) {
  const activeRun = decodeURIComponent((location.hash.match(/#\/run\/([^?]+)/) || [])[1] || "");
  const on = r.run_id === activeRun;
  const badge = TOOL_LABEL[r.tool] || r.tool;
  const cnt = r.counts && (r.counts.records != null) ? r.counts.records : null;
  const dim = r.indexed ? "" : "opacity:.5";
  const dateLabel = runLabel(r);
  // A run that has an index but whose own notification says it did not finish: the project
  // page's chip for it explains that, so the rail row gives the same words instead of the
  // folder name. monthFinished is worked out as the project page does it:
  // whether any other month of this tool in the project finished. Every other row keeps
  // the folder name as its tip.
  let rowTip = r.run_id;
  const stopped = runEndStatus(r);
  if (r.indexed && stopped) {
    const camp = state.campaigns.find(c => c.runs.some(x => x.run_id === r.run_id));
    const monthFinished = camp ? camp.runs.some(x => x.tool === r.tool && x.indexed && !runEndStatus(x)) : true;
    rowTip = chipTitle(true, false, badge, stopped, monthFinished);
  }
  return `
    <a href="#/run/${encodeURIComponent(r.run_id)}" title="${esc(rowTip)}"
       style="display:flex;align-items:center;gap:9px;padding:6px 20px 6px 34px;text-decoration:none;${dim}
              ${on ? "background:var(--ember-tint);border-left:2px solid var(--ember);padding-left:32px" : ""}">
      <span class="eb" style="font-size:10px;color:var(--ember);width:58px;flex-shrink:0;white-space:nowrap">${esc(badge)}</span>
      <span style="font-size:12.5px;color:var(--ink-soft);min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${esc(dateLabel)}</span>
      ${cnt != null ? `<span class="num" style="margin-left:auto;font-size:11px;color:var(--muted)" title="${esc(RAIL_COUNT_TIP)}">${num(cnt)}</span>`
                    : `<span class="eb" style="margin-left:auto;font-size:9px;color:var(--muted-2)" title="${esc(chipTitle(false, false, badge, runEndStatus(r)))}">${esc(railStatusLabel(r))}</span>`}
    </a>`;
}
const shortRun = (id) => { const b = id.split("/").pop(); const m = b.match(/_(\d{4}-\d{2}-\d{2})$/); return m ? m[1] : b; };
// Single source of truth for the human-readable run label shown in the rail,
// dossier chips, and attribution lines. When two runs share a date the version
// suffix ("2026-09-28 · v2") makes them distinguishable. Falls back to the
// folder-name date fragment for older rows that have no run_date field.
const runLabel = (r) => {
  if (!r) return "";
  return r.run_date
    ? (r.run_version > 1 ? r.run_date + " · v" + r.run_version : r.run_date)
    : shortRun(r.run_id || "");
};

/* ============================================================================
   ROUTER
   ========================================================================== */
// The filters live in the address too (#/run/<id>?platforms=maps&q=price&view=...), so a
// view can be reopened, and the agent can open the app on exactly the records it means
// (viewer_ctl.py url builds these). `source=` is the older dossier chip link. Unknown
// params are ignored; values a run does not have are ignored by applyRunOpts.
const URL_SET_KEYS = ["platforms", "formats", "advertisers", "durations", "polarities", "subreddits"];
function parseRunOpts(qs) {
  if (!qs) return {};
  const opts = {};
  try {
    const p = new URLSearchParams(qs);
    URL_SET_KEYS.forEach(k => { const v = p.getAll(k); if (v.length) opts[k] = v; });
    const src = p.get("source");
    if (src) opts.platforms = (opts.platforms || []).concat(src);
    if (p.get("q")) opts.q = p.get("q").trim().toLowerCase();
    if (p.get("fav") === "1") opts.favOnly = true;
    if (p.get("view")) opts.view = p.get("view");
    const ch = p.get("change"); if (ch === "new" || ch === "changed" || ch === "gone") opts.change = ch;
    if (p.get("vs")) opts.vs = p.get("vs");
  } catch { /* malformed query -> no preset */ }
  return opts;
}
// Preset filters from the address. Only values present in this run are honoured, so a
// stale or mistyped link degrades to no filter rather than an empty page.
function applyRunOpts(opts, avail) {
  URL_SET_KEYS.forEach(k => (opts[k] || []).forEach(v => {
    if (state.filters[k] && (avail[k] || []).includes(v)) state.filters[k].add(v);
  }));
  if (opts.q) state.filters.q = opts.q;
  if (opts.favOnly) state.filters.favOnly = true;
  if (opts.view && (state.views || []).some(v => v.id === opts.view)) state.filters.view = opts.view;
  if (opts.change) state.filters.change = opts.change;
  if (opts.vs) state.filters.vs = opts.vs;
}
// Mirror the filters into the address without a navigation (replaceState fires no
// hashchange), so the address always reopens what is on screen.
function syncFilterURL() {
  if (!state.runId) return;
  const f = state.filters, p = new URLSearchParams();
  URL_SET_KEYS.forEach(k => (f[k] ? [...f[k]] : []).forEach(v => p.append(k, v)));
  if (f.q) p.set("q", f.q);
  if (f.favOnly) p.set("fav", "1");
  if (f.view) p.set("view", f.view);
  if (f.change) p.set("change", f.change);
  if (f.vs && f.change) p.set("vs", f.vs);   // The other month of the comparison, on every change link
  const qs = p.toString();
  const h = "#/run/" + encodeURIComponent(state.runId) + (qs ? "?" + qs : "");
  if (location.hash !== h) history.replaceState(null, "", h);
}
function route() {
  const h = location.hash || "#/";
  const region = $("#region");
  // Every navigation invalidates the previous surface's repaint closure. Surfaces that
  // own one re-establish it via wireFilterRail; surfaces without one (generic run,
  // trash, reports) must NOT inherit a stale closure over a detached DOM -- callers
  // fall back to route() or their own re-render instead.
  window._repaint = null;
  state.visible = null;
  reportView();
  let m;
  // #/run/<id> with an optional ?source=<platform> deep-link (from the dossier's
  // per-source count chips). [^?]+ keeps slash-containing run ids intact; the query is
  // split off and parsed separately.
  if ((m = h.match(/^#\/run\/([^?]+)(?:\?(.*))?$/))) return openRun(decodeURIComponent(m[1]), parseRunOpts(m[2]));
  if ((m = h.match(/^#\/campaign\/(.+)$/))) return openCampaign(decodeURIComponent(m[1]));
  if ((m = h.match(/^#\/report\/(.+?)\/\/(.+)$/))) return openReport(decodeURIComponent(m[1]), decodeURIComponent(m[2]));
  if (h === "#/trash") return renderTrash();
  if (h === "#/innomaker") return renderInnomaker();
  renderOverview();
  syncRailActive();
}

function syncRailActive() { renderRail(); }

/* ============================================================================
   OVERVIEW  (Main artboard — the whole-picture entry)
   ========================================================================== */
function renderOverview() {
  syncRailActive();
  const region = $("#region");
  const totals = tally(state.registry);
  if (!state.registry.length) return renderEmpty();

  const cards = state.campaigns.map(c => {
    const tools = new Set(c.runs.map(r => r.tool));
    const chips = [...tools].map(t => `<span class="tag">${esc(TOOL_LABEL[t] || t)}</span>`).join("");
    const recs = c.runs.reduce((n, r) => n + ((r.counts && r.counts.records) || 0), 0);
    return `
      <a href="#/campaign/${encodeURIComponent(c.subject)}" class="card" title="Open the ${esc(c.subject)} dossier"
         style="padding:22px;text-decoration:none;gap:14px;transition:border-color .12s"
         onmouseover="this.style.borderColor='var(--ink)'" onmouseout="this.style.borderColor='var(--line-strong)'">
        <div class="eb">${c.runs.length} run${c.runs.length === 1 ? "" : "s"}</div>
        <div class="hl" style="font-size:26px;font-weight:700">${esc(c.subject)}</div>
        <div style="display:flex;gap:6px;flex-wrap:wrap;margin-top:auto">${chips}</div>
        <div class="num" style="font-size:12px;color:var(--muted)">${num(recs)} record${recs === 1 ? "" : "s"} indexed</div>
      </a>`;
  }).join("");

  region.innerHTML = `
    <section class="stage">
      ${grainSVG()}
      <div class="masthead">
        <div>
          <div class="eb" style="color:var(--ember);margin-bottom:7px" title="The viewer reads files on this computer only. Nothing is uploaded.">${esc(APP_SUBTITLE)} · reading your working folder</div>
          <h1 class="hl" style="font-size:38px;font-weight:700;color:var(--ink)">Everything you pulled, in one place.</h1>
        </div>
      </div>
      <div class="stage-scroll" style="padding:32px">
        <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(130px,1fr));gap:36px;padding:4px 0 30px;border-bottom:1px solid var(--line);margin-bottom:34px">
          ${ledgerCell(totals.campaigns, "projects", "Each group of runs sharing the same subject.")}
          ${ledgerCell(totals.runs, "runs on disk", "Every run folder found in the working folder, indexed or not, including scheduled months that did not run.")}
          ${ledgerCell(totals.records, "records indexed", "Individual reviews, ads, or search results across all indexed runs.")}
          ${ledgerCell(totals.creatives, "creatives downloaded", "Image and video files saved from ad scrapes across all runs.")}
        </div>
        <div class="eb" style="margin-bottom:16px">Projects</div>
        <div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:18px">${cards}</div>
      </div>
    </section>`;
}
function ledgerCell(n, label, tip) {
  return `<div${tip ? ` title="${esc(tip)}"` : ""}>
    <div class="num" style="font-family:var(--font-head);font-weight:700;font-size:46px;line-height:1;letter-spacing:-.02em;color:var(--ink)">${num(n)}</div>
    <div class="eb" style="margin-top:10px;color:var(--muted)">${esc(label)}</div></div>`;
}
function tally(rows) {
  let records = 0, creatives = 0;
  for (const r of rows) {
    if (r.counts) { records += r.counts.records || 0; creatives += r.counts.creatives_on_disk || 0; }
  }
  return { campaigns: state.campaigns.length, runs: rows.length, records, creatives };
}

/* ============================================================================
   OPEN A RUN — dispatch to the surface for its tool
   ========================================================================== */
// Which two months the compare call is made for, from the link's change= and vs=.
// gone: the page is the baseline and vs is the newer run, so compare
// vs against this page. new and changed: the page is the run and vs is its baseline,
// so the counts are made against the month the link names and the app never picks
// its own. No change=, or no vs=: the run against its automatic baseline, as before.
// Pure function.
function compareArgsFor(runId, change, vs) {
  if (change === "gone" && vs) return { run: vs, against: runId };
  return { run: runId, against: change && vs ? vs : null };
}
// What is wrong with the comparison a change link asked for, or null when
// nothing is. WHY: when the month a link names (vs=) is gone, or the compare call fails,
// there are no uids to filter by, and the page used to show every record under
// "Showing: new" with no warning. change is the link's change=, vs its vs=, cmp the
// compare result (null when the call failed), err the error it threw (null otherwise).
// "missing": the server answered 404 for a run it was given (or found no such month);
// "none": no month to compare with at all; "failed": any other failure.
function compareProblem(change, vs, cmp, err) {
  if (!change) return null;
  if (err) {
    const msg = (err.info && err.info.error) || "";
    return vs && /not found/.test(msg) ? { kind: "missing", vs } : { kind: "failed" };
  }
  if (!cmp) return { kind: "failed" };
  if (!cmp.against) return vs ? { kind: "missing", vs } : { kind: "none" };
  return null;
}
// cmp (optional) is the automatic comparison shown beside the sentence. When it names
// earlier months that stopped early (skipped_partial), "no earlier month" would sit next
// to a line that names one, so the sentence says none finished instead;
// compare.py says the same for the same case (_skipped_sentence, _month_sizes_no_baseline).
function compareProblemText(problem, labelOf, cmp) {
  if (!problem) return "";
  if (problem.kind === "missing") {
    return `The month this link compares with (${labelOf(problem.vs)}) is no longer available, so no change filter is applied and every record is shown.`;
  }
  if (problem.kind === "none") {
    if (cmp && (cmp.skipped_partial || []).length) {
      return "No earlier month finished, so there is nothing to compare with. No change filter is applied and every record is shown.";
    }
    return "There is no earlier month to compare with, so no change filter is applied and every record is shown.";
  }
  return "The comparison could not be read, so no change filter is applied and every record is shown.";
}
// A comparison made against a month the link named (vs=) is "a selected run" to
// the server, even when that month is the one the app would have chosen itself. The
// link's own chips always name it, so the strip would call every clicked chip "a
// selected run". When the automatic comparison (auto) names the same month, the basis,
// and the stopped months it passed over, are the automatic ones.
function withAutoBasis(cmp, auto) {
  if (!cmp || !auto || cmp.basis !== "explicit" || !auto.against || auto.against !== cmp.against) return cmp;
  return { ...cmp, basis: auto.basis, ...(auto.skipped_partial ? { skipped_partial: auto.skipped_partial } : {}) };
}
// The sentences that say a comparison rests on a month that stopped early
// (compare.py: run_partial, skipped_partial). labelOf turns a run id into its label.
function compareCaveats(cmp, labelOf) {
  const out = [];
  if (!cmp) return out;
  const skipped = (cmp.skipped_partial || []).map(labelOf);
  const many = skipped.length > 1;
  if (skipped.length && !cmp.against) {
    out.push(`The earlier month${many ? "s" : ""} (${skipped.join(", ")}) stopped before ${many ? "they" : "it"} finished, so ${many ? "none is" : "it is not"} used for comparison. You can still compare with ${many ? "one of them" : "it"} by asking for it by name.`);
  } else if (skipped.length) {
    out.push(`${skipped.join(" and ")} stopped before ${many ? "they" : "it"} finished, so ${many ? "they were" : "it was"} not used for comparison. This month is compared with ${labelOf(cmp.against)} instead.`);
  }
  if (cmp.against && cmp.run_partial) {
    out.push("This month stopped before it finished, so it holds only part of its records. A record it does not have may simply not have been collected, so read the not-in-this-run count with that in mind.");
  }
  // A step that did not finish in either month, said by compare.py (key
  // "unfinished_note", present only then); repeated as written.
  if (cmp.unfinished_note) out.push(cmp.unfinished_note);
  return out;
}
async function openRun(runId, opts = {}) {
  state.runId = runId;
  state.filters = {};
  syncRailActive();
  renderLoading("Reading " + shortRun(runId));
  const row = state.registry.find(r => r.run_id === runId);
  if (row && !row.indexed) return runEndStatus(row) ? renderRunEnded(runId, row) : renderNotIndexed(runId, row);
  // Baseline change stamps BEFORE reading, so a write that lands while this run loads is
  // still seen as a change by checkChanges.
  try { state.stamps = { run: runId, ...(await api.changes(runId)) }; } catch { state.stamps = null; }
  try {
    state.index = await api.index(runId);
  } catch (e) {
    if (e.info && e.info.error === "run not indexed") return runEndStatus(row) ? renderRunEnded(runId, row) : renderNotIndexed(runId, row);
    return renderFatal("Could not read this run.", e.message);
  }
  try { state.ann = normalizeAnn(await api.annotations(runId)); } catch { state.ann = { favorites: {}, notes: {}, trashed: {} }; }
  try { state.views = (await api.views(runId)).views || []; } catch { state.views = []; }
  // Keep the global trash map in sync with the freshly-loaded run.
  state.trash[runId] = state.ann.trashed;
  // Load carry marker (server triggers ensure_carried on /api/annotations above).
  try { state.carried = await api.carried(runId); } catch { state.carried = null; }
  // Reset compare state on every navigation. Compare data is loaded asynchronously below,
  // after the surface renders, so the initial paint is not delayed.
  state.compare = null;
  state.compareNote = null;

  const tool = state.index.tool || (row && row.tool);
  let surfaceRendered;
  if (tool === "ad") surfaceRendered = renderAdLibrary(opts);
  else if (tool === "pain") surfaceRendered = renderReviewWall(opts);
  else if (tool === "search") surfaceRendered = renderSearchLandscape(opts);
  else surfaceRendered = renderGenericRun();

  // Load compare data asynchronously so the strip appears without blocking the surface.
  // When change= is active we need the data immediately for filtering (handled in paint* fns
  // which call changeUids() -- they read state.compare after it resolves, then repaint).
  const _compareRunId = runId;
  const _compareOpts = opts;
  (async () => {
    if (state.runId !== _compareRunId) return;  // navigated away
    // change=gone: compare run=vs (newer run) against=this run, to get the gone uids.
    // change=new|changed: vs= is this run's baseline, so the counts are
    // made against the month the link names and the app never picks its own.
    const vsRun = (_compareOpts.vs || (state.filters && state.filters.vs)) || null;
    const changeNow = _compareOpts.change || (state.filters && state.filters.change) || "";
    const cmpArgs = compareArgsFor(_compareRunId, changeNow, vsRun);
    let cmpGot = null, cmpErr = null;
    try { cmpGot = await api.compare(cmpArgs.run, cmpArgs.against); } catch (e) { cmpErr = e; }
    if (state.runId !== _compareRunId) return;
    // A link whose comparison month is gone (or whose compare call failed) has
    // no uids to filter by. Say so, drop the change filter, and show the automatic
    // comparison instead of every record under "Showing: new".
    const problem = compareProblem(changeNow, vsRun, cmpGot, cmpErr);
    if (problem) {
      state.compareNote = problem;
      state.filters.change = ""; state.filters.vs = "";
      const chip = document.getElementById("changefilterchip");
      if (chip) chip.remove();
      cmpGot = null;
      try { cmpGot = await api.compare(_compareRunId, null); } catch { cmpGot = null; }
      if (state.runId !== _compareRunId) return;
    } else if (cmpGot && cmpArgs.against && cmpArgs.run === _compareRunId) {
      // A clicked chip names the month it was counted against: when that is the month
      // the app would have chosen itself, keep the automatic wording.
      let auto = null;
      try { auto = await api.compare(_compareRunId, null); } catch { auto = null; }
      if (state.runId !== _compareRunId) return;
      cmpGot = withAutoBasis(cmpGot, auto);
    }
    state.compare = cmpGot;
    paintCompareStrip();
    paintCarriedNotice();
    // Repaint the surface list if a change filter is active (now we have the uids)
    if (window._repaint && (state.filters.change || problem)) window._repaint();
    // U1: for search, route to the tab that contains the changed/new records.
    if (state.index && state.index.tool === "search" && state.filters.change && state.compare) {
      const byPlat = state.compare.by_platform || {};
      const cat = state.filters.change;
      const hasSerpChange = ((byPlat.google_serp || {})[cat] || []).length > 0;
      const hasKwChange = ((byPlat.semrush || {})[cat] || []).length > 0;
      const targetSeg = hasSerpChange && !hasKwChange ? "serp"
        : hasKwChange && !hasSerpChange ? "keyword"
        : null;
      if (targetSeg) {
        document.querySelectorAll("[data-seg]").forEach(x => x.classList.remove("on"));
        const targetBtn = document.querySelector(`[data-seg="${targetSeg}"]`);
        if (targetBtn) targetBtn.classList.add("on");
        const bodyFns = { serp: searchSerps, keyword: searchKeywords };
        const body = $("#searchBody");
        if (body && bodyFns[targetSeg]) body.innerHTML = bodyFns[targetSeg]();
      }
    }
  })();

  return surfaceRendered;
}
function normalizeAnn(a) {
  a = a || {};
  return { favorites: a.favorites || {}, notes: a.notes || {}, trashed: a.trashed || {} };
}

/* ---- carried notice ------------------------------------------- */
// A dismissible notice shown when a run received carried marks (or a carry error)
// on its first read. Dismissed per-session via sessionStorage. Shows a block-level
// details list of items that could not be carried (human-readable text).
function paintCarriedNotice() {
  const el = document.getElementById("carriednotice");
  if (!el) return;
  const c = state.carried;

  // Carry exception path -- no marks were carried, but the reason is known.
  if (c && c.error && !c.carried) {
    const key = "carried-err-dismissed-" + (state.runId || "");
    try { if (sessionStorage.getItem(key)) { el.innerHTML = ""; return; } } catch {}
    const prevRow = (state.registry || []).find(r => r.run_id === c.carried_from);
    const fromLabel = prevRow ? esc(runLabel(prevRow))
      : (c.carried_from ? esc(shortRun(c.carried_from)) : "previous run");
    const dismiss = `(function(el,key){try{sessionStorage.setItem(key,'1')}catch{}el.closest('.carriednotice').parentElement.innerHTML='';})(this,'${key.replace(/'/g, "\\'")}')`;
    el.innerHTML = `<div class="carriednotice">
      <div class="carriednotice-row1">Your marks from ${fromLabel} could not be carried: ${esc(c.error_plain || c.error)}
      <button class="carriednotice-dismiss" title="Dismiss" onclick="${dismiss}">x</button></div>
    </div>`;
    return;
  }

  const totalCarried = (c && c.carried)
    ? ((c.favorites_carried || 0) + (c.notes_carried || 0)
       + (c.trash_carried || 0) + (c.views_carried || 0))
    : 0;
  if (!c || !c.carried || totalCarried === 0) { el.innerHTML = ""; return; }
  const key = "carried-dismissed-" + (state.runId || "");
  try { if (sessionStorage.getItem(key)) { el.innerHTML = ""; return; } } catch {}

  // Use runLabel (date + optional version suffix) instead of raw run_id.
  const prevRow = (state.registry || []).find(r => r.run_id === c.carried_from);
  const fromLabel = prevRow ? esc(runLabel(prevRow))
    : (c.carried_from ? esc(shortRun(c.carried_from)) : "previous run");

  const parts = [];
  if (c.favorites_carried) parts.push(c.favorites_carried + " favorite" + (c.favorites_carried === 1 ? "" : "s"));
  if (c.notes_carried) parts.push(c.notes_carried + " note" + (c.notes_carried === 1 ? "" : "s"));
  if (c.trash_carried) parts.push(c.trash_carried + " hidden");
  if (c.views_carried) parts.push(c.views_carried + " saved view" + (c.views_carried === 1 ? "" : "s"));

  const skippedTotal = ((c.skipped || []).length + (c.skipped_views || []).length);
  let skippedHtml = "";
  if (skippedTotal > 0) {
    // Human-readable skip text using kind labels, text snippets, and reason mapping.
    const KIND_LABEL = {favorites: "Favorite", notes: "Note", trashed: "Hidden record"};
    const REASON_LABEL = {
      "record not in this run": "not in this run",
      "no stable identity": "could not be matched (older data)",
      "identity appears more than once in the previous run": "matched more than one record in the previous run",
      "identity appears more than once in this run": "matched more than one record in this run",
      "existing mark in this run takes precedence": "this run already has your own mark",
    };
    const VIEW_REASON_LABEL = {
      "a view with this title already exists in this run": "this run already has a view with that name",
      "no records from this view are in this run": "none of its records are in this run",
    };
    const items = [...(c.skipped || []), ...(c.skipped_views || [])].slice(0, 30).map(s => {
      const isView = !s.kind || s.kind === "view";
      if (isView) {
        return `<li>View “${esc(s.title || s.view_id || "?")}”: ${esc(VIEW_REASON_LABEL[s.reason] || s.reason || "")}</li>`;
      }
      const kindLabel = KIND_LABEL[s.kind] || esc(s.kind);
      const snip = s.text ? ` “${esc(s.text.length > 60 ? s.text.slice(0, 60) + "…" : s.text)}”` : "";
      const reasonLabel = REASON_LABEL[s.reason] || esc(s.reason || "");
      return `<li>${kindLabel}${snip}: ${reasonLabel}</li>`;
    }).join("");
    // display:block so the details expands on its own full-width line below the sentence.
    skippedHtml = `<details class="carriednotice-details"><summary>${skippedTotal} could not be carried</summary><ul>${items}</ul></details>`;
  }

  const dismiss = `(function(el,key){try{sessionStorage.setItem(key,'1')}catch{}el.closest('.carriednotice').parentElement.innerHTML='';})(this,'${key.replace(/'/g, "\\'")}')`;
  // Two-row layout -- sentence + dismiss button on row1; details on its own block row.
  el.innerHTML = `<div class="carriednotice">
    <div class="carriednotice-row1">Carried ${parts.join(", ")} from ${fromLabel}.
    <button class="carriednotice-dismiss" title="Dismiss" onclick="${dismiss}">x</button></div>
    ${skippedHtml}
  </div>`;
}

/* ---- compare strip -------------------------------------------- */
// The compare strip is a thin banner above the grid/wall/keywords showing
// "Since [baseline label]: N new · M changed · P not in this run".
// It relies entirely on /api/compare data in state.compare -- never recomputes.

function changeUids() {
  // Return the Set of uids to show for the active change filter, or null if no filter.
  // Used by paintAds / paintReviews / searchKeywords / searchSerps.
  if (!state.compare || !state.filters) return null;
  const cat = state.filters.change;
  if (!cat || (cat !== "new" && cat !== "changed" && cat !== "gone")) return null;
  const uids = new Set();
  for (const plat of Object.values(state.compare.by_platform || {})) {
    for (const entry of (plat[cat] || [])) { if (entry.uid) uids.add(entry.uid); }
  }
  return uids;
}

// U6: look up the compare entry for a uid so card renderers can show what changed.
// Returns the first matching {uid, changed_fields?, label?} entry from any platform, or null.
function changeEntryForUid(uid) {
  if (!state.compare) return null;
  for (const plat of Object.values(state.compare.by_platform || {})) {
    for (const cat of ["changed", "new", "gone"]) {
      const entry = (plat[cat] || []).find(e => e.uid === uid);
      if (entry) return { ...entry, _cat: cat };
    }
  }
  return null;
}

// U6: render a small change badge for a record, e.g. "stopped running" or changed field names.
function changeBadgeHTML(uid) {
  const entry = changeEntryForUid(uid);
  if (!entry || entry._cat !== "changed") return "";
  const label = entry.label;
  const fields = entry.changed_fields || [];
  if (label) return `<span class="tag" style="background:var(--ember-tint);color:var(--ember-press);font-size:10px" title="This record changed: ${esc(label)}">${esc(label)}</span>`;
  if (fields.length) return `<span class="tag" style="background:var(--ember-tint);color:var(--ember-press);font-size:10px" title="Changed fields: ${esc(fields.join(', '))}">${fields.length === 1 ? esc(fields[0]) + " changed" : fields.length + " fields changed"}</span>`;
  return "";
}

function paintCompareStrip() {
  const el = document.getElementById("comparestrip");
  if (!el) return;
  const cmp = state.compare;
  const f = state.filters || {};
  // The sentences about a comparison that cannot be made, or rests on a month
  // that stopped early. They show even when there is no baseline to put counts against.
  const labelOf = (id) => { const row = state.registry.find(r => r.run_id === id); return row ? runLabel(row) : id; };
  const rowsHTML = (texts) => {
    const rows = texts.filter(Boolean).map(t => `<span class="cmpstrip-platrow"><span class="cmpstrip-plat" style="font-style:italic">${esc(t)}</span></span>`);
    return rows.length ? `<div class="cmpstrip-platlist">${rows.join("")}</div>` : "";
  };
  const notesHTML = rowsHTML([compareProblemText(state.compareNote, labelOf, cmp), ...compareCaveats(cmp, labelOf)]);
  if (!cmp || !cmp.against) { el.innerHTML = notesHTML ? `<div class="cmpstrip">${notesHTML}</div>` : ""; return; }

  // Find the baseline row in the registry so we can show its label.
  const baselineId = cmp.against;
  const baselineRow = state.registry.find(r => r.run_id === baselineId);
  const baseLabel = baselineRow ? runLabel(baselineRow) : baselineId;

  const t = cmp.totals || {};
  const runId = state.runId;
  const activeChange = f.change || "";
  // For gone: the URL opens the baseline run with vs= set to this run
  const goneHash = "#/run/" + encodeURIComponent(baselineId) + "?change=gone&vs=" + encodeURIComponent(runId);
  // New and changed links carry the baseline these counts were made against.
  const baseVs = "&vs=" + encodeURIComponent(baselineId);

  // E3 (U4): In gone mode on the baseline page, show a count + link back.
  if (activeChange === "gone" && f.vs) {
    const newerRow = state.registry.find(r => r.run_id === f.vs);
    const newerLabel = newerRow ? runLabel(newerRow) : f.vs;
    const goneTotal = cmp ? (cmp.totals || {}).gone || 0 : 0;
    const countText = goneTotal === 1 ? "1 record" : `${num(goneTotal)} records`;
    const backHash = "#/run/" + encodeURIComponent(f.vs);
    // Here cmp.run is the NEWER run; if it stopped early, the records "not in"
    // it may simply not have been collected.
    const goneNotesHTML = rowsHTML([compareProblemText(state.compareNote, labelOf), cmp.run_partial
      ? `${newerLabel} stopped before it finished, so it holds only part of its records. A record it does not have may simply not have been collected, so this count may only be a gap.` : ""]);
    el.innerHTML = `<div class="cmpstrip"><span class="cmpstrip-gone-banner">${countText} were in ${esc(baseLabel)} but not in <a href="${esc(backHash)}" class="cmpstrip-link">${esc(newerLabel)}</a>.</span>${goneNotesHTML}</div>`;
    return;
  }

  function catLink(cat, count, label) {
    if (count === 0) return `<span class="cmpstrip-zero">${num(count)} ${label}</span>`;
    const isActive = activeChange === cat;
    const clearHash = "#/run/" + encodeURIComponent(runId);
    const catHash = cat === "gone" ? goneHash : "#/run/" + encodeURIComponent(runId) + "?change=" + cat + baseVs;
    // E4b: grammatically correct tooltip for each category.
    const catTitles = {
      gone: "the records that are not in this run",
      new: "new records",
      changed: "changed records",
    };
    const friendlyName = catTitles[cat] || (label + " records");
    const titleText = isActive
      ? `Showing only ${friendlyName}. Click to clear.`
      : `Show only ${friendlyName}`;
    const href = isActive ? clearHash : catHash;
    return `<a href="${esc(href)}" title="${esc(titleText)}" class="cmpstrip-link${isActive ? " cmpstrip-active" : ""}">${num(count)} ${label}</a>`;
  }

  // Platforms where this category is suppressed by the positional-uid guard.
  // The headline total doesn't include these platforms, so we annotate the chip
  // with their names so the user knows the count may be incomplete.
  const cannotTellGonePlatforms = Object.entries(cmp.by_platform || {})
    .filter(([, p]) => (p.cannot_tell || []).includes("gone"))
    .map(([plat]) => platLabel(plat));
  const cannotTellNewPlatforms = Object.entries(cmp.by_platform || {})
    .filter(([, p]) => (p.cannot_tell || []).includes("new"))
    .map(([plat]) => platLabel(plat));
  function ncNote(platforms) {
    if (!platforms.length) return "";
    return ` <span class="cmpstrip-nc">(${esc(platforms.join(", "))} not counted)</span>`;
  }
  const notInThisRun = catLink("gone", t.gone || 0, "not in this run") + ncNote(cannotTellGonePlatforms);
  const newLink = catLink("new", t.new || 0, "new") + ncNote(cannotTellNewPlatforms);

  // U6: show "stopped running" count in the changed chip when present
  const stoppedCount = Object.values(cmp.by_platform || {}).reduce(
    (n, p) => n + (p.changed || []).filter(e => e.label === "stopped running").length, 0);
  const changedLabel = stoppedCount > 0
    ? `changed (${stoppedCount} stopped running)`
    : "changed";
  const changedLink = catLink("changed", t.changed || 0, changedLabel);

  // U5b: basis description text
  // A baseline chosen by hand that stopped before it finished holds only
  // part of its records; the strip says so instead of presenting the counts as plain.
  const basisText = cmp.against_partial
    ? "compared with a month that stopped before it finished, so it holds only part of its records"
    : cmp.basis === "repeat_of"
    ? "compared with the run this repeats"
    : cmp.basis === "same_project"
    ? "compared with the previous run of this project"
    : cmp.basis === "explicit"
    ? "compared with a selected run"
    : "";

  // U5a + E1: per-platform breakdown.
  // Include platforms that have any change counts OR a cannot_tell notice.
  const platRows = Object.entries(cmp.by_platform || {})
    .filter(([, p]) =>
      (p.new || []).length + (p.gone || []).length + (p.changed || []).length > 0
      || (p.shown_again || []).length + (p.set_aside_now || []).length > 0
      || (p.not_pulled_now || []).length + (p.not_pulled_before || []).length > 0
      || (p.domains_not_compared_now || []).length + (p.domains_not_compared_before || []).length > 0
      || (p.library_unknown_now || []).length + (p.library_unknown_before || []).length > 0
      || (p.library_said || []).length > 0
      || (p.cannot_tell || []).length > 0
    )
    .map(([plat, p]) => {
      const parts = [];
      if ((p.new || []).length) parts.push(`${num(p.new.length)} new`);
      if ((p.changed || []).length) parts.push(`${num(p.changed.length)} changed`);
      if ((p.gone || []).length) parts.push(`${num(p.gone.length)} not in this run`);
      // Ads that moved between shown and set aside (compare.py); never new or gone.
      if ((p.set_aside_now || []).length) parts.push(`${num(p.set_aside_now.length)} set aside in this run because the advertiser's name does not match the competitor (left out of the count of those not in this run)`);
      if ((p.shown_again || []).length) parts.push(`${num(p.shown_again.length)} set aside in ${baseLabel} and shown in this run (not counted as new)`);
      // (compare.py): a search keyword whose results one month pulled and the
      // other did not is neither new nor not in this run: the tool did not look.
      const npNow = (p.not_pulled_now || []).length, npBefore = (p.not_pulled_before || []).length;
      const kw = (n) => `${num(n)} ${n === 1 ? "keyword" : "keywords"}`;
      if (npNow) parts.push(`search results for ${kw(npNow)} pulled in ${baseLabel} but not in this run (nothing to compare)`);
      if (npBefore) parts.push(`search results for ${kw(npBefore)} pulled in this run but not in ${baseLabel} (nothing to compare)`);
      // (compare.py): the domains that rank on those keywords. A domain with no
      // ranking found on a keyword with search results in both runs is neither new nor not in this run.
      const dmNow = (p.domains_not_compared_now || []).length, dmBefore = (p.domains_not_compared_before || []).length;
      const dm = (n) => `${num(n)} ${n === 1 ? "domain" : "domains"}`;
      const dmThem = (n) => n === 1 ? "it" : "them";
      if (dmNow) parts.push(`${dm(dmNow)} from ${baseLabel} left out of the count of those not in this run (no ranking for ${dmThem(dmNow)} was found on a keyword with search results in both runs)`);
      if (dmBefore) parts.push(`${dm(dmBefore)} found in this run left out of the count of new ones (no ranking for ${dmThem(dmBefore)} was found on a keyword with search results in both runs)`);
      // (compare.py): the source's own library errored on a page in one run, so that
      // page's ads are neither new nor not in this run: the tool does not know.
      const luNow = (p.library_unknown_now || []).length, luBefore = (p.library_unknown_before || []).length;
      const adN = (n) => `${num(n)} ${n === 1 ? "ad" : "ads"}`;
      if (luNow) parts.push(`${adN(luNow)} from ${baseLabel} left out of the count of those not in this run (the ad library returned an error for ${luNow === 1 ? "its page" : "their pages"} in this run)`);
      if (luBefore) parts.push(`${adN(luBefore)} in this run left out of the count of new ones (the ad library returned an error for ${luBefore === 1 ? "its page" : "their pages"} in ${baseLabel})`);
      const countStr = parts.length ? parts.join(", ") : "";
      const notPulledWhy = (npNow || npBefore || dmNow || dmBefore) ? "Search results are pulled for a limited set of keywords, and which ones can change from month to month." : "";
      const cannotTellReason = p.cannot_tell_reason || "";
      const libraryWords = (p.library_said || []).join(" ");
      const body = [countStr, notPulledWhy, cannotTellReason, libraryWords].filter(Boolean).join("; ");
      return `<span class="cmpstrip-platrow"><span class="cmpstrip-plat">${esc(platLabel(plat))}:</span> ${esc(body)}</span>`;
    });

  // E2: platforms present in only one run get a plain explanatory line each.
  const onlyRows = (cmp.platforms_only_in_one_run || []).map(item => {
    const pLabel = platLabel(item.platform);
    // A run that found results here but set them all aside did collect them.
    const aside = item.set_aside_in_other_run;
    // A month with no search-results records at all says that, not "Google was
    // not collected" (the Google ads and the Google search results are different things).
    if (item.platform === "google_serp") {
      const t = item.in_run === "baseline"
        ? `Search results were pulled in ${baseLabel} but not in this run`
        : `Search results were pulled in this run but not in ${baseLabel}`;
      return `<span class="cmpstrip-platrow"><span class="cmpstrip-plat" style="font-style:italic">${esc(t)}</span></span>`;
    }
    // The other run's ad library answered with a status message instead of ads: it was
    // asked, so "not collected" would be false; say what it said (compare.py).
    if (Array.isArray(item.library_said) && item.library_said.length) {
      return `<span class="cmpstrip-platrow"><span class="cmpstrip-plat" style="font-style:italic">${esc(item.library_said.join(" "))}</span></span>`;
    }
    const text = item.in_run === "baseline"
      ? (aside ? `${pLabel} ads were collected in this run, but all ${num(aside)} were set aside because the advertiser's name does not match the competitor`
               : `${pLabel} was in ${baseLabel} but not collected in this run`)
      : (aside ? `${pLabel} ads are shown in this run; in ${baseLabel} all ${num(aside)} were set aside because the advertiser's name did not match the competitor`
               : `${pLabel} was collected in this run but not in ${baseLabel}`);
    return `<span class="cmpstrip-platrow"><span class="cmpstrip-plat" style="font-style:italic">${esc(text)}</span></span>`;
  });
  const allPlatRows = [...platRows, ...onlyRows];

  el.innerHTML = `<div class="cmpstrip">
    <span class="cmpstrip-label">Since ${esc(baseLabel)}:</span>
    ${newLink} <span class="cmpstrip-dot">·</span>
    ${changedLink} <span class="cmpstrip-dot">·</span>
    ${notInThisRun}
    ${t.not_comparable > 0 ? `<span class="cmpstrip-nc" title="Records without a stable identity that can be matched across runs. This is expected for older indexes.">${num(t.not_comparable)} not comparable</span>` : ""}
    ${cmp.settings_differ ? `<span class="cmpstrip-settings tag" title="Settings that differ from the previous run: ${esc((cmp.differing_settings_keys || []).join(", "))}">settings changed</span>` : ""}
    ${basisText ? `<span class="cmpstrip-basis">${esc(basisText)}</span>` : ""}
    ${allPlatRows.length > 0 ? `<div class="cmpstrip-platlist">${allPlatRows.join("")}</div>` : ""}
    ${notesHTML}
  </div>`;
}

/* ---- annotations --------------------------------------------------------- */
// Take the server's copy of the marks after a write. When it holds nothing beyond this
// tab's own change, record its stamp so the change watcher does not repaint for our own
// click; when it holds someone else's change too (the agent's), leave the stamp so the
// watcher repaints and the user sees that change.
function sameMarks(a, b) {
  return ["favorites", "notes", "trashed"].every(k => {
    const x = a[k] || {}, y = b[k] || {}, kx = Object.keys(x), ky = Object.keys(y);
    return kx.length === ky.length && kx.every(u => x[u] === y[u]);
  });
}
function adoptMarks(runId, res) {
  const ann = normalizeAnn(res && res.annotations);
  if (runId !== state.runId) { state.trash[runId] = ann.trashed; return; }
  const onlyOurs = sameMarks(state.ann, ann);
  state.ann = ann;
  state.trash[runId] = ann.trashed;
  if (onlyOurs && res.stamp && state.stamps && state.stamps.run === runId) state.stamps.annotations = res.stamp;
}
async function toggleFav(uid) {
  const next = !state.ann.favorites[uid];
  if (next) state.ann.favorites[uid] = true; else delete state.ann.favorites[uid];
  paintFav(uid);
  try { adoptMarks(state.runId, await api.mark(state.runId, "favorites", uid, next || null)); }
  catch (e) { console.warn("annotation save failed", e); }
}
function paintFav(uid) {
  document.querySelectorAll(`[data-fav="${cssEsc(uid)}"]`).forEach(b => {
    b.classList.toggle("on", !!state.ann.favorites[uid]);
    b.title = state.ann.favorites[uid] ? "Favorited" : "Favorite";
  });
}
const cssEsc = (s) => String(s).replace(/["\\]/g, "\\$&");

/* ============================================================================
   SURFACE: AD LIBRARY
   ========================================================================== */
function renderAdLibrary(opts = {}) {
  const recs = (state.index.records || []).filter(r => !state.ann.trashed[r.uid]);
  const platforms = distinct(recs.map(r => r.platform));
  const formats = distinct(recs.flatMap(recFormats));   // an ad can be IMAGE and CAROUSEL both
  if (!_skipFilterReset) state.filters = { platforms: new Set(), formats: new Set(), advertisers: new Set(), durations: new Set(), favOnly: false, q: "", view: "", change: "", vs: "" };

  // Advertiser facet: advertiser_name is present on every ad on every platform,
  // so this is the one filter that generalises cleanly. Sorted by ad count (who
  // is running the most), with the count shown inline.
  const advCount = new Map();
  recs.forEach(r => { const a = (r.data || {}).advertiser_name; if (a) advCount.set(a, (advCount.get(a) || 0) + 1); });
  const advValues = [...advCount.keys()].sort((a, b) => advCount.get(b) - advCount.get(a) || a.localeCompare(b));
  // Run-duration bands: only offer bands that actually occur, in natural order.
  const durPresent = new Set(recs.map(durationBand));
  const durValues = DUR_BANDS.map(([k]) => k).filter(k => durPresent.has(k));
  if (!_skipFilterReset) applyRunOpts(opts, { platforms, formats, advertisers: advValues, durations: durValues });

  const rail = filterRail([
    viewsGroup(),
    facetGroup("Platform", platforms, "platforms", p => platLabel(p)),
    facetGroup("Advertiser", advValues, "advertisers", a => `${a}  ·  ${advCount.get(a)}`, true),
    facetGroup("Run duration", durValues, "durations", k => DUR_LABEL[k]),
    facetGroup("Format", formats, "formats", f => f),
    favToggle(),
    changeFilterGroup(),
  ]);

  const meta = state.index;
  const stage = `
    <section class="stage">
      ${grainSVG()}
      ${surfaceMasthead("Ad library", meta, [
        countChip((meta.counts && meta.counts.records) || recs.length, "ads", "shownCount", "Records on screen after your source filters, text search, and trash are applied."),
        meta.counts && meta.counts.creatives_on_disk != null ? countChip(meta.counts.creatives_on_disk, "creatives on disk", null, "Image and video files downloaded with the scrape.") : "",
      ])}
      <div class="stage-scroll" style="padding:28px">
        ${runStatusNotes(state.runId, meta)}
        <div id="carriednotice"></div>
        <div id="comparestrip"></div>
        <div id="viewbanner"></div>
        <div id="grid"></div>
        ${setAsideSection(meta)}
      </div>
    </section>`;

  $("#region").innerHTML = `<div class="runview">${rail}${stage}</div>`;
  wireFilterRail(() => paintAds(recs));
  restoreFilterUI();
  paintAds(recs);
}

function paintAds(recs) {
  const f = state.filters;
  // `recs` is closed over at surface render time, so a record trashed mid-session must
  // be excluded here too -- the derivation-point filter only runs on a full re-render.
  const cUids = changeUids();
  const list = recs.filter(r => !state.ann.trashed[r.uid] && matchCommon(r, f) && (!cUids || cUids.has(r.uid)));
  const grid = $("#grid");
  grid.innerHTML = list.length ? list.map(adCard).join("")
    : `<div style="column-span:all">${emptyResult("No ads match these filters.")}</div>`;
  setCount("shownCount", list.length);
  wireCardActs();
  wireReadMore();
  wireHScroll();
  afterPaint(list, recs);
}

/* Ad body: the primary copy, with Meta's dynamic-ad tokens handled. A body with real words
   loses only the {{...}} tokens; a body that is ONLY tokens (Meta fills it per viewer from the
   catalog) shows a short note instead of broken template code -- the real per-product copy, when
   the ad is a carousel, is in the cards below. */
function adBodyBlock(r) {
  if (!has(r.text)) return "";
  const clean = stripTokens(r.text);
  // The tip only hints: the full text is on the card ("read more"). Cut long text, as the
  // note-chip text elsewhere is cut, so the tip is not as tall as the ad.
  const tipText = r.text.length > 280 ? r.text.slice(0, 280).trimEnd() + "…" : r.text;
  if (clean) {
    return `<p class="adbody" title="${esc(tipText)}">${esc(clean)}</p><button class="readmore" hidden onclick="toggleClamp(this)" title="Show the full ad text">read more</button>`;
  }
  if (hasTokens(r.text)) {
    return `<p title="${esc(tipText)}" class="eb" style="margin:0;font-size:11px;color:var(--muted-2);font-style:italic">Dynamic ad: the body copy is filled in for each viewer from the advertiser's product catalog</p>`;
  }
  return "";
}

/* Meta carousel: a horizontal strip of the per-card copy (title/body/cta/link). Emit surfaces
   card TEXT only -- the downloaded creatives stay in the ad's media gallery above, because the
   manifest records no card->file mapping (see emit_index._cards). Returns "" for non-carousel
   ads so the render is a no-op there.
   For DCO (dynamic creative) ads display_format === "DCO": Meta ships the creative variants in
   the same cards field, but these are not swipeable cards -- Meta assembles the ad per viewer.
   The language branches so viewers are not misled into thinking these are carousels. */
// Case-normalized DCO check: the actor's casing is not contractual, and a wrong-case value
// must fail toward the honest fallback (carousel wording), never toward garbage.
function isDCOFormat(d) { return String(d.display_format || "").toUpperCase() === "DCO"; }

function adCarousel(d) {
  const cards = d.cards;   // DISTINCT copy (emit deduped identical slots; see _dedupe_cards)
  if (!Array.isArray(cards) || !cards.length) return "";
  const unique = cards.length;
  // Original slot count. Only a number counts (a string "5" does not): the same rule as
  // recFormats, so this header and the CAROUSEL chip never disagree.
  const total = typeof d.card_count === "number" ? d.card_count : unique;
  const isDCO = isDCOFormat(d);
  const strip = cards.map((c, i) => {
    const kickerLabel = isDCO ? `version ${i + 1}` : `copy ${i + 1}`;
    const kickerTip = isDCO
      ? `One of ${total} creative variants in this dynamic ad. Meta selects the combination it predicts will perform best for each viewer.`
      : `The text of card ${i + 1} of this carousel.`;
    return `
    <div class="hs-card" style="flex:0 0 210px;border:1px solid var(--line);border-radius:8px;padding:10px;background:var(--creative-bg);display:flex;flex-direction:column;gap:5px">
      <div class="eb" style="color:var(--muted-2)" title="${kickerTip}">${kickerLabel}</div>
      ${has(stripTokens(c.title)) ? `<div style="font-weight:600;font-size:12.5px;line-height:1.35">${esc(stripTokens(c.title))}</div>` : ""}
      ${has(stripTokens(c.body)) ? `<div style="font-size:11.5px;color:var(--ink-soft);line-height:1.45;display:-webkit-box;-webkit-line-clamp:4;-webkit-box-orient:vertical;overflow:hidden">${esc(stripTokens(c.body))}</div>` : ""}
      ${(has(c.cta_text) || has(c.link_url)) ? `<div style="display:flex;gap:6px;flex-wrap:wrap;align-items:center;margin-top:auto;padding-top:4px">
        ${has(c.cta_text) ? `<span class="tag" title="The button text this ad shows people. Shown for reference; it is not a link here."><span class="tag-k">button</span> ${esc(c.cta_text)}</span>` : ""}
        ${has(c.link_url) ? `<a href="${esc(c.link_url)}" target="_blank" rel="noopener noreferrer" class="pbtn" style="font-size:10px" title="${isDCO ? "Open this variant's landing page in a new tab" : "Open this carousel card's landing page in a new tab"}">open ↗</a>` : ""}
      </div>` : ""}
    </div>`;
  }).join("");
  let label, headerTip;
  if (isDCO) {
    // DCO: versions assembled per viewer, not swipeable cards.
    if (unique === 1 && total > 1) label = `${total} versions, identical copy`;
    else if (unique < total) label = `${total} versions · ${unique} distinct`;
    else label = `${total} creative version${total === 1 ? "" : "s"} · Meta assembles this ad per viewer`;
    headerTip = "A dynamic creative ad. Meta mixes these versions and shows each viewer the combination it predicts will work best. The versions are variants of one ad, not swipeable cards.";
  } else {
    // State the slot count honestly, and flag when a "carousel" is really one message repeated
    // -- three identical cards add no signal, so say so.
    if (unique === 1 && total > 1) label = `${total}-card carousel · ${total} identical cards, one message`;
    else if (unique < total) label = `${total}-card carousel · ${unique} distinct of ${total} slots`;
    else label = `${total}-card carousel · copy per card`;
    headerTip = "A carousel shows several swipeable cards in sequence. Identical cards repeat the same image or copy.";
  }
  return `<div>
    <div class="eb" style="color:var(--muted-2);margin-bottom:6px" title="${esc(headerTip)}">${esc(label)}</div>
    <div class="hscroll">
      <button class="hs-btn hs-prev" hidden onclick="hsStep(this,-1)" aria-label="Previous ${isDCO ? "version" : "card"}" title="Previous ${isDCO ? "version" : "card"}">${HS_CHEV_L}</button>
      <div class="hs-track">${strip}</div>
      <button class="hs-btn hs-next" hidden onclick="hsStep(this,1)" aria-label="Next ${isDCO ? "version" : "card"}" title="Next ${isDCO ? "version" : "card"}">${HS_CHEV_R}</button>
    </div>
  </div>`;
}

// aside: a set-aside Google ad under its label (setAsideSection). It is
// not a record of this run, so it gets no star, note, trash or change badge (those
// would count or compare it); everything else is the same card a shown ad gets.
// Only `true` counts: paintAds calls list.map(adCard), which passes the list index.
// Its header never falls back to r.subject: a shown ad with no name is
// the competitor's, so their name heads it; a set-aside ad is not confirmed as theirs,
// so with no name it says so (SET_ASIDE_NO_NAME) and its avatar is "?". A name of only
// spaces counts as no name there (it gave a blank header).
function adCard(r, asideArg) {
  const aside = asideArg === true;
  const d = r.data || {}, t = r.tags || {};
  const fav = state.ann.favorites[r.uid] ? " on" : "";
  const sparse = r.platform === "linkedin";
  // Pick the video to PLAY and the image to use as its poster; a video ad has both on disk,
  // so the old media[0] often showed the poster as a dead static image. Prefer the video.
  const mediaList = r.media || [];
  const vid = mediaList.find(m => m.type === "video");
  const poster = mediaList.find(m => m.type === "image");
  const creative = vid
    ? `<video controls preload="metadata" playsinline
              ${poster ? `poster="${esc(api.fileURL(state.runId, poster.path))}"` : ""}
              style="width:100%;display:block;background:#000;max-height:340px">
         <source src="${esc(api.fileURL(state.runId, vid.path))}">
       </video>`
    : poster
      ? `<img src="${esc(api.fileURL(state.runId, poster.path))}" alt="" loading="lazy"
              style="width:100%;display:block;background:var(--creative-bg)"
              onerror="var b=this.parentNode.querySelector('[data-ondisk]');if(b)b.remove();var e=document.createElement('div');e.className='creative-missing';e.textContent='creative not on disk';this.replaceWith(e);">`
      : "";
  const creativeBlock = mediaList.length
    ? `<div style="position:relative">
         <div class="eb" data-ondisk="1" style="position:absolute;top:10px;left:10px;z-index:2;background:var(--ink);color:var(--ember-lite);padding:5px 9px;border-radius:6px" title="The creative file was downloaded and is available on disk.">${vid ? "video · on disk" : "on disk"}</div>
         ${mediaList.length > 1 ? `<div class="eb" style="position:absolute;top:10px;right:10px;z-index:2;background:var(--ink);color:var(--paper);padding:5px 9px;border-radius:6px" title="${mediaList.length} image or video files saved for this ad.">${mediaList.length} creatives</div>` : ""}
         ${creative}
       </div>`
    : `<div style="aspect-ratio:1.91/1;background:var(--creative-bg);display:flex;align-items:center;justify-content:center;border-bottom:1px solid var(--line)">
         <span class="eb" style="color:var(--muted-2)" title="No image or video files were saved for this ad.">no creative on disk</span>
       </div>`;

  const run = runLine(d);
  const unknowns = [
    !run && sparse ? "run dates not disclosed" : null,
    !has(d.impressions_text) && sparse ? "impressions not disclosed" : null,
  ].filter(Boolean);
  const activeLabel = { active: "active", ended: "ended", known_dates: "known dates" }[t.active] || "";
  // Placement (which surfaces the ad runs on) + targeting dimensions -- competitive signal.
  const PLAT_MAP = { FACEBOOK: "Facebook", INSTAGRAM: "Instagram", AUDIENCE_NETWORK: "Audience Net", WHATSAPP: "WhatsApp", THREADS: "Threads", MESSENGER: "Messenger" };
  const placements = (Array.isArray(d.publisher_platforms) ? d.publisher_platforms : []).map(p => PLAT_MAP[p] || String(p).replace(/_/g, " ").toLowerCase());
  const signalRow = (label, items, labelTip, chipTip) => items.length
    ? `<div style="display:flex;gap:6px;flex-wrap:wrap;align-items:center">
         <span class="eb" style="color:var(--muted-2);font-size:10px"${labelTip ? ` title="${esc(labelTip)}"` : ""}>${label}</span>
         ${items.map(x => `<span class="tag"${chipTip ? ` title="${esc(chipTip)}"` : ""}>${esc(x)}</span>`).join("")}
       </div>` : "";
  // "Paid by" only earns space when it differs from the advertiser (reveals a hidden payer).
  const payer = has(d.paid_by) && String(d.paid_by).trim().toLowerCase() !== String(d.advertiser_name || "").trim().toLowerCase()
    ? d.paid_by : null;
  // delivery group: run dates + impressions + disclosure chips; guards: each item present-checked,
  // unknowns only fire for sparse (linkedin) cards, group omitted when all absent.
  const deliveryItems = [
    run ? `<span class="tag" title="The period the platform reports this ad ran.">${run}</span>` : "",
    has(d.impressions_text) ? `<span class="tag">${esc(d.impressions_text)} impressions</span>` : "",
    ...unknowns.map(x => `<span class="tag unknown" title="LinkedIn's ad library does not publish this information.">${x}</span>`),
  ].filter(Boolean);
  const deliveryGroup = deliveryItems.length
    ? `<div style="display:flex;gap:6px;flex-wrap:wrap;align-items:center">
         <span class="eb" style="color:var(--muted-2);font-size:10px" title="The dates and duration the platform reports this ad ran.">runtime</span>
         ${deliveryItems.join("")}
       </div>` : "";
  // advertiser info group: followers + caption self-labelled "links to" (fixes a naked-value
  // confusion a reviewer flagged) + native id + payer (moved from footer -- it is
  // advertiser metadata, not card chrome). Group omitted when all four are absent.
  const advertiserItems = [
    has(d.page_like_count) ? `<span class="tag" title="The page's follower count when the ad was scraped.">${num(d.page_like_count)} followers</span>` : "",
    has(d.caption) ? `<span class="tag" title="The domain or URL the ad's call-to-action points to.">links to ${esc(d.caption)}</span>` : "",
    has(d.native_id) ? `<span class="tag" style="color:var(--muted-2)" title="The platform's own identifier for this ad. Useful for finding it in the platform's public ad library.">id ${esc(String(d.native_id).slice(-8))}</span>` : "",
    payer ? `<span class="tag accent" title="Paid by a different entity than the advertiser">paid by ${esc(payer)}</span>` : "",
  ].filter(Boolean);
  const advertiserGroup = advertiserItems.length
    ? `<div style="display:flex;gap:6px;flex-wrap:wrap;align-items:center">
         <span class="eb" style="color:var(--muted-2);font-size:10px" title="Details about the advertiser scraped with this ad.">advertiser info</span>
         ${advertiserItems.join("")}
       </div>` : "";

  const fmtLabel = recFormats(r).join(" · ").toLowerCase() || "ad";
  const fmtTip = fmtLabel === "follow_company_ad"
    ? "A LinkedIn ad format that asks the viewer to follow the company."
    : fmtLabel === "ad"
    ? "The creative format was not reported by the platform."
    : "The creative format as reported by the platform.";

  // The "landing" button below shows r.url. A Google ad indexed before the root fix carries
  // the scraper's own start page there (adstransparency.google.com/?region=..&domain=..),
  // not a page the ad sends people to; the viewer serves old indexes as they are, so such
  // a link is hidden here. A real Google ad page starts /advertiser/ and
  // is reached by "original ad" (d.ad_url), which this does not touch.
  return `
    <article class="card" style="break-inside:avoid;margin-bottom:22px;${sparse ? "border-style:dashed" : ""}">
      <header style="display:flex;align-items:center;gap:10px;padding:14px 16px;border-bottom:1px solid var(--line)">
        <div style="width:32px;height:32px;border-radius:var(--radius-sm);background:var(--ink);color:var(--paper);display:flex;align-items:center;justify-content:center;font-family:var(--font-head);font-weight:700;font-size:12px">${esc(aside ? initials(d.advertiser_name) : initials(d.advertiser_name || r.subject))}</div>
        <div style="min-width:0">
          <div style="font-weight:600;font-size:13.5px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${esc(aside ? (String(d.advertiser_name || "").trim() ? d.advertiser_name : SET_ASIDE_NO_NAME) : (d.advertiser_name || r.subject || "-"))}</div>
          <div class="eb" style="font-size:10px" title="Platform, advertiser page category, and run status. Known dates means Google discloses exact run dates for this ad.${r.platform === "google" ? " Google ad creatives are captured images, so these cards carry fewer structured fields than Meta cards." : ""}">${esc(platLabel(r.platform))}${has(d.page_category) ? " · " + esc(d.page_category) : ""}${activeLabel ? " · " + esc(activeLabel) : ""}</div>
        </div>
        <span class="tag" style="margin-left:auto" title="${fmtTip}">${esc(fmtLabel)}</span>
      </header>
      ${creativeBlock}
      <div style="padding:16px;display:flex;flex-direction:column;gap:10px;flex-grow:1">
        ${aside ? `<div><span class="tag unknown" data-note="not-confirmed">not confirmed as the competitor's</span></div>` : changeBadgeHTML(r.uid)}
        ${has(stripTokens(d.headline)) ? `<div class="hl" style="font-size:17px;font-weight:600">${esc(stripTokens(d.headline))}</div>` : ""}
        ${adBodyBlock(r)}
        ${has(stripTokens(d.link_description)) ? `<p style="margin:0;font-size:12px;color:var(--muted);line-height:1.45;display:-webkit-box;-webkit-line-clamp:3;-webkit-box-orient:vertical;overflow:hidden">${esc(stripTokens(d.link_description))}</p>` : ""}
        ${adCarousel(d)}
        ${has(d.cta_text) ? `<div><span class="tag" title="The button text this ad shows people. Shown for reference; it is not a link here."><span class="tag-k">button</span> ${esc(d.cta_text)}</span></div>` : ""}
        ${signalRow("placements", placements, "Where Meta says this ad appeared.", "A platform where this ad ran.")}
        ${signalRow("targeting", Array.isArray(d.audience_selections) ? d.audience_selections : [], "Targeting signals Google reports for this ad.", "A dimension Google targeted with this ad.")}
        ${deliveryGroup}
        ${advertiserGroup}
      </div>
      <footer style="display:flex;align-items:center;gap:8px;padding:12px 16px;border-top:1px solid var(--line);flex-wrap:wrap">
        ${has(d.ad_url) ? `<a href="${esc(d.ad_url)}" target="_blank" rel="noopener noreferrer" class="pbtn" title="Open the original ad in the platform's public ad library">original ad ↗</a>` : ""}
        ${has(r.url) && !(r.platform === "google" && String(r.url).startsWith("https://adstransparency.google.com/?")) ? `<a href="${esc(r.url)}" target="_blank" rel="noopener noreferrer" class="pbtn" title="Open the page this ad sends people to">landing ↗</a>` : ""}
        ${aside ? "" : `<div style="margin-left:auto;display:flex;gap:6px">${actButtons(r.uid)}</div>`}
      </footer>
      ${aside ? "" : noteBlock(r.uid)}
    </article>`;
}

/* ============================================================================
   SURFACE: REVIEW WALL
   ========================================================================== */
/* Normalise a star distribution to {1..5:count}, accepting BOTH shapes the emit produces:
   Google Maps' named keys (oneStar..fiveStar, its full-corpus histogram) and the numeric-string
   keys ("1".."5") of a sample histogram computed from the records. Returns null if empty. */
function starHist(dist) {
  if (!dist || typeof dist !== "object") return null;
  const KEY = { fiveStar: 5, fourStar: 4, threeStar: 3, twoStar: 2, oneStar: 1, "5": 5, "4": 4, "3": 3, "2": 2, "1": 1 };
  const h = { 1: 0, 2: 0, 3: 0, 4: 0, 5: 0 }; let any = false;
  for (const [k, v] of Object.entries(dist)) {
    const s = KEY[k];
    if (s && typeof v === "number") { h[s] += v; any = true; }
  }
  return any ? h : null;
}

/* Source snapshots: ONE aggregate card per reviewed source, each labelled by its source so the
   UI never implies one source is "the" snapshot. Every non-Reddit source the tool scrapes gets a
   card (App Store / G2 / Capterra included the day their data lands), built from the reviews we
   hold; Maps and Trustpilot are enriched with the source's own authoritative figures. Provenance
   is always stated: a score reads "Google rating" (Google's own) vs "average of the reviews
   analysed" (ours), and a histogram says whether it is the full corpus or just the sample. */
// Reddit overview card. Derived from the wall's recs (already tombstone-filtered).
// Counts only -- no score, no star histogram. Reddit sentiment is inferred from wording
// and there are no star ratings to aggregate, so showing a score would launder an
// estimate into a fact. The card vanishes when no reddit records are present.
function redditSnapshot(recs) {
  const reddit = (recs || []).filter(r => r.platform === "reddit");
  if (!reddit.length) return "";
  const posts    = reddit.filter(r => r.data && r.data.is_comment === false).length;
  const comments = reddit.filter(r => r.data && r.data.is_comment === true).length;
  const other    = reddit.length - posts - comments;
  // Subreddit breakdown: normalize names, count, sort desc, cap at 10.
  const subCounts = {};
  reddit.forEach(r => { const s = subredditOf(r); if (s) subCounts[s] = (subCounts[s] || 0) + 1; });
  const subList = Object.entries(subCounts).sort((a, b) => b[1] - a[1]).slice(0, 10);
  // Title falls back to a plain label when the records carry no subreddit field at all
  // (older or partial corpora): the post/comment counts are still worth showing, but a
  // "0 subreddits" title would read as broken.
  const titleStr = subList.length === 0 ? "Reddit discussions"
    : subList.length === 1
    ? `r/${esc(subList[0][0])}` : `${subList.length} subreddits`;
  const chips = subList.map(([s, c]) =>
    `<span class="tag" title="${c} posts and comments from r/${esc(s)} in this run">r/${esc(s)} <span class="num" style="opacity:.6">${num(c)}</span></span>`
  ).join("");
  const pl = (n, w) => `${num(n)} ${w}${n === 1 ? "" : "s"}`;
  const totalLine = other > 0
    ? `${pl(posts, "post")} · ${pl(comments, "comment")} · ${num(other)} other · ${num(reddit.length)} total analysed here`
    : `${pl(posts, "post")} · ${pl(comments, "comment")} analysed here`;
  return `<div class="card" style="padding:18px;margin-bottom:16px">
    <div class="eb" style="color:var(--ember);margin-bottom:4px">Reddit · discussion overview</div>
    <div style="font-family:var(--font-head);font-size:18px;font-weight:600;line-height:1.25">${titleStr}</div>
    <div class="eb" style="font-size:13px;color:var(--muted);margin-top:8px" title="Posts and comments from this subreddit downloaded and analysed in this run.">${totalLine}</div>
    ${chips ? `<div style="display:flex;gap:6px;flex-wrap:wrap;margin-top:10px">${chips}</div>` : ""}
    <div class="eb" style="font-size:10px;color:var(--muted-2);margin-top:10px">Open discussion, not reviews. Sentiment on Reddit cards is inferred from wording; there are no star ratings to average, so no score is shown.</div>
  </div>`;
}

function sourceSnapshots(snaps) {
  if (!Array.isArray(snaps) || !snaps.length) return "";
  // The tip on a card's headline score. score_basis says what the number is (the emitter
  // writes "sample" for the average of the reviews this run holds, "source" for the
  // platform's own figure). A sample average must never be described as the platform's
  // rating. An index with no score_basis keeps the wording by source.
  const scoreTip = (s) => s.score_basis === "sample"
    ? "The average star rating of the reviews analysed here, out of 5. It is worked out from this run's reviews, not the platform's own overall rating."
    : s.source === "maps" ? "The venue's overall Google Maps rating, out of 5."
    : s.source === "trustpilot" ? "Trustpilot's 1 to 5 score for the company."
    : "The overall score from this platform.";
  // The tip on the "reviews" line, saying only what the line shows: the platform's total,
  // the number analysed here, or both.
  const corpusTip = (s) => has(s.total_reviews) && has(s.sample_size) ? "The platform's total review count and how many this run downloaded and analysed."
    : has(s.total_reviews) ? "The platform's total review count."
    : "How many reviews this run downloaded and analysed.";
  return snaps.map(s => {
    const hist = starHist(s.distribution);
    const rows = [5, 4, 3, 2, 1];
    const max = hist ? Math.max(1, ...rows.map(n => hist[n] || 0)) : 1;
    const bars = hist ? rows.map(n => {
      const v = hist[n] || 0;
      return `<div style="display:flex;align-items:center;gap:8px;font-size:11px" title="${num(v)} of the reviews analysed here gave ${n} star${n === 1 ? "" : "s"}.">
        <span class="num" style="width:12px;color:var(--muted-2);text-align:right">${n}</span>
        <span style="flex:1;height:8px;background:var(--creative-bg);border-radius:4px;overflow:hidden"><span style="display:block;height:100%;width:${Math.round((v / max) * 100)}%;background:var(--ember)"></span></span>
        <span class="num" style="width:34px;color:var(--muted)">${num(v)}</span>
      </div>`;
    }).join("") : "";
    const tags = (s.tags || []).slice(0, 10).map(tg =>
      `<span class="tag" title="${has(tg.count) ? `Google Maps counted this topic in ${num(tg.count)} reviews of this place.` : "A topic Google Maps identified in reviews of this place."}">${esc(tg.title)}${has(tg.count) ? ` <span class="num" style="opacity:.6">${num(tg.count)}</span>` : ""}</span>`).join("");
    // Provenance caption under the histogram: full corpus (the source's own) vs our sample.
    const distCaption = s.distribution_basis === "source"
      ? (has(s.total_reviews) ? `across all ${num(s.total_reviews)} reviews on ${esc(platLabel(s.source))}` : `across all reviews on ${esc(platLabel(s.source))}`)
      : `across the ${num(s.sample_size)} reviews analysed here`;
    // The "reviews" line: the source's own corpus total (if known) and how many we analysed.
    const corpusLine = has(s.total_reviews)
      ? `${num(s.total_reviews)} reviews on ${esc(platLabel(s.source))}${has(s.sample_size) ? ` · ${num(s.sample_size)} analysed here` : ""}`
      : (has(s.sample_size) ? `${num(s.sample_size)} reviews analysed here` : "");
    return `<div class="card snap-card-grid${hist ? " snap-card-grid--hist" : ""}" style="padding:18px;margin-bottom:16px">
      <div>
        <div class="eb" style="color:var(--ember);margin-bottom:4px">${esc(platLabel(s.source))} · ${esc(s.scope || "source")} overview</div>
        <div style="font-family:var(--font-head);font-size:18px;font-weight:600;line-height:1.25">${esc(s.title || "-")}</div>
        ${has(s.subtitle) ? `<div class="eb" style="font-size:11px;color:var(--muted-2);margin-top:4px">${esc(s.subtitle)}</div>` : ""}
        <div style="margin-top:10px;display:flex;align-items:baseline;gap:8px;flex-wrap:wrap">
          ${has(s.score) ? `<span style="font-family:var(--font-head);font-size:26px;font-weight:700;color:var(--ember)" title="${esc(scoreTip(s))}">${esc(String(s.score))}</span>
          <span class="eb" style="font-size:11px;color:var(--muted)">${esc(s.score_label || "")}</span>` : ""}
        </div>
        ${corpusLine ? `<div class="eb" style="font-size:11px;color:var(--muted);margin-top:6px" title="${esc(corpusTip(s))}">${corpusLine}</div>` : ""}
        ${has(s.url) ? `<a href="${esc(s.url)}" target="_blank" rel="noopener noreferrer" class="pbtn" style="margin-top:10px" title="Open this business's ${esc(platLabel(s.source))} page in a new tab">on ${esc(platLabel(s.source))} ↗</a>` : ""}
      </div>
      ${hist ? `<div style="display:flex;flex-direction:column;gap:6px">
        ${bars}
        <div class="eb" style="font-size:10px;color:var(--muted-2);margin-top:2px">${distCaption}</div>
        ${tags ? `<div style="display:flex;gap:6px;flex-wrap:wrap;margin-top:8px">${tags}</div>` : ""}
      </div>` : (tags ? `<div style="display:flex;gap:6px;flex-wrap:wrap">${tags}</div>` : "")}
    </div>`;
  }).join("");
}

// Reddit tombstones: content removed by moderators or deleted by the author. The card
// would show nothing but the marker, so it is suppressed from display. The record stays
// in the data untouched -- nothing is ever deleted from the backend. The index step flags
// them (tags.removed) so an agent reading the index skips the same records; the text
// check stays for runs indexed before that flag existed.
const isRedditTombstone = (r) => r.platform === "reddit"
  && ((r.tags || {}).removed === true
      || ["[removed]", "[deleted]"].includes((r.text || "").trim().toLowerCase()));
// data.subreddit is inconsistent in real corpora ("acmefitness" on comments,
// "r/acmefitness" on posts) -- one normalizer, used everywhere, so the two
// spellings are one subreddit in the facet, the filter and the overview card. The index
// step now strips the prefix too; this stays for older indexes (it is a no-op on new ones).
const subredditOf = (r) => {
  const s = r.data && r.data.subreddit;
  return s ? String(s).replace(/^r\//i, "") : null;
};

// Why the reviews page can show fewer records than the sidebar:
// the sidebar counts every record in the run's index; the page never shows removed
// Reddit posts (isRedditTombstone) or records moved to the trash. A user who sees
// "130" in the sidebar and "129 shown" on the page is told why, on both numbers.
const RAIL_COUNT_TIP = "Records in this run's index, before any filters. The run's page can show fewer: removed Reddit posts and records you moved to the trash are not shown there.";
// The "shown" chip's tip on the reviews page, with this run's own count of removed
// Reddit posts. Pure function.
function reviewsShownTip(records) {
  const all = records || [];
  const removed = all.filter(isRedditTombstone).length;
  const base = "Records on screen after your source filters, text search, and trash are applied.";
  if (!removed) return base;
  return base + ` This run's index holds ${all.length} records; ${removed} of them ${removed === 1 ? "is a removed Reddit post" : "are removed Reddit posts"} (deleted by the author or a moderator), which ${removed === 1 ? "is" : "are"} never shown here.`;
}

// Shared between renderReviewWall and paintSnapshots. Set in renderReviewWall when
// the wall is built; read in paintSnapshots to derive the Reddit overview card.
// Sentiment/favourites/search must not affect the card -- this is the full
// tombstone-filtered recs, never a post-filter slice.
let _wallRecs = [];
function renderReviewWall(opts = {}) {
  const recs = (state.index.records || []).filter(r => !isRedditTombstone(r) && !state.ann.trashed[r.uid]);
  _wallRecs = recs;
  const platforms = distinct(recs.map(r => r.platform));
  const polarities = sortPolarities(distinct(recs.map(r => (r.tags && (r.tags.polarity || r.tags.banding))).filter(Boolean)));
  const subreddits = distinct(recs.filter(r => r.platform === "reddit").map(subredditOf).filter(Boolean)).sort();
  if (!_skipFilterReset) state.filters = { platforms: new Set(), polarities: new Set(), subreddits: new Set(), favOnly: false, q: "", view: "", change: "", vs: "" };
  // Preset filters from the address (the dossier's per-source chips use ?source=, the
  // agent's links use the full filter set).
  if (!_skipFilterReset) applyRunOpts(opts, { platforms, polarities, subreddits });

  const rail = filterRail([
    viewsGroup(),
    facetGroup("Source", platforms, "platforms", p => platLabel(p)),
    facetGroup("Sentiment", polarities, "polarities", p => polLabel(p)),
    facetGroup("Subreddit", subreddits, "subreddits", s => "r/" + s),
    favToggle(),
    changeFilterGroup(),
  ]);

  const meta = state.index;
  const syn = meta.synthesis;
  const stage = `
    <section class="stage">
      ${grainSVG()}
      ${surfaceMasthead("Reviews", meta, [
        countChip((meta.counts && meta.counts.records) || recs.length, "shown", "shownCount", reviewsShownTip(state.index.records)),
        syn && syn.meta ? countChip(syn.meta.total_reviews, "analysed", null, ANALYSED_TIP) : "",
      ])}
      <div class="stage-scroll" style="padding:28px">
        ${runStatusNotes(state.runId, meta)}
        <div id="carriednotice"></div>
        <div id="comparestrip"></div>
        <div id="viewbanner"></div>
        <p class="eb" style="margin:0 0 18px;max-width:640px;line-height:1.7;color:var(--muted)">
          These cards are rebuilt from the scraped fields, not screenshots of the original pages.
        </p>
        <div id="snaprail"></div>
        <div id="wall"></div>
      </div>
    </section>`;

  $("#region").innerHTML = `<div class="runview">${rail}${stage}</div>`;
  wireFilterRail(() => { paintSnapshots(); paintReviews(recs); });
  // Reflect preset filters in the rail so it shows what is applied.
  restoreFilterUI();
  paintSnapshots();
  paintReviews(recs);
}

/* The source snapshots obey the Source facet: ticking "Google Maps" leaves only the Maps
   overview, exactly like the review cards below. Sentiment/favourites/search do NOT filter
   them -- an aggregate is a per-source rollup, not a per-review card, so a sentiment filter
   has no meaning for it. Reddit gets a counts-only overview card derived from the records;
   ticking only Reddit shows that card (and hides the others). */
function paintSnapshots() {
  const el = $("#snaprail");
  if (!el) return;
  const f = state.filters;
  const snaps = (state.index.snapshots || []).filter(s =>
    !(f.platforms && f.platforms.size) || f.platforms.has(s.source));
  const showReddit = !(f.platforms && f.platforms.size) || f.platforms.has("reddit");
  const notes = (state.index.snapshot_notes || []).filter(n =>
    !(f.platforms && f.platforms.size) || f.platforms.has(n.source));
  el.innerHTML = snapshotRail(snaps, notes) + (showReddit ? redditSnapshot(_wallRecs) : "");
}

/* The cards in order, with the Maps note placed directly after the LAST Google Maps card (or,
   with no Maps card, before any other source's card), so it can never be read as being about
   the card above it from another source. */
function snapshotRail(snaps, notes) {
  const list = snaps || [];
  let cut = -1;
  list.forEach((s, i) => { if (s.source === "maps") cut = i; });
  return sourceSnapshots(list.slice(0, cut + 1)) + snapshotNotes(notes, list)
    + sourceSnapshots(list.slice(cut + 1));
}

/* What the place cards do NOT cover, said in one plain line instead of hidden. A Maps search
   finds a page of places but collects reviews for only a few; a place with no collected
   reviews gets no card (its card would have nothing "analysed here"), so the page says how
   many such places there were. Reviews that could not be tied to a listed place are named too,
   because no place card counts them. */
function snapshotNotes(notes, snaps) {
  return (notes || []).filter(n => n && n.source === "maps").map(n => {
    const lines = [];
    const k = n.places_not_reviewed || 0;
    const cards = (snaps || []).filter(s => s.source === "maps").length;
    if (k > 0) {
      const pl = k === 1 ? "place" : "places";
      lines.push(cards > 0
        ? `The Google Maps search also found ${num(k)} other ${pl}; ${k === 1 ? "its" : "their"} reviews were not collected in this run.`
        : `The Google Maps search found ${num(k)} ${pl}; ${k === 1 ? "its" : "their"} reviews were not collected in this run.`);
    }
    const u = n.reviews_unmatched || 0;
    if (u > 0) {
      lines.push(`${num(u)} Google Maps review${u === 1 ? "" : "s"} could not be tied to one of the places the Google Maps search found, so no place card counts ${u === 1 ? "it" : "them"}.`);
    }
    return lines.length
      ? `<div class="carriednotice" data-note="maps-places" style="margin:-6px 0 16px"><div>${lines.map(esc).join(" ")}</div></div>`
      : "";
  }).join("");
}

function paintReviews(recs) {
  const f = state.filters;
  const cUids = changeUids();
  const list = recs.filter(r => {
    // Same reason as paintAds: `recs` is a render-time closure, so mid-session trashes
    // must be filtered at paint time as well.
    if (state.ann.trashed[r.uid]) return false;
    if (cUids && !cUids.has(r.uid)) return false;
    if (f.favOnly && !state.ann.favorites[r.uid]) return false;
    if (f.platforms.size && !f.platforms.has(r.platform)) return false;
    const pol = r.tags && (r.tags.polarity || r.tags.banding);
    if (f.polarities.size && !f.polarities.has(pol)) return false;
    if (f.subreddits && f.subreddits.size && (r.platform !== "reddit" || !f.subreddits.has(subredditOf(r)))) return false;
    if (f.q && !hay(r).includes(f.q)) return false;
    if (f.view && !viewHas(f.view, r.uid)) return false;
    return true;
  });
  $("#wall").innerHTML = list.length ? list.map(reviewCard).join("")
    : `<div style="column-span:all">${emptyResult("No reviews match these filters.")}</div>`;
  setCount("shownCount", list.length);
  wireCardActs();
  afterPaint(list, recs);
}

// The line under a review card that says where the review came from. A Google Maps review
// names the place it was collected from (data.place_name, set when the index ties each Maps
// review to its place). A Maps record without it (an older index) keeps the label it always
// had: the run subject. Place names already carry the brand ("Acme Fitness Studios New York"), so
// the subject is not repeated in front of them. Every other source is unchanged.
function reviewSource(r) {
  const d = r.data || {};
  if (r.platform === "maps" && typeof d.place_name === "string" && has(d.place_name.trim())) return d.place_name;
  return d.business_name || (d.subreddit ? "r/" + d.subreddit : r.subject) || platLabel(r.platform);
}

function reviewCard(r) {
  const d = r.data || {}, t = r.tags || {};
  const pol = t.polarity || t.banding;
  const polChipClass = pol === "low" ? "tag accent" : pol === "neutral" ? "tag" : "tag dark";
  // Dashed outline = "inferred, not a real rating" (Reddit has no stars); a visual cue the
  // tooltip alone cannot carry, since most readers never hover.
  const polChipStyle = r.platform === "reddit" ? ' style="outline:1px dashed var(--muted-2);outline-offset:-1px"' : "";
  const polChipTitle = r.platform === "reddit"
    ? ' title="Inferred from the wording. Reddit has no star ratings."'
    : (typeof d.stars === "number" ? ' title="Banded from the star rating: 1 to 3 stars negative, 4 to 5 positive."' : "");
  const polChip = pol ? `<span class="${polChipClass}"${polChipStyle}${polChipTitle}>${esc(polLabel(pol))}</span>` : "";
  const starsTitle = r.platform === "reddit"
    ? ' title="Estimated from the wording of this Reddit post or comment. Reddit has no star ratings."'
    : ' title="The star rating this reviewer gave."';
  const stars = (typeof d.stars === "number")
    ? `<span class="num" style="color:var(--ember);letter-spacing:1px"${starsTitle}>${"★".repeat(d.stars)}${"☆".repeat(Math.max(0, 5 - d.stars))}</span>`
    : "";
  const source = reviewSource(r);
  // Reddit engagement: score (already had) + post reach signals (upvote ratio, comment count).
  const eng = [
    (r.platform === "reddit" && typeof d.reddit_score === "number")
      ? `<span class="num" style="display:inline-flex;align-items:center;gap:3px;color:var(--muted)" title="Net upvotes on this post or comment.">▲ ${num(d.reddit_score)}</span>` : "",
    has(d.upvote_ratio) ? `<span class="num" style="font-size:11px;color:var(--muted)" title="Share of voters who upvoted this post or comment.">${Math.round(d.upvote_ratio * 100)}% upvoted</span>` : "",
    has(d.comments_count) ? `<span class="num" style="font-size:11px;color:var(--muted)" title="Comments on this post when it was scraped.">${num(d.comments_count)} comments</span>` : "",
  ].filter(Boolean).join("");
  // Maps reviewer credibility: Local Guide badge + how many reviews this reviewer has written.
  const cred = [
    d.is_local_guide ? `<span class="tag" title="Google's badge for active Maps contributors.">Local Guide</span>` : "",
    has(d.reviewer_review_count) ? `<span class="eb" style="font-size:10px;color:var(--muted-2)" title="How many reviews this reviewer has written on the platform.">${num(d.reviewer_review_count)} reviews</span>` : "",
  ].filter(Boolean).join("");
  const kindChip = (r.platform === "reddit" && typeof d.is_comment === "boolean")
    ? `<span class="tag" title="${d.is_comment
        ? "A comment written under a Reddit post"
        : "A Reddit post that started the thread"}">${d.is_comment ? "comment" : "post"}</span>`
    : "";
  return `
    <article class="card" style="break-inside:avoid;margin-bottom:18px;padding:18px">
      <header style="display:flex;align-items:center;gap:9px;margin-bottom:12px;flex-wrap:wrap">
        <span class="eb" style="color:var(--ember);white-space:nowrap;flex-shrink:0" title="Where this record was scraped from.">${esc(platLabel(r.platform))}</span>
        ${kindChip}
        ${stars}
        <div style="margin-left:auto;display:flex;gap:6px;align-items:center">${polChip}</div>
      </header>
      ${changeBadgeHTML(r.uid)}
      ${has(d.review_title) ? `<div class="hl" style="font-size:15px;font-weight:600;margin-bottom:8px;line-height:1.35">${esc(d.review_title)}</div>` : ""}
      <p class="quote">${esc(r.text)}</p>
      ${has(d.company_reply) ? `<div style="margin-top:12px;padding:10px 12px;border-left:2px solid var(--ember);background:var(--creative-bg)">
        <div class="eb" style="color:var(--ember);margin-bottom:4px" title="The company's public reply to this review, scraped alongside it.">Company response</div>
        <div style="font-size:12.5px;color:var(--ink-soft);line-height:1.5">${esc(d.company_reply)}</div>
      </div>` : ""}
      <div style="display:flex;align-items:center;gap:10px;margin-top:14px;padding-top:12px;border-top:1px solid var(--line);flex-wrap:wrap">
        <span style="font-size:12px;color:var(--muted);overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${esc(source)}</span>
        ${d.verified ? `<span class="tag">verified</span>` : ""}
        ${cred}
        ${eng}
        ${has(r.date) ? `<span class="num" style="margin-left:auto;font-size:11px;color:var(--muted-2)">${esc(fmtDate(r.date) || r.date)}</span>` : `<span style="margin-left:auto"></span>`}
      </div>
      <div style="display:flex;gap:6px;margin-top:12px">
        ${actButtons(r.uid)}
        ${sourceLink(r)}
      </div>
      ${noteBlock(r.uid)}
    </article>`;
}
// The per-review source link, labelled honestly by what it actually opens.
// Trustpilot and Reddit both carry a real per-review permalink (recovered in
// emit_index from reviewUrl / postUrl / per-comment url), so "source" is accurate.
// Google Maps has no per-review permalink in the scrape — r.url is the business's
// place page — so we say so rather than imply the link lands on this exact review.
// Absent url (e.g. a raw file missing on disk, or a run from before permalink
// recovery) shows nothing instead of a dead link.
function sourceLink(r) {
  if (!has(r.url)) return "";
  const a = (label, tip) =>
    `<a href="${esc(r.url)}" target="_blank" rel="noopener noreferrer" class="pbtn" style="margin-left:auto" title="${esc(tip)}">${label} ↗</a>`;
  // Maps runs link each review when the index could rebuild its link (/maps/reviews/);
  // older runs or unexpected data only carry the business's page, and the label says so.
  if (r.platform === "maps" && !/\/maps\/reviews\//.test(r.url))
    return a("on Google Maps", "No link to this single review was saved for this run. Opens the business's Google Maps page.");
  return a("source", `Opens this review on ${platLabel(r.platform)}.`);
}

/* ============================================================================
   SURFACE: SEARCH LANDSCAPE
   ========================================================================== */
// Sortable-column definitions for the two search tables (shared by render + click handlers).
const KW_COLS = [
  { key: "text", label: "Keyword", num: false, align: "left", get: r => r.text, tip: "The search phrase." },
  { key: "volume", label: "Volume", num: true, align: "right", get: r => toNum(r.data && r.data.volume), tip: "Monthly search volume. Click to sort." },
  { key: "cpc_usd", label: "Cost / click", num: true, align: "right", get: r => toNum(r.data && r.data.cpc_usd), tip: "Average advertiser price per click on this keyword. Click to sort." },
  { key: "competition", label: "Competition", num: true, align: "right", get: r => toNum(r.data && r.data.competition), tip: "How many advertisers bid on this keyword, from 0 to 1. Click to sort." },
  { key: "keyword_difficulty", label: "Difficulty", num: true, align: "left", get: r => toNum(r.data && r.data.keyword_difficulty), tip: "How hard it is to rank on page one for this keyword, from 0 to 100. Click to sort." },
];
const DOM_SORTS = [
  { key: "keywords_owned", label: "Keywords owned", defDir: "desc", get: r => toNum(r.data && r.data.keywords_owned), tip: "Keywords where this domain appears in Google's top 10." },
  { key: "avg_position", label: "Average position", defDir: "asc", get: r => toNum(r.data && r.data.avg_position), tip: "This domain's typical rank across the keywords it appears in." },
  { key: "best_position", label: "Best position", defDir: "asc", get: r => toNum(r.data && r.data.best_position), tip: "This domain's highest rank anywhere in the results. Not always 1." },
];

function renderSearchLandscape(opts = {}) {
  if (!_skipFilterReset) state.filters = { change: "", vs: "" };
  if (!_skipFilterReset && opts) { if (opts.change) state.filters.change = opts.change; if (opts.vs) state.filters.vs = opts.vs; }
  const recs = state.index.records || [];
  const kw = recs.filter(r => r.kind === "keyword");
  const serp = recs.filter(r => r.kind === "serp");
  const dom = recs.filter(r => r.kind === "domain");
  const meta = state.index;

  // Cross-reference: which SERP queries each domain ranks in (this reproduces the domain's
  // keywords_owned count exactly, so the "which keywords" drill-down is the real answer).
  const domQ = {};
  serp.forEach(s => (s.data.results || []).forEach(x => {
    if (!x.domain) return;
    (domQ[x.domain] = domQ[x.domain] || []).push({ query: s.text, position: x.position });
  }));
  // Search state lives on `state` so the sort/expand click handlers can re-render from it.
  state.search = {
    kw, serp, dom, domQ,
    kwSort: lsGet("sms.kwSort", { col: "volume", dir: "desc" }),
    domSort: lsGet("sms.domSort", { col: "keywords_owned", dir: "desc" }),
  };

  const seg = (id, label, n) => `<button class="seg" data-seg="${id}" title="Show the ${label.toLowerCase()} view (${n})">${label} <span class="num" style="margin-left:6px;opacity:.7">${n}</span></button>`;
  // segment labels are plain English: "Rankings" reads clearer than "SERPs"
  const stage = `
    <section class="stage">
      ${grainSVG()}
      <div class="masthead" style="flex-direction:column;align-items:flex-start;gap:16px">
        <div style="display:flex;align-items:center;gap:16px;width:100%">
          ${backButton("#/campaign/" + encodeURIComponent(meta.subject || ""), "Project", "Back to the " + (meta.subject || "") + " project")}
          <div>
            <div class="eb" style="color:var(--ember);margin-bottom:6px">${esc(meta.subject || "")} · search landscape</div>
            <h1 class="hl" style="font-size:30px;font-weight:700;color:var(--ink)">Who owns the search results.</h1>
          </div>
          <div style="margin-left:auto">${reportButton(meta)}</div>
        </div>
        <div style="display:inline-flex;gap:24px">
          ${seg("keyword", "Keywords", kw.length)}${seg("serp", "Rankings", serp.length)}${seg("domain", "Domains", dom.length)}
        </div>
      </div>
      <div class="stage-scroll" style="padding:28px">
        ${runStatusNotes(state.runId, meta)}
        <div id="carriednotice"></div>
        <div id="comparestrip"></div>
        <div id="searchBody"></div>
      </div>
    </section>`;

  $("#region").innerHTML = `<div class="runview">${stage}</div>`;
  const segs = { keyword: searchKeywords, serp: searchSerps, domain: searchDomains };
  // No keywords, rankings or domains at all: pick no tab, so the empty message below shows.
  // The old last choice, "domain", always found its button and left an empty table
  // without a word.
  const first = kw.length ? "keyword" : serp.length ? "serp" : dom.length ? "domain" : null;
  document.querySelectorAll("[data-seg]").forEach(b => b.onclick = () => {
    document.querySelectorAll("[data-seg]").forEach(x => x.classList.remove("on"));
    b.classList.add("on"); $("#searchBody").innerHTML = segs[b.dataset.seg]();
    reportView();
  });
  const firstBtn = document.querySelector(`[data-seg="${first}"]`);
  if (firstBtn) { firstBtn.classList.add("on"); $("#searchBody").innerHTML = segs[first](); }
  else $("#searchBody").innerHTML = emptyResult("No search records in this run.");
  reportView();
}

// Compact inline sparkline (12-month volume trend). Pure SVG, themed stroke, no axes.
function sparkline(arr) {
  if (!Array.isArray(arr) || arr.length < 2) return "";
  const w = 66, h = 16, nums = arr.map(Number).filter(v => !isNaN(v));
  if (nums.length < 2) return "";
  const max = Math.max(...nums), min = Math.min(...nums), rng = (max - min) || 1;
  const pts = nums.map((v, i) => `${(i / (nums.length - 1) * w).toFixed(1)},${(h - ((v - min) / rng) * (h - 2) - 1).toFixed(1)}`).join(" ");
  return `<svg width="${w}" height="${h}" viewBox="0 0 ${w} ${h}" style="display:block;margin-top:4px;margin-left:auto" aria-hidden="true"><polyline points="${pts}" fill="none" stroke="var(--ember)" stroke-width="1.5" stroke-linejoin="round"/></svg>`;
}

function searchKeywords() {
  let kw = state.search.kw;
  const cUids = changeUids();
  if (cUids) kw = kw.filter(r => cUids.has(r.uid));
  if (!kw.length) return emptyResult(cUids ? "No keywords match this change filter." : "No keywords in this run.");
  const sort = state.search.kwSort;
  const col = KW_COLS.find(c => c.key === sort.col) || KW_COLS[1];
  const rows = sortBy(kw, col.get, col.num, sort.dir).map(r => {
    const d = r.data || {};
    const band = r.tags && r.tags.difficulty_band;
    const intent = r.tags && r.tags.intent;
    const kd = toNum(d.keyword_difficulty);
    const INTENT_TIPS = { informational: "People looking to learn or find information.", commercial: "People comparing options before buying.", transactional: "People ready to buy or complete an action.", navigational: "People looking for a specific website or page." };
    const intentTip = INTENT_TIPS[intent] || "The keyword's search intent as classified by the data provider.";
    const BAND_TIPS = { high: "Hard to rank on page one for this keyword.", medium: "Moderate difficulty. Some established competitors hold the top spots.", low: "Easier to rank on page one. Less competitive." };
    const bandTip = BAND_TIPS[band] || "How hard it is to rank on page one for this keyword.";
    // Semrush ad sightings for the term: the titles make a useful hover summary of who bids.
    const adEx = Array.isArray(d.ad_examples) ? d.ad_examples : [];
    const adTip = adEx.map(a => a.title || a.description).filter(Boolean).join("  •  ");
    // Difficulty bar graded in three tiers (not binary): high ranks loud in ember,
    // medium in ink, low recedes in muted — so the eye scans difficulty by weight.
    const barColor = { high: "var(--ember)", medium: "var(--ink)", low: "var(--muted-2)" }[band] || "var(--ink)";
    return `<tr>
      <td style="font-weight:600">${esc(r.text)}${intent ? `<span class="tag" style="font-size:10px;margin-left:8px" title="${esc(intentTip)}">${esc(intent)}</span>` : ""}${adEx.length ? `<span class="tag" title="${adEx.length} different ads were found running on this keyword${adTip ? esc(': ' + adTip) : '.'}" style="font-size:10px;margin-left:6px;cursor:help">${adEx.length} ads</span>` : ""}</td>
      <td style="text-align:right" class="num">${d.no_data === true ? "no data from Semrush" : has(d.volume) ? num(d.volume) : "-"}${sparkline(d.volume_trend)}</td>
      <td style="text-align:right" class="num">${has(d.cpc_usd) ? "$" + num(d.cpc_usd) : "-"}</td>
      <td style="text-align:right" class="num">${has(d.competition) ? d.competition : "-"}</td>
      <td style="width:220px">
        ${kd != null ? `<div style="display:flex;align-items:center;gap:8px">
          <div class="track" style="flex-grow:1"><div class="fill" style="width:${Math.max(0, Math.min(100, kd))}%;background:${barColor}"></div></div>
          <span class="num" style="font-size:12px;width:26px;text-align:right">${kd}</span>
          ${band ? `<span class="${band === "high" ? "tag accent" : "tag"}" style="font-size:10px" title="${esc(bandTip)}">${esc(band)}</span>` : ""}
        </div>` : "-"}
      </td></tr>`;
  }).join("");
  const heads = KW_COLS.map(c => {
    const active = c.key === sort.col;
    const caret = active ? (sort.dir === "desc" ? " ▼" : " ▲") : "";
    return `<th class="eb" onclick="sortKw('${c.key}')" title="${esc(c.tip || ('Sort by ' + c.label))}"
      style="text-align:${c.align};cursor:pointer;user-select:none;${active ? "color:var(--ember)" : ""}">${esc(c.label)}${caret}</th>`;
  }).join("");
  return `<div class="eb" style="margin-bottom:12px;color:var(--muted)">${kw.length} keywords · click a column to sort</div>
    <table class="dtable">
      <thead><tr>${heads}</tr></thead>
      <tbody>${rows}</tbody></table>`;
}

function sortKw(colKey) {
  const s = state.search.kwSort;
  const c = KW_COLS.find(x => x.key === colKey) || KW_COLS[1];
  if (s.col === colKey) s.dir = s.dir === "desc" ? "asc" : "desc";
  else { s.col = colKey; s.dir = c.num ? "desc" : "asc"; }
  lsSet("sms.kwSort", s);
  $("#searchBody").innerHTML = searchKeywords();
}

// SERP feature values are either a raw count (old index) or {count, items} (enriched).
function featCount(v) { return typeof v === "number" ? v : (v && typeof v === "object" ? (v.count != null ? v.count : (Array.isArray(v.items) ? v.items.length : 0)) : 0); }
function featItems(v) { return v && typeof v === "object" && Array.isArray(v.items) ? v.items : []; }

function searchSerps() {
  let serp = state.search.serp;
  const cUids = changeUids();
  if (cUids) serp = serp.filter(r => cUids.has(r.uid));
  if (!serp.length) return emptyResult(cUids ? "No rankings match this change filter." : "No rankings in this run.");
  return `<div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(440px,1fr));gap:20px">` +
    serp.map((r, i) => {
      const d = r.data || {}, results = d.results || [], sf = d.serp_features || {};
      const rows = results.map(x => {
        const href = x.url || (x.domain ? "https://" + x.domain : null);
        const title = href
          ? `<a href="${esc(href)}" target="_blank" rel="noopener noreferrer" title="Open this result in a new tab" style="color:inherit;text-decoration:none;border-bottom:1px solid var(--line-strong)">${esc(x.title || x.domain)} ↗</a>`
          : esc(x.title || x.domain);
        return `<div style="display:flex;align-items:baseline;gap:12px;padding:9px 0;border-bottom:1px solid var(--line)">
          <span class="rank" style="font-size:16px;width:22px" title="Position ${esc(x.position)} in Google's results for this query. The tool reads the first page, positions 1 to 10.">${esc(x.position)}</span>
          <div style="min-width:0">
            <div style="font-weight:600;font-size:13.5px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${title}</div>
            <div style="display:flex;gap:8px;align-items:baseline">
              <span class="num" style="font-size:11px;color:var(--ember);overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${esc(x.domain || x.displayed_url || "")}</span>
              ${has(x.date) ? `<span class="num" style="font-size:10.5px;color:var(--muted-2);white-space:nowrap">${esc(fmtDate(x.date) || "")}</span>` : ""}
            </div>
            ${has(x.description) ? `<div style="font-size:12px;color:var(--muted);margin-top:3px;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden">${esc(x.description)}</div>` : ""}
          </div>
        </div>`;
      }).join("");
      const entries = Object.entries(sf).filter(([, v]) => featCount(v) > 0);
      // Keep the standard .chip border (visible in BOTH themes); the old transparent-border
      // + dark fill was invisible in dark mode. The ember caret signals it expands.
      const SF_TIPS = { paid_results: "Ads shown on this query.", people_also_ask: "Questions Google surfaces as related to this query.", related_searches: "Queries Google suggests as related.", related_queries: "Queries Google lists as related to this one.", ai_overview: "Google's AI-generated summary for this query.", local_pack: "The map-based local business results.", knowledge_panel: "An information panel shown for this entity.", featured_snippet: "The highlighted answer box shown above organic results.", image_results: "Image results shown for this query.", video_results: "Video results shown for this query." };
      const sfTip = (k) => SF_TIPS[k] || (k.replace(/_/g, " ") + " feature shown on this page.");
      const chips = entries.map(([k, v]) => {
        const label = k.replace(/_/g, " "), count = featCount(v);
        if (!featItems(v).length) return `<span class="tag" title="${esc(sfTip(k))}">${esc(label)} ${count}</span>`;
        return `<button class="pbtn disc" aria-expanded="false" aria-controls="sf_${i}_${k}" onclick="toggleEl('sf_${i}_${k}',this)" title="${esc(sfTip(k))} Click to show or hide.">${esc(label)} ${count}${discState()}</button>`;
      }).join("");
      const panels = entries.map(([k, v]) => {
        const items = featItems(v);
        if (!items.length) return "";
        const body = items.map(it => {
          if (typeof it === "string") return `<div style="padding:3px 0;font-size:12.5px">${esc(it)}</div>`;
          const label = it.title || it.url || "";
          // For a paid ad, name the advertiser inline so the user sees WHO without clicking.
          const adv = it.advertiser ? ` <span style="color:var(--muted)">· ${esc(it.advertiser)}</span>` : "";
          const head = it.url
            ? `<a href="${esc(it.url)}" target="_blank" rel="noopener noreferrer" title="Open this result in a new tab" style="color:var(--ember);text-decoration:none">${esc(label)} ↗</a>${adv}`
            : `${esc(label)}${adv}`;
          // Paid ads also carry the ad's copy + its sitelink extensions (titles only -- the
          // sitelink URLs are aclk tracking redirects, so they are shown as plain text chips).
          const desc = it.description ? `<div style="font-size:11.5px;color:var(--muted);line-height:1.45;margin:2px 0">${esc(it.description)}</div>` : "";
          const sl = Array.isArray(it.sitelinks) && it.sitelinks.length
            ? `<div style="display:flex;gap:5px;flex-wrap:wrap;margin-top:3px">${it.sitelinks.map(s => `<span class="tag" style="font-size:10px" title="A link or extension shown as part of this ad.">${esc(s)}</span>`).join("")}</div>` : "";
          return `<div style="padding:${desc || sl ? "7px" : "3px"} 0;font-size:12.5px">${head}${desc}${sl}</div>`;
        }).join("");
        return `<div id="sf_${i}_${k}" style="display:none;margin-top:8px;border-left:2px solid var(--ember);padding-left:12px">
          <div class="eb" style="margin-bottom:4px">${esc(k.replace(/_/g, " "))}</div>${body}</div>`;
      }).join("");
      // Google's AI Overview for this query, if the raw scrape captured one: its generated
      // summary + the sources it cites. Collapsed by default so it doesn't dominate the card.
      const ai = d.ai_overview;
      // Google ships the AI overview as a flattened wall; emit restores it to lead + bullet blocks.
      const aiBody = ai && ai.blocks && ai.blocks.length
        ? ai.blocks.map(b => {
            if (b.kind === "bullet") {
              const ci = b.text.indexOf(":");
              const lab = ci > 0 && ci < 24 ? b.text.slice(0, ci + 1) : "";
              const rest = lab ? b.text.slice(ci + 1) : b.text;
              return `<div style="font-size:13px;line-height:1.5;color:var(--ink);padding-left:15px;text-indent:-15px;margin:3px 0"><span style="color:var(--ember)">•</span> ${lab ? `<strong>${esc(lab)}</strong>` : ""}${esc(rest)}</div>`;
            }
            return `<div style="font-size:13px;line-height:1.55;color:var(--ink);margin:4px 0">${esc(b.text)}</div>`;
          }).join("")
        : (ai ? `<div style="font-size:13px;line-height:1.55;color:var(--ink)">${esc(ai.content || "")}</div>` : "");
      const aiBlock = ai && ai.content ? `
        <div style="border:1px solid var(--ember-tint-border);background:var(--ember-tint);padding:10px 14px;margin-bottom:14px">
          <button class="disc-row" aria-expanded="false" aria-controls="ai_${i}" onclick="toggleEl('ai_${i}',this)" title="Show or hide Google's AI overview for this query"><span class="eb" style="color:var(--ember-press)">Google AI Overview${ai.sources && ai.sources.length ? " · " + ai.sources.length + " sources" : ""}</span>${discState()}</button>
          <div id="ai_${i}" style="display:none;margin-top:8px">
            ${aiBody}
            ${ai.sources && ai.sources.length ? `<div class="eb" style="margin:10px 0 4px" title="Pages Google cited in its AI overview for this query.">sources</div>` + ai.sources.map(s => {
              const dom = s.domain ? `<span style="color:var(--muted)"> · ${esc(s.domain)}</span>` : "";
              const desc = s.description ? `<div style="font-size:11px;color:var(--muted);line-height:1.4;margin:1px 0 2px">${esc(s.description)}</div>` : "";
              const head = s.url
                ? `<a href="${esc(s.url)}" target="_blank" rel="noopener noreferrer" title="Open this source in a new tab" style="color:var(--ember-press);text-decoration:none">${esc(s.title || s.url)} ↗</a>${dom}`
                : `${esc(s.title || "")}${dom}`;
              return `<div style="padding:3px 0;font-size:12px">${head}${desc}</div>`;
            }).join("") : ""}
          </div>
        </div>` : "";
      return `<article class="card" style="padding:20px">
        <div class="eb" style="margin-bottom:4px" title="The search phrase we looked up.">query</div>
        <div class="hl" style="font-size:19px;font-weight:700;margin-bottom:14px">${esc(r.text)}</div>
        ${aiBlock}
        ${rows}
        <div style="display:flex;gap:6px;flex-wrap:wrap;margin-top:14px">${chips}</div>
        ${panels}
      </article>`;
    }).join("") + `</div>`;
}

function searchDomains() {
  const dom = state.search.dom;
  if (!dom.length) return emptyResult("No domain data in this run.");
  const sort = state.search.domSort;
  const sd = DOM_SORTS.find(s => s.key === sort.col) || DOM_SORTS[0];
  const ordered = sortBy(dom, sd.get, true, sort.dir);
  const max = Math.max(...dom.map(r => toNum(r.data && r.data.keywords_owned) || 0), 1);
  const controls = DOM_SORTS.map(s => {
    const active = s.key === sort.col;
    const caret = active ? (sort.dir === "desc" ? " ▼" : " ▲") : "";
    return `<button class="${active ? "pbtn on" : "pbtn"}" onclick="sortDom('${s.key}')" title="${esc(s.tip)} Click to sort.">${esc(s.label)}${caret}</button>`;
  }).join("");
  const cards = ordered.map((r, i) => {
    const d = r.data || {};
    const owned = toNum(d.keywords_owned) || 0;
    const q = (state.search.domQ[r.text] || []).slice().sort((a, b) => a.position - b.position);
    const drill = q.length ? `
      <button class="pbtn disc" aria-expanded="false" aria-controls="dq_${i}" onclick="toggleEl('dq_${i}',this)" title="Show or hide the keywords this domain ranks for"
        style="align-self:flex-start">which keywords (${q.length})${discState()}</button>
      <div id="dq_${i}" style="display:none;border-top:1px solid var(--line);margin-top:2px;padding-top:8px">
        ${q.map(x => `<div style="display:flex;gap:10px;padding:4px 0;font-size:12.5px">
          <span class="num" style="color:var(--muted);width:30px">#${x.position}</span>
          <span style="min-width:0">${esc(x.query)}</span></div>`).join("")}
      </div>` : "";
    return `<article class="card" style="padding:20px;gap:12px">
      <div style="display:flex;align-items:baseline;gap:12px">
        <span class="rank" style="font-size:34px">${i + 1}</span>
        <div style="min-width:0"><div class="hl" style="font-size:20px;font-weight:700">${esc(r.text)}</div>
        ${has(r.url) ? `<a href="${esc(r.url)}" target="_blank" rel="noopener noreferrer" class="num" title="Open this domain in a new tab" style="font-size:11px;color:var(--ember);display:inline-block;max-width:100%">${esc(r.url)} ↗</a>` : ""}</div>
      </div>
      <div class="track"><div class="fill" style="width:${Math.round((owned / max) * 100)}%;background:var(--ember)"></div></div>
      <div style="display:flex;gap:18px">
        ${stat(d.keywords_owned, "keywords owned", "Keywords where this domain appears in Google's top 10.")}
        ${stat(d.avg_position, "average position", "This domain's typical rank across the keywords it appears in.")}
        ${stat(d.best_position, "best position", "This domain's highest rank anywhere in the results. Not always 1.")}
      </div>
      ${drill}
    </article>`;
  }).join("");
  return `<div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin-bottom:16px">
      <span class="eb" style="color:var(--muted)" title="Sort the list by one of these metrics.">sort by</span>${controls}</div>
    <div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(320px,1fr));gap:18px">${cards}</div>`;
}

function sortDom(colKey) {
  const s = state.search.domSort;
  const sd = DOM_SORTS.find(x => x.key === colKey) || DOM_SORTS[0];
  if (s.col === colKey) s.dir = s.dir === "desc" ? "asc" : "desc";
  else { s.col = colKey; s.dir = sd.defDir; }
  lsSet("sms.domSort", s);
  $("#searchBody").innerHTML = searchDomains();
}

function stat(v, label, tip) {
  return `<div><div class="num" style="font-family:var(--font-head);font-weight:700;font-size:22px">${has(v) ? num(v) : "-"}</div>
    <div class="eb" style="font-size:9px;margin-top:2px"${tip ? ` title="${esc(tip)}"` : ""}>${esc(label)}</div></div>`;
}

/* ============================================================================
   CAMPAIGN DOSSIER — the whole picture across a subject's runs
   ========================================================================== */

// Returns the tooltip text for a run chip on the campaign dossier page.
// For unindexed runs the text describes the index state; for indexed runs it
// describes whether this is the latest or an older run.
// An unindexed run with a high run_seq must not say "Older run." --
// it has not been indexed yet, so it is not summarised here at all.
// Pure function.
// monthFinished (optional, default true) is whether a month of this tool finished at all.
// When none did, the summaries cannot come from "the latest month that finished", so the
// tip says there are none (the all-stopped project).
function chipTitle(indexed, isLatest, toolLabel, status, monthFinished = true) {
  if (!indexed) {
    if (status === "refused") {
      return "This month's scheduled run did not start, so nothing was collected and nothing was spent. Open it to see why.";
    }
    if (status === "incomplete") {
      return "This month's run stopped before it finished, so it has no results to show here. Open it to see what it said.";
    }
    return "No index yet, so this run is not summarised here. Open it to see its status.";
  }
  // Indexed, but the month's own notification says it did not finish (its folder was
  // re-indexed by hand): newestPart skips it, so it is neither "latest" nor "older".
  if (status === "refused") {
    return monthFinished
      ? "This month's scheduled run did not start, so the summaries here come from the latest month that finished. Open it to see what is in its folder."
      : `This month's scheduled run did not start, and no ${toolLabel.toLowerCase()} run in this project has finished, so there is no summary for it yet. Open it to see what is in its folder.`;
  }
  if (status === "incomplete") {
    return monthFinished
      ? "This month's run stopped before it finished, so the summaries here come from the latest month that finished. Open it to see what is in its folder."
      : `This month's run stopped before it finished, and no ${toolLabel.toLowerCase()} run in this project has finished, so there is no summary for it yet. Open it to see what is in its folder.`;
  }
  const prefix = isLatest ? "This is the most recent run. " : "Older run. ";
  return prefix + "Open the " + toolLabel.toLowerCase() + " records for this run";
}

// How a scheduled run ended, as the run itself recorded it in notification.json
// (the server's discover_runs passes it forward on the registry row). "refused" or
// "incomplete", else "" (a normal run, or a "done" scheduled run).
// Pure function.
function runEndStatus(r) {
  const s = r && r.notification && r.notification.status;
  return s === "refused" || s === "incomplete" ? s : "";
}
// The label for a run with no index, on the project page's chips. A refused month was
// never scraped, so "no index" (which invites a re-index) would be false for it.
// Pure function.
function unindexedLabel(r) {
  const s = runEndStatus(r);
  return s === "refused" ? "skipped" : s === "incomplete" ? "stopped early" : "no index";
}
// The same, shortened for the narrow sidebar.
function railStatusLabel(r) {
  const s = runEndStatus(r);
  return s === "refused" ? "skipped" : s === "incomplete" ? "stopped" : "no idx";
}

// What the "analysed" number on the reviews page is: synthesis meta.total_reviews,
// counted by the extractor over reviews that have text and a rating, after
// near-identical copies are collapsed. It exists whether or not the written reports
// were produced, so the tip must not claim the reports used it.
const ANALYSED_TIP = "Reviews counted in this run's pain and trust analysis: every review with text and a rating, with near-identical copies counted once.";

// Plain notes at the top of a run page (every tool):
//  - the run's scheduled month reported a problem (index present, notification says
//    refused or incomplete): say so, with the run's own message;
//  - the run has no written reports: say so, so the page never implies a summary exists.
// Pure function (reads only its arguments and state.registry).
function runStatusNotes(runId, meta) {
  const row = (state.registry || []).find(r => r.run_id === runId);
  const out = [];
  const st = runEndStatus(row);
  if (st) {
    const msg = row.notification.message;
    const lead = st === "refused"
      ? "This month's scheduled run was refused"
      : "This month's run stopped before it finished";
    out.push(`<div class="carriednotice" data-note="run-ended"><div>${esc(lead)}${msg ? `, and said: ${esc(msg)}` : "."}
      The records below are what is in this folder now. If the run was finished by hand later, this note is out of date.</div></div>`);
  }
  // A step that did not finish (it failed, the budget cap stopped it, or its
  // scraper run did not work) left no results, which reads the same as a step that ran and
  // found nothing. The index carries one plain sentence per such step (emit_index
  // "unfinished_steps", made by postprocess.unfinished_steps from the records run_phase
  // wrote); shown as written, never worked out here. Nothing for an index without the key.
  // Inline, so this function still stands alone (the tests that load it by name).
  const unfinished = (meta && Array.isArray(meta.unfinished_steps) ? meta.unfinished_steps : [])
    .map(s => s && s.sentence).filter(Boolean);
  if (unfinished.length) {
    out.push(`<div class="carriednotice" data-note="unfinished-steps">${unfinished.map(l => `<div>${esc(l)}</div>`).join("")}</div>`);
  }
  // What a scraper itself said about a place it searched (emit_index "source_notes":
  // the Meta library's "this page is not currently running ads", or an error whose
  // result is unknown). Such a record is not an ad, so it has no card and no count; the
  // sentence was made once by the ad tool (adrecords) and is shown as written, never
  // worked out here. Nothing for an index without the key. Inline, like the notes above.
  const sourceNotes = (meta && Array.isArray(meta.source_notes) ? meta.source_notes : [])
    .map(s => s && s.sentence).filter(Boolean);
  if (sourceNotes.length) {
    out.push(`<div class="carriednotice" data-note="source-notes">${sourceNotes.map(l => `<div>${esc(l)}</div>`).join("")}</div>`);
  }
  out.push(setAsideNote(meta));
  out.push(reportCheckNote(meta));
  if (meta && Array.isArray(meta.reports) && meta.reports.length === 0) {
    out.push(`<div class="carriednotice" data-note="no-report"><div>The written summary was not produced for this run. All collected records are shown here.</div></div>`);
  }
  return out.join("");
}

// Ads the ad tool set aside as another advertiser's are not
// in the index's records, so every ad count the app shows is the smaller number. The
// index carries the set-aside per platform (emit_index: count, summary, lines, all
// written by code); the app shows them as they are, under the count, and never
// recounts. "" when nothing was set aside (and for an index from an earlier version).
// Pure function.
function setAsideNote(meta) {
  const sa = meta && meta.set_aside;
  if (!sa || typeof sa !== "object") return "";
  const parts = Object.values(sa).filter(p => p && (p.summary || (p.lines || []).length));
  if (!parts.length) return "";
  const body = parts.map(p => {
    const lines = (p.lines || []).map(l => `<li>${esc(l)}</li>`).join("");
    return `${p.summary ? `<div>${esc(p.summary)}</div>` : ""}${lines ? `<ul style="margin:6px 0 0 18px;padding:0">${lines}</ul>` : ""}`;
  }).join("");
  // An index that carries the ads themselves shows them at the end of the page
  // (setAsideSection). Checked inline so this function still stands alone.
  const where = Object.values(sa).some(p => p && p.labelled && Array.isArray(p.labelled.ads) && p.labelled.ads.length)
    ? `<div data-note="set-aside-where">${esc("These ads are shown at the end of this page, labelled as not confirmed as the competitor's.")}</div>` : "";
  return `<div class="carriednotice" data-note="set-aside"><div>${body}${where}</div></div>`;
}

// ("label them"): the set-aside Google ads themselves.
// A set-aside ad may be the competitor's own under another legal name ("37signals LLC"
// for Basecamp), so the user can read it, but only in its own section at the end of the
// ad library, under the label the ad tool wrote (emit_index.labelled_set_aside, inside
// the index's "set_aside" entry, never in "records"): no count, filter, star or
// month-to-month comparison in the app includes these ads. "" for an index without them
// (every index from an earlier version). Pure function.
const SET_ASIDE_HEADING = "Google ads not confirmed as the competitor's";
// The same words emit_index.SET_ASIDE_NO_NAME and the report use.
const SET_ASIDE_NO_NAME = "(no advertiser name given)";
function setAsideAds(meta) {
  const sa = meta && meta.set_aside;
  if (!sa || typeof sa !== "object") return [];
  return Object.values(sa).filter(p => p && p.labelled && Array.isArray(p.labelled.ads) && p.labelled.ads.length)
    .map(p => p.labelled);
}
function setAsideSection(meta) {
  const parts = setAsideAds(meta);
  if (!parts.length) return "";
  const blocks = parts.map(lab => {
    const ads = lab.ads.filter(a => a && typeof a === "object");
    const groups = (Array.isArray(lab.labels) ? lab.labels : []).map(l => {
      const mine = ads.filter(a => ((a.set_aside || {}).searched || "") === ((l && l.searched) || ""));
      if (!mine.length) return "";
      return `<div class="carriednotice" data-note="set-aside-label"><div>${esc(l.label)}</div></div>
        <div class="setaside-grid" style="column-count:auto;column-width:300px;column-gap:22px">${mine.map(a => adCard(a, true)).join("")}</div>`;
    }).join("");
    return `${groups}${has(lab.confirm) ? `<div class="carriednotice" data-note="set-aside-confirm"><div>${esc(lab.confirm)}</div></div>` : ""}`;
  }).join("");
  return `<section data-note="set-aside-ads" style="margin-top:34px;padding-top:18px;border-top:1px dashed var(--line)">
    <h2 style="font-family:var(--font-head);font-size:18px;margin:0 0 10px">${esc(SET_ASIDE_HEADING)}</h2>
    ${blocks}
  </section>`;
}

// What the report step's automated check found in the written reports
// (quotes it could not match to the collected reviews, numbers it could not match to
// the counted data). The server reads it from the report files with the tool's own
// check (viewer_server._report_check) and hands it over as meta.report_check; the app
// shows those lines as written and never counts anything itself. Earlier these facts reached the
// user only through Claude's reply, which lost them.
const REPORT_CHECK_UNREAD = "The app could not read what the automated check found in the written reports. Each report lists any quoted passage or number it could not match under the heading \"Provenance note (automated check)\".";
function reportCheckBox(inner) {
  return `<div class="carriednotice" data-note="report-check"><div>${inner}</div></div>`;
}
// On the run page: where the reports are, then one line per checked report.
// "" when the run has no checked reports (an ad run, or no report was written).
// Pure function.
function reportCheckNote(meta) {
  const rc = meta && meta.report_check;
  if (!rc || typeof rc !== "object") return "";
  if (rc.error) return reportCheckBox(esc(REPORT_CHECK_UNREAD));
  const lines = (Array.isArray(rc.files) ? rc.files : [])
    .filter(f => f && f.line).map(f => `<li>${esc(f.line)}</li>`).join("");
  if (!lines) return "";
  return reportCheckBox(`${rc.folder_line ? `<div>${esc(rc.folder_line)}</div>` : ""}<div>What the automated check found:</div><ul style="margin:6px 0 0 18px;padding:0">${lines}</ul>`);
}
// Above one report: its own line, only when the file does not already start with it
// (a report written by an earlier version has no block at its top). Pure function.
function reportCheckForFile(meta, path) {
  const rc = meta && meta.report_check;
  if (!rc || typeof rc !== "object") return "";
  if (rc.error) return reportCheckBox(esc(REPORT_CHECK_UNREAD));
  const f = (Array.isArray(rc.files) ? rc.files : []).find(x => x && x.file === path);
  if (!f || f.top_block || !f.line) return "";
  return reportCheckBox(`<div>What the automated check found:</div><div>${esc(f.line)}</div>`);
}

// The short form under an ad count on the project page: the summary sentences only.
function setAsideShort(meta) {
  const sa = meta && meta.set_aside;
  if (!sa || typeof sa !== "object") return "";
  return Object.values(sa).map(p => (p && p.summary) || "").filter(Boolean).join(" ");
}

// Returns the pain panel to show when the newest indexed pain run exists but has
// no synthesis.  Never falls back to an older run's synthesis (FROZEN:
// the page must never silently show an older run's numbers).
// Pure function.
function noSummaryPainPanel(runId, lbl) {
  const href = "#/run/" + encodeURIComponent(runId);
  return `<article class="card" style="padding:24px;grid-column:1/-1">
    <div class="eb" style="color:var(--muted);margin-bottom:10px">Reviews · no summary yet</div>
    <p style="font-size:14px;color:var(--muted);margin:0">The latest reviews run, <a href="${href}">${esc(lbl)}</a>, has no summary yet. Open the run to see its records.</p>
  </article>`;
}

// Pick the newest run for one tool from an array of {row, idx} parts.
// "Newest" means the highest run_seq, which viewer_server._assign_run_seq sets
// using the run ordering (folder date, then _vN version, then generated_at).
// A month whose notification says "refused" or "incomplete" is skipped even when it
// has an index (a refused or stopped folder re-indexed by hand holds no complete
// month). WHY: repeat.py's _run_completed skips exactly these months, so the
// app's "latest" and the month the next repeat builds on stay the same month.
// The app never re-derives this order from folder names in JS — a second
// implementation would silently diverge from the server on edge cases.
// Pure function.
function newestPart(parts, tool) {
  const matching = parts.filter(p => (p.idx.tool || p.row.tool) === tool && !runEndStatus(p.row));
  if (!matching.length) return null;
  return matching.reduce((best, p) =>
    (p.row.run_seq ?? -1) > (best.row.run_seq ?? -1) ? p : best
  );
}

async function openCampaign(subject) {
  state.runId = null;
  syncRailActive();
  renderLoading("Building the full picture for " + subject);
  const camp = state.campaigns.find(c => c.subject === subject);
  if (!camp) return renderFatal("Project not found.", subject);

  const indexed = camp.runs.filter(r => r.indexed);
  const idxs = await Promise.all(indexed.map(r => api.index(r.run_id).then(x => ({ row: r, idx: x })).catch(() => null)));
  const parts = idxs.filter(Boolean);
  const pain = newestPart(parts, "pain");
  const ad = newestPart(parts, "ad");
  const search = newestPart(parts, "search");
  // Every month of the project stopped early or did not start: newestPart drops all of
  // them, so no panel can render. Say so, rather than leave an empty page under the chips.
  const allStopped = camp.runs.length > 0 && camp.runs.every(r => runEndStatus(r));

  const syn = pain && pain.idx.synthesis;
  const verdict = campaignVerdict(pain, ad, search);

  // When pain has a newest run but no synthesis, show a plain note rather than
  // leaving the panel blank.  Never fall back to an older run (FROZEN).
  const painPanel = syn
    ? synthesisPanel(syn, pain.row.run_id, runLabel(pain.row))
    : (pain ? noSummaryPainPanel(pain.row.run_id, runLabel(pain.row)) : "");
  const searchPanel = search ? searchLeaderPanel(search) : "";
  const adPanel = ad ? adHighlightPanel(ad) : "";
  // Header chips: ordered by section (Reviews, Search, Ads), then newest first within each
  // tool. Chips for the runs the panels read from are marked "latest" (ember chip) so a
  // user can see at a glance which run each section summarises. Older runs are secondary
  // (plain chip). camp.runs is not mutated here.
  const latestRunIds = new Set([pain, ad, search].filter(Boolean).map(p => p.row.run_id));
  const runLinks = [...camp.runs]
    .sort((a, b) => {
      const t = toolOrder(a.tool) - toolOrder(b.tool);
      if (t !== 0) return t;
      return (b.run_seq ?? -1) - (a.run_seq ?? -1);
    })
    .map(r => {
      const isLatest = latestRunIds.has(r.run_id);
      const cls = isLatest ? "pbtn on" : "pbtn";
      const dateLabel = runLabel(r);
      const latestMark = isLatest ? " · latest" : "";
      const unindexed = r.indexed && !runEndStatus(r) ? "" : " (" + unindexedLabel(r) + ")";
      // chipTitle handles unindexed runs separately so they never say "Older run."
      const tip = chipTitle(r.indexed, isLatest, TOOL_LABEL[r.tool] || r.tool, runEndStatus(r), !!newestPart(parts, r.tool));
      return `<a href="#/run/${encodeURIComponent(r.run_id)}" class="${cls}"
        title="${tip}"
        >${esc(TOOL_LABEL[r.tool] || r.tool)} · ${esc(dateLabel)}${esc(latestMark)}${esc(unindexed)}</a>`;
    }).join("");

  $("#region").innerHTML = `
    <section class="stage">
      ${grainSVG()}
      <div class="masthead" style="padding:0;border:none;flex-direction:column;align-items:stretch">
        <div style="padding:28px 32px 22px">
          <div style="margin-bottom:16px">${backButton("#/", "All projects", "Back to all projects")}</div>
          <div class="eb" style="color:var(--ember);margin-bottom:8px">the full picture · ${camp.runs.length} run${camp.runs.length === 1 ? "" : "s"}</div>
          <h1 class="hl" style="font-size:40px;font-weight:700;color:var(--ink)">${esc(subject)}</h1>
          <div style="display:flex;gap:8px;flex-wrap:wrap;margin-top:16px">${runLinks}</div>
        </div>
        ${verdict}
      </div>
      <div class="stage-scroll" style="padding:28px">
        <div class="two-up">
          ${painPanel}${searchPanel}${adPanel}
        </div>
        ${allStopped ? emptyResult("No month of this project has finished yet, so there is nothing to summarise. Every month listed above stopped early or did not start; open one to see what it said.")
          : parts.length ? "" : emptyResult("No indexed runs for this project yet.")}
      </div>
    </section>`;
}

// Synthesis phrase count: the emit tags each phrase with its star_band and gives it a
// band-specific low_count / high_count (there is no flat `count`). Read the count in the
// phrase's own band; keep `count` first for backward-compat with any older index.
function phraseCount(p) {
  if (!p) return 0;
  if (p.count != null) return p.count;
  return (p.star_band === "high" ? p.high_count : p.low_count) || 0;
}

// A phrase list in the order its words claim: most reviews first. The current extractor
// already writes the list this way, but an index written before that change is ordered by
// distinctiveness score, which put "dont" (14 reviews) above "not" (35) on the page and in
// the "complaint mentioned most" headline. Sorted on a copy, so the
// index data is untouched; the sort is stable, so equal counts keep the index's order and a
// list that is already in order comes out unchanged.
function byMentionCount(arr) {
  return (arr || []).slice().sort((a, b) => phraseCount(b) - phraseCount(a));
}

function campaignVerdict(pain, ad, search) {
  const syn = pain && pain.idx.synthesis;
  if (syn && syn.pain_points_low_star && syn.pain_points_low_star.length) {
    // [0] of the list in mention order is the phrase the most low-star reviews mention.
    // The number is that count; low_count is a
    // per-review count (each review counts a phrase once), so "N of M reviews" is a
    // literal, checkable claim -- no distinctiveness jargon, no unfounded superlative.
    const top = byMentionCount(syn.pain_points_low_star)[0];
    const n = phraseCount(top);
    const m = (syn.meta && syn.meta.low_star_n) || null;
    const runLbl = pain && pain.row ? runLabel(pain.row) : null;
    return `<div style="background:var(--paper-2);border-top:1px solid var(--line);padding:24px 32px;display:flex;align-items:center;gap:24px">
      <div class="num" style="font-family:var(--font-head);font-weight:700;font-size:96px;line-height:.9;color:var(--ember)">${num(n)}</div>
      <div><div class="eb" style="color:var(--muted);margin-bottom:6px" title="The single word or phrase appearing in the most low-star reviews.">the complaint mentioned most</div>
      <div class="hl" style="font-size:26px;font-weight:600;color:var(--ink);max-width:520px">“${esc(top.phrase)}”</div>
      <div class="eb" style="color:var(--muted);margin-top:8px" title="How many of the analysed 1 to 2 star reviews mention this phrase.">${m ? `in ${num(n)} of ${num(m)} low-star reviews` : `in ${num(n)} low-star reviews`}</div>
      ${runLbl ? `<div style="font-size:12px;color:var(--muted-2);margin-top:6px">From the latest reviews run, ${esc(runLbl)}.</div>` : ""}</div>
    </div>`;
  }
  return "";
}

// The tip on a "source N analysed" chip on the project page. N is how many of that
// source's reviews the counts above were made from (meta.source_distribution: the
// reviews with text and stars, near-identical duplicates removed, Reddit items judged
// neither complaint nor praise set aside). The chip opens the source's review records,
// which are the records themselves, so the page's own count can differ; the tip says so
// instead of promising N (for example the chip counts: Google Maps 17 against 20 shown).
// Pure function.
function sourceChipTip(source, n) {
  return `Open the ${platLabel(source)} review records. The counts above were made from the ${num(n)} of them that were analysed; the page lists the records themselves, so its own count can differ.`;
}

function synthesisPanel(syn, runId, runLbl) {
  const meta = syn.meta || {};
  const bars = (arr, tint) => {
    const max = Math.max(...(arr || []).map(phraseCount), 1);
    return byMentionCount(arr).map(p => {
      const c = phraseCount(p);
      return `<div style="margin-bottom:12px">
        <div style="display:flex;justify-content:space-between;font-size:13.5px;margin-bottom:5px">
          <span style="font-weight:500">${esc(p.phrase)}</span><span class="num" style="color:var(--muted)">${num(c)}</span></div>
        <div class="track"><div class="fill" style="width:${Math.round((c / max) * 100)}%;background:${tint}"></div></div>
      </div>`;
    }).join("");
  };
  return `
    <article class="card" style="padding:24px;grid-column:1/-1">
      <div class="eb" style="margin-bottom:4px" title="Common words counted across all the reviews in this run.">counted across ${num(meta.total_reviews)} reviews</div>
      <h2 class="hl" style="font-size:22px;font-weight:700;margin-bottom:8px">What people complain about, and what they praise</h2>
      ${runLbl ? `<div style="font-size:12.5px;color:var(--muted);margin:0 0 14px">From the latest reviews run, ${esc(runLbl)}.</div>` : ""}
      <p style="font-size:13.5px;color:var(--muted);margin:0 0 22px;max-width:660px">Each phrase is ordered by how many reviews mention it. Common filler words, and brand or product names that show up in good and bad reviews alike, are removed.</p>
      <div class="two-up" style="gap:32px">
        <div><div class="eb" style="color:var(--ember);margin-bottom:14px" title="Words that appear most often in 1 to 2 star reviews.">Pain · ${num(meta.low_star_n)} low-star</div>${bars(syn.pain_points_low_star, "var(--ember)")}</div>
        <div><div class="eb" style="margin-bottom:14px" title="Words that appear most often in 4 to 5 star reviews.">Trust · ${num(meta.high_star_n)} high-star</div>${bars(syn.trust_signals_high_star, "var(--ink)")}</div>
      </div>
      ${runId ? `<div style="margin-top:20px;padding-top:18px;border-top:1px solid var(--line)">
        <div class="eb" style="color:var(--muted);margin-bottom:10px" title="Open the individual review records from this run.">Open the reviews${meta.source_distribution ? ", filtered by source" : ""}</div>
        <div style="display:flex;gap:8px;flex-wrap:wrap;align-items:center">
          ${meta.source_distribution ? Object.entries(meta.source_distribution).map(([k, v]) =>
            `<a href="#/run/${encodeURIComponent(runId)}?source=${encodeURIComponent(k)}" class="pbtn" title="${esc(sourceChipTip(k, v))}">${esc(platLabel(k))} ${num(v)} analysed →</a>`).join("") : ""}
          <a href="#/run/${encodeURIComponent(runId)}" class="pbtn" title="Open all review records in this run">all reviews →</a>
        </div>
      </div>` : ""}
    </article>`;
}

function searchLeaderPanel(search) {
  const doms = (search.idx.records || []).filter(r => r.kind === "domain")
    .sort((a, b) => ((b.data && b.data.keywords_owned) || 0) - ((a.data && a.data.keywords_owned) || 0));
  const leader = doms[0];
  return `
    <article class="card" style="padding:24px">
      <div class="eb" style="color:var(--ember);margin-bottom:4px">search landscape</div>
      <h2 class="hl" style="font-size:22px;font-weight:700;color:var(--ink);margin-bottom:18px">Who owns the results</h2>
      ${leader ? `<div class="num" style="font-family:var(--font-head);font-weight:700;font-size:30px;color:var(--ink)">${esc(leader.text)}</div>
        <div class="eb" style="color:var(--muted);margin-top:6px">${num((leader.data && leader.data.keywords_owned) || 0)} keywords owned · average position ${esc((leader.data && leader.data.avg_position) ?? "unknown")}</div>`
        : `<div class="eb" style="color:var(--muted)">no domain ranking captured</div>`}
      <a href="#/run/${encodeURIComponent(search.row.run_id)}" class="pbtn" style="margin-top:18px;align-self:flex-start" title="Open the full search landscape for this run">open search run →</a>
    </article>`;
}

function adHighlightPanel(ad) {
  const recs = ad.idx.records || [];
  const withCreative = recs.filter(r => (r.media || []).length).slice(0, 3);
  const thumbs = withCreative.map(r => {
    // For a thumbnail, show the poster frame (a video ad has its poster jpg on disk too),
    // with a small "video" badge -- not a dead black tile.
    const img = (r.media || []).find(m => m.type === "image");
    const isVid = (r.media || []).some(m => m.type === "video");
    return img
      ? `<div style="position:relative"><img src="${esc(api.fileURL(ad.row.run_id, img.path))}" style="width:100%;aspect-ratio:1;object-fit:cover;background:var(--creative-bg)" loading="lazy">${isVid ? `<span class="eb" style="position:absolute;bottom:6px;right:6px;background:var(--ink);color:var(--ember-lite);padding:3px 7px;border-radius:var(--radius-pill)">video</span>` : ""}</div>`
      : `<div style="aspect-ratio:1;background:#000;display:flex;align-items:center;justify-content:center"><span class="eb" style="color:var(--ember)">video</span></div>`;
  }).join("");
  return `
    <article class="card" style="padding:24px">
      <div class="eb" style="margin-bottom:4px">ads · ${num((ad.idx.counts && ad.idx.counts.records) || recs.length)}</div>
      ${setAsideShort(ad.idx) ? `<div data-note="set-aside-short" style="font-size:12.5px;color:var(--muted);margin:0 0 10px">${esc(setAsideShort(ad.idx))}</div>` : ""}
      <h2 class="hl" style="font-size:22px;font-weight:700;margin-bottom:18px">Ad creative on file</h2>
      ${thumbs ? `<div style="display:grid;grid-template-columns:repeat(3,1fr);gap:8px;margin-bottom:16px">${thumbs}</div>` : ""}
      <a href="#/run/${encodeURIComponent(ad.row.run_id)}" class="pbtn" style="align-self:flex-start" title="Open the full ad library for this run">open ad library →</a>
    </article>`;
}

/* ============================================================================
   REPORTS — rendered markdown from /files
   ========================================================================== */
// A report chip's words, escaped. The chip showed the report's id before
// its title ("E3 E3 creative angles"). The id (E1, E3) is the pipeline's internal
// name for the report, which a user never chose, so it is never shown. An index
// written by an earlier version also starts the title with the id; only a title that
// starts with its own id and a space loses it, so no other title is touched.
function reportChipLabel(rep) {
  let t = String(rep.title || "");
  if (rep.id && t.startsWith(rep.id + " ")) {
    t = t.slice(rep.id.length + 1);
    t = t.charAt(0).toUpperCase() + t.slice(1);
  }
  return esc(t || rep.path);
}

async function openReport(runId, reportPath) {
  state.runId = runId;
  syncRailActive();
  renderLoading("Opening the report");
  let idx = state.index;
  if (!idx || state.index.run_id !== runId) {
    try { idx = await api.index(runId); state.index = idx; } catch (e) { return renderFatal("Could not read run.", e.message); }
  }
  const reports = idx.reports || [];
  let md = "";
  try {
    const r = await fetch(api.fileURL(runId, reportPath));
    md = r.ok ? await r.text() : `# Report not found\n\nThe file \`${reportPath}\` is not on disk.`;
  } catch (e) { md = "# Could not load report\n\n" + e.message; }

  const switcher = reports.map(rep => {
    const active = rep.path === reportPath;
    // Active chip uses ink-on-paper so it stands out on the ember-tint band background;
    // chip.ember would vanish (same tint bg as the band). Inactive chips stay as plain outlined chips.
    const cls = active ? "pbtn current" : "pbtn";
    return `<a href="#/report/${encodeURIComponent(runId)}//${encodeURIComponent(rep.path)}"
        class="${cls}"${active ? ' aria-current="page"' : ''} title="${active ? `The ${reportChipLabel(rep)} report is open.` : `Open the ${reportChipLabel(rep)} report`}">${reportChipLabel(rep)}</a>`;
  }).join("");

  // Collect headings while rendering so we can build a contents rail from them.
  const headings = [];
  const bodyHtml = renderMarkdown(md, headings);
  const toc = reportTOC(headings);
  // TOC visibility: default true (shown), persisted per-viewer; ignored under 920px
  // (media query display:none overrides regardless of toggle state; adjustable).
  const tocVisible = toc ? lsGet("mv.toc", true) : false;
  // Name the exit by where it goes: a pain run's records are reviews, an ad
  // run's are ads. "← records" was too vague to notice; a named button is not.
  const backLabel = (TOOL_LABEL[idx.tool] || "records").toLowerCase();
  // TOC toggle button lives inside the report-switch band so it reads as part of
  // the same controls row ("in or beside the report switch band, your call").
  const tocToggleBtn = toc
    ? `<button id="toc-toggle-btn" class="ctl" style="margin-left:auto;flex-shrink:0" aria-expanded="${tocVisible}" title="${tocVisible ? "Hide contents" : "Show contents"}">${tocVisible ? "Hide contents" : "Contents"}</button>`
    : "";
  const showBand = switcher || !!toc;
  const bandHtml = showBand
    ? `<div class="report-switch">
        ${switcher ? `<span class="report-switch-lbl">Reports in this run</span>${switcher}` : ""}
        ${tocToggleBtn}
      </div>`
    : "";

  $("#region").innerHTML = `
    <section class="stage">
      ${grainSVG()}
      <div class="masthead">
        <a href="#/run/${encodeURIComponent(runId)}" class="ctl" style="text-decoration:none;gap:8px" title="Back to the ${esc(backLabel)} this report was built from">← Back to ${esc(backLabel)}</a>
        <div style="flex-grow:1">
          <div class="eb" style="color:var(--ember);margin-bottom:6px">${esc(idx.subject || "")} · report</div>
          <h1 class="hl" style="font-size:26px;font-weight:700;color:var(--ink)">${esc(runLabel(state.registry.find(r => r.run_id === runId)) || shortRun(runId))}</h1>
        </div>
      </div>
      <div class="stage-scroll" style="padding:28px">
        ${bandHtml}
        ${reportCheckForFile(idx, reportPath)}
        <div class="report-layout">
          ${toc}
          <div class="md">${bodyHtml}</div>
        </div>
      </div>
      <button class="backtotop" title="Back to top" aria-label="Back to top">↑</button>
    </section>`;

  // Apply initial TOC visibility without a second render pass.
  const tocEl = $("#region .report-toc");
  if (tocEl && !tocVisible) tocEl.classList.add("toc-hidden");

  // Wire TOC toggle button.
  const tocBtnEl = document.getElementById("toc-toggle-btn");
  if (tocBtnEl && tocEl) {
    tocBtnEl.addEventListener("click", () => {
      const nowHidden = tocEl.classList.toggle("toc-hidden");
      const nowVisible = !nowHidden;
      lsSet("mv.toc", nowVisible);
      tocBtnEl.setAttribute("aria-expanded", String(nowVisible));
      tocBtnEl.title = nowVisible ? "Hide contents" : "Show contents";
      tocBtnEl.textContent = nowVisible ? "Hide contents" : "Contents";
    });
  }

  // Wire back-to-top: show after 300 px of scroll; respect reduced-motion.
  const scroll = $("#region .stage-scroll");
  const btn    = $("#region .backtotop");
  // Sticky band: publish its height so the contents rail and heading jumps sit below it
  // (it wraps to more lines on narrow stages, hence a ResizeObserver, not a one-off read).
  const band = $("#region .report-switch");
  if (window._bandRO) { window._bandRO.disconnect(); window._bandRO = null; }
  if (scroll && band) {
    // --band-off: sticky top is measured from the scroller's content edge, so the band
    // actually pins one padding-top below the scroller; heading jumps must clear that too.
    const setBandH = () => {
      scroll.style.setProperty("--band-h", band.offsetHeight + "px");
      scroll.style.setProperty("--band-off", (parseFloat(getComputedStyle(scroll).paddingTop) || 0) + "px");
    };
    setBandH();
    if (window.ResizeObserver) { window._bandRO = new ResizeObserver(setBandH); window._bandRO.observe(band); }
  }
  if (scroll && btn) {
    scroll.addEventListener("scroll", () => {
      btn.classList.toggle("show", scroll.scrollTop > 300);
      if (band) band.classList.toggle("stuck", scroll.scrollTop > 0);
    });
    btn.addEventListener("click", () => {
      const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
      scroll.scrollTo({ top: 0, behavior: reducedMotion ? "auto" : "smooth" });
    });
  }
}

/* ---- generic / not-indexed / states ------------------------------------- */
function renderGenericRun() {
  const recs = (state.index.records || []).filter(r => !state.ann.trashed[r.uid]);
  $("#region").innerHTML = `
    <section class="stage">${grainSVG()}
      ${surfaceMasthead("Run", state.index, [countChip(recs.length, "records")])}
      <div class="stage-scroll" style="padding:28px">
        <div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(320px,1fr));gap:16px">
          ${recs.map(r => `<article class="card" style="padding:16px;gap:8px">
            <div class="eb">${esc(platLabel(r.platform))} · ${esc(r.kind || "")}</div>
            <p style="margin:0;font-size:13.5px">${esc(r.text || "")}</p>
            <div style="display:flex;gap:6px">${actButtons(r.uid)}</div>
            ${noteBlock(r.uid)}</article>`).join("")}
        </div>
      </div>
    </section>`;
  wireCardActs();
}

/* ============================================================================
   SURFACE: TRASH
   ========================================================================== */
// Module-level index cache keyed by runId. Populated lazily when renderTrash
// fetches an index for the first time; null means the fetch failed.
const _trashIndexCache = {};

/* ============================================================================
   INNOMAKER PARTNERS — the rail promo block + its page
   The tools are free and stay free; this is the quiet way back to the people who
   build them, for users who get stuck or want a version tailored to them. It sits in
   the foot of the rail, below Trash and the footer, where the rail had free space. Copy reuses the live site's own lines; every link opens the public site
   in the browser, and nothing here sends data anywhere. Logos are bundled locally
   (static/brand/) so the tool stays self-contained.
   ========================================================================== */
const IMP_SITE = "https://www.innomakerpartners.com";
function promoBlock() {
  return `<a href="#/innomaker" class="promo${location.hash === "#/innomaker" ? " on" : ""}" title="Who built this free tool, and how to reach them">
    <img src="./brand/imp_horizontal_lockup_color.svg" alt="InnoMaker Partners" width="150" height="51">
    <span class="promo-q">Stuck, or need this built around your business?</span>
  </a>`;
}
function renderInnomaker() {
  syncRailActive();
  const ext = (path, label, tip, cls) =>
    `<a href="${IMP_SITE}${path}" target="_blank" rel="noopener noreferrer" data-ext class="${cls || "pbtn"}" title="${esc(tip)}">${esc(label)} ↗</a>`;
  const sec = (key, kicker, title, body, actions) => `
    <section id="imp-${key}" class="card imp-sec">
      <div class="eb" style="color:var(--ember)">${kicker}</div>
      <h2 class="hl" style="font-size:22px;font-weight:700;margin:6px 0 10px">${title}</h2>
      <p style="margin:0 0 16px;color:var(--ink-soft);line-height:1.65">${body}</p>
      <div style="display:flex;gap:10px;flex-wrap:wrap;align-items:center">${actions}</div>
    </section>`;
  $("#region").innerHTML = `<section class="stage">${grainSVG()}
    <div class="masthead">
      <div>
        <div class="eb" style="color:var(--ember);margin-bottom:7px">InnoMaker Partners</div>
        <h1 class="hl" style="font-size:30px;font-weight:700;color:var(--ink)">The people behind this tool.</h1>
      </div>
    </div>
    <div class="stage-scroll" style="padding:32px">
      <div style="max-width:720px;display:flex;flex-direction:column;gap:18px">
        <div class="imp-intro">
          <img src="./brand/imp_primary_lockup_color.svg" alt="InnoMaker Partners" width="101" height="72">
          <div>
            <h2 class="hl" style="font-size:26px;font-weight:700;margin:0 0 8px">When a tool will not do it, we will.</h2>
            <p style="margin:0;color:var(--ink-soft);line-height:1.65">This local app and the marketing scraping tools are free, and they stay free. We built them to run our own marketing, then cleaned them up so you can run them too. But some things a tool cannot do on its own. That is where we can help.</p>
          </div>
        </div>
        ${sec("stuck", "Something not working as expected?", "Tell us where it broke.",
          "A run came back thin, a report reads wrong, or you are not sure what to do with what you found. Write to us. Your email reaches the people who build these tools, and it helps us decide what to fix next.",
          `<a data-ext href="mailto:info@innomakerpartners.com?subject=${encodeURIComponent("Feedback on the marketing scraping local app")}" class="pbtn" title="Opens your email app with a new message to InnoMaker Partners">Email us</a>
           <span class="tag" title="Our email address, if your email app does not open">info@innomakerpartners.com</span>`)}
        ${sec("tailored", "Need it built specifically for you?", "We tailor these tools to your needs.",
          "Your market, your sources and your reports, set up in your stack and taught to you and your team. Fixed scope, published prices, no retainer lock-in. We work in the room with you, not as a handoff.",
          ext("/consulting", "See how you can work with us", "Our offers on innomakerpartners.com", "btn-ember"))}
        ${sec("tools", "Want more of this?", "The other free tools.",
          "Free tools to do the work of a bigger team: meeting research, content, follower tracking and more. Every one comes with a plain guide, and nothing asks for your email before it does something useful.",
          ext("/tools", "Browse the tools", "All free tools on innomakerpartners.com"))}
        <div class="imp-about">
          <span>We are a small team that built our own AI tools because we had to.</span>
          ${ext("/about", "Read our story", "About InnoMaker Partners on innomakerpartners.com")}
        </div>
        <p class="eb" style="margin:4px 0 0;color:var(--muted-2);font-size:10px;line-height:1.7">Links open innomakerpartners.com in your browser. This local app sends nothing anywhere.</p>
      </div>
    </div>
  </section>`;
}

async function renderTrash() {
  syncRailActive();
  const region = $("#region");
  region.innerHTML = `<section class="stage">${grainSVG()}
    <div class="masthead">
      <div>
        <div class="eb" style="color:var(--ember);margin-bottom:7px">Trash</div>
        <h1 class="hl" style="font-size:30px;font-weight:700;color:var(--ink)">Removed from view.</h1>
      </div>
      <p class="eb" style="margin-left:auto;max-width:320px;text-align:right;color:var(--muted);font-size:10px;line-height:1.7">Records moved out of the working view. Nothing is ever deleted from the scraped data on disk.</p>
    </div>
    <div class="stage-scroll" style="padding:32px" id="trashBody">
      <div class="eb" style="color:var(--muted)">Loading</div>
    </div>
  </section>`;

  // Collect runs that actually have trashed uids.
  const runIds = Object.keys(state.trash).filter(rid => Object.keys(state.trash[rid] || {}).length > 0);

  if (!runIds.length) {
    $("#trashBody").innerHTML = `<div style="padding:60px 20px;text-align:center;color:var(--muted)">
      <div class="eb" style="margin-bottom:8px">nothing here</div>
      <div style="font-size:15px">Trash is empty. Removing a record from a view moves it here; nothing is ever deleted from the scraped data on disk.</div>
    </div>`;
    return;
  }

  // Fetch indexes for any run not yet cached.
  await Promise.all(runIds.map(async rid => {
    if (!_trashIndexCache[rid]) {
      try { _trashIndexCache[rid] = await api.index(rid); } catch { _trashIndexCache[rid] = null; }
    }
  }));

  // Render a compact restore card for one uid.
  const trashCard = (rid, uid) => {
    const idx = _trashIndexCache[rid];
    const rec = idx ? (idx.records || []).find(r => r.uid === uid) : null;
    const restoreBtn = `<button class="pbtn"
      title="Return this record to its surface."
      onclick="restoreRecord('${jsStr(rid)}','${jsStr(uid)}')">Restore</button>`;
    if (rec) {
      const platChip = `<span class="tag">${esc(platLabel(rec.platform))}</span>`;
      const dateStr = has(rec.date) ? `<span class="num" style="font-size:11px;color:var(--muted-2)">${esc(fmtDate(rec.date) || rec.date)}</span>` : "";
      const excerpt = (rec.text || "").slice(0, 240);
      return `<div class="card" style="padding:14px;margin-bottom:10px;gap:8px;display:flex;flex-direction:column">
        <div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap">${platChip}${dateStr}</div>
        ${excerpt ? `<p style="margin:0;font-size:13px;color:var(--ink-soft);line-height:1.5;display:-webkit-box;-webkit-line-clamp:4;-webkit-box-orient:vertical;overflow:hidden">${esc(excerpt)}</p>` : ""}
        <div style="margin-top:2px">${restoreBtn}</div>
      </div>`;
    }
    // Uid not found in index (run re-scraped, record gone): show stub.
    return `<div class="card" style="padding:14px;margin-bottom:10px;gap:6px;display:flex;flex-direction:column">
      <div class="eb" style="color:var(--muted-2)">${esc(uid)}</div>
      <div style="font-size:12px;color:var(--muted)">Record not found in the current index. It may have been removed when the run was re-scraped.</div>
      <div style="margin-top:2px">${restoreBtn}</div>
    </div>`;
  };

  let html = "";

  // Walk campaigns in their display order so the Trash hierarchy mirrors the rail.
  const knownRunIds = new Set();
  for (const camp of state.campaigns) {
    const campRids = camp.runs.map(r => r.run_id).filter(rid => runIds.includes(rid));
    if (!campRids.length) continue;
    html += `<div style="margin-bottom:32px">
      <div class="eb" style="color:var(--ember);margin-bottom:12px">${esc(camp.subject)}</div>`;
    for (const rid of campRids) {
      knownRunIds.add(rid);
      const trashedUids = Object.keys(state.trash[rid] || {});
      const reg = state.registry.find(r => r.run_id === rid);
      const toolBadge = reg ? (TOOL_LABEL[reg.tool] || reg.tool) : "";
      html += `<div style="margin-bottom:20px">
        <div style="display:flex;align-items:center;gap:8px;margin-bottom:10px;flex-wrap:wrap">
          ${toolBadge ? `<span class="tag">${esc(toolBadge)}</span>` : ""}
          <span style="font-size:12.5px;color:var(--ink-soft)">${esc(runLabel(reg) || shortRun(rid))}</span>
          <span class="num" style="font-size:11px;color:var(--muted)">${trashedUids.length} removed</span>
        </div>`;
      trashedUids.forEach(uid => { html += trashCard(rid, uid); });
      html += `</div>`;
    }
    html += `</div>`;
  }

  // Any runs not attached to a known campaign (defensive; should not arise normally).
  const orphanRids = runIds.filter(rid => !knownRunIds.has(rid));
  if (orphanRids.length) {
    html += `<div style="margin-bottom:32px">
      <div class="eb" style="color:var(--muted);margin-bottom:12px">Other runs</div>`;
    for (const rid of orphanRids) {
      const trashedUids = Object.keys(state.trash[rid] || {});
      html += `<div style="margin-bottom:20px">
        <div style="display:flex;align-items:center;gap:8px;margin-bottom:10px">
          <span style="font-size:12.5px;color:var(--ink-soft)">${esc(runLabel(state.registry.find(r => r.run_id === rid)) || shortRun(rid))}</span>
          <span class="num" style="font-size:11px;color:var(--muted)">${trashedUids.length} removed</span>
        </div>`;
      trashedUids.forEach(uid => { html += trashCard(rid, uid); });
      html += `</div>`;
    }
    html += `</div>`;
  }

  // The index fetches above are async: if the user navigated away meanwhile, the
  // surface that owned #trashBody is gone and this completion must not write into
  // (or throw over) whatever replaced it.
  const trashBody = $("#trashBody");
  if (!trashBody) return;
  trashBody.innerHTML = html || `<div style="padding:60px 20px;text-align:center;color:var(--muted)">
    <div class="eb" style="margin-bottom:8px">nothing here</div>
    <div style="font-size:15px">Trash is empty. Removing a record from a view moves it here; nothing is ever deleted from the scraped data on disk.</div>
  </div>`;
}

/* ============================================================================
   SHARED SURFACE CHROME
   ========================================================================== */
function surfaceMasthead(kind, meta, chips) {
  // Every run surface (Ads / Reviews / Search / generic) belongs to a campaign,
  // so its back control goes up to that campaign's dossier; a run with no subject
  // (defensive) falls back to the campaign list.
  const back = meta && meta.subject
    ? backButton("#/campaign/" + encodeURIComponent(meta.subject), "Project", "Back to the " + meta.subject + " project")
    : backButton("#/", "All projects", "Back to all projects");
  return `<div class="masthead">
    ${back}
    <div style="flex-grow:1">
      <div class="eb" style="color:var(--ember);margin-bottom:6px">${esc(meta.subject || "")} · ${esc(kind.toLowerCase())}</div>
      <h1 class="hl" style="font-size:30px;font-weight:700;color:var(--ink)">${esc(kind)}</h1>
    </div>
    <div style="display:flex;flex-direction:column;align-items:flex-end;gap:10px;flex-shrink:0">
      ${(chips || []).filter(Boolean).length ? `<div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap;justify-content:flex-end">${(chips || []).filter(Boolean).map(c => c).join("")}</div>` : ""}
      <div style="display:flex;gap:10px;align-items:center">${reportButton(meta)}<button class="ctl" style="cursor:pointer" onclick="exportCurrent()" title="Download this run's records, favourites and notes as a JSON file">Export ↓</button></div>
    </div>
  </div>`;
}
function countChip(n, label, id, tip) {
  return `<span class="tag"${id ? ` id="${id}"` : ""}${tip ? ` title="${esc(tip)}"` : ""}><span class="num">${num(n)}</span> ${esc(label)}</span>`;
}
// Update a countChip's number in place after a repaint (so a "shown" count tracks
// the filtered view instead of freezing at the run total).
function setCount(id, n) {
  const el = document.querySelector(`#${id} .num`);
  if (el) el.textContent = num(n);
}
// A prominent back control for a surface masthead's top-left — the same .ctl
// affordance the report page uses, applied across every surface so the whole
// product has one clear, consistent way back up the hierarchy
// (report -> run -> project -> all projects).
function backButton(href, label, title) {
  return `<a href="${href}" class="ctl" style="text-decoration:none;gap:8px;flex-shrink:0;white-space:nowrap" title="${esc(title || label)}">← ${esc(label)}</a>`;
}
function reportButton(meta) {
  const reps = meta.reports || [];
  if (!reps.length) return "";
  const first = reps[0];
  // Use the authoritative routing run_id (registry path relative to root), NOT the
  // index's baked run_id: the latter is only the folder basename at emit time and
  // diverges for nested runs (marketing-research/<client>/...) or moved/renamed dirs,
  // which 404s the report. Every other fetch here already keys off the routing id.
  const rid = state.runId || meta.run_id;
  const n = reps.length;
  return `<a href="#/report/${encodeURIComponent(rid)}//${encodeURIComponent(first.path)}"
    class="btn-ember" style="text-decoration:none" title="Open the written analysis for this run: ${n} report${n===1?"":"s"}">${n} report${n===1?"":"s"} →</a>`;
}

/* ---- filter rail --------------------------------------------------------- */
function filterRail(groups) {
  const collapsed = lsGet("mv.filters", false);
  if (collapsed) {
    return `<aside class="filterrail filterrail-collapsed">
      <button class="iconbtn" onclick="toggleFilters()" aria-expanded="false"
              title="Show the filters" style="font-size:16px">›</button>
    </aside>`;
  }
  return `<aside class="filterrail">
    <div style="display:flex;justify-content:flex-end">
      <button class="iconbtn" onclick="toggleFilters()" aria-expanded="true"
              title="Hide the filters" style="font-size:16px">‹</button>
    </div>
    <div class="searchbox"><span class="eb" style="letter-spacing:0">⌕</span>
      <input id="fq" type="search" placeholder="Search the text" autocomplete="off"></div>
    ${groups.filter(Boolean).join("")}
    <div style="margin-top:auto;padding-top:16px;border-top:1px solid var(--line)">
      <button class="ctl" style="width:100%;justify-content:center" onclick="clearFilters()" title="Remove all filters and show every record">Clear filters</button>
    </div>
  </aside>`;
}

function toggleFilters() {
  lsSet("mv.filters", !lsGet("mv.filters", false));
  // finally: a throw mid-render must not leave the flag stuck true, or the
  // next re-render on this surface would silently skip its filter reset.
  _skipFilterReset = true;
  try {
    const tool = state.index && state.index.tool;
    if (tool === "ad") renderAdLibrary();
    else if (tool === "pain") renderReviewWall();
  } finally {
    _skipFilterReset = false;
  }
  restoreFilterUI();
}
function facetGroup(title, values, key, label, scroll) {
  if (!values.length) return "";
  const items = values.map(v => `<label class="facet" data-facet="${key}" data-val="${esc(v)}" title="${esc(label ? label(v) : v)}">
      <span class="box"></span><span>${esc(label ? label(v) : v)}</span></label>`).join("");
  // A long facet list (advertisers can run to dozens) gets its own bounded scroll
  // so the facets below it stay reachable instead of being pushed off-screen.
  return `<div><div class="eb" style="margin-bottom:8px">${esc(title)}</div>
    ${scroll ? `<div class="facet-scroll">${items}</div>` : items}</div>`;
}
function favToggle() {
  return `<label class="facet" data-favtoggle="1" title="Show only the records you have starred" style="border-top:1px solid var(--line);padding-top:14px">
    <span class="box"></span><span>Favorites only</span></label>`;
}
// U3: show an active change-filter chip in the rail so the user can see and remove it.
function changeFilterGroup() {
  const cat = state.filters && state.filters.change;
  if (!cat) return "";
  const label = cat === "new" ? "Showing: new" : cat === "changed" ? "Showing: changed" : cat === "gone" ? "Showing: not in this run" : "Showing: " + cat;
  return `<div id="changefilterchip" style="border-top:1px solid var(--line);padding-top:14px">
    <div class="eb" style="margin-bottom:6px" title="The active change filter. Click to clear.">Change filter</div>
    <div style="display:flex;align-items:center;gap:6px">
      <span class="tag" style="background:var(--ember-tint);color:var(--ember-press)">${esc(label)}</span>
      <button class="iconbtn" style="font-size:12px;padding:2px 6px" title="Clear the change filter" onclick="clearChangeFilter()">×</button>
    </div>
  </div>`;
}
function wireFilterRail(repaint) {
  const q = $("#fq");
  if (q) q.oninput = () => { state.filters.q = q.value.trim().toLowerCase(); repaint(); };
  document.querySelectorAll("[data-facet]").forEach(el => {
    el.onclick = () => {
      const set = state.filters[el.dataset.facet];
      const v = el.dataset.val;
      if (set.has(v)) set.delete(v); else set.add(v);
      el.classList.toggle("sel");
      repaint();
    };
  });
  const ft = document.querySelector("[data-favtoggle]");
  if (ft) ft.onclick = () => { state.filters.favOnly = !state.filters.favOnly; ft.classList.toggle("sel"); repaint(); };
  // Saved views: one at a time; clicking the open one closes it.
  document.querySelectorAll("[data-viewsel]").forEach(el => {
    el.onclick = () => {
      const id = el.dataset.viewsel;
      state.filters.view = state.filters.view === id ? "" : id;
      restoreFilterUI();
      repaint();
    };
  });
  window._repaint = repaint;
}
function clearFilters() {
  const f = state.filters;
  if (f.platforms) f.platforms.clear();
  if (f.formats) f.formats.clear();
  if (f.advertisers) f.advertisers.clear();
  if (f.durations) f.durations.clear();
  if (f.polarities) f.polarities.clear();
  if (f.subreddits) f.subreddits.clear();
  f.favOnly = false; f.q = ""; f.view = ""; f.change = ""; f.vs = "";
  const q = $("#fq"); if (q) q.value = "";
  document.querySelectorAll(".facet.sel").forEach(el => el.classList.remove("sel"));
  if (window._repaint) window._repaint();
}
// U3: clear only the change filter and re-render (the compare strip stays visible).
function clearChangeFilter() {
  if (!state.filters) return;
  state.filters.change = ""; state.filters.vs = "";
  // The rail's "Change filter" chip and the strip's active count link are painted once,
  // not by the repaint, so both are redone here: after the x the page must look as it
  // does with no filter set.
  const chip = document.getElementById("changefilterchip");
  if (chip) chip.remove();
  paintCompareStrip();
  if (window._repaint) window._repaint();
}

/* ---- saved views --------------------------------------------------------- */
// A saved view is a named set of records in this run (.viewer/views.json), usually saved by
// Claude while working with the user ("the 14 reviews that mention hidden fees"). Picking
// one filters the page to exactly those records, on top of any other filter.
function viewsGroup() {
  const vs = state.views || [];
  if (!vs.length) return "";
  const items = vs.map(v => `<label class="facet" data-viewsel="${esc(v.id)}" title="${esc(v.note || v.title)}">
      <span class="box"></span><span>${esc(v.title)}  ·  ${num((v.uids || []).length)}</span></label>`).join("");
  return `<div><div class="eb" style="margin-bottom:8px" title="Named sets of records saved for this run, for example by Claude while you work together. Pick one to show only those records.">Saved views</div>
    <div class="facet-scroll">${items}</div></div>`;
}
function viewHas(id, uid) {
  const v = (state.views || []).find(x => x.id === id);
  if (!v) return true;                          // a view deleted meanwhile filters nothing
  if (!v._set) v._set = new Set(v.uids || []);
  return v._set.has(uid);
}
function paintViewBanner() {
  const el = $("#viewbanner");
  if (!el) return;
  const v = state.filters.view && (state.views || []).find(x => x.id === state.filters.view);
  el.innerHTML = v ? `<div class="viewbanner">
      <div style="min-width:0">
        <div class="eb" style="color:var(--ember);margin-bottom:5px">Saved view${v.created_by === "agent" ? " · from Claude" : ""}</div>
        <div class="hl" style="font-size:17px;font-weight:600;line-height:1.3">${esc(v.title)}</div>
        ${v.note ? `<p style="margin:6px 0 0;color:var(--ink-soft);font-size:13px;line-height:1.55">${esc(v.note)}</p>` : ""}
      </div>
      <button class="pbtn" onclick="closeView()" title="Show all records again">Close view</button>
    </div>` : "";
}
function closeView() {
  state.filters.view = "";
  restoreFilterUI();
  if (window._repaint) window._repaint();
}
// Shared tail of every filtered paint: banner, address, and the agent's view report.
function afterPaint(list, recs) {
  paintViewBanner();
  syncFilterURL();
  state.visible = { uids: list.map(r => r.uid), total: recs.length };
  reportView();
}

/* ---- telling the agent what is on screen ----------------------------------- */
// The local server writes this to <root>/.viewer/current_view.json; the agent reads it with
// viewer_ctl.py current-view, so "these reviews" in the chat means what the user sees.
let _viewTimer = null;
function reportView() {
  clearTimeout(_viewTimer);
  _viewTimer = setTimeout(sendView, 400);
}
function sendView() {
  const h = location.hash || "#/";
  const page = h.startsWith("#/run/") ? "run" : h.startsWith("#/campaign/") ? "project"
    : h.startsWith("#/report/") ? "report" : h === "#/trash" ? "trash" : h === "#/innomaker" ? "about" : "overview";
  const onRun = page === "run" && state.index && state.runId;
  const f = state.filters || {};
  const filters = {};
  if (onRun) {
    URL_SET_KEYS.forEach(k => { if (f[k] && f[k].size) filters[k] = [...f[k]]; });
    if (f.q) filters.text = f.q;
    if (f.favOnly) filters.favorites_only = true;
    if (f.view) filters.view = f.view;
    if (f.change) filters.change = f.change;
    if (f.vs) filters.vs = f.vs;
  }
  const vis = onRun ? state.visible : null;
  const seg = document.querySelector(".seg.on");
  const body = {
    page, route: h, url: location.href,
    run_id: onRun ? state.runId : null,
    tool: onRun ? state.index.tool : null,
    subject: onRun ? state.index.subject : null,
    filters,
    visible: vis ? { count: vis.uids.length, listed: vis.total, uids: vis.uids.slice(0, 1000),
                     uids_truncated: vis.uids.length > 1000 } : null,
    search_tab: onRun && state.index.tool === "search" && seg ? seg.dataset.seg : null,
    at: new Date().toISOString(),
  };
  fetch("/api/view", { method: "POST", headers: { "Content-Type": "application/json" },
                       body: JSON.stringify(body) }).catch(() => {});
}

/* ---- picking up the agent's changes ---------------------------------------- */
// Polls the open run's change stamps; when the agent saved a view, marked a record or
// re-indexed the run, the page updates in place, keeping the user's filters.
let _checking = false;
async function checkChanges() {
  if (_checking || document.hidden || !state.runId || !state.index || !(location.hash || "").startsWith("#/run/")) return;
  _checking = true;
  try {
    const runId = state.runId;
    const s = await api.changes(runId);
    if (runId !== state.runId) return;
    const prev = state.stamps;
    if (!prev || prev.run !== runId) { state.stamps = { run: runId, ...s }; return; }
    const changed = ["annotations", "views", "index"].filter(k => s[k] !== prev[k]);
    if (!changed.length) return;
    // Never repaint under the user's hands: an open note editor, or typing in the search
    // box. The stamps stay old, so the next tick tries again.
    if (document.querySelector(".note-editor") || document.activeElement === $("#fq")) return;
    if (changed.includes("index")) state.index = await api.index(runId);
    if (changed.includes("annotations")) { state.ann = normalizeAnn(await api.annotations(runId)); state.trash[runId] = state.ann.trashed; }
    if (changed.includes("views")) state.views = (await api.views(runId)).views || [];
    if (runId !== state.runId) return;
    state.stamps = { run: runId, ...s };
    if (changed.includes("index") || changed.includes("views")) rerenderKeepingFilters();
    else if (window._repaint) window._repaint();
    if (changed.includes("annotations")) renderRail();
  } catch (_) { /* server busy or gone: the next tick tries again */ }
  finally { _checking = false; }
}
function rerenderKeepingFilters() {
  if (state.filters.view && !(state.views || []).some(v => v.id === state.filters.view)) state.filters.view = "";
  const tool = state.index && state.index.tool;
  if (tool === "search") return renderSearchLandscape();
  if (tool !== "ad" && tool !== "pain") return;
  _skipFilterReset = true;
  try { if (tool === "ad") renderAdLibrary(); else renderReviewWall(); }
  finally { _skipFilterReset = false; }
  restoreFilterUI();
}

/* ---- record actions (favorite) ------------------------------------------ */
function actButtons(uid) {
  const on = state.ann.favorites[uid] ? " on" : "";
  const note = state.ann.notes[uid];
  // SVG speech bubble: reads as "comment / note" at a glance (a pencil read as
  // "edit this review"). stroke="currentColor" so the .act button's muted/ember/white
  // color states keep working without any extra CSS. 13x13 fits the 30x30 button box.
  const bubbleSVG = `<svg width="13" height="13" viewBox="0 0 14 14" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M1 2a1 1 0 0 1 1-1h10a1 1 0 0 1 1 1v7a1 1 0 0 1-1 1H5l-3 3V10H2a1 1 0 0 1-1-1V2z"/></svg>`;
  // Trash can: stroke-based currentColor, same 13x13 footprint as the bubble.
  const trashSVG = `<svg width="13" height="13" viewBox="0 0 14 14" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M2 3.5h10M5 3.5V2h4v1.5M3.5 3.5l.7 8h5.6l.7-8"/></svg>`;
  return `<button class="act${on}" data-fav="${esc(uid)}" title="${on ? "Favorited" : "Favorite"}" onclick="toggleFav('${jsStr(uid)}')">★</button>
    <button class="act${note ? " on" : ""}" data-note="${esc(uid)}" title="${note ? "Note: " + esc(note) : "Add note"}" onclick="editNote('${jsStr(uid)}')">${bubbleSVG}</button>
    <button class="act" title="Move to Trash. Nothing is deleted; restore it any time from the Trash menu." onclick="trashRecord('${jsStr(uid)}')">${trashSVG}</button>`;
}
function wireCardActs() { /* onclick inline; favorites repainted via paintFav after toggle */ }
// The note a user jotted on this record, rendered ON the card. The hover title on the
// bubble button was the only surface before -- a user saved a note and the card looked
// unchanged ("the note disappeared"). A note you cannot see is a note that does not exist.
function noteBlock(uid) {
  const n = state.ann.notes[uid];
  return `<div class="noteslot" data-note-uid="${esc(uid)}">${n ? `<div class="notecard" title="Your note, saved locally with this run">${esc(n)}</div>` : ''}</div>`;
}
const jsStr = (s) => String(s).replace(/\\/g, "\\\\").replace(/'/g, "\\'");

async function editNote(uid) {
  const slot = document.querySelector('[data-note-uid="' + cssEsc(uid) + '"]');
  if (!slot) { if (window._repaint) window._repaint(); else route(); return; }
  // Toggle: clicking note button while editor is open closes it without saving
  if (slot.querySelector('.note-editor')) { _renderNoteSlot(slot, uid); return; }
  const cur = state.ann.notes[uid] || '';
  const hasCur = !!state.ann.notes[uid];
  const editorDiv = document.createElement('div');
  editorDiv.className = 'note-editor';
  editorDiv.innerHTML =
    `<textarea class="note-editor-ta" rows="3" placeholder="Add a note...">${esc(cur)}</textarea>` +
    `<div class="note-editor-footer">` +
      `<span class="note-editor-hint">Saved on this computer, with this run</span>` +
      `<div class="note-editor-actions">` +
        (hasCur ? `<button class="pbtn note-editor-del" title="Remove this note">Delete note</button>` : '') +
        `<button class="pbtn note-editor-cancel" title="Close without saving (Esc)">Cancel</button>` +
        `<button class="pbtn on note-editor-save" title="Save this note to the run (Ctrl or Cmd + Enter)">Save note</button>` +
      `</div>` +
    `</div>` +
    `<div class="note-editor-error" hidden></div>`;
  slot.innerHTML = '';
  slot.appendChild(editorDiv);
  const ta = editorDiv.querySelector('.note-editor-ta');
  ta.focus();
  ta.setSelectionRange(ta.value.length, ta.value.length);
  const showErr = (msg) => { const errEl = editorDiv.querySelector('.note-editor-error'); errEl.textContent = msg; errEl.hidden = false; };
  const doSave = async () => {
    const val = ta.value.trim();
    const prevNote = state.ann.notes[uid];
    if (val === '') delete state.ann.notes[uid]; else state.ann.notes[uid] = val;
    try {
      adoptMarks(state.runId, await api.mark(state.runId, "notes", uid, val || null));
    } catch (e) {
      if (prevNote !== undefined) state.ann.notes[uid] = prevNote; else delete state.ann.notes[uid];
      showErr('Could not save. Is the viewer still running? Your text is still here.');
      return;
    }
    paintNote(uid);
    _renderNoteSlot(slot, uid);
  };
  const doCancel = () => _renderNoteSlot(slot, uid);
  const doDelete = async () => {
    const prevNote = state.ann.notes[uid];
    delete state.ann.notes[uid];
    try {
      adoptMarks(state.runId, await api.mark(state.runId, "notes", uid, null));
    } catch (e) {
      if (prevNote !== undefined) state.ann.notes[uid] = prevNote;
      showErr('Could not save. Is the viewer still running? Your text is still here.');
      return;
    }
    paintNote(uid);
    _renderNoteSlot(slot, uid);
  };
  editorDiv.querySelector('.note-editor-save').addEventListener('click', doSave);
  editorDiv.querySelector('.note-editor-cancel').addEventListener('click', doCancel);
  const delBtn = editorDiv.querySelector('.note-editor-del');
  if (delBtn) delBtn.addEventListener('click', doDelete);
  ta.addEventListener('keydown', e => {
    if (e.key === 'Escape') { e.preventDefault(); doCancel(); }
    if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) { e.preventDefault(); doSave(); }
  });
}
function _renderNoteSlot(slot, uid) {
  const n = state.ann.notes[uid];
  slot.innerHTML = n ? `<div class="notecard" title="Your note, saved locally with this run">${esc(n)}</div>` : '';
}
function paintNote(uid) {
  const note = state.ann.notes[uid];
  document.querySelectorAll(`[data-note="${cssEsc(uid)}"]`).forEach(b => {
    b.classList.toggle('on', !!note);
    b.title = note ? 'Note: ' + note : 'Add note';   // a DOM property, not HTML: no esc()
  });
}

async function trashRecord(uid) {
  state.ann.trashed[uid] = true;
  if (!state.trash[state.runId]) state.trash[state.runId] = {};
  state.trash[state.runId][uid] = true;
  try {
    adoptMarks(state.runId, await api.mark(state.runId, "trashed", uid, true));
  } catch (e) { console.warn("trash save failed", e); }
  // Repaint the current surface (record disappears) and the rail (count updates).
  if (window._repaint) window._repaint(); else route();
  renderRail();
}

async function restoreRecord(runId, uid) {
  // One-mark write: only this record's trashed flag changes, so favorites and notes of a
  // run whose marks this tab has not loaded are never touched.
  if (runId === state.runId) delete state.ann.trashed[uid];
  try { adoptMarks(runId, await api.mark(runId, "trashed", uid, null)); }
  catch (e) { console.warn("restore save failed", e); }
  // Mirror into in-memory maps.
  if (state.trash[runId]) delete state.trash[runId][uid];
  if (runId === state.runId && window._repaint) window._repaint();
  // Re-render the Trash view and update the rail count.
  renderTrash();
  renderRail();
}

/* ---- export current view ------------------------------------------------- */
function exportCurrent() {
  const recs = (state.index && state.index.records) || [];
  const payload = {
    exported_at: new Date().toISOString(),
    run_id: state.runId, subject: state.index && state.index.subject, tool: state.index && state.index.tool,
    records: recs, favorites: Object.keys(state.ann.favorites), notes: state.ann.notes,
  };
  const blob = new Blob([JSON.stringify(payload, null, 2)], { type: "application/json" });
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = (shortRun(state.runId || "export")) + "_viewer_export.json";
  a.click();
  setTimeout(() => URL.revokeObjectURL(a.href), 2000);
}

/* ---- filter matching helpers -------------------------------------------- */
function matchCommon(r, f) {
  if (f.favOnly && !state.ann.favorites[r.uid]) return false;
  if (f.platforms && f.platforms.size && !f.platforms.has(r.platform)) return false;
  if (f.formats && f.formats.size) {
    // An ad matches the Format facet if ANY of its formats is ticked (a carousel image
    // ad matches under either IMAGE or CAROUSEL).
    if (!recFormats(r).some(fmt => f.formats.has(fmt))) return false;
  }
  if (f.advertisers && f.advertisers.size && !f.advertisers.has((r.data || {}).advertiser_name)) return false;
  if (f.durations && f.durations.size && !f.durations.has(durationBand(r))) return false;
  if (f.q && !hay(r).includes(f.q)) return false;
  if (f.view && !viewHas(f.view, r.uid)) return false;
  return true;
}
// Mirrored by viewer_ctl.py _hay: the agent and the app must find the same records.
// place_name is the Google Maps place a review names on its card.
function hay(r) {
  const d = r.data || {};
  return [r.text, r.subject, d.headline, d.advertiser_name, d.business_name, d.place_name, d.cta_text, r.platform]
    .filter(Boolean).join(" ").toLowerCase();
}
const distinct = (arr) => [...new Set(arr.filter(v => v != null))];

// Every ad record needs filterable format(s). An ad can be MORE than one: a Meta
// carousel is a carousel AND (usually) an image ad, so this returns a LIST, not a
// single value -- otherwise CAROUSEL could never be its own facet without hiding the
// creative type. Meta records carry no explicit format in the envelope, so derive the
// creative type from the media on disk (video beats image) and add CAROUSEL when the
// ad has more than one card slot (d.card_count is the ORIGINAL slot count, before the
// emit deduped identical copy -- an all-identical carousel is still a carousel format).
// Without this, ticking a Format facet silently dropped all Meta ads.
// The index step now emits this exact list as tags.formats (ad emit_index _ad_formats), so
// the agent and the app count the same ads; the derivation below is the fallback for runs
// indexed before that field existed.
function recFormats(r) {
  const d = r.data || {}, t = r.tags || {};
  if (Array.isArray(t.formats)) return t.formats;
  const out = [];
  const explicit = d.format || t.format;
  if (explicit) out.push(explicit);
  else {
    const media = r.media || [];
    if (media.some(m => m.type === "video")) out.push("VIDEO");
    else if (media.some(m => m.type === "image")) out.push("IMAGE");
  }
  // Only a number is a slot count (a string "5" is not); otherwise count the cards list.
  // Same rule as viewer_ctl.py _formats, so the app and the agent never disagree.
  const slots = typeof d.card_count === "number" ? d.card_count : (Array.isArray(d.cards) ? d.cards.length : 0);
  // DCO ads carry versions in the cards field but are not carousels: label them "dynamic"
  // so the format chip reads "· dynamic" and the facet filter groups them correctly.
  // DCO keys on display_format alone (a 1-variant DCO is still dynamic); CAROUSEL still
  // requires slots > 1 (one card is not a carousel). Case-normalized: the actor's casing
  // is not contractual.
  if (isDCOFormat(d)) out.push("DYNAMIC");
  else if (slots > 1) out.push("CAROUSEL");
  return out;
}

function emptyResult(msg) {
  return `<div style="grid-column:1/-1;padding:60px 20px;text-align:center;color:var(--muted)">
    <div class="eb" style="margin-bottom:8px">nothing here</div>
    <div style="font-size:15px">${esc(msg)}</div></div>`;
}

/* ---- states -------------------------------------------------------------- */
function renderLoading(msg) {
  $("#region").innerHTML = `<div class="stage" style="align-items:center;justify-content:center">
    ${grainSVG()}<div style="text-align:center;position:relative;z-index:1">
    <div class="spin" style="margin:0 auto 16px"></div>
    <div class="eb">${esc(msg || "Loading")}</div></div></div>`;
}
function renderEmpty() {
  $("#region").innerHTML = `<div class="stage" style="align-items:center;justify-content:center">${grainSVG()}
    <div style="text-align:center;max-width:420px;position:relative;z-index:1">
      <div class="hl" style="font-size:30px;font-weight:700;margin-bottom:12px">Nothing to show yet.</div>
      <p style="color:var(--muted);line-height:1.6">No runs were found under this folder. Run <span class="tag">ad-intelligence</span>,
      <span class="tag">pain-and-messaging</span> or <span class="tag">search-intelligence</span>,
      then reopen the viewer here.</p></div></div>`;
}
// A run folder with no index whose run recorded how it ended (notification.json):
// a refused scheduled month (nothing collected, nothing spent) or one that stopped
// before it finished. Never the "scraped before the viewer existed" text, which is
// false for both, and never re-index advice.
function renderRunEnded(runId, row) {
  $("#region").innerHTML = runEndedHTML(runId, row);
}
// Pure function.
function runEndedHTML(runId, row) {
  const n = row.notification || {};
  const refused = n.status === "refused";
  const eyebrow = refused ? "skipped this month" : "stopped early";
  const lead = refused
    ? "This month's scheduled run did not start, so nothing was collected and nothing was spent."
    : "This month's run stopped before it finished, so there are no results to show for it.";
  const said = n.message ? `<p style="color:var(--ink-soft);line-height:1.6;margin:14px 0 0"><span class="eb" style="display:block;margin-bottom:4px">What the run said</span>${esc(n.message)}</p>` : "";
  const report = n.report ? `<p style="color:var(--muted);line-height:1.6;white-space:pre-wrap;margin:10px 0 0">${esc(n.report)}</p>` : "";
  return `<div class="stage" style="align-items:center;justify-content:center">${grainSVG()}
    <div style="text-align:left;max-width:520px;position:relative;z-index:1">
      <div class="eb" style="color:var(--ember);margin-bottom:10px">${esc(eyebrow)}</div>
      <div class="hl" style="font-size:26px;font-weight:700;margin-bottom:12px">${esc(TOOL_LABEL[row.tool] || row.tool || "")} · ${esc(runLabel(row) || shortRun(runId))}</div>
      <p style="color:var(--muted);line-height:1.6;margin:0">${esc(lead)}</p>
      ${said}${report}</div></div>`;
}
function renderNotIndexed(runId, row) {
  // Without a registry row the folder is not in the list the viewer built: the only thing
  // known is that it is not listed, so say that and nothing about when it was scraped.
  // A listed folder without an index keeps the older explanation.
  const body = row
    ? `This run was scraped before the viewer existed, so it has no
      <span class="tag">frontend_index.json</span>. Ask Claude to re-index this run, then refresh. That step
      is free: it only reads what is already on disk, and nothing is scraped again.`
    : "This folder is not in the list of runs the viewer found, so there is nothing to show for it. Check the folder name in the address.";
  $("#region").innerHTML = `<div class="stage" style="align-items:center;justify-content:center">${grainSVG()}
    <div style="text-align:center;max-width:460px;position:relative;z-index:1">
      <div class="eb" style="color:var(--ember);margin-bottom:10px">${row ? "not indexed" : "not in the list"}</div>
      <div class="hl" style="font-size:26px;font-weight:700;margin-bottom:12px">${esc(shortRun(runId))}</div>
      <p style="color:var(--muted);line-height:1.6">${body}</p></div></div>`;
}
function renderFatal(title, detail) {
  $("#region").innerHTML = `<div class="stage" style="align-items:center;justify-content:center">${grainSVG()}
    <div style="text-align:center;max-width:460px;position:relative;z-index:1">
      <div class="eb" style="color:var(--ember);margin-bottom:10px">error</div>
      <div class="hl" style="font-size:24px;font-weight:700;margin-bottom:10px">${esc(title)}</div>
      <p class="num" style="color:var(--muted);font-size:13px">${esc(detail || "")}</p></div></div>`;
}

/* ---- minimal, safe Markdown renderer (offline; no CDN) ------------------ */
// Turn heading text into a stable, URL-safe anchor id; `seen` dedupes collisions
// (two "### Meta" headings become meta, meta-2) so every TOC link lands on the
// right heading and not just the first match.
function slugify(text, seen) {
  let base = stripInline(text).toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "") || "section";
  let id = base, n = 1;
  while (seen[id]) id = `${base}-${++n}`;
  seen[id] = true;
  return id;
}
// Strip inline markdown (**bold**, `code`, [text](url), *em*) down to readable
// plain text — used for both slugs and TOC labels, so a heading like
// "**Tier 1** deal-breakers" reads cleanly in the contents rail.
function stripInline(s) {
  return String(s)
    .replace(/\[([^\]]+)\]\([^)]*\)/g, "$1")
    .replace(/[`*_]/g, "")
    .trim();
}
// renderMarkdown(md, headings?) — when a `headings` array is passed, every
// heading gets an id and is recorded as {level, text, id} so the caller can
// build a table of contents. Without it, behaviour is unchanged.
function renderMarkdown(md, headings) {
  const lines = String(md).replace(/\r\n/g, "\n").split("\n");
  const seen = {};
  let html = "", inList = false, listType = "ul", inCode = false, code = "", para = [], inTable = false, tbl = [];
  // Consecutive ">" lines are ONE quote. A bare ">" line ends a paragraph inside it, it does
  // not end the quote. quote holds the paragraphs; the last one is open.
  let quote = null;
  const flushQuote = () => {
    if (!quote) return;
    const paras = quote.filter(p => p.length).map(p => inline(p.join(" ")));
    if (paras.length) html += `<blockquote>${paras.join("<br><br>")}</blockquote>`;
    quote = null;
  };
  const flushPara = () => { if (para.length) { html += `<p>${inline(para.join(" "))}</p>`; para = []; } };
  const flushList = () => { if (inList) { html += `</${listType}>`; inList = false; } };
  const flushTable = () => {
    if (!inTable) return;
    const rows = tbl.filter(r => !/^\s*\|?[\s:|-]+\|?\s*$/.test(r));
    if (rows.length) {
      const cells = (r) => r.replace(/^\||\|$/g, "").split("|").map(c => c.trim());
      const head = cells(rows[0]);
      // A cell whose VISIBLE text holds a run of 30 or more characters without a space (a
      // creative file name such as AR..._CR..._0.jpg) is marked "longtok", and its table
      // "has-longtok"; styles.css then lets that cell break anywhere and the table scroll,
      // instead of squeezing every other column to single letters. The
      // text is measured with each link reduced to its words: a link's long address is not
      // on screen. 30 sits in a gap in the fixture reports: the longest ordinary word in
      // a table is 28 characters (a domain) and every file name is 50 or more.
      let anyLong = false;
      const body = rows.slice(1).map(r => "<tr>" + cells(r).map(c => {
        const long = /\S{30,}/.test(c.replace(/\[([^\]]+)\]\([^)]*\)/g, "$1"));
        if (long) anyLong = true;
        return `<td${long ? ' class="longtok"' : ""}>${inline(c)}</td>`;
      }).join("") + "</tr>").join("");
      html += `<table${anyLong ? ' class="has-longtok"' : ""}><thead><tr>` + head.map(h => `<th>${inline(h)}</th>`).join("") + "</tr></thead><tbody>" + body + "</tbody></table>";
    }
    inTable = false; tbl = [];
  };
  for (let raw of lines) {
    if (quote && !/^\s*>/.test(raw)) flushQuote();
    if (/^```/.test(raw)) {
      if (inCode) { html += `<pre><code>${esc(code)}</code></pre>`; code = ""; inCode = false; }
      else { flushPara(); flushList(); flushTable(); inCode = true; }
      continue;
    }
    if (inCode) { code += raw + "\n"; continue; }
    if (/^\s*\|.*\|\s*$/.test(raw)) { flushPara(); flushList(); inTable = true; tbl.push(raw); continue; }
    else flushTable();
    const h = raw.match(/^(#{1,6})\s+(.*)$/);
    if (h) {
      flushPara(); flushList();
      const level = h[1].length, text = h[2];
      let attr = "";
      if (headings) {
        const id = slugify(text, seen);
        headings.push({ level, text, id });
        attr = ` id="${id}"`;
      }
      html += `<h${level}${attr}>${inline(text)}</h${level}>`;
      continue;
    }
    if (/^\s*(---|\*\*\*|___)\s*$/.test(raw)) { flushPara(); flushList(); html += "<hr>"; continue; }
    if (/^\s*>\s?/.test(raw)) {
      flushPara(); flushList();
      if (!quote) quote = [[]];
      const qt = raw.replace(/^\s*>\s?/, "");
      if (/^\s*$/.test(qt)) { if (quote[quote.length - 1].length) quote.push([]); }
      else quote[quote.length - 1].push(qt);
      continue;
    }
    const li = raw.match(/^\s*([-*+]|\d+\.)\s+(.*)$/);
    if (li) {
      const t = /\d/.test(li[1]) ? "ol" : "ul";
      if (!inList || listType !== t) { flushList(); flushPara(); listType = t; html += `<${t}>`; inList = true; }
      html += `<li>${inline(li[2])}</li>`; continue;
    }
    if (/^\s*$/.test(raw)) { flushPara(); flushList(); continue; }
    para.push(raw);
  }
  flushPara(); flushList(); flushTable(); flushQuote();
  if (inCode) html += `<pre><code>${esc(code)}</code></pre>`;
  return html;
}
function inline(s) {
  // Escape once, here. Everything below works on already-escaped text, so a link's address
  // is taken as it stands: escaping it a second time turned every "&" into "&amp;amp;"
  // and sent the link to a different address.
  let t = esc(s).replace(/[\u0001\u0002]/g, "");
  // Lift each link's address out before the emphasis passes, so a "_" or "*" inside an
  // address is never read as italics or bold. A placeholder holds its place; it carries
  // no "_", "*" or backtick. Only http(s) addresses are lifted, as before.
  const hrefs = [];
  t = t.replace(/\[([^\]]+)\]\((https?:[^)\s]+)\)/g, (_, txt, url) => {
    hrefs.push(url);
    return `[${txt}](\u0001${hrefs.length - 1}\u0001)`;
  });
  t = t.replace(/`([^`]+)`/g, (_, c) => `<code>${c}</code>`);
  t = t.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
  t = t.replace(/(^|[^*])\*([^*]+)\*/g, "$1<em>$2</em>");
  // _italic_ (the tool's own report text uses it). It opens only after a space, an opening
  // bracket or a tag, and closes only before a non-word character, so the underscores in
  // run_02_full.json or review_target_count are never read as italics. An opening "_"
  // that starts an identifier (a file such as _run_ledger.json) is not an opening either.
  // Code spans are set aside first, so nothing inside them is touched.
  const codes = [];
  t = t.replace(/<code>[^<]*<\/code>/g, (c) => { codes.push(c); return `\u0002${codes.length - 1}\u0002`; });
  t = t.replace(/(^|[\s(\[>])_(?![A-Za-z0-9]+_[A-Za-z0-9])([^\s_](?:.*?[^\s_])??)_(?![A-Za-z0-9_])/g, "$1<em>$2</em>");
  t = t.replace(/\u0002(\d+)\u0002/g, (_, i) => codes[+i]);
  t = t.replace(/\[([^\]]+)\]\(\u0001(\d+)\u0001\)/g, (_, txt, i) =>
    `<a href="${hrefs[+i]}" target="_blank" rel="noopener noreferrer" title="Open in a new tab">${txt}</a>`);
  return t;
}

// Smooth-scroll a report heading into view. Registered on window because the
// TOC links are inline onclick handlers rendered into innerHTML — a plain
// href="#id" would be swallowed by the hash router, so we scroll manually.
window.scrollToHeading = (id) => {
  const el = document.getElementById(id);
  if (el) el.scrollIntoView({ behavior: "smooth", block: "start" });
};
// Build the sticky "On this page" rail from collected headings. We show H2 and
// H3 only: H1 is the report title (already in the masthead), H4+ is too granular
// for a rail. A report with fewer than two entries gets no rail — it would be noise.
function reportTOC(headings) {
  const items = (headings || []).filter(h => h.level === 2 || h.level === 3);
  if (items.length < 2) return "";
  const links = items.map(h =>
    `<a class="toc-link toc-h${h.level}" onclick="scrollToHeading('${h.id}')" title="${esc(stripInline(h.text))}">${esc(stripInline(h.text))}</a>`
  ).join("");
  return `<nav class="report-toc" aria-label="On this page">
    <div class="toc-title">On this page</div>${links}</nav>`;
}

/* ---- read-more toggle ---------------------------------------------------- */
function toggleClamp(btn) {
  const p = btn.previousElementSibling;
  const open = p.classList.toggle("open");
  btn.textContent = open ? "read less" : "read more";
}

/* Reveal the read-more button only when the paragraph is actually truncated.
   Called after every paintAds so filter repaints re-measure correctly. */
function wireReadMore() {
  document.querySelectorAll(".adbody").forEach(p => {
    const b = p.nextElementSibling;
    if (b && b.classList.contains("readmore")) b.hidden = !(p.scrollHeight > p.clientHeight + 1);
  });
}

/* Horizontal strips (carousel cards, dynamic-ad versions): arrow buttons so a desktop mouse
   can page through them; before this only a touchpad's sideways swipe could. Each arrow shows
   only when there is more in its direction, so a strip that
   fits shows none. Re-measured on scroll and on resize (panel collapse changes widths). */
const HS_CHEV_L = `<svg width="14" height="14" viewBox="0 0 14 14" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M8.5 3L4.5 7l4 4"/></svg>`;
const HS_CHEV_R = `<svg width="14" height="14" viewBox="0 0 14 14" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M5.5 3l4 4-4 4"/></svg>`;
function hsUpdate(track) {
  const wrap = track.parentElement;
  const prev = wrap.querySelector(".hs-prev"), next = wrap.querySelector(".hs-next");
  // 2px tolerance: sub-pixel card borders at fractional zoom can overflow by ~1px, which
  // would show an arrow that moves nothing.
  const max = Math.max(0, track.scrollWidth - track.clientWidth);
  if (prev) prev.hidden = track.scrollLeft <= 2;
  if (next) next.hidden = track.scrollLeft >= max - 2;
}
function hsStep(btn, dir) {
  const track = btn.parentElement.querySelector(".hs-track");
  const card = track.querySelector(".hs-card");
  const step = card ? card.getBoundingClientRect().width + 10 : track.clientWidth * 0.8;   // 10 = the strip gap
  const max = Math.max(0, track.scrollWidth - track.clientWidth);
  // Chain from a pending target so rapid clicks stay card-aligned mid-animation.
  const base = track._hsTarget != null ? track._hsTarget : track.scrollLeft;
  const target = Math.min(max, Math.max(0, base + dir * step));
  track._hsTarget = target;
  const reduced = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  track.scrollTo({ left: target, behavior: reduced ? "auto" : "smooth" });
  // Smooth scrolling needs animation frames; a throttled page (background window,
  // power saving, the embedded pane) can leave it short or unmoved. Guarantee the step.
  clearTimeout(track._hsTimer);
  track._hsTimer = setTimeout(() => {
    if (Math.abs(track.scrollLeft - target) > 2) track.scrollLeft = target;
    track._hsTarget = null;
    hsUpdate(track);
  }, 400);
}
function wireHScroll() {
  if (window._hsRO) window._hsRO.disconnect();
  window._hsRO = window.ResizeObserver ? new ResizeObserver(es => es.forEach(e => hsUpdate(e.target))) : null;
  document.querySelectorAll(".hs-track").forEach(t => {
    hsUpdate(t);
    t.addEventListener("scroll", () => hsUpdate(t), { passive: true });
    if (window._hsRO) window._hsRO.observe(t);
  });
}

/* ---- go ------------------------------------------------------------------ */
window.toggleFav = toggleFav;
window.editNote = editNote;
window.trashRecord = trashRecord;
window.restoreRecord = restoreRecord;
window.exportCurrent = exportCurrent;
window.clearFilters = clearFilters;
window.clearChangeFilter = clearChangeFilter;
window.toggleClamp = toggleClamp;
window.toggleRail = toggleRail;
window.toggleTips = toggleTips;
window.hsStep = hsStep;
window.toggleFilters = toggleFilters;
document.addEventListener("DOMContentLoaded", boot);
