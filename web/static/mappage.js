// SPDX-License-Identifier: GPL-3.0-or-later
// Copyright (C) 2026 Pedro Ayala. Rollbook is free software, see LICENSE.
"use strict";
/* Map page (#/map): every track of a sport, heatmap, areas. */

// ------------------------------------------------------------------ map page

async function loadTracks(type) {
  if (!S.tracks[type]) {
    const r = await api("tracks?type=" + encodeURIComponent(type));
    S.tracks[type] = r.ids.map((id, i) => { const pts = decodeTrack(r.trk[i]); return { id, pts, bbox: bboxOf(pts) }; });
  }
  return S.tracks[type];
}

/** Group tracks by where they start (~5 km cells), biggest cluster first. */
function areas(tracks) {
  const cell = 1 / 8192, g = new Map();
  for (const t of tracks) {
    const k = Math.round(t.pts[0] / cell / 1) + ":" + Math.round(t.pts[1] / cell);
    if (!g.has(k)) g.set(k, []); g.get(k).push(t);
  }
  // merge neighbouring cells into the largest nearby cluster
  const cl = [];
  for (const ts of [...g.values()].sort((a, b) => b.length - a.length)) {
    const x = ts[0].pts[0], y = ts[0].pts[1];
    const c = cl.find(c => Math.abs(c.x - x) < cell * 3 && Math.abs(c.y - y) < cell * 3);
    if (c) c.t.push(...ts); else cl.push({ x, y, t: ts.slice() });
  }
  for (const c of cl) {
    const q = (i, p) => quantile(c.t.map(t => t.bbox[i]), p);
    c.bbox = [q(0, .1), q(1, .1), q(2, .9), q(3, .9)];
    const lat = Math.atan(Math.sinh(Math.PI * (1 - 2 * c.y))) * 180 / Math.PI, lon = c.x * 360 - 180;
    c.label = `${Math.abs(lat).toFixed(2)}°${lat >= 0 ? "N" : "S"} ${Math.abs(lon).toFixed(2)}°${lon >= 0 ? "E" : "W"}`;
  }
  return cl.sort((a, b) => b.t.length - a.t.length);
}

async function viewMap() {
  if (isAll()) return viewPickSport("map");
  const app = $("#app");
  app.innerHTML = `<p class="muted">Loading tracks…</p>`;
  const acts = inRange(await loadActs(S.type)), all = await loadTracks(S.type);
  const byId = new Map(acts.map(a => [a.id, a]));
  const tracks = all.filter(t => byId.has(t.id)).map(t => ({ ...t, a: byId.get(t.id) }));
  let mode = localStorage.mapMode || "heat", colorBy = localStorage.mapColor || "best10min_kmh", sel = [];
  const km = tracks.reduce((s, t) => s + (t.a.km || 0), 0);
  app.innerHTML = `
    <div class="head"><div><h1>${esc(S.type)} map</h1><div class="muted">${tracks.length} sessions with GPS · ${fkm(km)} km${S.range !== "all" ? " · " + esc(rangeLabel(S.range).replace(/^(Last|This) /, m => m.toLowerCase())) : ""}</div></div></div>
    <div class="panel">
      <div class="toolbar">
        <div class="seg" id="mmode"><button data-m="heat">Heatmap</button><button data-m="lines">Sessions</button></div>
        <label class="muted small" id="lcolor">colour by <select id="mcolor">
          <option value="best10min_kmh">best 10-min</option><option value="wind_neutral">wind-neutral</option>
          <option value="kmh_moving">avg speed</option><option value="km">distance</option><option value="_t">date</option></select></label>
        <label class="muted small" id="lsat">saturate at <input id="msat" type="range" min="0" max="80" value="${localStorage.heatSat || 0}"> <span id="msatv" class="num"></span></label>
        <span id="areas" class="areas"></span>
      </div>
      <div id="bigmap" class="bigmap"></div>
      <div class="legend" id="mleg" style="margin-top:8px"></div>
      <div class="note">Drag to pan, scroll or double-click to zoom. Click the map to list every session through that spot${""}; in <b>Sessions</b> mode hover to identify a line and click to open it.</div>
    </div>
    <div class="panel" id="here" hidden></div>`;
  if (!tracks.length) { $("#bigmap").innerHTML = `<p class="muted" style="padding:16px">No GPS tracks for ${esc(S.type)} in this range.</p>`; return; }

  const map = new SlippyMap($("#bigmap"));
  const heat = new HeatLayer(tracks), lines = new LinesLayer(tracks, { width: 2, alpha: .6 });
  const selLayer = new LinesLayer([], { width: 2.5 });
  const hoverLayer = new LinesLayer([], { width: 2.5 });

  const colorize = () => {
    const key = colorBy, vals = tracks.map(t => t.a[key]).filter(isNum);
    const lo = quantile(vals, .05), hi = quantile(vals, .95);
    for (const t of tracks) {
      const v = t.a[key];
      t.color = isNum(v) ? SPEED_COLORS[Math.max(0, Math.min(SPEED_COLORS.length - 1, Math.floor((v - lo) / ((hi - lo) || 1) * SPEED_COLORS.length)))] : PAL.na;
    }
    const lab = key === "_t" ? v => fmon(new Date(v)) : key === "km" ? v => f0(v) : v => f1(v);
    return `<span>${$("#mcolor").selectedOptions[0].text}</span>` + SPEED_COLORS.map((c, i) => `<span><i style="background:${c}"></i>${lab(lo + (hi - lo) * i / SPEED_COLORS.length)}</span>`).join("") + `<span><i style="background:${PAL.na}"></i>n/a</span>`;
  };
  const render = () => {
    $("#mmode").querySelectorAll("button").forEach(b => b.classList.toggle("on", b.dataset.m === mode));
    $("#lcolor").hidden = mode !== "lines"; $("#lsat").hidden = mode !== "heat";
    heat.passes = +$("#msat").value || null;
    map.layers = [mode === "heat" ? heat : lines, selLayer, hoverLayer];
    if (mode === "lines") $("#mleg").innerHTML = colorize();
    map.draw();
    if (mode === "heat") {
      $("#msatv").textContent = heat.passes ? `${heat.passes} sessions` : `auto (${heat.lastN})`;
      const r = HeatLayer.ramp(), c = f => { const j = Math.max(1, Math.round(f * 255)) * 4; return `rgb(${r[j]},${r[j + 1]},${r[j + 2]})`; };
      $("#mleg").innerHTML = `<span>sessions through a spot</span>` + [1, .25, .5, .75, 1].map((f, i) => `<span><i style="background:${c(i ? f : 1 / heat.lastN)}"></i>${i ? Math.round(f * heat.lastN) + (f === 1 ? "+" : "") : 1}</span>`).join("");
    }
  };
  $("#mmode").onclick = e => { const m = e.target.dataset.m; if (m) { mode = localStorage.mapMode = m; render(); } };
  $("#mcolor").value = colorBy;
  $("#mcolor").onchange = e => { colorBy = localStorage.mapColor = e.target.value; render(); };
  $("#msat").oninput = e => { localStorage.heatSat = e.target.value; render(); };

  // areas: start-point clusters, the biggest is "home"
  const ar = areas(tracks);
  $("#areas").innerHTML = ar.slice(0, 6).map((c, i) => `<button class="btn small" data-i="${i}" title="${c.label}">${i ? c.label : "Home"} <span class="muted">${c.t.length}</span></button>`).join("") +
    (ar.length > 1 ? `<button class="btn small" data-i="all">All</button>` : "");
  const allBox = [Math.min(...tracks.map(t => t.bbox[0])), Math.min(...tracks.map(t => t.bbox[1])), Math.max(...tracks.map(t => t.bbox[2])), Math.max(...tracks.map(t => t.bbox[3]))];
  $("#areas").onclick = e => { const b = e.target.closest("button"); if (!b) return; map.fit(b.dataset.i === "all" ? allBox : ar[+b.dataset.i].bbox); };
  map.home = ar[0].bbox;
  const saved = S.mapView[S.type];
  if (saved) { Object.assign(map, saved); map.redraw(); } else map.fit(ar[0].bbox);
  map.on("view", () => { S.mapView[S.type] = { cx: map.cx, cy: map.cy, z: map.z }; if (mode === "heat") render(); });

  const tipFor = t => { const a = t.a; return `<b>${fdate(a._d)}</b> · ${esc(a.name)}<div class="row"><span>distance</span><span>${fkm(a.km)} km</span></div><div class="row"><span>avg</span><span>${f2(a.kmh_moving)} km/h</span></div><div class="row"><span>best 10-min</span><span>${f2(a.best10min_kmh)}</span></div>` + (isNum(a.wind_neutral) ? `<div class="row"><span>wind-neutral</span><span>${f2(a.wind_neutral)}</span></div>` : ""); };
  map.on("hover", (pos, ev) => {
    if (!pos || mode !== "lines") { if (hoverLayer.tracks.length) { hoverLayer.tracks = []; map.redraw(); } hideTip(); return; }
    const h = hitTracks(map, tracks, pos.x, pos.y, 6)[0];
    const cur = hoverLayer.tracks[0];
    if ((cur && cur.src) !== h) { hoverLayer.tracks = h ? [{ ...h, hi: true, src: h }] : []; map.redraw(); }
    map.canvas.style.cursor = h ? "pointer" : "";
    if (h) showTip(tipFor(h), ev); else hideTip();
  });
  map.on("click", pos => {
    const hits = hitTracks(map, tracks, pos.x, pos.y, mode === "lines" ? 6 : 9);
    if (mode === "lines" && hits.length) { hideTip(); go(hits[0].id); return; }
    sel = hits;
    selLayer.tracks = sel.map(t => ({ ...t, hi: true }));
    map.redraw();
    listHere(sel);
  });
  addEventListener("hashchange", () => map.destroy(), { once: true });

  function listHere(ts) {
    const here = $("#here");
    if (!ts.length) { here.hidden = true; return; }
    here.hidden = false;
    const as = ts.map(t => t.a).sort((a, b) => b._t - a._t);
    const bw = as.filter(a => isNum(a.wind_neutral)).reduce((b, a) => !b || a.wind_neutral > b.wind_neutral ? a : b, null);
    here.innerHTML = `<div class="toolbar"><h2 style="margin:0">${as.length} session${as.length > 1 ? "s" : ""} through this spot</h2>
        <span class="muted small">${fdate(as[as.length - 1]._d)} → ${fdate(as[0]._d)} · median best 10-min ${f2(median(as.map(a => a.best10min_kmh)))}${bw ? ` · best wind-neutral ${f2(bw.wind_neutral)}` : ""}</span>
        <button class="btn" id="hclear" style="margin-left:auto">clear</button></div>
      <div class="scroll"><table><thead><tr><th>date</th><th class="l">name</th><th>km</th><th>moving</th><th>avg km/h</th><th>pace</th><th>best 10</th><th>wind-neutral</th><th>HR</th><th class="l">route</th></tr></thead><tbody>` +
      as.map(a => `<tr class="click" data-id="${a.id}"><td class="l">${fdate(a._d)}</td><td class="l">${esc(a.name)}</td><td>${fkm(a.km)}</td><td>${dur(a.moving_s, true)}</td><td>${f2(a.kmh_moving)}</td><td>${pace(a.kmh_moving)}</td><td>${f2(a.best10min_kmh)}</td><td>${f2(a.wind_neutral)}</td><td>${f0(a.hr_avg)}</td><td class="l">${routePill(a.route)}</td></tr>`).join("") + `</tbody></table></div>`;
    here.querySelectorAll("tr[data-id]").forEach(tr => {
      tr.onclick = () => go(tr.dataset.id);
      tr.onmouseenter = () => { hoverLayer.tracks = ts.filter(t => String(t.id) === tr.dataset.id).map(t => ({ ...t, hi: true })); selLayer.tracks = []; map.redraw(); };
      tr.onmouseleave = () => { hoverLayer.tracks = []; selLayer.tracks = sel.map(t => ({ ...t, hi: true })); map.redraw(); };
    });
    $("#hclear").onclick = () => { sel = []; selLayer.tracks = []; here.hidden = true; map.redraw(); };
  }
  render();
}
