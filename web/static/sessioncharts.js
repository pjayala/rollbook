// SPDX-License-Identifier: GPL-3.0-or-later
// Copyright (C) 2026 Pedro Ayala. Rollbook is free software, see LICENSE.
"use strict";
/* The session page's linked charts: speed (+ HR), power and elevation, on a
 * distance or time axis. Hovering one moves the cursor on the others and the
 * marker on the map; brushing selects a stretch; clicking selects that km. */

/** Draw the three charts into P.charts and wire the distance / time toggle. */
function sessionCharts(P) {
  const { a, m, ser } = P;
  const xi = () => P.xmode === "km" ? 1 : 0;
  const xf = () => P.xmode === "km" ? v => v.toFixed(0) : v => dur(v);
  const others = (self, x) => Object.entries(P.charts).forEach(([k, c]) => { if (k !== self && c) c.cursor(x); });
  // shared by the three: map marker + the other charts' cursors, km click, brush
  const link = (self, idxOf) => ({
    onHover: x => { P.map && P.map.mark(x == null ? null : idxOf.get(x)); others(self, x); },
    onClick: j => { const k = Math.floor(ser[j][1]) + 1; if ((a.splits || []).some(s => s.km === k)) P.selectKm(k); },
    onBrush: (x0, x1) => { P.clearRowSel(); P.hl.highlightBy(xi(), x0, x1, false); },
  });
  const head = r => `<b>${f2(r[1])} km · ${dur(r[0], true)}</b>`;
  const row = (k, v) => `<div class="row"><span>${k}</span><span>${v}</span></div>`;

  const drawSpeed = () => {
    const x = xi(), pts = ser.map((r, j) => ({ x: r[x], j, r }));
    const kmh = pts.map(p => p.r[2]).filter(isNum), avg = P.avg;
    const ymax = Math.max(5, (quantile(kmh, .995) || 30) * 1.1);
    const idxOf = new Map(pts.map(p => [p.x, p.j]));
    P.charts.speed = chart($("#c-speed"), {
      ...link("speed", idxOf),
      height: 300, x: { type: "linear", fmt: xf() }, y: { min: 0, max: ymax, fmt: v => v.toFixed(0), label: "km/h" },
      y2: P.hasHr ? { fmt: v => v.toFixed(0), label: "bpm" } : null, padRight: P.hasEle,
      series: [
        { name: "speed", kind: "line", color: PAL.hue, width: 1.5, data: pts.map(p => ({ x: p.x, y: Math.min(p.r[2], ymax), ref: p.j })) },
        ...(isNum(avg) ? [{ name: `avg ${f1(avg)}`, kind: "line", color: PAL.ink, width: 1, dash: "5 4", data: [{ x: pts[0].x, y: avg }, { x: pts[pts.length - 1].x, y: avg }] }] : []),
        ...(P.hasHr ? [{ name: "heart rate", kind: "line", color: PAL.hr, axis: "y2", width: 1.3, opacity: .85, data: pts.map(p => ({ x: p.x, y: p.r[3] || null, ref: p.j })) }] : []),
      ],
      tip: x => { const j = idxOf.get(x); if (j == null) return ""; const r = ser[j]; return head(r) + row("speed", `${f1(r[2])} km/h · ${pace(r[2])}/km`) + (r[3] ? row("HR", `${f0(r[3])} bpm`) : "") + (r[7] != null ? row("power", `${f0(r[7])} W`) : "") + (r[4] != null ? row("elevation", `${f0(r[4])} m`) : ""); },
    });
  };

  const drawPw = () => {
    if (!P.hasPw || !$("#c-pw")) return;
    const x = xi(), W = P.W, ftp = P.ftp;
    const vals = ser.map(r => r[7]).filter(isNum), top = Math.max(50, (quantile(vals, .995) || 300) * 1.1);
    $("#pw-sum").textContent = `30 s mean · avg ${f0(W)} W` + (isNum(m.np) ? ` · weighted ${f0(m.np)} W` : "") + (ftp ? ` · FTP ${f0(ftp)} W` : "");
    const idxOf = new Map(ser.map((r, j) => [r[x], j]));
    const x0 = ser[0][x], x1 = ser[ser.length - 1][x];
    const data = ser.map((r, j) => ({ x: r[x], y: r[7], ref: j }));
    P.charts.pw = chart($("#c-pw"), {
      ...link("pw", idxOf),
      height: 170, padRight: true,
      x: { type: "linear", fmt: xf() }, y: { min: 0, max: top, fmt: v => v.toFixed(0), label: "W", ticks: 4 },
      series: [
        { name: "power", kind: "area", color: PAL.hue, opacity: .14, legend: false, data },
        { name: "power", kind: "line", color: PAL.hue, width: 1.4, data },
        ...(isNum(W) ? [{ name: `avg ${f0(W)}`, kind: "line", color: PAL.ink, width: 1, dash: "5 4", data: [{ x: x0, y: W }, { x: x1, y: W }] }] : []),
        ...(ftp ? [{ name: `FTP ${f0(ftp)}`, kind: "line", color: PAL.grey, width: 1, dash: "2 3", data: [{ x: x0, y: ftp }, { x: x1, y: ftp }] }] : []),
      ],
      tip: x => { const j = idxOf.get(x); if (j == null) return ""; const r = ser[j]; return head(r) + row("power", `${f0(r[7])} W${ftp && r[7] != null ? ` · ${f0(r[7] / ftp * 100)}% FTP` : ""}`) + row("speed", `${f1(r[2])} km/h`) + (r[3] ? row("HR", `${f0(r[3])} bpm`) : ""); },
    });
  };

  const drawEle = () => {
    const ele = P.ele;
    if (!ele || !$("#c-ele")) return;
    const x = xi(), vals = ele.filter(isNum), lo = Math.min(...vals), hi = Math.max(...vals);
    const pad = Math.max(5, (hi - lo) * 0.15);
    const asc = isNum(a.ascent_m) ? a.ascent_m : eleClimb(ele, 0, ser.length - 1).up;
    $("#ele-sum").textContent = `${f0(lo)}–${f0(hi)} m · ascent +${f0(asc)} m${isNum(a.ascent_m) ? "" : " (from the watch, may be high)"}`;
    const idxOf = new Map(ser.map((r, j) => [r[x], j]));
    const data = ser.map((r, j) => ({ x: r[x], y: ele[j], ref: j }));
    P.charts.ele = chart($("#c-ele"), {
      ...link("ele", idxOf),
      height: 150, legend: false, padRight: true,
      x: { type: "linear", fmt: xf() }, y: { min: Math.floor(lo - pad), max: Math.ceil(hi + pad / 3), fmt: v => v.toFixed(0), label: "m", ticks: 3 },
      series: [
        { name: "elevation", kind: "area", color: PAL.grey, opacity: .22, data },
        { name: "elevation", kind: "line", color: PAL.soft, width: 1.5, data },
      ],
      tip: x => { const j = idxOf.get(x); if (j == null) return ""; const r = ser[j], g = eleGradeAt(ser, ele, j); return head(r) + row("elevation", `${f0(ele[j])} m`) + (isNum(g) ? row("grade", fgrade(g)) : "") + row("speed", `${f1(r[2])} km/h`); },
    });
  };

  const drawAll = () => { drawSpeed(); drawPw(); drawEle(); };
  const seg = $("#xmode");
  seg.querySelectorAll("button").forEach(b => b.onclick = () => {
    P.xmode = b.dataset.x;
    seg.querySelectorAll("button").forEach(z => z.classList.toggle("on", z === b));
    drawAll(); P.paintSel();
  });
  drawAll();
}
