// SPDX-License-Identifier: GPL-3.0-or-later
// Copyright (C) 2026 Pedro Ayala. Rollbook is free software, see LICENSE.
"use strict";
/* The session page's one selected stretch. Everything that highlights the map
 * (km splits, out/back legs, laps, best efforts / speeds, segment rows, brushing a
 * chart) goes through P.hl.highlightBy, which also shades the charts and fills
 * the "Selected" box with the stretch's stats. */

// tables whose rows select a stretch: picking in one clears the others
const SEL_ROWS = "#t-splits tr.sel, #t-legs tr.sel, #t-laps tr.sel, #t-be tr.sel, #t-bw tr.sel";

/** First and last series index with lo <= row[col] <= hi, or null. */
function idxRange(ser, col, lo, hi) {
  let i0 = -1, i1 = -1;
  ser.forEach((r, j) => { if (r[col] >= lo && r[col] <= hi) { if (i0 < 0) i0 = j; i1 = j; } });
  return i0 < 0 ? null : [i0, i1];
}

/** Stats of series rows i0..i1: moving s (>= 0.5 m/s), time-weighted HR and power
 * (steps over 60 s left out, a paused recording). */
function stretchStats(ser, i0, i1) {
  let mv = 0, hrS = 0, hrT = 0, pwS = 0, pwT = 0;
  for (let j = i0; j < i1; j++) {
    const dt = ser[j + 1][0] - ser[j][0];
    if (dt <= 0) continue;
    if ((ser[j + 1][1] - ser[j][1]) * 1000 / dt >= 0.5) mv += dt;
    if (ser[j][3] && dt <= 60) { hrS += ser[j][3] * dt; hrT += dt; }
    if (ser[j][7] != null && dt <= 60) { pwS += ser[j][7] * dt; pwT += dt; }
  }
  return { mv, hr: hrT ? hrS / hrT : null, pw: pwT ? pwS / pwT : null };
}

/** Wire the selection into page P:
 *   P.hl            the map, or a stand-in without GPS; highlightBy(col, lo, hi, fit, exact) selects a stretch
 *   P.pick(owner, key, range)   a table row / bar / point: picks it ([col, lo, hi, fit, exact]),
 *                   or drops it when it is already the pick (click again) or range is null
 *   P.isPicked(owner, key), P.markRows(rows, owner, keyOf)   for a panel's .sel classes
 *   P.clearRowSel() drops the pick and every .sel row (a brush, the clear button, another pick)
 *   P.onClear(fn)   runs on every clear (a panel that draws its pick, e.g. a dot)
 *   P.preview(t0, t1) / P.preview(null)   hover: shows a stretch on the map only, keeps the pick
 *   P.paintSel()    redraws bands + the "Selected" box (after charts are redrawn)
 * Must run after P.map is set and before anything that selects. */
function sessionSelection(P) {
  const { ser } = P, clears = [];
  const hl = P.map || { highlightBy() {}, highlight(t0, t1, f) { this.highlightBy(0, t0, t1, f); }, mark() {} };
  const mapHighlightBy = hl.highlightBy.bind(hl);
  let sel = null, picked = null;  // sel: the stretch {col, lo, hi, fit, exact}; picked: who chose it {owner, key}
  P.hl = hl;
  P.onClear = fn => clears.push(fn);
  P.isPicked = (owner, key) => !!picked && picked.owner === owner && picked.key === key;
  P.clearRowSel = () => {
    picked = null;
    document.querySelectorAll(SEL_ROWS).forEach(tr => tr.classList.remove("sel"));
    clears.forEach(fn => fn());
  };
  P.pick = (owner, key, range) => {
    const off = !range || P.isPicked(owner, key);
    P.clearRowSel();
    if (off) { hl.highlightBy(null); return false; }
    picked = { owner, key };
    hl.highlightBy(...range);
    return true;
  };
  P.markRows = (rows, owner, keyOf) => rows.forEach(tr => tr.classList.toggle("sel", P.isPicked(owner, keyOf(tr))));
  P.preview = (t0, t1) => t0 == null ? mapHighlightBy(sel && sel.col, sel && sel.lo, sel && sel.hi, false) : mapHighlightBy(0, t0, t1, false);
  const bands = (lo, hi) => Object.values(P.charts).forEach(c => c && c.setBand(lo, hi));
  P.paintSel = () => {
    const box = $("#speed-sel");
    if (!sel) { bands(null); if (box) box.hidden = true; return; }
    const r = idxRange(ser, sel.col, sel.lo, sel.hi);
    if (!r) return;
    const [i0, i1] = r, xi = P.xmode === "km" ? 1 : 0;
    bands(ser[i0][xi], ser[i1][xi]);
    let { mv, hr, pw } = stretchStats(ser, i0, i1);
    // a best effort / speed carries its exact time and distance; the series is downsampled
    const ex = sel.exact, km = ex ? ex.km : ser[i1][1] - ser[i0][1], el = ex ? ex.el : ser[i1][0] - ser[i0][0];
    if (ex) mv = Math.min(mv, el);
    const v = ex ? km / (el / 3600) : mv ? km / (mv / 3600) : null, avg = P.avg, W = P.W;
    box.hidden = false;
    const row = (k, val) => `<div class="row"><span>${k}</span><span>${val}</span></div>`;
    const eg = P.ele ? eleNetGrade(ser, P.ele, i0, i1) : {};
    box.innerHTML = `<div class="selhead"><b>Selected</b> <span class="num muted">${f2(ser[i0][1])}–${f2(ser[i1][1])} km</span><button class="btn small" id="sel-clear" title="Clear selection" aria-label="Clear selection">${ICON.close}</button></div>` +
      row("time", dur(el, el >= 3600) + (mv < el - 2 ? ` <span class="muted">(moving ${dur(mv, mv >= 3600)})</span>` : "")) +
      row("speed", `${f2(v)} km/h` + (isNum(v) && isNum(avg) ? ` <span class="${v >= avg ? "up" : "down"}">${pct((v / avg - 1) * 100)}</span>` : "")) +
      row("pace", `${pace(v)}/km`) + (isNum(hr) ? row("HR", `${f0(hr)} bpm`) : "") +
      (isNum(pw) ? row("power", `${f0(pw)} W` + (isNum(W) && W ? ` <span class="${pw >= W ? "up" : "down"}">${pct((pw / W - 1) * 100)}</span>` : "")) : "") +
      (isNum(eg.net) ? row("elev", `${fnet(eg.net)}${isNum(eg.g) ? ` <span class="muted">(${fgrade(eg.g)})</span>` : ""}`) : "");
    $("#sel-clear").onclick = () => { P.clearRowSel(); hl.highlightBy(null); };
  };
  hl.highlightBy = (col, lo, hi, fit, exact) => { sel = lo == null ? null : { col, lo, hi, exact }; mapHighlightBy(col, lo, hi, fit); P.paintSel(); };
}
