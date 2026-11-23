// SPDX-License-Identifier: GPL-3.0-or-later
// Copyright (C) 2026 Pedro Ayala. Rollbook is free software, see LICENSE.
"use strict";
/* Import dialog: uploads, Strava / intervals.icu sync, refresh after the data changed. */

// ------------------------------------------------------------------ import

const Imp = { files: [], busy: false };
const fsize = n => n > 1e9 ? (n / 1e9).toFixed(1) + " GB" : n > 1e6 ? (n / 1e6).toFixed(0) + " MB" : (n / 1e3).toFixed(0) + " kB";
const kindOf = f => /\.zip$/i.test(f.name) ? "export" : /\.(fit|gpx|tcx)(\.gz)?$/i.test(f.name) ? "activity" : null;

function openImport(files) {
  const dlg = $("#import-dlg");
  if (!dlg.open) dlg.showModal();
  if (!Imp.busy) {
    $("#imp-types").innerHTML = S.types.map(t => `<option value="${esc(t.type)}">`).join("");
    if (!$("#imp-sport").value) $("#imp-sport").value = isAll() ? "" : S.type || "";
  }
  if (files && files.length && !Imp.busy) addImportFiles(files);
  if (!Imp.busy) { $("#imp-prog").hidden = true; syncStatus(); }  // a finished run's progress isn't news on reopening
}
function addImportFiles(list) {
  for (const f of list) if (!Imp.files.some(g => g.name === f.name && g.size === f.size)) Imp.files.push(f);
  drawImportList();
}
function drawImportList(state = {}) {
  $("#imp-list").innerHTML = Imp.files.map((f, i) => {
    const k = kindOf(f), st = state[i];
    return `<div class="row"><span>${esc(f.name)}</span><span class="muted">${fsize(f.size)}</span>
      <span class="${k ? "muted" : "down"}">${k === "export" ? "zip (export or activity files)" : k === "activity" ? "activity file" : "unsupported"}</span>
      <span>${st ? esc(st) : Imp.busy ? "" : `<a href="#" data-rm="${i}">remove</a>`}</span></div>`;
  }).join("");
  $("#imp-list").querySelectorAll("[data-rm]").forEach(a => a.onclick = e => { e.preventDefault(); Imp.files.splice(+a.dataset.rm, 1); drawImportList(); });
  $("#imp-sport-row").hidden = !Imp.files.length;
  $("#imp-go").disabled = Imp.busy || !Imp.files.some(kindOf);
  if (Imp.busy) $("#import-dlg").querySelectorAll("#imp-sync, [data-sync]").forEach(b => b.disabled = true);
}
function upload(f, sport, onProgress) {
  return new Promise((resolve, reject) => {
    const x = new XMLHttpRequest();  // fetch() has no upload progress
    x.open("POST", `/api/import/upload?name=${encodeURIComponent(f.name)}&type=${encodeURIComponent(sport)}`);
    x.setRequestHeader("X-Rollbook", "1");
    x.setRequestHeader("Content-Type", "application/octet-stream");
    x.upload.onprogress = e => e.lengthComputable && onProgress(e.loaded / e.total);
    x.onload = () => { let j = {}; try { j = JSON.parse(x.responseText); } catch { } x.status === 200 ? resolve(j) : reject(new Error(j.error || `HTTP ${x.status}`)); };
    x.onerror = () => reject(new Error("upload failed"));
    x.send(f);
  });
}
async function runImport() {
  const files = Imp.files.filter(kindOf), sport = $("#imp-sport").value.trim();
  if (files.some(f => kindOf(f) === "activity") && !sport) { $("#imp-sport").focus(); return; }
  Imp.busy = true;
  const state = {}, bar = $("#imp-bar"), phase = $("#imp-phase"), log = $("#imp-log");
  $("#imp-prog").hidden = false; log.textContent = ""; phase.textContent = ""; bar.style.width = "0"; nrCancel($("#imp-records")); drawImportList(state);
  const total = files.reduce((s, f) => s + f.size, 0) || 1;
  let sent = 0, ok = 0;
  try {
    for (const f of files) {
      const i = Imp.files.indexOf(f);
      state[i] = "uploading…"; drawImportList(state);
      try {
        const r = await upload(f, sport, p => { bar.style.width = ((sent + p * f.size) / total * 30).toFixed(1) + "%"; phase.textContent = `Uploading ${f.name} · ${(p * 100).toFixed(0)}%`; });
        state[i] = r.duplicate ? "file already stored, checking" : r.kind === "export" ? `ok · ${r.files} files` : r.kind === "activities" ? `ok · ${r.files} activity files` : "ok"; ok++;
      } catch (e) { state[i] = "failed: " + e.message; }
      sent += f.size; drawImportList(state);
    }
    if (!ok) throw new Error("nothing could be uploaded");
    const r = await fetch("/api/import/run", { method: "POST", headers: { "X-Rollbook": "1" } });
    if (!r.ok) throw new Error((await r.json()).error || r.status);
    await finishImport(30);
    Imp.files = [];
  } catch (e) {
    phase.innerHTML = `<span class="down">Failed: ${esc(e.message)}</span>`;
  } finally {
    Imp.busy = false; drawImportList(state); syncStatus();
  }
}
/** Poll a running import job (upload or a sync) to the end; the bar goes from `base` % to 100 %. */
async function finishImport(base) {
  const bar = $("#imp-bar"), phase = $("#imp-phase"), log = $("#imp-log");
  let j;
  for (;;) {
    j = await api("import");
    const frac = j.total ? j.done / j.total : 0;
    const p = j.phase || "";
    bar.style.width = (base + (p.startsWith("importing") || p.startsWith("checking ") ? frac * (85 - base) : j.state === "running" ? 88 - base : 100 - base)).toFixed(1) + "%";
    phase.textContent = `${p}${j.total ? ` · ${j.done}/${j.total}` : ""}`;
    log.textContent = (j.log || []).join("\n"); log.scrollTop = log.scrollHeight;
    if (j.state !== "running") break;
    await sleep(800);
  }
  if (j.state === "error") {
    if (j.changed) {  // an earlier step committed rows: show them anyway
      await refreshData();
      if (j.new_total || j.replaced) await newRecordsPanel($("#imp-records"));
    }
    throw new Error(j.error);
  }
  bar.style.width = "100%";
  bar.classList.toggle("warn", Object.values(j.sources && typeof j.sources === "object" ? j.sources : {}).some(r => r && r.error));  // partly failed: not a plain success
  const dups = (j.log || []).filter(l => /skipped, (same session|already imported)/.test(l)).length;
  phase.innerHTML = syncSummary(j) + (j.added ? `<span class="up">${j.added} new activit${j.added === 1 ? "y" : "ies"} imported.</span>` : `<span class="muted">Done, nothing new: everything was already imported.</span>`) +
    (dups ? ` <span class="muted">${dups} already in Rollbook from another file (same start time and distance), skipped.</span>` : "");
  // `added` is a count: an export replacing an uploaded copy swaps ids at the same count
  if (j.added || j.changed) await refreshData();
  if (j.new_total || j.replaced) await newRecordsPanel($("#imp-records"));
  return j;
}
/** Sync sources, in the order "Sync" runs them (Strava's copy is the watch's own file, intervals.icu fills gaps):
 * label, how to set it up, what its downloads are (one, many). A Map: keys come from the server (job.source). */
const SYNC = new Map([
  ["strava", { label: "Strava", setup: "./rollbook strava auth", tip: "log into strava.com, copy the _strava4_session cookie", files: ["file downloaded", "files downloaded"] }],
  ["intervals", { label: "intervals.icu", setup: "./rollbook intervals auth", tip: "intervals.icu Settings → Developer → API key", files: ["session Strava didn't have", "sessions Strava didn't have"] }],
]);
/** "YYYY-MM-DD HH:MM", local, like dates elsewhere in the app. */
function whenLocal(iso) {
  const d = new Date(iso);
  if (isNaN(d)) return "";
  const p = n => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
}
/** One source's status line: {cls, text} from GET /api/<source>; offline: nothing (one line says it for all). */
function syncLine(c, s, offline) {
  if (!s || offline) return { cls: "muted", text: "" };
  if (!s.configured) return { cls: "muted", text: `not set up: run ${c.setup} in a terminal`, title: c.tip, off: true };
  if (s.auth_expired) return { cls: "down", text: `login expired: run ${c.setup} again (Sync skips it until then)`, title: c.tip };
  return { cls: "muted", text: s.last_sync ? `last synced ${whenLocal(s.last_sync)}` : "never synced" };
}
/** The Sync block: one button for every set-up source, a line per source with an "only this one" link. */
function syncHtml(st, offline) {
  return [...SYNC].map(([k, c]) => {
    const s = st[k], l = syncLine(c, s, offline);
    return `<div class="imp-src-row"><span>${esc(c.label)}</span><span class="small ${l.cls}"${l.title ? ` title="${esc(l.title)}"` : ""}>${esc(l.text)}</span>
      <span>${s && s.configured && !offline ? `<button class="linkbtn small" type="button" data-sync="${esc(k)}">only this one</button>` : ""}</span></div>`;
  }).join("");
}
/** What a finished sync did, per source: files fetched (green), failures (red). j: GET /api/import. */
function syncSummary(j) {
  const res = j.sources && typeof j.sources === "object" ? j.sources : {};
  return [...SYNC].filter(([k]) => Object.hasOwn(res, k)).map(([k, c]) => {
    const r = res[k] || {};
    if (r.error) return `<div><b>${esc(c.label)}</b>: <span class="down">${esc(r.error)}</span></div>`;
    const n = isNum(r.fetched) ? r.fetched : 0;
    return `<div><b>${esc(c.label)}</b>: <span class="${n ? "up" : "muted"}">${f0(n)} ${esc(c.files[n === 1 ? 0 : 1])}</span>${r.partial ? ` <span class="muted">(stopped at its time limit; sync again to continue)</span>` : ""}</div>`;
  }).join("");
}
async function syncStatus() {
  const st = {};
  await Promise.all([...SYNC.keys()].map(async k => { try { st[k] = await api(k); } catch { st[k] = null; } }));
  const offline = Object.values(st).some(s => s && s.offline);
  const ready = [...SYNC].filter(([k]) => st[k] && st[k].configured && !st[k].auth_expired).map(([, c]) => c.label);
  $("#imp-src").innerHTML = syncHtml(st, offline);
  $("#imp-src").querySelectorAll("[data-sync]").forEach(b => { b.disabled = Imp.busy; b.onclick = () => runSync(b.dataset.sync); });
  $("#imp-sync").disabled = Imp.busy || offline || !ready.length;
  const msg = $("#imp-sync-st");
  msg.textContent = offline ? "off: rollbook runs with --offline" : ready.length > 1 ? "Strava first, then what only intervals.icu has"
    : ready.length ? `${ready[0]} only` : Object.values(st).some(s => s && s.configured) ? "log in again first (below)" : "no source set up yet";
  msg.className = "small muted";
}
/** Sync one source, or k = "all": every set-up one, Strava first. */
async function runSync(k) {
  const phase = $("#imp-phase");
  phase.tabIndex = -1; phase.focus();  // the button about to be disabled had focus: keep it in the dialog, on the status
  Imp.busy = true; drawImportList();
  $("#imp-prog").hidden = false; $("#imp-log").textContent = ""; nrCancel($("#imp-records"));
  phase.textContent = k === "all" ? "contacting Strava and intervals.icu…" : `contacting ${SYNC.get(k).label}…`; $("#imp-bar").style.width = "0";
  try {
    const r = await fetch(k === "all" ? "/api/sync" : `/api/${k}/sync`, { method: "POST", headers: { "X-Rollbook": "1" } });
    if (!r.ok) throw new Error((await r.json()).error || r.status);
    await finishImport(0);
  } catch (e) {
    phase.innerHTML = `<span class="down">Failed: ${esc(e.message)}</span>`;
  } finally {
    Imp.busy = false; drawImportList();
    await syncStatus();
    if (document.activeElement === phase || document.activeElement === document.body) ($("#imp-sync").disabled ? phase : $("#imp-sync")).focus();
  }
}
/** Drop every client cache and redraw, after the DB (or its edits) changed underneath us.
 * With `dest`, navigate there instead of redrawing the current page. */
async function refreshData(dest) {
  S.acts = {}; S.tracks = {}; S.segs = {}; S.segsP = {}; S.lastImport = null; S.mapView = {};
  [S.types, , S.favs] = await Promise.all([api("types"), loadHrmax(), api("favorites"), loadGear(true).catch(() => null)]);
  fillSports();
  pollStatus();
  if (dest && location.hash !== dest) location.hash = dest;  // hashchange routes
  else route();
}

function initImport() {
  $("#import-btn").onclick = () => openImport();
  $("#imp-close").onclick = () => $("#import-dlg").close();
  $("#imp-file").onchange = e => { addImportFiles(e.target.files); e.target.value = ""; };
  $("#imp-go").onclick = runImport;
  $("#imp-sync").onclick = () => runSync("all");
  const drop = $("#imp-drop");  // a label around a hidden input: give it a keyboard way in
  drop.tabIndex = 0;
  drop.addEventListener("keydown", e => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); $("#imp-file").click(); } });
  $("#import-dlg").addEventListener("cancel", e => { if (Imp.busy) e.preventDefault(); });
  // drag files anywhere onto the page
  let depth = 0;
  const hasFiles = e => [...(e.dataTransfer?.types || [])].includes("Files");
  addEventListener("dragenter", e => { if (!hasFiles(e)) return; e.preventDefault(); if (++depth === 1 && !$("#import-dlg").open) document.body.classList.add("dragging"); });
  addEventListener("dragleave", e => { if (!hasFiles(e)) return; if (--depth <= 0) { depth = 0; document.body.classList.remove("dragging"); } });
  addEventListener("dragover", e => { if (hasFiles(e)) e.preventDefault(); });
  addEventListener("drop", e => { if (!hasFiles(e)) return; e.preventDefault(); depth = 0; document.body.classList.remove("dragging"); openImport(e.dataTransfer.files); });
}
