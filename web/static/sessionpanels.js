// SPDX-License-Identifier: GPL-3.0-or-later
// Copyright (C) 2026 Pedro Ayala. Rollbook is free software, see LICENSE.
"use strict";
/* The session page's panels below the charts: km splits, out / back legs, best
 * efforts, power, heart-rate zones, same route. Each takes the page P (see
 * viewSession) and fills its own element; a row that selects a stretch goes
 * through P.pick (sessionsel.js), which drops any other table's pick first. */

/** Speed per km (bars) and the splits table. Sets P.selectKm, which the charts call on a click. */
function splitsPanel(P) {
  const { a, m, ser, avg, ele } = P, sp = a.splits || [];
  if (!sp.length) {
    $("#c-splits").innerHTML = $("#t-splits").innerHTML = `<p class="muted">No per-km splits (no distance channel).</p>`;
    return;
  }
  const lo = quantile(sp.map(s => s.kmh), .1), hi = quantile(sp.map(s => s.kmh), .9);
  const col = v => SPEED_COLORS[Math.max(0, Math.min(SPEED_COLORS.length - 1, Math.floor((v - lo) / ((hi - lo) || 1) * (SPEED_COLORS.length - 1) + .5)))];
  const turnKm = m.route === "out-back" ? m.turn_km : null;
  const totKm = ser.length ? ser[ser.length - 1][1] : a.km;
  P.selectKm = k => {
    const s = sp.find(s => s.km === k);
    P.pick("km", k, s && [1, s.km - 1, s === sp[sp.length - 1] ? totKm : s.km, false]);
    P.markRows($("#t-splits").querySelectorAll("tr[data-km]"), "km", tr => +tr.dataset.km);
  };
  chart($("#c-splits"), {
    onClick: k => P.selectKm(k),
    x: { type: "linear", fmt: v => v.toFixed(0) }, y: { min: Math.max(0, Math.floor(Math.min(...sp.map(s => s.kmh)) - 2)), fmt: v => v.toFixed(0), label: "km/h" }, legend: false,
    refY: avg,
    series: [{ name: "km/h", kind: "bar", color: PAL.hue, data: sp.map(s => ({ x: s.km, y: s.kmh, ref: s.km, color: col(s.kmh) })) }],
    tip: x => { const s = sp.find(s => s.km === x); return s ? `<b>km ${f0(s.km)}${turnKm != null ? (s.km <= turnKm ? " · out" : " · back") : ""}</b><div class="row"><span>speed</span><span>${f2(s.kmh)} km/h</span></div><div class="row"><span>pace</span><span>${dur(s.moving_s)} /km</span></div>${s.pause_s > 0 ? `<div class="row"><span>paused</span><span>${dur(s.pause_s)}</span></div>` : ""}${s.hr ? `<div class="row"><span>HR</span><span>${f0(s.hr)}</span></div>` : ""}` : ""; }
  });
  const fastest = Math.max(...sp.map(s => s.kmh));
  const eleCells = s => {
    const [i0, i1] = kmIdx(ser, s.km - 1, s.km), { net, g } = eleNetGrade(ser, ele, i0, i1);
    return `<td>${Math.abs(net) >= 1 ? fnet(net) : ""}</td><td class="${isNum(g) && Math.abs(g) >= 3 ? (g > 0 ? "down" : "up") : ""}">${fgrade(g)}</td>`;
  };
  let cum = 0;
  $("#t-splits").innerHTML = `<table><thead><tr><th>km</th><th>pace</th><th>km/h</th><th class="l"></th><th>Δ avg</th><th>paused</th><th>HR</th>${ele ? "<th>elev Δ</th><th>grade</th>" : ""}<th>cumulative</th></tr></thead><tbody>` +
    sp.map(s => { cum += s.elapsed_s || 0; return `<tr class="click" data-km="${f0(s.km)}"><td>${f0(s.km)}${turnKm != null && s.km === Math.ceil(turnKm) ? " ↩" : ""}</td><td>${dur(s.moving_s)}</td><td>${f2(s.kmh)}</td>
      <td class="l"><span class="bar" style="width:${(s.kmh / fastest * 90).toFixed(0)}px;background:${col(s.kmh)}"></span></td>
      <td class="${s.kmh >= avg ? "up" : "down"}">${isNum(avg) ? (s.kmh - avg >= 0 ? "+" : "") + (s.kmh - avg).toFixed(1) : ""}</td>
      <td>${s.pause_s > 0.5 ? dur(s.pause_s) : ""}</td><td>${f0(s.hr)}</td>${ele ? eleCells(s) : ""}<td>${dur(cum, true)}</td></tr>`; }).join("") + `</tbody></table>
    <div class="note">Last row may be a partial kilometre. ↩ marks the turnaround. Click a km (here, in the bars or in the speed chart) to show it on the map; click again to clear.</div>`;
  $("#t-splits").querySelectorAll("tr[data-km]").forEach(tr => tr.onclick = () => P.selectKm(+tr.dataset.km));
}

/** Out-and-back: the two legs, with wind / slope effects when there is weather. */
function legsPanel(P) {
  const { a, m, ser } = P;
  const C = a.cond && a.cond.legs ? a.cond : null, Lg = C ? C.legs : {};
  const cx = leg => C ? `<td>${fsigned(Lg[leg].tail, "")}</td><td>${fsigned(Lg[leg].grade, "%")}</td><td>${fsigned(Lg[leg].wind_pct)}</td><td>${fsigned(Lg[leg].slope_pct)}</td><td><b>${f2(Lg[leg].adj_b10)}</b></td>` : "";
  const host = $("#t-legs");
  host.innerHTML = `<div class="scroll"><table><thead><tr><th>leg</th><th>km</th><th>moving</th><th>avg km/h</th><th>best 10 min</th>${C ? "<th>tailwind km/h</th><th>grade</th><th>wind effect</th><th>slope effect</th><th>adjusted</th>" : ""}</tr></thead><tbody>
    <tr class="click" data-leg="out"><td>Out (${f0(m.bearing_out)}°)</td><td>${f2(m.out_km)}</td><td>${dur(m.out_moving_s, true)}</td><td>${f2(m.out_kmh)}</td><td>${f2(m.out_b10)}</td>${cx("out")}</tr>
    <tr class="click" data-leg="back"><td>Back</td><td>${f2(m.back_km)}</td><td>${dur(m.back_moving_s, true)}</td><td>${f2(m.back_kmh)}</td><td>${f2(m.back_b10)}</td>${cx("back")}</tr>
    <tr><td><b>Neutral</b></td><td></td><td></td><td>${pct(m.asym_pct)}</td><td><b>${f2(m.wind_neutral)}</b></td>${C ? `<td colspan="4"></td><td><b>${f2(C.adjusted)}</b></td>` : ""}</tr></tbody></table></div>` +
    (C ? `<div class="gapbar">${[["wind", C.gap_wind, PAL.hue], ["slope", C.gap_slope, PAL.soft], ["other", C.gap_other, PAL.grey]].map(([n, v, c]) =>
      `<span style="flex:${Math.max(0.5, Math.abs(v))};background:${c}" title="${n} ${fsigned(v)}">${n} ${fsigned(v)}</span>`).join("")}</div>
      <div class="note">Out leg ${fsigned(C.gap_pct)} faster than back (best 10 min): <b>${fsigned(C.gap_wind)}</b> from wind (${f0(C.ws)} km/h from ${fdeg(C.wd)}, ${C.wind_k.toFixed(2)}% per km/h, fitted on your sessions), <b>${fsigned(C.gap_slope)}</b> from slope, ${fsigned(C.gap_other)} other (pacing, fatigue, gusts the hourly model misses). Weather: Open-Meteo, hourly, ~10 km grid.</div>` : "");
  const end = ser.length ? ser[ser.length - 1][1] : a.km;
  const rows = host.querySelectorAll("tr[data-leg]");
  rows.forEach(tr => tr.onclick = () => {
    const out = tr.dataset.leg === "out";
    P.pick("leg", tr.dataset.leg, [1, out ? 0 : m.turn_km, out ? m.turn_km : end, false]);
    P.markRows(rows, "leg", r => r.dataset.leg);
  });
}

/** Best efforts by distance, ranked against every visible session of the sport, all time. */
function bestEffortsPanel(P) {
  const { a, m, peers } = P, me = String(a.id);
  const rows = BE.filter(b => m.be[b.k]).map(b => {
    const [secs, t0, m0] = m.be[b.k], key = "be" + b.k;
    const others = peers.filter(p => String(p.id) !== me && p[key] > 0).map(p => p[key]).sort((x, y) => x - y);
    const r = Math.round(secs), rank = others.filter(v => Math.round(v) < r).length + 1;
    const vs = a.hidden ? null : rank === 1 ? (others.length ? Math.round(others[0]) - r : null) : r - Math.round(others[0]);
    return { b, secs, t0, m0, rank, of: others.length + 1, vs };
  });
  const host = $("#t-be");
  host.innerHTML = `<table><thead><tr><th>distance</th><th>time</th><th>pace</th><th>km/h</th><th>rank</th><th>vs the best</th></tr></thead><tbody>` +
    rows.map(x => `<tr class="click" data-be="${esc(x.b.k)}"><td class="l">${esc(x.b.n)}</td><td>${dur(x.secs, x.secs >= 3600)}</td><td>${pace(x.b.m / x.secs * 3.6)}</td><td>${f2(x.b.m / x.secs * 3.6)}</td>
      <td>${a.hidden ? `<span class="muted">hidden</span>` : x.rank === 1 && x.of > 1 ? `<span class="pill pill-pr">best</span>` : `${f0(x.rank)} <span class="muted">of ${f0(x.of)}</span>`}</td>
      <td class="muted">${x.vs == null ? "–" : x.rank === 1 ? (x.vs ? `${dur(x.vs)} ahead of 2nd` : "tied") : `+${dur(x.vs)}`}</td></tr>`).join("") + `</tbody></table>` +
    `<div class="note">Elapsed time over the distance, stops included, anywhere in the session. ${a.km_diff_pct != null && Math.abs(a.km_diff_pct) > 10 ? "This track disagrees with Strava's distance by over 10%, so treat these with care. " : ""}Short efforts (400 m, 1 km) read slower on 6-second recording (since 2025-08) than on 1-second.</div>`;
  const trs = host.querySelectorAll("tr[data-be]");
  trs.forEach(tr => tr.onclick = () => {
    const x = rows.find(r => r.b.k === tr.dataset.be);
    P.pick("be", x.b.k, [1, x.m0 / 1000, (x.m0 + x.b.m) / 1000, true, { el: x.secs, km: x.b.m / 1000 }]);
    P.markRows(trs, "be", r => r.dataset.be);
  });
}

/** Power: zone strip (needs an FTP), averages and best power per window. */
function powerPanel(P) {
  const { a, m, W, ftp, FT, IF, load } = P, pwb = m.pw || {};
  const st = (k, v, u = "W", t = "") => `<div class="pstat"${t ? ` title="${esc(t)}"` : ""}><span>${k}</span><div><b>${v}</b>${v === "–" ? "" : ` <small>${u}</small>`}</div></div>`;
  const pp = (lbl, w) => st(lbl, isNum(pwb[w]) && pwb[w] > 0 ? f0(pwb[w]) : "–");
  const zs = m.pw_zero_s, dt = a.elapsed_s || 1;
  $("#p-power").innerHTML = (ftp && m.pw_hist ? zoneStrip(pzSecs(m.pw_hist, ftp), PZONES, i => `${pzLabel(i, ftp)} · ${PZONES[i].d}`) : "") +
    `<div class="pstats"><div>${st("Avg power", f0(W))}${st("Max power", f0(a.watts_max))}${st("Weighted power", f0(m.np), "W", "normalized power (Coggan): what a steady effort of the same cost would be")}${isNum(zs) ? st("Zero power", `${f0(zs)} s / ${f0(zs / dt * 100)}`, "%") : ""}</div>
      <div>${pp("Best 5 s", "5")}${pp("Best 30 s", "30")}${pp("Best 1 min", "60")}${pp("Best 5 min", "300")}</div>
      <div>${pp("Best 20 min", "1200")}${pp("Best 30 min", "1800")}${pp("Best 60 min", "3600")}${isNum(IF) ? st("Intensity / load", `${IF.toFixed(2)} / ${f0(load)}`, "") : ""}</div></div>
    <div class="note">Intensity = weighted power ÷ FTP; load = hours × intensity² × 100 (Coggan's NP, IF and TSS, trademarks of TrainingPeaks). Set your FTP in the <a href="#/profile">profile</a> to override ${FT && FT.src === "app" ? "the one from your training app" : "the estimate"}.</div>`;
}

/** Time per heart-rate zone, zones as % of the HRmax in effect that day. */
function hrZonesPanel(P) {
  const { a, m } = P, hm = a.hrmax || S.hrmax, z = zoneSecs(m.hr_hist, hm), tot = z.reduce((x, y) => x + y, 0) || 1;
  $("#t-zones").innerHTML = `<table><tbody>` + z.map((s, k) => `<tr><td class="l">${zoneLabel(k, hm)}</td><td>${dur(s, true)}</td><td>${(s / tot * 100).toFixed(0)}%</td>
    <td class="l"><span class="bar" style="width:${(s / tot * 240).toFixed(0)}px;background:${ZONES[k].c}"></span></td></tr>`).join("") + `</tbody></table>`;
}

/** The same-route table, ranked by wind-neutral (out-and-back) or best 10-min speed. */
function sameRoutePanel(P) {
  const { a, m, peers } = P, same = sameRoute(a, m, peers), host = $("#t-same");
  const metric = m.route === "out-back" ? "wind_neutral" : "best10min_kmh";
  const rank = same.filter(p => isNum(p[metric])).filter(p => p[metric] > (m[metric] ?? a[metric])).length + 1;
  host.innerHTML = same.length ? `<div class="note" style="margin:0 0 8px">${f0(same.length)} similar sessions. This one ranks <b>#${f0(rank)}</b> of ${same.filter(p => isNum(p[metric])).length + 1} by ${metric === "wind_neutral" ? "wind-neutral" : "best 10-min"} speed.</div>
    <table><thead><tr><th>date</th><th>km</th><th>moving</th><th>avg km/h</th><th>best 10</th><th>wind-neutral</th><th>out vs back</th><th>HR</th><th></th></tr></thead><tbody>` +
    same.slice().sort((x, y) => y._t - x._t).slice(0, 25).map(p => `<tr class="click" data-id="${esc(p.id)}"><td class="l">${fdate(p._d)}</td><td>${fkm(p.km)}</td><td>${dur(p.moving_s, true)}</td><td>${f2(p.kmh_moving)}</td><td>${f2(p.best10min_kmh)}</td><td>${f2(p.wind_neutral)}</td><td>${pct(p.asym_pct)}</td><td>${f0(p.hr_avg)}</td><td><a class="rowact" href="#/compare/${esc(a.id)}/${esc(p.id)}">Compare</a></td></tr>`).join("") + `</tbody></table>` : `<p class="muted">No other sessions on a matching route.</p>`;
  host.querySelectorAll("tr[data-id]").forEach(tr => tr.onclick = e => { if (!e.target.closest("a")) go(tr.dataset.id); });
}
