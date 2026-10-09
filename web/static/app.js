// SPDX-License-Identifier: GPL-3.0-or-later
// Copyright (C) 2026 Pedro Ayala. Rollbook is free software, see LICENSE.
"use strict";
/* Rollbook dashboard. Vanilla JS, no dependencies, talks to web/server.py. */

// ------------------------------------------------------------------ utils

const $ = (s, el = document) => el.querySelector(s);
const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const isNum = v => typeof v === "number" && isFinite(v);
const f1 = v => isNum(v) ? v.toFixed(1) : "–";
const f2 = v => isNum(v) ? v.toFixed(2) : "–";
const f0 = v => isNum(v) ? Math.round(v).toString() : "–";
const fkm = v => isNum(v) ? (v >= 100 ? v.toFixed(0) : v.toFixed(2)) : "–";
function dur(s, forceH) {
  if (!isNum(s)) return "–";
  s = Math.round(s);
  const h = Math.floor(s / 3600), m = Math.floor(s % 3600 / 60), ss = s % 60;
  const p = n => String(n).padStart(2, "0");
  return (h || forceH) ? `${h}:${p(m)}:${p(ss)}` : `${m}:${p(ss)}`;
}
const hours = s => isNum(s) ? (s / 3600).toFixed(s >= 360000 ? 0 : 1) + " h" : "–";
const pace = kmh => isNum(kmh) && kmh > 0 ? dur(3600 / kmh) : "–";  // min:ss per km
const pct = v => isNum(v) ? (v > 0 ? "+" : "") + v.toFixed(1) + "%" : "–";
const dateOf = a => new Date(a.date + "T12:00:00");
const DAY = 86400000;
const MON = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
const fdate = d => `${d.getDate()} ${MON[d.getMonth()]} ${d.getFullYear()}`;
const fmon = d => `${MON[d.getMonth()]} ${String(d.getFullYear()).slice(2)}`;
function median(xs) { xs = xs.filter(isNum).sort((a, b) => a - b); if (!xs.length) return null; const m = xs.length >> 1; return xs.length % 2 ? xs[m] : (xs[m - 1] + xs[m]) / 2; }
function quantile(xs, q) { xs = xs.filter(isNum).sort((a, b) => a - b); if (!xs.length) return null; const i = (xs.length - 1) * q, lo = Math.floor(i); return xs[lo] + (xs[Math.min(lo + 1, xs.length - 1)] - xs[lo]) * (i - lo); }
function rolling(pts, n) {  // rolling median over the previous n points with a value
  const out = [], buf = [];
  for (const p of pts) { if (!isNum(p.y)) continue; buf.push(p.y); if (buf.length > n) buf.shift(); if (buf.length >= Math.min(n, 3)) out.push({ x: p.x, y: median(buf) }); }
  return out;
}
const api = async p => { const r = await fetch("/api/" + p); const j = await r.json(); if (!r.ok) throw new Error(j.error || r.status); return j; };

// HR zones as % of HRmax; bounds are lower edges of Z2..Z5
const ZONES = [{ n: "Z1", lo: 0, c: "#4a90d9" }, { n: "Z2", lo: .75, c: "#3ecf8e" }, { n: "Z3", lo: .84, c: "#f5c542" },
  { n: "Z4", lo: .90, c: "#ff8a3d" }, { n: "Z5", lo: .95, c: "#ff4d4d" }];
function zoneSecs(hist, hrmax) {
  const z = ZONES.map(() => 0);
  if (!hist || !hrmax) return z;
  for (const [bpm, s] of Object.entries(hist)) {
    const r = +bpm / hrmax; let k = 0;
    for (let i = 0; i < ZONES.length; i++) if (r >= ZONES[i].lo) k = i;
    z[k] += s;
  }
  return z;
}
const zoneLabel = (i, hrmax) => {
  const lo = Math.round(ZONES[i].lo * hrmax), hi = i + 1 < ZONES.length ? Math.round(ZONES[i + 1].lo * hrmax) - 1 : null;
  return i === 0 ? `${ZONES[i].n} <${hi + 1}` : hi ? `${ZONES[i].n} ${lo}–${hi}` : `${ZONES[i].n} ${lo}+`;
};

// ------------------------------------------------------------------ tooltip

const tip = $("#tip");
function showTip(html, ev) {
  tip.innerHTML = html; tip.hidden = false;
  const w = tip.offsetWidth, h = tip.offsetHeight;
  let x = ev.clientX + 14, y = ev.clientY + 14;
  if (x + w > innerWidth - 8) x = ev.clientX - w - 14;
  if (y + h > innerHeight - 8) y = ev.clientY - h - 14;
  tip.style.left = x + "px"; tip.style.top = y + "px";
}
const hideTip = () => { tip.hidden = true; };

// ------------------------------------------------------------------ charts (SVG)

const NS = "http://www.w3.org/2000/svg";
function el(tag, attrs = {}, parent) {
  const e = document.createElementNS(NS, tag);
  for (const k in attrs) if (attrs[k] != null) e.setAttribute(k, attrs[k]);
  if (parent) parent.appendChild(e);
  return e;
}
function niceTicks(lo, hi, n = 5) {
  if (!isFinite(lo) || !isFinite(hi)) { lo = 0; hi = 1; }
  if (lo === hi) { lo -= 1; hi += 1; }
  const raw = (hi - lo) / n, mag = 10 ** Math.floor(Math.log10(raw)), e = raw / mag;
  const step = (e >= 7.5 ? 10 : e >= 3.5 ? 5 : e >= 1.5 ? 2 : 1) * mag;
  const a = Math.floor(lo / step) * step, b = Math.ceil(hi / step) * step, t = [];
  for (let v = a; v <= b + step / 2; v += step) t.push(+v.toFixed(10));
  return { lo: a, hi: b, ticks: t };
}
function timeTicks(lo, hi) {
  const span = (hi - lo) / DAY, t = [];
  if (span <= 75) {
    const d = new Date(lo); d.setHours(0, 0, 0, 0);
    const step = span <= 14 ? 2 : 7;
    for (; +d <= hi; d.setDate(d.getDate() + step)) if (+d >= lo) t.push({ v: +d, l: `${d.getDate()} ${MON[d.getMonth()]}` });
    return t;
  }
  const months = span / 30.4, step = [1, 2, 3, 6, 12, 24, 36].find(s => months / s <= 9) || 48;
  const d = new Date(lo); d.setDate(1); d.setHours(0, 0, 0, 0);
  while (d.getMonth() % Math.min(step, 12)) d.setMonth(d.getMonth() + 1);
  for (; +d <= hi; d.setMonth(d.getMonth() + step)) if (+d >= lo) t.push({ v: +d, l: step >= 12 ? String(d.getFullYear()) : fmon(d) });
  return t;
}

/**
 * chart(host, opt)
 *  opt.x     {type:'time'|'linear', fmt, label, min, max}
 *  opt.y     {fmt, label, min, max, zero}     opt.y2 same, optional
 *  opt.series[] {name, color, kind:'line'|'dot'|'bar'|'area', data:[{x,y,ref}], axis:'y'|'y2', stack, width, r, opacity, legend:false}
 *  opt.tip(x, hits) -> html   opt.onClick(ref)   opt.onHover(x|null)
 */
function chart(host, opt) {
  host.innerHTML = "";
  const H = opt.height || 260, W = Math.max(320, host.clientWidth || 600);
  const m = { l: 46, r: opt.y2 || opt.padRight ? 46 : 14, t: 18, b: 24 };
  const iw = W - m.l - m.r, ih = H - m.t - m.b;
  const ser = opt.series.filter(s => s.data && s.data.length);
  if (opt.legend !== false) {
    const lg = document.createElement("div"); lg.className = "legend";
    // swatch shape follows the series kind, so a dot series and its trend line never look alike
    const sw = s => s.kind === "dot" ? `<i class="sw-dot" style="background:${s.color}"></i>`
      : s.kind === "line" ? `<i class="sw-line" style="border-top:${Math.max(2, s.width || 2)}px ${s.dash ? "dashed" : "solid"} ${s.color}"></i>`
      : `<i style="background:${s.color}"></i>`;
    lg.innerHTML = opt.series.filter(s => s.legend !== false).map(s => `<span>${sw(s)}${esc(s.name)}</span>`).join("");
    host.appendChild(lg);
  }
  const svg = el("svg", { class: "chart", viewBox: `0 0 ${W} ${H}`, height: H });
  host.appendChild(svg);
  if (!ser.length) { el("text", { x: W / 2, y: H / 2, "text-anchor": "middle", fill: "#8a94a3" }, svg).textContent = "no data"; return; }

  // stacking
  const stacks = {};
  for (const s of ser) if (s.stack) {
    const base = stacks[s.stack] || (stacks[s.stack] = new Map());
    s._d = s.data.map(p => { const b = base.get(p.x) || 0; base.set(p.x, b + (p.y || 0)); return { ...p, y0: b, y1: b + (p.y || 0) }; });
  } else s._d = s.data.map(p => ({ ...p, y0: 0, y1: p.y }));

  const xs = ser.flatMap(s => s._d.map(p => p.x));
  let xlo = opt.x.min ?? Math.min(...xs), xhi = opt.x.max ?? Math.max(...xs);
  const hasBar = ser.some(s => s.kind === "bar");
  let band = 0;
  if (hasBar) {
    const ux = [...new Set(ser.filter(s => s.kind === "bar").flatMap(s => s._d.map(p => p.x)))].sort((a, b) => a - b);
    let dmin = Infinity; for (let i = 1; i < ux.length; i++) dmin = Math.min(dmin, ux[i] - ux[i - 1]);
    if (!isFinite(dmin)) dmin = opt.x.type === "time" ? 30 * DAY : 1;
    xlo -= dmin / 2; xhi += dmin / 2; band = dmin;
  }
  if (xlo === xhi) { xlo -= 1; xhi += 1; }
  const X = v => m.l + (v - xlo) / (xhi - xlo) * iw;
  const bandPx = band ? Math.max(1, band / (xhi - xlo) * iw * 0.78) : 0;

  function yscale(axis, cfg) {
    const vals = ser.filter(s => (s.axis || "y") === axis).flatMap(s => s._d.flatMap(p => [p.y1, s.stack || s.kind === "bar" || s.kind === "area" ? p.y0 : null])).filter(isNum);
    if (!vals.length) return null;
    let lo = cfg.min ?? Math.min(...vals), hi = cfg.max ?? Math.max(...vals);
    if (cfg.zero) lo = Math.min(0, lo);
    if (cfg.min == null && !cfg.zero) { const pad = (hi - lo) * 0.06 || 1; lo -= pad; hi += pad; }
    const t = niceTicks(lo, hi, cfg.ticks || 5);
    const L = cfg.min ?? t.lo, Hh = cfg.max ?? t.hi;
    return { lo: L, f: v => m.t + ih - (Math.max(L, Math.min(Hh, v)) - L) / (Hh - L) * ih, ticks: t.ticks.filter(v => v >= L - 1e-9 && v <= Hh + 1e-9), cfg };
  }
  const Y = { y: yscale("y", opt.y), y2: opt.y2 ? yscale("y2", opt.y2) : null };

  // grid + axes
  const g = el("g", { class: "grid" }, svg), ax = el("g", { class: "axis" }, svg);
  // fall back to more decimals when the formatter would print duplicate labels
  const labels = (ticks, fmt) => {
    const l = ticks.map(fmt || String);
    if (new Set(l).size === l.length) return l;
    const step = ticks.length > 1 ? Math.abs(ticks[1] - ticks[0]) : 1;
    return ticks.map(v => v.toFixed(Math.max(0, -Math.floor(Math.log10(step)))));
  };
  if (Y.y) labels(Y.y.ticks, opt.y.fmt).forEach((l, i) => {
    const v = Y.y.ticks[i];
    el("line", { x1: m.l, x2: m.l + iw, y1: Y.y.f(v), y2: Y.y.f(v) }, g);
    el("text", { x: m.l - 6, y: Y.y.f(v) + 4, "text-anchor": "end" }, ax).textContent = l;
  });
  if (Y.y2) labels(Y.y2.ticks, opt.y2.fmt).forEach((l, i) => { el("text", { x: m.l + iw + 6, y: Y.y2.f(Y.y2.ticks[i]) + 4 }, ax).textContent = l; });
  const xt = opt.x.type === "time" ? timeTicks(xlo, xhi) : niceTicks(xlo, xhi, Math.max(3, Math.floor(iw / 80))).ticks.filter(v => v >= xlo && v <= xhi).map(v => ({ v, l: (opt.x.fmt || String)(v) }));
  for (const t of xt) {
    el("line", { x1: X(t.v), x2: X(t.v), y1: m.t, y2: m.t + ih, opacity: .45 }, g);
    el("text", { x: X(t.v), y: H - 6, "text-anchor": "middle" }, ax).textContent = t.l;
  }
  if (opt.y.label) el("text", { x: 4, y: 10 }, ax).textContent = opt.y.label;
  if (opt.y2 && opt.y2.label) el("text", { x: W - 4, y: 10, "text-anchor": "end" }, ax).textContent = opt.y2.label;
  if (opt.refY != null && Y.y) el("line", { x1: m.l, x2: m.l + iw, y1: Y.y.f(opt.refY), y2: Y.y.f(opt.refY), stroke: "#8a94a3" }, svg);

  // selection band, behind the series
  const bandEl = el("rect", { y: m.t, height: ih, fill: "#fff", opacity: .13, visibility: "hidden" }, svg);
  const bandL = el("line", { y1: m.t, y2: m.t + ih, stroke: "#fff", opacity: .5, visibility: "hidden" }, svg);
  const bandR = el("line", { y1: m.t, y2: m.t + ih, stroke: "#fff", opacity: .5, visibility: "hidden" }, svg);
  function setBand(x0, x1) {
    const on = x0 != null;
    for (const e of [bandEl, bandL, bandR]) e.setAttribute("visibility", on ? "visible" : "hidden");
    if (!on) return;
    const a = Math.max(m.l, X(Math.min(x0, x1))), b = Math.min(m.l + iw, X(Math.max(x0, x1)));
    bandEl.setAttribute("x", a); bandEl.setAttribute("width", Math.max(1, b - a));
    bandL.setAttribute("x1", a); bandL.setAttribute("x2", a); bandR.setAttribute("x1", b); bandR.setAttribute("x2", b);
  }

  // series
  for (const s of ser) {
    const sc = Y[s.axis || "y"]; if (!sc) continue;
    const pts = s._d.filter(p => isNum(p.y1));
    if (s.kind === "bar") {
      const gg = el("g", { fill: s.color, opacity: s.opacity ?? 1 }, svg);
      for (const p of pts) {
        const y1 = sc.f(p.y1), y0 = sc.f(p.y0);
        el("rect", { x: X(p.x) - bandPx / 2, width: bandPx, y: Math.min(y0, y1), height: Math.max(0.5, Math.abs(y0 - y1)), fill: p.color || null, rx: 1 }, gg);
      }
    } else if (s.kind === "dot") {
      const gg = el("g", { fill: s.color, opacity: s.opacity ?? .85 }, svg);
      for (const p of pts) el("circle", { cx: X(p.x), cy: sc.f(p.y1), r: s.r || 3, fill: p.color || null }, gg);
    } else {
      // lines break where y is null or the x gap is large (gapX)
      let d = "", prev = null;
      for (const p of s._d) {
        if (!isNum(p.y1)) { prev = null; continue; }
        const brk = !prev || (s.gapX && p.x - prev.x > s.gapX);
        d += (brk ? "M" : "L") + X(p.x).toFixed(1) + "," + sc.f(p.y1).toFixed(1);
        prev = p;
      }
      if (s.kind === "area") {
        const base = sc.f(Math.max(sc.cfg.min ?? -Infinity, Math.min(...pts.map(p => p.y1))));
        let a = "";
        const segs = d.split("M").filter(Boolean);
        for (const sg of segs) {
          const c = sg.split("L"), first = c[0].split(","), last = c[c.length - 1].split(",");
          a += `M${first[0]},${base}L${sg}L${last[0]},${base}Z`;
        }
        el("path", { d: a, fill: s.color, opacity: s.opacity ?? .18, stroke: "none" }, svg);
      } else {
        el("path", { d, fill: "none", stroke: s.color, "stroke-width": s.width || 2, opacity: s.opacity ?? 1, "stroke-dasharray": s.dash || null, "stroke-linejoin": "round" }, svg);
      }
    }
  }

  // interaction
  const xh = el("line", { class: "xhair", y1: m.t, y2: m.t + ih, visibility: "hidden" }, svg);
  const hot = el("circle", { r: 5, fill: "none", stroke: "#fff", "stroke-width": 1.5, visibility: "hidden" }, svg);
  const ov = el("rect", { x: m.l, y: m.t, width: iw, height: ih, fill: "transparent", style: "cursor:crosshair" }, svg);
  const sorted = ser.map(s => ({ s, d: s._d.filter(p => isNum(p.y1)).slice().sort((a, b) => a.x - b.x) }));
  function nearest(arr, x) { let lo = 0, hi = arr.length - 1; if (hi < 0) return null; while (hi - lo > 1) { const mid = (lo + hi) >> 1; if (arr[mid].x < x) lo = mid; else hi = mid; } return Math.abs(arr[lo].x - x) <= Math.abs(arr[hi].x - x) ? arr[lo] : arr[hi]; }
  let cur = null;
  function pick(ev) {
    const r = svg.getBoundingClientRect(), px = (ev.clientX - r.left) * W / r.width, py = (ev.clientY - r.top) * H / r.height;
    const x = xlo + (px - m.l) / iw * (xhi - xlo);
    // prefer the closest point in 2D among dot series, else nearest x
    let best = null, bd = Infinity;
    for (const { s, d } of sorted) {
      const sc = Y[s.axis || "y"]; if (!sc) continue;
      const p = nearest(d, x); if (!p) continue;
      const dd = Math.hypot(X(p.x) - px, (s.kind === "dot" ? sc.f(p.y1) - py : 0) * 1);
      if (dd < bd) { bd = dd; best = { s, p }; }
    }
    if (!best) return null;
    const hx = best.p.x;
    const hits = sorted.map(({ s, d }) => { const p = nearest(d, hx); return p && Math.abs(p.x - hx) <= (band || (xhi - xlo) / iw * 6) ? { s, p } : null; }).filter(Boolean);
    return { x: hx, best, hits };
  }
  ov.addEventListener("mousemove", ev => {
    const h = pick(ev); if (!h) return;
    cur = h;
    xh.setAttribute("x1", X(h.x)); xh.setAttribute("x2", X(h.x)); xh.setAttribute("visibility", "visible");
    const sc = Y[h.best.s.axis || "y"];
    hot.setAttribute("cx", X(h.best.p.x)); hot.setAttribute("cy", sc.f(h.best.p.y1)); hot.setAttribute("visibility", "visible");
    const html = opt.tip ? opt.tip(h.x, h.hits, h.best) : h.hits.map(({ s, p }) => `<div class="row"><span>${esc(s.name)}</span><span>${f2(p.y)}</span></div>`).join("");
    if (html) showTip(html, ev);
    opt.onHover && opt.onHover(h.x);
  });
  ov.addEventListener("mouseleave", () => { xh.setAttribute("visibility", "hidden"); hot.setAttribute("visibility", "hidden"); hideTip(); cur = null; opt.onHover && opt.onHover(null); });
  // drag across the plot to select an x range (opt.onBrush); a plain click stays a click
  let brush = null, suppressClick = false;
  const xAt = ev => { const r = svg.getBoundingClientRect(); return xlo + (((ev.clientX - r.left) * W / r.width) - m.l) / iw * (xhi - xlo); };
  if (opt.onBrush) {
    ov.addEventListener("pointerdown", ev => { if (ev.button === 0) { brush = { x0: xAt(ev), px: ev.clientX }; ov.setPointerCapture(ev.pointerId); } });
    ov.addEventListener("pointermove", ev => { if (brush && Math.abs(ev.clientX - brush.px) > 5) { brush.moved = true; setBand(brush.x0, xAt(ev)); hideTip(); } });
    ov.addEventListener("pointerup", ev => {
      if (!brush) return;
      const b = brush; brush = null;
      if (b.moved) { suppressClick = true; const x1 = Math.max(xlo, Math.min(xhi, xAt(ev))), x0 = Math.max(xlo, Math.min(xhi, b.x0)); opt.onBrush(Math.min(x0, x1), Math.max(x0, x1)); }
    });
  }
  if (opt.onClick) ov.addEventListener("click", () => { if (suppressClick) { suppressClick = false; return; } if (cur && cur.best.p.ref != null) { hideTip(); opt.onClick(cur.best.p.ref); } });
  /** Show the crosshair at x from outside (synced charts), no tooltip; null hides it. */
  function cursor(x) {
    if (x == null || x < xlo || x > xhi) { xh.setAttribute("visibility", "hidden"); hot.setAttribute("visibility", "hidden"); return; }
    xh.setAttribute("x1", X(x)); xh.setAttribute("x2", X(x)); xh.setAttribute("visibility", "visible");
    const main = sorted.find(({ s }) => s.kind === "line" && s.data.length > 2) || sorted[0];
    const p = main && nearest(main.d, x), sc = main && Y[main.s.axis || "y"];
    if (p && sc) { hot.setAttribute("cx", X(p.x)); hot.setAttribute("cy", sc.f(p.y1)); hot.setAttribute("visibility", "visible"); }
  }
  return { X, svg, setBand, cursor };
}

// ------------------------------------------------------------------ session map (tiles + speed-coloured track)

const SPEED_COLORS = ["#3b6fd9", "#3fb6ff", "#3ecf8e", "#c6e04a", "#f5c542", "#ff8a3d", "#ff4d4d"];
function sessionMap(host, series, opt = {}) {
  const pts = [];
  series.forEach((r, i) => { if (r[5] != null) { const m = merc(r[5], r[6]); pts.push({ i, x: m[0], y: m[1], v: r[2] }); } });
  if (pts.length < 2) { host.innerHTML = `<p class="muted">No GPS track.</p>`; return null; }
  const moving = pts.map(p => p.v).filter(v => v > 3);  // stops would drag the scale to 0
  const lo = quantile(moving, .05) ?? 0, hi = quantile(moving, .95) ?? 1;
  const bucket = v => Math.max(0, Math.min(SPEED_COLORS.length - 1, Math.floor((v - lo) / ((hi - lo) || 1) * SPEED_COLORS.length)));
  const wrap = document.createElement("div"); wrap.className = "map";
  host.innerHTML = ""; host.appendChild(wrap);
  const map = new SlippyMap(wrap);
  let turnJ = null;
  if (opt.turnKm != null) { turnJ = 0; pts.forEach((p, j) => { if (series[p.i][1] <= opt.turnKm) turnJ = j; }); }
  let hover = null, range = null;
  const gate = opt.gate ? merc(opt.gate[0], opt.gate[1]) : null;
  map.layers.push({
    draw(ctx, mp) {
      const S = pts.map(p => mp.toScreen(p.x, p.y));
      ctx.lineJoin = ctx.lineCap = "round";
      ctx.beginPath(); S.forEach(([x, y], j) => j ? ctx.lineTo(x, y) : ctx.moveTo(x, y));
      ctx.strokeStyle = "rgba(0,0,0,.65)"; ctx.lineWidth = 7; ctx.stroke();
      ctx.lineWidth = 4;
      let j0 = 0, b = bucket(pts[0].v);
      const flush = j1 => { ctx.beginPath(); for (let j = j0; j <= j1; j++) j > j0 ? ctx.lineTo(...S[j]) : ctx.moveTo(...S[j]); ctx.strokeStyle = SPEED_COLORS[b]; ctx.stroke(); };
      for (let j = 1; j < pts.length; j++) { const nb = bucket(pts[j].v); if (nb !== b) { flush(j); j0 = j; b = nb; } }
      flush(pts.length - 1);
      const dot = ([x, y], c, r) => { ctx.beginPath(); ctx.arc(x, y, r, 0, 7); ctx.fillStyle = c; ctx.fill(); ctx.lineWidth = 2; ctx.strokeStyle = "#000"; ctx.stroke(); };
      dot(S[0], "#3ecf8e", 7); dot(S[S.length - 1], "#ff4d4d", 6);
      if (turnJ != null) dot(S[turnJ], "#fff", 6);
      if (range) {
        ctx.beginPath(); let pen = false;
        pts.forEach((p, j) => { const v = series[p.i][range.col]; if (v < range.lo || v > range.hi) { pen = false; return; } pen ? ctx.lineTo(...S[j]) : ctx.moveTo(...S[j]); pen = true; });
        ctx.strokeStyle = "#000"; ctx.lineWidth = 9; ctx.stroke(); ctx.strokeStyle = "#fff"; ctx.lineWidth = 5; ctx.stroke();
      }
      if (gate) {
        const [x, y] = mp.toScreen(gate[0], gate[1]);
        ctx.beginPath(); ctx.moveTo(x, y - 9); ctx.lineTo(x + 9, y); ctx.lineTo(x, y + 9); ctx.lineTo(x - 9, y); ctx.closePath();
        ctx.fillStyle = "#ff7a1a"; ctx.fill(); ctx.lineWidth = 2; ctx.strokeStyle = "#000"; ctx.stroke();
      }
      if (hover != null) { ctx.beginPath(); ctx.arc(...S[hover], 9, 0, 7); ctx.lineWidth = 3; ctx.strokeStyle = "#fff"; ctx.stroke(); }
    }
  });
  const bb = [Math.min(...pts.map(p => p.x)), Math.min(...pts.map(p => p.y)), Math.max(...pts.map(p => p.x)), Math.max(...pts.map(p => p.y))];
  map.fit(bb);
  // hover on the map: nearest track point within 14 px
  map.on("hover", (pos, ev) => {
    if (!pos) { hover = null; hideTip(); map.redraw(); return; }
    let bj = null, bd = 196;
    pts.forEach((p, j) => { const [x, y] = map.toScreen(p.x, p.y); const d = (x - pos.x) ** 2 + (y - pos.y) ** 2; if (d < bd) { bd = d; bj = j; } });
    hover = bj; map.redraw();
    if (bj == null) { hideTip(); return; }
    const r = series[pts[bj].i];
    showTip(`<b>${f2(r[1])} km · ${dur(r[0], true)}</b><div class="row"><span>speed</span><span>${f1(r[2])} km/h · ${pace(r[2])}/km</span></div>` + (r[3] ? `<div class="row"><span>HR</span><span>${r[3]} bpm</span></div>` : ""), ev);
  });
  const leg = document.createElement("div"); leg.className = "legend"; leg.style.marginTop = "6px";
  leg.innerHTML = `<span>speed</span>` + SPEED_COLORS.map((c, i) => `<span><i style="background:${c}"></i>${f1(lo + (hi - lo) * i / SPEED_COLORS.length)}</span>`).join("") +
    `<span>km/h</span><span><i style="background:#3ecf8e;border-radius:50%"></i>start</span><span><i style="background:#ff4d4d;border-radius:50%"></i>end</span>` +
    (turnJ != null ? `<span><i style="background:#fff;border-radius:50%"></i>turnaround</span>` : "") +
    (gate ? `<span><i style="background:#ff7a1a;transform:rotate(45deg)"></i>lap gate</span>` : "");
  host.appendChild(leg);
  return {
    highlight(t0, t1, fit) { this.highlightBy(0, t0, t1, fit); },
    /** Highlight the part of the track where series[col] is in [lo, hi]; col 0 = time, 1 = km. */
    highlightBy(col, lo, hi, fit) {
      range = lo == null ? null : { col, lo, hi };
      if (range) {
        const sub = pts.filter(p => { const v = series[p.i][col]; return v >= lo && v <= hi; });
        if (sub.length > 1) {
          const bb = [Math.min(...sub.map(p => p.x)), Math.min(...sub.map(p => p.y)), Math.max(...sub.map(p => p.x)), Math.max(...sub.map(p => p.y))];
          const v = map.view(-20);
          if (fit) map.fit(bb, 50);
          else if (bb[0] < v[0] || bb[1] < v[1] || bb[2] > v[2] || bb[3] > v[3]) {  // off-screen: pan, keep zoom
            map.cx = (bb[0] + bb[2]) / 2; map.cy = (bb[1] + bb[3]) / 2;
          }
        }
      }
      map.redraw();
    },
    mark(seriesIdx) {
      if (seriesIdx == null) hover = null;
      else { let j = 0; for (let k = 0; k < pts.length; k++) { if (pts[k].i <= seriesIdx) j = k; else break; } hover = j; }
      map.redraw();
    }
  };
}

// ------------------------------------------------------------------ state

const S = { segs: {}, tracks: {}, mapView: {}, types: [], type: localStorage.type || "Inline Skate", range: localStorage.range || "all", acts: {}, hrmax: null, sort: { k: "date", dir: -1 }, q: "" };
async function loadActs(type) {
  if (!S.acts[type]) S.acts[type] = (await api("activities?type=" + encodeURIComponent(type))).map(a => ({ ...a, _d: dateOf(a), _t: +dateOf(a) }));
  return S.acts[type];
}
function inRange(acts) {
  if (S.range === "all") return acts;
  const cut = Date.now() - (+S.range) * DAY;
  return acts.filter(a => a._t >= cut);
}
const isSkate = () => /skate/i.test(S.type);

// ------------------------------------------------------------------ views

function cards(items) {
  return `<div class="cards">` + items.map(c => `<div class="card ${c.hl ? "hl" : ""}"><div class="k">${esc(c.k)}</div><div class="v">${c.v}</div>${c.s ? `<div class="s">${c.s}</div>` : ""}</div>`).join("") + `</div>`;
}
const go = id => { location.hash = "#/session/" + id; };
const routePill = r => r ? `<span class="pill ${r === "out-back" ? "ob" : r === "loop" ? "loop" : r === "one-way" ? "ow" : ""}">${esc(r)}</span>` : "";

async function viewProgress() {
  const all = await loadActs(S.type), acts = inRange(all);
  const app = $("#app");
  if (!acts.length) { app.innerHTML = `<p class="muted">No ${esc(S.type)} sessions in this range.</p>`; return; }
  const km = acts.reduce((s, a) => s + (a.km || 0), 0), mv = acts.reduce((s, a) => s + (a.moving_s || 0), 0);
  const best = (k, xs = acts) => xs.filter(a => isNum(a[k])).reduce((b, a) => !b || a[k] > b[k] ? a : b, null);
  const b10 = best("best10min_kmh"), wn = best("wind_neutral"), lg = best("km");
  const last30 = all.filter(a => a._t >= Date.now() - 30 * DAY), prev30 = all.filter(a => a._t >= Date.now() - 60 * DAY && a._t < Date.now() - 30 * DAY);
  const k30 = last30.reduce((s, a) => s + a.km, 0), kp30 = prev30.reduce((s, a) => s + a.km, 0);
  const wnRecent = median(last30.map(a => a.wind_neutral)), wnPrev = median(all.filter(a => a._t < Date.now() - 30 * DAY).slice(-40).map(a => a.wind_neutral));
  const link = a => a ? `<a href="#/session/${a.id}">${fdate(a._d)}</a>` : "";
  app.innerHTML = `
    <div class="head"><div><h1>${esc(S.type)} progress</h1><div class="muted">${acts.length} sessions · ${fdate(acts[0]._d)} → ${fdate(acts[acts.length - 1]._d)}</div></div></div>
    ${cards([
      { k: "Sessions", v: acts.length, s: `${(acts.length / Math.max(1, (acts[acts.length - 1]._t - acts[0]._t) / (7 * DAY))).toFixed(1)} / week` },
      { k: "Distance", v: fkm(km) + " km", s: `${f1(km / acts.length)} km / session` },
      { k: "Moving time", v: hours(mv) },
      { k: "Longest", v: fkm(lg?.km) + " km", s: link(lg) },
      { k: "Best 10-min", v: f2(b10?.best10min_kmh), s: `km/h · ${link(b10)}`, hl: 1 },
      { k: "Best wind-neutral", v: f2(wn?.wind_neutral), s: wn ? `km/h · ${link(wn)}` : "out/back routes only", hl: 1 },
      { k: "Last 30 days", v: fkm(k30) + " km", s: `${last30.length} sessions · prev 30d ${fkm(kp30)} km` },
      { k: "Wind-neutral, 30d median", v: f2(wnRecent), s: isNum(wnRecent) && isNum(wnPrev) ? `<span class="${wnRecent >= wnPrev ? "up" : "down"}">${pct((wnRecent / wnPrev - 1) * 100)}</span> vs previous 40` : "" },
    ])}
    <div class="panel"><h2>Calendar <span class="muted">km per day · click a day to open it</span></h2><div id="cal"></div></div>
    <div class="grid g2">
      <div class="panel"><h2>Form: best 10-min speed <span class="muted">click a dot to open the session</span></h2><div id="c-form"></div>
        <div class="note">Wind-neutral = mean of the best 10 min on the out leg and on the back leg, so tail/head wind cancels. Only for out-and-back routes. Lines are rolling medians of 8 sessions.</div></div>
      <div class="panel"><h2>Volume per month</h2><div id="c-vol"></div></div>
      <div class="panel"><h2>Endurance: best sustained speed <span class="muted">30 / 60 min windows</span></h2><div id="c-endu"></div></div>
      <div class="panel"><h2>Average moving speed per session</h2><div id="c-avg"></div>
        <div class="note">Not comparable across 2025-08: the watch sample rate dropped then and inflates moving speed. Use best-10 for long-term form.</div></div>
      <div class="panel"><h2>Efficiency: best 10-min ÷ avg HR <span class="muted">km/h per 100 bpm</span></h2><div id="c-eff"></div></div>
      <div class="panel"><h2>Wind: out-leg vs back-leg speed <span class="muted">out/back routes</span></h2><div id="c-wind"></div>
        <div class="note">Positive = out leg faster. Calm days on your home route sit around 0–5% (gentle −0.15% grade).</div></div>
      <div class="panel"><h2>Time in HR zones per month <span class="muted">HRmax ${S.hrmax ?? "?"} bpm</span></h2><div id="c-zones"></div></div>
      <div class="panel" id="p-pred"></div>
    </div>
    <div class="panel" id="p-favs" hidden></div>
    <div class="panel" id="p-circ" hidden></div>
    <div class="grid g2">
      <div class="panel"><h2>Personal records</h2><div id="t-rec"></div></div>
      <div class="panel"><h2>Monthly summary</h2><div class="scroll" id="t-mon"></div></div>
    </div>`;

  const P = (k, xs = acts, color) => xs.filter(a => isNum(a[k])).map(a => ({ x: a._t, y: a[k], ref: a.id, color: color && color(a) }));
  const tipAct = (x, hits, best) => {
    const a = acts.find(a => a.id === best.p.ref) || acts.find(a => a._t === x);
    if (!a) return "";
    return `<b>${fdate(a._d)}</b> · ${fkm(a.km)} km ${routePill(a.route)}<br>` + hits.filter(h => h.p.ref === a.id || h.p.ref == null).map(({ s, p }) => `<div class="row"><span>${esc(s.name)}</span><span>${f2(p.y)}</span></div>`).join("");
  };
  chart($("#c-form"), {
    x: { type: "time" }, y: { fmt: v => v.toFixed(0), label: "km/h" }, tip: tipAct, onClick: go,
    series: [
      { name: "raw best 10-min", kind: "dot", color: "#8a94a3", r: 2.5, opacity: .6, data: P("best10min_kmh", acts.filter(a => isNum(a.best10min_kmh) && a.best10min_kmh > 0)) },
      { name: "wind-neutral", kind: "dot", color: "#ff7a1a", r: 3, data: P("wind_neutral") },
      { name: "raw, rolling", kind: "line", color: "#c9d1dc", width: 1.5, opacity: .7, data: rolling(P("best10min_kmh"), 8), gapX: 45 * DAY },
      { name: "wind-neutral, rolling", kind: "line", color: "#ffd166", width: 2.5, data: rolling(P("wind_neutral"), 8), gapX: 45 * DAY },
    ]
  });

  // monthly aggregation
  const months = new Map();
  for (const a of acts) {
    const k = a.date.slice(0, 7);
    if (!months.has(k)) months.set(k, { k, x: +new Date(k + "-15T12:00:00"), a: [] });
    months.get(k).a.push(a);
  }
  const mons = [...months.values()].sort((a, b) => a.x - b.x);
  for (const m of mons) {
    m.n = m.a.length; m.km = m.a.reduce((s, a) => s + (a.km || 0), 0); m.mv = m.a.reduce((s, a) => s + (a.moving_s || 0), 0);
    m.b10 = Math.max(...m.a.map(a => a.best10min_kmh || 0)) || null; m.b10med = median(m.a.map(a => a.best10min_kmh));
    m.wn = median(m.a.map(a => a.wind_neutral)); m.kmh = m.mv ? m.km / (m.mv / 3600) : null;
    m.hr = median(m.a.map(a => a.hr_avg)); m.zones = ZONES.map(() => 0);
    for (const a of m.a) zoneSecs(a.hr_hist, S.hrmax).forEach((s, i) => m.zones[i] += s);
  }
  chart($("#c-vol"), {
    x: { type: "time" }, y: { zero: true, fmt: v => v.toFixed(0), label: "km" }, y2: { zero: true, fmt: v => v.toFixed(0), label: "sessions" },
    series: [{ name: "km", kind: "bar", color: "#3fb6ff", data: mons.map(m => ({ x: m.x, y: m.km })) },
      { name: "sessions", kind: "line", color: "#ff7a1a", axis: "y2", data: mons.map(m => ({ x: m.x, y: m.n })) }],
    tip: x => { const m = mons.find(m => m.x === x); return m ? `<b>${fmon(new Date(m.x))}</b><div class="row"><span>distance</span><span>${fkm(m.km)} km</span></div><div class="row"><span>sessions</span><span>${m.n}</span></div><div class="row"><span>moving</span><span>${hours(m.mv)}</span></div>` : ""; }
  });
  chart($("#c-endu"), {
    x: { type: "time" }, y: { fmt: v => v.toFixed(0), label: "km/h" }, tip: tipAct, onClick: go,
    series: [{ name: "best 30 min", kind: "dot", color: "#3fb6ff", data: P("b30", acts.filter(a => a.b30 > 5)) },
      { name: "best 60 min", kind: "dot", color: "#ff7a1a", r: 3.5, data: P("b60", acts.filter(a => a.b60 > 5)) },
      { name: "60 min, rolling", kind: "line", color: "#ffd166", width: 2.5, data: rolling(P("b60", acts.filter(a => a.b60 > 5)), 6), gapX: 60 * DAY }]
  });
  chart($("#c-avg"), {
    x: { type: "time" }, y: { fmt: v => v.toFixed(0), label: "km/h" }, tip: tipAct, onClick: go,
    series: [{ name: "moving avg", kind: "dot", color: "#3ecf8e", r: 2.5, data: P("kmh_moving", acts.filter(a => a.kmh_moving > 0)) },
      { name: "moving avg, rolling", kind: "line", color: "#b8f5d6", width: 2.5, data: rolling(P("kmh_moving"), 8), gapX: 45 * DAY },
      { name: "elapsed avg, rolling", kind: "line", color: "#8a94a3", width: 1.5, dash: "4 3", data: rolling(P("kmh_elapsed"), 8), gapX: 45 * DAY }]
  });
  const withHr = acts.filter(a => a.hr_avg > 60 && a.best10min_kmh > 0);
  chart($("#c-eff"), {
    x: { type: "time" }, y: { fmt: v => v.toFixed(0) }, y2: { fmt: v => v.toFixed(0), label: "bpm" }, tip: tipAct, onClick: go,
    series: [{ name: "efficiency", kind: "dot", color: "#ff7a1a", r: 3.5, data: withHr.map(a => ({ x: a._t, y: a.best10min_kmh / a.hr_avg * 100, ref: a.id })) },
      { name: "avg HR", kind: "dot", color: "#ff4d4d", axis: "y2", r: 2, opacity: .45, data: withHr.map(a => ({ x: a._t, y: a.hr_avg, ref: a.id })) }]
  });
  const ob = acts.filter(a => isNum(a.asym_pct));
  chart($("#c-wind"), {
    x: { type: "time" }, y: { fmt: v => v.toFixed(0) + "%" }, refY: 0, tip: tipAct, onClick: go,
    series: [{ name: "out vs back", kind: "bar", color: "#3fb6ff", data: ob.map(a => ({ x: a._t, y: a.asym_pct, ref: a.id, color: Math.abs(a.asym_pct) < 6 ? "#3ecf8e" : Math.abs(a.asym_pct) < 15 ? "#f5c542" : "#ff4d4d" })) }],
    legend: false
  });
  const zm = mons.filter(m => m.zones.some(z => z > 0));
  chart($("#c-zones"), {
    x: { type: "time" }, y: { zero: true, fmt: v => v.toFixed(0), label: "h" },
    series: ZONES.map((z, i) => ({ name: zoneLabel(i, S.hrmax || 0), kind: "bar", stack: "z", color: z.c, data: zm.map(m => ({ x: m.x, y: m.zones[i] / 3600 })) })),
    tip: x => { const m = zm.find(m => m.x === x); if (!m) return ""; const tot = m.zones.reduce((a, b) => a + b, 0); return `<b>${fmon(new Date(m.x))}</b>` + m.zones.map((s, i) => `<div class="row"><span>${zoneLabel(i, S.hrmax)}</span><span>${dur(s, true)} · ${(s / tot * 100).toFixed(0)}%</span></div>`).join(""); }
  });
  if (!zm.length) $("#c-zones").innerHTML = `<p class="muted">No heart-rate data in this range.</p>`;

  // records
  const recs = [["Best 5 min", "best5min_kmh", "km/h"], ["Best 10 min", "best10min_kmh", "km/h"], ["Best 20 min", "best20min_kmh", "km/h"],
    ["Best 30 min", "b30", "km/h"], ["Best 60 min", "b60", "km/h"], ["Best wind-neutral 10 min", "wind_neutral", "km/h"],
    ["Fastest avg (≥10 km)", "kmh_moving", "km/h", acts.filter(a => a.km >= 10)], ["Longest", "km", "km"], ["Longest moving time", "moving_s", ""], ["Most climbing", "ascent_m", "m"]];
  $("#t-rec").innerHTML = `<table><thead><tr><th>record</th><th>value</th><th class="l">date</th><th>km</th><th class="l">route</th></tr></thead><tbody>` +
    recs.map(([n, k, u, xs]) => { const a = best(k, xs); if (!a) return ""; return `<tr class="click" data-id="${a.id}"><td class="l">${n}</td><td>${k === "moving_s" ? dur(a[k], true) : f2(a[k])} ${u}</td><td class="l">${fdate(a._d)}</td><td>${fkm(a.km)}</td><td class="l">${routePill(a.route)}</td></tr>`; }).join("") + `</tbody></table>`;
  $("#t-mon").innerHTML = `<table><thead><tr><th>month</th><th>n</th><th>km</th><th>moving</th><th>km/h</th><th>best10 max</th><th>best10 med</th><th>wind-neutral</th><th>HR</th></tr></thead><tbody>` +
    mons.slice().reverse().map(m => `<tr><td class="l">${fmon(new Date(m.x))}</td><td>${m.n}</td><td>${fkm(m.km)}</td><td>${hours(m.mv)}</td><td>${f1(m.kmh)}</td><td>${f2(m.b10)}</td><td>${f2(m.b10med)}</td><td>${f2(m.wn)}</td><td>${f0(m.hr)}</td></tr>`).join("") + `</tbody></table>`;
  app.querySelectorAll("tr[data-id]").forEach(tr => tr.onclick = () => go(tr.dataset.id));
  predictor($("#p-pred"), all);
  calendar($("#cal"), acts);
  circuits($("#p-circ"), acts);
  favPanel($("#p-favs"), all);
}

function predictor(host, all) {
  const recent = all.filter(a => a._t >= Date.now() - 60 * DAY);
  host.innerHTML = `<h2>Race predictor <span class="muted">heuristic, from the last 60 days</span></h2>
    <div class="toolbar"><label>Distance <input id="pk" type="number" min="1" max="200" step="0.5" value="${localStorage.predKm || 24}" style="width:80px"> km</label></div>
    <div id="pout"></div>`;
  const render = () => {
    const D = +$("#pk").value; localStorage.predKm = D;
    // Strongest sustained effort: prefer 60 min, fall back to 30 min. Riegel exponent 1.06 converts duration.
    const cand = [];
    for (const a of recent) {
      if (a.b60 > 5) cand.push({ a, v: a.b60, T: 60 });
      else if (a.b30 > 5) cand.push({ a, v: a.b30, T: 30 });
    }
    if (!cand.length) { $("#pout").innerHTML = `<p class="muted">No sessions of 30+ min in the last 60 days.</p>`; return; }
    const riegel = (v, T) => { const d0 = v * T / 60; const t1 = T * (D / d0) ** 1.06; return D / (t1 / 60); };
    const base = cand.map(c => ({ ...c, pv: riegel(c.v, c.T) })).reduce((b, c) => c.pv > b.pv ? c : b);
    const hr = base.a.hr_avg;
    const effort = hr && hr < 155 ? 1.04 : 1.0;  // sub-race HR on the reference session → headroom
    const rows = [["Floor: same effort as reference", base.pv], ["Solo, race effort", base.pv * effort], ["Solo, perfect day", base.pv * effort * 1.025],
      ["Pack, drafting (low)", base.pv * effort * 1.08], ["Pack, drafting (high)", base.pv * effort * 1.16]];
    $("#pout").innerHTML = `<table class="pred"><thead><tr><th>scenario</th><th>km/h</th><th>time</th><th>pace</th></tr></thead><tbody>` +
      rows.map(([n, v]) => `<tr><td>${n}</td><td>${f2(v)}</td><td>${dur(D / v * 3600, true)}</td><td>${pace(v)}/km</td></tr>`).join("") + `</tbody></table>
      <div class="note">Reference: best ${base.T}-min ${f2(base.v)} km/h on <a href="#/session/${base.a.id}">${fdate(base.a._d)}</a>${hr ? ` at ${f0(hr)} bpm avg` : ""}${effort > 1 ? " (+4% for race effort since HR was sub-threshold)" : ""}. Duration scaled with Riegel (1.06). Pack factor assumes you can hold the surges.</div>`;
  };
  $("#pk").oninput = render; render();
}

// ------------------------------------------------------------------ circuits (repeated-lap venues)

const CIRC_COLORS = ["#ff7a1a", "#3fb6ff", "#3ecf8e", "#f5c542", "#c77dff", "#ff4d4d"];
function circuits(host, acts) {
  const near = (a, b) => { const dy = (a[0] - b[0]) * 111320, dx = (a[1] - b[1]) * 111320 * Math.cos(a[0] * Math.PI / 180); return Math.hypot(dx, dy); };
  const cs = [];
  for (const a of acts.filter(a => a.laps_n && a.lap_center && a.best_lap_s)) {
    // same circuit: centre within 25% of a lap length (min 60 m) and lap length within 12%
    let c = cs.find(c => near(c.center, a.lap_center) < Math.max(60, 250 * c.km) && Math.abs(a.lap_km / c.km - 1) < 0.12);
    if (!c) cs.push(c = { center: a.lap_center, km: a.lap_km, a: [] });
    c.a.push(a);
  }
  const list = cs.filter(c => c.a.length >= 2 || c.a[0].laps_n >= 5).sort((x, y) => y.a.length - x.a.length).slice(0, CIRC_COLORS.length);
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

// ------------------------------------------------------------------ calendar heatmap

const CAL_COLORS = ["#4a2208", "#8c3a0c", "#d9580f", "#ff8a3d", "#ffc27a"];
const ymd = d => `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
function calendar(host, acts, opt = {}) {
  const byDay = new Map();
  for (const a of acts) { const d = byDay.get(a.date) || { km: 0, n: 0, mv: 0, a: [] }; d.km += a.km || 0; d.mv += a.moving_s || 0; d.n++; d.a.push(a); byDay.set(a.date, d); }
  const years = [...new Set(acts.map(a => a._d.getFullYear()))].sort((a, b) => b - a);
  const vmax = quantile([...byDay.values()].map(d => d.km), .95) || 1;
  const color = km => km > 0 ? CAL_COLORS[Math.min(CAL_COLORS.length - 1, Math.floor(km / vmax * (CAL_COLORS.length - 0.001)))] : "#1e232b";
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
    b.onclick = () => calendar(host, acts, { all: true }); host.appendChild(b);
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
    if (v.n === 1) go(v.a[0].id); else { S.q = k; location.hash = "#/sessions"; }
  };
}

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
  const app = $("#app");
  app.innerHTML = `<p class="muted">Loading tracks…</p>`;
  const acts = inRange(await loadActs(S.type)), all = await loadTracks(S.type);
  const byId = new Map(acts.map(a => [a.id, a]));
  const tracks = all.filter(t => byId.has(t.id)).map(t => ({ ...t, a: byId.get(t.id) }));
  let mode = localStorage.mapMode || "heat", colorBy = localStorage.mapColor || "best10min_kmh", sel = [];
  const km = tracks.reduce((s, t) => s + (t.a.km || 0), 0);
  app.innerHTML = `
    <div class="head"><div><h1>${esc(S.type)} map</h1><div class="muted">${tracks.length} sessions with GPS · ${fkm(km)} km${S.range !== "all" ? " · " + esc($("#range").selectedOptions[0].text.toLowerCase()) : ""}</div></div></div>
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
      t.color = isNum(v) ? SPEED_COLORS[Math.max(0, Math.min(SPEED_COLORS.length - 1, Math.floor((v - lo) / ((hi - lo) || 1) * SPEED_COLORS.length)))] : "#555";
    }
    const lab = key === "_t" ? v => fmon(new Date(v)) : key === "km" ? v => f0(v) : v => f1(v);
    return `<span>${$("#mcolor").selectedOptions[0].text}</span>` + SPEED_COLORS.map((c, i) => `<span><i style="background:${c}"></i>${lab(lo + (hi - lo) * i / SPEED_COLORS.length)}</span>`).join("") + `<span><i style="background:#555"></i>n/a</span>`;
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

// ------------------------------------------------------------------ favorites

// Stored server-side (data/favorites.json) so the app window and a browser
// tab share them. {sessions: {id: {...}}, segments: {id: {type, start, end, m}}}
S.favs = { sessions: {}, segments: {} };
const isFav = (kind, id) => !!S.favs[kind][String(id)];
async function setFav(kind, id, on, meta) {
  const r = await fetch("/api/favorites", { method: "POST", headers: { "X-Rollbook": "1", "Content-Type": "application/json" },
    body: JSON.stringify({ kind, id: String(id), on, meta }) });
  const j = await r.json();
  if (!r.ok) throw new Error(j.error || r.status);
  S.favs = j;
}
const segMeta = sg => ({ type: S.type, start: sg.pts[0], end: sg.pts[sg.pts.length - 1], m: Math.round(sg.m) });
/** A star toggle; `onchange(on)` runs after the server confirms. */
function starBtn(kind, id, meta, onchange) {
  const b = document.createElement("button");
  b.className = "star"; b.type = "button";
  const paint = () => { const on = isFav(kind, id); b.classList.toggle("on", on); b.textContent = on ? "★" : "☆"; b.title = on ? "remove from favorites" : "add to favorites"; };
  b.onclick = async e => {
    e.stopPropagation(); e.preventDefault();
    try { await setFav(kind, id, !isFav(kind, id), typeof meta === "function" ? meta() : meta); paint(); onchange && onchange(isFav(kind, id)); }
    catch (err) { b.title = "could not save: " + err.message; }
  };
  paint();
  return b;
}
/** Replace every <span data-star="kind:id"> placeholder under root with a live star. */
function mountStars(root, metaFor, onchange) {
  root.querySelectorAll("[data-star]").forEach(el => {
    const [kind, id] = el.dataset.star.split(":");
    el.replaceWith(starBtn(kind, id, () => metaFor && metaFor(kind, id), onchange));
  });
}
const starSlot = (kind, id) => `<span data-star="${kind}:${id}"></span>`;

function favPanel(host, all) {
  const fav = all.filter(a => isFav("sessions", a.id)).sort((x, y) => y._t - x._t);
  const segs = Object.entries(S.favs.segments).filter(([, f]) => f.type === S.type);
  if (!fav.length && !segs.length) { host.hidden = true; return; }
  host.hidden = false;
  host.innerHTML = `<h2>★ Favorites</h2><div class="grid g2"><div>${fav.length ? `<table><thead><tr><th>date</th><th class="l">session</th><th>km</th><th>moving</th><th>avg km/h</th><th>best 10</th></tr></thead><tbody>` +
    fav.map(a => `<tr class="click" data-id="${a.id}"><td class="l">${fdate(a._d)}</td><td class="l">${esc(a.name)}</td><td>${fkm(a.km)}</td><td>${dur(a.moving_s, true)}</td><td>${f2(a.kmh_moving)}</td><td>${f2(a.best10min_kmh)}</td></tr>`).join("") + `</tbody></table>` : `<p class="muted small">No starred ${esc(S.type)} sessions. Use ☆ in the session list or on a session.</p>`}</div>
    <div id="fav-segs">${segs.length ? `<p class="muted small">loading segments…</p>` : `<p class="muted small">No starred segments. Use ☆ on the Segments page.</p>`}</div></div>`;
  host.querySelectorAll("tr[data-id]").forEach(tr => tr.onclick = () => go(tr.dataset.id));
  if (!segs.length) return;
  loadSegs(S.type).then(list => {
    const box = $("#fav-segs"); if (!box) return;
    const rows = list.filter(sg => isFav("segments", sg.id)).map(sg => ({ sg, st: segStats(sg) })).filter(r => r.st.n);
    box.innerHTML = rows.length ? `<table><thead><tr><th class="l">segment</th><th>sessions</th><th>best</th><th>km/h</th><th>latest</th></tr></thead><tbody>` +
      rows.map(r => `<tr class="click" data-sid="${r.sg.id}"><td class="l">${esc(segName(r.sg))}</td><td>${r.st.n}</td><td>${dur(r.st.pr.el, r.st.pr.el >= 3600)}</td><td>${f2(r.st.pr.kmh)}</td>
        <td>${dur(r.st.latest.el, r.st.latest.el >= 3600)} <span class="${r.st.latest === r.st.pr ? "up" : "muted"} small">${r.st.latest === r.st.pr ? "PR" : "+" + dur(r.st.latest.el - r.st.pr.el)}</span></td></tr>`).join("") + `</tbody></table>`
      : `<p class="muted small">Starred segments have no efforts in this range.</p>`;
    box.querySelectorAll("tr[data-sid]").forEach(tr => tr.onclick = () => { location.hash = "#/segment/" + tr.dataset.sid; });
  }).catch(() => { });
}

// ------------------------------------------------------------------ segments

const COMPASS = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"];
const compass = b => COMPASS[Math.round(((b % 360) + 360) % 360 / 45) % 8];
const flen = m => m < 1000 ? `${f0(m)} m` : `${f2(m / 1000)} km`;
const segNames = () => JSON.parse(localStorage.segNames || "{}");
function segName(sg) {
  const custom = segNames()[sg.id];
  if (custom) return custom;
  return sg.autoName || fallbackName(sg);
}
const fallbackName = sg => sg.kind === "loop" ? `${flen(sg.m)} loop` : `${sg.full ? "Full " : ""}${flen(sg.m)} ${compass(sg.bearing)}`;
/** Name from OSM places: "Area → Area" for lines, "Area loop" for loops, plus the road when one dominates. */
function placeName(sg) {
  const p = sg.place;
  if (!p || (!p.from && !p.to && !p.road)) return null;
  if (sg.kind === "loop") return `${p.from || p.city || ""} loop${p.road ? ` (${p.road})` : ""}`.trim();
  if (p.from && p.to && p.from !== p.to) return `${p.from} → ${p.to}`;
  const road = p.road || p.roads[0];
  return road ? `${road}, ${p.from || p.city}` : `${p.from || p.city} ${compass(sg.bearing)}`;
}
let namesTimer = null;
const sleep = ms => new Promise(r => setTimeout(r, ms));

/** Segments for a sport, with efforts resolved to activities. Polls while the server computes. */
async function loadSegs(type, onWait) {
  if (S.segs[type]) return S.segs[type];
  let r;
  for (;;) {
    r = await api("segments?type=" + encodeURIComponent(type));
    if (r.status === "ready" || r.error) break;
    onWait && onWait(r);
    await sleep(1000);
  }
  if (r.error) throw new Error(r.error);
  const acts = await loadActs(type), byId = new Map(acts.map(a => [a.id, a]));
  const segs = r.segments.map(sg => {
    const pts = sg.pts.flatMap(p => merc(p[0], p[1]));
    const efforts = sg.efforts.map(([aid, t0, el, mv, hr]) => ({ aid, t0, el, mv, hr, a: byId.get(aid), kmh: sg.m / el * 3.6 })).filter(e => e.a);
    return { ...sg, mpts: new Float64Array(pts), bbox: bboxOf(pts), efforts };
  });
  // place names; several segments can share "A → B" (a piece and a full leg),
  // so the length is added to tell them apart
  for (const sg of segs) sg.autoName = placeName(sg);
  const seen = {};
  for (const sg of segs) if (sg.autoName) seen[sg.autoName] = (seen[sg.autoName] || 0) + 1;
  for (const sg of segs) if (sg.autoName && seen[sg.autoName] > 1) sg.autoName += ` · ${flen(sg.m)}`;
  // lookups still running on the server: refetch soon and redraw this page
  if (r.names_pending && !namesTimer) namesTimer = setTimeout(() => {
    namesTimer = null; delete S.segs[type];
    if (/^#\/segment/.test(location.hash)) route();
  }, 6000);
  // the same stretch in the opposite direction (out vs back leg), if any
  // pieces are cut from different sessions, so endpoints rarely coincide:
  // match by path overlap (>= 70% of points within 50 m) and opposite heading
  const dist = (p, q) => Math.hypot((p[0] - q[0]) * 110540, (p[1] - q[1]) * 111320 * Math.cos(p[0] * Math.PI / 180));
  const overlap = (a, b) => { const pa = a.pts.filter((_, i) => i % 4 === 0); return pa.filter(p => b.pts.some(q => dist(p, q) < 50)).length / pa.length; };
  for (const sg of segs) {
    sg.reverse = null;
    if (sg.kind !== "line") continue;
    let best = 0;
    for (const o of segs) {
      if (o === sg || o.kind !== "line" || o.full !== sg.full || Math.abs(o.m / sg.m - 1) > 0.25) continue;
      const diff = Math.abs(((o.bearing - sg.bearing + 540) % 360) - 180);  // 0 = same heading
      if (diff < 130) continue;  // not roughly opposite
      const ov = Math.min(overlap(sg, o), overlap(o, sg));
      if (ov >= 0.7 && ov > best) { best = ov; sg.reverse = o; }
    }
  }
  // segment ids hash geometry and change when new data regenerates them;
  // re-attach starred segments of this sport by start, end and length
  const lost = Object.entries(S.favs.segments).filter(([id, f]) => f.type === type && f.start && !segs.some(sg => sg.id === id));
  for (const [oldId, f] of lost) {
    const sg = segs.find(sg => !isFav("segments", sg.id) && dist(sg.pts[0], f.start) < 80 && dist(sg.pts[sg.pts.length - 1], f.end) < 80 && Math.abs(sg.m / f.m - 1) < 0.1);
    if (!sg) continue;
    const name = segNames()[oldId];
    if (name && !segNames()[sg.id]) { const n = segNames(); n[sg.id] = name; localStorage.segNames = JSON.stringify(n); }
    try { await setFav("segments", sg.id, true, segMeta(sg)); await setFav("segments", oldId, false); } catch { }
  }
  return (S.segs[type] = segs);
}

/** Efforts in the current range; best per session, fastest first. */
function segStats(sg) {
  const cut = S.range === "all" ? -Infinity : Date.now() - (+S.range) * DAY;
  const effs = sg.efforts.filter(e => e.a._t >= cut);
  const per = new Map();
  for (const e of effs) { const b = per.get(e.aid); if (!b || e.el < b.el) per.set(e.aid, e); }
  const best = [...per.values()].sort((a, b) => a.el - b.el);
  const latest = best.slice().sort((a, b) => b.a._t - a.a._t)[0];
  return { effs, best, pr: best[0], latest, med: median(best.map(e => e.el)), n: best.length };
}

function segMap(host, segs, opt = {}) {
  const map = new SlippyMap(host);
  const tr = segs.map((sg, i) => ({ id: sg.id, sg, pts: sg.mpts, bbox: sg.bbox, color: opt.color ? opt.color(sg, i) : SPEED_COLORS[i % SPEED_COLORS.length] }));
  const lines = new LinesLayer(tr, { width: opt.width || 3.5, alpha: .9 });
  const hl = new LinesLayer([], { width: 4 });
  map.layers.push(lines, hl, {
    draw(ctx, mp) {  // start (green) and end (red) markers, arrow at mid-point
      for (const t of tr) {
        const p = t.pts, n = p.length / 2;
        if (!opt.markers && !t.hi && segs.length > 1) continue;
        const a = mp.toScreen(p[0], p[1]), b = mp.toScreen(p[p.length - 2], p[p.length - 1]);
        const m = Math.floor(n / 2) * 2, c = mp.toScreen(p[m], p[m + 1]), d = mp.toScreen(p[Math.min(p.length - 2, m + 4)], p[Math.min(p.length - 1, m + 5)]);
        const ang = Math.atan2(d[1] - c[1], d[0] - c[0]);
        ctx.save(); ctx.translate(...c); ctx.rotate(ang);
        ctx.beginPath(); ctx.moveTo(8, 0); ctx.lineTo(-6, -6); ctx.lineTo(-6, 6); ctx.closePath();
        ctx.fillStyle = "#fff"; ctx.strokeStyle = "#000"; ctx.lineWidth = 1.5; ctx.fill(); ctx.stroke(); ctx.restore();
        for (const [q, col] of [[a, "#3ecf8e"], [b, "#ff4d4d"]]) { ctx.beginPath(); ctx.arc(...q, 6, 0, 7); ctx.fillStyle = col; ctx.fill(); ctx.lineWidth = 2; ctx.strokeStyle = "#000"; ctx.stroke(); }
      }
    }
  });
  const all = [Math.min(...tr.map(t => t.bbox[0])), Math.min(...tr.map(t => t.bbox[1])), Math.max(...tr.map(t => t.bbox[2])), Math.max(...tr.map(t => t.bbox[3]))];
  map.fit(opt.fit || all, 40);
  return {
    map,
    highlight(id) { hl.tracks = tr.filter(t => t.id === id).map(t => ({ ...t, hi: true })); tr.forEach(t => t.hi = t.id === id); map.redraw(); },
    hit(pos) { return hitTracks(map, tr, pos.x, pos.y, 8)[0]; },
  };
}

async function viewSegments() {
  const app = $("#app");
  app.innerHTML = `<p class="muted" id="segwait">Finding segments…</p>`;
  const segs = await loadSegs(S.type, r => { const w = $("#segwait"); if (w) w.textContent = `Finding segments: ${r.phase}${r.total ? ` ${r.done}/${r.total}` : ""}…`; });
  const rows = segs.map(sg => ({ sg, st: segStats(sg) })).filter(r => r.st.n);
  let favOnly = localStorage.segFavOnly === "1";
  if (!rows.length) { app.innerHTML = `<p class="muted">No segments for ${esc(S.type)} in this range. Segments are stretches at least ${POP_HINT} sessions share.</p>`; return; }
  let sort = { k: "n", dir: -1 };
  app.innerHTML = `
    <div class="head"><div><h1>${esc(S.type)} segments</h1><div class="muted">${rows.length} segments found automatically from the roads and loops you use most · best time per session</div></div></div>
    <div class="panel"><div id="segmap" class="map" style="height:460px"></div>
      <div class="note">Arrows show direction: out and back legs are separate segments, so their times reflect wind. Hover a line or a row to match them; click to open. "Full" segments chain several shorter ones into a whole leg.</div></div>
    <div class="panel"><div class="toolbar"><label class="muted small"><input type="checkbox" id="segfav" ${favOnly ? "checked" : ""}> ★ favorites only</label><span class="muted small" id="segcnt"></span></div>
      <div class="scroll" style="max-height:640px"><table id="segtbl"></table></div></div>`;
  const sm = segMap($("#segmap"), rows.map(r => r.sg), { width: 3 });
  const cols = [["fav", "★", r => starSlot("segments", r.sg.id)], ["name", "segment", r => `${esc(segName(r.sg))}${r.sg.place && r.sg.place.roads.length ? `<div class="muted small">${esc(r.sg.place.roads.slice(0, 2).join(", "))}</div>` : ""}${r.sg.kind === "loop" ? ` <span class="pill loop">loop</span>` : ""}`, "l"],
    ["m", "length", r => flen(r.sg.m)], ["n", "sessions", r => r.st.n],
    ["pr", "best", r => dur(r.st.pr.el, r.st.pr.el >= 3600)], ["prk", "km/h", r => f2(r.st.pr.kmh)], ["prd", "best on", r => fdate(r.st.pr.a._d), "l"],
    ["last", "latest", r => `${dur(r.st.latest.el, r.st.latest.el >= 3600)} <span class="${r.st.latest === r.st.pr ? "up" : "muted"} small">${r.st.latest === r.st.pr ? "PR" : "+" + dur(r.st.latest.el - r.st.pr.el)}</span>`],
    ["med", "median", r => dur(r.st.med, r.st.med >= 3600)]];
  const val = { fav: r => isFav("segments", r.sg.id) ? 1 : 0, name: r => segName(r.sg), m: r => r.sg.m, n: r => r.st.n, pr: r => r.st.pr.el, prk: r => r.st.pr.kmh, prd: r => r.st.pr.a._t, last: r => r.st.latest.el - r.st.pr.el, med: r => r.st.med };
  const draw = () => {
    const rs = rows.filter(r => !favOnly || isFav("segments", r.sg.id)).sort((a, b) => { const x = val[sort.k](a), y = val[sort.k](b); return (x > y ? 1 : x < y ? -1 : 0) * sort.dir; });
    $("#segcnt").textContent = favOnly ? `${rs.length} of ${rows.length}` : `${Object.values(S.favs.segments).filter(f => f.type === S.type).length} starred`;
    $("#segtbl").innerHTML = `<thead><tr>${cols.map(([k, n, , c]) => `<th class="sort ${c || ""} ${sort.k === k ? (sort.dir > 0 ? "asc" : "desc") : ""}" data-k="${k}">${n}</th>`).join("")}</tr></thead><tbody>` +
      rs.map(r => `<tr class="click" data-id="${r.sg.id}">${cols.map(([, , f, c]) => `<td class="${c || ""}">${f(r)}</td>`).join("")}</tr>`).join("") + `</tbody>`;
    mountStars($("#segtbl"), (k, id) => segMeta(rows.find(r => r.sg.id === id).sg), () => draw());
    $("#segtbl").querySelectorAll("th").forEach(th => th.onclick = () => { const k = th.dataset.k; sort = { k, dir: sort.k === k ? -sort.dir : (k === "name" || k === "pr" || k === "med" || k === "last" ? 1 : -1) }; draw(); });
    $("#segtbl").querySelectorAll("tr[data-id]").forEach(tr => {
      tr.onclick = () => { location.hash = "#/segment/" + tr.dataset.id; };
      tr.onmouseenter = () => sm.highlight(tr.dataset.id);
      tr.onmouseleave = () => sm.highlight(null);
    });
  };
  sm.map.on("hover", (pos, ev) => {
    const h = pos && sm.hit(pos);
    sm.highlight(h ? h.id : null); sm.map.canvas.style.cursor = h ? "pointer" : "";
    if (!h) { hideTip(); return; }
    const r = rows.find(r => r.sg.id === h.id);
    showTip(`<b>${esc(segName(r.sg))}</b><div class="row"><span>sessions</span><span>${r.st.n}</span></div><div class="row"><span>best</span><span>${dur(r.st.pr.el)} · ${f2(r.st.pr.kmh)} km/h</span></div>`, ev);
  });
  $("#segfav").onchange = e => { favOnly = e.target.checked; localStorage.segFavOnly = favOnly ? "1" : "0"; draw(); };
  sm.map.on("click", pos => { const h = sm.hit(pos); if (h) { hideTip(); location.hash = "#/segment/" + h.id; } });
  addEventListener("hashchange", () => sm.map.destroy(), { once: true });
  draw();
}
const POP_HINT = 5;

async function viewSegment(id) {
  const app = $("#app");
  app.innerHTML = `<p class="muted" id="segwait">Loading segment…</p>`;
  const segs = await loadSegs(S.type, r => { const w = $("#segwait"); if (w) w.textContent = `Finding segments: ${r.phase}…`; });
  const sg = segs.find(s => s.id === id);
  if (!sg) { app.innerHTML = `<p class="muted">Segment not found for ${esc(S.type)} (segments are regenerated when new sessions are imported).</p>`; return; }
  const st = segStats(sg);
  let allEfforts = false;
  const last5 = st.best.slice().sort((a, b) => b.a._t - a.a._t).slice(0, 5);
  const trendRecent = median(last5.map(e => e.kmh)), trendPrev = median(st.best.slice().sort((a, b) => b.a._t - a.a._t).slice(5, 15).map(e => e.kmh));
  app.innerHTML = `
    <div class="head"><div><h1>${starSlot("segments", sg.id)} <span id="sgname">${esc(segName(sg))}</span> <button class="btn small" id="rename" title="rename">✎</button></h1>
      <div class="muted">${sg.place && sg.place.roads.length ? `via ${esc(sg.place.roads.slice(0, 3).join(", "))}${sg.place.city ? ` · ${esc(sg.place.city)}` : ""} · ` : ""}${sg.kind === "loop" ? "loop" : `heading ${compass(sg.bearing)} (${f0(sg.bearing)}°)`} · starts ${sg.pts[0][0].toFixed(4)}, ${sg.pts[0][1].toFixed(4)}${sg.parts?.length ? ` · made of ${sg.parts.length} shorter segments` : ""}</div></div>
      <div class="nav">${sg.reverse ? `<a class="btn" href="#/segment/${sg.reverse.id}">⇄ opposite direction</a>` : ""}<a class="btn" href="#/segments">all segments</a></div></div>
    ${st.n ? cards([
      { k: "Length", v: flen(sg.m) },
      { k: "Sessions", v: st.n, s: `${st.effs.length} efforts` },
      { k: "Best", v: dur(st.pr.el, st.pr.el >= 3600), s: `${f2(st.pr.kmh)} km/h · <a href="#/session/${st.pr.aid}">${fdate(st.pr.a._d)}</a>`, hl: 1 },
      { k: "Median", v: dur(st.med, st.med >= 3600), s: `${f2(sg.m / st.med * 3.6)} km/h` },
      { k: "Latest", v: dur(st.latest.el, st.latest.el >= 3600), s: `${fdate(st.latest.a._d)} · ${st.latest === st.pr ? "PR" : "+" + dur(st.latest.el - st.pr.el) + " vs best"}` },
      { k: "Last 5 vs previous 10", v: isNum(trendRecent) && isNum(trendPrev) ? `<span class="${trendRecent >= trendPrev ? "up" : "down"}">${pct((trendRecent / trendPrev - 1) * 100)}</span>` : "–", s: "median speed" },
      ...(sg.reverse ? (() => { const r = segStats(sg.reverse); return r.n ? [{ k: "Opposite direction best", v: dur(r.pr.el, r.pr.el >= 3600), s: `${f2(r.pr.kmh)} km/h · ${pct((st.pr.kmh / r.pr.kmh - 1) * 100)} this way` }] : []; })() : []),
    ]) : `<p class="muted">No efforts in this date range.</p>`}
    <div class="grid g2">
      <div class="panel"><h2>Map</h2><div id="sgmap" class="map"></div></div>
      <div class="panel"><h2>Speed over time <span class="muted">best effort per session · click to open</span></h2><div id="c-seg"></div>
        <div class="note">${sg.kind === "line" ? "One-way segment: a tailwind day makes a fast time here. Compare with the opposite direction before reading a PR as fitness." : "Loop: wind mostly cancels out, so times here are a fair fitness comparison."}</div></div>
    </div>
    <div class="panel"><div class="toolbar"><h2 style="margin:0">Leaderboard</h2><div class="seg" id="lbmode"><button data-m="best" class="on">best per session</button><button data-m="all">all efforts</button></div></div>
      <div class="scroll" style="max-height:600px"><table id="lb"></table></div></div>`;
  mountStars(app.querySelector(".head"), () => segMeta(sg));
  $("#rename").onclick = () => {
    const n = prompt(`Segment name (empty to reset to "${sg.autoName || fallbackName(sg)}")`, segNames()[sg.id] || "");
    if (n === null) return;
    const names = segNames(); if (n.trim()) names[sg.id] = n.trim(); else delete names[sg.id];
    localStorage.segNames = JSON.stringify(names); $("#sgname").textContent = segName(sg);
  };
  const sm = segMap($("#sgmap"), [sg], { markers: true, width: 5, color: () => "#ff7a1a" });
  addEventListener("hashchange", () => sm.map.destroy(), { once: true });
  if (!st.n) return;
  chart($("#c-seg"), {
    x: { type: "time" }, y: { fmt: v => v.toFixed(0), label: "km/h" }, onClick: go,
    series: [{ name: "best per session", kind: "dot", color: "#ff7a1a", r: 3.5, data: st.best.map(e => ({ x: e.a._t, y: e.kmh, ref: e.aid, color: e === st.pr ? "#fff" : null })) },
      { name: "rolling median", kind: "line", color: "#ffd166", width: 2, data: rolling(st.best.slice().sort((a, b) => a.a._t - b.a._t).map(e => ({ x: e.a._t, y: e.kmh })), 6), gapX: 60 * DAY }],
    tip: (x, hits, b) => { const e = st.best.find(e => e.aid === b.p.ref); return e ? `<b>${fdate(e.a._d)}</b>${e === st.pr ? " · PR" : ""}<div class="row"><span>time</span><span>${dur(e.el, e.el >= 3600)}</span></div><div class="row"><span>speed</span><span>${f2(e.kmh)} km/h</span></div>${e.hr ? `<div class="row"><span>HR</span><span>${e.hr}</span></div>` : ""}` : ""; },
  });
  const drawLb = () => {
    const list = (allEfforts ? st.effs.slice().sort((a, b) => a.el - b.el) : st.best);
    $("#lb").innerHTML = `<thead><tr><th>#</th><th class="l">date</th><th class="l">session</th><th>time</th><th>vs best</th><th>km/h</th><th>pace</th><th>moving</th><th>HR</th></tr></thead><tbody>` +
      list.map((e, i) => `<tr class="click${e === st.pr ? " best" : ""}" data-id="${e.aid}"><td>${i + 1}</td><td class="l">${fdate(e.a._d)}</td><td class="l">${esc(e.a.name)}</td>
        <td>${dur(e.el, e.el >= 3600)}</td><td>${e === st.pr ? "PR" : "+" + dur(e.el - st.pr.el)}</td><td>${f2(e.kmh)}</td><td>${pace(e.kmh)}</td><td>${dur(e.mv, e.mv >= 3600)}</td><td>${f0(e.hr)}</td></tr>`).join("") + `</tbody>`;
    $("#lb").querySelectorAll("tr[data-id]").forEach(tr => tr.onclick = () => go(tr.dataset.id));
  };
  $("#lbmode").onclick = e => { const m = e.target.dataset.m; if (!m) return; allEfforts = m === "all"; $("#lbmode").querySelectorAll("button").forEach(b => b.classList.toggle("on", b.dataset.m === m)); drawLb(); };
  drawLb();
}

/** Session page panel: this session's efforts with rank among all sessions. */
async function sessionSegments(a, map) {
  const host = $("#p-segs");
  let segs;
  try { segs = await loadSegs(a.type, r => { host.hidden = false; host.innerHTML = `<h2>Segments</h2><p class="muted small">finding segments: ${r.phase}…</p>`; }); }
  catch { host.hidden = true; return; }
  const mine = [];
  for (const sg of segs) {
    const es = sg.efforts.filter(e => e.aid === a.id);
    if (!es.length) continue;
    const best = es.reduce((b, e) => e.el < b.el ? e : b);
    const per = new Map(); for (const e of sg.efforts) { const b = per.get(e.aid); if (!b || e.el < b.el) per.set(e.aid, e); }
    const ranked = [...per.values()].sort((x, y) => x.el - y.el), rank = ranked.indexOf(best) + 1;
    const prior = ranked.filter(e => e.a._t < a._t || (e.a._t === a._t && e.aid !== a.id && e.t0 < best.t0));
    mine.push({ sg, best, n: es.length, rank, of: ranked.length, pr: ranked[0], prBefore: prior.length ? prior[0] : null });
  }
  if (!$("#p-segs")) return;  // navigated away
  if (!mine.length) { host.hidden = true; return; }
  host.hidden = false;
  mine.sort((x, y) => x.best.t0 - y.best.t0);
  host.innerHTML = `<h2>Segments <span class="muted">${mine.length} in this session · hover to show on the map</span></h2><div class="scroll"><table><thead><tr><th class="l">segment</th><th>length</th><th>time</th><th>km/h</th><th>HR</th><th>rank</th><th>vs best</th><th class="l"></th></tr></thead><tbody>` +
    mine.map(m => {
      const isPr = m.rank === 1, newPr = m.prBefore ? m.best.el < m.prBefore.el : true;
      return `<tr class="click${isPr ? " best" : ""}" data-sid="${m.sg.id}"><td class="l">${isFav("segments", m.sg.id) ? `<span class="star on">★</span> ` : ""}${esc(segName(m.sg))}${m.n > 1 ? ` <span class="muted small">×${m.n}</span>` : ""}</td><td>${flen(m.sg.m)}</td>
        <td>${dur(m.best.el, m.best.el >= 3600)}</td><td>${f2(m.best.kmh)}</td><td>${f0(m.best.hr)}</td><td>${m.rank} / ${m.of}</td>
        <td>${isPr ? "PR" : "+" + dur(m.best.el - m.pr.el)}</td><td class="l">${newPr && m.of > 1 ? `<span class="pill ob">PR at the time</span>` : ""}</td></tr>`;
    }).join("") + `</tbody></table></div><div class="note">Rank is among each session's best effort. "PR at the time" = fastest so far when this session happened.</div>`;
  host.querySelectorAll("tr[data-sid]").forEach(tr => {
    const m = mine.find(m => m.sg.id === tr.dataset.sid);
    tr.onclick = () => { location.hash = "#/segment/" + m.sg.id; };
    tr.onmouseenter = () => map && map.highlight(m.best.t0, m.best.t0 + m.best.el, false);
    tr.onmouseleave = () => map && map.highlight(null);
  });
}

async function viewSessions() {
  const acts = inRange(await loadActs(S.type));
  const cols = [
    ["fav", "★", a => starSlot("sessions", a.id)], ["date", "date", a => `${fdate(a._d)}`, "l"], ["name", "name", a => esc(a.name), "l"], ["km", "km", a => fkm(a.km)],
    ["elapsed_s", "elapsed", a => dur(a.elapsed_s, true)], ["moving_s", "moving", a => dur(a.moving_s, true)], ["pause_s", "paused", a => dur(a.pause_s)],
    ["kmh_moving", "avg km/h", a => f2(a.kmh_moving)], ["pace", "pace /km", a => pace(a.kmh_moving)], ["best10min_kmh", "best 10", a => f2(a.best10min_kmh)],
    ["b60", "best 60", a => f2(a.b60)], ["wind_neutral", "wind-neutral", a => f2(a.wind_neutral)], ["asym_pct", "out vs back", a => pct(a.asym_pct)],
    ["laps_n", "laps", a => a.laps_n ? `${a.laps_n} × ${f2(a.lap_km)}` : ""], ["best_lap_s", "best lap", a => a.best_lap_s ? dur(a.best_lap_s) : ""],
    ["hr_avg", "HR", a => f0(a.hr_avg)], ["ascent_m", "ascent", a => f0(a.ascent_m)], ["route", "route", a => routePill(a.route), "l"],
  ];
  const app = $("#app");
  app.innerHTML = `<div class="head"><div><h1>${esc(S.type)} sessions</h1><div class="muted" id="cnt"></div></div></div>
    <div class="panel"><div class="toolbar"><input id="q" placeholder="filter: name, date, route…" value="${esc(S.q)}" style="width:260px">
      <label class="muted small">min km <input id="mink" type="number" value="${localStorage.minKm || 0}" style="width:70px"></label>
      <label class="muted small"><input type="checkbox" id="favonly" ${localStorage.favOnly === "1" ? "checked" : ""}> ★ favorites only</label></div>
      <div class="scroll"><table id="tbl"></table></div></div>`;
  const draw = () => {
    const q = S.q.toLowerCase(), mink = +($("#mink").value || 0);
    const k = S.sort.k, dir = S.sort.dir;
    const val = a => k === "pace" ? (a.kmh_moving ? -a.kmh_moving : null) : k === "date" ? a._t : k === "fav" ? (isFav("sessions", a.id) ? 1 : 0) : a[k];
    const favOnly = $("#favonly").checked;
    const rows = acts.filter(a => (a.km || 0) >= mink && (!favOnly || isFav("sessions", a.id)) && (!q || `${a.name} ${a.date} ${a.route} ${a.device}`.toLowerCase().includes(q)))
      .sort((a, b) => { const x = val(a), y = val(b); if (x == null) return 1; if (y == null) return -1; return (x > y ? 1 : x < y ? -1 : 0) * dir; });
    $("#cnt").textContent = `${rows.length} of ${acts.length} sessions · ${fkm(rows.reduce((s, a) => s + (a.km || 0), 0))} km`;
    $("#tbl").innerHTML = `<thead><tr>${cols.map(([k, n, , c]) => `<th class="sort ${c || ""} ${S.sort.k === k ? (S.sort.dir > 0 ? "asc" : "desc") : ""}" data-k="${k}">${n}</th>`).join("")}</tr></thead><tbody>` +
      rows.map(a => `<tr class="click" data-id="${a.id}">${cols.map(([, , f, c]) => `<td class="${c || ""}">${f(a)}</td>`).join("")}</tr>`).join("") + `</tbody>`;
    $("#tbl").querySelectorAll("th").forEach(th => th.onclick = () => { const k = th.dataset.k; S.sort = { k, dir: S.sort.k === k ? -S.sort.dir : -1 }; draw(); });
    $("#tbl").querySelectorAll("tr[data-id]").forEach(tr => tr.onclick = () => go(tr.dataset.id));
    mountStars($("#tbl"), null, () => favOnly && draw());
  };
  $("#q").oninput = e => { S.q = e.target.value; draw(); };
  $("#mink").oninput = e => { localStorage.minKm = e.target.value; draw(); };
  $("#favonly").onchange = e => { localStorage.favOnly = e.target.checked ? "1" : "0"; draw(); };
  draw();
}

/** Laps that count for stats: regular and not dominated by a stop. */
const goodLaps = L => L.laps.filter(x => x.counted);

function lapWidgets(L, map, hasHr) {
  const laps = L.laps, good = goodLaps(L);
  const best = good.reduce((b, x) => !b || x.elapsed < b.elapsed ? x : b, null);
  const worst = good.reduce((b, x) => !b || x.elapsed > b.elapsed ? x : b, null);
  const mt = good.map(x => x.moving), mean = mt.reduce((s, v) => s + v, 0) / (mt.length || 1);
  const cv = mt.length > 1 ? Math.sqrt(mt.reduce((s, v) => s + (v - mean) ** 2, 0) / mt.length) / mean * 100 : null;
  const half = Math.floor(good.length / 2);
  const h1 = median(good.slice(0, half).map(x => x.kmh)), h2 = median(good.slice(good.length - half).map(x => x.kmh));
  const lapKm = laps.reduce((s, x) => s + x.km, 0);
  $("#lap-cards").innerHTML = cards([
    { k: "Best lap", v: best ? dur(best.elapsed) : "–", s: best ? `lap ${best.n} · ${f2(best.kmh_elapsed)} km/h` : "", hl: 1 },
    { k: "Median lap", v: good.length ? dur(median(good.map(x => x.elapsed))) : "–", s: good.length ? `${f2(median(good.map(x => x.kmh)))} km/h moving` : "" },
    { k: "Slowest lap", v: worst ? dur(worst.elapsed) : "–", s: worst ? `lap ${worst.n} · ${f2(worst.kmh_elapsed)} km/h` : "" },
    { k: "Consistency", v: isNum(cv) ? "±" + cv.toFixed(1) + "%" : "–", s: "spread of lap moving times" },
    { k: "Fade", v: isNum(h1) && isNum(h2) ? `<span class="${h2 >= h1 ? "up" : "down"}">${pct((h2 / h1 - 1) * 100)}</span>` : "–", s: `2nd half vs 1st half speed` },
    { k: "Counted laps", v: `${good.length} / ${laps.length}`, s: `${fkm(lapKm)} km in laps` },
  ]);
  const vals = good.map(x => x.kmh), lo = quantile(vals, .1), hi = quantile(vals, .9);
  const col = x => !good.includes(x) ? "#555" : SPEED_COLORS[Math.max(0, Math.min(SPEED_COLORS.length - 1, Math.floor((x.kmh - lo) / ((hi - lo) || 1) * (SPEED_COLORS.length - 1) + .5)))];
  let selected = null;
  const select = n => {
    selected = selected === n ? null : n;
    const x = laps.find(x => x.n === selected);
    if (map) map.highlight(x ? x.t0 : null, x ? x.t1 : null, !!x);
    $("#t-laps").querySelectorAll("tr[data-n]").forEach(tr => tr.classList.toggle("sel", +tr.dataset.n === selected));
  };
  const tipLap = x => `<b>Lap ${x.n}${x === best ? " · best" : ""}${good.includes(x) ? "" : " · irregular"}</b>
    <div class="row"><span>time</span><span>${dur(x.elapsed)}</span></div><div class="row"><span>moving</span><span>${dur(x.moving)}${x.pause > 1 ? ` (+${dur(x.pause)} stopped)` : ""}</span></div>
    <div class="row"><span>speed</span><span>${f2(x.kmh)} km/h · ${pace(x.kmh)}/km</span></div><div class="row"><span>distance</span><span>${f2(x.km)} km</span></div>` + (x.hr ? `<div class="row"><span>HR</span><span>${f0(x.hr)} bpm</span></div>` : "");
  const ymin = Math.max(0, Math.floor((quantile(vals, 0) ?? 10) - 3));
  chart($("#c-laps"), {
    height: 300, x: { type: "linear", fmt: v => Number.isInteger(v) ? String(v) : "" }, y: { min: ymin, fmt: v => v.toFixed(0), label: "km/h" },
    y2: hasHr && laps.some(x => x.hr) ? { fmt: v => v.toFixed(0), label: "bpm" } : null, refY: median(vals),
    series: [{ name: "lap speed (moving)", kind: "bar", color: "#3fb6ff", data: laps.filter(x => x.kmh).map(x => ({ x: x.n, y: Math.max(ymin, x.kmh), ref: x.n, color: col(x) })) },
      ...(hasHr ? [{ name: "avg HR", kind: "line", axis: "y2", color: "#ff4d4d", width: 1.5, data: laps.filter(x => x.hr).map(x => ({ x: x.n, y: x.hr, ref: x.n })) }] : [])],
    tip: (xv) => { const x = laps.find(l => l.n === xv); return x ? tipLap(x) : ""; },
    onClick: select,
  });
  const row = (x) => `<tr class="click${x === best ? " best" : ""}" data-n="${x.n}"><td>${x.n}</td><td>${f2(x.km)}</td><td>${dur(x.elapsed)}</td><td>${dur(x.moving)}</td>
    <td>${x.pause > 1 ? dur(x.pause) : ""}</td><td><span class="bar" style="width:${x.kmh && hi ? Math.max(4, (x.kmh - ymin) / (Math.max(...vals) - ymin) * 70).toFixed(0) : 0}px;background:${col(x)}"></span> ${f2(x.kmh)}</td>
    <td>${pace(x.kmh)}</td><td class="${best && x.elapsed - best.elapsed > 0 ? "down" : "up"}">${best ? (x === best ? "best" : "+" + dur(x.elapsed - best.elapsed)) : ""}</td><td>${f0(x.hr)}</td></tr>`;
  const extra = (label, seg) => seg.km > 0.05 ? `<tr class="muted"><td class="l" colspan="2">${label}</td><td colspan="7" class="l">${f2(seg.km)} km</td></tr>` : "";
  $("#t-laps").innerHTML = `<table><thead><tr><th>lap</th><th>km</th><th>time</th><th>moving</th><th>stopped</th><th>km/h</th><th>pace</th><th>vs best</th><th>HR</th></tr></thead><tbody>` +
    extra("before first lap", L.pre) + laps.map(row).join("") + extra("after last lap", L.post) + `</tbody></table>`;
  $("#t-laps").querySelectorAll("tr[data-n]").forEach(tr => tr.onclick = () => select(+tr.dataset.n));
}

async function viewSession(id) {
  const app = $("#app");
  app.innerHTML = `<p class="muted">Loading session…</p>`;
  const a = await api("activity/" + id);
  const m = a.metrics || {};
  const peers = await loadActs(a.type);
  const i = peers.findIndex(p => p.id === a.id), prev = peers[i - 1], next = peers[i + 1];
  const d = new Date(a.datetime || a.date);
  const ser = a.series || [];
  const hasHr = ser.some(r => r[3]), hasEle = ser.some(r => r[4] != null), hasGps = ser.some(r => r[5] != null);
  const avg = a.kmh_moving;
  const L = m.laps;
  app.innerHTML = `
    <div class="head">
      <div><h1>${starSlot("sessions", a.id)} ${esc(a.name)} <span class="muted small">${esc(a.type)}</span></h1>
        <div class="muted">${fdate(d)} · ${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")} · ${esc(a.device || "")} · ${routePill(m.route)}</div></div>
      <div class="nav">
        <button class="btn" id="prev" ${prev ? "" : "disabled"}>← previous</button>
        <button class="btn" id="next" ${next ? "" : "disabled"}>next →</button>
      </div>
    </div>
    ${cards([
      { k: "Distance", v: fkm(a.km) + " km", s: isNum(a.km_diff_pct) && Math.abs(a.km_diff_pct) > 1 ? `track differs ${pct(a.km_diff_pct)}` : "" },
      { k: "Elapsed", v: dur(a.elapsed_s, true) }, { k: "Moving", v: dur(a.moving_s, true) },
      { k: "Paused", v: dur(a.pause_s), s: isNum(a.pause_s) && a.elapsed_s ? (a.pause_s / a.elapsed_s * 100).toFixed(1) + "% of elapsed" : "" },
      { k: "Avg speed (moving)", v: f2(avg), s: "km/h", hl: 1 }, { k: "Avg pace", v: pace(avg), s: "min/km (moving)" },
      { k: "Avg speed (elapsed)", v: f2(a.kmh_elapsed), s: `${pace(a.kmh_elapsed)} /km` },
      { k: "Best 5 / 10 / 20 min", v: `${f1(a.best5min_kmh)}`, s: `${f1(a.best10min_kmh)} · ${f1(a.best20min_kmh)} km/h` },
      { k: "Best 30 / 60 min", v: f1(m.b30), s: `${f1(m.b60)} km/h` },
      { k: "Top speed (30 s)", v: f1(a.best30_kmh), s: "km/h" },
      ...(L ? [{ k: "Laps", v: `${L.laps.length}`, s: `${f2(L.lap_km)} km circuit` }] : []),
      ...(m.route === "out-back" ? [{ k: "Wind-neutral 10 min", v: f2(m.wind_neutral), s: `out ${pct(m.asym_pct)} vs back`, hl: 1 }] : []),
      ...(a.hr_avg ? [{ k: "Heart rate", v: f0(a.hr_avg), s: `avg · max ${f0(a.hr_max)} bpm` }, { k: "Efficiency", v: a.best10min_kmh ? f2(a.best10min_kmh / a.hr_avg * 100) : "–", s: "best10 / HR ×100" }] : []),
      ...(a.s_avg_watts ? [{ k: "Power", v: f0(a.s_avg_watts) + " W", s: `max ${f0(a.s_max_watts)} W` }] : []),
      { k: "Ascent", v: f0(a.ascent_m) + " m" },
      ...(a.s_calories ? [{ k: "Calories", v: f0(a.s_calories) }] : []),
    ])}
    <div class="grid g2">
      <div class="panel"><h2>Route <span class="muted">coloured by speed · drag / scroll to explore · hover the chart to follow along</span></h2><div id="map"></div></div>
      <div class="panel"><h2>Speed${hasHr ? ", heart rate" : ""}${hasEle ? " &amp; elevation" : ""}</h2>
        <div class="toolbar"><div class="seg" id="xmode"><button data-x="km" class="on">distance</button><button data-x="t">time</button></div></div>
        <div id="c-speed"></div>
        ${hasEle ? `<div class="subhead"><b>Elevation</b> <span class="muted small" id="ele-sum"></span></div><div id="c-ele"></div>` : ""}
        <div id="speed-sel" class="selbox" hidden></div>
        <div class="note">Drag across the chart to select a stretch; click to select that km.</div></div>
    </div>
    ${L ? `<div class="panel"><h2>Laps <span class="muted">${L.laps.length} × ${f2(L.lap_km)} km circuit · click a lap to show it on the map</span></h2>
      <div id="lap-cards"></div>
      <div class="grid g2" style="margin-top:14px"><div id="c-laps"></div><div class="scroll" id="t-laps" style="max-height:520px"></div></div>
      <div class="note">A lap starts each time you cross the gate (◆ on the map) heading the same way. Grey laps are irregular — a shortcut, a GPS dropout or a different line — and are left out of the lap stats.</div></div>` : ""}
    <div class="grid g2">
      <div class="panel"><h2>Speed per km</h2><div id="c-splits"></div></div>
      <div class="panel"><h2>Splits</h2><div class="scroll" id="t-splits"></div></div>
    </div>
    <div class="grid g2">
      ${m.route === "out-back" ? `<div class="panel"><h2>Out / back legs</h2><div id="t-legs"></div>
        <div class="note">A large gap between legs on a flat route is wind. Wind-neutral speed averages the two legs' best 10 min.</div></div>` : ""}
      ${hasHr ? `<div class="panel"><h2>Heart-rate zones <span class="muted">HRmax ${S.hrmax} bpm</span></h2><div id="t-zones"></div></div>` : ""}
      <div class="panel" id="p-segs" hidden></div>
      <div class="panel"><h2>Same route, other sessions</h2><div class="scroll" id="t-same"></div></div>
    </div>`;
  mountStars(app.querySelector(".head"));
  $("#prev").onclick = () => prev && go(prev.id);
  $("#next").onclick = () => next && go(next.id);

  let selKm = null, selectKm = () => {};
  const map = hasGps ? sessionMap($("#map"), ser, { turnKm: m.route === "out-back" ? m.turn_km : null, gate: L ? L.gate : null }) : ($("#map").innerHTML = `<p class="muted">No GPS (indoor session).</p>`, null);
  // One selected stretch for the whole page. Everything that highlights the map
  // (km splits, out/back legs, laps, segment rows, brushing the speed chart)
  // goes through hl.highlightBy, which also shades the speed chart and
  // shows stats for the stretch.
  const hl = map || { highlightBy() {}, highlight(t0, t1, f) { this.highlightBy(0, t0, t1, f); }, mark() {} };
  let sel = null, speedChart = null;
  const mapHighlightBy = hl.highlightBy.bind(hl);
  const idxRange = (col, lo, hi) => { let i0 = -1, i1 = -1; ser.forEach((r, j) => { if (r[col] >= lo && r[col] <= hi) { if (i0 < 0) i0 = j; i1 = j; } }); return i0 < 0 ? null : [i0, i1]; };
  const paintSel = () => {
    const box = $("#speed-sel");
    if (!sel) { speedChart && speedChart.setBand(null); eleChart && eleChart.setBand(null); if (box) box.hidden = true; return; }
    const r = idxRange(sel.col, sel.lo, sel.hi);
    if (!r) return;
    const [i0, i1] = r, xi = xmode === "km" ? 1 : 0;
    speedChart && speedChart.setBand(ser[i0][xi], ser[i1][xi]);
    eleChart && eleChart.setBand(ser[i0][xi], ser[i1][xi]);
    let mv = 0, hrS = 0, hrT = 0;
    for (let j = i0; j < i1; j++) {
      const dt = ser[j + 1][0] - ser[j][0];
      if (dt <= 0) continue;
      if ((ser[j + 1][1] - ser[j][1]) * 1000 / dt >= 0.5) mv += dt;
      if (ser[j][3] && dt <= 60) { hrS += ser[j][3] * dt; hrT += dt; }
    }
    const km = ser[i1][1] - ser[i0][1], el = ser[i1][0] - ser[i0][0], v = mv ? km / (mv / 3600) : null;
    box.hidden = false;
    box.innerHTML = `<b>Selected</b> <span class="num">${f2(ser[i0][1])}–${f2(ser[i1][1])} km</span> · <span class="num">${dur(el, el >= 3600)}</span>` +
      (mv < el - 2 ? ` <span class="muted">(moving ${dur(mv, mv >= 3600)})</span>` : "") +
      ` · <span class="num">${f2(v)} km/h</span> · <span class="num">${pace(v)}/km</span>` + (hrT ? ` · HR <span class="num">${f0(hrS / hrT)}</span>` : "") +
      (ele ? (() => { const { net, g } = netGrade(i0, i1); return isNum(net) ? ` · elev <span class="num">${fnet(net)}</span>${isNum(g) ? ` <span class="muted">(${fgrade(g)})</span>` : ""}` : ""; })() : "") +
      (isNum(v) && isNum(avg) ? ` · <span class="${v >= avg ? "up" : "down"}">${pct((v / avg - 1) * 100)}</span> vs avg` : "") +
      ` <button class="btn small" id="sel-clear">✕ clear</button>`;
    $("#sel-clear").onclick = () => { clearRowSel(); hl.highlightBy(null); };
  };
  hl.highlightBy = (col, lo, hi, fit) => { sel = lo == null ? null : { col, lo, hi }; mapHighlightBy(col, lo, hi, fit); paintSel(); };
  const clearRowSel = () => { selKm = null; document.querySelectorAll("#t-splits tr.sel, #t-legs tr.sel, #t-laps tr.sel").forEach(tr => tr.classList.remove("sel")); };

  // Elevation for display: a 7-sample median removes dropouts (some FIT files
  // write 0 m for a sample), then a +-15 s mean takes out barometer jitter.
  // Summed climbing from a watch altimeter still drifts 2-10x above Strava's
  // terrain-model ascent outdoors, so totals use Strava's figure, and splits /
  // selections show net change and grade, which drift barely affects.
  const ele = (() => {
    if (!hasEle) return null;
    const raw = ser.map(r => r[4]), med = raw.map((_, j) => {
      const w = raw.slice(Math.max(0, j - 3), j + 4).filter(v => v != null).sort((x, y) => x - y);
      return w.length ? w[w.length >> 1] : null;
    });
    const out = new Array(ser.length).fill(null);
    let lo = 0, hi = 0, sum = 0, cnt = 0;
    for (let j = 0; j < ser.length; j++) {
      while (hi < ser.length && ser[hi][0] - ser[j][0] <= 15) { if (med[hi] != null) { sum += med[hi]; cnt++; } hi++; }
      while (ser[j][0] - ser[lo][0] > 15) { if (med[lo] != null) { sum -= med[lo]; cnt--; } lo++; }
      out[j] = cnt ? sum / cnt : null;
    }
    // bridge gaps between sparse readings (GT 6 Pro: one every ~5 s, some
    // longer holes) by linear interpolation in time, up to 3 minutes
    let prev = -1;
    for (let j = 0; j < out.length; j++) {
      if (out[j] == null) continue;
      if (prev >= 0 && j - prev > 1 && ser[j][0] - ser[prev][0] <= 180)
        for (let k = prev + 1; k < j; k++) out[k] = out[prev] + (out[j] - out[prev]) * (ser[k][0] - ser[prev][0]) / ((ser[j][0] - ser[prev][0]) || 1);
      prev = j;
    }
    return out;
  })();
  /** climb / descent over series indices, 4 m hysteresis; fallback when Strava has no ascent */
  const climb = (i0, i1) => {
    let up = 0, dn = 0, ref = null;
    for (let j = i0; j <= i1; j++) {
      const e = ele[j]; if (e == null) continue;
      if (ref == null) { ref = e; continue; }
      if (e - ref >= 4) { up += e - ref; ref = e; } else if (ref - e >= 4) { dn += ref - e; ref = e; }
    }
    return { up, dn };
  };
  /** net change and grade over series indices */
  const netGrade = (i0, i1) => {
    const dm = (ser[i1][1] - ser[i0][1]) * 1000;
    if (ele[i0] == null || ele[i1] == null) return { net: null, g: null };
    const net = ele[i1] - ele[i0];
    return { net, g: dm > 100 ? net / dm * 100 : null };
  };
  const fnet = v => isNum(v) ? (v >= 0 ? "+" : "−") + Math.abs(v).toFixed(0) + " m" : "";
  const fgrade = g => isNum(g) ? (g >= 0 ? "+" : "") + g.toFixed(1) + "%" : "";
  /** grade % at index j, over +-50 m */
  const gradeAt = j => {
    if (!ele || ele[j] == null) return null;
    let a = j, b = j;
    while (a > 0 && ser[j][1] - ser[a][1] < 0.05) a--;
    while (b < ser.length - 1 && ser[b][1] - ser[j][1] < 0.05) b++;
    const dm = (ser[b][1] - ser[a][1]) * 1000;
    return dm > 30 && ele[a] != null && ele[b] != null ? (ele[b] - ele[a]) / dm * 100 : null;
  };
  /** km range [k0, k1] -> series index range */
  const kmIdx = (k0, k1) => { let i0 = 0, i1 = ser.length - 1; while (i0 < i1 && ser[i0][1] < k0) i0++; while (i1 > i0 && ser[i1][1] > k1) i1--; return [i0, i1]; };
  let eleChart = null;
  const drawEle = () => {
    if (!ele || !$("#c-ele")) return;
    const xi = xmode === "km" ? 1 : 0, xf = xmode === "km" ? v => v.toFixed(0) : v => dur(v);
    const vals = ele.filter(isNum), lo = Math.min(...vals), hi = Math.max(...vals);
    const pad = Math.max(5, (hi - lo) * 0.15);
    const asc = isNum(a.ascent_m) ? a.ascent_m : climb(0, ser.length - 1).up;
    $("#ele-sum").textContent = `${f0(lo)}–${f0(hi)} m · ascent +${f0(asc)} m${isNum(a.ascent_m) ? "" : " (from the watch, may be high)"}`;
    const idxOf = new Map(ser.map((r, j) => [r[xi], j]));
    eleChart = chart($("#c-ele"), {
      height: 150, legend: false, padRight: true,
      x: { type: "linear", fmt: xf }, y: { min: Math.floor(lo - pad), max: Math.ceil(hi + pad / 3), fmt: v => v.toFixed(0), label: "m", ticks: 3 },
      series: [
        { name: "elevation", kind: "area", color: "#8a94a3", opacity: .35, data: ser.map((r, j) => ({ x: r[xi], y: ele[j], ref: j })) },
        { name: "elevation", kind: "line", color: "#c9d1dc", width: 1.5, data: ser.map((r, j) => ({ x: r[xi], y: ele[j], ref: j })) },
      ],
      tip: x => { const j = idxOf.get(x); if (j == null) return ""; const r = ser[j], g = gradeAt(j); return `<b>${f2(r[1])} km · ${dur(r[0], true)}</b><div class="row"><span>elevation</span><span>${f0(ele[j])} m</span></div>` + (isNum(g) ? `<div class="row"><span>grade</span><span>${g >= 0 ? "+" : ""}${g.toFixed(1)}%</span></div>` : "") + `<div class="row"><span>speed</span><span>${f1(r[2])} km/h</span></div>`; },
      onHover: x => { map && map.mark(x == null ? null : idxOf.get(x)); speedChart && speedChart.cursor(x); },
      onClick: j => { const k = Math.floor(ser[j][1]) + 1; if ((a.splits || []).some(s => s.km === k)) selectKm(k); },
      onBrush: (x0, x1) => { clearRowSel(); hl.highlightBy(xi, x0, x1, false); },
    });
  };

  let xmode = "km";
  const drawSpeed = () => {
    const xi = xmode === "km" ? 1 : 0, xf = xmode === "km" ? v => v.toFixed(0) : v => dur(v);
    const pts = ser.map((r, j) => ({ x: r[xi], j, r }));
    const kmh = pts.map(p => p.r[2]).filter(isNum);
    const ymax = Math.max(5, (quantile(kmh, .995) || 30) * 1.1);
    const idxOf = new Map(pts.map(p => [p.x, p.j]));
    speedChart = chart($("#c-speed"), {
      onBrush: (x0, x1) => { clearRowSel(); hl.highlightBy(xmode === "km" ? 1 : 0, x0, x1, false); },
      height: 300, x: { type: "linear", fmt: xf }, y: { min: 0, max: ymax, fmt: v => v.toFixed(0), label: "km/h" },
      y2: hasHr ? { fmt: v => v.toFixed(0), label: "bpm" } : null, padRight: hasEle,
      series: [
        { name: "speed", kind: "line", color: "#3fb6ff", width: 1.5, data: pts.map(p => ({ x: p.x, y: Math.min(p.r[2], ymax), ref: p.j })) },
        ...(isNum(avg) ? [{ name: `avg ${f1(avg)}`, kind: "line", color: "#ff7a1a", width: 1, dash: "5 4", data: [{ x: pts[0].x, y: avg }, { x: pts[pts.length - 1].x, y: avg }] }] : []),
        ...(hasHr ? [{ name: "heart rate", kind: "line", color: "#ff4d4d", axis: "y2", width: 1.3, opacity: .85, data: pts.map(p => ({ x: p.x, y: p.r[3] || null, ref: p.j })) }] : []),
      ],
      tip: x => { const j = idxOf.get(x); if (j == null) return ""; const r = ser[j]; return `<b>${f2(r[1])} km · ${dur(r[0], true)}</b><div class="row"><span>speed</span><span>${f1(r[2])} km/h · ${pace(r[2])}/km</span></div>` + (r[3] ? `<div class="row"><span>HR</span><span>${r[3]} bpm</span></div>` : "") + (r[4] != null ? `<div class="row"><span>elevation</span><span>${f0(r[4])} m</span></div>` : ""); },
      onHover: x => { map && map.mark(x == null ? null : idxOf.get(x)); eleChart && eleChart.cursor(x); },
      onClick: j => { const k = Math.floor(ser[j][1]) + 1; if ((a.splits || []).some(s => s.km === k)) selectKm(k); },
    });
  };
  $("#xmode").querySelectorAll("button").forEach(b => b.onclick = () => { xmode = b.dataset.x; $("#xmode").querySelectorAll("button").forEach(z => z.classList.toggle("on", z === b)); drawSpeed(); drawEle(); paintSel(); });
  drawSpeed(); drawEle();

  if (L) lapWidgets(L, hl, hasHr);
  if (hasGps) sessionSegments(peers.find(p => p.id === a.id) || a, hl);

  // splits
  const sp = a.splits || [];
  if (sp.length) {
    const lo = quantile(sp.map(s => s.kmh), .1), hi = quantile(sp.map(s => s.kmh), .9);
    const col = v => SPEED_COLORS[Math.max(0, Math.min(SPEED_COLORS.length - 1, Math.floor((v - lo) / ((hi - lo) || 1) * (SPEED_COLORS.length - 1) + .5)))];
    const turnKm = m.route === "out-back" ? m.turn_km : null;
    const totKm = ser.length ? ser[ser.length - 1][1] : a.km;
    selectKm = k => {
      selKm = selKm === k ? null : k;
      const s = sp.find(s => s.km === selKm);
      s ? hl.highlightBy(1, s.km - 1, s === sp[sp.length - 1] ? totKm : s.km, false) : hl.highlightBy(1, null);
      $("#t-splits").querySelectorAll("tr[data-km]").forEach(tr => tr.classList.toggle("sel", +tr.dataset.km === selKm));
      $("#t-legs")?.querySelectorAll("tr[data-leg]").forEach(tr => tr.classList.remove("sel"));
    };
    chart($("#c-splits"), {
      onClick: k => selectKm(k),
      x: { type: "linear", fmt: v => v.toFixed(0) }, y: { min: Math.max(0, Math.floor(Math.min(...sp.map(s => s.kmh)) - 2)), fmt: v => v.toFixed(0), label: "km/h" }, legend: false,
      refY: avg,
      series: [{ name: "km/h", kind: "bar", color: "#3fb6ff", data: sp.map(s => ({ x: s.km, y: s.kmh, ref: s.km, color: col(s.kmh) })) }],
      tip: x => { const s = sp.find(s => s.km === x); return s ? `<b>km ${s.km}${turnKm != null ? (s.km <= turnKm ? " · out" : " · back") : ""}</b><div class="row"><span>speed</span><span>${f2(s.kmh)} km/h</span></div><div class="row"><span>pace</span><span>${dur(s.moving_s)} /km</span></div>${s.pause_s > 0 ? `<div class="row"><span>paused</span><span>${dur(s.pause_s)}</span></div>` : ""}${s.hr ? `<div class="row"><span>HR</span><span>${f0(s.hr)}</span></div>` : ""}` : ""; }
    });
    const fastest = Math.max(...sp.map(s => s.kmh));
    let cum = 0;
    $("#t-splits").innerHTML = `<table><thead><tr><th>km</th><th>pace</th><th>km/h</th><th class="l"></th><th>Δ avg</th><th>paused</th><th>HR</th>${ele ? "<th>elev Δ</th><th>grade</th>" : ""}<th>cumulative</th></tr></thead><tbody>` +
      sp.map(s => { cum += s.elapsed_s || 0; return `<tr class="click" data-km="${s.km}"><td>${s.km}${turnKm != null && s.km === Math.ceil(turnKm) ? " ↩" : ""}</td><td>${dur(s.moving_s)}</td><td>${f2(s.kmh)}</td>
        <td class="l"><span class="bar" style="width:${(s.kmh / fastest * 90).toFixed(0)}px;background:${col(s.kmh)}"></span></td>
        <td class="${s.kmh >= avg ? "up" : "down"}">${isNum(avg) ? (s.kmh - avg >= 0 ? "+" : "") + (s.kmh - avg).toFixed(1) : ""}</td>
        <td>${s.pause_s > 0.5 ? dur(s.pause_s) : ""}</td><td>${f0(s.hr)}</td>${ele ? (() => { const [i0, i1] = kmIdx(s.km - 1, s.km), { net, g } = netGrade(i0, i1);
          return `<td>${Math.abs(net) >= 1 ? fnet(net) : ""}</td><td class="${isNum(g) && Math.abs(g) >= 3 ? (g > 0 ? "down" : "up") : ""}">${fgrade(g)}</td>`; })() : ""}<td>${dur(cum, true)}</td></tr>`; }).join("") + `</tbody></table>
      <div class="note">Last row may be a partial kilometre. ↩ marks the turnaround. Click a km (here, in the bars or in the speed chart) to show it on the map; click again to clear.</div>`;
    $("#t-splits").querySelectorAll("tr[data-km]").forEach(tr => tr.onclick = () => selectKm(+tr.dataset.km));
  } else {
    $("#c-splits").innerHTML = $("#t-splits").innerHTML = `<p class="muted">No per-km splits (no distance channel).</p>`;
  }

  if (m.route === "out-back") {
    $("#t-legs").innerHTML = `<table><thead><tr><th>leg</th><th>km</th><th>moving</th><th>avg km/h</th><th>pace</th><th>best 10 min</th></tr></thead><tbody>
      <tr class="click" data-leg="out"><td>Out (${f0(m.bearing_out)}°)</td><td>${f2(m.out_km)}</td><td>${dur(m.out_moving_s, true)}</td><td>${f2(m.out_kmh)}</td><td>${pace(m.out_kmh)}</td><td>${f2(m.out_b10)}</td></tr>
      <tr class="click" data-leg="back"><td>Back</td><td>${f2(m.back_km)}</td><td>${dur(m.back_moving_s, true)}</td><td>${f2(m.back_kmh)}</td><td>${pace(m.back_kmh)}</td><td>${f2(m.back_b10)}</td></tr>
      <tr><td><b>Difference</b></td><td></td><td></td><td>${pct(m.asym_pct)}</td><td></td><td>neutral <b>${f2(m.wind_neutral)}</b></td></tr></tbody></table>`;
  }
  if (m.route === "out-back") {
    let selLeg = null;
    $("#t-legs").querySelectorAll("tr[data-leg]").forEach(tr => tr.onclick = () => {
      selLeg = selLeg === tr.dataset.leg ? null : tr.dataset.leg;
      if (selKm != null) selectKm(selKm);  // clear km selection
      const end = ser.length ? ser[ser.length - 1][1] : a.km;
      selLeg ? hl.highlightBy(1, selLeg === "out" ? 0 : m.turn_km, selLeg === "out" ? m.turn_km : end, false) : hl.highlightBy(1, null);
      $("#t-legs").querySelectorAll("tr[data-leg]").forEach(r => r.classList.toggle("sel", r.dataset.leg === selLeg));
    });
  }
  if (hasHr) {
    const z = zoneSecs(m.hr_hist, S.hrmax), tot = z.reduce((x, y) => x + y, 0) || 1;
    $("#t-zones").innerHTML = `<table><tbody>` + z.map((s, k) => `<tr><td class="l">${zoneLabel(k, S.hrmax)}</td><td>${dur(s, true)}</td><td>${(s / tot * 100).toFixed(0)}%</td>
      <td class="l"><span class="bar" style="width:${(s / tot * 240).toFixed(0)}px;background:${ZONES[k].c}"></span></td></tr>`).join("") + `</tbody></table>`;
  }
  // similar route: same kind, similar distance from start to farthest point and outbound bearing
  const same = peers.filter(p => p.id !== a.id && p.route === m.route && m.route && m.route !== "indoor" &&
    isNum(p.crow_km) && isNum(m.crow_km) && Math.abs(p.crow_km - m.crow_km) < Math.max(0.6, m.crow_km * 0.12) &&
    (m.route !== "out-back" || Math.abs(((p.bearing_out - m.bearing_out + 540) % 360) - 180) < 20) &&
    Math.abs((p.km || 0) - (a.km || 0)) < Math.max(2, a.km * 0.2));
  const metric = m.route === "out-back" ? "wind_neutral" : "best10min_kmh";
  const rank = same.filter(p => isNum(p[metric])).filter(p => p[metric] > (m[metric] ?? a[metric])).length + 1;
  $("#t-same").innerHTML = same.length ? `<div class="note" style="margin:0 0 8px">${same.length} similar sessions. This one ranks <b>#${rank}</b> of ${same.filter(p => isNum(p[metric])).length + 1} by ${metric === "wind_neutral" ? "wind-neutral" : "best 10-min"} speed.</div>
    <table><thead><tr><th>date</th><th>km</th><th>moving</th><th>avg km/h</th><th>best 10</th><th>wind-neutral</th><th>out vs back</th><th>HR</th></tr></thead><tbody>` +
    same.slice().sort((x, y) => y._t - x._t).slice(0, 25).map(p => `<tr class="click" data-id="${p.id}"><td class="l">${fdate(p._d)}</td><td>${fkm(p.km)}</td><td>${dur(p.moving_s, true)}</td><td>${f2(p.kmh_moving)}</td><td>${f2(p.best10min_kmh)}</td><td>${f2(p.wind_neutral)}</td><td>${pct(p.asym_pct)}</td><td>${f0(p.hr_avg)}</td></tr>`).join("") + `</tbody></table>` : `<p class="muted">No other sessions on a matching route.</p>`;
  app.querySelectorAll("#t-same tr[data-id]").forEach(tr => tr.onclick = () => go(tr.dataset.id));
}

// ------------------------------------------------------------------ router / boot

let lastRoute = null;
// ------------------------------------------------------------------ history
// The app window has no browser chrome, so Rollbook draws its own back /
// forward buttons. Every entry is numbered in history.state ({i, y}) so the
// buttons know whether there is anywhere to go, and y restores the scroll
// position when coming back to a page.
const Nav = { i: 0, max: 0, pop: false };
function navInit() {
  if (!history.state || history.state.i == null) history.replaceState({ i: 0 }, "");
  Nav.i = history.state.i;
  Nav.max = Math.max(Nav.i, +(sessionStorage.navMax || 0));
  // runs before route(): a hashchange without our state is a new navigation
  addEventListener("hashchange", () => {
    const st = history.state;
    if (st && st.i != null) { Nav.i = st.i; Nav.pop = true; }
    else { Nav.i++; Nav.max = Nav.i; Nav.pop = false; history.replaceState({ i: Nav.i }, ""); }
    sessionStorage.navMax = Nav.max;
    navButtons();
  });
  let t;
  addEventListener("scroll", () => { clearTimeout(t); t = setTimeout(() => history.replaceState({ ...history.state, y: scrollY }, ""), 150); }, { passive: true });
  $("#nav-back").onclick = () => Nav.i > 0 && history.back();
  $("#nav-fwd").onclick = () => Nav.i < Nav.max && history.forward();
  const typing = e => /^(INPUT|TEXTAREA|SELECT)$/.test(e.target.tagName) || e.target.isContentEditable;
  addEventListener("keydown", e => {
    if (typing(e) || $("#import-dlg").open) return;
    const back = (e.altKey && e.key === "ArrowLeft") || (e.metaKey && e.key === "[");
    const fwd = (e.altKey && e.key === "ArrowRight") || (e.metaKey && e.key === "]");
    if (back || fwd) { e.preventDefault(); back ? $("#nav-back").click() : $("#nav-fwd").click(); }
  });
  // mouse side buttons (pywebview doesn't map them to history itself)
  addEventListener("mouseup", e => { if (e.button === 3) { e.preventDefault(); $("#nav-back").click(); } if (e.button === 4) { e.preventDefault(); $("#nav-fwd").click(); } });
  navButtons();
}
function navButtons() {
  $("#nav-back").disabled = Nav.i <= 0;
  $("#nav-fwd").disabled = Nav.i >= Nav.max;
}

async function route() {
  const h = location.hash.replace(/^#\/?/, "");
  const [page, arg] = h.split("/");
  document.querySelectorAll("nav a").forEach(a => a.classList.toggle("on", a.dataset.nav === (page === "session" ? "sessions" : page === "segment" ? "segments" : page || "progress")));
  hideTip();
  try {
    if (page === "session" && /^\d+$/.test(arg || "")) await viewSession(arg);
    else if (page === "sessions") await viewSessions();
    else if (page === "map") await viewMap();
    else if (page === "segments") await viewSegments();
    else if (page === "segment" && arg) await viewSegment(arg);
    else await viewProgress();
    const y = history.state && history.state.y;
    if (Nav.pop && y) requestAnimationFrame(() => scrollTo(0, y));  // back/forward: where you were
    else if (lastRoute !== h) scrollTo(0, 0);
    Nav.pop = false;
    lastRoute = h;
  } catch (e) {
    $("#app").innerHTML = `<p class="down">Error: ${esc(e.message)}</p>`;
    console.error(e);
  }
}

async function pollStatus() {
  try {
    const s = await api("status");
    $("#status").textContent = s.running ? `computing metrics ${s.done}/${s.total}…` : "";
    if (s.running) setTimeout(pollStatus, 1500);
  } catch { /* server gone */ }
}

// ------------------------------------------------------------------ import

const Imp = { files: [], busy: false };
const fsize = n => n > 1e9 ? (n / 1e9).toFixed(1) + " GB" : n > 1e6 ? (n / 1e6).toFixed(0) + " MB" : (n / 1e3).toFixed(0) + " kB";
const kindOf = f => /\.zip$/i.test(f.name) ? "export" : /\.(fit|gpx|tcx)(\.gz)?$/i.test(f.name) ? "activity" : null;

function openImport(files) {
  const dlg = $("#import-dlg");
  if (!dlg.open) dlg.showModal();
  if (!Imp.busy) {
    $("#imp-types").innerHTML = S.types.map(t => `<option value="${esc(t.type)}">`).join("");
    if (!$("#imp-sport").value) $("#imp-sport").value = S.type || "";
  }
  if (files && files.length && !Imp.busy) addImportFiles(files);
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
  $("#imp-prog").hidden = false; log.textContent = ""; phase.textContent = ""; bar.style.width = "0"; drawImportList(state);
  const total = files.reduce((s, f) => s + f.size, 0) || 1;
  let sent = 0, ok = 0;
  try {
    for (const f of files) {
      const i = Imp.files.indexOf(f);
      state[i] = "uploading…"; drawImportList(state);
      try {
        const r = await upload(f, sport, p => { bar.style.width = ((sent + p * f.size) / total * 30).toFixed(1) + "%"; phase.textContent = `Uploading ${f.name} · ${(p * 100).toFixed(0)}%`; });
        state[i] = r.duplicate ? "file already stored, checking" : r.kind === "export" ? `ok · ${r.files} files` : r.kind === "activities" ? `ok · ${r.files} activity files` : "ok"; ok++;
      } catch (e) { state[i] = "✕ " + e.message; }
      sent += f.size; drawImportList(state);
    }
    if (!ok) throw new Error("nothing could be uploaded");
    const r = await fetch("/api/import/run", { method: "POST", headers: { "X-Rollbook": "1" } });
    if (!r.ok) throw new Error((await r.json()).error || r.status);
    let j;
    for (;;) {
      j = await api("import");
      const frac = j.total ? j.done / j.total : 0;
      bar.style.width = (30 + (j.phase && j.phase.startsWith("importing") ? frac * 55 : j.state === "running" ? 88 : 100)).toFixed(1) + "%";
      phase.textContent = `${j.phase || ""}${j.total ? ` · ${j.done}/${j.total}` : ""}`;
      log.textContent = (j.log || []).join("\n"); log.scrollTop = log.scrollHeight;
      if (j.state !== "running") break;
      await sleep(800);
    }
    if (j.state === "error") throw new Error(j.error);
    bar.style.width = "100%";
    const dups = (j.log || []).filter(l => /skipped, same session/.test(l)).length;
    phase.innerHTML = (j.added ? `<span class="up">✓ ${j.added} new activit${j.added === 1 ? "y" : "ies"} imported.</span>` : `<span class="muted">✓ Done, nothing new: everything was already imported.</span>`) +
      (dups ? ` <span class="muted">${dups} already in Rollbook from another file (same start time and distance), skipped.</span>` : "");
    Imp.files = [];
    if (j.added) await refreshData();
  } catch (e) {
    phase.innerHTML = `<span class="down">✕ ${esc(e.message)}</span>`;
  } finally {
    Imp.busy = false; drawImportList(state);
  }
}
/** Drop every client cache and redraw, after the DB changed underneath us. */
async function refreshData() {
  S.acts = {}; S.tracks = {}; S.segs = {}; S.mapView = {};
  [S.types, { hrmax: S.hrmax }, S.favs] = await Promise.all([api("types"), api("hrmax"), api("favorites")]);
  S.hrmax = S.hrmax || 185;
  const sel = $("#type"), cur = S.type;
  sel.innerHTML = S.types.map(t => `<option value="${esc(t.type)}">${esc(t.type)} (${t.n})</option>`).join("");
  sel.value = S.types.some(t => t.type === cur) ? cur : S.types[0]?.type;
  S.type = sel.value;
  pollStatus();
  route();
}
function initImport() {
  $("#import-btn").onclick = () => openImport();
  $("#imp-close").onclick = () => $("#import-dlg").close();
  $("#imp-file").onchange = e => { addImportFiles(e.target.files); e.target.value = ""; };
  $("#imp-go").onclick = runImport;
  $("#import-dlg").addEventListener("cancel", e => { if (Imp.busy) e.preventDefault(); });
  // drag files anywhere onto the page
  let depth = 0;
  const hasFiles = e => [...(e.dataTransfer?.types || [])].includes("Files");
  addEventListener("dragenter", e => { if (!hasFiles(e)) return; e.preventDefault(); if (++depth === 1 && !$("#import-dlg").open) document.body.classList.add("dragging"); });
  addEventListener("dragleave", e => { if (!hasFiles(e)) return; if (--depth <= 0) { depth = 0; document.body.classList.remove("dragging"); } });
  addEventListener("dragover", e => { if (hasFiles(e)) e.preventDefault(); });
  addEventListener("drop", e => { if (!hasFiles(e)) return; e.preventDefault(); depth = 0; document.body.classList.remove("dragging"); openImport(e.dataTransfer.files); });
}

// In the desktop app the server stops once its window is gone. pagehide also
// fires on reload, so the launcher waits a few seconds for a new page.
if (new URLSearchParams(location.search).has("app"))
  addEventListener("pagehide", () => navigator.sendBeacon("/api/bye"));

async function boot() {
  [S.types, { hrmax: S.hrmax }, S.favs] = await Promise.all([api("types"), api("hrmax"), api("favorites")]);
  S.hrmax = S.hrmax || 185;
  if (!S.types.some(t => t.type === S.type)) S.type = S.types[0]?.type;
  const sel = $("#type");
  sel.innerHTML = S.types.map(t => `<option value="${esc(t.type)}">${esc(t.type)} (${t.n})</option>`).join("");
  sel.value = S.type;
  sel.onchange = () => { S.type = localStorage.type = sel.value; if (location.hash.startsWith("#/session/")) location.hash = "#/"; else if (location.hash.startsWith("#/segment/")) location.hash = "#/segments"; else route(); };
  const rg = $("#range"); rg.value = S.range;
  rg.onchange = () => { S.range = localStorage.range = rg.value; if (!location.hash.startsWith("#/session/")) route(); };
  navInit();
  addEventListener("hashchange", route);
  let rt; addEventListener("resize", () => { clearTimeout(rt); rt = setTimeout(() => { if (!/^#\/(map|segment)/.test(location.hash)) route(); }, 200); });
  initImport();
  pollStatus();
  route();
}
boot();
