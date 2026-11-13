// SPDX-License-Identifier: GPL-3.0-or-later
// Copyright (C) 2026 Pedro Ayala. Rollbook is free software, see LICENSE.
"use strict";
/* Weather formatting, the session's weather card and its enlarged hour-by-hour view. */

// ------------------------------------------------------------------ weather
const fdeg = d => isNum(d) ? COMPASS[Math.round(((d % 360) + 360) % 360 / 45) % 8] : "";
/** "from W" arrow pointing where the wind blows to */
const windArrow = wd => isNum(wd) ? `<span class="warrow" aria-hidden="true" style="transform:rotate(${(wd + 180) % 360}deg)">↑</span>` : "";
const fwind = c => c && isNum(c.ws) ? `${f0(c.ws)} km/h from ${fdeg(c.wd)} ${windArrow(c.wd)}` : "–";
const fsigned = (v, u = "%") => isNum(v) ? (v >= 0 ? "+" : "−") + Math.abs(v).toFixed(1) + u : "–";
/** WMO weather code -> short text (Open-Meteo's table) */
const WMO = { 0: "Clear", 1: "Mainly clear", 2: "Partly cloudy", 3: "Overcast", 45: "Fog", 48: "Rime fog",
  51: "Light drizzle", 53: "Drizzle", 55: "Dense drizzle", 56: "Freezing drizzle", 57: "Freezing drizzle",
  61: "Light rain", 63: "Rain", 65: "Heavy rain", 66: "Freezing rain", 67: "Freezing rain",
  71: "Light snow", 73: "Snow", 75: "Heavy snow", 77: "Snow grains", 80: "Rain showers", 81: "Rain showers",
  82: "Violent showers", 85: "Snow showers", 86: "Snow showers", 95: "Thunderstorm", 96: "Thunderstorm, hail", 99: "Thunderstorm, hail" };
const fsky = code => isNum(code) ? (WMO[code] || `code ${code}`) : "";
/** European AQI bands */
const faqi = v => !isNum(v) ? "" : v <= 20 ? "good" : v <= 40 ? "fair" : v <= 60 ? "moderate" : v <= 80 ? "poor" : v <= 100 ? "very poor" : "extremely poor";
/** The session page's one weather card (cond = conditions() on the server); click opens weatherDetails(). */
function weatherCards(c, missing) {
  const nw = t => `<span class="nw">${t}</span>`;
  if (missing) return [{ k: "Weather", v: S.readonly ? "–" : "…", id: "wx-wait",
    s: `<span id="wx-note" aria-live="polite">${S.readonly ? "not loaded yet" : "fetching from Open-Meteo"}</span>` }];
  if (!c || !isNum(c.temp)) return [];
  const parts = [isNum(c.ws) && nw(fwind(c)),
    c.precip_sum > 0 && nw(`${c.precip_sum.toFixed(1)} mm`), isNum(c.aqi) && nw(`AQI ${f0(c.aqi)}`)].filter(Boolean);
  return [{ k: "Weather ›", v: `${f0(c.temp)}°C <span class="wx-sky">${esc(fsky(c.code))}</span>`, cls: "click wx-card",
    id: "wx-card", button: true, title: "Hour-by-hour weather", s: parts.join(" · ") }];
}

/** Hour-by-hour weather of a session in an enlarged panel: summary, charts, table. */
function weatherDetails(a) {
  const H = a.weather_hours || [], c = a.cond || {};
  if (!H.length) return;
  const t0 = +new Date(a.datetime), t1 = t0 + (a.elapsed_s || 0) * 1000, hx = ms => ms / 3600e3;
  const clock = ms => { const d = new Date(ms); return `${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`; };
  const rows = H.map(h => ({ ...h, x: hx(+new Date(h.t)) }));
  const has = k => rows.some(r => isNum(r[k]));
  const pts = k => rows.filter(r => isNum(r[k])).map(r => ({ x: r.x, y: r[k] }));
  const vals = k => rows.map(r => r[k]).filter(isNum), spread = k => { const v = vals(k); return v.length ? Math.max(...v) - Math.min(...v) : 0; };
  const num = (v, d = 0) => isNum(v) ? v.toFixed(d) : "–";
  const wet = rows.some(r => r.precip > 0);
  const flatAir = spread("rh") < 5 && spread("pmsl") < 2;  // humidity and pressure barely move: no chart
  const steady = k => spread(k) < (k === "rh" ? 5 : 2) ? "steady" : `${f0(Math.min(...vals(k)))}–${f0(Math.max(...vals(k)))}`;
  const range = isNum(c.temp_min) && isNum(c.temp_max) && c.temp_max - c.temp_min >= 1 ? `${f0(c.temp_min)}–${f0(c.temp_max)}°C` : "";
  const stats = [
    { k: "Temperature", v: `${f0(c.temp)}°C`, s: [isNum(c.feels) && `feels ${f0(c.feels)}°C`, range].filter(Boolean).join(" · ") },
    { k: "Sky", v: esc(fsky(c.code)) || "–", s: isNum(c.cloud) ? `cloud ${f0(c.cloud)}%` : "" },
    { k: "Wind", v: isNum(c.ws) ? `${f0(c.ws)} km/h` : "–", s: `${isNum(c.wd) ? `from ${fdeg(c.wd)} ${windArrow(c.wd)}` : ""}${isNum(c.gust_max) ? ` · gusts ${f0(c.gust_max)}` : ""}` },
    { k: "Rain", v: c.precip_sum > 0 ? `${c.precip_sum.toFixed(1)} mm` : isNum(c.precip_sum) ? "dry" : "–", s: "during the session" },
    { k: "Humidity", v: isNum(c.rh) ? `${f0(c.rh)}%` : "–", s: [has("rh") && (spread("rh") < 5 ? "steady" : `${steady("rh")}%`), isNum(c.dew) && `dew point ${f0(c.dew)}°C`].filter(Boolean).join(" · ") },
    { k: "Pressure", v: isNum(c.pmsl) ? `${f0(c.pmsl)} hPa` : "–", s: has("pmsl") ? `sea level · ${steady("pmsl")}` : "sea level" },
    ...(isNum(c.aqi) || isNum(c.pm25) ? [{ k: "Air quality", v: isNum(c.aqi) ? `AQI ${f0(c.aqi)}` : "–", s: [faqi(c.aqi), isNum(c.pm25) && `PM2.5 ${f0(c.pm25)} µg/m³`].filter(Boolean).join(" · ") }] : []),
  ];
  const charts = [["wx-temp", "Temperature", "°C"], ["wx-wind", "Wind", "km/h"],
    ...(wet ? [["wx-rain", "Rain", "mm per hour"]] : spread("cloud") >= 5 ? [["wx-cloud", "Cloud cover", "%"]] : []),
    ...(flatAir ? [] : [["wx-air", "Humidity &amp; pressure", ""]]), ...(has("aqi") ? [["wx-aq", "Air quality", "European AQI"]] : [])];
  const sec = ' class="wx-2"';  // left out on a phone
  const panel = document.createElement("div");
  panel.className = "panel wx-panel";
  panel.innerHTML = `<h2 id="wx-title">Weather <span class="muted">${fdate(new Date(t0))} · ${clock(t0)}–${clock(t1)} · hour by hour at your position</span></h2>
    <div class="wx-stats">${cards(stats)}</div>
    <div class="grid g2 wx-charts">${charts.map(([id, t, u]) => `<div class="wx-chart"><h3>${t}${u ? ` <span class="muted">${u}</span>` : ""}</h3><div id="${id}"></div>${id === "wx-wind" ? `<div class="wx-row" id="wx-dirs"></div>` : id === "wx-rain" ? `<div class="wx-row" id="wx-clouds"><b class="wx-row-k">cloud</b></div>` : ""}</div>`).join("")}</div>
    <div class="scroll" tabindex="0" role="region" aria-label="Hourly weather"><table class="wx-table"><thead><tr><th class="l">hour</th><th class="l wx-2">sky</th><th>°C</th><th${sec}>feels °C</th><th>wind km/h</th><th class="wx-2 wx-3">gusts km/h</th><th>rain mm</th><th${sec}>cloud %</th><th${sec}>humidity %</th><th${sec}>hPa</th>${has("aqi") ? `<th>AQI</th><th${sec}>PM2.5</th><th class="wx-2 wx-3">PM10</th><th class="wx-2 wx-3">O₃</th>` : ""}</tr></thead><tbody>
      ${rows.map(r => { const ms = r.x * 3600e3, inS = ms >= t0 - 1800e3 && ms <= t1 + 1800e3;
        return `<tr class="${inS ? "" : "muted"}"><td class="l num">${clock(ms)}</td><td class="l wx-2">${esc(fsky(r.code))}</td><td>${num(r.temp, 1)}</td><td${sec}>${num(r.feels, 1)}</td>
          <td>${isNum(r.ws) ? `${f0(r.ws)} ${fdeg(r.wd)} ${windArrow(r.wd)}` : "–"}</td><td class="wx-2 wx-3">${num(r.gust)}</td><td>${r.precip > 0 ? r.precip.toFixed(1) : isNum(r.precip) ? "0" : "–"}</td><td${sec}>${num(r.cloud)}</td><td${sec}>${num(r.rh)}</td><td${sec}>${num(r.pmsl)}</td>
          ${has("aqi") ? `<td>${num(r.aqi)}</td><td${sec}>${num(r.pm25, 1)}</td><td class="wx-2 wx-3">${num(r.pm10, 1)}</td><td class="wx-2 wx-3">${num(r.o3)}</td>` : ""}</tr>`; }).join("")}
    </tbody></table></div>
    <div class="note">Open-Meteo model values (ERA5 / ECMWF IFS reanalysis, CAMS air quality) for the ~10 km area you were in each hour, not a measurement on the spot. Rain is the total of the hour before. The shaded band is the session; grey table rows are outside it. Times in your time zone.${c.final ? "" : " Recent: provisional, refreshed by the next import."}</div>`;
  $("#app").appendChild(panel);
  ensureExpander(panel.querySelector("h2"));  // the close button
  panel._onCollapse = () => panel.remove();
  expandPanel(panel);
  const xs = rows.map(r => r.x), x = { type: "linear", fmt: v => clock(v * 3600e3), min: Math.min(...xs), max: Math.max(...xs) };
  // ticks on whole quarter / half / hours, as many as fit the plot (a label is ~48 px)
  x.ticks = iw => {
    const fit = Math.max(2, Math.floor(iw / 48)), st = [0.25, 0.5, 1, 2, 3, 6, 12].find(q => (x.max - x.min) / q <= fit) || 24, out = [];
    for (let v = Math.ceil(x.min / st - 1e-9) * st; v <= x.max + 1e-9; v += st) out.push(v);
    return out;
  };
  // one tooltip row per legend entry, in legend order
  const tip = units => (hx_, hits) => `<b>${clock(hx_ * 3600e3)}</b>` + hits.filter(({ s }) => s.legend !== false)
    .filter(({ s }, i, arr) => arr.findIndex(h => h.s.name === s.name) === i)
    .map(({ s, p }) => `<div class="row"><span>${esc(s.name)}</span><span>${num(p.y, s.dp ?? 1)} ${units[s.name] || ""}</span></div>`).join("");
  // every chart keeps a right axis margin, so the hours line up from chart to chart
  const draw = (id, opt) => { const el_ = $("#" + id); if (!el_) return; chart(el_, { height: 190, x, padRight: true, ...opt }).setBand(hx(t0), hx(t1)); };
  // text under a chart, one item per hour, on the chart's x positions (46 px margins each side)
  const span = x.max - x.min;  // 0 for a single hour: chart() centres it
  const under = (id, f) => { const e = $("#" + id); if (e) e.innerHTML += rows.map(r => { const t = f(r); return t ? `<span style="left:calc(46px + (100% - 92px) * ${(span ? (r.x - x.min) / span : 0.5).toFixed(4)})">${t}</span>` : ""; }).join(""); };
  draw("wx-temp", { y: { fmt: v => `${v < 0 ? "−" : ""}${Math.abs(v)}°`, ticks: 3 }, tip: tip({ temperature: "°C", "feels like": "°C", "dew point": "°C" }), series: [
    { name: "temperature", kind: "line", color: PAL.hue, width: 2.5, data: pts("temp") },
    { name: "feels like", kind: "line", color: PAL.grey, dash: "4 3", data: pts("feels") },
    { name: "dew point", kind: "line", color: PAL.dim, width: 1.5, data: pts("dew") }] });
  draw("wx-wind", { y: { zero: true, ticks: 4 }, tip: tip({ wind: "km/h", gusts: "km/h" }), series: [
    { name: "gusts", kind: "area", color: PAL.grey, opacity: .14, base: 0, legend: false, data: pts("gust") },
    { name: "wind", kind: "line", color: PAL.hue, width: 2.5, dp: 0, data: pts("ws") },
    { name: "gusts", kind: "line", color: PAL.grey, dash: "4 3", dp: 0, data: pts("gust") }] });
  under("wx-dirs", r => isNum(r.wd) ? `${windArrow(r.wd)}<small>${fdeg(r.wd)}</small>` : "");
  if (wet) {
    // rain is a total over the hour before each time: a step, not a slope
    const step = [];
    rows.forEach((r, i) => { if (!i && rows.length > 1) return; const v = r.precip || 0; if (i) step.push({ x: rows[i - 1].x, y: v }); step.push({ x: r.x, y: v }); });
    const rmax = Math.max(...step.map(p => p.y));
    draw("wx-rain", { y: { min: 0, max: rmax > 1 ? Math.ceil(rmax) : 1, ticks: 4 }, series: [
      { name: "rain", kind: "area", color: PAL.hue, opacity: .18, base: 0, legend: false, data: step },
      { name: "rain", kind: "line", color: PAL.hue, width: 2, data: step }], legend: false,
      tip: hx_ => { const r = rows.find(q => q.x === hx_); return `<b>${clock((hx_ - 1) * 3600e3)}–${clock(hx_ * 3600e3)}</b><div class="row"><span>rain</span><span>${r && isNum(r.precip) ? r.precip.toFixed(1) : "–"} mm</span></div>`; } });
    under("wx-clouds", r => isNum(r.cloud) ? `${f0(r.cloud)}%` : "");
  } else draw("wx-cloud", { y: { min: 0, max: 100, ticks: 4 }, tip: tip({ cloud: "%" }), series: [
    { name: "cloud", kind: "area", color: PAL.grey, opacity: .16, base: 0, legend: false, data: pts("cloud") },
    { name: "cloud", kind: "line", color: PAL.grey, width: 2, dp: 0, data: pts("cloud") }], legend: false });
  if (!flatAir) {
    const pv = vals("pmsl"), pmid = pv.length ? (Math.min(...pv) + Math.max(...pv)) / 2 : 1013, half = Math.max(5, spread("pmsl") / 2 + 1);
    draw("wx-air", { y: { min: 0, max: 100, label: "%", ticks: 4 }, y2: { min: Math.floor(pmid - half), max: Math.ceil(pmid + half), label: "hPa", fmt: v => v.toFixed(0), ticks: 4 },
      tip: tip({ humidity: "%", pressure: "hPa" }), series: [
        { name: "humidity", kind: "line", color: PAL.hue, width: 2.5, dp: 0, data: pts("rh") },
        { name: "pressure", kind: "line", axis: "y2", color: PAL.grey, dp: 0, data: pts("pmsl") }] });
  }
  if (has("aqi")) {
    const amax = Math.max(60, Math.ceil((Math.max(...vals("aqi")) + 5) / 20) * 20);
    draw("wx-aq", { y: { min: 0, max: amax, ticks: 4 }, legend: false, tip: tip({ AQI: "" }),
      // each label in the middle of its band (good is 0–20, fair 20–40 …), on the left
      hlines: [[20, "good"], [40, "fair"], [60, "moderate"], [80, "poor"], [100, "very poor"]].filter(([v]) => v <= amax).map(([y, label]) => ({ y, label, labelY: y - 10 })),
      series: [{ name: "AQI", kind: "line", color: PAL.hue, width: 2.5, dp: 0, data: pts("aqi") }] });
  }
}

/** A session opened without weather: have the server fetch and store it
 * (POST /api/weather), poll, and draw the page again once it's there. Once per
 * session per page load, so one with no data never loops. */
const WX_TRIED = new Map();  // id -> what the last try said
async function fetchWeather(a, seq) {
  // a final note: the value stops reading "…" (still loading)
  const note = (t, done = true) => { const el = $("#wx-note"); if (el) el.textContent = t; const v = $("#wx-wait .v"); if (v && done) v.textContent = "–"; };
  if (S.readonly) return;
  if (WX_TRIED.has(a.id) && WX_TRIED.get(a.id) !== "later") { note(WX_TRIED.get(a.id)); return; }
  WX_TRIED.set(a.id, "not available");
  const say = { offline: "not loaded (offline)", busy: "after the import", error: "not available, try later", none: "not available" };
  try {
    const r = await fetch("/api/weather", { method: "POST", headers: { "X-Rollbook": "1", "Content-Type": "application/json" }, body: JSON.stringify({ id: a.id }) });
    let st = (await r.json()).state;
    for (let i = 0; i < 160 && (st === "queued" || st === "running"); i++) {  // the server gives a run 300 s
      await new Promise(res => setTimeout(res, 2000));
      if (seq !== routeSeq) { WX_TRIED.set(a.id, "later"); return; }  // left: may poll again on return
      st = (await api("weather?id=" + a.id)).state;
      if (i === 15) note("still fetching…", false);
    }
    if (seq !== routeSeq) { WX_TRIED.set(a.id, "later"); return; }
    if (st === "done") {
      for (const k of Object.keys(S.acts)) if (k.split("\u0000")[0] === a.type) delete S.acts[k];  // lists carry temp / wind too
      const y = scrollY;
      await route();
      scrollTo(0, y);
    } else if (st === "queued" || st === "running") { WX_TRIED.set(a.id, "later"); note("still fetching, open it again later"); }  // may try again
    else { WX_TRIED.set(a.id, say[st] || "not available"); note(WX_TRIED.get(a.id)); }
  } catch { WX_TRIED.set(a.id, "later"); note("not available, try later"); }  // a network blip: may retry
}
