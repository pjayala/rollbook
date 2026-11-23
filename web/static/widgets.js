// SPDX-License-Identifier: GPL-3.0-or-later
// Copyright (C) 2026 Pedro Ayala. Rollbook is free software, see LICENSE.
"use strict";
/* Small HTML builders shared by the pages: KPI cards, route pill, session links. */

// ------------------------------------------------------------------ views

/** KPI strip: one surface split into equal cells. The column count comes from
 * the item count (8 → 4 + 4, 10 → 5 + 5, 17 → 6 + 6 + 5), so no row ends with an orphan. */
function cards(items) {
  const n = items.length, fit = max => Math.max(1, Math.ceil(n / Math.ceil(n / max)));
  // span of cell i in 60ths with c columns: the last, shorter row widens its cells (60 = lcm 1..6)
  const span = (i, c) => { const r = n % c, last = n - r; return 60 / (r && i >= last ? r : c); };
  return `<div class="cards">` + items.map((c, i) => `<div class="card ${c.hl ? "hl" : ""} ${c.cls || ""}"${c.id ? ` id="${esc(c.id)}"` : ""}${c.button ? ` role="button" tabindex="0" aria-haspopup="dialog"` : ""}${c.title ? ` title="${esc(c.title)}"` : ""} style="--s:${span(i, fit(6))};--st:${span(i, fit(4))};--sp:${span(i, 2)}"><div class="k">${esc(c.k)}</div><div class="v">${c.v}</div>${c.s ? `<div class="s">${c.s}</div>` : ""}</div>`).join("") + `</div>`;
}
const go = id => { location.hash = "#/session/" + id; };
const routePill = r => r ? `<span class="pill ${r === "out-back" ? "ob" : r === "loop" ? "loop" : r === "one-way" ? "ow" : ""}">${esc(r)}</span>` : "";

/** Sessions on the same route as `a` (its metrics `m`): same kind, similar distance from
 * the start to the farthest point and, out-and-back, a similar outbound bearing. */
function sameRoute(a, m, peers) {
  return peers.filter(p => p.id !== a.id && p.route === m.route && m.route && m.route !== "indoor" &&
    isNum(p.crow_km) && isNum(m.crow_km) && Math.abs(p.crow_km - m.crow_km) < Math.max(0.6, m.crow_km * 0.12) &&
    (m.route !== "out-back" || Math.abs(((p.bearing_out - m.bearing_out + 540) % 360) - 180) < 20) &&
    Math.abs((p.km || 0) - (a.km || 0)) < Math.max(2, a.km * 0.2));
}
