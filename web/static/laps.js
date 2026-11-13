// SPDX-License-Identifier: GPL-3.0-or-later
// Copyright (C) 2026 Pedro Ayala. Rollbook is free software, see LICENSE.
"use strict";
/* A session's laps panel: lap cards, lap chart and table. */

/** Laps that count for stats: regular and not dominated by a stop. */
const goodLaps = L => L.laps.filter(x => x.counted);

/** The laps panel of session page P (P.L); a lap selects through P.pick like every table. */
function lapWidgets(P) {
  const L = P.L, hasHr = P.hasHr;
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
  const col = x => !good.includes(x) ? PAL.na : SPEED_COLORS[Math.max(0, Math.min(SPEED_COLORS.length - 1, Math.floor((x.kmh - lo) / ((hi - lo) || 1) * (SPEED_COLORS.length - 1) + .5)))];
  const select = n => {
    const x = laps.find(x => x.n === n);
    P.pick("lap", n, x && [0, x.t0, x.t1, true]);
    P.markRows($("#t-laps").querySelectorAll("tr[data-n]"), "lap", tr => +tr.dataset.n);
  };
  const tipLap = x => `<b>Lap ${x.n}${x === best ? " · best" : ""}${good.includes(x) ? "" : " · irregular"}</b>
    <div class="row"><span>time</span><span>${dur(x.elapsed)}</span></div><div class="row"><span>moving</span><span>${dur(x.moving)}${x.pause > 1 ? ` (+${dur(x.pause)} stopped)` : ""}</span></div>
    <div class="row"><span>speed</span><span>${f2(x.kmh)} km/h · ${pace(x.kmh)}/km</span></div><div class="row"><span>distance</span><span>${f2(x.km)} km</span></div>` + (x.hr ? `<div class="row"><span>HR</span><span>${f0(x.hr)} bpm</span></div>` : "");
  const ymin = Math.max(0, Math.floor((quantile(vals, 0) ?? 10) - 3));
  chart($("#c-laps"), {
    height: 300, x: { type: "linear", fmt: v => Number.isInteger(v) ? String(v) : "" }, y: { min: ymin, fmt: v => v.toFixed(0), label: "km/h" },
    y2: hasHr && laps.some(x => x.hr) ? { fmt: v => v.toFixed(0), label: "bpm" } : null, refY: median(vals),
    series: [{ name: "lap speed (moving)", kind: "bar", color: PAL.hue, data: laps.filter(x => x.kmh).map(x => ({ x: x.n, y: Math.max(ymin, x.kmh), ref: x.n, color: col(x) })) },
      ...(hasHr ? [{ name: "avg HR", kind: "line", axis: "y2", color: PAL.hr, width: 1.5, data: laps.filter(x => x.hr).map(x => ({ x: x.n, y: x.hr, ref: x.n })) }] : [])],
    tip: (xv) => { const x = laps.find(l => l.n === xv); return x ? tipLap(x) : ""; },
    onClick: select,
  });
  const row = (x) => `<tr class="click${x === best ? " best" : ""}" data-n="${f0(x.n)}"><td>${f0(x.n)}</td><td>${f2(x.km)}</td><td>${dur(x.elapsed)}</td><td>${dur(x.moving)}</td>
    <td>${x.pause > 1 ? dur(x.pause) : ""}</td><td><span class="bar" style="width:${x.kmh && hi ? Math.max(4, (x.kmh - ymin) / (Math.max(...vals) - ymin) * 70).toFixed(0) : 0}px;background:${col(x)}"></span> ${f2(x.kmh)}</td>
    <td>${pace(x.kmh)}</td><td class="${best && x.elapsed - best.elapsed > 0 ? "down" : "up"}">${best ? (x === best ? "best" : "+" + dur(x.elapsed - best.elapsed)) : ""}</td><td>${f0(x.hr)}</td></tr>`;
  const extra = (label, seg) => seg.km > 0.05 ? `<tr class="muted"><td class="l" colspan="2">${label}</td><td colspan="7" class="l">${f2(seg.km)} km</td></tr>` : "";
  $("#t-laps").innerHTML = `<table><thead><tr><th>lap</th><th>km</th><th>time</th><th>moving</th><th>stopped</th><th>km/h</th><th>pace</th><th>vs best</th><th>HR</th></tr></thead><tbody>` +
    extra("before first lap", L.pre) + laps.map(row).join("") + extra("after last lap", L.post) + `</tbody></table>`;
  $("#t-laps").querySelectorAll("tr[data-n]").forEach(tr => tr.onclick = () => select(+tr.dataset.n));
}
