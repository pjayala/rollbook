// SPDX-License-Identifier: GPL-3.0-or-later
// Copyright (C) 2026 Pedro Ayala. Rollbook is free software, see LICENSE.
"use strict";
/* Progress page (#/progress): trends, best-efforts trend, race predictor. */

async function viewProgress() {
  if (isAll()) return viewPickSport("progress");
  const all = await loadActs(S.type), acts = inRange(all);
  const app = $("#app");
  if (!acts.length) { app.innerHTML = `<p class="muted">No ${esc(S.type)} sessions in this range.</p>`; return; }
  const km = acts.reduce((s, a) => s + (a.km || 0), 0), mv = acts.reduce((s, a) => s + (a.moving_s || 0), 0);
  const best = (k, xs = acts, low) => xs.filter(a => isNum(a[k]) && (!low || a[k] > 0)).reduce((b, a) => !b || (low ? a[k] < b[k] : a[k] > b[k]) ? a : b, null);
  const b10 = best("best10min_kmh"), wn = best("wind_neutral"), lg = best("km");
  const last30 = all.filter(a => a._t >= Date.now() - 30 * DAY), prev30 = all.filter(a => a._t >= Date.now() - 60 * DAY && a._t < Date.now() - 30 * DAY);
  const k30 = last30.reduce((s, a) => s + a.km, 0), kp30 = prev30.reduce((s, a) => s + a.km, 0);
  const wnRecent = median(last30.map(a => a.wind_neutral)), wnPrev = median(all.filter(a => a._t < Date.now() - 30 * DAY).slice(-40).map(a => a.wind_neutral));
  const link = a => a ? `<a href="#/session/${a.id}">${fdate(a._d)}</a>` : "";
  const hasPw = acts.some(a => isNum(a.pw20m)), p20 = best("pw20m"), FT = ftpAt(all), ftp = FT && FT.w;
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
      ...(hasPw ? [{ k: "Best 20-min power", v: f0(p20?.pw20m) + " W", s: link(p20), hl: 1 },
        { k: FT && FT.src === "estimate" ? "FTP estimate" : "FTP", v: isNum(ftp) ? f0(ftp) + " W" : "–", s: (FT ? FT.note : "") + (ftp && S.profile?.weight_kg ? ` · ${(ftp / S.profile.weight_kg).toFixed(2)} W/kg` : "") }] : []),
    ])}
    <div class="panel" id="p-new" hidden></div>
    <div class="panel"><h2>Calendar <span class="muted">km per day · click a day to open it</span></h2><div id="cal"></div></div>
    <div class="grid g2">
      <div class="panel"><h2>Form: best 10-min speed <span class="muted">click a dot to open the session</span></h2><div id="c-form"></div>
        <div class="note">Wind-neutral = mean of the best 10 min on the out leg and on the back leg, so tail/head wind cancels. Only for out-and-back routes. Lines are rolling medians of 8 sessions.</div></div>
      <div class="panel"><h2>Volume per month</h2><div id="c-vol"></div><div class="note" id="vol-note"></div></div>
      <div class="panel"><h2>Energy per month <span class="muted">kcal, Rollbook's per-sport estimate</span></h2><div id="c-kcal"></div>
        <div class="note" id="kcal-note"></div></div>
      <div class="panel" id="p-be" hidden></div>
      <div class="panel"><h2>Endurance: best sustained speed <span class="muted">30 / 60 min windows</span></h2><div id="c-endu"></div></div>
      <div class="panel"><h2>Average moving speed per session</h2><div id="c-avg"></div>
        <div class="note">Not comparable across 2025-08: the watch sample rate dropped then and inflates moving speed. Use best-10 for long-term form.</div></div>
      <div class="panel"><h2>Efficiency: best 10-min ÷ avg HR <span class="muted">km/h per 100 bpm</span></h2><div id="c-eff"></div>
        <div class="note" id="eff-note"></div></div>
      ${hasPw ? `<div class="panel"><h2>Power: best 5 / 20 min <span class="muted">click a dot to open the session</span></h2><div id="c-pow"></div>
        <div class="note">Recorded by the trainer or power meter, the most reliable fitness signal here. FTP: your profile's if set, else the one your training app used (ROUVY stores it in each ride), else 95% of the best 20 min over the previous ${FTP_DAYS / 30} months.</div></div>
      <div class="panel"><h2>Time in power zones per month <span class="muted">% of FTP at the time of each ride</span></h2><div id="c-pzones"></div></div>
      <div class="panel"><h2>Power ÷ heart rate <span class="muted">avg W per bpm</span></h2><div id="c-pwhr"></div>
        <div class="note">More watts for the same heart rate = fitter. Only sessions with both.</div></div>` : ""}
      <div class="panel"><h2>Out-leg vs back-leg speed <span class="muted">out/back routes · bars = measured, dots = explained by wind + slope</span></h2><div id="c-wind"></div>
        <div class="note">Positive = out leg faster. Dots use the hourly wind at each leg and the leg's grade; the rest is pacing, fatigue and gusts.</div></div>
      <div class="panel"><h2>Heat: speed vs temperature <span class="muted">best 10-min (adjusted where available)</span></h2><div id="c-heat"></div>
        <div class="note" id="heat-note"></div></div>
      <div class="panel"><h2>Time in HR zones per month <span class="muted">% of each session's max HR on its date · <a href="#/profile">change</a></span></h2><div id="c-zones"></div></div>
      <div class="panel" id="p-pred"></div>
    </div>
    <div class="panel" id="p-favs" hidden></div>
    <div class="panel" id="p-circ" hidden></div>
    <div class="grid g2 top">
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
      { name: "raw best 10-min", kind: "dot", color: PAL.dim, r: 2.5, opacity: .8, data: P("best10min_kmh", acts.filter(a => isNum(a.best10min_kmh) && a.best10min_kmh > 0)) },
      { name: "wind-neutral", kind: "dot", color: PAL.hue, r: 3, opacity: .75, data: P("wind_neutral") },
      { name: "raw, rolling", kind: "line", color: PAL.grey, width: 1.5, data: rolling(P("best10min_kmh"), 8), gapX: 45 * DAY },
      { name: "wind-neutral, rolling", kind: "line", color: PAL.ink, width: 2.5, data: rolling(P("wind_neutral"), 8), gapX: 45 * DAY },
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
    m.kcal = m.a.reduce((t, a) => t + (a.kcal || 0), 0);
    for (const a of m.a) zoneSecs(a.hr_hist, a.hrmax || S.hrmax).forEach((s, i) => m.zones[i] += s);  // each against its own day's max
  }
  chart($("#c-vol"), {
    x: { type: "time" }, y: { zero: true, fmt: v => v.toFixed(0), label: "km" }, y2: { zero: true, fmt: v => v.toFixed(0), label: "sessions" },
    series: [{ name: "km", kind: "bar", color: PAL.hue, data: mons.map(m => ({ x: m.x, y: m.km })) },
      { name: "sessions", kind: "line", color: PAL.ink, axis: "y2", data: mons.map(m => ({ x: m.x, y: m.n })) }],
    tip: x => { const m = mons.find(m => m.x === x); return m ? `<b>${fmon(new Date(m.x))}</b><div class="row"><span>distance</span><span>${fkm(m.km)} km</span></div><div class="row"><span>sessions</span><span>${m.n}</span></div><div class="row"><span>moving</span><span>${hours(m.mv)}</span></div>` : ""; }
  });
  const busy = mons.reduce((b, m) => !b || m.km > b.km ? m : b, null);
  $("#vol-note").textContent = busy ? `${fkm(km)} km in ${acts.length} sessions over ${mons.length} months. Busiest: ${fmon(new Date(busy.x))}, ${fkm(busy.km)} km in ${busy.n} sessions.` : "";
  if (mons.some(m => m.kcal)) {
    chart($("#c-kcal"), {
      x: { type: "time" }, y: { zero: true, fmt: v => v >= 1000 ? (v / 1000).toFixed(0) + "k" : v.toFixed(0), label: "kcal" },
      series: [{ name: "kcal", kind: "bar", color: PAL.hue, data: mons.map(m => ({ x: m.x, y: m.kcal })) }], legend: false,
      tip: x => { const m = mons.find(m => m.x === x); return m ? `<b>${fmon(new Date(m.x))}</b><div class="row"><span>energy</span><span>${f0(m.kcal)} kcal</span></div><div class="row"><span>per session</span><span>${f0(m.kcal / m.n)}</span></div><div class="row"><span>per hour</span><span>${m.mv ? f0(m.kcal / (m.mv / 3600)) : "–"}</span></div>` : ""; },
    });
    const tot = acts.reduce((t, a) => t + (a.kcal || 0), 0), src = [...new Set(acts.map(a => (a.kcal_src || "").split(" ")[0]).filter(Boolean))];
    $("#kcal-note").innerHTML = `${f0(tot)} kcal in this range (${f0(tot / 7700)} kg of fat-equivalent). Method: ${esc(src.join(", "))}, from your <a href="#/profile">profile</a> weight.`;
  } else $("#c-kcal").innerHTML = `<p class="muted">Enter your weight in the <a href="#/profile">profile</a> to estimate calories.</p>`;
  bestEffortsTrend($("#p-be"), all, acts);
  chart($("#c-endu"), {
    x: { type: "time" }, y: { fmt: v => v.toFixed(0), label: "km/h" }, tip: tipAct, onClick: go,
    series: [{ name: "best 30 min", kind: "dot", color: PAL.dim, data: P("b30", acts.filter(a => a.b30 > 5)) },
      { name: "best 60 min", kind: "dot", color: PAL.hue, r: 3.5, opacity: .8, data: P("b60", acts.filter(a => a.b60 > 5)) },
      { name: "60 min, rolling", kind: "line", color: PAL.ink, width: 2.5, data: rolling(P("b60", acts.filter(a => a.b60 > 5)), 6), gapX: 60 * DAY }]
  });
  chart($("#c-avg"), {
    x: { type: "time" }, y: { fmt: v => v.toFixed(0), label: "km/h" }, tip: tipAct, onClick: go,
    series: [{ name: "moving avg", kind: "dot", color: PAL.hue, r: 2.5, opacity: .7, data: P("kmh_moving", acts.filter(a => a.kmh_moving > 0)) },
      { name: "moving avg, rolling", kind: "line", color: PAL.ink, width: 2.5, data: rolling(P("kmh_moving"), 8), gapX: 45 * DAY },
      { name: "elapsed avg, rolling", kind: "line", color: PAL.grey, width: 1.5, dash: "4 3", data: rolling(P("kmh_elapsed"), 8), gapX: 45 * DAY }]
  });
  const withHr = acts.filter(a => a.hr_avg > 60 && a.best10min_kmh > 0);
  $("#eff-note").textContent = withHr.length ? `Higher = more speed for the same heart rate. ${withHr.length} of ${acts.length} sessions have heart rate.` : "Needs sessions with heart rate.";
  chart($("#c-eff"), {
    x: { type: "time" }, y: { fmt: v => v.toFixed(0) }, y2: { fmt: v => v.toFixed(0), label: "bpm" }, tip: tipAct, onClick: go,
    series: [{ name: "efficiency", kind: "dot", color: PAL.hue, r: 3.5, data: withHr.map(a => ({ x: a._t, y: a.best10min_kmh / a.hr_avg * 100, ref: a.id })) },
      { name: "avg HR", kind: "dot", color: PAL.hr, axis: "y2", r: 2, opacity: .45, data: withHr.map(a => ({ x: a._t, y: a.hr_avg, ref: a.id })) }]
  });
  if (hasPw) {
    const pw = acts.filter(a => isNum(a.pw20m));
    const ftpLine = pw.map(a => ({ x: a._t, y: ftpEst(all, a._t, a) }));
    const tipPw = (x, hits, b) => { const a = acts.find(a => a.id === b.p.ref); return a ? `<b>${fdate(a._d)}</b> · ${esc(a.name)}<div class="row"><span>best 5 min</span><span>${f0(a.pw5m)} W</span></div><div class="row"><span>best 20 min</span><span>${f0(a.pw20m)} W</span></div><div class="row"><span>weighted</span><span>${f0(a.np)} W</span></div><div class="row"><span>avg</span><span>${f0(pwAvg(a))} W</span></div>` : ""; };
    chart($("#c-pow"), {
      x: { type: "time" }, y: { fmt: v => v.toFixed(0), label: "W" }, tip: tipPw, onClick: go,
      series: [{ name: "best 5 min", kind: "dot", color: PAL.dim, r: 2.5, opacity: .9, data: P("pw5m", pw) },
        { name: "best 20 min", kind: "dot", color: PAL.hue, r: 3.5, data: P("pw20m", pw) },
        { name: "FTP", kind: "line", color: PAL.ink, width: 2.5, data: ftpLine, gapX: 60 * DAY }],
    });
    const pzm = mons.map(mo => ({ x: mo.x, z: mo.a.reduce((z, a) => pzSecs(a.pw_hist, ftpEst(all, a._t, a)).map((v, i) => z[i] + v), PZONES.map(() => 0)) })).filter(mo => mo.z.some(v => v > 0));
    if (pzm.length) chart($("#c-pzones"), {
      x: { type: "time" }, y: { zero: true, fmt: v => v.toFixed(0), label: "h" },
      series: PZONES.map((z, i) => ({ name: `${z.n} ${z.d}`, kind: "bar", stack: "z", color: z.c, data: pzm.map(mo => ({ x: mo.x, y: mo.z[i] / 3600 })) })),
      tip: x => { const mo = pzm.find(mo => mo.x === x); if (!mo) return ""; const tot = mo.z.reduce((p, q) => p + q, 0); return `<b>${fmon(new Date(mo.x))}</b>` + mo.z.map((v, i) => `<div class="row"><span>${PZONES[i].n} ${PZONES[i].d}</span><span>${dur(v, true)} · ${(v / tot * 100).toFixed(0)}%</span></div>`).join(""); },
    });
    else $("#c-pzones").innerHTML = `<p class="muted">No power data in this range.</p>`;
    const ph = acts.filter(a => pwAvg(a) && a.hr_avg > 60);
    if (ph.length) chart($("#c-pwhr"), {
      x: { type: "time" }, y: { fmt: v => v.toFixed(2), label: "W/bpm" }, onClick: go, legend: false,
      tip: (x, h, b) => { const a = acts.find(a => a.id === b.p.ref); return a ? `<b>${fdate(a._d)}</b><div class="row"><span>avg power</span><span>${f0(pwAvg(a))} W</span></div><div class="row"><span>avg HR</span><span>${f0(a.hr_avg)} bpm</span></div><div class="row"><span>W per bpm</span><span>${(pwAvg(a) / a.hr_avg).toFixed(2)}</span></div>` : ""; },
      series: [{ name: "W per bpm", kind: "dot", color: PAL.hue, r: 3, data: ph.map(a => ({ x: a._t, y: pwAvg(a) / a.hr_avg, ref: a.id })) },
        { name: "rolling", kind: "line", color: PAL.ink, width: 2.5, data: rolling(ph.map(a => ({ x: a._t, y: pwAvg(a) / a.hr_avg })), 6), gapX: 45 * DAY }],
    });
    else $("#c-pwhr").innerHTML = `<p class="muted">No sessions with both power and heart rate in this range.</p>`;
  }
  const ob = acts.filter(a => isNum(a.asym_pct));
  chart($("#c-wind"), {
    x: { type: "time" }, y: { fmt: v => v.toFixed(0) + "%" }, refY: 0, tip: tipAct, onClick: go,
    series: [{ name: "explained", kind: "dot", color: PAL.ink, r: 2.5, data: ob.filter(a => a.cond && a.cond.legs).map(a => ({ x: a._t, y: a.cond.gap_wind + a.cond.gap_slope, ref: a.id })) },
      { name: "out vs back", kind: "bar", color: PAL.hue, data: ob.map(a => ({ x: a._t, y: a.asym_pct, ref: a.id, color: Math.abs(a.asym_pct) < 6 ? PAL.dim : Math.abs(a.asym_pct) < 15 ? PAL.deep : PAL.hue })) }],
    legend: false
  });
  const hot = acts.filter(a => isNum(a.temp) && a.best10min_kmh > 5);
  if (hot.length > 5) {
    const pts = hot.map(a => ({ x: a.temp, y: a.adj_neutral || a.best10min_kmh, ref: a.id, color: null }));
    const bins = {}; for (const p of pts) (bins[Math.round(p.x / 3) * 3] ||= []).push(p.y);
    $("#heat-note").textContent = `${hot.length} outdoor sessions with weather; the line is the median of each 3 °C band with at least 3 sessions.`;
    chart($("#c-heat"), {
      x: { type: "linear", fmt: v => v.toFixed(0) + "°" }, y: { fmt: v => v.toFixed(0), label: "km/h" }, onClick: go,
      series: [{ name: "session", kind: "dot", color: PAL.hue, r: 2.5, opacity: .7, data: pts },
        { name: "median per 3 °C", kind: "line", color: PAL.ink, width: 2.5, data: Object.entries(bins).filter(([, v]) => v.length >= 3).map(([k, v]) => ({ x: +k, y: median(v) })).sort((p, q) => p.x - q.x) }],
      tip: (x, h, b) => { const a = acts.find(a => a.id === b.p.ref); return a ? `<b>${fdate(a._d)}</b> · ${f0(a.temp)}°C<div class="row"><span>best 10</span><span>${f2(b.p.y)}</span></div>${a.hr_avg ? `<div class="row"><span>HR</span><span>${f0(a.hr_avg)}</span></div>` : ""}` : `${f0(x)}°C`; },
    });
  } else $("#c-heat").innerHTML = `<p class="muted">Weather is loading (or no outdoor sessions in range).</p>`;
  const zm = mons.filter(m => m.zones.some(z => z > 0));
  chart($("#c-zones"), {
    x: { type: "time" }, y: { zero: true, fmt: v => v.toFixed(0), label: "h" },
    series: ZONES.map((z, i) => ({ name: zonePct(i), kind: "bar", stack: "z", color: z.c, data: zm.map(m => ({ x: m.x, y: m.zones[i] / 3600 })) })),
    tip: x => { const m = zm.find(m => m.x === x); if (!m) return ""; const tot = m.zones.reduce((a, b) => a + b, 0); return `<b>${fmon(new Date(m.x))}</b>` + m.zones.map((s, i) => `<div class="row"><span>${zonePct(i)}</span><span>${dur(s, true)} · ${(s / tot * 100).toFixed(0)}%</span></div>`).join(""); }
  });
  if (!zm.length) $("#c-zones").innerHTML = `<p class="muted">No heart-rate data in this range.</p>`;

  // records
  $("#t-rec").innerHTML = `<table><thead><tr><th>record</th><th>value</th><th class="l">date</th><th>km</th><th class="l">route</th></tr></thead><tbody>` +
    RECORD_METRICS.filter(m => !m.power || hasPw).map(m => { const a = best(m.k, m.minKm ? acts.filter(a => a.km >= m.minKm) : acts, m.low); if (!a) return ""; return `<tr class="click" data-id="${a.id}"><td class="l">${m.n}</td><td>${recFmt(m, a[m.k])}${m.u === "s" ? "" : " " + m.u}</td><td class="l">${fdate(a._d)}</td><td>${fkm(a.km)}</td><td class="l">${routePill(a.route)}</td></tr>`; }).join("") + `</tbody></table>`;
  $("#t-mon").innerHTML = `<table><thead><tr><th>month</th><th>n</th><th>km</th><th>moving</th><th>km/h</th><th>best10 max</th><th>best10 med</th><th>wind-neutral</th><th>HR</th><th>kcal</th></tr></thead><tbody>` +
    mons.slice().reverse().map(m => `<tr><td class="l">${fmon(new Date(m.x))}</td><td>${m.n}</td><td>${fkm(m.km)}</td><td>${hours(m.mv)}</td><td>${f1(m.kmh)}</td><td>${f2(m.b10)}</td><td>${f2(m.b10med)}</td><td>${f2(m.wn)}</td><td>${f0(m.hr)}</td><td>${m.kcal ? f0(m.kcal) : "–"}</td></tr>`).join("") + `</tbody></table>`;
  app.querySelectorAll("tr[data-id]").forEach(tr => tr.onclick = () => go(tr.dataset.id));
  // the monthly table scrolls inside a frame as tall as the records beside it
  if (innerWidth > 1100) $("#t-mon").style.maxHeight = Math.max(320, $("#t-rec").offsetHeight) + "px";
  predictor($("#p-pred"), all);
  calendar($("#cal"), acts);
  circuits($("#p-circ"), acts);
  favPanel($("#p-favs"), all);
  newRecordsPanel($("#p-new"), { dismiss: true, sport: isAll() ? null : S.type });
}

/** Best efforts over time: one dot per session for the chosen distance (its average
 * km/h, so up is faster like the other form charts) and the record progression. */
function bestEffortsTrend(host, all, acts) {
  const avail = BE.filter(b => all.filter(a => a["be" + b.k] > 0).length >= 3);
  if (!avail.length || /^Virtual/.test(S.type)) { host.hidden = true; return; }
  host.hidden = false;
  let k = avail.some(b => b.k === localStorage.beDist) ? localStorage.beDist : (avail.find(b => b.k === "5k") || avail[0]).k;
  host.innerHTML = `<div class="toolbar"><h2 style="margin:0">Best efforts <span class="muted">click a dot to open the session</span></h2>
    <div class="seg" id="be-dist" role="group" aria-label="Distance">${avail.map(b => `<button data-k="${b.k}">${esc(b.short || b.n)}</button>`).join("")}</div></div>
    <div id="c-be"></div><div class="note" id="be-note"></div>`;
  const draw = () => {
    const b = BE.find(x => x.k === k), key = "be" + b.k, kmh = s => b.m / s * 3.6;
    host.querySelectorAll("#be-dist button").forEach(x => { const on = x.dataset.k === k; x.classList.toggle("on", on); x.setAttribute("aria-pressed", on); });
    // the record progression runs over all time, then is clipped to the range
    const prog = [], sorted = all.filter(a => a[key] > 0).slice().sort(cmpWhen);
    let rec = null;
    for (const a of sorted) if (rec == null || a[key] < rec) { rec = a[key]; prog.push({ a, s: rec }); }
    const lo = acts.length ? acts[0]._t : 0, inr = acts.filter(a => a[key] > 0), inSet = new Set(prog.map(p => p.a.id));
    const before = prog.filter(p => p.a._t < lo).pop(), steps = [...(before ? [{ ...before, x: lo }] : []), ...prog.filter(p => p.a._t >= lo).map(p => ({ ...p, x: p.a._t }))];
    const line = steps.flatMap((p, i) => i ? [{ x: p.x, y: kmh(steps[i - 1].s) }, { x: p.x, y: kmh(p.s) }] : [{ x: p.x, y: kmh(p.s) }]);
    if (line.length && acts.length) line.push({ x: acts[acts.length - 1]._t, y: line[line.length - 1].y });
    chart($("#c-be"), {
      height: 214, x: { type: "time" }, y: { fmt: v => v.toFixed(0), label: "km/h" }, onClick: go,
      series: [{ name: `${b.short || b.n} per session`, kind: "dot", color: PAL.hue, r: 3, opacity: .8, data: inr.map(a => ({ x: a._t, y: kmh(a[key]), ref: a.id, color: inSet.has(a.id) ? PAL.ink : null })) },
        { name: "record", kind: "line", color: PAL.ink, width: 2, data: line }],
      tip: (x, h, best) => { const a = acts.find(a => a.id === best.p.ref); return a ? `<b>${fdate(a._d)}</b> · ${esc(a.name)}<div class="row"><span>${esc(b.n)}</span><span>${dur(a[key], a[key] >= 3600)} · ${pace(kmh(a[key]))}/km</span></div><div class="row"><span>speed</span><span>${f2(kmh(a[key]))} km/h</span></div>${inSet.has(a.id) ? `<div class="row"><span></span><span>a record when set</span></div>` : ""}` : ""; },
    });
    const pr = prog[prog.length - 1];
    $("#be-note").textContent = `${inr.length} sessions in range reach ${b.n}. Record ${dur(pr.s, pr.s >= 3600)} (${f2(kmh(pr.s))} km/h) on ${fdate(pr.a._d)}; white dots set a record when they happened. Elapsed time, stops included.`;
  };
  host.querySelector("#be-dist").onclick = e => { const x = e.target.closest("button"); if (!x) return; k = localStorage.beDist = x.dataset.k; draw(); };
  draw();
}

function predictor(host, all) {
  const recent = all.filter(a => a._t >= Date.now() - 60 * DAY);
  host.innerHTML = `<h2>Race predictor <span class="muted">heuristic, from the last 60 days</span></h2>
    <div class="toolbar"><label>Distance <input id="pk" type="number" min="1" max="200" step="0.5" value="${Math.min(200, Math.max(1, +localStorage.predKm || 24))}" style="width:80px"> km</label></div>
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
