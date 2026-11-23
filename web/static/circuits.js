// SPDX-License-Identifier: GPL-3.0-or-later
// Copyright (C) 2026 Pedro Ayala. Rollbook is free software, see LICENSE.
"use strict";
/* Circuits panel: sessions grouped by the lap venue they used. */

// ------------------------------------------------------------------ circuits (repeated-lap venues)

const CIRC_COLORS = [PAL.hue, PAL.ink, PAL.tint, PAL.grey, PAL.deep, PAL.dim];
/** Sessions grouped by the circuit they lapped: lap centre within max(60 m, 25% of
 * a lap) and lap length within 12%. Oldest first, so the first session of a
 * circuit anchors it whatever order the list came in. */
function groupCircuits(acts) {
  const near = (a, b) => { const dy = (a[0] - b[0]) * 111320, dx = (a[1] - b[1]) * 111320 * Math.cos(a[0] * Math.PI / 180); return Math.hypot(dx, dy); };
  const cs = [];
  for (const a of acts.filter(a => a.laps_n && a.lap_center && a.best_lap_s).sort(cmpWhen)) {
    let c = cs.find(c => near(c.center, a.lap_center) < Math.max(60, 250 * c.km) && Math.abs(a.lap_km / c.km - 1) < 0.12);
    if (!c) cs.push(c = { center: a.lap_center, km: a.lap_km, a: [] });
    c.a.push(a);
  }
  return cs.filter(c => c.a.length >= 2 || c.a[0].laps_n >= 5);
}
function circuits(host, acts) {
  const list = groupCircuits(acts).sort((x, y) => y.a.length - x.a.length).slice(0, CIRC_COLORS.length);
  if (!list.length) { host.hidden = true; return; }
  host.hidden = false;
  list.forEach((c, i) => {
    c.color = CIRC_COLORS[i];
    c.km = median(c.a.map(a => a.lap_km));
    c.best = c.a.reduce((b, a) => !b || a.best_lap_s < b.best_lap_s ? a : b, null);
    c.last = c.a.reduce((b, a) => !b || a._t > b._t ? a : b, null);
    c.laps = c.a.reduce((s, a) => s + a.laps_n, 0);
    c.name = `${c.km < 1 ? f0(c.km * 1000) + " m" : f2(c.km) + " km"} circuit`;
  });
  host.innerHTML = `<h2>Circuits <span class="muted">sessions where you lapped the same loop · best lap per session</span></h2>
    <div class="grid g2"><div id="c-circ"></div><div class="scroll"><table><thead><tr><th class="l">circuit</th><th>sessions</th><th>laps</th><th>best lap</th><th>km/h</th><th class="l">when</th><th>latest best</th></tr></thead><tbody>` +
    list.map(c => `<tr><td class="l"><span class="bar" style="width:10px;background:${c.color}"></span> ${c.name} <span class="muted small">${c.center[0].toFixed(3)}, ${c.center[1].toFixed(3)}</span></td><td>${c.a.length}</td><td>${c.laps}</td>
      <td>${dur(c.best.best_lap_s)}</td><td>${f2(c.best.best_lap_kmh)}</td><td class="l"><a href="#/session/${c.best.id}">${fdate(c.best._d)}</a></td>
      <td><a href="#/session/${c.last.id}">${dur(c.last.best_lap_s)}</a></td></tr>`).join("") + `</tbody></table></div></div>`;
  chart($("#c-circ"), {
    x: { type: "time" }, y: { fmt: v => v.toFixed(0), label: "best lap km/h" }, onClick: go,
    series: list.flatMap(c => [
      { name: c.name, kind: "dot", color: c.color, r: 3.5, data: c.a.map(a => ({ x: a._t, y: a.best_lap_kmh, ref: a.id })) },
      { name: c.name, kind: "line", color: c.color, width: 1.5, opacity: .6, legend: false, data: c.a.slice().sort((x, y) => x._t - y._t).map(a => ({ x: a._t, y: a.best_lap_kmh, ref: a.id })), gapX: 60 * DAY }]),
    tip: (x, hits, b) => { const a = acts.find(a => a.id === b.p.ref); return a ? `<b>${fdate(a._d)}</b> · ${esc(b.s.name)}<div class="row"><span>best lap</span><span>${dur(a.best_lap_s)} · ${f2(a.best_lap_kmh)} km/h</span></div><div class="row"><span>laps</span><span>${a.laps_n}</span></div>` : ""; },
  });
}
