// SPDX-License-Identifier: GPL-3.0-or-later
// Copyright (C) 2026 Pedro Ayala. Rollbook is free software, see LICENSE.
"use strict";
/* Session page (#/session/<id>): loads the session, builds the page object P
 * that every part shares, lays the page out and lets each part fill its panel:
 *   sessionsel.js     the one selected stretch (P.hl, P.pick, P.clearRowSel, P.preview)
 *   sessioncharts.js  speed / power / elevation charts (P.charts, P.xmode)
 *   sessionpanels.js  splits, legs, best efforts, power, HR zones, same route
 *   bestspeeds.js     the best-speeds curve;  laps.js  laps;  segments.js  efforts */

/** Everything the page's parts read, derived once from the API answer. */
function sessionPage(a, peers) {
  const m = a.metrics || {}, ser = a.series || [];
  const d = new Date(a.datetime || a.date);
  const i = peers.findIndex(p => p.id === a.id);
  // power: recorded average (or Strava's indoors), intensity vs the FTP estimate at that date
  const self = peers.find(p => p.id === a.id) || { ...a, _t: +d };
  const W = pwAvg(a), FT = ftpAt(peers, +d, self), ftp = FT && FT.w, IF = isNum(m.np) && ftp ? m.np / ftp : null;
  return {
    a, m, ser, peers, self, d, W, FT, ftp, IF,
    prev: i >= 0 ? peers[i - 1] : null, next: i >= 0 ? peers[i + 1] : null,
    hasHr: ser.some(r => r[3]), hasEle: ser.some(r => r[4] != null), hasGps: ser.some(r => r[5] != null), hasPw: ser.some(r => r[7] != null),
    load: isNum(IF) ? a.elapsed_s / 3600 * IF * IF * 100 : null,
    cad: isNum(a.cad_avg) ? a.cad_avg : a.s_avg_cad,
    vmax: isNum(a.kmh_max) ? a.kmh_max : a.s_max_speed ? a.s_max_speed * 3.6 : null,
    avg: a.kmh_moving, L: m.laps,
    bw: bestSpeeds(a, m, peers, whenOf(self)),
    ele: smoothEle(ser),
    // filled in by the parts: map by viewSession, the selection by sessionSelection,
    // charts by sessionCharts, selectKm (a click on a chart) by splitsPanel
    map: null, charts: {}, xmode: "km", selectKm: () => { },
  };
}

/** The KPI strip. */
function sessionCards(P) {
  const { a, m, W, IF, ftp, load, cad, avg, L } = P, b10 = P.bw.b10;
  return cards([
    { k: "Distance", v: fkm(a.km) + " km", s: isNum(a.km_diff_pct) && Math.abs(a.km_diff_pct) > 1 ? `track differs ${pct(a.km_diff_pct)}` : "" },
    { k: "Elapsed", v: dur(a.elapsed_s, true) }, { k: "Moving", v: dur(a.moving_s, true) },
    { k: "Paused", v: dur(a.pause_s), s: isNum(a.pause_s) && a.elapsed_s ? (a.pause_s / a.elapsed_s * 100).toFixed(1) + "% of elapsed" : "" },
    { k: "Avg speed (moving)", v: f2(avg), s: "km/h", hl: 1 }, { k: "Avg pace", v: pace(avg), s: "min/km (moving)" },
    { k: "Avg speed (elapsed)", v: f2(a.kmh_elapsed), s: `${pace(a.kmh_elapsed)} /km` },
    ...(b10 ? [{ k: "Best 10 min", v: f2(b10.v), s: !a.hidden && b10.rank && b10.of > 1 ? `km/h · #${f0(b10.rank)} of ${f0(b10.of)}` : "km/h", hl: 1 }] : []),
    ...(L ? [{ k: "Laps", v: `${L.laps.length}`, s: `${f2(L.lap_km)} km circuit` }] : []),
    ...(m.route === "out-back" ? [{ k: "Wind-neutral 10 min", v: f2(m.wind_neutral), s: `out ${pct(m.asym_pct)} vs back`, hl: 1 }] : []),
    ...(a.hr_avg ? [{ k: "Heart rate", v: f0(a.hr_avg), s: `avg · max ${f0(a.hr_max)} bpm` }, { k: "Efficiency", v: a.best10min_kmh ? f2(a.best10min_kmh / a.hr_avg * 100) : "–", s: "best10 / HR ×100" }] : []),
    ...(isNum(W) ? [{ k: "Power", v: f0(W) + " W", s: [isNum(m.np) && `weighted ${f0(m.np)}`, isNum(a.watts_max) && `max ${f0(a.watts_max)} W`].filter(Boolean).join(" · ") || "avg (Strava)", hl: 1 }]
      : a.s_avg_watts ? [{ k: "Power (Strava est.)", v: f0(a.s_avg_watts) + " W", s: "estimated, no power meter" }] : []),
    ...(isNum(IF) ? [{ k: "Intensity", v: IF.toFixed(2), s: `load ${f0(load)} · FTP ${f0(ftp)} W` + (isNum(a.f_if) ? ` · <span title="the intensity and training load your training app computed (its NP ${f0(a.f_np)} W)">app ${a.f_if.toFixed(2)} / ${f0(a.f_tss)}</span>` : "") }] : []),
    ...(isNum(cad) ? [{ k: "Cadence", v: f0(cad), s: `rpm avg${isNum(a.cad_max) ? ` · max ${f0(a.cad_max)}` : ""}` }] : []),
    ...(isNum(W) && a.hr_avg ? [{ k: "Power / HR", v: (W / a.hr_avg).toFixed(2), s: "W per bpm" }] : []),
    { k: "Ascent", v: f0(a.ascent_m) + " m" },
    ...weatherCards(a.cond, a.weather_missing),
    ...(a.cond && isNum(a.cond.adjusted) ? [{ k: "Conditions-adjusted 10 min", v: f2(a.cond.adjusted), s: "wind and slope removed per leg", hl: 1 }] : []),
    ...(a.cal ? [{ k: "Calories", v: `${f0(a.cal.kcal)} kcal`, s: `${esc(a.cal.source)}${isNum(a.cal.hr_kcal) ? ` · HR ${f0(a.cal.hr_kcal)}` : ""}${a.s_calories ? ` · Strava ${f0(a.s_calories)}` : ""}` }]
      : a.s_calories ? [{ k: "Calories", v: f0(a.s_calories), s: `Strava · <a href="#/profile">add your weight</a>` }] : []),
  ]);
}

/** The page skeleton: header, cards, and an empty element per panel. */
function sessionHtml(P) {
  const { a, m, d, prev, next, hasGps, hasHr, hasPw, hasEle, L, FT, ftp } = P;
  return `
    <div class="head">
      <div><h1>${starSlot("sessions", a.id)} ${esc(a.name)}</h1>
        <div class="muted">${esc(a.type)} · ${fdate(d)} · ${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")} · ${esc(a.device || "")}${gearChip(a)} · ${routePill(m.route)}</div></div>
      <div class="headnav">
        <button class="btn" id="cmp" title="Compare with another session">Compare…</button>
        <button class="btn" id="edit" title="Rename, change sport or gear, hide or delete">Edit</button>
        <button class="btn" id="prev" ${prev ? "" : "disabled"}>← previous</button>
        <button class="btn" id="next" ${next ? "" : "disabled"}>next →</button>
      </div>
    </div>
    ${a.hidden ? `<div class="panel hiddenbar"><span>${a.deleted ? `<b>Deleted.</b> ${a.edit && a.edit.file_removed ? "Its imported file was removed; the session stays restorable until the next fresh rebuild." : ""}` : "<b>Hidden.</b>"}
      Left out of stats, lists, maps and segments.</span><button class="btn" id="restore">Restore</button></div>` : ""}
    ${sessionCards(P)}
    <div class="grid ${hasGps ? "g2" : ""}">
      ${hasGps ? `<div class="panel fill"><h2>Route <span class="muted">coloured by speed · drag / scroll to explore · hover the chart to follow along</span></h2><div id="map"></div></div>` : ""}
      <div class="panel selhost"><div id="speed-sel" class="selbox" hidden></div><h2>Speed${hasHr ? ", heart rate" : ""}${hasPw ? ", power" : ""}${hasEle ? " &amp; elevation" : ""}</h2>
        <div class="toolbar"><div class="seg" id="xmode"><button data-x="km" class="on">distance</button><button data-x="t">time</button></div></div>
        <div id="c-speed"></div>
        ${hasPw ? `<div class="subhead"><b>Power</b> <span class="muted small" id="pw-sum"></span></div><div id="c-pw"></div>` : ""}
        ${hasEle ? `<div class="subhead"><b>Elevation</b> <span class="muted small" id="ele-sum"></span></div><div id="c-ele"></div>` : ""}
        <div class="note">Drag across the chart to select a stretch; click to select that km.</div></div>
    </div>
    ${P.bw.show ? `<div class="panel" id="p-bw"><h2>Best speeds <span class="muted">fastest stretch of each length · click to show it</span></h2>
      <div class="grid g2 bw-grid"><div id="c-bw"></div><div class="scroll" id="t-bw"></div></div>
      <div class="note" id="bw-note"></div></div>` : ""}
    ${L ? `<div class="panel"><h2>Laps <span class="muted">${L.laps.length} × ${f2(L.lap_km)} km circuit · click a lap to show it on the map</span></h2>
      <div id="lap-cards"></div>
      <div class="grid g2" style="margin-top:14px"><div id="c-laps"></div><div class="scroll" id="t-laps" style="max-height:520px"></div></div>
      <div class="note">A lap starts each time you cross the gate (◆ on the map) heading the same way. Grey laps are irregular — a shortcut, a GPS dropout or a different line — and are left out of the lap stats.</div></div>` : ""}
    <div class="grid g2 top">
      <div class="panel stick"><h2>Speed per km</h2><div id="c-splits"></div></div>
      <div class="panel"><h2>Splits</h2><div class="scroll" id="t-splits"></div></div>
    </div>
    <div class="grid g2 top">
      ${m.be && Object.keys(m.be).length ? `<div class="panel"><h2>Best efforts <span class="muted">fastest stretch of each distance · click to show it</span></h2><div id="t-be"></div></div>` : ""}
      ${m.route === "out-back" ? `<div class="panel"><h2>Out / back legs</h2><div id="t-legs"></div>
        <div class="note">Wind-neutral averages the two legs' best 10 min. Adjusted first removes each leg's estimated wind and slope effect.</div></div>` : ""}
      ${m.pw ? `<div class="panel"><h2>Power <span class="muted">zones as % of FTP ${f0(ftp)} W (${esc(FT ? FT.note : "none")})</span></h2><div id="p-power"></div></div>` : ""}
      ${hasHr ? `<div class="panel"><h2>Heart-rate zones <span class="muted">HRmax ${f0(a.hrmax || S.hrmax)} bpm · ${a.hrmax ? hrSrc(a.hrmax_src) : `today's, ${hrSrc()}`} · <a href="#/profile">change</a></span></h2><div id="t-zones"></div></div>` : ""}
      <div class="panel" id="p-segs" hidden></div>
      <div class="panel"><h2>Same route, other sessions</h2><div class="scroll" id="t-same"></div></div>
    </div>`;
}

/** Header buttons, weather card, restore. */
function sessionHead(P, app, seq) {
  const { a, m, self, peers, prev, next } = P;
  mountStars(app.querySelector(".head"));
  $("#edit").onclick = () => openEdit(a);
  $("#cmp").onclick = () => comparePicker(self, m, peers);
  if ($("#restore")) $("#restore").onclick = async () => {
    try { await postEdit({ id: String(a.id), restore: true }); await refreshData(); } catch (err) { alert("Could not restore: " + err.message); }
  };
  $("#prev").onclick = () => prev && go(prev.id);
  $("#next").onclick = () => next && go(next.id);
  if (a.weather_missing) fetchWeather(a, seq);
  const wxc = $("#wx-card");  // not awaited: the page is usable meanwhile
  if (wxc) { wxc.onclick = () => weatherDetails(a); wxc.onkeydown = e => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); weatherDetails(a); } }; }
}

async function viewSession(id) {
  const app = $("#app"), seq = routeSeq;
  app.innerHTML = `<p class="muted">Loading session…</p>`;
  const a = await api("activity/" + id);
  if (seq !== routeSeq) return;  // left while loading
  const peers = await loadActs(a.type);
  if (seq !== routeSeq) return;
  const P = sessionPage(a, peers);
  const { m, ser } = P;
  app.innerHTML = sessionHtml(P);
  sessionHead(P, app, seq);
  // indoor: no route panel, the charts take the width
  if (P.hasGps) {
    P.map = sessionMap($("#map"), ser, { turnKm: m.route === "out-back" ? m.turn_km : null, gate: P.L ? P.L.gate : null, wind: a.cond });
    onLeave(() => P.map.destroy());
  }
  // order matters: the map, then the selection (it wraps the map), then whatever selects
  sessionSelection(P);
  sessionCharts(P);
  if (P.L) lapWidgets(P);
  if (P.hasGps) sessionSegments(P.self, P);
  splitsPanel(P);
  if (m.route === "out-back") legsPanel(P);
  if ($("#t-be")) bestEffortsPanel(P);
  if (P.bw.show) bestSpeedsPanel(P);
  if (m.pw) powerPanel(P);
  if (P.hasHr) hrZonesPanel(P);
  sameRoutePanel(P);
}
