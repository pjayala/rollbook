// SPDX-License-Identifier: GPL-3.0-or-later
// Copyright (C) 2026 Pedro Ayala. Rollbook is free software, see LICENSE.
"use strict";
/* Best speed per window length (30 s to 60 min): the session page's speed curve
 * and its table. The helpers above bestSpeedsPanel are pure (tested under node). */

// best speed per window length: the metrics' `bw` {seconds: [km/h, t0, t1]}, list rows' `bw` {seconds: km/h}
const BW = [{ s: 30, n: "30 s" }, { s: 60, n: "1 min" }, { s: 120, n: "2 min" }, { s: 300, n: "5 min" }, { s: 600, n: "10 min" },
  { s: 1200, n: "20 min" }, { s: 1800, n: "30 min" }, { s: 2700, n: "45 min" }, { s: 3600, n: "60 min" }];
// rows cached before `bw` existed: the same windows from the older columns (never
// best30_kmh, which used to be 30 samples, 3 minutes at 6 s recording)
const BW_OLD = { 300: "best5min_kmh", 600: "best10min_kmh", 1200: "best20min_kmh", 1800: "b30", 2700: "b45", 3600: "b60" };
const BW_MIN_PEERS = 3;  // fewer other sessions with a window: no context point for it
const BW_TICK = { 30: "30 s", 60: "1 min", 120: "2", 300: "5", 600: "10", 1200: "20", 1800: "30", 3600: "60 min" };
/** A list row's best speed over s seconds, or null. */
function bwOf(p, s) {
  const v = p && p.bw && p.bw[s];
  if (isNum(v) && v > 0) return v;
  const c = BW_OLD[s], o = c && p ? p[c] : null;
  return isNum(o) && o > 0 ? o : null;
}
/** Per window: the other sessions' values, the all-time best {v, p} and the best of the
 * 90 days before t (ms), each only with >= BW_MIN_PEERS values. me = this session's id. */
function bwContext(peers, me, t) {
  const out = {};
  for (const w of BW) {
    const others = [], recent = [];
    let best = null;
    for (const p of peers) {
      if (String(p.id) === String(me)) continue;
      const v = bwOf(p, w.s);
      if (v == null) continue;
      others.push(v);
      if (!best || v > best.v) best = { v, p };
      const pt = whenOf(p);
      if (pt >= t - 90 * DAY && pt < t) recent.push(v);
    }
    out[w.s] = { others, all: others.length >= BW_MIN_PEERS ? best : null, d90: recent.length >= BW_MIN_PEERS ? Math.max(...recent) : null };
  }
  return out;
}
/** Rank of v (1 = fastest) among others after display rounding (2 decimals); ties share.
 * gap: km/h ahead of the next best when first (0 = tied), else behind the best (< 0). */
function bwRank(v, others) {
  const r = Math.round(v * 100), os = others.map(o => Math.round(o * 100));
  const top = os.length ? Math.max(...os) : null;
  return { rank: os.filter(o => o > r).length + 1, of: os.length + 1, gap: top == null ? null : (r - top) / 100 };
}
/** x ticks (seconds) for a plot this wide: fewer when narrow, 45 min never labelled (it crowds 30 and 60). */
const bwTicks = w => w < 420 ? [30, 60, 300, 600, 1800, 3600] : [30, 60, 120, 300, 600, 1200, 1800, 3600];
/** The table under the curve. rows: [{n, s, v, rank, of, gap}]; hidden: no ranks. */
function bwTable(rows, hidden) {
  const rk = x => hidden ? `<span class="muted">hidden</span>` : x.rank == null ? "–"
    : x.rank === 1 && x.of > 1 ? `<span class="pill pill-pr">best</span>` : `${f0(x.rank)} <span class="muted">of ${f0(x.of)}</span>`;
  const vs = x => hidden || !isNum(x.gap) ? `<td class="muted">–</td>` : x.rank === 1 ? `<td class="muted">${x.gap > 0 ? `${f2(x.gap)} ahead of 2nd` : "tied"}</td>`
    : `<td class="muted">−${f2(-x.gap)}</td>`;
  return `<table class="bw"><thead><tr><th class="l">length</th><th>km/h</th><th>pace</th><th>distance</th><th>rank</th><th>vs the best</th></tr></thead><tbody>` +
    rows.map(x => `<tr class="click" tabindex="0" data-bw="${f0(x.s)}"><td class="l">${esc(x.n)}</td><td>${x.s === 600 ? `<b>${f2(x.v)}</b>` : f2(x.v)}</td><td>${pace(x.v)}</td>
      <td>${f2(x.v * x.s / 3600)} km</td><td>${rk(x)}</td>${vs(x)}</tr>`).join("") + `</tbody></table>`;
}

/** This session's windows with rank and peer context. rows: [{s, n, v, t0, t1, rank, of, gap}];
 * b10: the best 10 min for the KPI tile (the DB column when the metrics lack it);
 * show: draw the curve (not on a trainer: simulated gradients; needs 3 windows). */
function bestSpeeds(a, m, peers, t) {
  const virtual = m.route === "virtual" || String(a.type || "").startsWith("Virtual");
  const bw = m.bw || {}, ctx = bwContext(peers, a.id, t);
  const rows = BW.filter(w => bw[w.s] && isNum(bw[w.s][0]))
    .map(w => ({ ...w, v: bw[w.s][0], t0: bw[w.s][1], t1: bw[w.s][2], ...bwRank(bw[w.s][0], ctx[w.s].others) }));
  const b10 = rows.find(x => x.s === 600) || (isNum(a.best10min_kmh) ? { v: a.best10min_kmh } : null);
  return { rows, ctx, b10, show: !virtual && rows.length >= 3 };
}

/** The "Best speeds" panel of page P (#c-bw curve, #t-bw table, #bw-note). */
function bestSpeedsPanel(P) {
  const { a, ser, peers, vmax } = P, { rows, ctx } = P.bw;
  // where a window lies, in km, from the (downsampled) series
  const kmAt = t => { let b = null; for (const r of ser) if (!b || Math.abs(r[0] - t) < Math.abs(b[0] - t)) b = r; return b ? b[1] : null; };
  const lg = Math.log10, byS = new Map(rows.map(x => [x.s, x]));
  const nearestW = x => BW.reduce((b, w) => Math.abs(lg(w.s) - x) < Math.abs(lg(b.s) - x) ? w : b);
  const ctxAll = BW.filter(w => ctx[w.s].all).map(w => ({ x: lg(w.s), y: ctx[w.s].all.v, ref: w.s }));
  const ctx90 = BW.filter(w => isNum(ctx[w.s].d90)).map(w => ({ x: lg(w.s), y: ctx[w.s].d90, ref: w.s }));
  const vals = [...rows.map(x => x.v), ...ctxAll.map(p => p.y), ...ctx90.map(p => p.y)];
  const ylo = Math.max(0, Math.floor(Math.min(...vals) - 1)), yhi = Math.ceil(Math.max(...vals) + 1);
  const el = a.elapsed_s, longest = rows[rows.length - 1].s;
  const row = (k, v) => `<div class="row"><span>${k}</span><span>${v}</span></div>`;
  const allTime = c => c.all ? row("all time", `${f2(c.all.v)} km/h · ${esc(fdate(c.all.p._d || dateOf(c.all.p)))}`) : "";
  let dot = null;  // the window drawn as picked (white dot)
  const draw = () => {
    const me = rows.map(x => ({ x: lg(x.s), y: x.v, ref: x.s }));
    const sel = rows.find(x => P.isPicked("bw", x.s));
    dot = sel ? sel.s : null;
    const series = [
      { name: "best, all time", kind: "line", color: PAL.dim, width: 1.5, data: ctxAll },
      { name: "best, 90 days before", kind: "line", color: PAL.grey, width: 1, dash: "4 3", data: ctx90 },
      { name: "this session", kind: "line", color: PAL.hue, width: 2, data: me },
      { name: "record", kind: "dot", color: PAL.ink, r: 5, opacity: 1, legend: false, data: a.hidden ? [] : rows.filter(x => x.rank === 1 && x.of > 1).map(x => ({ x: lg(x.s), y: x.v, ref: x.s })) },
      { name: "window", kind: "dot", color: PAL.hue, r: 3.5, opacity: 1, legend: false, data: me },
      { name: "selected", kind: "dot", color: PAL.ink, r: 4.5, opacity: 1, legend: false, data: sel ? [{ x: lg(sel.s), y: sel.v, ref: sel.s }] : [] },
    ].filter(x => x.data.length);
    chart($("#c-bw"), {
      // level with the table beside it (~37 px a row); short and stacked on a phone
      height: innerWidth <= 720 ? 200 : Math.max(220, rows.length * 37 + 10), legend: series.some(x => x.name.startsWith("best")) ? undefined : false,
      x: { type: "linear", min: lg(30), max: lg(3600), ticks: w => bwTicks(w).map(lg), fmt: v => BW_TICK[Math.round(10 ** v)] || "" },
      y: { min: ylo, max: yhi, fmt: v => v.toFixed(0), label: "km/h", ticks: 4 },
      marks: isNum(el) && el < 3600 && longest < 3600 ? [{ x: lg(Math.max(30, el)), label: `${f0(el / 60)} min session` }] : [],
      series,
      tip: x => {
        const w = nearestW(x), r = byS.get(w.s), c = ctx[w.s];
        if (!r) return `<b>Best ${esc(w.n)}</b>` + row("this session", "too short") + allTime(c);
        const k0 = kmAt(r.t0), k1 = kmAt(r.t1);
        return `<b>Best ${esc(w.n)}</b>` + row("speed", `${f2(r.v)} km/h · ${pace(r.v)} /km`) +
          (isNum(k0) && isNum(k1) ? row("where", `${f2(k0)}–${f2(k1)} km · ${dur(r.t0 - ser[0][0], true)}–${dur(r.t1 - ser[0][0], true)}`) : "") +
          row("rank", a.hidden ? "hidden" : r.rank === 1 && r.of > 1 ? "best" : `${f0(r.rank)} of ${f0(r.of)}`) +
          allTime(c) + (isNum(c.d90) ? row("90 days", `${f2(c.d90)} km/h`) : "");
      },
      onClick: s => select(s),
    });
  };
  let picking = false;  // select() draws once itself, not again from the clear it triggers
  const select = s => {
    const r = byS.get(s);
    picking = true;
    try { P.pick("bw", s, r && [0, r.t0, r.t1, true, { el: r.t1 - r.t0, km: r.v * (r.t1 - r.t0) / 3600 }]); } finally { picking = false; }
    P.markRows($("#t-bw").querySelectorAll("tr[data-bw]"), "bw", tr => +tr.dataset.bw);
    draw();
  };
  // any other pick, a brush or the clear button drops the white dot
  P.onClear(() => { if (!picking && dot != null) draw(); });
  $("#t-bw").innerHTML = bwTable(rows, a.hidden);
  $("#t-bw").querySelectorAll("tr[data-bw]").forEach(tr => {
    tr.onclick = () => select(+tr.dataset.bw);
    tr.onkeydown = e => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); select(+tr.dataset.bw); } };
  });
  const slow = a.samples && a.elapsed_s && a.samples / a.elapsed_s < 0.5;
  $("#bw-note").textContent = "Distance over elapsed time, stops included, in any stretch of that length." +
    (isNum(vmax) ? ` Device top speed ${f1(vmax)} km/h.` : "") +
    (slow ? " 30 s and 1 min read slower on 6-second recording." : "") +
    (isNum(a.km_diff_pct) && Math.abs(a.km_diff_pct) > 10 ? " This track disagrees with Strava's distance by over 10%, so treat these with care." : "") +
    (!ctxAll.length && peers.length > 1 ? " Your other sessions appear here once their metrics are computed." : "");
  draw();
}
