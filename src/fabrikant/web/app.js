// fabrikant UI — vanilla JS, no build
const $ = (s) => document.querySelector(s);
let state = { projects: [], selected: null, runTimer: null, openLoop: null, graphOpen: null, openApprovals: null, openProposals: null, openShelved: null, schedOpen: false, schedTimer: null };
// Last loaded run record of the open panel — basis for the log toggle
// (formatted view ↔ raw log) and for the refresh after approve/reject.
let runPanelRec = null;
// Re-render guard: counts renderDetail calls. Stale (outdated) renders
// must no longer overwrite the detail view, otherwise a parallel
// auto-refresh would discard a just-opened loop spec.
let detailRenderSeq = 0;

// Auth (ADR 0030): mode "local" → session cookie + CSRF header on write requests.
const auth = { mode: "none", user: null, role: null, csrf: "" };

// ---- i18n (ADR 0049): de/en message catalogs, no framework, no build ----
// Translates ONLY the UI chrome strings. User content (specs, prompts, logs,
// chat answers, run records) stays in the language it was created in.
// Language choice: localStorage > browser language > Fallback "en".
const I18N_LANGS = ["de", "en"];
const I18N_DEFAULT = "en";
const I18N_STORAGE_KEY = "fabrikant.lang";
const i18n = { lang: I18N_DEFAULT, catalogs: {}, ready: false };

// t(key, vars): translation with {placeholders}; fallback en → the key itself.
function t(key, vars) {
  let s = i18n.catalogs[i18n.lang]?.[key] ?? i18n.catalogs.en?.[key] ?? key;
  if (vars) for (const [k, v] of Object.entries(vars)) s = s.split(`{${k}}`).join(String(v));
  return s;
}

// tOr(key, fallback): the translation or — for open values (unknown trigger
// type, status, failure class) — the raw value instead of the catalog key.
function tOr(key, fallback) {
  return i18n.catalogs[i18n.lang]?.[key] ?? i18n.catalogs.en?.[key] ?? fallback;
}

// Locale for Intl (clock, date/time) — the chosen UI language.
function i18nLocale() {
  return i18n.lang;
}

// Format a date/time (ISO string) via Intl with the chosen locale.
// Invalid/empty input → unchanged (no UI strings, only raw data).
function fmtDateTime(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  if (isNaN(d.getTime())) return String(iso);
  return d.toLocaleString(i18nLocale(), {
    year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit",
  });
}

// Stored choice > browser language > Fallback "en".
function i18nResolve() {
  let lang = null;
  try { lang = localStorage.getItem(I18N_STORAGE_KEY); } catch (e) { /* private mode */ }
  if (!lang || !I18N_LANGS.includes(lang)) {
    const nav = (navigator.language || "").toLowerCase();
    lang = I18N_LANGS.includes(nav.slice(0, 2)) ? nav.slice(0, 2) : I18N_DEFAULT;
  }
  return lang;
}

async function i18nLoad() {
  i18n.catalogs = {};
  await Promise.all(I18N_LANGS.map(async (l) => {
    try { i18n.catalogs[l] = await (await fetch(`/i18n/${l}.json`)).json(); }
    catch (e) { i18n.catalogs[l] = {}; }
  }));
  i18n.ready = true;
}

// Render static elements (index.html) + dynamic views in the new language.
function i18nApply() {
  document.documentElement.lang = i18n.lang;
  document.title = t("app.title");
  document.querySelectorAll("[data-i18n]").forEach((el) => { el.textContent = t(el.dataset.i18n); });
  document.querySelectorAll("[data-i18n-ph]").forEach((el) => { el.placeholder = t(el.dataset.i18nPh); });
  document.querySelectorAll("[data-i18n-title]").forEach((el) => { el.title = t(el.dataset.i18nTitle); });
  $("#langtoggle").textContent = i18n.lang.toUpperCase();
  renderProjects();
  if (state.selected) {
    // Rebuild the history too (filter labels) — a language switch is a
    // deliberate action, a filter reset is acceptable.
    const hv = $("#histview");
    if (hv) delete hv.dataset.project;
    renderDetail(state.selected);
  }
  if (state.online !== undefined) setHealth(state.online);
  if (runPanelRec) {
    // Re-label status badge + actions; log/transcript tab (live stream,
    // content) stays untouched — only the overview gets re-rendered.
    const st = $("#runstatus").dataset.status;
    setRunStatus(st);
    renderRunActions(st);
    if (runTab === "overview") $("#runlog").innerHTML = formatRun(runPanelRec);
  }
}

function setLang(lang) {
  if (!I18N_LANGS.includes(lang) || lang === i18n.lang) return;
  i18n.lang = lang;
  try { localStorage.setItem(I18N_STORAGE_KEY, lang); } catch (e) { /* private mode */ }
  i18nApply();
}

// Toggle in the header (base view): de ↔ en.
function toggleLang() {
  setLang(i18n.lang === "de" ? "en" : "de");
}

async function api(path, opts = {}) {
  const method = (opts.method || "GET").toUpperCase();
  if (auth.csrf && method !== "GET") {
    opts = { ...opts, headers: { ...opts.headers, "X-CSRF-Token": auth.csrf } };
  }
  const r = await fetch(path, opts);
  if (r.status === 401 && auth.mode !== "none") showLogin();
  if (!r.ok) {
    // Carry over the server's reason (`{"error": …}`) so messages can display it.
    const detail = await r.json().then((b) => (b && typeof b.error === "string" ? b.error : ""), () => "");
    const err = new Error(detail ? `${path} → ${r.status}: ${detail}` : `${path} → ${r.status}`);
    err.status = r.status; // e.g. 409 = optimistic lock (spec changed externally)
    err.detail = detail;
    throw err;
  }
  return r.json();
}

function applySession(s) {
  Object.assign(auth, { mode: s.mode, user: s.user, role: s.role, csrf: s.csrf || "" });
  const who = $("#whoami");
  who.classList.toggle("hidden", !auth.user);
  $("#whoami-name").textContent = auth.user ? `${auth.user} (${auth.role})` : "";
  $("#loginview").classList.toggle("hidden", sessionReady());
  $("#loginform").classList.toggle("hidden", auth.mode !== "local");
  $("#oidclogin").classList.toggle("hidden", auth.mode !== "oidc");
}

// No login needed (mode none) or already signed in.
function sessionReady() {
  return auth.mode === "none" || !!auth.user;
}

// OIDC: at most once per page load redirect automatically to the IdP (no redirect loop).
let oidcRedirected = false;

function showLogin() {
  applySession({ mode: auth.mode, user: null, role: null, csrf: "" });
  // OIDC: no form, straight to the IdP (ADR 0046); on failure the callback shows an error page.
  if (auth.mode === "oidc" && !oidcRedirected) {
    oidcRedirected = true;
    location.assign("/api/login/oidc");
  }
}

async function initSession() {
  try {
    applySession(await (await fetch("/api/session")).json());
  } catch (e) {
    /* server offline — the health indicator shows that */
  }
  if (sessionReady()) refresh();
  else showLogin();
}

async function doLogin(ev) {
  ev.preventDefault();
  const r = await fetch("/api/login", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ user: $("#login-user").value, password: $("#login-pass").value }),
  });
  $("#login-error").textContent = r.ok ? "" : t("login.error");
  if (!r.ok) return;
  $("#login-pass").value = "";
  await initSession();
}

async function doLogout() {
  await api("/api/logout", { method: "POST" }).catch(() => {});
  // OIDC: not straight back to the IdP — its SSO session would sign right back in.
  oidcRedirected = true;
  showLogin();
}

// HTML escape for text AND attribute values (including quotes). The UI uses
// no inline JS handlers anymore (issue #268): values live in data-* attributes,
// handlers are bound via addEventListener/delegation — there,
// entity-masked values are safe.
const ESC_MAP = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" };
function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ESC_MAP[c]);
}

// ---- Delegated click handlers (issue #268): no inline JS, values in data-* ----
// The INNERMOST [data-act] parent of the click target wins — this replaces
// the former event.stopPropagation() calls (button in a clickable row).
// [data-stop] swallows clicks without action (e.g. the locked ▶ run in the
// clickable loop card).

// Project list: selection, approval/shelve badges, approval list, shelve actions.
function onProjectsClick(e) {
  const el = e.target.closest("[data-act]");
  if (!el) return;
  const d = el.dataset;
  switch (d.act) {
    case "select-project": selectProject(d.name); break;
    case "toggle-approvals": toggleApprovals(d.name); break;
    case "open-run": openRun(d.run); break;
    case "toggle-proposals": toggleProposals(d.name); break;
    case "open-proposal": openProposal(d.id); break;
    case "toggle-shelved": toggleShelved(d.name); break;
    case "shelved-retry":
    case "shelved-dismiss":
      shelvedAction(d.name, d.loop, d.key, d.act === "shelved-retry" ? "retry" : "dismiss");
      break;
  }
}

// Detail view: header (spec/graph), loop cards, run buttons, state buttons
// and the graph editor (card + save bar).
function onDetailClick(e) {
  if (e.target.closest("[data-stop]")) return;
  const el = e.target.closest("[data-act]");
  if (!el || !el.closest("#detail-main")) return;
  const d = el.dataset;
  switch (d.act) {
    case "toggle-spec": toggleSpec(d.name); break;
    case "toggle-graph": toggleGraph(d.name); break;
    case "toggle-loop-spec": toggleLoopSpec(d.name, d.loop); break;
    case "start-run": startRun(d.name, d.loop); break;
    case "set-state": setRunState(d.name, d.loop || null, d.to); break;
    case "graph-save": graphDraftSave(); break;
    case "graph-discard": graphDraftDiscard(); break;
    case "close-card": closeGraphCard(); break;
    case "card-add-step": graphCardAddStep(); break;
    case "card-add-trigger": graphCardAddTrigger(); break;
    case "card-remove-loop": graphCardRemoveLoop(); break;
    case "card-move-step": graphCardMoveStep(parseInt(d.dir, 10)); break;
    case "card-remove-step": graphCardRemoveStep(); break;
    case "step-move": graphCardMoveStepInLoop(parseInt(d.idx, 10), parseInt(d.dir, 10)); break;
    case "step-remove": graphCardRemoveStepInLoop(parseInt(d.idx, 10)); break;
    case "trigger-remove": graphCardRemoveTriggerInLoop(parseInt(d.idx, 10)); break;
  }
}

// Context menu of the graph: closes before every action (replaces the former
// `before` argument of the state buttons). ctx-run/ctx-edit close themselves
// (they read graphCtxTarget, which hideGraphCtx() sets to null).
function onGraphCtxClick(e) {
  const el = e.target.closest("[data-act]");
  if (!el) return;
  const d = el.dataset;
  if (d.act === "ctx-run") graphCtxRun();
  else if (d.act === "ctx-edit") graphCtxEdit();
  else if (d.act === "set-state") { hideGraphCtx(); setRunState(d.name, d.loop || null, d.to); }
}

// Approval buttons of the run modal (no value needed: runApprove reads id/project
// from the title).
function onRunActionsClick(e) {
  const el = e.target.closest("[data-act]");
  if (!el) return;
  if (el.dataset.act === "run-approve") runApprove(true);
  else if (el.dataset.act === "run-reject") runApprove(false);
}

// Schedule panel: run now, one-time form (⏱ open, set, close)
// and pending (✕). Values in data-*.
function onSchedClick(e) {
  const el = e.target.closest("[data-act]");
  if (!el) return;
  const d = el.dataset;
  switch (d.act) {
    case "sched-run-now": startRun(d.project, d.loop); break;
    case "sched-ot-open": schedOtOpen(d.project, d.loop); break;
    case "sched-ot-cancel": schedOnetimeCancel(d.project, d.loop); break;
    case "sched-ot-submit": schedOnetimeSubmit(); break;
    case "sched-ot-close": schedOtClose(); break;
  }
}

// Enter/Escape in the one-time field (#sched-when is re-created on every open).
function onSchedKeydown(e) {
  if (e.target.id !== "sched-when") return;
  if (e.key === "Enter") schedOnetimeSubmit();
  else if (e.key === "Escape") schedOtClose();
}

function badge(text, cls) {
  return `<span class="badge ${cls || ""}">${esc(text)}</span>`;
}

// Failure class of failed runs/steps (ADR 0035); legacy records without the field = unknown.
// Mirror of runmodel.FAILED_RUN_STATUSES / FAILED_STEP_STATUSES (parity is checked by tests/test_web_constants.py).
const FAILED_RUN_STATUSES = ["error", "fail"];
const FAILED_STEP_STATUSES = ["error", "fail", "timeout"];
// Failure-class hints from the i18n catalog (keys `kind.<class>`).
const FAILURE_KINDS = ["interrupted", "transient", "environment", "outcome", "unknown"];
const kindHint = (kind) => tOr(`kind.${kind}`, kind);
const kindLabel = (kind) => tOr(`kind.label.${kind}`, kind);
const stateLabel = (st) => tOr(`state.label.${st}`, st);
// Failure-class badge (label + hint tooltip) for a class; unknown
// classes are shown raw (tOr fallback).
function kindBadgeHtml(kind) {
  return `<span class="badge kind ${esc(kind)}" title="${esc(t("kind.title", { kind: kindHint(kind) }))}">${esc(kindLabel(kind))}</span>`;
}
function kindBadge(rec, failedStatuses = FAILED_RUN_STATUSES) {
  if (!rec || !failedStatuses.includes(rec.status)) return "";
  const kind = FAILURE_KINDS.includes(rec.failure_kind) ? rec.failure_kind : "unknown";
  return kindBadgeHtml(kind);
}

// Verdict badge (business outcome, e.g. relay-audit `fail`) — independent of run status: a
// green run may have published a negative verdict. Run: verdict of the last step that
// reported one (mirror of runmodel.Run.verdict); step: its own.
const VERDICT_TONES = { pass: "good", done: "good", fail: "bad", blocked: "bad" };
function runVerdict(rec) {
  if (!rec) return "";
  if (rec.verdict) return rec.verdict;
  const steps = Array.isArray(rec.steps) ? rec.steps : [];
  for (let i = steps.length - 1; i >= 0; i--) if (steps[i] && steps[i].verdict) return steps[i].verdict;
  return "";
}
function verdictBadge(rec) {
  const v = runVerdict(rec);
  if (!v) return "";
  const tone = VERDICT_TONES[v] || "neutral";
  return `<span class="badge verdict ${tone}" title="${esc(t("verdict.title"))}">${esc(t("verdict.label", { verdict: tOr(`verdict.value.${v}`, v) }))}</span>`;
}

// Health display; remember the state so a language switch can re-label it.
function setHealth(online) {
  state.online = online;
  $("#health").textContent = t(online ? "health.online" : "health.offline");
}

// opts.force: after an explicit user action (pause/resume) re-render the
// detail view in any case — the guards below apply only to the
// auto-refresh. renderDetail restores the open loop spec and history itself.
async function refresh(opts = {}) {
  try {
    state.projects = await api("/api/projects");
  } catch (e) {
    setHealth(false);
    return;
  }
  setHealth(true);
  renderProjects();
  // Re-render the detail view only if no loop spec is open and no graph is open
  // — otherwise
  // the auto-refresh (5 s) would discard the open spec. The
  // run history lies outside the re-rendered part and is always
  // updated in place (loaded pages + scroll position are preserved).
  if (!state.selected) return;
  if (opts.force || (!state.openLoop && !state.graphOpen)) renderDetail(state.selected);
  loadHistory(state.selected, "refresh");
}

function renderProjects() {
  const el = $("#projects");
  el.innerHTML = state.projects.map((p) => {
    if (p.error) {
      return `<div class="project"><div class="pname">✖ ${esc(p.name)}</div>
        <div class="pdesc">${esc(p.error)}</div></div>`;
    }
    return `<div class="project ${p.name === state.selected ? "active" : ""}"
        data-act="select-project" data-name="${esc(p.name)}">
      <div class="pname">${esc(p.name)}${stateBadge(p.state)}${approvalBadge(p)}${proposalBadge(p)}${shelvedBadge(p)}</div>
      <div class="pdesc">${esc(p.description)}</div>
      ${p.branch && p.branch !== "-" ? `<div class="pbranch">⎇ ${esc(p.branch)}</div>` : ""}
      ${loopSummary(p)}
      ${approvalList(p)}
      ${proposalList(p)}
      ${shelvedList(p)}
    </div>`;
  }).join("");
}

// Accordion (no nested scroll): only the selected project shows
// its loops, all others only the count. The state follows state.selected
// and thereby survives every re-render (5-s refresh).
function loopSummary(p) {
  const loops = p.loops || [];
  const head = `<div class="ploopcount">${esc(t("loops.count", { n: loops.length, s: loops.length === 1 ? "" : "s" }))}</div>`;
  if (p.name !== state.selected || !loops.length) return head;
  return head + `<div class="ploops">` + loops.map((l) => {
    const st = l.last ? l.last.status : "–";
    return `<div class="ploop"><span class="ploopname">${esc(l.name)}</span>
      <span class="ploopstatus ${esc(st)}">${esc(statusLabel(st))}</span></div>`;
  }).join("") + `</div>`;
}

// Open approval requests (needs_approval): badge with count in the project view.
// Click (stopPropagation: the project is NOT selected) expands the list.
function approvalBadge(p) {
  const n = (p.pending_approvals || []).length;
  if (!n) return "";
  return `<span class="apprbadge" title="${esc(t("approvals.badgeTitle", { n }))}"
      data-act="toggle-approvals" data-name="${esc(p.name)}">⏸ ${n}</span>`;
}

// Expanded list of open approvals (newest first). Clicking an
// entry opens the run-log modal — the approval context (what/why) is
// rendered there as usual (formatRun → formatApproval).
function approvalList(p) {
  const appr = p.pending_approvals || [];
  if (!appr.length || state.openApprovals !== p.name) return "";
  return `<div class="applist">` + appr.map((a) => {
    const ap = a.approval || {};
    const what = [ap.condition, ap.why || ap.pr_title || ap.what || ap.reason].filter(Boolean).join(" · ");
    return `<div class="apprline" title="${esc(t("approvals.openRun"))}" data-act="open-run" data-run="${esc(a.id)}">
      <span class="apprloop">${esc(a.loop)}</span>
      <span class="apprtime">${esc(fmtDateTime(a.started))}</span>
      <span class="apprwhat">${esc(what)}</span>
    </div>`;
  }).join("") + `</div>`;
}

function toggleApprovals(name) {
  state.openApprovals = state.openApprovals === name ? null : name;
  renderProjects();
}

// Spec change proposals (ADR 0066): badge with count, expandable list, diff modal
// with apply/reject. The server computes the diff (GET /api/spec-proposals/<id>).
const PROPOSALS_API = "/api/spec-proposals";
const proposalUi = { diff: null, error: "" };

function proposalBadge(p) {
  const n = (p.pending_proposals || []).length;
  if (!n) return "";
  return `<span class="apprbadge propbadge" title="${esc(t("proposals.badgeTitle", { n }))}"
      data-act="toggle-proposals" data-name="${esc(p.name)}">✎ ${n}</span>`;
}

function proposalList(p) {
  const list = p.pending_proposals || [];
  if (!list.length || state.openProposals !== p.name) return "";
  return `<div class="applist">` + list.map((pr) => `<div class="apprline" data-act="open-proposal" data-id="${esc(pr.id)}">
      <span class="apprloop">${esc(pr.proposed_by)}</span>
      <span class="apprtime">${esc(fmtDateTime(pr.created))}</span>
      <span class="apprwhat">${esc(pr.changes.join(", "))}</span>
    </div>`).join("") + `</div>`;
}

function toggleProposals(name) {
  state.openProposals = state.openProposals === name ? null : name;
  renderProjects();
}

// Line diff (unified) in red/green; `@@` headers dimmed.
function diffHtml(lines) {
  return `<pre class="propdiff">` + lines.map((ln) => {
    const cls = ln.startsWith("+") ? "add" : ln.startsWith("-") ? "del" : ln.startsWith("@@") ? "hunk" : "";
    return `<span class="${cls}">${esc(ln)}\n</span>`;
  }).join("") + `</pre>`;
}

function loopChangeHtml(c) {
  const body = c.kind === "changed" ? diffHtml(c.diff)
    : diffHtml((c.kind === "added" ? c.after : c.before).split("\n").filter(Boolean).map((l) => (c.kind === "added" ? "+" : "-") + l));
  return `<details class="proploop"><summary><span class="propkind ${esc(c.kind)}">${esc(t(`proposals.kind.${c.kind}`))}</span> ${esc(c.name)}</summary>${body}</details>`;
}

function proposalHtml(d) {
  const canDecide = d.status === "pending" && !d.error && auth.role !== "viewer";
  const n = d.counts || {};
  const fields = d.fields.map((f) => `<div class="propfield"><b>${esc(f.name)}</b> ${esc(f.before || "–")} → ${esc(f.after || "–")}</div>`).join("");
  return `<div class="propmeta">${esc(d.project)} · ${esc(t("proposals.by", { who: d.proposed_by }))} · ${esc(fmtDateTime(d.created))}
      · ${esc(t(`proposals.status.${d.status}`))}${d.decided_by ? ` (${esc(d.decided_by)})` : ""}</div>
    ${d.stale ? `<div class="proposal-warn">${esc(t("proposals.stale"))}</div>` : ""}
    ${d.error ? `<div class="proposal-warn">${esc(d.error)}</div>` : ""}
    ${d.status === "pending" && !d.error ? `<div class="propsummary">${esc(t("proposals.summary", { added: n.added, changed: n.changed, removed: n.removed }))}</div>` : ""}
    ${fields}${d.loops.map(loopChangeHtml).join("")}
    <div class="proposal-err">${esc(proposalUi.error)}</div>
    ${canDecide ? `<div class="propactions">
      <button class="run" data-act="prop-apply"${d.stale ? " disabled" : ""}>${esc(t("proposals.apply"))}</button>
      <button class="ghost" data-act="prop-reject">${esc(t("proposals.reject"))}</button></div>` : ""}`;
}

function renderProposal() {
  $("#proposalbody").innerHTML = proposalUi.diff ? proposalHtml(proposalUi.diff) : `<div class="proposal-err">${esc(proposalUi.error)}</div>`;
}

async function openProposal(id) {
  Object.assign(proposalUi, { diff: null, error: "" });
  $("#proposalbackdrop").classList.remove("hidden");
  $("#proposalpanel").classList.remove("hidden");
  try {
    proposalUi.diff = await api(`${PROPOSALS_API}/${encodeURIComponent(id)}`);
  } catch (e) {
    proposalUi.error = e.detail || e.message;
  }
  renderProposal();
}

function closeProposal() {
  $("#proposalbackdrop").classList.add("hidden");
  $("#proposalpanel").classList.add("hidden");
}

async function decideProposal(action) {
  const d = proposalUi.diff;
  if (!d || !confirm(t(`proposals.confirm.${action}`, { project: d.project }))) return;
  try {
    await api(`${PROPOSALS_API}/${encodeURIComponent(d.id)}/${action}`, { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" });
    closeProposal();
    await refresh({ force: true });
  } catch (e) {
    proposalUi.error = e.detail || e.message;
    renderProposal();
  }
}

function onProposalClick(e) {
  const act = e.target.closest("[data-act]")?.dataset.act;
  if (act === "prop-apply") decideProposal("apply");
  else if (act === "prop-reject") decideProposal("reject");
  else if (act === "prop-close") closeProposal();
}

// Exhausted retry chains ("shelved", ADR 0036, decision 6): like
// approvals — badge with count, expandable list with class, last error,
// attempt count and the actions *retry now* (exhausted → immediate run)
// and *discard* (chain dismissed).
function shelvedBadge(p) {
  const n = (p.shelved || []).length;
  if (!n) return "";
  return `<span class="shelvedbadge" title="${esc(t("shelved.badgeTitle", { n }))}"
      data-act="toggle-shelved" data-name="${esc(p.name)}">📦 ${n}</span>`;
}

function shelvedList(p) {
  const sh = p.shelved || [];
  if (!sh.length || state.openShelved !== p.name) return "";
  return `<div class="shelvedlist">` + sh.map((c) => {
    const kind = FAILURE_KINDS.includes(c.failure_kind) ? c.failure_kind : (c.failure_kind || "unknown");
    const attempt = c.attempt != null ? esc(t("shelved.attempt", { n: c.attempt })) : "";
    return `<div class="shelvedline">
      <span class="shelvedloop">${esc(c.loop)}</span>
      ${kindBadgeHtml(kind)}
      <span class="shelvedmeta">${esc(c.trigger_key)}${attempt}</span>
      <span class="shelvederr" title="${esc(c.error)}">${esc(c.error)}</span>
      <span class="shelvedactions">
        <button class="run" title="${esc(t("shelved.retryTitle"))}"
          data-act="shelved-retry" data-name="${esc(p.name)}" data-loop="${esc(c.loop)}" data-key="${esc(c.trigger_key)}">${esc(t("shelved.retry"))}</button>
        <button class="ghost" title="${esc(t("shelved.dismissTitle"))}"
          data-act="shelved-dismiss" data-name="${esc(p.name)}" data-loop="${esc(c.loop)}" data-key="${esc(c.trigger_key)}">${esc(t("shelved.dismiss"))}</button>
      </span>
    </div>`;
  }).join("") + `</div>`;
}

function toggleShelved(name) {
  state.openShelved = state.openShelved === name ? null : name;
  renderProjects();
}

async function shelvedAction(project, loop, triggerKey, action) {
  const label = t(`shelved.action.${action}`);
  if (!confirm(t("shelved.confirm", { loop, triggerKey, action: label }))) return;
  try {
    await api(`/api/retry-chains/${action === "retry" ? "retry-now" : "dismiss"}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ project, loop, trigger_key: triggerKey }),
    });
  } catch (e) {
    alert(t("shelved.failed", { error: e.message }));
  }
  refresh({ force: true });
}

// Pause/Disable (ADR 0019): badge + buttons per project/loop.
function stateBadge(st) {
  if (!st || st.state === "active") return "";
  const until = st.until ? esc(t("state.until", { until: fmtDateTime(st.until) })) : "";
  return ` <span class="statebadge ${esc(st.state)}">${esc(stateLabel(st.state))}${until}</span>`;
}

// State buttons (pause/resume/disable): values in data-*, action via delegation
// (onDetailClick resp. onGraphCtxClick — the latter closes the menu first).
function stateButtons(project, loop, st) {
  const cur = (st && st.state) || "active";
  const btn = (to, label) => cur === to ? "" :
    `<button class="ghost" data-act="set-state" data-to="${to}" data-name="${esc(project)}"${loop ? ` data-loop="${esc(loop)}"` : ""}>${label}</button>`;
  return btn("active", t("state.resume")) + btn("paused", t("state.pause")) + btn("disabled", t("state.disable"));
}

// Hint on the locked ▶ run: where to lift the effective `disabled` state.
// A disabled project overrides the loop state (pause.effective_state)
// — then the loop card has no matching resume button, only the header does.
function runLockReason(p) {
  return (p.state || {}).state === "disabled" ? t("state.lockProject") : t("state.lockLoop");
}

async function setRunState(project, loop, to) {
  let until;
  if (to !== "active") {
    until = prompt(t("state.promptUntil"), "") || undefined;
  }
  try {
    await api(`/api/projects/${encodeURIComponent(project)}/state`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ loop: loop || undefined, state: to, until }),
    });
    await refresh({ force: true });
  } catch (e) {
    alert(t("state.changeFailed", { error: e.message }));
  }
}

function selectProject(name) {
  if (state.selected !== name) { state.openLoop = null; state.graphOpen = null; }
  state.selected = name;
  renderProjects();
  renderDetail(name);
  prefetchLoopSpecs(name);
}

// Loads the complete spec and fills loopSpecCache so loop specs can be opened
// (without their own fetch) immediately. Important: the auto-refresh must
// restore the open spec *synchronously* — its own fetch per
// re-render would keep getting aborted under parallel renders.
async function prefetchLoopSpecs(name) {
  try {
    const spec = await api(`/api/projects/${encodeURIComponent(name)}/spec`);
    for (const [loopName, ldef] of Object.entries(spec.loops || {})) {
      loopSpecCache[name + "/" + loopName] = renderLoopSpec(ldef);
    }
  } catch (e) { /* ignore */ }
}

// Detail view = re-rendered header/loops (#detail-main) + run history
// (#histview). The history is only rebuilt on project switch, so that
// auto-refresh renders neither discard the filter focus nor the loaded pages.
function renderDetail(name) {
  ++detailRenderSeq;
  const p = state.projects.find((x) => x.name === name);
  const el = $("#detail");
  if (!p || p.error) { el.innerHTML = `<div class="placeholder">${esc(p ? p.error : t("detail.notFound"))}</div>`; return; }
  if (!$("#detail-main") || $("#histview").dataset.project !== name) {
    el.innerHTML = `<div id="detail-main"></div><div id="histview" class="histview"></div>`;
    openHistory(name);
  }
  // Graph view mode: the graph replaces the loop list (no extra panel).
  const graphMode = state.graphOpen === name;
  $("#detail-main").innerHTML = `
    <div class="row" style="margin-bottom:14px">
      <div>
        <div style="font-family:var(--mono);font-size:17px;font-weight:700">${esc(p.name)}</div>
        <div style="color:var(--dim);font-size:12px">${esc(p.description)} · ${esc(p.root)}</div>
      </div>
      <span class="row" style="margin-left:auto">${stateBadge(p.state)}${stateButtons(p.name, null, p.state)}</span>
      <button class="ghost" data-act="toggle-spec" data-name="${esc(p.name)}">${esc(t("detail.spec"))}</button>
      <button class="ghost" data-act="toggle-graph" data-name="${esc(p.name)}">${esc(t(graphMode ? "detail.list" : "detail.graph"))}</button>
    </div>
    <div id="specview" class="specview hidden"></div>
    ${graphMode
      ? `<div id="graphview" class="graphview">
          <div id="graphcanvas" class="graphcanvas"></div>
          <div id="graphnode" class="graphnode hidden"></div>
          <div id="graphbar" class="graphbar hidden">
            <span class="graphnode-dirty">${esc(t("graph.dirty"))}</span>
            <span class="graphnode-err" id="graphbar-err"></span>
            <button class="ghost" title="${esc(t("graph.discardTitle"))}" data-act="graph-discard">${esc(t("graph.discard"))}</button>
            <button class="run" data-act="graph-save">${esc(t("graph.save"))}</button>
          </div>
        </div>`
      : p.loops.map((l) => `
      <div class="loop ${l.effective_state === "disabled" ? "disabled" : ""}" id="loop-${esc(l.name)}" data-act="toggle-loop-spec" data-name="${esc(p.name)}" data-loop="${esc(l.name)}" title="${esc(t("detail.loopSpecHint"))}">
        <div class="row" style="margin-bottom:4px">
          <h3 style="margin:0">${esc(l.name)}<span class="loophint">▸ spec</span>${stateBadge({ state: l.effective_state, until: (l.state || {}).until })}</h3>
          <span style="margin-left:auto">${l.triggers.map((tr) => badge(tr, "trig")).join("")}</span>
        </div>
        <div class="ldesc">${esc(l.description)}</div>
        <div class="row">
          ${l.steps.map((s) => `<span class="stepchip">${esc(s)}</span>`).join("")}
          ${l.last ? badge(statusLabel(l.last.status) + " · " + fmtDateTime(l.last.finished), l.last.status) + kindBadge(l.last) + verdictBadge(l.last) : badge(t("detail.neverRun"))}
          ${stateButtons(p.name, l.name, l.state)}
          ${l.effective_state === "disabled"
            ? `<span class="runlock" title="${esc(runLockReason(p))}" data-stop><button class="run" disabled>${esc(t("detail.run"))}</button></span>`
            : `<button class="run" data-act="start-run" data-name="${esc(p.name)}" data-loop="${esc(l.name)}">${esc(t("detail.run"))}</button>`}
        </div>
        <div class="loopspec hidden" id="loopspec-${esc(l.name)}"></div>
      </div>`).join("")}`;
  // Restore the kept-open loop spec after a re-render (auto-refresh).
  if (state.openLoop) openLoopSpec(name, state.openLoop);
  // Restore the open graph after a (forced) re-render
  // (from graphDrafts — no own fetch, so parallel renders don't race).
  if (state.graphOpen === name) loadGraph(name);
  // Graph card: delegated input handler (field change → draft update).
  // #graphnode is re-created on every renderDetail → exactly one listener.
  const gn = $("#graphnode");
  if (gn) gn.addEventListener("input", graphNodeInput);
}

// ---- History: searchable, paginated run list (issue #163) ----
const HISTORY_STATUSES = ["running", "queued", "needs_approval", "pass", "fail", "error", "rejected"];
const HISTORY_PAGE_SIZE = 20;
const HISTORY_MAX_PAGE_SIZE = 200; // = runsearch.MAX_PAGE_SIZE (server truncates above this)
const HISTORY_EMPTY_FILTER = { q: "", from: "", to: "", status: "", failure_kind: "", loop: "" };
let histFilter = { ...HISTORY_EMPTY_FILTER };
let histRuns = [];
let histCursor = null;
let histHtml = "";
let histSeq = 0; // only reset/more count up → discard outdated responses
let histBusy = false; // reset/more in flight → suspend refresh (would overwrite the new page)
let histTimer = null;

// Build a project's history (filters reset) and load the first page.
// Only called on project switch — see renderDetail.
function openHistory(name) {
  const p = state.projects.find((x) => x.name === name);
  const el = $("#histview");
  if (!p || !el) return;
  el.dataset.project = name;
  histFilter = { ...HISTORY_EMPTY_FILTER };
  histRuns = [];
  histCursor = null;
  histHtml = "";
  const opt = (label) => (v) => `<option value="${esc(v)}">${esc(v ? label(v) : t("history.all"))}</option>`;
  el.innerHTML = `
    <h4>${esc(t("history.title"))}</h4>
    <div class="histfilters">
      <input id="h-q" type="search" placeholder="${esc(t("history.search"))}">
      <label>${esc(t("history.from"))} <input id="h-from" type="date"></label>
      <label>${esc(t("history.to"))} <input id="h-to" type="date"></label>
      <select id="h-status" title="${esc(t("history.status"))}">${["", ...HISTORY_STATUSES].map(opt(statusLabel)).join("")}</select>
      <select id="h-kind" title="${esc(t("history.kind"))}">${["", ...FAILURE_KINDS].map(opt(kindLabel)).join("")}</select>
      <select id="h-loop" title="${esc(t("history.loop"))}">${["", ...p.loops.map((l) => l.name)].map(opt((n) => n)).join("")}</select>
    </div>
    <div id="h-list"></div>
    <button id="h-more" class="ghost hidden">${esc(t("history.more"))}</button>`;
  const onChange = () => { clearTimeout(histTimer); histTimer = setTimeout(() => loadHistory(name, "reset"), 250); };
  for (const [id, key] of [["h-q", "q"], ["h-from", "from"], ["h-to", "to"], ["h-status", "status"], ["h-kind", "failure_kind"], ["h-loop", "loop"]]) {
    $("#" + id).addEventListener("input", (e) => { histFilter[key] = e.target.value; onChange(); });
  }
  $("#h-list").addEventListener("click", onHistoryClick);
  $("#h-more").addEventListener("click", () => loadHistory(name, "more"));
  loadHistory(name, "reset");
}

// Load a page. mode:
// - "reset":   new search from the start (filters changed / project opened)
// - "more":    append the next page via cursor
// - "refresh": update already-loaded runs in place (auto-refresh,
//              after actions). Loads as many as are already shown, so the
//              list doesn't shrink to page 1 and the scroll doesn't jump.
async function loadHistory(name, mode) {
  if (!$("#h-list") || $("#histview").dataset.project !== name) return;
  const isRefresh = mode === "refresh";
  if (isRefresh && histBusy) return;
  const seq = isRefresh ? histSeq : ++histSeq;
  if (!isRefresh) histBusy = true;
  const loaded = histRuns.length;
  const limit = isRefresh ? Math.min(Math.max(loaded, HISTORY_PAGE_SIZE), HISTORY_MAX_PAGE_SIZE) : HISTORY_PAGE_SIZE;
  const qs = new URLSearchParams({ project: name, ...histFilter, limit });
  if (mode === "more" && histCursor) qs.set("cursor", histCursor);
  // Drop empty filters, but always send "q": switches the API into search mode.
  for (const [k, v] of [...qs]) if (!v && k !== "q") qs.delete(k);
  let data;
  try {
    data = await api(`/api/runs?${qs}`);
  } catch (e) {
    if (isRefresh || seq !== histSeq) return; // refresh error (briefly offline): keep the list
    histBusy = false;
    histHtml = "";
    if ($("#h-list")) $("#h-list").innerHTML = `<div class="runerr">✖ ${esc(e.message)}</div>`;
    return;
  }
  if (seq !== histSeq || !$("#h-list")) return;
  histBusy = false;
  if (mode === "more") {
    histRuns = histRuns.concat(data.runs);
    histCursor = data.next_cursor;
  } else if (isRefresh && loaded > limit) {
    // More loaded than a single request may deliver: replace the head, keep rest + cursor.
    const ids = new Set(data.runs.map((r) => r.id));
    histRuns = data.runs.concat(histRuns.slice(limit).filter((r) => !ids.has(r.id)));
  } else {
    histRuns = data.runs;
    histCursor = data.next_cursor;
  }
  renderHistoryList();
}

function renderHistoryList() {
  const html = histRuns.length ? histRuns.map((r) => `
    <div class="histrow" data-run="${esc(r.id)}" data-project="${esc(r.project)}">
      <span class="histmain" data-act="open">
        <span class="histid">${esc(r.id)}</span> ${badge(statusLabel(r.status), r.status)}${kindBadge(r)}${verdictBadge(r)}
        <span class="histmeta">${esc(r.loop)} · ${esc(r.trigger)} · ${esc(fmtDateTime(r.started))}</span>
        ${fmtCost(r.cost) ? `<span class="histcost">${esc(fmtCost(r.cost))}</span>` : ""}
        ${r.note ? `<span class="histnote">${esc(r.note)}</span>` : ""}
      </span>
      ${r.status === "needs_approval" ? `<span class="histactions">
        <button class="run" data-act="approve">${esc(t("history.approve"))}</button>
        <button class="ghost" data-act="reject">${esc(t("history.reject"))}</button>
      </span>` : ""}
    </div>`).join("") : `<div class="histempty">${esc(t("history.empty"))}</div>`;
  // Don't rewrite an unchanged list (no flicker / hover loss on auto-refresh).
  if (html !== histHtml) $("#h-list").innerHTML = histHtml = html;
  $("#h-more").classList.toggle("hidden", !histCursor);
}

// Delegated click handler of the history list: values come from data-* (no inline JS).
function onHistoryClick(e) {
  const act = e.target.closest("[data-act]");
  const row = act && act.closest(".histrow");
  if (!row) return;
  const { run, project } = row.dataset;
  if (act.dataset.act === "open") openRun(run, project);
  else histApprove(project, run, act.dataset.act === "approve");
}

// Approve/reject directly from the list (with confirmation); same API as chat/modal.
async function histApprove(project, runId, ok) {
  const action = t(ok ? "history.approve" : "history.reject");
  if (!confirm(t("history.confirm", { runId, action }))) return;
  try {
    await api(ok ? "/api/approve" : "/api/reject", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ project, run_id: runId }),
    });
  } catch (e) {
    alert(t(ok ? "history.approveFailed" : "history.rejectFailed", { error: e.message }));
  }
  refresh();
}

// Optional initial variables (K=V, one per line) for manual runs —
// e.g. issue_number=42 for issue-driven loops (like CLI --var).
// null = the user cancelled.
function promptVars() {
  const raw = prompt(t("run.varsPrompt"));
  if (raw === null) return null;
  const vars = {};
  for (const line of raw.split("\n")) {
    const trimmed = line.trim();
    if (!trimmed) continue;
    const i = trimmed.indexOf("=");
    if (i <= 0) {
      alert(t("run.invalidVar", { var: trimmed }));
      return null;
    }
    vars[trimmed.slice(0, i).trim()] = trimmed.slice(i + 1).trim();
  }
  return vars;
}

async function startRun(project, loop) {
  const vars = promptVars();
  if (vars === null) return;
  try {
    const r = await api("/api/runs", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ project, loop, vars }),
    });
    // The server returns the run id in the response (BEFORE the worker writes the record
    // to disk) → the panel follows exactly this run by id.
    // (Formerly: search for the "newest run of the loop" — in the race the panel found the
    // OLD run, whose terminal status stopped the polling immediately.)
    if (r.run_id) {
      openRunPanel(`${project} / ${loop} · ${r.run_id}`, r.queued ? "queued" : "running");
      pollRunById(project, r.run_id);
    } else {
      openRunPanel(`${project} / ${loop}`, "running");
    }
  } catch (e) {
    alert(t("run.startFailed", { error: e.message }));
  }
}

// Polls a specific run by id (robust against the start race).
// Run id in the title: runApprove() reads it from there for /api/approve|reject.
// Without the id the buttons in the live panel (needs_approval) would be inactive.
// opts.graceNeedsApproval: after an approval the resume worker writes
// "running" only shortly AFTER the POST → needs_approval is here a
// transitional status, not a gate. Without the flag the polling stops at the gate,
// because the run (correctly) pauses there.
async function pollRunById(project, runId, opts = {}) {
  clearTimeout(state.runTimer);
  try {
    const rec = await api(`/api/run/${encodeURIComponent(runId)}?project=${encodeURIComponent(project)}`);
    openRunPanel(`${project} / ${rec.loop} · ${runId}`, rec.status);
    setRunRecord(rec);
    if (runTab === "overview") $("#runlog").scrollTop = $("#runlog").scrollHeight;
  } catch (e) { /* ignore */ }
  const st = $("#runstatus").dataset.status;
  // queued: a manual run waits with priority for the project lock (ADR 0015).
  const keep = st === "running" || st === "queued" || (opts.graceNeedsApproval && st === "needs_approval");
  if (keep) state.runTimer = setTimeout(() => pollRunById(project, runId, opts), 1500);
  refresh();
}

const specCache = {};

async function toggleSpec(name) {
  const el = $("#specview");
  if (!el) return;
  if (!el.classList.contains("hidden")) { el.classList.add("hidden"); return; }
  el.classList.remove("hidden");
  if (!specCache[name]) {
    el.textContent = t("spec.loading");
    try {
      const r = await fetch(`/api/projects/${encodeURIComponent(name)}/spec.yaml`);
      specCache[name] = r.ok ? await r.text() : t("spec.loadFailed", { status: r.status });
    } catch (e) {
      specCache[name] = `✖ ${e.message}`;
    }
  }
  el.textContent = specCache[name];
}

// ---- Spec graph (Cytoscape) — view mode that replaces the loop list ----
// The graph is a DRAFT editor: it renders from an in-memory working copy
// (graphDrafts) of the spec, not from the server payload. Adding/changing/
// removing/reordering loops & steps only changes the draft (dry-run) and
// is written into the spec only via "💾 save". Details/editor of a node
// appear as a floating card (#graphnode) IN the graph, not in the bottom panel.
//
// Layers: top = project + loops (edges colored by trigger type); click on
// a loop → drill-down to its steps as a directed chain from the loop root
// (loop → s1 → s2 → …; only the first step has an edge to the loop).
// Step/loop/project click → card (details + edit). Gray ghost nodes
// ("+ New Loop" / "+ New Step") add nodes.

let graphRenderSeq = 0;
const graphDrafts = {}; // project → working copy of the spec (draft)
const graphSaved = {};  // project → JSON of the last saved draft
const graphBaseHash = {}; // project → _hash of the spec at draft load (optimistic lock)
const graphInherited = {}; // project → { loop → {triggers, steps} } of the `use:` loops (expanded, read-only)
// project → { loop → {triggers, steps, description} }: loops that only come into being on expansion
// (e.g. from the `relay:` block) — read-only, never part of the draft (save doesn't write them).
const graphGenerated = {};
const graphTplParams = {}; // template → params block (null = unknown) — cache for the with: editor
let graphDrill = null;  // { project, loop } — current drill-down layer
let graphCtxTarget = null; // node for which the context menu is open
let graphCtxClose = null;  // document listener that closes the menu
let graphSel = null;     // { kind, id, loop?, index? } — node with an open card

// Trigger type → edge color (literal hex: Cytoscape does not resolve CSS var()).
// Keys = config.TRIGGER_TYPES (manual, cron, on_file, on_issue, on_pr, on_webhook).
const TRIGGER_EDGE_COLORS = {
  manual: "#7d8899", cron: "#d9a441", on_file: "#50b867",
  on_issue: "#9b59b6", on_pr: "#56b6c2", on_webhook: "#e05561",
};
const EDGE_DEFAULT_COLOR = "#56b6c2";
// Display name of a trigger type (i18n `trig.<type>`); unknown → raw type.
const trigLabel = (type) => (type ? tOr(`trig.${type}`, type) : "?");
// Display name of a run/step status (i18n `run.status.<status>`); unknown →
// raw status (closed enum, fallback for old/new statuses).
const statusLabel = (st) => (st ? tOr(`run.status.${st}`, st) : st);

// ---- Draft (in-memory working copy of the spec) ----
// Deep copy of the spec so edits don't touch the fetched spec.
function draftFromSpec(spec) {
  const loops = {};
  for (const [ln, ldef] of Object.entries(spec.loops || {})) {
    if (!ldef || typeof ldef !== "object") continue;
    // Keep ALL keys of the loop definition (worktree, retry, tier,
    // use, with, insert, …) — only triggers/steps get deep-copied. The
    // draft source is the RAW spec (?raw=1): vars stay literal,
    // use:/insert: loops un-inlined. triggers/steps only if the spec has
    // them: an injected `steps: []`/`triggers: []` would overwrite template
    // values in `use:` loops (→ loopList creates them on demand only).
    loops[ln] = { ...ldef };
    for (const key of ["triggers", "steps"]) {
      if (Array.isArray(ldef[key])) loops[ln][key] = ldef[key].map((x) => ({ ...(x || {}) }));
    }
  }
  return {
    description: spec.description || "",
    max_parallel: spec.max_parallel ?? "",
    loops,
  };
}

// Get the steps/triggers list of a draft loop for editing — creates it
// only now, so untouched loops don't write empty lists into the spec.
function loopList(ldef, key) {
  if (!Array.isArray(ldef[key])) ldef[key] = [];
  return ldef[key];
}

// After a removal: drop an empty list of a `use:` loop again — then
// the template values apply again, instead of being overwritten by `[]`.
function pruneEmptyList(ldef, key) {
  if (ldef.use && Array.isArray(ldef[key]) && !ldef[key].length) delete ldef[key];
}

// Own steps editable? `use:` loops inherit their steps from the template; own
// `steps` would replace them entirely (and collide with `insert:`) — there
// only if the raw spec already has own steps.
function loopStepsEditable(ldef) {
  return !ldef.use || Array.isArray(ldef.steps);
}

// Does the draft have unsaved changes? (Comparison with the last save state.)
function draftDirty(name) {
  const d = graphDrafts[name];
  if (!d) return false;
  return JSON.stringify(d) !== (graphSaved[name] ?? "");
}

// Draft → nodes/edges (client-side, so the graph renders from the draft,
// not from a server payload — the graph is the single source of truth).
function draftGraph(name, draft) {
  const nodes = [{ id: `proj:${name}`, kind: "project", label: name }];
  const edges = [];
  const seenTpl = new Set();
  for (const [lname, ldef] of Object.entries(draft.loops || {})) {
    const loopId = `loop:${name}:${lname}`;
    // `use:` loops without own triggers/steps: show the inherited ones
    // (from the expanded spec) — read-only, not part of the draft.
    const inh = ldef.use ? (graphInherited[name] || {})[lname] || {} : {};
    const trigTypes = (Array.isArray(ldef.triggers) ? ldef.triggers : inh.triggers || [])
      .filter((t) => t && t.type).map((t) => String(t.type));
    const stepNames = (ldef.steps || []).map((s, i) => String(s.name || s.type || `step ${i}`));
    nodes.push({ id: loopId, kind: "loop", label: lname, triggers: trigTypes, description: ldef.description || "", steps: stepNames });
    edges.push({ source: `proj:${name}`, target: loopId, kind: "has_loop" });
    (ldef.triggers || []).forEach((t, ti) => {
      const trigId = `trig:${name}:${lname}:${ti}`;
      nodes.push({ id: trigId, kind: "trigger", label: trigLabel(t.type) });
      edges.push({ source: loopId, target: trigId, kind: "has_trigger" });
    });
    if (ldef.use) {
      const tplId = `tpl:${ldef.use}`;
      if (!seenTpl.has(tplId)) { seenTpl.add(tplId); nodes.push({ id: tplId, kind: "template", label: String(ldef.use) }); }
      edges.push({ source: loopId, target: tplId, kind: "uses_template" });
    }
    // Step chain: only the FIRST step hangs off the loop (has_step), the
    // rest follow via next_step — the drill-down shows the execution
    // as a directed chain from the loop root (loop → s1 → s2 → …).
    let prev = null;
    (ldef.steps || []).forEach((s, si) => {
      const stepId = `step:${name}:${lname}:${si}`;
      nodes.push({ id: stepId, kind: "step", label: String(s.name || s.type || `step ${si}`), type: s.type || null, params: Object.fromEntries(Object.entries(s).filter(([k]) => k !== "type")) });
      if (!prev) edges.push({ source: loopId, target: stepId, kind: "has_step" });
      if (prev) edges.push({ source: prev, target: stepId, kind: "next_step" });
      prev = stepId;
    });
    if (!Array.isArray(ldef.steps)) pushReadonlySteps(nodes, edges, name, lname, inh.steps || [], prev);
  }
  // Generated loops (e.g. relay-dispatch/-session/-audit from `relay:`): read-only.
  for (const [lname, gdef] of Object.entries(graphGenerated[name] || {})) {
    if (lname in (draft.loops || {})) continue; // a draft loop of the same name takes precedence
    const loopId = `loop:${name}:${lname}`;
    const trigTypes = gdef.triggers.filter((t) => t && t.type).map((t) => String(t.type));
    const stepNames = gdef.steps.map((s, i) => String(s.name || s.type || `step ${i}`));
    nodes.push({ id: loopId, kind: "gloop", label: lname, triggers: trigTypes, description: gdef.description, steps: stepNames });
    edges.push({ source: `proj:${name}`, target: loopId, kind: "has_loop" });
    pushReadonlySteps(nodes, edges, name, lname, gdef.steps, null);
  }
  return { nodes, edges };
}

// Graph button: toggle view mode (graph ↔ loop list).
function toggleGraph(name) {
  if (state.graphOpen === name) {
    const el = $("#graphcanvas");
    if (el && el._cy) { el._cy.destroy(); el._cy = null; }
    state.graphOpen = null;
    graphDrill = null;
    graphSel = null;
    hideGraphCtx();
    closeGraphCard();
    renderDetail(name);
    return;
  }
  state.graphOpen = name;
  graphDrill = null;
  closeGraphCard();
  renderDetail(name); // whose restore logic calls loadGraph(name)
}

// Load the draft (from the spec) and render it.
async function loadGraph(name) {
  const el = $("#graphcanvas");
  if (!el) return;
  const seq = ++graphRenderSeq;
  if (!graphDrafts[name] || !graphInherited[name]) {
    el.innerHTML = `<div class="graphmsg">${esc(t("graph.loading"))}</div>`;
    try {
      const base = `/api/projects/${encodeURIComponent(name)}/spec`;
      // ?raw=1 → raw (not expanded) spec: draft source with all
      // loop keys, vars literal, use:/insert: un-inlined. _hash for
      // the optimistic lock on save. The expanded spec only provides
      // the (read-only) template steps/triggers of use: loops.
      const [raw, expanded] = await Promise.all([
        graphDrafts[name] ? null : api(`${base}?raw=1`),
        api(base),
      ]);
      if (seq !== graphRenderSeq) return; // project switched
      if (raw) {
        graphDrafts[name] = draftFromSpec(raw);
        graphSaved[name] = JSON.stringify(graphDrafts[name]);
        graphBaseHash[name] = raw._hash || "";
      }
      graphInherited[name] = {};
      const generated = {};
      const rawLoops = raw ? raw.loops || {} : null;
      for (const [ln, ldef] of Object.entries(expanded.loops || {})) {
        if (!ldef || typeof ldef !== "object") continue;
        graphInherited[name][ln] = { triggers: ldef.triggers || [], steps: ldef.steps || [] };
        if (rawLoops && !(ln in rawLoops)) generated[ln] = { ...graphInherited[name][ln], description: ldef.description || "" };
      }
      // Without a fresh raw spec (draft already exists) the known list stays valid.
      if (rawLoops) graphGenerated[name] = generated;
    } catch (e) {
      if (seq === graphRenderSeq) el.innerHTML = `<div class="graphmsg err">${esc(e.message)}</div>`;
      return;
    }
  }
  if (seq !== graphRenderSeq) return; // stale render (project switch)
  renderGraph(name);
}

// Attach a read-only step chain (`tstep`) to a loop: template or generated steps.
function pushReadonlySteps(nodes, edges, name, lname, steps, prev) {
  const loopId = `loop:${name}:${lname}`;
  steps.forEach((s, si) => {
    const stepId = `tstep:${name}:${lname}:${si}`;
    nodes.push({ id: stepId, kind: "tstep", label: String(s.name || s.type || `step ${si}`) });
    if (!prev) edges.push({ source: loopId, target: stepId, kind: "has_step" }); // only the first step hangs off the loop
    if (prev) edges.push({ source: prev, target: stepId, kind: "next_step" });
    prev = stepId;
  });
}

// Loop node (editable or generated)?
function isLoopKind(kind) {
  return kind === "loop" || kind === "gloop";
}

// has_loop edge: label = trigger types of the target loop, color from the first.
function loopEdge(e, byId) {
  const triggers = (byId[e.target] || {}).triggers || [];
  return { ...e, label: triggers.map(trigLabel).join(", "), colorKey: triggers[0] };
}

// Elements of the current layer + ghost nodes:
// Top: project + loops + "+ New Loop";
// Drill-down: project + loop + steps + templates + "+ New Step".
function graphElements(name, g) {
  const byId = {};
  for (const n of g.nodes) byId[n.id] = n;
  if (graphDrill && graphDrill.project === name) {
    const loopId = `loop:${name}:${graphDrill.loop}`;
    const projId = `proj:${name}`;
    // Children exactly via the loop's edges (no ID prefix: loop `a` would
    // otherwise also collect the steps of `a:b`). Only the first step hangs
    // directly off the loop (has_step) — the rest follow via the next_step chain.
    const childIds = new Set(g.edges
      .filter((e) => (e.kind === "uses_template" || e.kind === "has_step") && e.source === loopId)
      .map((e) => e.target));
    for (const e of g.edges) {
      if (e.kind === "next_step" && childIds.has(e.source)) childIds.add(e.target);
    }
    const nodes = g.nodes.filter((n) => n.id === loopId || n.id === projId || childIds.has(n.id));
    const ids = new Set(nodes.map((n) => n.id));
    const edges = g.edges
      .filter((e) => (e.kind === "has_step" || e.kind === "next_step" || e.kind === "uses_template" || (e.kind === "has_loop" && e.target === loopId)) && ids.has(e.source) && ids.has(e.target))
      .map((e) => (e.kind === "has_loop" ? loopEdge(e, byId) : e));
    // Ghost node: append a new step to the chain — not for template loops
    // without own steps (one step would replace all template steps). The
    // ghost hangs at the CHAIN END (new steps are appended to the end),
    // for a loop without steps at the loop itself.
    const ldef = (graphDrafts[name] || { loops: {} }).loops[graphDrill.loop];
    if (ldef && loopStepsEditable(ldef)) {
      const n = (ldef.steps || []).length;
      const lastStepId = n ? `step:${name}:${graphDrill.loop}:${n - 1}` : loopId;
      nodes.push({ id: "ghost:step", kind: "ghost", label: t("graph.ghostStep"), ghost: "step" });
      edges.push({ source: lastStepId, target: "ghost:step", kind: "ghost_edge" });
    }
    return { nodes, edges };
  }
  const nodes = g.nodes.filter((n) => n.kind === "project" || isLoopKind(n.kind));
  const ids = new Set(nodes.map((n) => n.id));
  const edges = g.edges
    .filter((e) => e.kind === "has_loop" && ids.has(e.source) && ids.has(e.target))
    .map((e) => loopEdge(e, byId));
  // Ghost node: create a new loop.
  nodes.push({ id: "ghost:loop", kind: "ghost", label: t("graph.ghostLoop"), ghost: "loop" });
  edges.push({ source: `proj:${name}`, target: "ghost:loop", kind: "ghost_edge" });
  return { nodes, edges };
}

// Cytoscape stylesheet: literal hex (Cytoscape does not resolve CSS var()).
const GRAPH_STYLE = [
  { selector: "node", style: { "label": "data(label)", "color": "#d8dee7", "font-size": 11, "text-valign": "bottom", "text-halign": "center", "text-margin-y": 4, "text-background-color": "#15181d", "text-background-opacity": 0.85, "text-background-padding": 3, "text-wrap": "ellipsis", "text-max-width": "140px" } },
  { selector: 'node[kind="project"]', style: { "background-color": "#4a90d9", "shape": "ellipse", "width": 46, "height": 46, "font-size": 13, "font-weight": "bold" } },
  { selector: 'node[kind="loop"]', style: { "background-color": "#50b867", "shape": "round-rectangle", "width": 36, "height": 36 } },
  // Generated loops (e.g. from `relay:`): read-only, faded + dashed.
  { selector: 'node[kind="gloop"]', style: { "background-color": "#50b867", "background-opacity": 0.35, "border-color": "#50b867", "border-width": 1.5, "border-style": "dashed", "shape": "round-rectangle", "width": 36, "height": 36 } },
  { selector: 'node[kind="step"]', style: { "background-color": "#e0a030", "shape": "rectangle", "width": 28, "height": 28, "font-size": 10 } },
  { selector: 'node[kind="trigger"]', style: { "background-color": "#9b59b6", "shape": "diamond", "width": 20, "height": 20, "font-size": 9 } },
  { selector: 'node[kind="template"]', style: { "background-color": "#e05561", "shape": "hexagon", "width": 30, "height": 30 } },
  // Steps inherited from the template (read-only): faded + dashed.
  { selector: 'node[kind="tstep"]', style: { "background-color": "#e0a030", "background-opacity": 0.35, "border-color": "#e0a030", "border-width": 1.5, "border-style": "dashed", "shape": "rectangle", "width": 28, "height": 28, "font-size": 10, "color": "#7d8899" } },
  { selector: 'node[kind="ghost"]', style: { "background-opacity": 0, "border-color": "#5a6472", "border-width": 1.5, "border-style": "dashed", "color": "#7d8899", "shape": "round-rectangle", "width": 40, "height": 30, "font-size": 10, "cursor": "pointer" } },
  { selector: "node:selected", style: { "border-color": "#ffb454", "border-width": 2.5 } },
  { selector: "edge", style: { "width": 1.5, "line-color": "#3a4250", "curve-style": "straight", "label": "data(label)", "font-size": 9, "color": "#7d8899", "text-background-color": "#15181d", "text-background-opacity": 0.85, "text-background-padding": 2 } },
  { selector: 'edge[kind="has_loop"]', style: { "width": 2 } },
  // Step chain (has_step = entry at the loop, next_step = chain): both
  // with arrows so the execution direction is visible (loop → s1 → s2 → …).
  { selector: 'edge[kind="has_step"]', style: { "line-color": "#e0a030", "target-arrow-shape": "triangle", "target-arrow-color": "#e0a030", "arrow-scale": 0.7 } },
  { selector: 'edge[kind="next_step"]', style: { "line-color": "#e0a030", "target-arrow-shape": "triangle", "target-arrow-color": "#e0a030", "arrow-scale": 0.7 } },
  { selector: 'edge[kind="uses_template"]', style: { "line-color": "#e05561", "line-style": "dashed" } },
  { selector: 'edge[kind="ghost_edge"]', style: { "line-color": "#5a6472", "line-style": "dashed", "width": 1 } },
];

function renderGraph(name) {
  const el = $("#graphcanvas");
  if (!el) return;
  const draft = graphDrafts[name];
  if (!draft) return;
  if (el._cy) { el._cy.destroy(); el._cy = null; }
  el.innerHTML = "";
  if (!window.cytoscape) { el.innerHTML = `<div class="graphmsg err">${esc(t("graph.cytoscapeMissing"))}</div>`; return; }
  const g = draftGraph(name, draft);
  const { nodes, edges } = graphElements(name, g);
  if (!nodes.length) { el.innerHTML = `<div class="graphmsg">${esc(t("graph.empty"))}</div>`; return; }
  const els = [
    ...nodes.map((n) => ({
      data: { id: n.id, label: n.label, kind: n.kind, type: n.type || null, ghost: n.ghost || null },
    })),
    ...edges.map((e) => ({
      data: { id: `${e.source}→${e.target}:${e.kind}`, source: e.source, target: e.target, kind: e.kind, label: e.label || "" },
      // Color the has_loop edge after the loop's (first) trigger type (raw type as key).
      style: e.kind === "has_loop" ? { "line-color": TRIGGER_EDGE_COLORS[e.colorKey] || EDGE_DEFAULT_COLOR } : undefined,
    })),
  ];
  const cy = cytoscape({
    container: el,
    elements: els,
    style: GRAPH_STYLE,
    layout: { name: "breadthfirst", directed: true, padding: 16, spacingFactor: 1.15 },
    minZoom: 0.2,
  });
  el._cy = cy;
  wireGraphEvents(cy, name);
  updateGraphDirtyBadge(name);
  // Re-position the open card after the (re-)render.
  if (graphSel && graphSel.project === name) positionGraphCard(name);
}

// Loop name from a loop node ID (`loop:<project>:<loop>`) — exact by
// prefix, since project/loop names may themselves contain ":".
function loopNameFromId(name, id) {
  return id.slice(`loop:${name}:`.length);
}

// Step node ID (`step:<project>:<loop>:<i>`) → { loop, index }.
function stepInfoFromId(name, id) {
  const rest = id.slice(id.indexOf(":") + 1 + name.length + 1); // `step:` or `tstep:`
  const i = rest.lastIndexOf(":");
  return { loop: rest.slice(0, i), index: parseInt(rest.slice(i + 1), 10) };
}

// Click: loop → drill-down; step/loop/project → card (details+edit);
// Ghost → create node; project (in drill-down) → back. Right-click: menu.
function wireGraphEvents(cy, name) {
  cy.on("tap", "node", (ev) => {
    hideGraphCtx();
    const n = ev.target;
    const kind = n.data("kind");
    const id = n.data("id");
    if (kind === "ghost") {
      graphGhostAdd(name, n.data("ghost"));
      return;
    }
    if (isLoopKind(kind)) {
      // Loop click: drill-down only (the card would cover the navigation).
      // Edit a loop = right-click → "✎ edit".
      graphDrill = { project: name, loop: loopNameFromId(name, id) };
      graphSel = null;
      closeGraphCard();
      renderGraph(name);
      return;
    }
    if (kind === "project") {
      if (graphDrill) { graphDrill = null; graphSel = null; closeGraphCard(); renderGraph(name); return; }
      graphSel = { project: name, kind: "project", id };
      openGraphCard(name);
      return;
    }
    if (kind === "step" || kind === "tstep") {
      const info = stepInfoFromId(name, id);
      graphSel = { project: name, kind, id, loop: info.loop, index: info.index };
      openGraphCard(name);
    }
  });
  // cxttap = right-click in Cytoscape 3.30.x (there is no "cxtadd" event).
  cy.on("cxttap", "node", (ev) => {
    ev.originalEvent.preventDefault();
    showGraphCtx(ev.originalEvent.clientX, ev.originalEvent.clientY, ev.target);
  });
  // Card follows the node on pan/zoom.
  cy.on("pan zoom", () => { if (graphSel && graphSel.project === name) positionGraphCard(name); });
}

// Ghost node clicked → put a default node into the draft (dry-run) + open the card.
function graphGhostAdd(name, ghost) {
  const draft = graphDrafts[name];
  if (!draft) return;
  if (ghost === "loop") {
    let ln = "new-loop";
    let i = 2;
    while (draft.loops[ln]) ln = `new-loop-${i++}`;
    draft.loops[ln] = { description: "", triggers: [{ type: "manual" }], steps: [{ type: "shell", name: "new-step", cmd: "echo TODO" }] };
    graphSel = { project: name, kind: "loop", id: `loop:${name}:${ln}`, loop: ln };
    renderGraph(name);
    openGraphCard(name);
  } else if (ghost === "step") {
    if (!graphDrill || graphDrill.project !== name) return;
    const ldef = draft.loops[graphDrill.loop];
    if (!ldef || !loopStepsEditable(ldef)) return;
    const steps = loopList(ldef, "steps");
    steps.push({ type: "shell", name: "new-step", cmd: "echo TODO" });
    const idx = steps.length - 1;
    graphSel = { project: name, kind: "step", id: `step:${name}:${graphDrill.loop}:${idx}`, loop: graphDrill.loop, index: idx };
    renderGraph(name);
    openGraphCard(name);
  }
}

// ---- Floating node card (#graphnode) — details + edit IN the graph ----
// Absolute card in #graphview, anchored at the clicked node (renderedPosition),
// follows on pan/zoom. Edits the draft (dry-run); save writes the spec.
function cardHead(title) {
  // `title` is already HTML (callers escape dynamic values themselves), e.g.
  // the step card with <span class="dim">…</span> — do NOT escape here.
  return `<div class="graphnode-head"><b>${title}</b><button class="ghost" title="${esc(t("graph.cardClose"))}" data-act="close-card">✕</button></div>`;
}

function cardFoot(name) {
  return `<div class="graphnode-foot">
    <span class="graphnode-dirty" id="graphnode-dirty"></span>
    <span class="graphnode-err" id="graphnode-err"></span>
    <button class="ghost" title="${esc(t("graph.discardTitle"))}" data-act="graph-discard">${esc(t("graph.discard"))}</button>
    <button class="run" data-act="graph-save">${esc(t("graph.save"))}</button>
  </div>`;
}

function openGraphCard(name) {
  renderGraphCard(name);
  positionGraphCard(name);
}

function closeGraphCard() {
  const el = $("#graphnode");
  if (el) { el.classList.add("hidden"); el.innerHTML = ""; }
  graphSel = null;
  if (state.selected) updateGraphDirtyBadge(state.selected); // show the save bar again if needed
}

// Loop definition that the card edits (step/loop card) — from draft + graphSel.
function graphCardLoop(name) {
  const draft = graphDrafts[name];
  if (!draft || !graphSel) return null;
  return draft.loops[graphSel.loop] || null;
}

function renderGraphCard(name) {
  const el = $("#graphnode");
  if (!el || !graphSel) return;
  const draft = graphDrafts[name];
  if (!draft) return;
  let html = "";
  if (graphSel.kind === "project") {
    html = cardHead(`✎ ${esc(name)}`) + `
      <div class="graphnode-body">
        <div class="ge-field"><label>${esc(t("graph.field.description"))}</label><input data-gn="description" value="${esc(draft.description)}"></div>
        <div class="ge-field"><label>${esc(t("graph.field.max_parallel"))}</label><input type="number" min="1" data-gn="max_parallel" value="${esc(draft.max_parallel)}"></div>
      </div>` + cardFoot(name);
  } else if (graphSel.kind === "loop") {
    const ldef = graphCardLoop(name);
    if (!ldef) { closeGraphCard(); return; }
    const trigs = (ldef.triggers || []).map((t, i) => geTriggerRow(t, i)).join("");
    const steps = (ldef.steps || []).map((s, i) => geStepCard(s, i)).join("");
    // Template loops (`use:`): show inherited triggers/steps as a hint only.
    const inh = (graphInherited[name] || {})[graphSel.loop] || {};
    const tpl = esc(ldef.use || "");
    const inhList = (xs, f) => xs.map((x) => esc(String(f(x)))).join(", ") || "—";
    const trigsEmpty = ldef.use && !Array.isArray(ldef.triggers)
      ? `<div class="ge-empty">${t("graph.inheritedTriggers", { tpl, list: inhList(inh.triggers || [], (tr) => trigLabel(tr.type)) })}</div>`
      : `<div class="ge-empty">${esc(t("graph.noTriggers"))}</div>`;
    const stepsBlock = loopStepsEditable(ldef)
      ? `<div id="ge-steps">${steps || `<div class="ge-empty">${esc(t("graph.noSteps"))}</div>`}</div>
        <button class="ghost" data-act="card-add-step">${esc(t("graph.addStep"))}</button>`
      : `<div id="ge-steps"><div class="ge-empty">${t("graph.inheritedSteps", { tpl, list: inhList(inh.steps || [], (s) => s.name || s.type || "step") })}</div></div>`;
    // Template loops (`use:`): with: parameters editable (fields are
    // loaded asynchronously from /api/templates/<tpl>/params → renderGraphWithFields).
    const withBlock = ldef.use
      ? `<div class="ge-sec">${esc(t("graph.withSection", { tpl: ldef.use }))}</div>
        <div id="ge-with">${esc(t("graph.withLoading"))}</div>`
      : "";
    html = cardHead(`✎ ${esc(graphSel.loop)}`) + `
      <div class="graphnode-body">
        <div class="ge-field"><label>${esc(t("graph.field.name"))}</label><input data-gn="name" value="${esc(graphSel.name || graphSel.loop)}"></div>
        <div class="ge-field"><label>${esc(t("graph.field.description"))}</label><input data-gn="description" value="${esc(ldef.description || "")}"></div>
        ${withBlock}
        <div class="ge-sec">${esc(t("graph.triggers"))}</div>
        <div id="ge-triggers">${trigs || trigsEmpty}</div>
        <button class="ghost" data-act="card-add-trigger">${esc(t("graph.addTrigger"))}</button>
        <div class="ge-sec">${esc(t("graph.steps"))}</div>
        ${stepsBlock}
        <div class="ge-step-actions" style="margin-top:8px">
          <button class="ghost" title="${esc(t("graph.removeLoopTitle"))}" data-act="card-remove-loop">${esc(t("graph.removeLoop"))}</button>
        </div>
      </div>` + cardFoot(name);
  } else if (graphSel.kind === "gloop") {
    html = generatedLoopCard(name, graphSel.loop);
    if (!html) { closeGraphCard(); return; }
  } else if (graphSel.kind === "tstep") {
    html = readonlyStepCard(name, graphSel.loop, graphSel.index);
    if (!html) { closeGraphCard(); return; }
  } else if (graphSel.kind === "step") {
    const ldef = graphCardLoop(name);
    const s = ldef && (ldef.steps || [])[graphSel.index];
    if (!s) { closeGraphCard(); return; }
    const type = s.type || "shell";
    const fields = geSchemaFor("step", type).map((d) => geFieldHtml("step", type, d, s, graphSel.index)).join("");
    const extra = geExtraHtml("step", type, s, graphSel.index);
    html = cardHead(`✎ ${esc(s.name || s.type || "step")} <span class="dim">(${graphSel.index + 1}/${ldef.steps.length} · ${esc(graphSel.loop)})</span>`) + `
      <div class="graphnode-body">
        <div class="ge-field"><label>${esc(t("graph.field.type"))}</label><select data-ge="type" data-ge-kind="step" data-ge-idx="${graphSel.index}">${GE_STEP_TYPES.map((t) => `<option value="${esc(t)}"${t === type ? " selected" : ""}>${esc(t)}</option>`).join("")}</select></div>
        <div class="ge-field"><label>${esc(t("graph.field.name"))}</label><input data-ge="name" data-ge-kind="step" data-ge-idx="${graphSel.index}" value="${esc(s.name || "")}"></div>
        ${fields}${extra}
        <div class="ge-step-actions" style="margin-top:8px">
          <button class="ghost" title="${esc(t("graph.moveUp"))}" data-act="card-move-step" data-dir="-1">↑</button>
          <button class="ghost" title="${esc(t("graph.moveDown"))}" data-act="card-move-step" data-dir="1">↓</button>
          <button class="ghost" title="${esc(t("graph.remove"))}" data-act="card-remove-step">${esc(t("graph.remove"))}</button>
        </div>
      </div>` + cardFoot(name);
  }
  el.innerHTML = html;
  el.classList.remove("hidden");
  updateGraphDirtyBadge(name);
  // Load the with: fields of the `use:` loop asynchronously (template params).
  if (graphSel.kind === "loop") {
    const ldef = graphCardLoop(name);
    if (ldef && ldef.use) renderGraphWithFields(name);
  }
}

// with: values ↔ input text: edited as JSON literals (3, true, [1,2]);
// anything that is not JSON stays a string.
function withToText(v) {
  return typeof v === "string" ? v : JSON.stringify(v);
}
function withFromText(text) {
  const s = text.trim();
  if (s === "") return undefined;
  try {
    return JSON.parse(s);
  } catch (e) {
    return text;
  }
}

// Load the template parameters (with:) of a `use:` loop (cached) and render them
// as edit fields in the loop card. Default = template default;
// a `null` default = required field (marked with *).
async function renderGraphWithFields(name) {
  const ldef = graphCardLoop(name);
  if (!ldef || !ldef.use) return;
  const tpl = ldef.use;
  // Cache per project+template (project-local templates take precedence);
  // errors (null = with: not editable) are not cached.
  const ckey = `${name}/${tpl}`;
  let params = graphTplParams[ckey];
  if (params === undefined) {
    try {
      const data = await api(`/api/templates/${encodeURIComponent(tpl)}/params?project=${encodeURIComponent(name)}`);
      params = graphTplParams[ckey] = data.params || {};
    } catch (e) {
      params = null;
    }
  }
  // Card closed/changed in the meantime → don't render anymore.
  const cur = graphCardLoop(name);
  const box = $("#ge-with");
  if (!cur || cur.use !== tpl || !box) return;
  if (params === null) {
    box.innerHTML = `<div class="ge-empty">${esc(t("graph.withUnknown", { tpl }))}</div>`;
    return;
  }
  const keys = Object.keys(params);
  if (!keys.length) {
    box.innerHTML = `<div class="ge-empty">${esc(t("graph.withNone"))}</div>`;
    return;
  }
  const withVals = cur.with || {};
  box.innerHTML = keys
    .map((k) => {
      const val = withVals[k] !== undefined ? withVals[k] : params[k] ?? "";
      const req = params[k] === null ? " *" : "";
      return `<div class="ge-field"><label>${esc(k)}${req}</label><input data-gw="${esc(k)}" value="${esc(withToText(val))}"></div>`;
    })
    .join("");
}

// ---- Read-only cards: generated loops (e.g. `relay:`) and their/template steps ----
// Value for display: strings literal (prompts, cmd), everything else as JSON.
function roValue(v) {
  return typeof v === "string" ? v : JSON.stringify(v, null, 2);
}

function roFields(obj, skip) {
  return Object.entries(obj || {})
    .filter(([k]) => !skip.includes(k))
    .map(([k, v]) => `<div class="ge-field"><label>${esc(k)}</label><pre class="ge-ro">${esc(roValue(v))}</pre></div>`)
    .join("");
}

function roSourceNote(name, loop) {
  const generated = loop in (graphGenerated[name] || {});
  const use = ((graphDrafts[name] || { loops: {} }).loops[loop] || {}).use;
  const src = generated ? t("graph.roGenerated") : t("graph.roTemplate", { use: `<b>${esc(use || "?")}</b>` });
  return `<div class="ge-empty">${t("graph.roNote", { src })}</div>`;
}

function generatedLoopCard(name, loop) {
  const gdef = (graphGenerated[name] || {})[loop];
  if (!gdef) return "";
  const trigs = gdef.triggers.map((t) => `<div class="ge-field"><label>${esc(t.type || "?")}</label><pre class="ge-ro">${esc(roValue(Object.fromEntries(Object.entries(t).filter(([k]) => k !== "type"))))}</pre></div>`).join("");
  const steps = gdef.steps.map((st, i) => `<div class="ge-field"><label>${i + 1}. ${esc(st.name || st.type || "step")}</label><pre class="ge-ro">${esc(st.type || "")}</pre></div>`).join("");
  return cardHead(`ⓘ ${esc(loop)}`) + `
    <div class="graphnode-body">
      ${roSourceNote(name, loop)}
      ${gdef.description ? `<div class="ge-field"><label>description</label><pre class="ge-ro">${esc(gdef.description)}</pre></div>` : ""}
      <div class="ge-sec">Triggers</div>${trigs || '<div class="ge-empty">keine Triggers</div>'}
      <div class="ge-sec">Steps</div>${steps || '<div class="ge-empty">keine Steps</div>'}
    </div>`;
}

function readonlyStepCard(name, loop, index) {
  const steps = ((graphInherited[name] || {})[loop] || {}).steps || [];
  const st = steps[index];
  if (!st) return "";
  return cardHead(`ⓘ ${esc(st.name || st.type || "step")} <span class="dim">(${index + 1}/${steps.length} · ${esc(loop)})</span>`) + `
    <div class="graphnode-body">
      ${roSourceNote(name, loop)}
      <div class="ge-field"><label>type</label><pre class="ge-ro">${esc(st.type || "shell")}</pre></div>
      ${roFields(st, ["name", "type"])}
    </div>`;
}

// Anchor the card at the node (renderedPosition → canvas coordinates).
function positionGraphCard(name) {
  const el = $("#graphnode");
  const view = $("#graphview");
  const canvas = $("#graphcanvas");
  const cy = canvas && canvas._cy;
  if (!el || !cy || !graphSel) return;
  const node = cy.getElementById(graphSel.id);
  if (!node || node.removed()) return;
  const p = node.renderedPosition();
  const w = el.offsetWidth || 320;
  const h = el.offsetHeight || 200;
  const vw = view.clientWidth;
  const vh = view.clientHeight;
  // Right of the node; on overflow mirror to the left/top.
  let x = p.x + 24;
  let y = p.y - 12;
  if (x + w > vw - 8) x = p.x - w - 24;
  if (x < 8) x = 8;
  if (y + h > vh - 8) y = vh - h - 8;
  if (y < 8) y = 8;
  el.style.left = `${x}px`;
  el.style.top = `${y}px`;
}

// Update the dirty badge in the card footer.
// Show the dirty state: badge in the card resp. save bar in the graph. The
// bar appears only with a closed card (that one has its own save/discard
// in the footer; so the two never overlap) — otherwise after
// "✕ remove loop" (which closes the card) there would be no way left to save.
function updateGraphDirtyBadge(name) {
  const dirty = draftDirty(name);
  const el = $("#graphnode-dirty");
  if (el) el.textContent = dirty ? t("graph.dirtyShort") : "";
  const card = $("#graphnode");
  const cardOpen = card && !card.classList.contains("hidden");
  const bar = $("#graphbar");
  if (bar) bar.classList.toggle("hidden", !dirty || cardOpen);
}

// Show draft-editor errors in the card AND the save bar ("" = clear).
function graphError(msg) {
  for (const id of ["#graphnode-err", "#graphbar-err"]) {
    const el = $(id);
    if (el) el.textContent = msg;
  }
}

// Delegated input handler of the card: field change → draft update (dry-run).
function graphNodeInput(e) {
  const el = e.target;
  const name = state.selected;
  const draft = graphDrafts[name];
  if (!draft || !graphSel) return;
  graphError("");
  // Project fields (description / max_parallel).
  if (el.dataset.gn !== undefined) {
    if (graphSel.kind === "project") {
      if (el.dataset.gn === "description") draft.description = el.value;
      else if (el.dataset.gn === "max_parallel") draft.max_parallel = el.value === "" ? "" : parseInt(el.value, 10);
    } else if (graphSel.kind === "loop") {
      const ldef = graphCardLoop(name);
      if (!ldef) return;
      if (el.dataset.gn === "description") ldef.description = el.value;
      // Name: only remember it (graphSel.name); the draft key is only renamed on save
      // (no re-render per keystroke → focus/cursor stays).
      else if (el.dataset.gn === "name") graphSel.name = el.value.trim();
    }
    updateGraphDirtyBadge(name);
    return;
  }
  // Template parameters (with:) of a `use:` loop (data-gw).
  if (el.dataset.gw !== undefined) {
    const ldef = graphCardLoop(name);
    if (!ldef) return;
    const key = el.dataset.gw;
    ldef.with = ldef.with || {};
    const parsed = withFromText(el.value);
    // Empty → remove the key (template default applies / required field stays missing).
    if (parsed === undefined) delete ldef.with[key];
    else ldef.with[key] = parsed;
    if (!Object.keys(ldef.with).length) delete ldef.with;
    updateGraphDirtyBadge(name);
    return;
  }
  // Step/trigger fields (data-ge).
  if (el.dataset.ge === undefined) return;
  const loop = graphCardLoop(name);
  if (!loop) return;
  const key = el.dataset.ge;
  const kind = el.dataset.geKind;
  const idx = parseInt(el.dataset.geIdx, 10);
  const arr = (kind === "step" ? loop.steps : loop.triggers) || [];
  const obj = arr[idx];
  if (!obj) return;
  // Type switch (step/trigger): the schema changes → re-render (values stay).
  if (key === "type") {
    obj.type = el.value;
    renderGraphCard(name);
    return;
  }
  if (key === "name") { obj.name = el.value; updateGraphDirtyBadge(name); return; }
  if (key === "__extra__") {
    const txt = el.value.trim();
    if (txt === "") return;
    let parsed;
    try { parsed = JSON.parse(txt); } catch (err) { graphError(t("graph.extraError", { error: err.message })); return; }
    if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) { graphError(t("graph.extraNotObject")); return; }
    const schemaKeys = new Set(geSchemaFor(kind, obj.type).map((d) => d[0]));
    for (const k of Object.keys(obj)) if (!schemaKeys.has(k) && k !== "type" && k !== "name") delete obj[k];
    Object.assign(obj, parsed);
    updateGraphDirtyBadge(name);
    return;
  }
  const schema = geSchemaFor(kind, obj.type);
  const def = schema.find((d) => d[0] === key);
  if (!def) return;
  try {
    geSetField(obj, key, def, geReadField(el, def));
    updateGraphDirtyBadge(name);
  } catch (err) {
    graphError(t("graph.fieldError", { key, error: err.message }));
  }
}

// ---- Structural actions of the card (add/move/remove) — edit the draft ----
function graphCardAddStep() {
  const name = state.selected;
  const ldef = graphCardLoop(name);
  if (!ldef || !loopStepsEditable(ldef)) return;
  loopList(ldef, "steps").push({ type: "shell", name: "new-step", cmd: "echo TODO" });
  renderGraphCard(name);
  renderGraph(name);
}

function graphCardAddTrigger() {
  const name = state.selected;
  const ldef = graphCardLoop(name);
  if (!ldef) return;
  loopList(ldef, "triggers").push({ type: "manual" });
  renderGraphCard(name);
  renderGraph(name);
}

function graphCardMoveStep(dir) {
  const name = state.selected;
  const ldef = graphCardLoop(name);
  if (!ldef) return;
  const i = graphSel.index;
  const j = i + dir;
  if (!Array.isArray(ldef.steps) || j < 0 || j >= ldef.steps.length) return;
  const tmp = ldef.steps[i];
  ldef.steps[i] = ldef.steps[j];
  ldef.steps[j] = tmp;
  graphSel.index = j;
  graphSel.id = `step:${name}:${graphSel.loop}:${j}`;
  renderGraphCard(name);
  renderGraph(name);
}

function graphCardRemoveStep() {
  const name = state.selected;
  const ldef = graphCardLoop(name);
  if (!ldef || !Array.isArray(ldef.steps)) return;
  ldef.steps.splice(graphSel.index, 1);
  pruneEmptyList(ldef, "steps");
  closeGraphCard();
  renderGraph(name);
}

// Remove a loop from the draft (dry-run — only effective with save, discard
// brings it back). The drill-down into the loop is reset along with it.
function graphCardRemoveLoop() {
  const name = state.selected;
  const draft = graphDrafts[name];
  if (!draft || !graphSel || graphSel.kind !== "loop") return;
  delete draft.loops[graphSel.loop];
  if (graphDrill && graphDrill.project === name && graphDrill.loop === graphSel.loop) graphDrill = null;
  closeGraphCard();
  renderGraph(name);
}

// ---- Save / discard (the single commit point of the draft editor) ----
// Diff draft vs. last save state → only changed loops + deleted names +
// changed top-level fields. This way a save doesn't overwrite foreign,
// interim changes to other loops (in addition to the optimistic lock via
// base_hash), and untouched fields stay out of the payload.
function draftDelta(name) {
  const draft = graphDrafts[name];
  const saved = graphSaved[name] ? JSON.parse(graphSaved[name]) : { loops: {}, description: "", max_parallel: "" };
  const loops = {};
  const remove_loops = [];
  for (const [ln, ldef] of Object.entries(draft.loops)) {
    if (JSON.stringify(ldef) !== JSON.stringify(saved.loops[ln])) loops[ln] = ldef;
  }
  for (const ln of Object.keys(saved.loops)) {
    if (!(ln in draft.loops)) remove_loops.push(ln);
  }
  const delta = { loops, remove_loops };
  if (draft.description !== (saved.description ?? "")) delta.description = draft.description;
  if (draft.max_parallel !== (saved.max_parallel ?? "")) delta.max_parallel = draft.max_parallel;
  return delta;
}

// Save: PUT the changed loops + deleted names + top-level fields as a spec update
// (base_hash = optimistic lock). The server merges per loop.
async function graphDraftSave() {
  const name = state.selected;
  const draft = graphDrafts[name];
  if (!draft) return;
  graphError("");
  // Rename a loop (name field of the loop card) → switch the draft key only now.
  if (graphSel && graphSel.kind === "loop" && graphSel.name && graphSel.name !== graphSel.loop) {
    const nn = graphSel.name;
    if (draft.loops[nn]) { graphError(t("graph.loopExists", { name: nn })); return; }
    draft.loops[nn] = draft.loops[graphSel.loop];
    delete draft.loops[graphSel.loop];
    const inh = graphInherited[name];
    if (inh && inh[graphSel.loop]) { inh[nn] = inh[graphSel.loop]; delete inh[graphSel.loop]; }
    graphSel.loop = nn;
    graphSel.id = `loop:${name}:${nn}`;
    if (graphDrill && graphDrill.project === name) graphDrill.loop = nn;
  }
  // Validation: every loop needs at least one step — except template loops
  // (`use:`) without own steps, whose steps come from the template.
  for (const [ln, ldef] of Object.entries(draft.loops)) {
    if (loopStepsEditable(ldef) && !(ldef.steps || []).length) { graphError(t("graph.loopNeedsStep", { name: ln })); return; }
  }
  const delta = draftDelta(name);
  const body = { loops: delta.loops };
  // Send top-level fields only if they changed — otherwise e.g.
  // `description: ''` would be written into specs that had no description.
  if (delta.description !== undefined) body.description = delta.description;
  if (delta.max_parallel !== "" && delta.max_parallel != null) body.max_parallel = delta.max_parallel;
  if (delta.remove_loops.length) body.remove_loops = delta.remove_loops;
  if (graphBaseHash[name]) body.base_hash = graphBaseHash[name];
  try {
    const resp = await api(`/api/projects/${encodeURIComponent(name)}/spec`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    graphBaseHash[name] = (resp.spec && resp.spec._hash) || graphBaseHash[name];
  } catch (e) {
    if (e.status === 409) {
      // External change since draft load → draft outdated, reload it.
      delete graphDrafts[name];
      graphError(t("graph.specChanged"));
      await loadGraph(name);
    } else {
      graphError(e.message);
    }
    return;
  }
  graphSaved[name] = JSON.stringify(draft);
  delete graphInherited[name]; // reload the template expansion (loadGraph on re-render)
  // Invalidate all caches of the project (loopSpecCache is keyed per project/loop,
  // specCache per project) — the old no-op `name + "/"` never hit.
  for (const k of Object.keys(loopSpecCache)) if (k.startsWith(name + "/")) delete loopSpecCache[k];
  delete specCache[name];
  // refresh(force) re-renders the detail view; renderDetail calls loadGraph.
  await refresh({ force: true });
  closeGraphCard();
}

// Discard: reset the draft to the last save state.
function graphDraftDiscard() {
  const name = state.selected;
  if (graphSaved[name] == null) return;
  graphDrafts[name] = JSON.parse(graphSaved[name]);
  if (graphSel) graphSel.name = undefined; // discard a pending loop rename as well
  renderGraphCard(name);
  renderGraph(name);
}

// Context menu (right-click): run + stateButtons + "✎ edit" (opens the card).
// State buttons in the menu close it before the status switch (onGraphCtxClick).
function showGraphCtx(x, y, node) {
  const menu = $("#graphctx");
  const name = state.selected;
  const p = state.projects.find((x) => x.name === name);
  if (!menu || !p) return;
  const kind = node.data("kind");
  const id = node.data("id");
  let html = "";
  if (isLoopKind(kind)) {
    const loop = loopNameFromId(name, id);
    const l = (p.loops || []).find((x) => x.name === loop);
    html = (l && l.effective_state === "disabled"
      ? `<button class="ghost" disabled title="${esc(t("graph.ctxRunDisabled"))}">${esc(t("detail.run"))}</button>`
      : `<button class="ghost" data-act="ctx-run">${esc(t("detail.run"))}</button>`)
      + stateButtons(name, loop, l && l.state)
      // Generated loops: details only (the source is e.g. the `relay:` block in the YAML).
      + (kind === "loop"
        ? `<button class="ghost" data-act="ctx-edit">${esc(t("graph.edit"))}</button>`
        : `<button class="ghost" data-act="ctx-edit">${esc(t("graph.details"))}</button>`);
  } else if (kind === "tstep") {
    html = `<button class="ghost" data-act="ctx-edit">${esc(t("graph.details"))}</button>`;
  } else if (kind === "step") {
    html = `<button class="ghost" data-act="ctx-edit">${esc(t("graph.edit"))}</button>`;
  } else if (kind === "project") {
    html = stateButtons(name, null, p.state) + `<button class="ghost" data-act="ctx-edit">${esc(t("graph.edit"))}</button>`;
  }
  if (!html) { hideGraphCtx(); return; }
  graphCtxTarget = { kind, id };
  menu.innerHTML = html;
  menu.classList.remove("hidden");
  const r = menu.getBoundingClientRect();
  menu.style.left = `${Math.max(4, Math.min(x, innerWidth - r.width - 8))}px`;
  menu.style.top = `${Math.max(4, Math.min(y, innerHeight - r.height - 8))}px`;
  if (graphCtxClose) document.removeEventListener("pointerdown", graphCtxClose);
  graphCtxClose = (e) => { if (!menu.contains(e.target)) hideGraphCtx(); };
  document.addEventListener("pointerdown", graphCtxClose);
}

function hideGraphCtx() {
  const menu = $("#graphctx");
  if (menu) menu.classList.add("hidden");
  if (graphCtxClose) { document.removeEventListener("pointerdown", graphCtxClose); graphCtxClose = null; }
  graphCtxTarget = null;
}

function graphCtxRun() {
  const t = graphCtxTarget;
  hideGraphCtx();
  if (t && isLoopKind(t.kind)) startRun(state.selected, loopNameFromId(state.selected, t.id));
}

// "✎ edit" → open the floating card for the node.
function graphCtxEdit() {
  const t = graphCtxTarget;
  hideGraphCtx();
  if (!t) return;
  const name = state.selected;
  if (isLoopKind(t.kind)) {
    graphSel = { project: name, kind: t.kind, id: t.id, loop: loopNameFromId(name, t.id) };
    openGraphCard(name);
  } else if (t.kind === "step" || t.kind === "tstep") {
    const info = stepInfoFromId(name, t.id);
    graphSel = { project: name, kind: t.kind, id: t.id, loop: info.loop, index: info.index };
    openGraphCard(name);
  } else if (t.kind === "project") {
    graphSel = { project: name, kind: "project", id: t.id };
    openGraphCard(name);
  }
}

// ---- Form schema (step/trigger type → fields) — as in the YAML spec ----
// Field definition: [key, label, type, opts?]. Types:
//   text  single line · area  multi line · num  integer · float  decimal
//   bool  checkbox · sel   select (opts.options) · list  comma list
//   json  JSON-Textarea
// Absence = field not set (deleted on save). bool with
// opts.default: checkbox state = value (or default); to default → deleted.
const GE_FIELD = { text: "text", area: "area", num: "num", float: "float", bool: "bool", sel: "sel", list: "list", json: "json" };
const GE_STEP_TYPES = ["shell", "git", "pr", "agent", "report", "http", "llm", "notify", "state", "verify", "review", "pr_review", "pr_merge", "issue", "human_gate"];
const GE_TRIGGER_TYPES = ["manual", "cron", "on_file", "on_issue", "on_pr", "on_webhook"];

// reconcile (on_pr): true/false/"head" — as a select with conversion.
const GE_RECONCILE_TO_STORED = (v) => (v === "" ? undefined : v === "true" ? true : v === "false" ? false : v);
const GE_RECONCILE_TO_DISPLAY = (v) => (v === true ? "true" : v === false ? "false" : v == null ? "" : String(v));

const GE_STEP_SCHEMAS = {
  shell: [
    ["cmd", "cmd", GE_FIELD.area, { required: true }],
    ["workdir", "workdir", GE_FIELD.text],
    ["env", "env (JSON: K → V)", GE_FIELD.json],
    ["timeout", "timeout (s)", GE_FIELD.num],
    ["failure_kind", "failure_kind", GE_FIELD.sel, { options: ["transient", "environment", "outcome", "interrupted"] }],
  ],
  git: [
    ["fetch", "fetch", GE_FIELD.bool],
    ["branch", "branch", GE_FIELD.text],
    ["track", "track", GE_FIELD.bool, { default: true }],
    ["commit", "commit (JSON: {add, message})", GE_FIELD.json],
    ["push", "push", GE_FIELD.bool],
    ["protected", "protected (Liste)", GE_FIELD.list],
  ],
  pr: [
    ["repo", "repo (owner/name)", GE_FIELD.text, { required: true }],
    ["base", "base", GE_FIELD.text],
    ["head", "head", GE_FIELD.text],
    ["title", "title", GE_FIELD.text],
    ["body", "body", GE_FIELD.area],
    ["labels", "labels (Liste)", GE_FIELD.list],
  ],
  agent: [
    ["prompt", "prompt", GE_FIELD.area],
    ["prompt_file", "prompt_file", GE_FIELD.text],
    ["backend", "backend", GE_FIELD.text],
    ["model", "model", GE_FIELD.text],
    ["endpoint", "endpoint", GE_FIELD.text],
    ["github_identity", "github_identity", GE_FIELD.text],
    ["workdir", "workdir", GE_FIELD.text],
    ["timeout", "timeout (s)", GE_FIELD.num],
    ["retries", "retries", GE_FIELD.num],
    ["permissions", "permissions (JSON)", GE_FIELD.json],
  ],
  report: [
    ["text", "text", GE_FIELD.area],
    ["write", "write (Pfad)", GE_FIELD.text],
  ],
  http: [
    ["url", "url", GE_FIELD.text, { required: true }],
    ["method", "method", GE_FIELD.sel, { options: ["GET", "POST", "PUT", "PATCH", "DELETE"] }],
    ["headers", "headers (JSON)", GE_FIELD.json],
    ["body", "body", GE_FIELD.area],
    ["expect", "expect (HTTP-Status)", GE_FIELD.num],
    ["timeout", "timeout (s)", GE_FIELD.num],
    ["emit", "emit", GE_FIELD.bool],
  ],
  llm: [
    ["prompt", "prompt", GE_FIELD.area, { required: true }],
    ["system", "system", GE_FIELD.area],
    ["model", "model", GE_FIELD.text],
    ["provider", "provider", GE_FIELD.text],
    ["temperature", "temperature", GE_FIELD.float],
    ["max_tokens", "max_tokens", GE_FIELD.num],
    ["schema", "schema (JSON)", GE_FIELD.json],
    ["untrusted", "untrusted", GE_FIELD.bool],
    ["timeout", "timeout (s)", GE_FIELD.num],
  ],
  notify: [
    ["message", "message", GE_FIELD.area],
    ["slack", "slack (JSON)", GE_FIELD.json],
    ["webhooks", "webhooks (JSON)", GE_FIELD.json],
  ],
  state: [
    ["action", "action", GE_FIELD.sel, { options: ["get", "set", "add"], required: true }],
    ["key", "key", GE_FIELD.text, { required: true }],
    ["value", "value", GE_FIELD.text],
    ["file", "file", GE_FIELD.text],
  ],
  verify: [
    ["checks", "checks (Liste von Steps, JSON)", GE_FIELD.json],
  ],
  review: [
    ["model", "model", GE_FIELD.text, { required: true }],
    ["backend", "backend", GE_FIELD.text],
    ["target", "target", GE_FIELD.text],
    ["prompt_file", "prompt_file", GE_FIELD.text],
  ],
  pr_review: [
    ["repo", "repo (owner/name)", GE_FIELD.text, { required: true }],
    ["pr", "pr", GE_FIELD.text],
    ["model", "model", GE_FIELD.text, { required: true }],
    ["backend", "backend", GE_FIELD.text],
    ["fix_backend", "fix_backend", GE_FIELD.text],
    ["fix_model", "fix_model", GE_FIELD.text],
    ["fix_commit", "fix_commit", GE_FIELD.bool],
    ["max_rounds", "max_rounds", GE_FIELD.num],
    ["max_findings", "max_findings", GE_FIELD.num],
    ["protected", "protected (Liste)", GE_FIELD.list],
  ],
  pr_merge: [
    ["repo", "repo (owner/name)", GE_FIELD.text, { required: true }],
    ["pr", "pr", GE_FIELD.text],
    ["method", "method", GE_FIELD.sel, { options: ["squash", "merge", "rebase"] }],
  ],
  issue: [
    ["repo", "repo (owner/name)", GE_FIELD.text],
    ["number", "number", GE_FIELD.text],
    ["action", "action", GE_FIELD.text],
    ["labels", "labels (Liste)", GE_FIELD.list],
    ["remove_labels", "remove_labels (Liste)", GE_FIELD.list],
    ["inherit_labels", "inherit_labels", GE_FIELD.bool],
    ["limit", "limit", GE_FIELD.num],
    ["only_if", "only_if", GE_FIELD.text],
    ["append", "append", GE_FIELD.area],
    ["skip_if_empty", "skip_if_empty", GE_FIELD.bool],
    ["emit", "emit", GE_FIELD.bool],
  ],
  human_gate: [
    ["repo", "repo", GE_FIELD.text],
    ["keywords", "keywords (Liste)", GE_FIELD.list],
    ["adr_paths", "adr_paths (Liste)", GE_FIELD.list],
    ["max_files", "max_files", GE_FIELD.num],
    ["max_lines", "max_lines", GE_FIELD.num],
    ["llm", "llm", GE_FIELD.bool],
  ],
};

// Fields that apply to EVERY step (approval + gate) — appended to the type schema.
const GE_STEP_COMMON = [
  ["gate", "gate (Fehler bricht Run ab)", GE_FIELD.bool, { default: true }],
  ["approval", "approval (immer pausieren)", GE_FIELD.bool],
  ["approval_reason", "approval_reason", GE_FIELD.text],
  ["approval_when", "approval_when (JSON: Var → Wert)", GE_FIELD.json],
];

const GE_TRIGGER_SCHEMAS = {
  manual: [],
  cron: [["cron", "cron (z. B. 0 9 * * *)", GE_FIELD.text, { required: true }]],
  on_file: [
    ["glob", "glob", GE_FIELD.text],
    ["debounce", "debounce (s)", GE_FIELD.num],
  ],
  on_issue: [
    ["repo", "repo (owner/name)", GE_FIELD.text],
    ["labels", "labels (Liste)", GE_FIELD.list],
    ["priority_labels", "priority_labels (Liste)", GE_FIELD.list],
    ["poll", "poll (s)", GE_FIELD.num],
    ["reconcile", "reconcile", GE_FIELD.bool],
    ["skip_blocked", "skip_blocked", GE_FIELD.bool],
    ["max_per_poll", "max_per_poll", GE_FIELD.num],
  ],
  on_pr: [
    ["repo", "repo (owner/name)", GE_FIELD.text],
    ["labels", "labels (Liste)", GE_FIELD.list],
    ["states", "states (Liste)", GE_FIELD.list],
    ["poll", "poll (s)", GE_FIELD.num],
    ["reconcile", "reconcile", GE_FIELD.sel, { options: ["", "true", "false", "head"], toStored: GE_RECONCILE_TO_STORED, toDisplay: GE_RECONCILE_TO_DISPLAY }],
  ],
  on_webhook: [
    ["secret_env", "secret_env", GE_FIELD.text, { required: true }],
    ["map", "map (JSON)", GE_FIELD.json],
  ],
};

function geSchemaFor(kind, type) {
  const base = kind === "step" ? (GE_STEP_SCHEMAS[type] || []) : (GE_TRIGGER_SCHEMAS[type] || []);
  return kind === "step" ? base.concat(GE_STEP_COMMON) : base;
}

function geFieldHtml(kind, type, def, obj, idx) {
  const [key, label, ftype, opts] = def;
  const val = obj[key];
  const data = `data-ge="${esc(key)}" data-ge-kind="${kind}" data-ge-idx="${idx}"`;
  const req = opts && opts.required ? ' data-ge-req="1"' : "";
  const inner = (() => {
    if (ftype === GE_FIELD.bool) {
      const defval = (opts && opts.default) ?? false;
      const checked = (val === undefined ? defval : val) ? "checked" : "";
      return `<input type="checkbox" ${data}${req} ${checked}>`;
    }
    if (ftype === GE_FIELD.sel) {
      const opts_ = (opts && opts.options) || [];
      const disp = opts && opts.toDisplay ? opts.toDisplay(val) : (val == null ? "" : String(val));
      const options = [`<option value="">—</option>`, ...opts_.map((o) => `<option value="${esc(o)}"${String(o) === String(disp) ? " selected" : ""}>${esc(o)}</option>`)];
      return `<select ${data}${req}>${options.join("")}</select>`;
    }
    if (ftype === GE_FIELD.json) {
      const txt = val === undefined ? "" : JSON.stringify(val, null, 2);
      return `<textarea rows="3" spellcheck="false" ${data}${req}>${esc(txt)}</textarea>`;
    }
    if (ftype === GE_FIELD.area) {
      return `<textarea rows="3" spellcheck="false" ${data}${req}>${esc(val == null ? "" : String(val))}</textarea>`;
    }
    if (ftype === GE_FIELD.num || ftype === GE_FIELD.float) {
      const step = ftype === GE_FIELD.float ? ' step="any"' : "";
      return `<input type="number"${step} ${data}${req} value="${esc(val == null ? "" : String(val))}">`;
    }
    if (ftype === GE_FIELD.list) {
      const txt = Array.isArray(val) ? val.join(", ") : (val == null ? "" : String(val));
      return `<input ${data}${req} value="${esc(txt)}">`;
    }
    return `<input ${data}${req} value="${esc(val == null ? "" : String(val))}">`;
  })();
  return `<div class="ge-field"><label>${esc(label)}</label>${inner}</div>`;
}

// Non-schema fields of an object as JSON (so nothing is hidden/lost).
function geExtraHtml(kind, type, obj, idx) {
  const schemaKeys = new Set(geSchemaFor(kind, type).map((d) => d[0]));
  const skip = new Set(["type", "name", ...schemaKeys]);
  const extra = {};
  for (const [k, v] of Object.entries(obj)) if (!skip.has(k)) extra[k] = v;
  if (!Object.keys(extra).length) return "";
  const data = `data-ge="__extra__" data-ge-kind="${kind}" data-ge-idx="${idx}"`;
  return `<div class="ge-field ge-extra"><label>${esc(t("graph.extra"))}</label><textarea rows="3" spellcheck="false" ${data}>${esc(JSON.stringify(extra, null, 2))}</textarea></div>`;
}

function geStepCard(s, i) {
  const type = s.type || "shell";
  const fields = geSchemaFor("step", type).map((d) => geFieldHtml("step", type, d, s, i)).join("");
  const extra = geExtraHtml("step", type, s, i);
  const typeOpts = GE_STEP_TYPES.map((t) => `<option value="${esc(t)}"${t === type ? " selected" : ""}>${esc(t)}</option>`).join("");
  return `<div class="ge-step" data-ge-step="${i}">
    <div class="ge-step-head">
      <span class="ge-step-no">${i + 1}</span>
      <select data-ge="type" data-ge-kind="step" data-ge-idx="${i}">${typeOpts}</select>
      <input data-ge="name" data-ge-kind="step" data-ge-idx="${i}" placeholder="${esc(t("graph.namePlaceholder"))}" value="${esc(s.name || "")}">
      <span class="ge-step-actions">
        <button class="ghost" title="${esc(t("graph.moveUp"))}" data-act="step-move" data-idx="${i}" data-dir="-1">↑</button>
        <button class="ghost" title="${esc(t("graph.moveDown"))}" data-act="step-move" data-idx="${i}" data-dir="1">↓</button>
        <button class="ghost" title="${esc(t("graph.remove"))}" data-act="step-remove" data-idx="${i}">✕</button>
      </span>
    </div>
    <div class="ge-step-body">${fields}${extra}</div>
  </div>`;
}

function geTriggerRow(trig, i) {
  const type = trig.type || "manual";
  const fields = geSchemaFor("trigger", type).map((d) => geFieldHtml("trigger", type, d, trig, i)).join("");
  const extra = geExtraHtml("trigger", type, trig, i);
  const typeOpts = GE_TRIGGER_TYPES.map((tt) => `<option value="${esc(tt)}"${tt === type ? " selected" : ""}>${esc(trigLabel(tt))}</option>`).join("");
  return `<div class="ge-trig" data-ge-trig="${i}">
    <div class="ge-trig-head">
      <select data-ge="type" data-ge-kind="trigger" data-ge-idx="${i}">${typeOpts}</select>
      <span class="ge-step-actions">
        <button class="ghost" title="${esc(t("graph.remove"))}" data-act="trigger-remove" data-idx="${i}">✕</button>
      </span>
    </div>
    <div class="ge-trig-body">${fields}${extra}</div>
  </div>`;
}

// Structural actions inside the loop card (index-based, not graphSel).
function graphCardMoveStepInLoop(i, dir) {
  const name = state.selected;
  const ldef = graphCardLoop(name);
  if (!ldef) return;
  const j = i + dir;
  if (!Array.isArray(ldef.steps) || j < 0 || j >= ldef.steps.length) return;
  const tmp = ldef.steps[i];
  ldef.steps[i] = ldef.steps[j];
  ldef.steps[j] = tmp;
  renderGraphCard(name);
  renderGraph(name);
}

function graphCardRemoveStepInLoop(i) {
  const name = state.selected;
  const ldef = graphCardLoop(name);
  if (!ldef || !Array.isArray(ldef.steps)) return;
  ldef.steps.splice(i, 1);
  pruneEmptyList(ldef, "steps");
  renderGraphCard(name);
  renderGraph(name);
}

function graphCardRemoveTriggerInLoop(i) {
  const name = state.selected;
  const ldef = graphCardLoop(name);
  if (!ldef || !Array.isArray(ldef.triggers)) return;
  ldef.triggers.splice(i, 1);
  pruneEmptyList(ldef, "triggers");
  renderGraphCard(name);
  renderGraph(name);
}

// ---- Read/write field values (draft update without re-render) ----
function geReadField(el, def) {
  const ftype = def[2];
  const opts = def[3] || {};
  if (ftype === GE_FIELD.bool) return el.checked;
  if (ftype === GE_FIELD.num) {
    const v = el.value.trim();
    return v === "" ? undefined : parseInt(v, 10);
  }
  if (ftype === GE_FIELD.float) {
    const v = el.value.trim();
    return v === "" ? undefined : parseFloat(v);
  }
  if (ftype === GE_FIELD.list) return el.value.split(",").map((x) => x.trim()).filter((x) => x);
  if (ftype === GE_FIELD.json) {
    const v = el.value.trim();
    if (v === "") return undefined;
    return JSON.parse(v); // throws on invalid JSON
  }
  if (ftype === GE_FIELD.sel) {
    const v = el.value;
    if (v === "") return undefined;
    return opts.toStored ? opts.toStored(v) : v;
  }
  const v = el.value;
  return v === "" ? undefined : v; // text / area
}

function geSetField(obj, key, def, raw) {
  const ftype = def[2];
  const opts = def[3] || {};
  if (ftype === GE_FIELD.bool) {
    const defval = opts.default ?? false;
    if (raw === defval) delete obj[key];
    else obj[key] = raw;
  } else if (raw === undefined) {
    delete obj[key];
  } else {
    obj[key] = raw;
  }
}

// ---- Loop spec (clickable loops) ----
const loopSpecCache = {};

function toggleLoopSpec(project, loop) {
  const el = $(`#loopspec-${CSS.escape(loop)}`);
  if (!el) return;
  if (el.classList.contains("hidden")) {
    openLoopSpec(project, loop);
  } else {
    state.openLoop = null;
    el.classList.add("hidden");
    const card = $(`#loop-${CSS.escape(loop)}`);
    if (card) card.classList.remove("open");
  }
}

// Open a loop spec (accordion: closes the previous one). Also called by the auto-refresh
// to restore the open spec after a re-render.
async function openLoopSpec(project, loop) {
  const seq = detailRenderSeq;
  const el = $(`#loopspec-${CSS.escape(loop)}`);
  if (!el) return;
  const card = $(`#loop-${CSS.escape(loop)}`);
  state.openLoop = loop;
  document.querySelectorAll(".loop.open").forEach((c) => {
    if (c === card) return;
    c.classList.remove("open");
    const spec = c.querySelector(".loopspec");
    if (spec) spec.classList.add("hidden");
  });
  el.classList.remove("hidden");
  if (card) card.classList.add("open");
  if (!loopSpecCache[project + "/" + loop]) {
    el.innerHTML = `<div class="loopspec-loading">${esc(t("spec.loading"))}</div>`;
    try {
      const spec = await api(`/api/projects/${encodeURIComponent(project)}/spec`);
      loopSpecCache[project + "/" + loop] = renderLoopSpec((spec.loops || {})[loop]);
    } catch (e) {
      loopSpecCache[project + "/" + loop] = `<div class="runerr">✖ ${esc(e.message)}</div>`;
    }
    // Guard: the DOM may have been re-rendered during the fetch (el detached)
    // or a newer render has started → then don't write anymore.
    if (seq !== detailRenderSeq) return;
  }
  // Write the content — both on cache hit (pre-fetch) and after the fetch.
  const cur = $(`#loopspec-${CSS.escape(loop)}`);
  if (cur) cur.innerHTML = loopSpecCache[project + "/" + loop];
}

function renderLoopSpec(ldef) {
  if (!ldef || !Array.isArray(ldef.steps)) return `<div class="runerr">${esc(t("spec.loopNotFound"))}</div>`;
  let out = "";
  if (ldef.description) out += `<div class="loopspec-desc">${esc(ldef.description)}</div>`;
  const trigs = ldef.triggers || [];
  if (trigs.length) {
    out += `<div class="loopspec-sec">${esc(t("spec.triggers"))}</div>` + trigs.map((tr) => {
      const extra = Object.entries(tr).filter(([k]) => k !== "type")
        .map(([k, v]) => `${esc(k)}=${esc(formatSpecVal(v))}`).join("  ");
      return `<div class="loopspec-trig">${badge(tr.type || "?", "trig")}${extra ? ` <span class="loopspec-trigextra">${extra}</span>` : ""}</div>`;
    }).join("");
  }
  out += `<div class="loopspec-sec">${esc(t("spec.steps"))}</div>` + ldef.steps.map((s, i) => renderStepSpec(i, s)).join("");
  return out;
}

function renderStepSpec(i, s) {
  const type = s.type || "?";
  const name = s.name || type;
  let out = `<div class="loopspec-step">
    <div class="loopspec-stephead"><span class="loopspec-stepno">${i + 1}.</span> <b>${esc(name)}</b> <span class="loopspec-steptype">[${esc(type)}]</span></div>`;
  const entries = Object.entries(s).filter(([k]) => !["type", "name"].includes(k));
  if (entries.length) {
    out += `<div class="loopspec-kv">` + entries.map(([k, v]) =>
      `<div class="loopspec-kvrow"><span class="loopspec-k">${esc(k)}</span><span class="loopspec-v">${esc(formatSpecVal(v))}</span></div>`
    ).join("") + `</div>`;
  }
  return out + `</div>`;
}

function formatSpecVal(v) {
  if (v == null) return "";
  if (typeof v === "object") return JSON.stringify(v);
  return String(v);
}

// Approval context in the run modal — same order as engine.approval_lines.
function formatApproval(ctx) {
  if (!ctx) return "";
  const row = (k, vHtml) => `<div class="appr-row"><span class="appr-k">${k}</span><span class="appr-v">${vHtml}</span></div>`;
  const rows = [];
  for (const key of ["kind", "condition", "why", "what"]) {
    if (ctx[key]) rows.push(row(t(`approval.${key}`), esc(ctx[key])));
  }
  for (const p of ctx.permissions || []) rows.push(row(t("approval.permissions"), esc(p)));
  const link = (href, text) => `<a href="${esc(href)}" target="_blank" rel="noopener">${esc(text)}</a>`;
  if (ctx.issue) rows.push(row(t("approval.issue"), link(ctx.issue, ctx.issue)));
  if (ctx.pr) rows.push(row(t("approval.pr"), `${link(ctx.pr, ctx.pr_title || ctx.pr)} · ${link(ctx.pr + "/files", t("approval.prFiles"))}`));
  if (ctx.tier != null) rows.push(row(t("approval.tier"), esc(ctx.tier)));
  if (!rows.length) return "";
  return `<div class="runapproval"><div class="appr-title">${esc(t("approval.title"))}</div>${rows.join("")}</div>`;
}

// Issue/PR of the run with links (rec.meta comes from the server, see runmeta.py).
function formatRunMeta(meta) {
  const rows = [];
  for (const kind of ["issue", "pr"]) {
    const m = meta && meta[kind];
    if (!m) continue;
    const label = `#${m.number || ""} ${m.title || ""}`.trim() || m.url;
    const val = m.url ? `<a href="${esc(m.url)}" target="_blank" rel="noopener">${esc(label)}</a>` : esc(label);
    const files = kind === "pr" && m.url ? ` · <a href="${esc(m.url)}/files" target="_blank" rel="noopener">${esc(t("approval.prFiles"))}</a>` : "";
    rows.push(`<div class="appr-row"><span class="appr-k">${esc(t(`approval.${kind}`))}</span><span class="appr-v">${val}${files}</span></div>`);
  }
  return rows.length ? `<div class="runmetabox">${rows.join("")}</div>` : "";
}

// Token count compact: 950, 12.3k, 1.30M.
function fmtTokens(n) {
  if (n >= 1e6) return `${(n / 1e6).toFixed(2)}M`;
  return n >= 1000 ? `${(n / 1000).toFixed(1)}k` : String(n);
}

// Costs/tokens compact ("$0.0123 · 1.30M tok (1.28M cached)"); "" without usage (run without LLM steps).
// Cache reads are included in total_tokens: an agent re-reads its context every turn.
// Subscription steps (billing=subscription) cost no money per token, but limits: "≈ $x" + hint.
function fmtUsd(c) {
  const sub = c.subscription_cost_usd || 0;
  const billed = c.cost_usd - sub;
  const parts = [];
  if (billed > 0 || !sub) parts.push(`${c.unpriced_steps ? "≥ " : ""}$${billed.toFixed(4)}`);
  if (sub) parts.push(`≈ $${sub.toFixed(4)} ${t("run.apiEquiv")}`);
  return parts.join(" + ");
}

function fmtCost(c) {
  if (!c || !c.total_tokens) return "";
  const usd = fmtUsd(c);
  const cached = c.cache_read_tokens ? ` (${fmtTokens(c.cache_read_tokens)} ${t("run.cached")})` : "";
  return `${usd} · ${fmtTokens(c.total_tokens)} ${t("run.tokens")}${cached}`;
}

function formatRunCost(cost) {
  const total = fmtCost(cost);
  if (!total) return "";
  const rows = (cost.steps || [])
    .map((s) => `<div class="appr-row"><span class="appr-k">${esc(s.step)}</span><span class="appr-v">${esc(s.backend)}${s.model ? ` / ${esc(s.model)}` : ""} · ${esc(fmtCost(s) || "–")}</span></div>`)
    .join("");
  return `<div class="runmetabox"><div class="appr-row"><span class="appr-k">${esc(t("run.cost"))}</span><span class="appr-v">${esc(total)}</span></div>${rows}</div>`;
}

const STEP_ICONS = { pass: "✔", skipped: "–", waiting: "⏸", running: "▶" };

function formatRun(rec) {
  if (!rec) return t("run.noRecord");
  let out = `<div class="runmeta">${esc(rec.id)} · ${badge(statusLabel(rec.status), rec.status)}${kindBadge(rec)}${verdictBadge(rec)} · ${esc(rec.trigger)} · ${esc(fmtDateTime(rec.started))}</div>`;
  // Retry chain (ADR 0036, decision 6): "attempt n/max, next at …".
  if (rec.retry) {
    const st = rec.retry.state;
    const max = rec.retry.max || 0;
    // rec.attempt is the 0-based index of this run (0 = original, 1..max = retries):
    // pending shows the upcoming attempt, exhausted the last failed one.
    const att = rec.attempt || 0;
    const next = rec.retry.next_attempt_at
      ? t("run.retryNext", { at: fmtDateTime(rec.retry.next_attempt_at) })
      : "";
    const label =
      st === "pending"
        ? t("run.retryPending", { att: att + 1, max, next })
        : st === "exhausted"
          ? t("run.retryExhausted", { att, max })
          : t("run.retryOther", { st });
    out += `<div class="runretry">${esc(label)}</div>`;
  }
  out += formatRunMeta(rec.meta);
  out += formatRunCost(rec.cost);
  if (rec.status === "queued") out += `<div class="runapproval"><div class="appr-title">${esc(t("run.queued"))}</div></div>`;
  if (rec.status === "needs_approval") {
    if (rec.approval_queued) out += `<div class="runapproval"><div class="appr-title">${esc(t("run.approvalQueued"))}</div></div>`;
    out += formatApproval(rec.approval);
  }
  for (const s of rec.steps || []) {
    out += `<div class="runstep ${esc(s.status)}">
      <span class="rsicon">${STEP_ICONS[s.status] || "✖"}</span>
      <span class="rsname">${esc(s.name)}</span>
      <span class="rstype">[${esc(s.type)}]</span>${kindBadge(s, FAILED_STEP_STATUSES)}${verdictBadge(s)}
      ${s.duration_s ? `<span class="rsdur">${s.duration_s}s</span>` : ""}
      <span class="rssummary">${esc(s.summary || "")}</span>
      ${s.output ? `<pre class="rsoutput">${esc(s.output.slice(-3000))}</pre>` : ""}
    </div>`;
  }
  if (rec.error) out += `<div class="runerr">${esc(t("run.error", { error: rec.error }))}</div>`;
  return out;
}

// Show a run record in the panel (tab "overview"). Stores the record for the
// tabs. For a NEW run (different id) the modal jumps to "overview"; for the
// same run (live polling) the tab stays — log/transcript are not overwritten.
function setRunRecord(rec) {
  const isNew = !runPanelRec || runPanelRec.id !== rec.id;
  runPanelRec = rec;
  // Running runs open directly in the log tab (the step bar shows the active step).
  if (isNew) showRunTab(rec.status === "running" ? "log" : "overview");
  else if (runTab === "overview") $("#runlog").innerHTML = formatRun(runPanelRec);
}

let runTab = "overview";
let runStream = null; // EventSource of the log tab
let runLogPoll = null; // polling fallback (timer id)

function stopRunStream() {
  if (runStream) runStream.close();
  runStream = null;
  clearTimeout(runLogPoll);
  runLogPoll = null;
}

// Switch tab: overview (record), log (SSE live), transcript (agent turns).
function showRunTab(tab) {
  stopRunStream();
  runTab = tab;
  document.querySelectorAll("#runtabs button").forEach((b) => {
    b.classList.toggle("active", b.dataset.tab === tab);
  });
  $("#runfollowlabel").classList.toggle("hidden", tab !== "log");
  const box = $("#runlog");
  if (!runPanelRec) return;
  if (tab === "overview") box.innerHTML = formatRun(runPanelRec);
  else if (tab === "log") startRunLog(box);
  else if (tab === "trace") loadTrace(box);
  else loadTranscript(box);
}

function runApiPath(suffix) {
  return `/api/run/${encodeURIComponent(runPanelRec.id)}/${suffix}`;
}

// Auto-scroll only with the active "follow" toggle.
function followRunLog() {
  if ($("#runfollow").checked) $("#runlog").scrollTop = $("#runlog").scrollHeight;
}

// ---- Step-aware log view ----
// The engine writes per step `[HH:MM:SS] ▶ <name> [<type>]` and at the end `✔|✖ <name> …`.
// From that, segment live: step bar (running step highlighted) + click = only
// that step's log. Lines before the first step (run context) belong to segment 0 ("run").
const LOG_STEP_START = /^\[[\d:]{8}\] ▶ (.+) \[([^\[\]]+)\]$/;
const LOG_STEP_END = /^\[[\d:]{8}\] ([✔✖]) /;
const LOG_RUN_END = /^\[[\d:]{8}\] ■ /;

class RunLogView {
  constructor(box) {
    box.innerHTML = '<div class="logsteps"></div><div class="logbody"></div>';
    this.bar = box.firstChild;
    this.body = box.lastChild;
    this.segs = [];
    this.partial = "";
    this.filter = null; // null = all segments
    this.bar.addEventListener("click", (ev) => {
      const chip = ev.target.closest("[data-seg]");
      if (chip) this.setFilter(chip.dataset.seg === "all" ? null : Number(chip.dataset.seg));
    });
    this.addSegment(t("run.logPrelude"), "", "pass", true);
  }
  addSegment(name, type, state, prelude = false) {
    const pre = document.createElement("pre");
    pre.className = "runrawlog logseg";
    this.body.appendChild(pre);
    const seg = { name, type, state, pre, prelude, empty: true };
    this.segs.push(seg);
    pre.dataset.seg = this.segs.length - 1;
    return seg;
  }
  reset() {
    this.body.innerHTML = "";
    this.segs = [];
    this.partial = "";
    this.addSegment(t("run.logPrelude"), "", "pass", true);
  }
  get current() { return this.segs[this.segs.length - 1]; }
  // Append text (line by line; an incomplete last line is held back).
  append(text) {
    const lines = (this.partial + text).split("\n");
    this.partial = lines.pop();
    for (const line of lines) this.addLine(line + "\n");
    this.render();
  }
  addLine(line) {
    const body = line.replace(/\n$/, "");
    const start = LOG_STEP_START.exec(body);
    if (start) this.addSegment(start[1], start[2], "running");
    const seg = this.current;
    seg.pre.appendChild(document.createTextNode(line));
    seg.empty = false;
    if (!start && !seg.prelude && seg.state === "running") {
      const end = LOG_STEP_END.exec(body);
      if (end && body.slice(end[0].length).startsWith(seg.name)) seg.state = end[1] === "✔" ? "pass" : "fail";
    }
    if (LOG_RUN_END.test(body)) this.finish();
  }
  // Run finished: a still "running" step (abort/timeout without ✔/✖) is no longer active.
  finish() {
    for (const s of this.segs) if (s.state === "running") s.state = "fail";
    this.render();
  }
  flush() {
    if (this.partial) { const rest = this.partial; this.partial = ""; this.addLine(rest); }
    this.render();
  }
  setFilter(idx) {
    this.filter = idx;
    this.render();
  }
  render() {
    const chips = [`<button class="logchip ${this.filter === null ? "active" : ""}" data-seg="all">${esc(t("run.logAll"))}</button>`];
    this.segs.forEach((s, i) => {
      if (s.prelude && s.empty) return;
      const icon = s.state === "running" ? "▶" : s.state === "pass" ? "✔" : "✖";
      chips.push(`<button class="logchip ${esc(s.state)} ${this.filter === i ? "active" : ""}" data-seg="${i}" title="${esc(s.type)}">` +
        `<span class="rsicon">${s.prelude ? "" : icon}</span>${esc(s.name)}</button>`);
    });
    this.bar.innerHTML = chips.join("");
    this.segs.forEach((s, i) => {
      s.pre.classList.toggle("hidden", this.filter !== null && this.filter !== i);
    });
    followRunLog();
  }
}

// Log tab: SSE (reconnect/Last-Event-ID is handled by the browser); without EventSource or on
// a persistent connection error fall back to polling the complete log.
function startRunLog(box) {
  const view = new RunLogView(box);
  const pre = { dataset: {}, view }; // marker object for ended/fallback state
  const project = encodeURIComponent(runPanelRec.project || "");
  if (typeof EventSource === "undefined") return pollRunLog(pre, project);
  const es = new EventSource(runApiPath(`stream?project=${project}`));
  runStream = es;
  let streamErrors = 0;
  let fallbackTimer = null;
  es.onopen = () => {
    streamErrors = 0;
    clearTimeout(fallbackTimer);
    fallbackTimer = null;
  };
  es.addEventListener("log", (ev) => {
    view.append(JSON.parse(ev.data).text);
  });
  es.addEventListener("status", (ev) => {
    const st = JSON.parse(ev.data).status;
    setRunStatus(st);
  });
  es.addEventListener("end", () => {
    es.close();
    runStream = null;
    clearTimeout(fallbackTimer);
    fallbackTimer = null;
    pre.dataset.ended = "1";
    view.flush();
    // Reload the record so overview/status show the terminal state.
    api(`/api/run/${encodeURIComponent(runPanelRec.id)}?project=${project}`).then((rec) => { runPanelRec = rec; }).catch(() => {});
  });
  es.onerror = () => {
    streamErrors += 1;
    if (!pre.dataset.ended && !fallbackTimer) {
      fallbackTimer = setTimeout(() => {
        fallbackTimer = null;
        if (streamErrors && runStream === es && !pre.dataset.ended) {
          es.close();
          runStream = null;
          pollRunLog(pre, project);
        }
      }, 5000);
    }
    if (es.readyState === EventSource.CLOSED && !pre.dataset.ended) {
      runStream = null;
      pollRunLog(pre, project);
    }
  };
}

async function pollRunLog(pre, project) {
  if (runTab !== "log" || !runPanelRec) return;
  try {
    const r = await fetch(runApiPath(`log?project=${project}`));
    if (r.ok) {
      const text = await r.text();
      const keep = pre.view.filter;
      pre.view.reset();
      pre.view.append(text || t("run.logEmpty") + "\n");
      if (keep !== null && keep < pre.view.segs.length) pre.view.setFilter(keep);
    }
  } catch (e) { /* next attempt */ }
  const st = $("#runstatus").dataset.status;
  if (st === "running" || st === "queued" || st === "needs_approval") {
    runLogPoll = setTimeout(() => pollRunLog(pre, project), 2000);
  }
}

// Transcript tab: agent turns page by page (lazy) from the session log.
async function loadTranscript(box) {
  box.innerHTML = '<div class="runtranscript"></div>';
  const list = box.firstChild;
  const id = runPanelRec.id;
  let after = 0;
  let n = 0;
  for (;;) {
    let page;
    try {
      page = await api(`${runApiPath("transcript")}?project=${encodeURIComponent(runPanelRec.project || "")}&after=${after}`);
    } catch (e) {
      list.insertAdjacentHTML("beforeend", `<div class="runerr">${esc(t("run.transcriptFailed", { error: e.message }))}</div>`);
      return;
    }
    if (runTab !== "transcript" || !runPanelRec || runPanelRec.id !== id) return; // tab/run changed
    n += page.entries.length;
    list.insertAdjacentHTML("beforeend", page.entries.map(transcriptEntry).join(""));
    if (!page.entries.length || page.next_after === after) break;
    after = page.next_after;
  }
  if (!n) list.innerHTML = `<div style="color:var(--dim)">${esc(t("run.noTranscript"))}</div>`;
}

function transcriptEntry(e) {
  const tool = typeof e.tool === "string" ? e.tool : JSON.stringify(e.tool, null, 2);
  const body = e.role === "tool" ? `<pre>${esc(tool)}</pre>` : `<pre>${esc(e.text)}</pre>`;
  return `<div class="turn turn-${esc(e.role)}"><div class="turnhead">${esc(e.role)} · ${esc(e.step)} · ${esc(e.ts)}</div>${body}</div>`;
}

// ---- Trace view (Issue #128) ----
// Chronological step trace from the full session log (GET /api/run/<id>/events):
// each step shows status, duration, verdict and usage (tokens/cost); agent events
// (prompt / tool_call / message) are drill-down items; artifacts are listed at the end.
// Works for legacy JSON runs and SQL runs — the endpoint abstracts the backend.

// Parse an event's `data` field (a redacted JSON string) into an object; bad JSON → {}.
function parseEventData(data) {
  if (!data) return {};
  if (typeof data === "object") return data;
  try {
    return JSON.parse(data);
  } catch {
    return {};
  }
}

// Fold the flat, chronological event list into ordered render blocks. Steps are
// sequential, so agent events attach to the currently open step; run_start /
// run_end / approval / warning become standalone timeline entries.
function buildTraceBlocks(events) {
  const blocks = [];
  let cur = null; // currently open step block
  const openStep = (name, type) => {
    cur = { type: "step", name, stepType: type || "", status: "running", durationS: null, usage: null, events: [] };
    blocks.push(cur);
    return cur;
  };
  for (const ev of events) {
    const d = parseEventData(ev.data);
    const kind = ev.kind;
    if (kind === "run_start") {
      blocks.push({ type: "run_start", loop: d.loop, trigger: d.trigger, ts: ev.ts });
      cur = null;
    } else if (kind === "step_start") {
      openStep(ev.step_name, ev.step_type);
    } else if (kind === "step_done") {
      const b = cur && cur.name === ev.step_name ? cur : openStep(ev.step_name, ev.step_type);
      b.status = ev.status || "pass";
      b.durationS = d.duration_s ?? null;
      b.usage = d.usage ?? null;
    } else if (kind === "prompt" || kind === "tool_call" || kind === "message") {
      if (!cur || cur.name !== ev.step_name) openStep(ev.step_name, ev.step_type || "agent");
      cur.events.push({ role: kind, ts: ev.ts, text: d.text ?? "", tool: d.tool ?? null });
    } else if (kind === "approval") {
      blocks.push({ type: "approval", ts: ev.ts, data: d });
      cur = null;
    } else if (kind === "warning") {
      blocks.push({ type: "warning", ts: ev.ts, data: d });
    } else if (kind === "run_end") {
      blocks.push({ type: "run_end", status: ev.status, ts: ev.ts });
      cur = null;
    }
    // unknown kinds are ignored
  }
  return blocks;
}

// Compact per-step usage line: "model · 1.3M tok · $0.0123".
function fmtStepUsage(u) {
  const parts = [];
  if (u.model) parts.push(u.model);
  if (u.total_tokens) parts.push(`${fmtTokens(u.total_tokens)} ${t("run.tokens")}`);
  if (u.cost_usd) parts.push(`$${Number(u.cost_usd).toFixed(4)}`);
  return parts.join(" · ");
}

function traceAgentEvent(e) {
  const icon = e.role === "prompt" ? "👤" : e.role === "tool_call" ? "🔧" : "💬";
  const body =
    e.role === "tool_call"
      ? `<pre class="trace-tool">${esc(typeof e.tool === "string" ? e.tool : JSON.stringify(e.tool, null, 2))}</pre>`
      : `<pre class="trace-text">${esc(e.text)}</pre>`;
  return `<details class="trace-event trace-event-${esc(e.role)}">
    <summary><span class="trace-icon">${icon}</span> ${esc(t(`run.trace.${e.role}`))} · ${esc(e.ts || "")}</summary>
    ${body}
  </details>`;
}

function traceBlock(b, verdicts = {}) {
  if (b.type === "run_start")
    return `<div class="trace-entry trace-runstart"><span class="trace-icon">▶</span> ${esc(t("run.trace.started"))} · ${esc(t("run.trace.loop"))}: ${esc(b.loop || "")} · ${esc(t("run.trace.trigger"))}: ${esc(b.trigger || "")}</div>`;
  if (b.type === "run_end")
    return `<div class="trace-entry trace-runend"><span class="trace-icon">■</span> ${esc(t("run.trace.ended"))} · ${badge(statusLabel(b.status), b.status)}</div>`;
  if (b.type === "approval")
    return `<div class="trace-entry trace-approval"><span class="trace-icon">⏸</span> ${esc(t("run.trace.approval"))}</div>`;
  if (b.type === "warning") {
    const msg = (b.data && (b.data.message || b.data.text || b.data.warning)) || "";
    return `<div class="trace-entry trace-warning"><span class="trace-icon">⚠</span> ${esc(t("run.trace.warning"))}${msg ? `: ${esc(msg)}` : ""}</div>`;
  }
  // step block
  const icon = STEP_ICONS[b.status] || "✖";
  const dur = b.durationS != null ? `<span class="trace-dur">${fmtDur(Math.round(b.durationS))}</span>` : "";
  const usage = b.usage ? `<span class="trace-usage">${esc(fmtStepUsage(b.usage))}</span>` : "";
  const verdict = verdicts[b.name] ? verdictBadge({ verdict: verdicts[b.name] }) : "";
  const count = b.events.length ? `<span class="trace-count">${b.events.length} ${esc(t("run.trace.events"))}</span>` : "";
  const events = b.events.map(traceAgentEvent).join("");
  return `<div class="trace-step ${esc(b.status)}">
    <div class="trace-stephead">
      <span class="trace-icon">${icon}</span>
      <span class="trace-stepname">${esc(b.name)}</span>
      <span class="trace-steptype">[${esc(b.stepType || "")}]</span>
      ${verdict}${dur}${usage}${count}
    </div>
    ${events ? `<div class="trace-events">${events}</div>` : ""}
  </div>`;
}

function traceArtifacts(artifacts) {
  const rows = artifacts
    .map(
      (a) =>
        `<div class="trace-artifact"><span class="trace-icon">📎</span> ${esc(a.path)} <span class="trace-artifactmeta">[${esc(a.kind || "")}] · ${esc(a.step_name || "")}</span></div>`,
    )
    .join("");
  return `<div class="trace-artifacts"><div class="trace-artifacts-title">${esc(t("run.trace.artifacts"))}</div>${rows}</div>`;
}

async function loadTrace(box) {
  box.innerHTML = '<div class="runtrace"></div>';
  const list = box.firstChild;
  const id = runPanelRec.id;
  let data;
  try {
    data = await api(`${runApiPath("events")}?project=${encodeURIComponent(runPanelRec.project || "")}`);
  } catch (e) {
    list.innerHTML = `<div class="runerr">${esc(t("run.traceFailed", { error: e.message }))}</div>`;
    return;
  }
  if (runTab !== "trace" || !runPanelRec || runPanelRec.id !== id) return; // Tab/Run changed
  const blocks = buildTraceBlocks(data.events || []);
  const artifacts = data.artifacts || [];
  if (!blocks.length && !artifacts.length) {
    list.innerHTML = `<div style="color:var(--dim)">${esc(t("run.noTrace"))}</div>`;
    return;
  }
  // Verdicts live in the run record (not the session log): map step name → verdict.
  const verdicts = {};
  for (const s of runPanelRec.steps || []) if (s && s.verdict) verdicts[s.name] = s.verdict;
  list.innerHTML = blocks.map((b) => traceBlock(b, verdicts)).join("") + (artifacts.length ? traceArtifacts(artifacts) : "");
}

// Status badge in the run panel: raw status in data-status (the polling logic
// compares against it), display text translated.
function setRunStatus(status) {
  const el = $("#runstatus");
  el.dataset.status = status || "";
  el.textContent = statusLabel(status);
  el.className = "badge " + (status || "");
}

function openRunPanel(title, status) {
  $("#runbackdrop").classList.remove("hidden");
  $("#runpanel").classList.remove("hidden");
  $("#runtitle").textContent = title;
  setRunStatus(status);
  renderRunActions(status);
}

function renderRunActions(status) {
  $("#runactions").innerHTML = status === "needs_approval"
    ? `<button class="run" data-act="run-approve">${esc(t("history.approve"))}</button>
        <button class="ghost" data-act="run-reject">${esc(t("history.reject"))}</button>` : "";
}

function runApprove(ok) {
  const id = $("#runtitle").textContent.match(/· (\S+)$/);
  if (!id) return;
  const proj = $("#runtitle").textContent.split(" / ")[0];
  chatApprove(proj, id[1], ok);
}

function openRun(id, project) {
  const qs = project ? `?project=${encodeURIComponent(project)}` : "";
  api(`/api/run/${encodeURIComponent(id)}${qs}`).then((rec) => {
    openRunPanel(`${rec.project} / ${rec.loop} · ${id}`, rec.status);
    setRunRecord(rec);
  });
}

function closeRun() {
  clearTimeout(state.runTimer);
  runPanelRec = null;
  stopRunStream();
  $("#runpanel").classList.add("hidden");
  $("#runbackdrop").classList.add("hidden");
}

// ---- Settings: connections (ADR 0052) ----
let connData = { connections: [], drivers: { agent: [], llm: [] } };

const canEditConnections = () => auth.mode !== "local" || auth.role === "admin";

async function openSettings() {
  $("#settingsbackdrop").classList.remove("hidden");
  $("#settingspanel").classList.remove("hidden");
  await loadConnections();
}

function closeSettings() {
  $("#settingsbackdrop").classList.add("hidden");
  $("#settingspanel").classList.add("hidden");
}

async function loadConnections() {
  try {
    connData = await api("/api/connections");
  } catch (e) {
    $("#settingsbody").innerHTML = `<div class="runerr">${esc(t("settings.loadFailed", { error: e.message }))}</div>`;
    return;
  }
  renderConnections();
}

function connRow(c) {
  const target = [c.endpoint, c.model].filter(Boolean).join(" · ");
  const flags = [
    c.is_default ? `<span class="badge pass">${esc(t("settings.default"))}</span>` : "",
    `<span class="badge trig">${esc(t(`settings.source.${c.source}`))}</span>`,
    c.secret_set ? `<span title="${esc(t("settings.secretSet"))}">🔑</span>` : "",
  ].join(" ");
  const actions = canEditConnections()
    ? `<button class="ghost" data-conn-edit="${esc(c.name)}">${esc(t(c.source === "env" ? "settings.override" : "settings.edit"))}</button>` +
      (c.source === "db" ? ` <button class="ghost" data-conn-del="${esc(c.name)}">${esc(t("settings.delete"))}</button>` : "")
    : "";
  return `<tr><td>${esc(c.name)}</td><td>${esc(t(`settings.kind.${c.kind}`))}</td><td>${esc(c.driver)}</td>` +
    `<td>${esc(target)}</td><td>${flags}</td><td class="connactions">${actions}</td></tr>`;
}

function renderConnections() {
  const rows = connData.connections.map(connRow).join("") ||
    `<tr><td colspan="6" style="color:var(--dim)">${esc(t("settings.empty"))}</td></tr>`;
  $("#settingsbody").innerHTML =
    `<table class="conntable"><thead><tr><th>${esc(t("settings.name"))}</th><th>${esc(t("settings.kind"))}</th>` +
    `<th>${esc(t("settings.driver"))}</th><th>${esc(t("settings.target"))}</th><th></th><th></th></tr></thead>` +
    `<tbody>${rows}</tbody></table>` +
    (canEditConnections()
      ? `<button class="run" id="connadd">${esc(t("settings.add"))}</button>`
      : `<div style="color:var(--dim)">${esc(t("settings.readOnly"))}</div>`) +
    `<div id="connformbox"></div>`;
}

function connForm(c) {
  const isNew = !c || c.source === "env";
  const cur = c || { name: "", kind: "agent", driver: "", endpoint: "", model: "", username: "", secret_set: false, is_default: false };
  const kindOpts = ["agent", "llm"].map((k) =>
    `<option value="${k}"${cur.kind === k ? " selected" : ""}>${esc(t(`settings.kind.${k}`))}</option>`).join("");
  const field = (id, label, val, extra = "") =>
    `<label for="cf-${id}">${esc(label)}</label><input id="cf-${id}" type="text" value="${esc(val)}" autocomplete="off" ${extra}>`;
  $("#connformbox").innerHTML =
    `<form class="connform" id="connform"><h3>${esc(t(c ? "settings.editing" : "settings.new", { name: cur.name }))}</h3>` +
    field("name", t("settings.name"), cur.name, isNew ? "" : "readonly") +
    `<label for="cf-kind">${esc(t("settings.kind"))}</label><select id="cf-kind">${kindOpts}</select>` +
    `<label for="cf-driver">${esc(t("settings.driver"))}</label><select id="cf-driver"></select>` +
    field("endpoint", t("settings.endpoint"), cur.endpoint) +
    field("model", t("settings.model"), cur.model) +
    `<span id="cf-username-row" style="display:contents">${field("username", t("settings.username"), cur.username)}</span>` +
    `<label for="cf-secret">${esc(t("settings.secret"))}</label><input id="cf-secret" type="password" autocomplete="new-password" ` +
    `placeholder="${esc(cur.secret_set && !isNewOverride(c) ? t("settings.secretKeep") : "")}">` +
    `<span></span><span class="connhint" id="cf-secret-hint"></span>` +
    `<span></span><span><label class="inline"><input type="checkbox" id="cf-default"${cur.is_default ? " checked" : ""}> ${esc(t("settings.makeDefault"))}</label>` +
    (cur.secret_set && !isNewOverride(c) ? ` <label class="inline"><input type="checkbox" id="cf-clear"> ${esc(t("settings.clearSecret"))}</label>` : "") + `</span>` +
    `<div class="connbuttons"><button class="run" type="submit">${esc(t("settings.save"))}</button>` +
    `<button class="ghost" type="button" id="cf-cancel">${esc(t("settings.cancel"))}</button><span class="connerror" id="cf-error"></span></div></form>`;
  syncConnDriver(cur.driver);
  $("#cf-secret").addEventListener("input", syncSecretHint);
}

// Short secrets are not masked by log masking (secrets.MIN_SECRET_LEN) — hint about that while typing.
function syncSecretHint() {
  const len = $("#cf-secret").value.length;
  const min = connData.min_masked_secret_len || 0;
  $("#cf-secret-hint").textContent = len > 0 && len < min ? t("settings.secretShort", { min }) : "";
}

// Env connections don't deliver a secret: an override starts without a stored secret.
const isNewOverride = (c) => !!c && c.source === "env";

// Driver selection and username field depend on the kind (agent/llm).
function syncConnDriver(selected) {
  const kind = $("#cf-kind").value;
  $("#cf-driver").innerHTML = (connData.drivers[kind] || []).map((d) =>
    `<option value="${esc(d)}"${d === selected ? " selected" : ""}>${esc(d)}</option>`).join("");
  $("#cf-username-row").style.display = kind === "agent" ? "contents" : "none";
}

async function saveConnection(e) {
  e.preventDefault();
  const val = (id) => $(`#cf-${id}`).value.trim();
  const body = {
    kind: val("kind"), driver: val("driver"), endpoint: val("endpoint"), model: val("model"),
    username: val("username"), is_default: $("#cf-default").checked,
  };
  if (val("secret")) body.secret = $("#cf-secret").value;
  if ($("#cf-clear")?.checked) body.clear_secret = true;
  try {
    await api(`/api/connections/${encodeURIComponent(val("name"))}`, {
      method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
    });
  } catch (err) {
    $("#cf-error").textContent = err.detail || err.message;
    return;
  }
  await loadConnections();
  refreshLlm();
}

async function deleteConnection(name) {
  if (!confirm(t("settings.confirmDelete", { name }))) return;
  try {
    await api(`/api/connections/${encodeURIComponent(name)}`, { method: "DELETE" });
  } catch (err) {
    alert(t("settings.deleteFailed", { error: err.detail || err.message }));
    return;
  }
  await loadConnections();
  refreshLlm();
}

$("#settingstoggle").addEventListener("click", openSettings);
$("#settingsclose").addEventListener("click", closeSettings);
$("#settingsbackdrop").addEventListener("click", closeSettings);
$("#settingsbody").addEventListener("click", (e) => {
  const edit = e.target.closest("[data-conn-edit]");
  const del = e.target.closest("[data-conn-del]");
  if (edit) connForm(connData.connections.find((c) => c.name === edit.dataset.connEdit));
  else if (del) deleteConnection(del.dataset.connDel);
  else if (e.target.id === "connadd") connForm(null);
  else if (e.target.id === "cf-cancel") $("#connformbox").innerHTML = "";
});
$("#settingsbody").addEventListener("submit", saveConnection);
$("#settingsbody").addEventListener("change", (e) => { if (e.target.id === "cf-kind") syncConnDriver(""); });

// ---- Chat ----
function chatFmt(t) {
  return esc(t)
    .replace(/\*\*(.+?)\*\*/g, "<b>$1</b>")
    .replace(/`([^`]+)`/g, "<code>$1</code>")
    .replace(/\n/g, "<br>");
}

function chatAdd(who, text) {
  const log = $("#chatlog");
  log.insertAdjacentHTML("beforeend",
    `<div class="chatline ${who}"><span class="chatwho">${esc(who === "me" ? t("chat.me") : t("chat.fabrikant"))}</span><span>${chatFmt(text)}</span></div>`);
  log.scrollTop = log.scrollHeight;
}

let chatAbort = null;

function chatPending() {
  const log = $("#chatlog");
  const div = document.createElement("div");
  div.className = "chatline fabrikant pending";
  div.innerHTML =
    `<span class="chatwho">${esc(t("chat.fabrikant"))}</span>` +
    `<span class="chatpending"><span class="dots"><i></i><i></i><i></i></span>` +
    `<button class="cancel" title="${esc(t("chat.cancelTitle"))}">${esc(t("chat.cancel"))}</button></span>`;
  log.appendChild(div);
  div.querySelector(".cancel").addEventListener("click", () => chatAbort && chatAbort.abort());
  log.scrollTop = log.scrollHeight;
  return div;
}

async function chatSend(e) {
  if (e) e.preventDefault();
  const input = $("#chatinput");
  const msg = input.value.trim();
  if (!msg) return;
  input.value = "";
  chatAdd("me", msg);
  const pend = chatPending();
  const reqId = `c${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
  chatAbort = new AbortController();
  // Replace the flex container (.chatpending) with a plain <span> so the text
  // flows the way it does in chatAdd() (inline-flex would lay <br>/lines side by side).
  const settle = (html, text) => {
    const span = document.createElement("span");
    if (text !== undefined) span.textContent = text;
    else span.innerHTML = html;
    pend.querySelector(".chatpending").replaceWith(span);
    pend.classList.remove("pending");
  };
  try {
    const r = await api("/api/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ message: msg, project: state.selected || "", req_id: reqId }),
      signal: chatAbort.signal,
    });
    settle(chatFmt(r.reply));
  } catch (err) {
    if (err.name === "AbortError") {
      settle(null, t("chat.aborted"));
      api("/api/chat/cancel", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ req_id: reqId }),
      }).catch(() => {});
    } else {
      settle(null, `✖ ${err.message}`);
    }
  }
  $("#chatlog").scrollTop = $("#chatlog").scrollHeight;
  chatAbort = null;
  refresh();
}

async function chatApprove(project, runId, ok) {
  try {
    const r = await api(ok ? "/api/approve" : "/api/reject", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ project, run_id: runId }),
    });
    chatAdd("fabrikant", r.reply);
    // Update the modal immediately: new status + buttons gone + fresh log.
    // Without this refresh, "approve"/"reject" would stay visible even though
    // the run was rejected (or continued).
    try {
      const rec = await api(`/api/run/${encodeURIComponent(runId)}?project=${encodeURIComponent(project)}`);
      openRunPanel(`${rec.project} / ${rec.loop} · ${runId}`, rec.status);
      setRunRecord(rec);
      $("#runlog").scrollTop = $("#runlog").scrollHeight;
      // Approve: the resume worker writes "running" only shortly AFTER the
      // POST → the GET here often still returns needs_approval. Poll
      // unconditionally (with grace), otherwise the panel freezes on needs_approval
      // although the run is long running. If the project lock is held, the
      // worker waits server-side (approval_queued) — the polling picks up the
      // switch to running. resumed=false (thread-start error): no polling.
      if (ok && r.resumed) pollRunById(project, runId, { graceNeedsApproval: true });
    } catch (e) { /* panel stays open; the auto-refresh picks up the status */ }
  } catch (e) {
    chatAdd("fabrikant", `✖ ${e.message}`);
  }
  refresh();
}

// ---- LLM providers ----
async function refreshLlm() {
  const sel = $("#llmselect");
  if (!sel) return;
  try {
    const data = await api("/api/llm");
    const prev = sel.value || data.active;
    sel.innerHTML = (data.providers || []).map((p) =>
      `<option value="${esc(p.name)}"${p.name === data.active ? " selected" : ""}>${esc(p.name)} · ${esc(p.model)}</option>`
    ).join("");
    if (!sel.value && prev) sel.value = prev;
    sel.disabled = !(data.providers || []).length;
  } catch (e) {
    sel.innerHTML = `<option value="">${esc(t("llm.offline"))}</option>`;
    sel.disabled = true;
  }
}

async function setLlm(name) {
  try {
    await api("/api/llm", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ provider: name }),
    });
    await refreshLlm();
  } catch (e) {
    alert(t("llm.switchFailed", { error: e.message }));
    refreshLlm();
  }
}

// ---- Schedule view: cron outlook + one-time overrides ----
// Shows when which loops (cron) run next; run runs now
// (startRun) or set a one-time override ("15:05", "in 10 min",
// ISO-8601) → POST/DELETE /api/schedule/onetime, the scheduler fires it.
let schedData = null;
let schedOtTarget = null; // {project, loop} of the open one-time form

function fmtDur(s) {
  if (s < 60) return `${s}s`;
  if (s < 3600) return `${Math.floor(s / 60)}m ${s % 60}s`;
  return `${Math.floor(s / 3600)}h ${Math.floor((s % 3600) / 60)}m`;
}

// Queue section: cron times are only the plan — here you see what is actually
// running (elapsed) / queued (submission time + start estimate) / waiting for
// approval, plus worker and slot context (concurrency limits).
function renderQueue() {
  const q = schedData.queue || {};
  const pool = q.pool || { workers: 0, queue: 0 };
  const running = q.running || [];
  const queued = q.queued || [];
  const approval = q.approval || [];
  const chips = (q.projects || []).map((p) =>
    `<span class="sched-chip" title="${esc(t("sched.slotsTitle", { name: p.name, used: p.slots_used, limit: p.slots_limit }))}">${esc(p.name)} ${p.slots_used}/${p.slots_limit}</span>`
  ).join(" ");
  const head = `<div class="sched-sec" title="${esc(t("sched.queueTitle"))}">${esc(t("sched.queueSection"))}</div>
    <div class="sched-queue-summary">${esc(t("sched.queueSummary", { workers: pool.workers, q: pool.queue, r: running.length }))} ${chips}</div>`;
  if (!running.length && !queued.length && !approval.length) {
    return head + `<div class="sched-empty">${esc(t("sched.queueEmpty"))}</div>`;
  }
  const estLabel = (e) => (e === "now" ? t("sched.estNow") : e === "worker" ? t("sched.estWorker") : t("sched.estSlot"));
  const runningRows = running.map((r) => `<div class="schedrow q-running">
    <div class="schedrow-main">
      <span class="schedname">${esc(r.project)}/${esc(r.loop)}</span>
      <span class="badge trig">${esc(r.trigger)}</span>
      <span class="q-wait q-run" title="${esc(fmtDateTime(r.started))}">${esc(t("sched.qElapsed", { d: fmtDur(r.elapsed_s) }))}</span>
    </div>
  </div>`).join("");
  const queuedRows = queued.map((e) => `<div class="schedrow q-queued">
    <div class="schedrow-main">
      <span class="schedname">${esc(e.project)}/${esc(e.loop)}</span>
      <span class="badge trig">${esc(e.trigger)}</span>
      <span class="q-wait q-waiting">⏳ ${esc(t("sched.queuedSince", { t: fmtDateTime(e.queued_at) }))} · ${esc(estLabel(e.est))}</span>
    </div>
  </div>`).join("");
  const approvalRows = approval.map((a) => `<div class="schedrow q-approval">
    <div class="schedrow-main">
      <span class="schedname">${esc(a.project)}/${esc(a.loop)}</span>
      <span class="q-wait q-approval-wait">🔒 ${esc(t("sched.approvalWait"))}</span>
    </div>
  </div>`).join("");
  return head + runningRows + queuedRows + approvalRows;
}

function setSchedule(open) {
  state.schedOpen = open;
  $("#schedpanel").classList.toggle("hidden", !open);
  $("#schattoggle").classList.toggle("active", open);
  if (open) {
    refreshSchedule();
    state.schedTimer = setInterval(refreshSchedule, 5000);
  } else if (state.schedTimer) {
    clearInterval(state.schedTimer);
    state.schedTimer = null;
  }
}

async function refreshSchedule() {
  try {
    schedData = await api("/api/schedule");
  } catch (e) {
    if (state.schedOpen) {
      $("#schedbody").innerHTML = `<div class="sched-err">${esc(t("sched.loadFailed", { error: e.message }))}</div>`;
    }
    return;
  }
  if (state.schedOpen) renderSchedule();
}

// KPI-Panel (Issue #127): Factory-KPIs je Projekt aus GET /api/kpis.
function fmtKpiRate(r) {
  return r === null || r === undefined ? "—" : `${Math.round(r * 100)} %`;
}

function renderKpiRow(name, k) {
  const ct = k.cycle_time_s || {};
  const cycle = ct.avg === null || ct.avg === undefined ? "—" : fmtDur(Math.round(ct.avg));
  const rt = k.runtime_s || {};
  const runtime = rt.count ? `${fmtDur(Math.round(rt.avg))} / ${fmtDur(Math.round(rt.total))}` : "—";
  return `<div class="schedrow">
    <div class="schedrow-main"><span class="schedname">${esc(name)}</span></div>
    <span title="${esc(t("kpis.runsTitle"))}">${esc(t("kpis.runs"))}: ${k.passed || 0} ✔ / ${k.failed || 0} ✖ (${k.runs || 0})</span>
    <span title="${esc(t("kpis.runtimeTitle"))}">${esc(t("kpis.runtime"))}: ${esc(runtime)}</span>
    <span title="${esc(t("kpis.costTitle"))}">${esc(t("kpis.cost"))}: ${esc(fmtCost(k.cost) || "—")}</span>
    <span title="${esc(t("kpis.cycleTitle"))}">${esc(t("kpis.cycle"))}: ${esc(cycle)} (${ct.count || 0})</span>
    <span title="${esc(t("kpis.vetoTitle"))}">${esc(t("kpis.veto"))}: ${esc(fmtKpiRate(k.judge_veto_rate))}</span>
    <span title="${esc(t("kpis.escapeTitle"))}">${esc(t("kpis.escape"))}: ${esc(fmtKpiRate(k.defect_escape_rate))}
      (${k.defects_caught}/${k.defects_escaped})</span>
  </div>`;
}

async function refreshKpis() {
  let data;
  try {
    data = await api("/api/kpis");
  } catch (e) {
    $("#kpibody").innerHTML = `<div class="sched-err">${esc(t("kpis.loadFailed", { error: e.message }))}</div>`;
    return;
  }
  const names = Object.keys(data.projects || {});
  const rows = names.flatMap((p) => [
    renderKpiRow(p, data.projects[p]),
    ...Object.entries(data.projects[p].loops || {}).map(([l, k]) => renderKpiRow(`${p}/${l}`, k)),
  ]);
  $("#kpibody").innerHTML = names.length
    ? renderKpiRow(t("kpis.total"), data.total) + rows.join("")
    : `<div class="sched-empty">${esc(t("kpis.empty"))}</div>`;
}

function setKpis(open) {
  $("#kpipanel").classList.toggle("hidden", !open);
  $("#kpitoggle").classList.toggle("active", open);
  if (open) refreshKpis();
}

// One-time form (⏱ button of a row). Rebuilt only on open/close —
// not on the 5-s refresh, otherwise input and focus would be lost.
function renderSchedForm() {
  const form = $("#schedform");
  if (schedOtTarget) {
    form.innerHTML = `<div class="schedform">
      <span class="schedname">${esc(schedOtTarget.project)}/${esc(schedOtTarget.loop)}</span>
      <input id="sched-when" placeholder="${esc(t("sched.onetimePlaceholder"))}" autocomplete="off">
      <button class="run" data-act="sched-ot-submit">${esc(t("sched.onetimeSet"))}</button>
      <button class="ghost" data-act="sched-ot-close">✕</button>
    </div>`;
    const inp = $("#sched-when");
    if (inp) inp.focus();
  } else {
    form.innerHTML = "";
  }
}

function renderSchedule() {
  if (!schedData) return;
  const rows = (schedData.loops || []).map((l) => {
    const next = (l.next || []).slice(0, 3).map((x) => esc(fmtDateTime(x))).join(" · ");
    const ot = l.onetime;
    return `<div class="schedrow">
      <div class="schedrow-main">
        <span class="schedname">${esc(l.project)}/${esc(l.loop)}</span>
        <span class="badge trig">${esc(l.cron)}</span>
        ${l.effective_state !== "active" ? stateBadge({ state: l.effective_state }) : ""}
      </div>
      <div class="schednext" title="${esc(t("sched.nextTitle"))}">${esc(t("sched.next"))}: ${next || "—"}</div>
      ${l.last ? badge(statusLabel(l.last.status), l.last.status) : badge(t("detail.neverRun"), "dim")}
      ${ot ? `<span class="sched-ot" title="${esc(t("sched.onetimePendingTitle"))}">⏱ ${esc(fmtDateTime(ot.at))}${ot.note ? ` · ${esc(ot.note)}` : ""}</span>` : ""}
      <span class="schedactions">
        <button class="run" title="${esc(t("sched.runNowTitle"))}" data-act="sched-run-now" data-project="${esc(l.project)}" data-loop="${esc(l.loop)}">${esc(t("sched.runNow"))}</button>
        <button class="ghost" title="${esc(t("sched.onetimeTitle"))}" data-act="sched-ot-open" data-project="${esc(l.project)}" data-loop="${esc(l.loop)}">⏱</button>
      </span>
    </div>`;
  }).join("");
  const pending = (schedData.onetime || []).map((e) => `<div class="schedrow">
    <div class="schedrow-main"><span class="schedname">${esc(e.project)}/${esc(e.loop)}</span>
      <span class="sched-ot">⏱ ${esc(fmtDateTime(e.at))}${e.note ? ` · ${esc(e.note)}` : ""}</span></div>
    <span class="schedactions"><button class="ghost" title="${esc(t("sched.onetimeCancelTitle"))}"
      data-act="sched-ot-cancel" data-project="${esc(e.project)}" data-loop="${esc(e.loop)}">✕</button></span>
  </div>`).join("");
  $("#schedbody").innerHTML = `
    ${renderQueue()}
    <div class="sched-sec">${esc(t("sched.cronSection"))}</div>
    ${rows || `<div class="sched-empty">${esc(t("sched.noCron"))}</div>`}
    ${pending ? `<div class="sched-sec">${esc(t("sched.onetimeSection"))}</div>` + pending : ""}`;
}

function schedOtOpen(project, loop) {
  schedOtTarget = { project, loop };
  renderSchedForm();
}

function schedOtClose() {
  schedOtTarget = null;
  renderSchedForm();
}

async function schedOnetimeSubmit() {
  const when = ($("#sched-when") || { value: "" }).value;
  const target = schedOtTarget;
  if (!target) return;
  try {
    await api("/api/schedule/onetime", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ project: target.project, loop: target.loop, when }),
    });
    schedOtClose();
    await refreshSchedule();
  } catch (e) {
    alert(t("sched.onetimeFailed", { error: e.detail || e.message }));
  }
}

async function schedOnetimeCancel(project, loop) {
  try {
    await api("/api/schedule/onetime", {
      method: "DELETE",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ project, loop }),
    });
    await refreshSchedule();
  } catch (e) {
    alert(t("sched.onetimeFailed", { error: e.detail || e.message }));
  }
}

function setChat(open) {
  $("#chatpanel").classList.toggle("hidden", !open);
  document.body.classList.toggle("chat-open", open);
}
$("#chattoggle").addEventListener("click", () => setChat($("#chatpanel").classList.contains("hidden")));
$("#chatclose").addEventListener("click", () => setChat(false));
$("#schattoggle").addEventListener("click", () => setSchedule($("#schedpanel").classList.contains("hidden")));
$("#schedclose").addEventListener("click", () => setSchedule(false));
$("#kpitoggle").addEventListener("click", () => setKpis($("#kpipanel").classList.contains("hidden")));
$("#kpiclose").addEventListener("click", () => setKpis(false));
$("#chatform").addEventListener("submit", chatSend);
$("#llmselect").addEventListener("change", (e) => setLlm(e.target.value));
$("#langtoggle").addEventListener("click", toggleLang);

setInterval(() => { $("#clock").textContent = new Date().toLocaleTimeString(i18nLocale()); }, 1000);
$("#runclose").addEventListener("click", closeRun);
document.querySelectorAll("#runtabs button").forEach((b) => {
  b.addEventListener("click", () => showRunTab(b.dataset.tab));
});
$("#runbackdrop").addEventListener("click", closeRun);
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && !$("#proposalpanel").classList.contains("hidden")) closeProposal();
  else if (e.key === "Escape" && !$("#settingspanel").classList.contains("hidden")) closeSettings();
  else if (e.key === "Escape" && !$("#runpanel").classList.contains("hidden")) closeRun();
});
$("#loginform").addEventListener("submit", doLogin);
$("#logout").addEventListener("click", doLogout);
// Delegated handlers (issue #268): the containers are static in index.html,
// so one listener each suffices for all dynamically rendered [data-act] elements.
$("#projects").addEventListener("click", onProjectsClick);
$("#detail").addEventListener("click", onDetailClick);
$("#graphctx").addEventListener("click", onGraphCtxClick);
$("#runactions").addEventListener("click", onRunActionsClick);
$("#schedpanel").addEventListener("click", onSchedClick);
$("#proposalpanel").addEventListener("click", onProposalClick);
$("#proposalbackdrop").addEventListener("click", closeProposal);
$("#schedpanel").addEventListener("keydown", onSchedKeydown);
// i18n: load catalogs, resolve the language (localStorage > browser > "en") and
// apply it (static + dynamic views). The chat greeting is content —
// it only appears once the language is known.
const i18nReady = i18nLoad().then(() => {
  i18n.lang = i18nResolve();
  i18nApply();
  chatAdd("fabrikant", t("chat.greeting"));
});
// Start only after the catalogs: otherwise the first refresh() renders raw keys.
i18nReady.then(initSession).then(() => sessionReady() ? refreshLlm() : 0);
setInterval(() => i18n.ready && sessionReady() && refresh(), 5000);
