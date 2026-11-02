// SPDX-License-Identifier: GPL-3.0-or-later
// Copyright (C) 2026 Pedro Ayala. Rollbook is free software, see LICENSE.
// Gear (#10): parts (wheels, frame, boots, shoes, chain…) make up a gear; a
// session uses its own choice or its sport's default. Stored server-side in
// data/gear.json (web/gear.py); every change is a POST /api/gear {op, rev}.
// Loaded before app.js (whose boot() calls loadGear): only declarations here, run later with app.js's globals.
"use strict";

// ------------------------------------------------------------------ pure helpers (tested under node)

/** "YYYY-MM-DD" of a Date in local time. */
function isoDay(d = new Date()) {
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
}
/** One line of a part's specs, e.g. "90 mm · 85A · ×8", in the kind's own field order. */
function specLine(kinds, p) {
  const k = kinds && kinds[p.kind];
  if (!k) return "";
  const out = [];
  for (const s of k.specs) {
    const v = (p.specs || {})[s.k];
    if (v == null || v === "") continue;
    if (s.k === "count") out.push(`×${v}`);
    else if (s.k === "speeds") out.push(`${v}-speed`);
    else if (s.unit === "A") out.push(`${v}A`);
    else if (s.unit) out.push(`${v} ${s.unit}`);
    else if (s.k === "size_eu") out.push(`EU ${v}`);
    else out.push(String(v));
  }
  return out.join(" · ");
}
const SERVICE_WARN = 0.9;  // share of the interval from which a part shows amber
/** Service state from the server's totals (it decides `due`): {cls: "" | "warn" | "over" | "none", text, frac}. */
function serviceState(p, t) {
  if (!p.service_km || !t) return { cls: "none", text: "–", frac: 0 };
  const since = t.km_since_service || 0, frac = since / p.service_km;
  if (p.retired) return { cls: "none", text: `${Math.round(since)} km`, frac: 0 };
  if (t.due) return { cls: "over", text: t.service_left < -0.5 ? `${Math.round(-t.service_left)} km over` : "due", frac };
  return { cls: frac >= SERVICE_WARN ? "warn" : "", text: `${Math.round(t.service_left)} km left`, frac };
}
/** Sessions-list gear filter: "" = all, "none" = no gear, else a gear id. */
function gearFilterMatch(a, f) {
  if (!f) return true;
  if (f === "none") return !a.gear_id;
  return a.gear_id === f;
}
/** Which gear a sport offers in pickers: not retired and listing the sport first, then the rest. */
function gearFor(gearMap, sport) {
  const all = [...gearMap.entries()].map(([id, g]) => ({ id, ...g }));
  const mine = all.filter(g => (g.sports || []).includes(sport) && !g.retired);
  return { mine, other: all.filter(g => !mine.includes(g)) };
}
/** Parts in the order the page lists them: due first, then near, then by kind and name. */
function partOrder(kindKeys, P, T) {
  const rank = id => { const st = serviceState(P.get(id), T.get(id)); return st.cls === "over" ? 0 : st.cls === "warn" ? 1 : 2; };
  return ids => [...ids].sort((a, b) => rank(a) - rank(b) || kindKeys.indexOf(P.get(a).kind) - kindKeys.indexOf(P.get(b).kind)
    || P.get(a).name.localeCompare(P.get(b).name));
}

// ------------------------------------------------------------------ data

/** The store as Maps (ids and sports are keys: never index plain objects with them). */
function gearIndex(v) {
  return { ...v, G: new Map(Object.entries(v.gear || {})), P: new Map(Object.entries(v.parts || {})),
    D: new Map(Object.entries(v.defaults || {})), SG: new Map(Object.entries(v.sport_group || {})),
    T: { gear: new Map(Object.entries((v.totals || {}).gear || {})), parts: new Map(Object.entries((v.totals || {}).parts || {})) } };
}
async function loadGear(force) {
  if (!S.gear || force) S.gear = gearIndex(await api("gear"));
  document.body.classList.toggle("gear-ro", !!S.gear.readonly_store);
  gearBadge();
  return S.gear;
}
// ops that change which gear a session shows (the session lists carry gear_id / gear_name)
const LIST_OPS = new Set(["assign", "default.set", "gear.save", "gear.delete", "gear.retire"]);
async function postGear(body) {
  const r = await fetch("/api/gear", { method: "POST", headers: { "X-Rollbook": "1", "Content-Type": "application/json" },
    body: JSON.stringify({ ...body, rev: S.gear ? S.gear.rev : -1 }) });
  const j = await r.json().catch(() => ({}));
  if (!r.ok) {
    if (r.status === 409 && /reload/.test(j.error || "")) await loadGear(true).catch(() => null);
    throw new Error(j.error || `HTTP ${r.status}`);
  }
  S.gear = gearIndex(j.gear);
  if (LIST_OPS.has(body.op)) S.acts = {};
  gearBadge();
  return j;
}
function gearBadge() {
  const b = $("#gear-badge"), n = S.gear ? (S.gear.due || []).length : 0;
  if (!b) return;
  b.hidden = !n;
  b.textContent = n;
  b.title = `${n} part${n === 1 ? "" : "s"} due for service`;
  const a = b.closest("a");
  if (a) a.setAttribute("aria-label", n ? `Gear, ${b.title}` : "Gear");
}
const kindLabel = k => (S.gear && S.gear.kinds[k] || { label: k }).label;
/** Where a part is now: {gid, from} of its open mount, or null (spare / retired). */
function partOn(id) {
  for (const [gid, g] of S.gear.G) for (const m of g.parts) if (m.part === id && !m.to) return { gid, from: m.from };
  return null;
}

// ------------------------------------------------------------------ dialogs

/** A modal form. fields: HTML of .pfield rows; submit(form) -> promise (throw to show the error);
 * extra: HTML after the form (a danger zone). Returns the dialog. */
function gearDialog(title, fields, submit, extra = "") {
  const dlg = document.createElement("dialog");
  dlg.className = "dlg";
  dlg.innerHTML = `<div class="dlg-head"><h2>${esc(title)}</h2><button class="iconbtn" data-x aria-label="Close" title="Close">${ICON.close}</button></div>
    <form class="pform">${fields}<div class="toolbar"><button class="btn primary" type="submit">Save</button><span class="small" data-msg role="status"></span></div></form>${extra}`;
  document.body.append(dlg);
  dlg.querySelector("[data-x]").onclick = () => dlg.close();
  dlg.addEventListener("close", () => dlg.remove());
  dlg.querySelector("form").onsubmit = ev => { ev.preventDefault(); dlgRun(dlg, () => submit(dlg.querySelector("form"), dlg)); };
  dlg.showModal();
  const first = dlg.querySelector("input,select");
  if (first) first.focus();
  return dlg;
}
/** Run a change from a dialog: on success close it and redraw (unless the hash changed, which routes). */
async function dlgRun(dlg, fn) {
  const msg = dlg.querySelector("[data-msg]"), h = location.hash;
  msg.className = "small muted"; msg.textContent = "saving…";
  try { await fn(); dlg.close(); if (location.hash === h) route(); }
  catch (e) { msg.className = "small down"; msg.textContent = "Could not save: " + e.message; }
}
const fld = (label, inner, help = "") => `<label class="pfield"><span>${esc(label)}</span>${inner}${help ? `<small>${esc(help)}</small>` : ""}</label>`;
const val = (f, n) => { const e = f.elements[n]; return e ? e.value.trim() : ""; };
const numOrNull = s => s === "" ? null : +s;
const danger = (what, btn, attr) => `<div class="danger-zone gear-w"><div class="small">${what}</div><div class="toolbar">${btn.map(([l, a, c]) => `<button class="btn ${c || ""}" type="button" ${a}>${esc(l)}</button>`).join("")}</div></div>`;

function specFields(kind, specs = {}) {
  const k = S.gear.kinds[kind];
  if (!k || !k.specs.length) return "";
  return `<div class="subhead">${esc(k.label)}</div>` + k.specs.map(s => {
    const v = specs[s.k] ?? "";
    if (s.type === "choice")
      return fld(s.label, `<select name="spec_${esc(s.k)}"><option value=""></option>${s.options.map(o => `<option ${o === v ? "selected" : ""}>${esc(o)}</option>`).join("")}</select>`);
    if (s.type === "text")
      return fld(s.label, `<input name="spec_${esc(s.k)}" maxlength="${s.max}" value="${esc(v)}">`);
    return fld(s.label + (s.unit ? ` (${s.unit})` : ""), `<input name="spec_${esc(s.k)}" type="number" inputmode="decimal" min="${s.min}" max="${s.max}" step="${s.type === "int" ? 1 : "any"}" value="${esc(v)}">`, `${s.min}–${s.max}`);
  }).join("");
}
function readSpecs(f, kind) {
  const out = {};
  for (const s of S.gear.kinds[kind].specs) {
    const v = val(f, "spec_" + s.k);
    out[s.k] = v === "" ? null : s.type === "num" || s.type === "int" ? +v : v;
  }
  return out;
}

/** New or edit a part. opts.gear: fit a new one straight to that gear (and order kinds by its sport). */
function openPartDialog(id, opts = {}) {
  const p = id ? S.gear.P.get(id) : null;
  const og = opts.gear && S.gear.G.get(opts.gear), sport = og && og.sports[0] || S.type;
  const group = S.gear.SG.get(sport) || "other";
  const kinds = Object.entries(S.gear.kinds).sort(([, a], [, b]) => (b.group === group) - (a.group === group));
  const kind0 = p ? p.kind : kinds[0][0];
  const gears = gearFor(S.gear.G, sport);
  const gearOpts = [...gears.mine, ...gears.other.filter(g => !g.retired)].map(g => `<option value="${esc(g.id)}" ${g.id === opts.gear ? "selected" : ""}>${esc(g.name)}</option>`).join("");
  const fields = fld("Kind", `<select name="kind">${kinds.map(([k, v]) => `<option value="${esc(k)}" ${k === kind0 ? "selected" : ""}>${esc(v.label)}</option>`).join("")}</select>`)
    + fld("Name", `<input name="name" maxlength="120" required value="${esc(p ? p.name : "")}">`)
    + fld("Brand / model", `<input name="brand" maxlength="120" value="${esc(p ? p.brand : "")}">`)
    + (p ? "" : fld("Fitted to", `<select name="mount_on"><option value="">spare (not fitted)</option>${gearOpts}</select>`)
      + fld("Installed", `<input name="installed" type="date" value="${isoDay()}">`, "sessions on the gear from this day count for the part"))
    + fld("Bought", `<input name="bought" type="date" value="${esc(p ? p.bought || "" : "")}">`)
    + fld("Km already on it", `<input name="km_offset" type="number" min="0" max="1000000" step="any" value="${esc(p ? p.km_offset || "" : "")}">`, "for a used part, before Rollbook saw it; counts until its first service")
    + fld("Service every (km)", `<input name="service_km" type="number" min="1" max="100000" step="any" value="${esc(p ? p.service_km || "" : "")}">`, "e.g. rotate wheels every 200 km; empty = no reminder")
    + `<div data-specs>${specFields(kind0, p ? p.specs : {})}</div>`
    + fld("Notes", `<input name="notes" maxlength="2000" value="${esc(p ? p.notes : "")}">`);
  const extra = p ? danger(`<b>Retire</b> <span class="muted">takes it off its gear and out of the reminders; its history stays.</span>
      <b>Delete</b> <span class="muted">only a part that was never fitted.</span>`,
    [[p.retired ? "Un-retire" : "Retire…", "data-ret"], ["Delete part", "data-del", "danger"]]) : "";
  const dlg = gearDialog(p ? `Edit ${p.name}` : "New part", fields, async f => {
    const kind = val(f, "kind");
    const body = { op: "part.save", id: id || null, kind, name: val(f, "name"), brand: val(f, "brand"), bought: val(f, "bought") || null,
      km_offset: numOrNull(val(f, "km_offset")) || 0, service_km: numOrNull(val(f, "service_km")), notes: val(f, "notes"), specs: readSpecs(f, kind) };
    if (!p && val(f, "mount_on")) Object.assign(body, { mount_on: val(f, "mount_on"), installed: val(f, "installed") });
    await postGear(body);
  }, extra);
  const f = dlg.querySelector("form");
  f.elements.kind.onchange = () => { dlg.querySelector("[data-specs]").innerHTML = specFields(f.elements.kind.value, {}); };
  if (p) {
    dlg.querySelector("[data-ret]").onclick = () => p.retired ? dlgRun(dlg, () => postGear({ op: "part.retire", id, undo: true }))
      : (dlg.close(), openRetireDialog("part", id, p.name));
    dlg.querySelector("[data-del]").onclick = () => confirm(`Delete ${p.name}?`) && dlgRun(dlg, () => postGear({ op: "part.delete", id }));
  }
}

function openGearDialog(id) {
  const g = id ? S.gear.G.get(id) : null;
  const sports = [...new Set([...S.types.map(t => t.type), ...(g ? g.sports : [])])];
  const on = new Set(g ? g.sports : [S.type]);
  const fields = fld("Name", `<input name="name" maxlength="120" required value="${esc(g ? g.name : "")}">`, "e.g. Powerslide 3×110, Inov-8 ParkClaw")
    + `<fieldset class="pfield"><legend>Sports</legend><div class="chips">${sports.map(s => `<label class="small"><input type="checkbox" name="sport" value="${esc(s)}" ${on.has(s) ? "checked" : ""}> ${esc(s)}</label>`).join("")}</div></fieldset>`
    + (g ? "" : fld("Default from", `<input name="default_since" type="date" value="">`, "make it the default for these sports from this day (older sessions keep theirs); empty = not the default"))
    + fld("Notes", `<input name="notes" maxlength="2000" value="${esc(g ? g.notes : "")}">`);
  const extra = g ? danger(`<b>Retire</b> <span class="muted">takes its parts off and ends it as a default; its history stays.</span>
      <b>Delete</b> <span class="muted">only gear no session uses that was never a default.</span>`,
    [[g.retired ? "Un-retire" : "Retire…", "data-ret"], ["Delete gear", "data-del", "danger"]]) : "";
  const dlg = gearDialog(g ? `Edit ${g.name}` : "New gear", fields, async f => {
    const sp = [...f.querySelectorAll("input[name=sport]:checked")].map(x => x.value);
    const j = await postGear({ op: "gear.save", id: id || null, name: val(f, "name"), sports: sp, notes: val(f, "notes"),
      default_since: g ? null : val(f, "default_since") || null });
    if (!g) location.hash = "#/gear/" + j.id;
  }, extra);
  if (g) {
    dlg.querySelector("[data-ret]").onclick = () => g.retired ? dlgRun(dlg, () => postGear({ op: "gear.retire", id, undo: true }))
      : (dlg.close(), openRetireDialog("gear", id, g.name));
    dlg.querySelector("[data-del]").onclick = () => confirm(`Delete ${g.name}?`) && dlgRun(dlg, async () => { await postGear({ op: "gear.delete", id }); location.hash = "#/gear"; });
  }
}

function openServiceDialog(id) {
  const p = S.gear.P.get(id);
  gearDialog(`Log service · ${p.name}`,
    fld("Date", `<input name="date" type="date" required value="${isoDay()}">`)
    + fld("What", `<select name="action">${S.gear.actions.map(a => `<option>${esc(a)}</option>`).join("")}</select>`, "the reminder counts again from this day")
    + fld("Note", `<input name="note" maxlength="500">`),
    f => postGear({ op: "part.service", id, date: val(f, "date"), action: val(f, "action"), note: val(f, "note") }));
}
function openRetireDialog(kind, id, name) {
  gearDialog(`Retire ${name}`, fld("Retired on", `<input name="date" type="date" required value="${isoDay()}">`,
    kind === "part" ? "it comes off its gear on this day" : "its parts come off and it is no longer a default from this day"),
    f => postGear({ op: kind + ".retire", id, date: val(f, "date") }));
}
function openDefaultDialog(sport) {
  const h = S.gear.D.get(sport) || [], cur = h[h.length - 1], { mine, other } = gearFor(S.gear.G, sport);
  gearDialog(`Default gear · ${sport}`,
    fld("Gear", `<select name="gear"><option value="">none</option>${[...mine, ...other.filter(x => !x.retired)].map(x => `<option value="${esc(x.id)}" ${cur && cur.gear === x.id ? "selected" : ""}>${esc(x.name)}</option>`).join("")}</select>`)
    + fld("From", `<input name="since" type="date" required value="${isoDay()}">`, "sessions from this day get it; older ones keep theirs. Pick an early date to set it for past sessions too."),
    f => postGear({ op: "default.set", sport, gear: val(f, "gear") || null, since: val(f, "since") }));
}
/** Fit a spare part (index null) or change / delete one fitting. */
function openMountDialog(gid, index) {
  const g = S.gear.G.get(gid), m = index != null ? g.parts[index] : null;
  const spare = [...S.gear.P.entries()].filter(([pid, p]) => !p.retired && !partOn(pid));
  const fields = (m ? fld("Part", `<span>${esc(S.gear.P.get(m.part).name)}</span>`)
    : fld("Part", `<select name="part" required>${spare.map(([pid, p]) => `<option value="${esc(pid)}">${esc(kindLabel(p.kind))}: ${esc(p.name)}</option>`).join("")}</select>`,
      spare.length ? "spare parts; a part on another gear must come off it first" : "no spare parts: add one with “New part…” below"))
    + fld("Installed", `<input name="from" type="date" required value="${esc(m ? m.from : isoDay())}">`)
    + fld("Removed", `<input name="to" type="date" value="${esc(m ? m.to || "" : "")}">`, "empty = still on");
  const extra = m ? danger(`<b>Delete from history</b> <span class="muted">as if it was never on this gear: its km here stop counting. To take it off, set Removed.</span>`,
    [["Delete from history", "data-mdel", "danger"]]) : `<div class="toolbar gear-w"><button class="linkbtn" type="button" data-pnew>New part…</button></div>`;
  const dlg = gearDialog(m ? `${S.gear.P.get(m.part).name} on ${g.name}` : `Fit a part to ${g.name}`, fields, f => postGear(m
    ? { op: "mount.set", gear: gid, index, from: val(f, "from"), to: val(f, "to") || null }
    : { op: "mount.add", gear: gid, part: val(f, "part"), from: val(f, "from"), to: val(f, "to") || null }), extra);
  if (m) dlg.querySelector("[data-mdel]").onclick = () => confirm("Delete this fitting from the gear's history?") && dlgRun(dlg, () => postGear({ op: "mount.delete", gear: gid, index }));
  else dlg.querySelector("[data-pnew]").onclick = () => { dlg.close(); openPartDialog(null, { gear: gid }); };
}

// ------------------------------------------------------------------ pages

/** Service cell: a bar and its text in the state's colour (one mark per row). */
function svcCell(p, t) {
  const st = serviceState(p, t);
  if (st.cls === "none") return `<span class="muted">${esc(st.text)}</span>`;
  return `<span class="svc ${st.cls}" aria-hidden="true"><i style="width:${Math.min(100, st.frac * 100).toFixed(0)}%"></i></span> <span class="small svc-t ${st.cls}">${esc(st.text)}</span>`;
}
const svcTitle = (p, t) => p.service_km ? `${Math.round(t.km_since_service || 0)} of ${p.service_km} km since ${t.last_service || "installed"}` : "no service interval set";
const kindName = p => `<span class="muted">${esc(kindLabel(p.kind))}</span> `;

function partRows(ids) {
  const K = S.gear.kinds;
  return partOrder(Object.keys(K), S.gear.P, S.gear.T.parts)(ids).map(id => {
    const p = S.gear.P.get(id), t = S.gear.T.parts.get(id) || {}, on = partOn(id);
    const where = on ? `<a href="#/gear/${esc(on.gid)}">${esc(S.gear.G.get(on.gid).name)}</a> <span class="muted small">since ${esc(on.from)}</span>`
      : `<span class="muted">${p.retired ? "retired " + esc(p.retired) : "spare"}</span>`;
    return `<tr class="${p.retired ? "hiddenrow" : ""}"><td class="l">${kindName(p)}<button class="linkbtn plink" data-pedit="${esc(id)}" aria-label="Edit ${esc(p.name)}">${esc(p.name)}</button>${p.brand ? ` <span class="muted">${esc(p.brand)}</span>` : ""}<div class="muted small">${esc(specLine(K, p))}</div></td>
      <td class="l">${where}</td><td>${fkm(t.km_total)}</td><td>${t.sessions || 0}</td><td class="l svccell" title="${esc(svcTitle(p, t))}">${svcCell(p, t)}</td>
      <td class="l acts">${p.service_km && !p.retired ? `<button class="linkbtn gear-w" data-svc="${esc(id)}" aria-label="Log service, ${esc(p.name)}">Log service</button>` : ""}</td></tr>`;
  }).join("");
}
function wirePartButtons(root) {
  root.querySelectorAll("[data-svc]").forEach(b => b.onclick = () => openServiceDialog(b.dataset.svc));
  root.querySelectorAll("[data-pedit]").forEach(b => b.onclick = () => openPartDialog(b.dataset.pedit));
}
const W = (html, ro = "") => `<span class="gear-w">${html}</span>${ro ? `<span class="gear-r">${ro}</span>` : ""}`;

async function viewGear(seq) {
  const app = $("#app");
  const G = await loadGear(true);
  if (seq !== routeSeq) return;
  const showRetired = localStorage.gearRetired === "1";
  const gears = [...G.G.entries()].filter(([, g]) => showRetired || !g.retired)
    .sort(([a], [b]) => ((G.T.gear.get(b) || {}).last || "").localeCompare((G.T.gear.get(a) || {}).last || ""));
  const nRet = [...G.G.values()].filter(g => g.retired).length + [...G.P.values()].filter(p => p.retired).length;
  const parts = [...G.P.keys()].filter(id => showRetired || !G.P.get(id).retired);
  const due = G.due || [];
  const finding = due.length ? `${due.length} part${due.length > 1 ? "s" : ""} due for service: ` + due.slice(0, 3).map(id =>
      `<button class="linkbtn" data-svc="${esc(id)}" aria-label="Log service, ${esc(G.P.get(id).name)}">${esc(G.P.get(id).name)}</button>`).join(", ") + (due.length > 3 ? "…" : "")
    : G.P.size ? "All parts within their service interval." : "";
  const dueOf = gid => G.G.get(gid).parts.filter(m => !m.to && due.includes(m.part)).length;
  const curParts = g => g.parts.filter(m => !m.to).map(m => G.P.get(m.part)).map(p => `${kindLabel(p.kind).toLowerCase()} ${p.name}`).join(", ");
  const sports = [...new Set([...G.D.keys(), ...[...G.G.values()].filter(g => !g.retired).flatMap(g => g.sports)])];
  const defRow = s => {
    const h = G.D.get(s) || [], cur = h[h.length - 1], g = cur && cur.gear && G.G.get(cur.gear);
    return `<tr><td class="l">${esc(s)}</td><td class="l">${g ? `<a href="#/gear/${esc(cur.gear)}">${esc(g.name)}</a>` : `<span class="muted">none</span>`}${cur ? ` <span class="muted small">since ${esc(cur.since)}</span>` : ""}</td>
      <td class="l acts"><button class="linkbtn gear-w" data-dset="${esc(s)}" aria-label="Change the default gear for ${esc(s)}">Change</button></td></tr>`;
  };
  app.innerHTML = `<div class="head"><div><h1>Gear</h1><div class="muted">${finding}</div></div>
      <div class="headnav gear-w"><button class="btn" id="gg-new">New gear</button><button class="btn" id="gp-new">New part</button></div></div>
    ${G.error ? `<div class="panel hiddenbar"><span>${esc(G.error)}</span></div>` : ""}
    <div class="panel"><h2>Gear <span class="muted">${G.G.size}</span></h2>
      ${nRet ? `<div class="toolbar"><label class="muted small"><input type="checkbox" id="g-showret" ${showRetired ? "checked" : ""}> show retired gear and parts (${nRet})</label></div>` : ""}
      ${gears.length ? `<div class="scroll"><table class="gtbl"><thead><tr><th class="l">gear</th><th>km</th><th>time</th><th>sessions</th><th>last used</th></tr></thead><tbody>
        ${gears.map(([id, g]) => { const t = G.T.gear.get(id) || {}, n = dueOf(id); return `<tr class="click${g.retired ? " hiddenrow" : ""}" data-gid="${esc(id)}">
          <td class="l"><a href="#/gear/${esc(id)}"><b>${esc(g.name)}</b></a>${g.retired ? ` <span class="pill">retired</span>` : ""}${n ? ` <span class="pill pill-neg">${n} part${n > 1 ? "s" : ""} due</span>` : ""}
            <div class="muted small">${esc((g.sports || []).join(", "))}${g.parts.some(m => !m.to) ? " · " + esc(curParts(g)) : ""}</div></td>
          <td>${t.sessions ? fkm(t.km) : "–"}</td><td>${t.sessions ? hours(t.moving_s) : "–"}</td><td>${t.sessions || 0}</td><td>${t.last ? esc(t.last) : "–"}</td></tr>`; }).join("")}</tbody></table></div>`
      : `<p class="muted">No gear yet.${W(" Add your skates, bike or shoes with <b>New gear</b>, then fit their parts (wheels, frame, boots, tyres…) to count the km on each.")}</p>`}
    </div>
    <div class="panel"><h2>Parts <span class="muted">${parts.length}</span></h2>
      ${parts.length ? `<div class="scroll"><table class="gparts gp-main"><thead><tr><th class="l">part</th><th class="l">fitted to</th><th>km</th><th>sessions</th><th class="l">service</th><th><span class="vh">actions</span></th></tr></thead><tbody>${partRows(parts)}</tbody></table></div>`
        : `<p class="muted">No parts yet.${W(" Add wheels to track rotation, shoes to know when to replace them.")}</p>`}</div>
    <div class="panel"><h2>Default gear per sport</h2><p class="muted small">New sessions of a sport get its default from the day it is set; older sessions keep theirs.${W(" Change one session's gear in its <b>Edit</b>.")}</p>
      ${sports.length ? `<div class="scroll gdefs"><table><thead><tr><th class="l">sport</th><th class="l">default</th><th><span class="vh">actions</span></th></tr></thead><tbody>${sports.map(defRow).join("")}</tbody></table></div>`
        : `<p class="muted small">Set a gear's sports to choose it as a default.</p>`}</div>`;
  if ($("#g-showret")) $("#g-showret").onchange = e => { localStorage.gearRetired = e.target.checked ? "1" : "0"; route(); };
  $("#gg-new").onclick = () => openGearDialog(null);
  $("#gp-new").onclick = () => openPartDialog(null);
  app.querySelectorAll("tr[data-gid]").forEach(tr => tr.onclick = e => { if (!e.target.closest("a")) location.hash = "#/gear/" + tr.dataset.gid; });
  app.querySelectorAll("[data-dset]").forEach(b => b.onclick = () => openDefaultDialog(b.dataset.dset));
  wirePartButtons(app);
}

async function viewGearOne(id, seq) {
  const app = $("#app");
  const G = await loadGear(true);
  if (seq !== routeSeq) return;
  const g = G.G.get(id);  // looked up, never put into a selector or the page as given
  if (!g) { app.innerHTML = `<p class="down">No such gear.</p><p><a href="#/gear">All gear</a></p>`; return; }
  const t = G.T.gear.get(id) || {}, recent = t.recent || [];
  const defs = [...G.D.entries()].filter(([, h]) => h.length && h[h.length - 1].gear === id).map(([s]) => s);
  const today = isoDay();
  const mounts = g.parts.map((m, i) => ({ ...m, i })).sort((a, b) => (!!a.to - !!b.to) || b.from.localeCompare(a.from));
  app.innerHTML = `<div class="head"><div><div class="small"><a class="muted" href="#/gear">Gear</a> <span class="muted">/</span></div><h1>${esc(g.name)}</h1>
        <div class="muted">${esc((g.sports || []).join(", ") || "no sport")}${defs.length ? ` · default for ${esc(defs.join(", "))}` : ""}${g.retired ? ` · retired ${esc(g.retired)}` : ""}</div></div>
      <div class="headnav gear-w"><button class="btn" id="m-add">Fit a part</button><button class="btn" id="g-edit">Edit</button></div></div>
    ${cards([{ k: "Distance", v: fkm(t.km) + " km" }, { k: "Time", v: hours(t.moving_s) }, { k: "Sessions", v: String(t.sessions || 0) },
      { k: "Last used", v: t.last ? esc(t.last) : "–", s: t.first ? `first ${esc(t.first)}` : "" }])}
    ${g.notes ? `<p class="muted">${esc(g.notes)}</p>` : ""}
    <div class="panel"><h2>Parts</h2>
      ${mounts.length ? `<div class="scroll"><table class="gparts"><thead><tr><th class="l">part</th><th class="l">installed</th><th class="l">removed</th><th>km total</th><th class="l">service</th><th><span class="vh">actions</span></th></tr></thead><tbody>
        ${mounts.map(m => { const p = G.P.get(m.part), pt = G.T.parts.get(m.part) || {}; return `<tr class="${m.to ? "hiddenrow" : ""}">
          <td class="l">${kindName(p)}<button class="linkbtn plink" data-pedit="${esc(m.part)}" aria-label="Edit ${esc(p.name)}">${esc(p.name)}</button><div class="muted small">${esc(specLine(G.kinds, p))}</div></td>
          <td class="l">${esc(m.from)}</td><td class="l">${m.to ? esc(m.to) : `<span class="muted">still on</span>`}</td><td>${fkm(pt.km_total)}</td>
          <td class="l svccell" title="${esc(svcTitle(p, pt))}">${svcCell(p, pt)}</td>
          <td class="l acts">${!m.to && m.from < today ? `<button class="linkbtn gear-w" data-off="${m.i}" aria-label="Take ${esc(p.name)} off today">Take off</button> ` : ""}${!m.to && p.service_km ? `<button class="linkbtn gear-w" data-svc="${esc(m.part)}" aria-label="Log service, ${esc(p.name)}">Log service</button> ` : ""}<button class="linkbtn gear-w" data-mdates="${m.i}" aria-label="Edit dates of ${esc(p.name)}">Edit dates</button></td></tr>`; }).join("")}</tbody></table></div>`
        : `<p class="muted">No parts on it yet.${W(" <b>Fit a part</b> to count the km on its wheels, frame, boots…")}</p>`}
    </div>
    <div class="panel"><h2>Latest sessions on it <span class="muted">${t.sessions || 0}</span></h2>
      ${recent.length ? `<div class="scroll gsess"><table><thead><tr><th class="l">date</th><th class="l">name</th><th class="l">sport</th><th>km</th><th class="l"></th></tr></thead><tbody>
        ${recent.map(a => `<tr class="click" data-id="${esc(a.id)}"><td class="l">${a.date ? fdate(new Date(a.date + "T12:00:00")) : "–"}</td><td class="l"><a href="#/session/${esc(a.id)}">${esc(a.name)}</a></td><td class="l muted">${esc(a.type)}</td><td>${fkm(a.km)}</td><td class="l muted small">${a.src === "explicit" ? "chosen" : ""}</td></tr>`).join("")}</tbody></table></div>
        ${(g.sports || []).length && t.sessions > recent.length ? `<p><button class="linkbtn" id="g-all">All ${t.sessions} in Sessions</button></p>` : ""}`
        : `<p class="muted">No session uses it yet.${W(" Make it a sport's default on the <a href=\"#/gear\">Gear</a> page, or pick it in a session's <b>Edit</b>.")}</p>`}
    </div>`;
  $("#g-edit").onclick = () => openGearDialog(id);
  $("#m-add").onclick = () => openMountDialog(id);
  if ($("#g-all")) $("#g-all").onclick = () => {
    const sp = g.sports.find(s => S.types.some(x => x.type === s));
    if (!sp) return;
    S.type = localStorage.type = sp; $("#type").value = sp;
    localStorage["gearFilter:" + sp] = id;
    location.hash = "#/sessions";
  };
  app.querySelectorAll("tr[data-id]").forEach(tr => tr.onclick = e => { if (!e.target.closest("a")) go(tr.dataset.id); });
  app.querySelectorAll("[data-off]").forEach(b => b.onclick = async () => {
    try { await postGear({ op: "mount.set", gear: id, index: +b.dataset.off, to: isoDay() }); route(); } catch (e) { alert("Could not save: " + e.message); } });
  app.querySelectorAll("[data-mdates]").forEach(b => b.onclick = () => openMountDialog(id, +b.dataset.mdates));
  wirePartButtons(app);
}

// ------------------------------------------------------------------ session page / edit dialog hooks

/** Subtitle piece for a session: its gear as a link, marked when it came from the default. */
function gearChip(a) {
  if (!a.gear_id) return "";
  const parts = (a.gear_parts || []).map(p => `${kindLabel(p.kind)}: ${p.name}${S.gear ? " " + specLine(S.gear.kinds, p) : ""}`).join("\n");
  return ` · <a href="#/gear/${esc(a.gear_id)}" title="${esc(parts || "no parts recorded on it that day")}">${esc(a.gear_name)}</a>${a.gear_src === "default" ? ` <span class="small muted">default</span>` : ""}`;
}
/** Fill the Edit dialog's gear select (S.gear fresh); returns () -> the assign op to post, or null. */
function editGearField(a) {
  const sel = $("#ed-gear"), row = $("#ed-gear-row");
  if (!sel || !S.gear || S.gear.readonly_store) { if (row) row.hidden = true; return () => null; }
  const cur = a.gear_src === "explicit" ? (a.gear_id || "none") : "default";
  const d = a.gear_default;  // the server's answer for this session's date and sport
  const fill = sport => {
    const { mine, other } = gearFor(S.gear.G, sport), v = sel.value || cur;
    const label = sport === a.type ? (d ? d.name : "none") : "that sport's";
    sel.innerHTML = `<option value="default">Default (${esc(label)})</option>`
      + mine.map(g => `<option value="${esc(g.id)}">${esc(g.name)}</option>`).join("")
      + (other.length ? `<optgroup label="Other or retired">${other.map(g => `<option value="${esc(g.id)}">${esc(g.name)}</option>`).join("")}</optgroup>` : "")
      + `<option value="none">None</option>`;
    sel.value = v;
  };
  sel.value = "";
  fill(a.type);
  // the default follows the sport; openEdit sets this handler fresh on each open
  $("#ed-type").oninput = () => fill($("#ed-type").value || a.type);
  row.hidden = false;
  return () => sel.value === cur ? null : { op: "assign", ids: [a.id], gear: sel.value === "none" ? null : sel.value };
}
