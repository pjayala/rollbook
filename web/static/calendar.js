// SPDX-License-Identifier: GPL-3.0-or-later
// Copyright (C) 2026 Pedro Ayala. Rollbook is free software, see LICENSE.
"use strict";
/* Calendar heatmap of training days. */

// ------------------------------------------------------------------ calendar heatmap

const CAL_COLORS = ["#4a2208", "#8c3a0c", "#d9580f", "#ff8a3d", "#ffc27a"];
const ymd = d => `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
function calendar(host, acts, opt = {}) {
  const byDay = new Map();
  for (const a of acts) { const d = byDay.get(a.date) || { km: 0, n: 0, mv: 0, a: [] }; d.km += a.km || 0; d.mv += a.moving_s || 0; d.n++; d.a.push(a); byDay.set(a.date, d); }
  const years = [...new Set(acts.map(a => a._d.getFullYear()))].sort((a, b) => b - a);
  const vmax = quantile([...byDay.values()].map(d => d.km), .95) || 1;
  const color = km => km > 0 ? CAL_COLORS[Math.min(CAL_COLORS.length - 1, Math.floor(km / vmax * (CAL_COLORS.length - 0.001)))] : PAL.none;
  const C = 12, G = 3, L = 30, T = 16;
  const show = opt.all ? years : years.slice(0, 3);
  host.innerHTML = "";
  for (const y of show) {
    const yacts = acts.filter(a => a._d.getFullYear() === y);
    const row = document.createElement("div"); row.className = "calrow";
    row.innerHTML = `<div class="calhead"><b>${y}</b><span class="muted small">${yacts.length} sessions · ${fkm(yacts.reduce((s, a) => s + (a.km || 0), 0))} km · ${hours(yacts.reduce((s, a) => s + (a.moving_s || 0), 0))}</span></div>`;
    const svg = el("svg", { class: "cal", viewBox: `0 0 ${L + 54 * (C + G)} ${T + 7 * (C + G)}`, width: L + 54 * (C + G), height: T + 7 * (C + G) });
    const off = (new Date(y, 0, 1).getDay() + 6) % 7;
    for (const [i, n] of [[0, "Mon"], [2, "Wed"], [4, "Fri"], [6, "Sun"]]) el("text", { x: 0, y: T + i * (C + G) + C - 2, class: "calt" }, svg).textContent = n;
    for (let d = new Date(y, 0, 1), k = 0; d.getFullYear() === y; d.setDate(d.getDate() + 1), k++) {
      const idx = k + off, cx = L + Math.floor(idx / 7) * (C + G), cy = T + (idx % 7) * (C + G);
      if (d.getDate() === 1) el("text", { x: cx, y: 10, class: "calt" }, svg).textContent = MON[d.getMonth()];
      const key = ymd(d), v = byDay.get(key);
      el("rect", { x: cx, y: cy, width: C, height: C, rx: 2, fill: color(v ? v.km : 0), "data-d": key, class: v ? "on" : null }, svg);
    }
    row.appendChild(svg);
    host.appendChild(row);
  }
  if (years.length > show.length) {
    const b = document.createElement("button"); b.className = "btn"; b.textContent = `show all ${years.length} years`;
    b.onclick = () => calendar(host, acts, { ...opt, all: true }); host.appendChild(b);
  }
  const leg = document.createElement("div"); leg.className = "legend"; leg.style.marginTop = "6px";
  leg.innerHTML = `<span>less</span>` + CAL_COLORS.map((c, i) => `<span><i style="background:${c}"></i>${f0(vmax * i / CAL_COLORS.length)}+</span>`).join("") + `<span>km / day</span>`;
  host.appendChild(leg);
  host.onmousemove = ev => {
    const k = ev.target.dataset && ev.target.dataset.d; if (!k) { hideTip(); return; }
    const v = byDay.get(k), d = new Date(k + "T12:00:00");
    showTip(`<b>${["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"][d.getDay()]} ${fdate(d)}</b>` + (v ? v.a.map(a => `<div class="row"><span>${esc(a.name)}</span><span>${fkm(a.km)} km · ${f1(a.kmh_moving)} km/h</span></div>`).join("") : `<div class="muted">rest</div>`), ev);
  };
  host.onmouseleave = hideTip;
  host.onclick = ev => {
    const k = ev.target.dataset && ev.target.dataset.d, v = k && byDay.get(k); if (!v) return;
    hideTip();
    if (opt.before) opt.before();
    if (v.n === 1) go(v.a[0].id); else { S.q = k; location.hash = "#/sessions"; }
  };
}
