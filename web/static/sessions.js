// SPDX-License-Identifier: GPL-3.0-or-later
// Copyright (C) 2026 Pedro Ayala. Rollbook is free software, see LICENSE.
"use strict";
/* Sessions list page (#/sessions). */

async function viewSessions() {
  const showHidden = localStorage.showHidden === "1";
  const acts = inRange(await loadActs(S.type, showHidden));
  let pick = loadPick();
  const cols = [
    ["pick", `<span class="vh">Select to compare</span>`, a => `<input type="checkbox" class="pick" data-pick="${a.id}" ${pick.includes(a.id) ? "checked" : ""} aria-label="Select ${esc(a.name)} on ${esc(a.date)} to compare">`, "pickc"],
    ["fav", `<span class="star on" title="Favorite">${ICON.star}</span>`, a => starSlot("sessions", a.id)], ["date", "date", a => `${fdate(a._d)}`, "l"],
    ["name", "name", a => esc(a.name) + (a.hidden ? ` <span class="pill">${a.deleted ? "deleted" : "hidden"}</span>` : ""), "l"],
    ...(isAll() ? [["type", "sport", a => esc(a.type), "l"]] : []),
    ...(acts.some(a => a.gear_id) ? [["gear_name", "gear", a => a.gear_id ? `<span class="${a.gear_src === "default" ? "muted" : ""}" title="${a.gear_src === "default" ? "the sport's default" : "chosen for this session"}">${esc(a.gear_name)}</span>` : `<span class="muted">–</span>`, "l"]] : []),
    ["km", "km", a => fkm(a.km)],
    ["elapsed_s", "elapsed", a => dur(a.elapsed_s, true)], ["moving_s", "moving", a => dur(a.moving_s, true)], ["pause_s", "paused", a => dur(a.pause_s)],
    ["kmh_moving", "avg km/h", a => f2(a.kmh_moving)], ["pace", "pace /km", a => pace(a.kmh_moving)], ["best10min_kmh", "best 10", a => f2(a.best10min_kmh)],
    ["b60", "best 60", a => f2(a.b60)],
    ...BE.filter(b => acts.some(a => a["be" + b.k] > 0)).map(b => ["be" + b.k, b.short || b.n, a => a["be" + b.k] > 0 ? dur(a["be" + b.k], a["be" + b.k] >= 3600) : "–", "", true]),
    ["wind_neutral", "wind-neutral", a => f2(a.wind_neutral)], ["asym_pct", "out vs back", a => pct(a.asym_pct)],
    ["laps_n", "laps", a => a.laps_n ? `${a.laps_n} × ${f2(a.lap_km)}` : ""], ["best_lap_s", "best lap", a => a.best_lap_s ? dur(a.best_lap_s) : ""],
    ["temp", "°C", a => f0(a.temp)], ["wind", "wind", a => a.cond && isNum(a.cond.ws) ? `${f0(a.cond.ws)} ${windArrow(a.cond.wd)}` : ""],
    ...(acts.some(a => a.rain > 0) ? [["rain", "rain mm", a => isNum(a.rain) && a.rain > 0 ? a.rain.toFixed(1) : ""]] : []),
    ...(acts.some(a => isNum(a.aqi)) ? [["aqi", "AQI", a => f0(a.aqi)]] : []),
    ["adj_neutral", "adjusted", a => f2(a.adj_neutral)], ["kcal", "kcal", a => f0(a.kcal)],
    ["hr_avg", "HR", a => f0(a.hr_avg)], ["ascent_m", "ascent", a => f0(a.ascent_m)], ["route", "route", a => routePill(a.route), "l"],
  ];
  if (acts.some(a => isNum(pwAvg(a)))) cols.splice(cols.findIndex(c => c[0] === "hr_avg"), 0,
    ["watts", "avg W", a => f0(pwAvg(a))], ["np", "weighted W", a => f0(a.np)], ["pw20m", "best 20 W", a => f0(a.pw20m)]);
  const gearOpts = [...new Map(acts.filter(a => a.gear_id).map(a => [a.gear_id, a.gear_name])).entries()];
  const gearKey = "gearFilter:" + S.type;  // per sport: a gear belongs to its sports
  let gearF = localStorage[gearKey] || "";
  if (gearF && !(gearF === "none" ? gearOpts.length : gearOpts.some(([id]) => id === gearF))) { gearF = ""; delete localStorage[gearKey]; }
  const app = $("#app");
  app.innerHTML = `<div class="head"><div><h1>${isAll() ? "All sessions" : esc(S.type) + " sessions"}</h1><div class="muted" id="cnt"></div></div></div>
    <div class="panel"><div class="toolbar"><input id="q" placeholder="${isAll() ? "filter: name, date, sport…" : "filter: name, date, route…"}" value="${esc(S.q)}" style="width:260px">
      <label class="muted small">min km <input id="mink" type="number" value="${Math.max(0, +localStorage.minKm || 0)}" style="width:70px"></label>
      ${gearOpts.length ? `<label class="muted small">gear <select id="gearf"><option value="">all</option>${gearOpts.map(([id, n]) => `<option value="${esc(id)}" ${id === gearF ? "selected" : ""}>${esc(n)}</option>`).join("")}<option value="none" ${gearF === "none" ? "selected" : ""}>none</option></select></label>` : ""}
      <label class="muted small"><input type="checkbox" id="favonly" ${localStorage.favOnly === "1" ? "checked" : ""}> Favorites only</label>
      <label class="muted small" title="hidden and deleted sessions never count towards stats, maps or segments"><input type="checkbox" id="showhidden" ${showHidden ? "checked" : ""}> show hidden</label>
      <span class="cmpbar" id="cmpbar" hidden><span class="muted small" id="cmpcnt"></span><button class="btn small" id="cmpgo" disabled>Compare</button><button class="linkbtn" id="cmpclr">Clear</button></span></div>
      <div class="scroll"><table id="tbl"></table></div></div>`;
  const draw = () => {
    const q = S.q.toLowerCase(), mink = +($("#mink").value || 0);
    const k = S.sort.k, dir = S.sort.dir;
    const val = a => k === "watts" ? pwAvg(a) : k === "pace" ? (a.kmh_moving ? -a.kmh_moving : null) : k === "date" ? a._t : k === "fav" ? (isFav("sessions", a.id) ? 1 : 0) : a[k];
    const favOnly = $("#favonly").checked;
    const rows = acts.filter(a => (a.km || 0) >= mink && (!favOnly || isFav("sessions", a.id)) && gearFilterMatch(a, gearF)
      && (!q || `${a.name} ${a.date} ${a.type} ${a.route} ${a.device} ${a.gear_name || ""}`.toLowerCase().includes(q)))
      .sort((a, b) => { const x = val(a), y = val(b); if (x == null) return y == null ? 0 : 1; if (y == null) return -1; return (x > y ? 1 : x < y ? -1 : 0) * dir; });
    const nh = rows.filter(a => a.hidden).length;
    $("#cnt").textContent = `${rows.length} of ${acts.length} sessions · ${fkm(rows.reduce((s, a) => s + (a.hidden ? 0 : a.km || 0), 0))} km` + (nh ? ` · ${nh} hidden` : "");
    $("#tbl").innerHTML = `<thead><tr>${cols.map(([k, n, , c]) => `<th class="${k === "pick" ? "" : "sort"} ${c || ""} ${S.sort.k === k ? (S.sort.dir > 0 ? "asc" : "desc") : ""}" data-k="${k}">${n}</th>`).join("")}</tr></thead><tbody>` +
      rows.map(a => `<tr class="click${a.hidden ? " hiddenrow" : ""}${pick.includes(a.id) ? " sel" : ""}" data-id="${a.id}">${cols.map(([, , f, c]) => `<td class="${c || ""}">${f(a)}</td>`).join("")}</tr>`).join("") + `</tbody>`;
    $("#tbl").querySelectorAll("th.sort").forEach(th => th.onclick = () => { const k = th.dataset.k; const low = (cols.find(c => c[0] === k) || [])[4] || k === "best_lap_s"; S.sort = { k, dir: S.sort.k === k ? -S.sort.dir : low ? 1 : -1 }; draw(); });
    $("#tbl").querySelectorAll("tr[data-id]").forEach(tr => tr.onclick = e => { if (!e.target.closest(".pickc")) go(tr.dataset.id); });
    $("#tbl").querySelectorAll("input.pick").forEach(cb => cb.onchange = () => { pick = pickToggle(pick, +cb.dataset.pick); savePick(pick); draw(); });
    const shown = new Set(rows.map(a => a.id)), off = pick.filter(id => !shown.has(id)).length;
    $("#cmpbar").hidden = !pick.length;
    $("#cmpcnt").textContent = `${pick.length} of 2 selected` + (off ? ` (${off} not shown)` : "");
    $("#cmpgo").disabled = pick.length !== 2;
    mountStars($("#tbl"), null, () => favOnly && draw());
  };
  $("#q").oninput = e => { S.q = e.target.value; draw(); };
  $("#mink").oninput = e => { localStorage.minKm = e.target.value; draw(); };
  $("#favonly").onchange = e => { localStorage.favOnly = e.target.checked ? "1" : "0"; draw(); };
  if ($("#gearf")) $("#gearf").onchange = e => { gearF = localStorage[gearKey] = e.target.value; draw(); };
  $("#showhidden").onchange = e => { localStorage.showHidden = e.target.checked ? "1" : "0"; viewSessions(); };
  $("#cmpclr").onclick = () => { pick = []; savePick(pick); draw(); };
  $("#cmpgo").onclick = () => {
    if (pick.length !== 2) return;
    const [x, y] = pick.map(id => acts.find(a => a.id === id) || { id, _t: 0 });
    const [sub, ref] = cmpWhen(x, y) >= 0 ? [x, y] : [y, x];  // the newer one is the subject
    pick = []; savePick(pick);
    location.hash = `#/compare/${sub.id}/${ref.id}`;
  };
  draw();
}
