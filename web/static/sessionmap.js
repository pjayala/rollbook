// SPDX-License-Identifier: GPL-3.0-or-later
// Copyright (C) 2026 Pedro Ayala. Rollbook is free software, see LICENSE.
"use strict";
/* A session's map: tiles plus the speed-coloured track, highlight and hover marker. */

// ------------------------------------------------------------------ session map (tiles + speed-coloured track)

// slow → fast: grey warming into the hue, then lighter (reads on dark and light tiles)
const SPEED_COLORS = ["#6e6e6e", "#8f7563", "#b07a52", "#d27a3a", "#ff7a1a", "#ffa05a", "#ffcb9a"];
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
      dot(S[0], PAL.start, 7); dot(S[S.length - 1], PAL.end, 6);
      if (turnJ != null) dot(S[turnJ], "#fff", 6);
      if (range) {
        ctx.beginPath(); let pen = false;
        pts.forEach((p, j) => { const v = series[p.i][range.col]; if (v < range.lo || v > range.hi) { pen = false; return; } pen ? ctx.lineTo(...S[j]) : ctx.moveTo(...S[j]); pen = true; });
        ctx.strokeStyle = "#000"; ctx.lineWidth = 9; ctx.stroke(); ctx.strokeStyle = "#fff"; ctx.lineWidth = 5; ctx.stroke();
      }
      if (gate) {
        const [x, y] = mp.toScreen(gate[0], gate[1]);
        ctx.beginPath(); ctx.moveTo(x, y - 9); ctx.lineTo(x + 9, y); ctx.lineTo(x, y + 9); ctx.lineTo(x - 9, y); ctx.closePath();
        ctx.fillStyle = PAL.ink; ctx.fill(); ctx.lineWidth = 2; ctx.strokeStyle = "#000"; ctx.stroke();
      }
      if (opt.wind && isNum(opt.wind.wd) && isNum(opt.wind.ws)) {  // wind compass, top-left: arrow points where it blows to
        const cx = 34, cy = 34, a = (opt.wind.wd + 180) * Math.PI / 180, L = 16;
        ctx.save(); ctx.fillStyle = "#000000c8"; ctx.beginPath(); ctx.arc(cx, cy, 26, 0, 7); ctx.fill();
        ctx.translate(cx, cy); ctx.rotate(a); ctx.strokeStyle = PAL.ink; ctx.fillStyle = PAL.ink; ctx.lineWidth = 3;
        ctx.beginPath(); ctx.moveTo(0, L); ctx.lineTo(0, -L + 4); ctx.stroke();
        ctx.beginPath(); ctx.moveTo(0, -L - 2); ctx.lineTo(-6, -L + 7); ctx.lineTo(6, -L + 7); ctx.closePath(); ctx.fill(); ctx.restore();
        ctx.fillStyle = PAL.ink; ctx.font = "500 12px Inter, system-ui, sans-serif"; ctx.textAlign = "center"; ctx.fillText(`${Math.round(opt.wind.ws)} km/h`, cx, cy + 40);
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
    `<span>km/h</span><span><i style="background:${PAL.start};border-radius:50%"></i>start</span><span><i style="background:${PAL.end};border-radius:50%"></i>end</span>` +
    (turnJ != null ? `<span><i style="background:#fff;border-radius:50%"></i>turnaround</span>` : "") +
    (gate ? `<span><i style="background:${PAL.ink};transform:rotate(45deg)"></i>lap gate</span>` : "");
  host.appendChild(leg);
  return {
    destroy() { map.destroy(); },
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
