// SPDX-License-Identifier: GPL-3.0-or-later
// Copyright (C) 2026 Pedro Ayala. Rollbook is free software, see LICENSE.
"use strict";
/* App shell: history and back / forward, sidebar, enlarged panels, the router, status polling. */

// ------------------------------------------------------------------ router / boot

let lastRoute = null;
// ------------------------------------------------------------------ history
// The app window has no browser chrome, so Rollbook draws its own back /
// forward buttons. Every entry is numbered in history.state ({i, y}) so the
// buttons know whether there is anywhere to go, and y restores the scroll
// position when coming back to a page.
const Nav = { i: 0, max: 0, pop: false };
function navInit() {
  if (!history.state || history.state.i == null) history.replaceState({ i: 0 }, "");
  Nav.i = history.state.i;
  Nav.max = Math.max(Nav.i, +(sessionStorage.navMax || 0));
  // runs before route(): a hashchange without our state is a new navigation
  addEventListener("hashchange", () => {
    const st = history.state;
    if (st && st.i != null) { Nav.i = st.i; Nav.pop = true; }
    else { Nav.i++; Nav.max = Nav.i; Nav.pop = false; history.replaceState({ i: Nav.i }, ""); }
    sessionStorage.navMax = Nav.max;
    navButtons();
  });
  let t;
  addEventListener("scroll", () => { clearTimeout(t); t = setTimeout(() => history.replaceState({ ...history.state, y: scrollY }, ""), 150); }, { passive: true });
  $("#nav-back").onclick = () => Nav.i > 0 && history.back();
  $("#nav-fwd").onclick = () => Nav.i < Nav.max && history.forward();
  const typing = e => /^(INPUT|TEXTAREA|SELECT)$/.test(e.target.tagName) || e.target.isContentEditable;
  addEventListener("keydown", e => {
    if (typing(e) || $("#import-dlg").open) return;
    const back = (e.altKey && e.key === "ArrowLeft") || (e.metaKey && e.key === "[");
    const fwd = (e.altKey && e.key === "ArrowRight") || (e.metaKey && e.key === "]");
    if (back || fwd) { e.preventDefault(); back ? $("#nav-back").click() : $("#nav-fwd").click(); }
  });
  // mouse side buttons (pywebview doesn't map them to history itself)
  addEventListener("mouseup", e => { if (e.button === 3) { e.preventDefault(); $("#nav-back").click(); } if (e.button === 4) { e.preventDefault(); $("#nav-fwd").click(); } });
  navButtons();
}
// ------------------------------------------------------------------ hide / show the sidebar
function sideInit() {
  const b = $("#side-toggle");
  const set = (hidden, save) => {
    document.body.classList.toggle("side-hidden", hidden);
    const label = hidden ? "Show sidebar" : "Hide sidebar";
    b.setAttribute("aria-expanded", String(!hidden)); b.setAttribute("aria-label", label); b.title = `${label} (⌘\\ / Ctrl+\\)`;
    if (save) {
      localStorage.sideHidden = hidden ? "1" : "0";
      // charts are drawn at the width they had; redraw once the page has its new width (maps resize themselves)
      setTimeout(() => document.querySelectorAll("#app *").forEach(h => h._redraw && !h.closest(".panel.expanded") && h._redraw()), 200);
    }
  };
  set(localStorage.sideHidden === "1", false);
  b.onclick = () => set(!document.body.classList.contains("side-hidden"), true);
  addEventListener("keydown", e => {
    if (e.key === "\\" && (e.metaKey || e.ctrlKey) && !e.altKey) { e.preventDefault(); b.click(); }
  });
}
function navButtons() {
  $("#nav-back").disabled = Nav.i <= 0;
  $("#nav-fwd").disabled = Nav.i >= Nav.max;
}

// ------------------------------------------------------------------ expand a widget
// ⤢ on every panel with a chart or map: the panel goes full-window and its
// charts are redrawn taller (maps resize themselves). Esc / close / backdrop closes.
function ensureExpander(el) {
  const panel = el.closest && el.closest(".panel");
  if (!panel || panel.querySelector(":scope > .expand-btn")) return;
  const b = document.createElement("button");
  b.className = "btn small expand-btn"; b.type = "button"; b.innerHTML = ICON.expand; b.title = "Enlarge"; b.setAttribute("aria-label", "Enlarge");
  b.onclick = e => { e.stopPropagation(); panel.classList.contains("expanded") ? collapsePanel() : expandPanel(panel); };
  panel.prepend(b);
}
let expanded = null;
function redrawCharts(panel, grow, natural) {
  const hosts = [...panel.querySelectorAll("*")].filter(h => h._redraw);
  if (!grow) { hosts.forEach(h => h._redraw()); return; }
  const base = hosts.reduce((t, h) => t + (h._chartOpt.height || 260), 0) || 1;
  // non-chart content (titles, legends, notes) measured at normal size
  const other = natural - hosts.reduce((t, h) => t + (h._chartOpt.height || 260), 0);
  const f = Math.max(1, Math.min(3.5, (innerHeight - 60 - other) / base));
  hosts.forEach(h => h._redraw(Math.round((h._chartOpt.height || 260) * f)));
}
let dlgSeq = 0;
const FOCUSABLE = 'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea, [tabindex]:not([tabindex="-1"])';
/** Tab / Shift+Tab stay inside the enlarged panel (it is a modal dialog). */
function trapTab(e) {
  if (e.key !== "Tab" || !expanded) return;
  const f = [...expanded.panel.querySelectorAll(FOCUSABLE)].filter(x => x.offsetParent !== null);
  if (!f.length) return;
  const first = f[0], last = f[f.length - 1];
  if (e.shiftKey && (document.activeElement === first || !expanded.panel.contains(document.activeElement))) { e.preventDefault(); last.focus(); }
  else if (!e.shiftKey && (document.activeElement === last || !expanded.panel.contains(document.activeElement))) { e.preventDefault(); first.focus(); }
}
function expandPanel(panel) {
  collapsePanel();
  const ret = document.activeElement;
  const natural = panel.offsetHeight;
  const bd = document.createElement("div"); bd.className = "expand-backdrop"; bd.onclick = collapsePanel;
  document.body.appendChild(bd);
  panel.classList.add("expanded"); document.body.classList.add("has-expanded");
  const h = panel.querySelector("h2");
  if (h && !h.id) h.id = "dlg-title-" + ++dlgSeq;
  panel.setAttribute("role", "dialog"); panel.setAttribute("aria-modal", "true");
  if (h) panel.setAttribute("aria-labelledby", h.id);
  const x = panel.querySelector(":scope > .expand-btn");
  x.innerHTML = ICON.close; x.title = "Close"; x.setAttribute("aria-label", "Close");
  expanded = { panel, bd, ret, natural };
  document.addEventListener("keydown", trapTab);
  hideTip(); redrawCharts(panel, true, natural);
  x.focus();
}
function collapsePanel() {
  if (!expanded) return;
  const { panel, bd, ret } = expanded; expanded = null;
  document.removeEventListener("keydown", trapTab);
  bd.remove(); panel.classList.remove("expanded"); document.body.classList.remove("has-expanded");
  for (const k of ["role", "aria-modal", "aria-labelledby"]) panel.removeAttribute(k);
  if (ret && ret.isConnected && ret.focus) ret.focus();  // back where the user was
  const b = panel.querySelector(":scope > .expand-btn"); if (b) { b.innerHTML = ICON.expand; b.title = "Enlarge"; b.setAttribute("aria-label", "Enlarge"); }
  hideTip();
  if (panel._onCollapse) { panel._onCollapse(); return; }  // a details view made just for the modal
  redrawCharts(panel, false);
}
addEventListener("keydown", e => { if (e.key === "Escape" && expanded) { e.preventDefault(); collapsePanel(); } });
/** The page under an enlarged panel is being replaced (navigation, re-render): drop the modal state. */
function dropExpanded() {
  if (!expanded) return;
  document.removeEventListener("keydown", trapTab);
  expanded.bd.remove(); document.body.classList.remove("has-expanded");
  if (expanded.panel._onCollapse) expanded.panel._onCollapse();
  expanded = null;
}
addEventListener("hashchange", dropExpanded);

// cleanup for the page being left (maps, replay loops), run before every render
const Leave = [];
const onLeave = fn => Leave.push(fn);
let routeSeq = 0;  // a view checks it after each await: a newer route() means it was left
async function route() {
  dropExpanded();  // a re-render (e.g. after weather arrived) must not leave a backdrop behind
  while (Leave.length) { try { Leave.pop()(); } catch (e) { console.error(e); } }
  const seq = ++routeSeq;
  const h = location.hash.replace(/^#\/?/, "");
  const [page, arg, arg2] = h.split("/");
  const cur = page === "session" || page === "compare" ? "sessions" : page === "segment" ? "segments" : page || "home";
  document.body.dataset.page = cur;  // Home is every sport: style.css hides the sport selector there
  document.querySelectorAll("[data-nav]").forEach(a => { const on = a.dataset.nav === cur; a.classList.toggle("on", on); on ? a.setAttribute("aria-current", "page") : a.removeAttribute("aria-current"); });
  hideTip();
  try {
    if (page === "session" && /^\d+$/.test(arg || "")) await viewSession(arg);
    else if (page === "sessions") await viewSessions();
    else if (page === "map") await viewMap();
    else if (page === "segments") await viewSegments();
    else if (page === "segment" && arg) await viewSegment(arg);
    else if (page === "profile") await viewProfile();
    else if (page === "compare") await viewCompare(arg, arg2, seq);
    else if (page === "gear") await (arg ? viewGearOne(arg, seq) : viewGear(seq));
    else if (page === "progress") await viewProgress();
    else await viewHome();
    const y = history.state && history.state.y;
    if (Nav.pop && y) requestAnimationFrame(() => scrollTo(0, y));  // back/forward: where you were
    else if (lastRoute !== h) scrollTo(0, 0);
    Nav.pop = false;
    lastRoute = h;
    document.querySelectorAll("#app .smap").forEach(ensureExpander);
  } catch (e) {
    $("#app").innerHTML = `<p class="down">Error: ${esc(e.message)}</p>`;
    console.error(e);
  }
}

async function pollStatus() {
  try {
    const s = await api("status");
    $("#status").textContent = s.running ? `computing metrics ${s.done}/${s.total}…` : "";
    if (s.running) setTimeout(pollStatus, 1500);
  } catch { /* server gone */ }
}
