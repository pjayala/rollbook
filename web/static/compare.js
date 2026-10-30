// SPDX-License-Identifier: GPL-3.0-or-later
// Copyright (C) 2026 Pedro Ayala. Rollbook is free software, see LICENSE.
"use strict";
/* Compare two sessions (#16): #/compare/<subject>/<reference>.
   The subject (the session you came from) is drawn in the data hue, the
   reference in white; every change is subject − reference. Loaded after
   app.js and uses its helpers (chart, esc, PAL, api, …). The pure functions
   at the top are run under node by tests/test_frontend_compare.py. */

// ------------------------------------------------------------------ pure helpers

const CMP_THR = 0.5;  // m/s, the pause threshold of `db build` and the server

/** Cumulative moving seconds: a step counts when its speed is at least CMP_THR. */
function movingClock(t, km) {
  const out = [];
  let acc = 0;
  for (let i = 0; i < t.length; i++) {
    if (i) { const dt = t[i] - t[i - 1]; if (dt > 0 && (km[i] - km[i - 1]) * 1000 / dt >= CMP_THR) acc += dt; }
    out.push(acc);
  }
  return out;
}

/** Arrays from an activity's series rows [t, km, kmh, hr, ele, lat, lon, watts, mv].
 * km is made non-decreasing (GPS jitter), so "time at a distance" is the first arrival. */
function cmpTrack(ser) {
  const T = { t: [], km: [], mv: [], kmh: [], hr: [], ele: [], lat: [], lon: [] };
  let k = 0, hasMv = true;
  for (const r of ser || []) {
    if (!isNum(r[0])) continue;
    k = Math.max(k, isNum(r[1]) ? r[1] : 0);
    T.t.push(r[0]); T.km.push(k); T.kmh.push(isNum(r[2]) ? r[2] : null); T.hr.push(r[3] || null);
    T.ele.push(isNum(r[4]) ? r[4] : null); T.lat.push(isNum(r[5]) ? r[5] : null); T.lon.push(isNum(r[6]) ? r[6] : null);
    if (isNum(r[8])) T.mv.push(r[8]); else hasMv = false;
  }
  // the server's full-resolution moving clock; recomputed only if it is missing
  if (!hasMv) T.mv = movingClock(T.t, T.km);
  return T;
}

/** Fractional index of the first point where the non-decreasing `arr` reaches x; null outside. */
function fracIndex(arr, x) {
  const n = arr.length;
  if (!n || !isNum(x) || x < arr[0] - 1e-9 || x > arr[n - 1] + 1e-9) return null;
  if (x <= arr[0]) return 0;
  let lo = 0, hi = n - 1;  // invariant: arr[lo] < x <= arr[hi]
  while (hi - lo > 1) { const mid = (lo + hi) >> 1; if (arr[mid] < x) lo = mid; else hi = mid; }
  const d = arr[hi] - arr[lo];
  return lo + (d > 0 ? (x - arr[lo]) / d : 1);
}

/** Value of `arr` at a fractional index; nulls fall back to the nearer neighbour. */
function lerpAt(arr, fi) {
  if (fi == null || !arr.length) return null;
  const i = Math.max(0, Math.min(arr.length - 1, Math.floor(fi))), f = fi - i, a = arr[i], b = arr[Math.min(arr.length - 1, i + 1)];
  if (!isNum(a) || !isNum(b)) return f < 0.5 ? (isNum(a) ? a : isNum(b) ? b : null) : (isNum(b) ? b : isNum(a) ? a : null);
  return a + (b - a) * f;
}

const cmpClock = (T, mode) => mode === "elapsed" ? T.t : T.mv;

/** Clock time (moving or elapsed) at which T first reaches x km; null beyond its end. */
function atDistance(T, mode, x) { return lerpAt(cmpClock(T, mode), fracIndex(T.km, x)); }

/** The track re-zeroed at elapsed time t0 (a segment's or lap gate's start): rows before
 * it dropped, the first row interpolated, time, distance and moving clock starting at 0. */
function alignAt(T, t0) {
  const fi = fracIndex(T.t, t0);
  if (fi == null) return T;
  const i0 = Math.ceil(fi - 1e-9), keys = Object.keys(T), out = {};
  for (const k of keys) out[k] = [lerpAt(T[k], fi), ...T[k].slice(i0 > fi + 1e-9 ? i0 : i0 + 1)];
  const z = { t: out.t[0], km: out.km[0], mv: out.mv[0] };
  for (const k of ["t", "km", "mv"]) out[k] = out[k].map(v => Math.max(0, +(v - z[k]).toFixed(6)));
  return out;
}

/** Time gap along the distance both covered: g = reference time − subject time at x km,
 * so positive = the subject got there first. Points every `step` km, the last at the end. */
function timeGap(A, B, mode, step) {
  const end = Math.min(A.km[A.km.length - 1] || 0, B.km[B.km.length - 1] || 0), out = [];
  if (!(end > 0) || !(step > 0)) return out;
  const n = Math.ceil(end / step - 1e-9);
  for (let i = 0; i <= n; i++) {
    const x = Math.min(end, i * step), ta = atDistance(A, mode, x), tb = atDistance(B, mode, x);
    if (isNum(ta) && isNum(tb)) out.push({ x: +x.toFixed(6), g: tb - ta, ta, tb });
  }
  return out;
}

/** Final gap, the largest one (sign kept) and where the lead changed hands (1 s hysteresis). */
function gapStats(pts) {
  if (!pts.length) return null;
  let max = pts[0], side = 0;
  const cross = [];
  for (const p of pts) {
    if (Math.abs(p.g) > Math.abs(max.g)) max = p;
    const s = p.g >= 1 ? 1 : p.g <= -1 ? -1 : 0;
    if (s && side && s !== side) cross.push({ x: p.x, to: s });
    if (s) side = s;
  }
  return { final: pts[pts.length - 1].g, end: pts[pts.length - 1].x, max: { x: max.x, g: max.g }, cross };
}

/** The stretch (a quarter of the distance, at least 1 km) that gained the most in the
 * direction of the final gap, if it explains at least half of it. */
function wonWhere(pts) {
  const st = gapStats(pts);
  if (!st || Math.abs(st.final) < 5) return null;
  const L = Math.max(1, st.end / 4), sg = Math.sign(st.final);
  let best = null;
  for (let i = 0, j = 0; i < pts.length; i++) {
    while (j < pts.length - 1 && pts[j].x - pts[i].x < L - 1e-9) j++;
    if (pts[j].x - pts[i].x < L - 1e-9) break;
    const d = pts[j].g - pts[i].g;
    if (!best || d * sg > best.d * sg) best = { x0: pts[i].x, x1: pts[j].x, d };
  }
  return best && best.d * sg >= Math.abs(st.final) / 2 ? best : null;
}

/** Signed gap as m:ss ("+1:12", "−0:05", "0:00"); hours when needed. */
function fgap(s) {
  if (!isNum(s)) return "–";
  const r = Math.round(s);
  if (!r) return "0:00";
  const a = Math.abs(r), h = Math.floor(a / 3600), m = Math.floor(a % 3600 / 60), ss = a % 60, p = n => String(n).padStart(2, "0");
  return (r > 0 ? "+" : "−") + (h ? `${h}:${p(m)}:${p(ss)}` : `${m}:${p(ss)}`);
}

/** Per-km times of both on one clock, from rounded cumulative times, so the rows' changes
 * add up exactly to the final gap. The last row is the part km to the common end. */
function cmpSplits(A, B, mode) {
  const end = Math.min(A.km[A.km.length - 1] || 0, B.km[B.km.length - 1] || 0), rows = [];
  if (!(end > 0)) return rows;
  const marks = [];
  for (let k = 1; k <= end + 1e-9; k++) marks.push(k);
  if (end - (marks[marks.length - 1] || 0) > 0.05) marks.push(end);
  let pa = 0, pb = 0, px = 0;
  for (const x of marks) {
    const ca = Math.round(atDistance(A, mode, x)), cb = Math.round(atDistance(B, mode, x));
    rows.push({ km: x, len: x - px, a: ca - pa, b: cb - pb, d: (ca - pa) - (cb - pb) });
    pa = ca; pb = cb; px = x;
  }
  return rows;
}

/** Sessions-list selection: toggling a picked id removes it, a third pick drops the oldest. */
function pickToggle(sel, id) {
  return sel.includes(id) ? sel.filter(x => x !== id) : [...sel, id].slice(-2);
}

/** The two ids of #/compare/<a>/<b>, or {error}. Ids are decimal (Strava's or an import's). */
function parseCompareHash(a, b) {
  const ok = v => /^\d{1,15}$/.test(v || "");
  if (!a || !b) return { error: "This compare link is missing a session." };
  if (!ok(a) || !ok(b)) return { error: "This compare link is not valid." };
  if (a === b) return { error: "Pick two different sessions to compare." };
  return { a: +a, b: +b };
}

/** Metres between two lat/lon points (equirectangular, fine below a few km). */
function llDist(la1, lo1, la2, lo2) {
  const k = Math.PI / 180, x = (lo2 - lo1) * k * Math.cos((la1 + la2) / 2 * k), y = (la2 - la1) * k;
  return Math.hypot(x, y) * 6371000;
}

// ------------------------------------------------------------------ selection store

/** Picked session ids (Sessions list), ids only, validated on read. */
function loadPick() {
  try {
    const v = JSON.parse(sessionStorage.cmpPick || "[]");
    if (Array.isArray(v)) return v.filter(x => Number.isSafeInteger(x) && x >= 0).slice(-2);
  } catch { /* corrupt: start over */ }
  delete sessionStorage.cmpPick;
  return [];
}
const savePick = sel => { sessionStorage.cmpPick = JSON.stringify(sel); };

// ------------------------------------------------------------------ page

const HR_REF = "#b05d7a";  // the reference's heart rate: the rose ramp, one step down
const cmpDate = (a, other) => {
  const d = new Date(a.datetime || a.date), o = new Date(other.datetime || other.date);
  let s = `${d.getDate()} ${MON[d.getMonth()]}`;
  if (d.getFullYear() !== o.getFullYear()) s += ` ${d.getFullYear()}`;
  if (a.date === other.date) s += ` ${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`;
  return s;
};
const fclock = s => isNum(s) ? dur(s, s >= 3600) : "–";
const isVirt = a => /^Virtual/.test(a.type || "");

/** Metrics of the strip and the all-metrics table: value getter, decimals, unit and the
 * good direction (+1 higher is better, −1 lower is better, 0 neither: drawn grey). */
function cmpMetrics(A, B) {
  const m = x => x.metrics || {}, eff = x => x.hr_avg && x.best10min_kmh ? x.best10min_kmh / x.hr_avg * 100 : null;
  const lapBest = x => { const L = m(x).laps; const c = L && L.laps.filter(l => l.counted); return c && c.length ? Math.min(...c.map(l => l.elapsed)) : null; };
  const sameKm = isNum(A.km) && isNum(B.km) && Math.abs(A.km / B.km - 1) <= 0.02;
  return {
    kmh: { n: "Avg speed (moving)", get: x => x.kmh_moving, dp: 2, u: "km/h", good: 1 },
    b10: { n: "Best 10 min", get: x => x.best10min_kmh, dp: 2, u: "km/h", good: 1 },
    b20: { n: "Best 20 min", get: x => x.best20min_kmh, dp: 2, u: "km/h", good: 1 },
    b5: { n: "Best 5 min", get: x => x.best5min_kmh, dp: 2, u: "km/h", good: 1 },
    b30s: { n: "Top speed (30 s)", get: x => x.best30_kmh, dp: 1, u: "km/h", good: 1 },
    b60: { n: "Best 60 min", get: x => m(x).b60, dp: 2, u: "km/h", good: 1 },
    wn: { n: "Wind-neutral 10 min", get: x => m(x).wind_neutral, dp: 2, u: "km/h", good: 1 },
    adj: { n: "Conditions-adjusted 10 min", get: x => x.cond && x.cond.adjusted, dp: 2, u: "km/h", good: 1 },
    lap: { n: "Best lap", get: lapBest, time: 1, good: -1 },
    eff: { n: "Efficiency", get: eff, dp: 2, u: "best 10 per 100 bpm", good: 1 },
    hr: { n: "Avg heart rate", get: x => x.hr_avg, dp: 0, u: "bpm", good: 0 },
    hrmax: { n: "Max heart rate", get: x => x.hr_max, dp: 0, u: "bpm", good: 0 },
    km: { n: "Distance", get: x => x.km, dp: 2, u: "km", good: 0 },
    mv: { n: "Moving time", get: x => x.moving_s, time: 1, good: sameKm ? -1 : 0 },
    el: { n: "Elapsed time", get: x => x.elapsed_s, time: 1, good: sameKm ? -1 : 0 },
    kmhe: { n: "Avg speed (with pauses)", get: x => x.kmh_elapsed, dp: 2, u: "km/h", good: 0 },
    pause: { n: "Paused", get: x => x.pause_s, time: 1, good: 0 },
    asc: { n: "Ascent", get: x => x.ascent_m, dp: 0, u: "m", good: 0 },
    ...Object.fromEntries(BE.map(b => ["be" + b.k, { n: `Best ${b.short || b.n}`, get: x => isVirt(x) ? null : (((x.metrics || {}).be || {})[b.k] || [])[0], time: 1, good: -1 }])),
    w: { n: "Avg power", get: x => pwAvg(x), dp: 0, u: "W", good: 1 },
    np: { n: "Weighted power", get: x => m(x).np, dp: 0, u: "W", good: 1 },
    w20: { n: "Best 20 min power", get: x => (m(x).pw || {})["1200"], dp: 0, u: "W", good: 1 },
    wbpm: { n: "Power per heart beat", get: x => pwAvg(x) && x.hr_avg ? pwAvg(x) / x.hr_avg : null, dp: 2, u: "W per bpm", good: 1 },
    cad: { n: "Cadence", get: x => x.cad_avg, dp: 0, u: "rpm", good: 0 },
    kcal: { n: "Calories", get: x => x.cal ? x.cal.kcal : null, dp: 0, u: "kcal", good: 0 },
  };
}
/** {v, sub, cls} display of one metric for subject A against reference B. */
function cmpCell(d, A, B, refLabel) {
  const va = d.get(A), vb = d.get(B);
  const show = v => !isNum(v) ? "–" : d.time ? fclock(v) : v.toFixed(d.dp);
  if (!isNum(va) || !isNum(vb)) return { v: show(va), vb: show(vb), delta: "–", cls: "muted" };
  // the change of the values as displayed, so it adds up to what is on screen
  const ra = d.time ? Math.round(va) : +va.toFixed(d.dp), rb = d.time ? Math.round(vb) : +vb.toFixed(d.dp), dv = +(ra - rb).toFixed(d.dp || 0);
  const tiny = !dv || (d.u === "km/h" && Math.abs(dv) < 0.1);
  const txt = tiny ? "±0" : d.time ? fgap(dv) : (dv > 0 ? "+" : "−") + Math.abs(dv).toFixed(d.dp);
  const cls = tiny || !d.good ? "muted" : dv * d.good > 0 ? "up" : "down";
  return { v: show(va), vb: show(vb), delta: txt, cls };
}

async function viewCompare(argA, argB, seq) {
  const app = $("#app");
  const frame = msg => `<div class="head"><div><h1>Compare sessions</h1><div class="muted">${esc(msg)}</div></div></div>
    <div class="panel"><p>Open a session and use <b>Compare…</b>, or tick two rows in <a href="#/sessions">Sessions</a>.</p></div>`;
  const ids = parseCompareHash(argA, argB);
  if (ids.error) { app.innerHTML = frame(ids.error); return; }
  app.innerHTML = `<p class="muted">Loading both sessions…</p>`;
  const got = await Promise.allSettled([api("activity/" + ids.a), api("activity/" + ids.b)]);
  if (seq !== routeSeq) return;  // left while loading
  const bad = got.map((r, i) => r.status === "rejected" ? `session ${[ids.a, ids.b][i]} ${/not found/.test(r.reason && r.reason.message) ? "was not found" : "could not be loaded"}` : null).filter(Boolean);
  if (bad.length) { app.innerHTML = frame(bad.join("; ").replace(/^s/, "S") + "."); return; }
  const [A, B] = got.map(r => r.value);
  const peers = await loadActs(A.type).catch(() => []);
  if (seq !== routeSeq) return;
  renderCompare(A, B, peers, seq);
}

function renderCompare(A, B, peers, seq) {
  const app = $("#app");
  const labA = cmpDate(A, B), labB = cmpDate(B, A);
  const sameName = (A.name || "") === (B.name || "");
  const title = sameName ? `${esc(A.name)} · ${esc(labA)} vs ${esc(labB)}` : `${esc(A.name)} ${esc(labA)} vs ${esc(B.name)} ${esc(labB)}`;
  const TA0 = cmpTrack(A.series), TB0 = cmpTrack(B.series);
  const lastKm = T => T.km.length ? T.km[T.km.length - 1] : 0;
  const hasDist = lastKm(TA0) > 0.2 && lastKm(TB0) > 0.2;
  // simulated speeds: comparable only on the same virtual route (same name, distance within 2%)
  const virtMix = isVirt(A) !== isVirt(B) || (isVirt(A) && isVirt(B) && (A.name !== B.name || !(Math.abs(A.km / B.km - 1) <= 0.02)));
  const gps = T => T.lat.some(isNum);
  const hasGps = gps(TA0) || gps(TB0), hasHr = TA0.hr.some(isNum) || TB0.hr.some(isNum);
  const hasPw = isNum(pwAvg(A)) || isNum(pwAvg(B));
  const M = cmpMetrics(A, B), mA = A.metrics || {}, mB = B.metrics || {};
  let mode = localStorage.cmpClock === "elapsed" ? "elapsed" : "moving";
  let align = "start", segRows = null, segFirst = null;

  // strip: always six cells, so desktop 6, tablet 3 × 2 and phone 2 × 3 rows are full
  const outback = mA.route === "out-back" && mB.route === "out-back", laps = mA.laps && mB.laps;
  const indoor = !hasGps && hasPw;
  const keys = indoor ? ["w", "np", "w20", "hr", "wbpm", "cad"]
    : ["kmh", "b10", outback ? "wn" : laps ? "lap" : "b20", hasHr ? "eff" : "b30s", hasHr ? "hr" : "asc", "pause"];
  const strip = cards(keys.map(k => {
    const c = cmpCell(M[k], A, B);
    const su = /^(km\/h|W|bpm|m|rpm|km|kcal)$/.test(M[k].u || "") ? M[k].u : "";
    return { k: M[k].n, v: esc(c.v) + (su && c.v !== "–" ? ` <span class="unit">${esc(su)}</span>` : ""), s: `vs ${esc(c.vb)} · <span class="${c.cls}">${esc(c.delta)}</span>` };
  }));
  const ctx = [[A, labA], [B, labB]].filter(([x]) => x.cond && isNum(x.cond.temp))
    .map(([x, l]) => `${esc(l)} ${f0(x.cond.temp)}°C, wind ${fwind(x.cond)}`).join(" · ");
  const flags = [A, B].filter(x => x.hidden).map(x => `<span class="pill">${esc(cmpDate(x, x === A ? B : A))} ${x.deleted ? "deleted" : "hidden"}</span>`).join(" ");

  const right = hasGps ? `<div class="panel span2 stick" id="p-replay"><h2>Replay <span class="muted">both on one clock</span></h2>
      <div id="cmp-map" class="cmpmap"></div>
      <div class="replay"><button class="iconbtn" id="rp-play" aria-label="Play" title="Play (space)">${ICON.play}</button>
        <div class="seg" id="rp-speed" role="group" aria-label="Replay speed"><button data-v="10">10×</button><button data-v="30" class="on">30×</button><button data-v="60">60×</button></div>
        <span class="rp-clock num" id="rp-clock"></span></div>
      <input type="range" id="rp-pos" min="0" max="1" step="1" value="0" aria-label="Replay position">
      <div class="rp-read num" id="rp-read"></div>
      <div class="legend rp-leg"><span><i class="sw-dot" style="background:${PAL.hue}"></i>${esc(labA)}</span><span><i class="sw-dot ghost"></i>${esc(labB)}</span></div></div>`
    : hasPw ? `<div class="panel span2" id="p-pow"><h2>Best power</h2><div id="t-pow"></div></div>` : "";
  app.innerHTML = `
    <div class="head"><div><h1>${title}</h1><div class="muted" id="cmp-find"></div></div>
      <div class="headnav">
        <div class="seg" id="cmp-clock" role="group" aria-label="Clock"><button data-v="moving">Moving</button><button data-v="elapsed">Elapsed</button></div>
        <button class="iconbtn" id="cmp-swap" title="Swap: make ${esc(labB)} the subject" aria-label="Swap the two sessions">${ICON.swap}</button>
        <button class="btn" id="cmp-change">Change…</button>
      </div></div>
    ${flags ? `<div class="cmpnote">${flags} Left out of stats, lists and segments.</div>` : ""}
    <div class="cmpnote" id="cmp-note" hidden></div>
    ${strip}
    ${ctx ? `<div class="cmpctx">${ctx}</div>` : ""}
    <div class="cmpgrid">
      <div class="panel ${right ? "span4" : "span6"}" id="p-lead"></div>
      ${right}
    </div>
    <div class="grid g2 top">
      <div class="panel" id="p-splits"></div>
      <div class="cmpstack"><div class="panel" id="p-csegs" hidden></div><div class="panel"><h2>All metrics <span class="muted">${esc(labA)} − ${esc(labB)}</span></h2><div id="t-all"></div></div></div>
    </div>`;

  // all metrics: rows that either session has
  $("#t-all").innerHTML = `<table><thead><tr><th>metric</th><th>${esc(labA)}</th><th>${esc(labB)}</th><th>change</th></tr></thead><tbody>` +
    Object.values(M).map(d => [d, cmpCell(d, A, B)]).filter(([, c]) => c.v !== "–" || c.vb !== "–")
      .map(([d, c]) => `<tr><td class="l">${esc(d.n)}${d.u && !d.time ? ` <span class="muted small">${esc(d.u)}</span>` : ""}</td><td>${esc(c.v)}</td><td>${esc(c.vb)}</td><td class="${c.cls}">${esc(c.delta)}</td></tr>`).join("") + `</tbody></table>`;
  if (!hasGps && hasPw) {
    const W = [["5", "5 s"], ["30", "30 s"], ["60", "1 min"], ["300", "5 min"], ["1200", "20 min"], ["1800", "30 min"], ["3600", "60 min"]];
    $("#t-pow").innerHTML = `<table><thead><tr><th>window</th><th>${esc(labA)}</th><th>${esc(labB)}</th><th>change</th></tr></thead><tbody>` + W.map(([k, n]) => {
      const c = cmpCell({ get: x => ((x.metrics || {}).pw || {})[k], dp: 0, good: 1 }, A, B);
      return c.v === "–" && c.vb === "–" ? "" : `<tr><td class="l">${n}</td><td>${esc(c.v)} W</td><td>${esc(c.vb)} W</td><td class="${c.cls}">${esc(c.delta)}</td></tr>`;
    }).join("") + `</tbody></table>`;
  }

  // header controls
  const clockSeg = $("#cmp-clock");
  const paintClock = () => clockSeg.querySelectorAll("button").forEach(b => { const on = b.dataset.v === mode; b.classList.toggle("on", on); b.setAttribute("aria-pressed", on); });
  paintClock();
  clockSeg.onclick = e => { const b = e.target.closest("button"); if (!b || b.dataset.v === mode) return; mode = b.dataset.v; localStorage.cmpClock = mode; paintClock(); draw(); };
  $("#cmp-swap").onclick = () => location.replace(`#/compare/${B.id}/${A.id}`);
  $("#cmp-change").onclick = () => { const self = peers.find(p => p.id === A.id) || { ...A, ...mA, _t: +new Date(A.date + "T12:00:00"), _d: new Date(A.date + "T12:00:00") }; comparePicker(self, mA, peers); };

  // alignment: from each start, unless the starts are far apart (then the first shared segment)
  const first = T => { const i = T.lat.findIndex(isNum); return i < 0 ? null : [T.lat[i], T.lon[i]]; };
  const sa = first(TA0), sb = first(TB0), startGap = sa && sb ? llDist(sa[0], sa[1], sb[0], sb[1]) : null;
  const gateGap = laps && mA.laps.gate && mB.laps.gate ? llDist(mA.laps.gate[0], mA.laps.gate[1], mB.laps.gate[0], mB.laps.gate[1]) : null;
  const tracks = () => {
    if (align === "seg" && segFirst) return [alignAt(TA0, segFirst.ta), alignAt(TB0, segFirst.tb)];
    if (align === "lap" && laps) return [alignAt(TA0, mA.laps.laps[0].t0), alignAt(TB0, mB.laps.laps[0].t0)];
    return [TA0, TB0];
  };
  const paintNote = () => {
    const n = $("#cmp-note"), parts = [], acts = [];
    if (A.type !== B.type) parts.push(`Different sports (${esc(A.type)} and ${esc(B.type)})${virtMix ? ": virtual speeds are simulated, so speed and gap are left out" : ""}.`);
    else if (virtMix) parts.push("Different virtual routes, so speed and gap are left out; compare the power.");
    if (align === "seg" && segFirst) {
      parts.push(`Starts are ${f0(startGap)} m apart, so the gap is measured from ${esc(segName(segFirst.sg))} (km ${f2(segFirst.ka)} on ${esc(labA)}, ${f2(segFirst.kb)} on ${esc(labB)}).`);
      acts.push(`<button class="linkbtn" data-al="start">Measure from the start</button>`);
    } else if (align === "lap") {
      parts.push(`Measured from the first pass of the lap gate.`);
      acts.push(`<button class="linkbtn" data-al="start">Measure from the start</button>`);
    } else {
      if (isNum(startGap) && startGap > 150) parts.push(`Starts are ${f0(startGap)} m apart${segRows ? "; no shared segment to line them up on" : ""}.`);
      if (segFirst && isNum(startGap) && startGap > 150) acts.push(`<button class="linkbtn" data-al="seg">Measure from ${esc(segName(segFirst.sg))}</button>`);
    }
    if (laps && isNum(gateGap) && gateGap < 60 && align !== "lap") {
      if (!parts.length) parts.push(`Both lapped the same ${f2(mA.laps.lap_km)} km circuit.`);
      acts.push(`<button class="linkbtn" data-al="lap">Measure from the lap gate</button>`);
    }
    if (outback && isNum(mA.turn_km) && isNum(mB.turn_km) && Math.abs(mA.turn_km - mB.turn_km) > 0.3)
      parts.push(`Turnarounds differ by ${f2(Math.abs(mA.turn_km - mB.turn_km))} km, so the way back lines up less well.`);
    n.hidden = !parts.length && !acts.length;
    n.innerHTML = parts.join(" ") + (acts.length ? " " + acts.join(" ") : "");
    n.querySelectorAll("[data-al]").forEach(b => b.onclick = () => { align = b.dataset.al; draw(); });
  };

  // replay state, kept across redraws
  const R = { T: 0, playing: false, speed: 30, raf: 0, last: 0, end: 0, map: null, hoverKm: null };
  let charts = [], TA = TA0, TB = TB0;
  const stop = () => { R.playing = false; if (R.raf) cancelAnimationFrame(R.raf); R.raf = 0; const b = $("#rp-play"); if (b) { b.innerHTML = ICON.play; b.setAttribute("aria-label", "Play"); } };
  const onVis = () => { if (document.hidden) stop(); };
  document.addEventListener("visibilitychange", onVis);
  onLeave(() => { stop(); document.removeEventListener("visibilitychange", onVis); if (R.map) R.map.destroy(); });

  function draw() {
    [TA, TB] = tracks();
    paintNote();
    drawLead();
    drawSplits();
    if (hasGps) setupReplay();
  }

  function drawLead() {
    const host = $("#p-lead");
    if (!hasDist || virtMix) {
      host.innerHTML = `<h2>Time gap</h2><p class="muted">${virtMix ? (isVirt(A) && isVirt(B) ? "Different virtual routes: speeds are simulated, so there is no fair gap. Best power is the comparison here." : "One of these is a virtual ride: its speed is simulated, so there is no fair gap to draw.") : "One of these sessions has no distance channel, so there is no gap to draw."}</p>`;
      $("#cmp-find").textContent = `${A.type} on ${labA} against ${labB}.`;
      return;
    }
    const end = Math.min(lastKm(TA), lastKm(TB)), maxKm = Math.max(lastKm(TA), lastKm(TB));
    const pts = timeGap(TA, TB, mode, Math.max(0.02, end / 500)), st = gapStats(pts), won = wonWhere(pts);
    // the headline from rounded clock times, the same numbers the splits add up to
    const ta = Math.round(atDistance(TA, mode, end)), tb = Math.round(atDistance(TB, mode, end)), gap = tb - ta;
    const dk = ta && tb ? end / (ta / 3600) - end / (tb / 3600) : null;
    const word = gap > 0 ? "faster" : gap < 0 ? "slower" : "";
    const clockName = mode === "elapsed" ? "elapsed, pauses included" : "moving";
    $("#cmp-find").innerHTML = (gap ? `${esc(fgap(gap).slice(1))} ${word} than ${esc(labB)}` : `Level with ${esc(labB)}`) +
      ` over the ${f2(end)} km both covered (${clockName})` + (isNum(dk) ? `, ${dk >= 0 ? "+" : "−"}${Math.abs(dk).toFixed(2)} km/h` : "") +
      (won ? `; most of it in km ${f1(won.x0)}–${f1(won.x1)} (${esc(fgap(won.d))})` : "") + ".";
    host.innerHTML = `<div class="leadhead"><h2>Time gap <span class="muted">${esc(labA)} ahead is up · ${mode}</span></h2>
        <div class="leadfig num">${gap ? `${esc(fgap(gap).slice(1))} <span>${word}</span>` : "level"}</div></div>
      <div id="c-gap"></div>
      <div class="subhead"><b>Speed</b> <span class="muted small">km/h, 30 s smoothed</span></div><div id="c-spd"></div>
      ${hasHr ? `<div class="subhead"><b>Heart rate</b> <span class="muted small">bpm${TA.hr.some(isNum) && TB.hr.some(isNum) ? "" : ` · ${esc(TA.hr.some(isNum) ? labB : labA)} has no heart rate`}</span></div><div id="c-hr"></div>` : ""}
      ${TA.ele.some(isNum) ? `<div class="subhead"><b>Elevation</b> <span class="muted small">m, ${esc(labA)}</span></div><div id="c-ele"></div>` : ""}`;
    const lo = Math.min(0, ...pts.map(p => p.g)), hi = Math.max(0, ...pts.map(p => p.g)), yt = niceTicks(lo, hi, 4);
    const marks = [];
    if (st) {
      marks.push({ x: st.end, y: st.final, label: `${fgap(st.final)} at ${f1(st.end)} km`, color: PAL.hue });
      if (Math.abs(st.max.g) >= 3 && Math.abs(st.max.g - st.final) >= 3 && st.max.x < st.end * 0.97) marks.push({ x: st.max.x, y: st.max.g, label: `max ${fgap(st.max.g)} at ${f1(st.max.x)} km` });
      for (const c of st.cross.slice(0, 3)) marks.push({ x: c.x, y: 0, label: `${c.to > 0 ? labA : labB} ahead from ${f1(c.x)} km` });
      if (mode === "elapsed") {  // a stop shows as a step: name the big ones
        const steps = [];
        for (let i = 1; i < pts.length; i++) { const d = pts[i].g - pts[i - 1].g; if (Math.abs(d) >= 20) steps.push({ x: pts[i].x, d }); }
        for (const s of steps.sort((p, q) => Math.abs(q.d) - Math.abs(p.d)).slice(0, 2))
          marks.push({ x: s.x, y: pts.find(p => p.x === s.x).g, label: `stop ${fclock(Math.abs(s.d))} (${s.d > 0 ? labB : labA})` });
      }
    }
    const xcfg = { type: "linear", min: 0, max: maxKm, fmt: v => +v.toFixed(1) + "" };
    const tipAt = x => {
      const t1 = atDistance(TA, mode, x), t2 = atDistance(TB, mode, x);
      const row = (l, v) => `<div class="row"><span>${esc(l)}</span><span>${v}</span></div>`;
      const v1 = lerpAt(TA.kmh, fracIndex(TA.km, x)), v2 = lerpAt(TB.kmh, fracIndex(TB.km, x));
      return `<b>${f2(x)} km</b>` + row(labA, `${fclock(t1)} · ${f1(v1)} km/h`) + row(labB, `${fclock(t2)} · ${f1(v2)} km/h`) +
        (isNum(t1) && isNum(t2) ? row("gap", esc(fgap(t2 - t1))) : "");
    };
    const sync = x => { for (const c of charts) c.cursor(x); R.hoverKm = x; paintDots(); };
    const base = { x: xcfg, tip: x => tipAt(x), onHover: sync };
    charts = [];
    charts.push(chart($("#c-gap"), { ...base, height: 220, legend: false, refY: 0, zeroLine: true, marks,
      sides: { hi: `${labA} ahead ↑`, lo: `${labB} ahead ↓` }, y: { min: yt.lo, max: yt.hi, fmt: fgap },
      series: [
        { name: "ahead", kind: "area", base: 0, color: PAL.hue, opacity: .14, data: pts.map(p => ({ x: p.x, y: Math.max(0, p.g) })) },
        { name: "behind", kind: "area", base: 0, color: PAL.ink, opacity: .08, data: pts.map(p => ({ x: p.x, y: Math.min(0, p.g) })) },
        { name: "gap", kind: "line", color: PAL.hue, width: 2, data: pts.map(p => ({ x: p.x, y: p.g })) }] }));
    const grid = T => { const out = [], e = lastKm(T), st_ = Math.max(0.02, maxKm / 600); for (let x = 0; x <= e + 1e-9; x += st_) out.push(x); out.push(e); return out; };
    const along = (T, key) => grid(T).map(x => ({ x, y: lerpAt(T[key], fracIndex(T.km, x)) })).filter(p => isNum(p.y));
    charts.push(chart($("#c-spd"), { ...base, height: 160, y: { fmt: v => v.toFixed(0), ticks: 3 },
      series: [{ name: labA, kind: "line", color: PAL.hue, width: 2, data: along(TA, "kmh") }, { name: labB, kind: "line", color: PAL.ink, width: 1.5, data: along(TB, "kmh") }] }));
    if (hasHr) charts.push(chart($("#c-hr"), { ...base, height: 100, y: { fmt: v => v.toFixed(0), ticks: 2 },
      series: [{ name: labA, kind: "line", color: PAL.hr, width: 2, data: along(TA, "hr") }, { name: labB, kind: "line", color: HR_REF, width: 1.5, data: along(TB, "hr") }].filter(s => s.data.length) }));
    if ($("#c-ele")) charts.push(chart($("#c-ele"), { ...base, height: 80, legend: false, y: { fmt: v => v.toFixed(0), ticks: 2 },
      series: [{ name: "elevation", kind: "area", color: PAL.grey, opacity: .22, data: along(TA, "ele") }, { name: "elevation", kind: "line", color: PAL.soft, width: 1.5, data: along(TA, "ele") }] }));
  }

  function drawSplits() {
    const host = $("#p-splits");
    if (!hasDist || virtMix) { host.innerHTML = `<h2>Splits</h2><p class="muted">${virtMix ? "Simulated speeds on different routes: no fair splits." : "No distance to split on."}</p>`; return; }
    const lapsOk = laps && Math.abs(mA.laps.lap_km / mB.laps.lap_km - 1) < 0.05;
    const view = host.dataset.view === "laps" && lapsOk ? "laps" : "km";
    const td = d => `<td class="${Math.abs(d) >= 3 ? (d < 0 ? "up" : "down") : "muted"}">${esc(fgap(d))}</td>`;
    let body;
    if (view === "laps") {
      const la = mA.laps.laps, lb = mB.laps.laps, n = Math.max(la.length, lb.length);
      const rows = [];
      for (let i = 0; i < n; i++) {
        const x = la[i], y = lb[i], d = x && y ? Math.round(x.elapsed) - Math.round(y.elapsed) : null;
        rows.push(`<tr><td class="l">${i + 1}</td><td>${x ? fclock(x.elapsed) : "–"}</td><td>${y ? fclock(y.elapsed) : "–"}</td>${isNum(d) ? td(d) : `<td class="muted">–</td>`}</tr>`);
      }
      body = `<table><thead><tr><th>lap</th><th>${esc(labA)}</th><th>${esc(labB)}</th><th>change</th></tr></thead><tbody>${rows.join("")}</tbody></table>
        <div class="note">Lap times are gate to gate, whatever the clock.</div>`;
    } else {
      const rows = cmpSplits(TA, TB, mode), tot = rows.reduce((s, r) => s + r.d, 0);
      const end = Math.min(lastKm(TA), lastKm(TB));
      console.assert(tot === Math.round(atDistance(TA, mode, end)) - Math.round(atDistance(TB, mode, end)), "splits must add up to the gap");
      const pace = (s, km) => km > 0 ? fclock(s / km) : "–";
      body = `<div class="scroll"><table><thead><tr><th>km</th><th>${esc(labA)} /km</th><th>${esc(labB)} /km</th><th>change</th></tr></thead><tbody>` +
        rows.map(r => `<tr><td class="l">${r.len < 0.999 ? f2(r.km) : f0(r.km)}</td><td>${pace(r.a, r.len)}</td><td>${pace(r.b, r.len)}</td>${td(r.d)}</tr>`).join("") +
        `<tr class="total"><td class="l">total</td><td>${fclock(rows.reduce((s, r) => s + r.a, 0))}</td><td>${fclock(rows.reduce((s, r) => s + r.b, 0))}</td>${td(tot)}</tr></tbody></table></div>
        <div class="note">${mode === "moving" ? "Moving time" : "Elapsed time"} per km over the distance both covered; the changes add up to the gap.</div>`;
    }
    host.innerHTML = `<div class="toolbar"><h2 style="margin:0">Splits</h2>${lapsOk ? `<div class="seg" id="sp-view"><button data-v="km" class="${view === "km" ? "on" : ""}">per km</button><button data-v="laps" class="${view === "laps" ? "on" : ""}">per lap</button></div>` : ""}</div>${body}`;
    if (lapsOk) $("#sp-view").onclick = e => { const b = e.target.closest("button"); if (b) { host.dataset.view = b.dataset.v; drawSplits(); } };
  }

  // ---------------------------------------------------------------- replay
  function setupReplay() {
    const clkA = () => cmpClock(TA, mode), clkB = () => cmpClock(TB, mode);
    R.end = Math.max(clkA()[clkA().length - 1] || 0, clkB()[clkB().length - 1] || 0);
    R.T = Math.min(R.T, R.end);
    const pos = $("#rp-pos");
    pos.max = String(Math.max(1, Math.round(R.end)));
    pos.value = String(Math.round(R.T));
    if (!R.map) {
      const host = $("#cmp-map");
      R.map = new SlippyMap(host);
      const line = T => T.lat.map((la, i) => isNum(la) && isNum(T.lon[i]) ? merc(la, T.lon[i]) : null).filter(Boolean);
      R.la = line(TA0); R.lb = line(TB0);
      R.map.layers.push({
        draw(ctx, mp) {
          ctx.lineJoin = ctx.lineCap = "round";
          const path = (P, col, w, alpha) => { if (P.length < 2) return; ctx.beginPath(); P.forEach((p, i) => { const [x, y] = mp.toScreen(p[0], p[1]); i ? ctx.lineTo(x, y) : ctx.moveTo(x, y); }); ctx.globalAlpha = alpha; ctx.strokeStyle = col; ctx.lineWidth = w; ctx.stroke(); ctx.globalAlpha = 1; };
          path(R.lb, PAL.ink, 5, .45); path(R.la, PAL.hue, 2, 1);  // the reference wider underneath, so a shared road shows both
          const dot = (p, ghost) => {
            if (!p) return; const [x, y] = mp.toScreen(...merc(p[0], p[1]));
            ctx.beginPath(); ctx.arc(x, y, 6, 0, 7); ctx.lineWidth = 2;
            if (ghost) { ctx.strokeStyle = "#000"; ctx.lineWidth = 5; ctx.stroke(); ctx.strokeStyle = PAL.ink; ctx.lineWidth = 2.5; ctx.stroke(); }
            else { ctx.fillStyle = PAL.hue; ctx.fill(); ctx.strokeStyle = "#000"; ctx.stroke(); }
          };
          dot(R.pb, true); dot(R.pa, false);
        }
      });
      const all = [...R.la, ...R.lb];
      // fitted once to both tracks; the replay never pans or zooms, so it loads no new tiles
      if (all.length) R.map.fit([Math.min(...all.map(p => p[0])), Math.min(...all.map(p => p[1])), Math.max(...all.map(p => p[0])), Math.max(...all.map(p => p[1]))]);
      pos.oninput = () => { stop(); R.T = +pos.value; R.hoverKm = null; paintDots(true); };
      pos.onkeydown = e => {
        if (e.key === "ArrowLeft" || e.key === "ArrowRight") { e.preventDefault(); stop(); R.T = Math.max(0, Math.min(R.end, R.T + (e.key === "ArrowRight" ? 10 : -10))); R.hoverKm = null; paintDots(true); }
      };
      // space plays / pauses anywhere in the panel (a focused button handles its own space)
      $("#p-replay").addEventListener("keydown", e => {
        if (e.key === " " && (e.target === pos || !/^(INPUT|SELECT|TEXTAREA|BUTTON)$/.test(e.target.tagName))) { e.preventDefault(); toggle(); }
      });
      $("#rp-play").onclick = toggle;
      $("#rp-speed").onclick = e => { const b = e.target.closest("button"); if (!b) return; R.speed = +b.dataset.v; $("#rp-speed").querySelectorAll("button").forEach(x => x.classList.toggle("on", x === b)); };
    }
    paintDots(true);
  }
  function toggle() {
    if (R.playing) { stop(); return; }
    if (R.T >= R.end) R.T = 0;
    R.playing = true; R.hoverKm = null; R.last = performance.now();
    const b = $("#rp-play"); b.innerHTML = ICON.pause; b.setAttribute("aria-label", "Pause");
    const step = now => {
      if (!R.playing || !$("#cmp-map") || !$("#cmp-map").isConnected) { stop(); return; }
      R.T = Math.min(R.end, R.T + (now - R.last) / 1000 * R.speed); R.last = now;
      paintDots(true);
      if (R.T >= R.end) { stop(); return; }
      R.raf = requestAnimationFrame(step);
    };
    R.raf = requestAnimationFrame(step);
  }
  /** Both dots: at the same distance while a chart is hovered, else at the same clock time. */
  function paintDots(fromClock) {
    if (!R.map) return;
    let fa, fb;
    const byKm = R.hoverKm != null && !fromClock;
    if (byKm) { fa = fracIndex(TA.km, R.hoverKm); fb = fracIndex(TB.km, R.hoverKm); }
    else {
      const at = (clk, T) => T > clk[clk.length - 1] ? clk.length - 1 : fracIndex(clk, T);
      fa = at(cmpClock(TA, mode), R.T); fb = at(cmpClock(TB, mode), R.T);
    }
    const ll = (T, fi) => { const la = lerpAt(T.lat, fi), lo = lerpAt(T.lon, fi); return isNum(la) && isNum(lo) ? [la, lo] : null; };
    R.pa = ll(TA, fa); R.pb = ll(TB, fb);
    R.map.redraw();
    const ka = lerpAt(TA.km, fa), kb = lerpAt(TB.km, fb);
    if (fromClock) {
      $("#rp-pos").value = String(Math.round(R.T));
      $("#rp-clock").textContent = `${fclock(R.T)} / ${fclock(R.end)}`;
      if (isNum(ka) && (R.playing || R.T > 0)) for (const c of charts) c.cursor(ka);
    }
    const lead = isNum(ka) && isNum(kb) ? ka - kb : null;
    $("#rp-read").textContent = isNum(ka) && isNum(kb)
      ? `${labA} ${f2(ka)} km · ${labB} ${f2(kb)} km` + (!byKm ? (Math.abs(lead) < 0.005 ? " · level" : ` · ${lead > 0 ? labA : labB} ${f0(Math.abs(lead) * 1000)} m ahead`) : " · same place")
      : "";
  }

  draw();
  // shared segments, once the server has them: a table, and the place to line up far-apart starts
  if (A.type === B.type && !virtMix) loadSegs(A.type).then(segs => {
    if (seq !== routeSeq) return;
    const rows = [];
    for (const sg of segs) {
      const ea = sg.efforts.filter(e => e.aid === A.id), eb = sg.efforts.filter(e => e.aid === B.id);
      if (!ea.length || !eb.length) continue;
      const fa = ea.reduce((p, q) => q.t0 < p.t0 ? q : p), fb = eb.reduce((p, q) => q.t0 < p.t0 ? q : p);
      rows.push({ sg, fa, fb, ba: Math.min(...ea.map(e => e.el)), bb: Math.min(...eb.map(e => e.el)) });
    }
    rows.sort((p, q) => p.fa.t0 - q.fa.t0);
    segRows = rows;
    const f0r = rows[0];
    if (f0r) segFirst = { sg: f0r.sg, ta: f0r.fa.t0, tb: f0r.fb.t0, ka: lerpAt(TA0.km, fracIndex(TA0.t, f0r.fa.t0)), kb: lerpAt(TB0.km, fracIndex(TB0.t, f0r.fb.t0)) };
    const host = $("#p-csegs");
    if (rows.length) {
      const list = rows.filter(r => !r.sg.full).concat(rows.filter(r => r.sg.full));
      const cap = 8, many = list.length > cap;
      const rowHtml = r => { const d = Math.round(r.ba) - Math.round(r.bb); return `<tr class="click" data-sid="${esc(r.sg.id)}"><td class="l">${esc(segName(r.sg))}</td><td>${fclock(r.ba)}</td><td>${fclock(r.bb)}</td><td class="${Math.abs(d) >= 3 ? (d < 0 ? "up" : "down") : "muted"}">${esc(fgap(d))}</td></tr>`; };
      host.hidden = false;
      host.innerHTML = `<h2>Shared segments <span class="muted">${rows.length} · best time on each</span></h2>
        <table><thead><tr><th>segment</th><th>${esc(labA)}</th><th>${esc(labB)}</th><th>change</th></tr></thead><tbody>${list.slice(0, cap).map(rowHtml).join("")}</tbody></table>
        ${many ? `<button class="linkbtn" id="seg-all">Show all ${list.length}</button>` : ""}`;
      const wire = () => host.querySelectorAll("tr[data-sid]").forEach(tr => tr.onclick = () => { location.hash = "#/segment/" + tr.dataset.sid; });
      wire();
      if (many) $("#seg-all").onclick = () => { host.querySelector("tbody").innerHTML = list.map(rowHtml).join(""); $("#seg-all").remove(); wire(); };
    }
    // far-apart starts: line up on the first shared segment by itself, and say so
    if (segFirst && isNum(startGap) && startGap > 150 && align === "start") { align = "seg"; draw(); } else paintNote();
  }).catch(() => { });
}

// ------------------------------------------------------------------ picker

/** "Compare with…" for session `a` (its list row, metrics `m`): suggestions, the same route,
 * then every other session of the sport, with a filter. Built from DOM nodes, not markup. */
function comparePicker(a, m, peers) {
  const old = $("#cmp-dlg"); if (old) old.remove();
  const dlg = document.createElement("dialog"); dlg.id = "cmp-dlg"; dlg.className = "dlg cmpdlg";
  const mk = (tag, cls, text) => { const e = document.createElement(tag); if (cls) e.className = cls; if (text != null) e.textContent = text; return e; };
  const head = mk("div", "dlg-head"), h = mk("h2", null, `Compare ${fdate(a._d || new Date(a.date + "T12:00:00"))} ${a.name || ""} with…`);
  const x = mk("button", "iconbtn"); x.type = "button"; x.innerHTML = ICON.close; x.setAttribute("aria-label", "Close"); x.onclick = () => dlg.close();
  head.append(h, x);
  const q = mk("input", "cmpq"); q.type = "search"; q.placeholder = "Filter: name, date, route"; q.maxLength = 80; q.setAttribute("aria-label", "Filter sessions");
  const body = mk("div", "cmplist");
  dlg.append(head, q, body);
  document.body.appendChild(dlg);
  dlg.addEventListener("close", () => dlg.remove());

  const others = peers.filter(p => p.id !== a.id);
  const same = sameRoute(a, m, others), sameIds = new Set(same.map(p => p.id));
  const metric = m.route === "out-back" ? "wind_neutral" : "best10min_kmh";
  const before = same.filter(p => cmpWhen(p, a) < 0).sort((p, r) => cmpWhen(r, p));
  const best = same.filter(p => isNum(p[metric])).sort((p, r) => r[metric] - p[metric])[0];
  const sugg = [];
  if (before[0]) sugg.push([before[0], "last time on this route"]);
  if (best && best !== before[0]) sugg.push([best, "route best"]);
  const iso = p => p.date;
  const row = (p, tag) => {
    const r = mk("a", "cmprow"); r.href = `#/compare/${a.id}/${p.id}`;
    const d = isNum(p.best10min_kmh) && isNum(a.best10min_kmh) ? p.best10min_kmh - a.best10min_kmh : null;
    r.append(mk("span", "c-date num", iso(p)));
    const nm = mk("span", "c-name", p.name || ""); if (tag) nm.append(" ", mk("span", "pill", tag));
    r.append(nm, mk("span", "c-n num", fkm(p.km) + " km"), mk("span", "c-n num", dur(p.moving_s, true)), mk("span", "c-n num", f2(p.kmh_moving)),
      mk("span", "c-n num", f2(p.best10min_kmh)), mk("span", "c-n num muted", isNum(d) ? (d >= 0 ? "+" : "−") + Math.abs(d).toFixed(2) : "–"));
    r.onclick = () => dlg.close();
    return r;
  };
  const cols = () => { const r = mk("div", "cmprow cmphead"); for (const [t, c] of [["date", "c-date"], ["name", "c-name"], ["km", "c-n"], ["moving", "c-n"], ["avg km/h", "c-n"], ["best 10", "c-n"], ["vs this", "c-n"]]) r.append(mk("span", c, t)); return r; };
  const group = (title, list, cap, step) => {
    const g = mk("div", "cmpgroup"); g.append(mk("div", "cmpgh", `${title} · ${list.length}`));
    let n = Math.min(cap, list.length);
    const box = mk("div"); g.append(box);
    const more = mk("button", "linkbtn");
    const fill = () => { box.replaceChildren(...list.slice(0, n).map(p => row(p))); more.hidden = n >= list.length; more.textContent = cap < 20 ? `Show all ${list.length}` : "Show more"; };
    more.onclick = () => { n = Math.min(list.length, n + step); fill(); };
    fill(); g.append(more);
    return g;
  };
  const paint = () => {
    const t = q.value.trim().toLowerCase(), hit = p => !t || `${p.name} ${p.date} ${p.route || ""}`.toLowerCase().includes(t);
    const nodes = [cols()];
    if (!t && sugg.length) { const g = mk("div", "cmpgroup"); g.append(mk("div", "cmpgh", "Suggested"), ...sugg.map(([p, tag]) => row(p, tag))); nodes.push(g); }
    const s = same.filter(hit).sort((p, r) => cmpWhen(r, p)), o = others.filter(p => !sameIds.has(p.id) && hit(p)).sort((p, r) => cmpWhen(r, p));
    if (s.length) nodes.push(group("Same route", s, 8, 1000));
    if (o.length) nodes.push(group(`Other ${(a.type || "sessions").toLowerCase()}`, o, 50, 50));
    if (!s.length && !o.length) nodes.push(mk("p", "muted", t ? "No session matches." : `No other ${(a.type || "").toLowerCase()} sessions yet.`));
    body.replaceChildren(...nodes);
  };
  q.oninput = paint;
  // ↑ / ↓ move between rows, Enter opens (rows are links)
  dlg.addEventListener("keydown", e => {
    if (e.key !== "ArrowDown" && e.key !== "ArrowUp") return;
    const rs = [...body.querySelectorAll("a.cmprow")], i = rs.indexOf(document.activeElement);
    const j = e.key === "ArrowDown" ? (i < 0 ? 0 : Math.min(rs.length - 1, i + 1)) : (i <= 0 ? -1 : i - 1);
    e.preventDefault(); (j < 0 ? q : rs[j]).focus();
  });
  paint();
  dlg.showModal();
  q.focus();
}
