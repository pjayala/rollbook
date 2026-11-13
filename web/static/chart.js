// SPDX-License-Identifier: GPL-3.0-or-later
// Copyright (C) 2026 Pedro Ayala. Rollbook is free software, see LICENSE.
"use strict";
/* The SVG chart engine (lines, areas, bars, dots, brushing, bands) every page draws with. */

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
 *  opt.x     {type:'time'|'linear', fmt, label, min, max, ticks: [values] | plotWidth => [values] (else automatic)}
 *  opt.y     {fmt, label, min, max, zero}     opt.y2 same, optional
 *  opt.series[] {name, color, kind:'line'|'dot'|'bar'|'area', data:[{x,y,ref}], axis:'y'|'y2', stack, width, r, opacity, legend:false}
 *  opt.tip(x, hits) -> html   opt.onClick(ref)   opt.onHover(x|null)
 *  opt.marks[] {x, y, label}   events: a 1px leader from a label row above the plot to (x, y)
 *  opt.sides {hi, lo}          captions inside the plot, top-left and bottom-left (a signed axis)
 *  series.base                 area fill to this y instead of the series minimum
 *  opt.hlines[] {y, label, labelY}  dashed reference lines; the label sits at labelY (default y), left
 *  opt.padRight                keep the right margin without a y2 axis (charts stacked on one x)
 */
function chart(host, opt, heightOverride) {
  host.innerHTML = "";
  // remember how to redraw at another size (expanded panel view)
  host._chartOpt = opt;
  host._redraw = h => { chart(host, host._chartOpt, h); const b = host._facade && host._facade._band; if (b && host._impl) host._impl.setBand(...b); };
  ensureExpander(host);
  const H = heightOverride || opt.height || 260, W = Math.max(320, host.clientWidth || 600);
  const m = { l: 46, r: opt.y2 || opt.padRight ? 46 : 14, t: 18, b: 24 };
  const iw = W - m.l - m.r;
  let ih = H - m.t - m.b;
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
  if (!ser.length) { el("text", { x: W / 2, y: H / 2, "text-anchor": "middle", fill: PAL.grey }, svg).textContent = "no data"; return; }

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
  // annotation labels: greedy rows above the plot so no two overlap, reserved only when a label exists
  const marks = (opt.marks || []).filter(k => isNum(k.x) && k.x >= xlo && k.x <= xhi && k.label).map(k => ({ ...k })).sort((p, q) => p.x - q.x);
  if (marks.length) {
    const ends = [];
    for (const k of marks) {
      const w = k.label.length * 6.3 + 10, x0 = Math.max(4, Math.min(W - m.r - w, X(k.x) - 3));
      let r = ends.findIndex(e => e < x0);
      if (r < 0) { r = ends.length; ends.push(0); }
      ends[r] = x0 + w; k.row = r; k.lx = x0;
    }
    m.t = 8 + ends.length * 16 + 6; ih = H - m.t - m.b;
    for (const k of marks) k.ly = 8 + k.row * 16 + 11;
  }
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
  const xtk = typeof opt.x.ticks === "function" ? opt.x.ticks(iw) : opt.x.ticks;
  const xt = xtk ? xtk.filter(v => v >= xlo && v <= xhi).map(v => ({ v, l: (opt.x.fmt || String)(v) }))
    : opt.x.type === "time" ? timeTicks(xlo, xhi) : niceTicks(xlo, xhi, Math.max(3, Math.floor(iw / 80))).ticks.filter(v => v >= xlo && v <= xhi).map(v => ({ v, l: (opt.x.fmt || String)(v) }));
  for (const t of xt) {
    el("text", { x: X(t.v), y: H - 6, "text-anchor": "middle" }, ax).textContent = t.l;
  }
  if (opt.y.label) el("text", { x: 4, y: 10 }, ax).textContent = opt.y.label;
  if (opt.y2 && opt.y2.label) el("text", { x: W - 4, y: 10, "text-anchor": "end" }, ax).textContent = opt.y2.label;
  if (opt.refY != null && Y.y) el("line", { x1: m.l, x2: m.l + iw, y1: Y.y.f(opt.refY), y2: Y.y.f(opt.refY), ...(opt.zeroLine ? { stroke: "#4a4a4a", "stroke-width": 1 } : { stroke: PAL.grey, "stroke-dasharray": "3 3" }) }, svg);
  if (opt.sides && Y.y) {
    if (opt.sides.hi) el("text", { x: m.l + 8, y: m.t + 14, class: "side" }, ax).textContent = opt.sides.hi;
    if (opt.sides.lo) el("text", { x: m.l + 8, y: m.t + ih - 8, class: "side" }, ax).textContent = opt.sides.lo;
  }

  // labelled reference lines (opt.hlines [{y, label}]), e.g. air-quality bands
  if (Y.y) for (const r of opt.hlines || []) {
    const y = Y.y.f(r.y);
    el("line", { x1: m.l, x2: m.l + iw, y1: y, y2: y, stroke: "#4a4a4a", "stroke-dasharray": "3 3" }, svg);
    if (r.label) el("text", { x: m.l + 6, y: Y.y.f(r.labelY ?? r.y) + 4, class: "side" }, ax).textContent = r.label;
  }

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
        const base = sc.f(s.base ?? Math.max(sc.cfg.min ?? -Infinity, Math.min(...pts.map(p => p.y1))));
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

  // annotations: leader from the label row down to the point
  if (marks.length && Y.y) {
    const mg = el("g", { class: "marks" }, svg);
    for (const k of marks) {
      const x = X(k.x), y = isNum(k.y) ? Y.y.f(k.y) : m.t + ih;
      el("line", { x1: x, x2: x, y1: k.ly + 4, y2: y, stroke: "#6a6a6a", "stroke-width": 1 }, mg);
      el("circle", { cx: x, cy: y, r: 2.5, fill: k.color || PAL.ink }, mg);
      el("text", { x: k.lx, y: k.ly, class: "mark" }, mg).textContent = k.label;
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
  // callers keep the returned object; it delegates to the latest render so
  // it stays valid after an expand/collapse redraw
  host._impl = { X, svg, setBand, cursor };
  if (!host._facade) host._facade = {
    setBand(...a) { host._facade._band = a[0] == null ? null : a; host._impl.setBand(...a); },
    cursor: x => host._impl.cursor(x),
    get svg() { return host._impl.svg; }, get X() { return host._impl.X; },
  };
  return host._facade;
}
