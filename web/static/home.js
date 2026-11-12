// SPDX-License-Identifier: GPL-3.0-or-later
// Copyright (C) 2026 Pedro Ayala. Rollbook is free software, see LICENSE.
"use strict";
/* Home (#/): every session of every sport on one screen. The question it answers
 * first: "what have I been doing, across everything?". Lead figure = distance;
 * signature = the ribbon, one column per week of the whole history, stacked by
 * sport. From here each sport drills down to its Progress / Sessions pages.
 * Uses app.js globals (S, api, loadActs, esc, f0, ...). */

// One hue family for the sports, never a rainbow: the main sport gets the data
// orange, the next two its tint and its deep shade, the rest step down in grey.
const SPORT_SHADES = ["#ff7a1a", "#ffb27a", "#b5530d", "#c8c8c8", "#8c8c8c", "#5a5a5a"];
const HOME_RECENT = 12;  // at most; trimmed to end level with the Sports column
const RUNLIKE = /run|walk|hike/i;

/** Sports ordered by km over the year before `today` (what you do now gets the
 * main hue), then by all-time sessions; `types` is /api/types. */
function sportOrder(types, acts, today) {
  const cut = today - 365 * DAY, recent = new Map();
  for (const a of acts) if (a._t >= cut) recent.set(a.type, (recent.get(a.type) || 0) + (a.km || 0));
  return types.map(t => t.type).sort((x, y) => (recent.get(y) || 0) - (recent.get(x) || 0)
    || (types.find(t => t.type === y).n - types.find(t => t.type === x).n));
}
const sportShade = i => SPORT_SHADES[Math.min(i, SPORT_SHADES.length - 1)];

/** Local midnight of the Monday on or before ms. */
function mondayOf(ms) {
  const d = new Date(ms); d.setHours(0, 0, 0, 0);
  d.setDate(d.getDate() - (d.getDay() + 6) % 7);
  return +d;
}
/** km per bucket and sport: buckets of a week (or a day when the span is short) from
 * `from` (default: the first session) to `today`'s bucket, `today` being the end of the
 * range. Returns {step, starts: [ms], km: [[per sport]], n: [count]}. */
function ribbonBuckets(acts, order, today, from) {
  if (!acts.length && from == null) return { step: "week", starts: [], km: [], n: [] };
  const lo = from ?? Math.min(...acts.map(a => a._t)), byDay = (today - lo) / DAY <= 120;
  const start = ms => { if (!byDay) return mondayOf(ms); const d = new Date(ms); d.setHours(0, 0, 0, 0); return +d; };
  const starts = [], idx = new Map();
  // step with setDate, not + 7 * DAY: a day is 23 or 25 h across a DST change
  for (const d = new Date(start(lo)); +d <= today; d.setDate(d.getDate() + (byDay ? 1 : 7))) { idx.set(+d, starts.length); starts.push(+d); }
  const km = starts.map(() => order.map(() => 0)), n = starts.map(() => 0), si = new Map(order.map((t, i) => [t, i]));
  for (const a of acts) {
    const b = idx.get(start(a._t)), s = si.get(a.type);
    if (b == null || s == null) continue;
    km[b][s] += a.km || 0; n[b]++;
  }
  return { step: byDay ? "day" : "week", starts, km, n };
}
/** "Today", "Yesterday", "3 days ago" within a week of `today` (local days), else the date. */
function relDay(date, today) {
  const t = new Date(today); t.setHours(0, 0, 0, 0);
  const d = dayOf(date), k = Math.round((+t - +d) / DAY);
  return k === 0 ? "Today" : k === 1 ? "Yesterday" : k > 1 && k < 7 ? `${k} days ago` : fdate(d);
}
/** Year to date by sport: km from 1 Jan to `today` against the same days a year
 * earlier (29 Feb falls back to 28 Feb). Rows in `order`, then the total. */
function yearToDate(acts, order, today) {
  const t = new Date(today), y = t.getFullYear();
  const end = new Date(y, t.getMonth(), t.getDate() + 1), prevEnd = new Date(y - 1, t.getMonth(), Math.min(t.getDate(), new Date(y - 1, t.getMonth() + 1, 0).getDate()) + 1);
  const span = [[+new Date(y, 0, 1), +end], [+new Date(y - 1, 0, 1), +prevEnd]];
  const rows = order.map(type => ({ type, now: 0, prev: 0 }));
  const at = new Map(order.map((x, i) => [x, rows[i]]));
  for (const a of acts) {
    const r = at.get(a.type); if (!r) continue;
    if (a._t >= span[0][0] && a._t < span[0][1]) r.now += a.km || 0;
    else if (a._t >= span[1][0] && a._t < span[1][1]) r.prev += a.km || 0;
  }
  const total = rows.reduce((s, r) => ({ type: "Total", now: s.now + r.now, prev: s.prev + r.prev }), { now: 0, prev: 0 });
  return { year: y, rows: rows.filter(r => r.now || r.prev), total };
}
/** Change of the km as shown (rounded): "+12%", "new", "–". Volume has no good direction, so no colour. */
function ytdChange(now, prev) {
  const n = Math.round(now), p = Math.round(prev);
  return p ? pct((n / p - 1) * 100) : n ? "new" : "–";
}
/** SVG path of a decoded track ([mx, my, ...], NaN pairs = pen up) fitted into w × h. */
function thumbPath(pts, w, h, pad = 4) {
  const [x0, y0, x1, y1] = bboxOf(pts);
  if (!isFinite(x0)) return "";
  const s = Math.min((w - 2 * pad) / ((x1 - x0) || 1e-9), (h - 2 * pad) / ((y1 - y0) || 1e-9));
  const ox = (w - (x1 - x0) * s) / 2, oy = (h - (y1 - y0) * s) / 2;
  let d = "", pen = false;
  for (let i = 0; i < pts.length; i += 2) {
    if (pts[i] !== pts[i]) { pen = false; continue; }
    d += `${pen ? "L" : "M"}${(ox + (pts[i] - x0) * s).toFixed(1)} ${(oy + (pts[i + 1] - y0) * s).toFixed(1)}`;
    pen = true;
  }
  return d;
}
/** The one figure that says how a session went, by sport: power indoors, pace on foot, else speed. */
function sessionFigure(a) {
  if (/^Virtual/.test(a.type || "")) { const w = pwAvg(a); if (isNum(w)) return `${f0(w)}<small> W</small>`; }
  if (RUNLIKE.test(a.type || "") && a.kmh_moving > 0) return `${pace(a.kmh_moving)}<small> /km</small>`;
  return isNum(a.kmh_moving) ? `${f1(a.kmh_moving)}<small> km/h</small>` : "–";
}
/** Each sport's best on its trusted metric: best 20-min power indoors, else best 10-min speed. */
function sportBest(acts, type) {
  const virt = /^Virtual/.test(type), k = virt ? "pw20m" : "best10min_kmh";
  const b = acts.filter(a => a.type === type && isNum(a[k])).reduce((m, a) => !m || a[k] > m[k] ? a : m, null);
  return b ? { a: b, html: virt ? `${f0(b[k])}<small> W</small>` : `${f1(b[k])}<small> km/h</small>`, label: virt ? "best 20 min" : "best 10 min" } : null;
}
function feedRow(a, shade, trk) {
  const d = trk ? thumbPath(trk, 64, 44) : "";
  const thumb = d ? `<svg class="thumb" viewBox="0 0 64 44" aria-hidden="true"><path d="${d}" stroke="${shade}"/></svg>`
    : `<span class="thumb none" aria-hidden="true">${/^Virtual/.test(a.type || "") ? "indoor" : "no GPS"}</span>`;
  return `<a class="feed-row" href="#/session/${+a.id}">${thumb}<span class="feed-name"><b>${esc(a.name)}</b>
    <span><i class="sw" style="background:${shade}"></i>${esc(a.type)} · ${esc(relDay(a.date, Date.now()))}</span></span>
    <span class="feed-n">${fkm(a.km)}<small> km</small></span><span class="feed-n f-t">${dur(a.moving_s, true)}</span><span class="feed-n f-x">${sessionFigure(a)}</span></a>`;
}
function sportRow(t, i, x) {
  const sp = esc(t.type);
  return `<tr class="click" data-i="${i}" data-sport="${sp}">
    <td class="l"><a class="plink" href="#/progress" data-sport="${sp}"><i class="sw" style="background:${sportShade(i)}"></i>${sp}</a></td>
    <td>${f0(x.n)}</td><td>${fkm(x.km)}</td><td>${hours(x.mv)}</td>
    <td title="${x.best ? esc(x.best.label) : ""}">${x.best ? x.best.html : "–"}</td>
    <td class="muted">${x.last ? esc(relDay(x.last, Date.now())) : "–"}</td>
    <td><a href="#/sessions" data-sport="${sp}" aria-label="${sp} sessions">List</a></td></tr>`;
}

/** The range choices on Home: the fixed ones, last year, an older year or custom range if it is the one set. */
function homeRanges(cur, now) {
  const y = new Date(now).getFullYear();
  const rs = [["all", "All time"], ["ytd", "This year"], ["y" + (y - 1), String(y - 1)], ["365", "12 months"], ["90", "90 days"], ["30", "30 days"]];
  if (/^y\d{4}$/.test(cur) && !rs.some(r => r[0] === cur)) rs.splice(3, 0, [cur, cur.slice(1)]);
  return rs;
}
/** Figure change vs the previous span, from the values as shown: "+8.0%", "new", "–"; grey (volume has no good direction). */
function prevNote(now, prev, unit, label) {
  return label ? `<em>${esc(ytdChange(now, prev))} <span>vs ${fnum(prev)}${unit}${label === true ? "" : " in " + esc(label)}</span></em>` : "";
}

/** Hover a sport (legend, list row) and the ribbon shows only it. */
function ribbonFocus(host, i) {
  const svg = host && host.querySelector("svg.ribbon"); if (!svg) return;
  svg.classList.toggle("focus", i != null);
  svg.querySelectorAll("path[data-i]").forEach(p => p.classList.toggle("on", i != null && +p.dataset.i === i));
}
function drawRibbon(host, R, order, onPick) {
  const W = Math.max(280, host.clientWidth), H = innerWidth <= 720 ? 96 : 150, T = 18, n = R.starts.length;
  if (!n) { host.innerHTML = `<p class="muted small">No sessions in this range.</p>`; return; }
  const tot = R.km.map(r => r.reduce((s, v) => s + v, 0)), top = Math.max(1, ...tot), PW = W - 56, bw = PW / n;  // 56 px: the km labels
  const tk = niceTicks(0, top, 2);
  const y = v => H - v / tk.hi * H;
  const paths = order.map(() => "");
  R.km.forEach((row, b) => { let acc = 0; row.forEach((v, s) => { if (v > 0) { const y0 = y(acc), y1 = y(acc + v); paths[s] += `M${(b * bw).toFixed(2)} ${y1.toFixed(2)}h${Math.max(bw - (bw > 3 ? 1 : 0), .6).toFixed(2)}V${y0.toFixed(2)}H${(b * bw).toFixed(2)}z`; } acc += v; }); });
  // year (or month) labels where one starts, kept apart
  const lab = [], first = new Date(R.starts[0]);
  let last = -1e9;
  R.starts.forEach((ms, b) => {
    const d = new Date(ms), prev = b ? new Date(R.starts[b - 1]) : null;
    const yr = n > 70, mark = yr ? (!prev || d.getFullYear() !== prev.getFullYear()) : (!prev || d.getMonth() !== prev.getMonth());
    if (!mark || b * bw - last < (yr ? 34 : 44)) return;
    last = b * bw; lab.push(`<text x="${(b * bw).toFixed(1)}" y="${H + T - 4}">${yr ? d.getFullYear() : MON[d.getMonth()]}</text>`);
  });
  if (!lab.length) lab.push(`<text x="0" y="${H + T - 4}">${first.getFullYear()}</text>`);
  host.innerHTML = `<svg class="ribbon" viewBox="0 0 ${W} ${H + T}" width="${W}" height="${H + T}" role="img" aria-label="Distance per ${R.step} by sport">
    ${tk.ticks.slice(1).map(v => `<line class="rg" x1="0" x2="${PW}" y1="${y(v).toFixed(1)}" y2="${y(v).toFixed(1)}"/><text class="rt" x="${W}" y="${(y(v) + 4).toFixed(1)}">${f0(v)} km</text>`).join("")}
    ${paths.map((d, s) => d ? `<path data-i="${s}" d="${d}" fill="${sportShade(s)}"/>` : "").join("")}
    <line class="base" x1="0" x2="${PW}" y1="${H}" y2="${H}"/><rect class="hov" y="0" height="${H}" width="${Math.max(bw, 2)}" style="display:none"/>${lab.join("")}</svg>`;
  const svg = host.querySelector("svg"), hov = svg.querySelector(".hov");
  const at = ev => { const r = svg.getBoundingClientRect(); return Math.max(0, Math.min(n - 1, Math.floor((ev.clientX - r.left) / r.width * W / bw))); };
  svg.onmousemove = ev => {
    const b = at(ev), d = new Date(R.starts[b]);
    hov.style.display = ""; hov.setAttribute("x", (b * bw).toFixed(1));
    const rows = order.map((t, s) => [t, s, R.km[b][s]]).filter(r => r[2] > 0);
    showTip(`<b>${R.step === "week" ? "Week of " : ""}${fdate(d)}</b>` + (rows.length ? rows.map(([t, s, v]) => `<div class="row"><span><i class="sw" style="background:${sportShade(s)}"></i>${esc(t)}</span><span>${fkm(v)} km</span></div>`).join("")
      + `<div class="row"><span>${R.n[b]} session${R.n[b] === 1 ? "" : "s"}</span><span>${fkm(tot[b])} km</span></div><div class="muted">click: sessions of ${MON[d.getMonth()]} ${d.getFullYear()}</div>` : `<div class="muted">rest</div>`), ev);
  };
  svg.onmouseleave = () => { hov.style.display = "none"; hideTip(); };
  svg.onclick = ev => { const b = at(ev); if (R.n[b]) { hideTip(); onPick(new Date(R.starts[b])); } };
}

async function viewHome() {
  const app = $("#app"), seq = routeSeq, today = Date.now();
  const all = await loadActs(ALL);
  if (seq !== routeSeq) return;
  const acts = inRange(all);
  if (!all.length) {
    app.innerHTML = `<div class="head"><div><h1>Rollbook</h1><div class="muted">No sessions yet.</div></div></div>
      <div class="panel"><h2>Import your history</h2><p class="muted">A Strava account export or single <b>Export Original</b> files: <button class="btn small" id="home-imp" type="button">Import</button></p></div>`;
    const b = $("#home-imp"); if (b) b.onclick = () => $("#import-btn").click();
    return;
  }
  const order = sportOrder(S.types, all, today), si = new Map(order.map((t, i) => [t, i]));
  const sum = (xs, k) => xs.reduce((s, a) => s + (a[k] || 0), 0);
  const km = sum(acts, "km"), mv = sum(acts, "moving_s"), days = new Set(acts.map(a => a.date)).size;
  const nSports = new Set(acts.map(a => a.type)).size;
  const wk0 = mondayOf(today), week = all.filter(a => a._t >= wk0);
  const recent = acts.slice().sort((a, b) => cmpWhen(b, a)).slice(0, HOME_RECENT);  // the newest in the range
  const name = S.profile && S.profile.name ? S.profile.name.trim() : "";
  const sp = rangeSpan(S.range, today), pv = prevSpan(S.range, today), pl = pv ? prevLabel(S.range, today) : "";
  const before = pv ? all.filter(a => inSpan(pv, a._t)) : [];
  const custom = S.range.startsWith("c:");
  const first = all[0].date, todayD = ymd(new Date(today));
  // Custom starts from the range shown now, so "90 days" can be nudged into any span
  const cs = sp ? [ymd(new Date(sp.lo)), ymd(new Date(Math.min(sp.hi - 1, today)))] : [first, todayD];
  const my = d => `${MON[d.getMonth()]} ${d.getFullYear()}`;
  const span = acts.length ? `${my(acts[0]._d)} → ${my(acts[acts.length - 1]._d)}` : "";
  const latest = all.reduce((b, a) => !b || cmpWhen(a, b) > 0 ? a : b, null);  // "this week" is about now, whatever the range
  const stats = order.map(t => {
    const xs = acts.filter(a => a.type === t);
    return { t, n: xs.length, km: sum(xs, "km"), mv: sum(xs, "moving_s"), last: xs.length ? xs[xs.length - 1].date : null, best: sportBest(xs, t) };
  });
  // the ribbon covers the range itself (a quiet stretch shows as one), to its end or today
  const rEnd = sp ? Math.min(today, sp.hi - 1) : today;
  const R = ribbonBuckets(acts, order, rEnd, sp ? sp.lo : undefined);
  app.innerHTML = `
    <section class="panel hero" aria-labelledby="home-h1">
      <div class="hero-top"><div><h1 id="home-h1">${esc(name || "Your training")}</h1>
        <div class="muted">${S.range === "all" ? "" : esc(rangeLabel(S.range, today)) + " · "}${f0(acts.length)} sessions in ${nSports} sport${nSports === 1 ? "" : "s"}${span && S.range === "all" ? " · " + esc(span) : ""}</div></div>
        <a class="btn" href="#/sessions" id="home-all">All sessions</a></div>
      <div class="hero-range" role="group" aria-label="Date range">
        <div class="seg" id="home-range">${homeRanges(S.range, today).map(([r, l]) => `<button type="button" data-r="${esc(r)}" class="${r === S.range ? "on" : ""}" aria-pressed="${r === S.range}">${esc(l)}</button>`).join("")}<button type="button" data-r="custom" class="${custom ? "on" : ""}" aria-pressed="${!!custom}">Custom</button></div>
        <span class="rg-custom" id="rg-custom"${custom ? "" : " hidden"}><label>From <input type="date" id="rg-from" min="${esc(first)}" max="${esc(todayD)}" value="${esc(cs[0])}"></label>
          <label>to <input type="date" id="rg-to" min="${esc(first)}" max="${esc(todayD)}" value="${esc(cs[1])}"></label><span class="small down" id="rg-msg" role="status"></span></span>
      </div>
      <div class="hero-figs">
        <div class="fig lead"><span>Distance</span><b>${fnum(km)}<small> km</small></b>${prevNote(km, sum(before, "km"), " km", pl)}</div>
        <div class="fig"><span>Moving time</span><b>${fnum(mv / 3600)}<small> h</small></b>${prevNote(mv / 3600, sum(before, "moving_s") / 3600, " h", !!pl)}</div>
        <div class="fig"><span>Sessions</span><b>${fnum(acts.length)}</b>${prevNote(acts.length, before.length, "", !!pl)}</div>
        <div class="fig"><span>Active days</span><b>${fnum(days)}</b>${prevNote(days, new Set(before.map(a => a.date)).size, "", !!pl)}</div>
      </div>
      <p class="finding">This week: ${week.length ? `${week.length} session${week.length === 1 ? "" : "s"}, ${fkm(sum(week, "km"))} km` : "nothing yet"}${latest ? ` · last: <a href="#/session/${+latest.id}">${esc(latest.type)}, ${esc(relDay(latest.date, today).replace(/^(Today|Yesterday|\d+ days ago)$/, m => m.toLowerCase()))}</a>` : ""}</p>
      <div class="legend" id="rib-leg">${stats.filter(x => x.n).map(x => `<span data-i="${si.get(x.t)}"><i style="background:${sportShade(si.get(x.t))}"></i>${esc(x.t)}</span>`).join("")}<span class="muted">km per ${R.step}</span></div>
      <div id="ribbon"></div>
    </section>
    <div class="grid g2 top home-grid">
      <section class="panel"><div class="phead"><h2>${sp && isFinite(sp.hi) && sp.hi < today ? "Latest in this range" : "Recent sessions"}</h2><a class="small" href="#/sessions" data-sport="${ALL}">View all</a></div>
        <div class="feed">${recent.map(a => feedRow(a, sportShade(si.get(a.type) ?? 9), null)).join("") || `<p class="muted">No sessions in this range.</p>`}</div></section>
      <div class="col" id="home-right"><section class="panel"><div class="phead"><h2>Sports <span class="muted">open one for its progress</span></h2></div>
        <div class="scroll"><table class="sports"><thead><tr><th class="l">sport</th><th>sessions</th><th>km</th><th>moving</th><th>best</th><th>last</th><th><span class="vh">list</span></th></tr></thead>
        <tbody>${stats.map((x, i) => x.n ? sportRow({ type: x.t }, i, x) : "").join("") || `<tr><td class="l muted" colspan="7">No sessions in this range.</td></tr>`}</tbody></table></div></section>
        ${ytdPanel(yearToDate(all, order, today), si)}</div>
    </div>
    <section class="panel"><h2>Calendar <span class="muted">all sports, km per day · click a day to open it</span></h2><div id="cal"></div></section>`;

  // the feed ends level with the column beside it (4 rows at least)
  let shown = recent.length;
  const feedP = $("#app .feed").closest(".panel"), right = $("#home-right");
  const rows = [...feedP.querySelectorAll(".feed-row")];
  if (right.offsetTop === feedP.offsetTop) {
    while (shown > 4 && feedP.offsetHeight - right.offsetHeight > (rows[0].offsetHeight + 2) / 2) rows[--shown].remove();
  } else while (shown > 6) rows[--shown].remove();  // one column: the sports come right after
  const toSessions = q => { S.q = q; setSport(ALL); location.hash = "#/sessions"; };
  const rib = $("#ribbon"), draw = () => drawRibbon(rib, R, order, d => toSessions(`${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}`));
  draw();
  rib._redraw = draw;  // sidebar toggle redraws charts at the new width
  rib._chartOpt = { height: 150 };
  calendar($("#cal"), acts, { before: () => setSport(ALL) });
  // a phone shows a few months of each year: scroll to where the range's sessions end
  $("#cal").querySelectorAll(".calrow").forEach(row => {
    const on = row.querySelectorAll("rect.on"), last = on[on.length - 1];
    if (last && row.scrollWidth > row.clientWidth) row.scrollLeft = Math.max(0, +last.getAttribute("x") * row.scrollWidth / +row.querySelector("svg").viewBox.baseVal.width - row.clientWidth + 40);
  });
  const segOn = $("#home-range .on");
  if (segOn) segOn.parentElement.scrollLeft = segOn.offsetLeft - 8;
  // range: a button sets it for every page (top-bar menu too); Custom opens two dates
  $("#home-range").querySelectorAll("button").forEach(b => b.onclick = () => {
    if (b.dataset.r !== "custom") setRange(b.dataset.r);
    else if (!custom) setRange(`c:${cs[0]}:${cs[1]}`);
    else $("#rg-from").focus();
  });
  const applyCustom = () => {
    const f = $("#rg-from").value, t = $("#rg-to").value, r = `c:${f}:${t}`;
    $("#rg-msg").textContent = !f || !t ? "" : rangeSpan(r) ? "" : "The start is after the end.";
    if (rangeSpan(r) && r !== S.range) setRange(r);
  };
  $("#rg-from").onchange = applyCustom; $("#rg-to").onchange = applyCustom;
  // drill down: a link carrying data-sport switches the sport, then the href navigates
  app.querySelectorAll("a[data-sport]").forEach(a => a.addEventListener("click", () => { if (a.dataset.sport === ALL) S.q = ""; setSport(a.dataset.sport); }));
  $("#home-all").addEventListener("click", () => { S.q = ""; setSport(ALL); });
  app.querySelectorAll("table.sports tr[data-sport]").forEach(tr => {
    tr.onclick = e => { if (e.target.closest("a")) return; setSport(tr.dataset.sport); location.hash = "#/progress"; };
    tr.onmouseenter = () => ribbonFocus(rib, +tr.dataset.i); tr.onmouseleave = () => ribbonFocus(rib, null);
  });
  $("#rib-leg").querySelectorAll("span[data-i]").forEach(s => { s.onmouseenter = () => ribbonFocus(rib, +s.dataset.i); s.onmouseleave = () => ribbonFocus(rib, null); });
  // route thumbnails, after the page is up: one request for the feed's sessions
  const ids = recent.filter(a => a.has_gps).map(a => +a.id);
  if (ids.length) api("tracks?ids=" + ids.join(",")).then(r => {
    if (seq !== routeSeq || !r.ids) return;
    const trk = new Map(r.ids.map((id, i) => [id, decodeTrack(r.trk[i])]));
    $("#app .feed").innerHTML = recent.slice(0, shown).map(a => feedRow(a, sportShade(si.get(a.type) ?? 9), trk.get(+a.id))).join("");
  }).catch(() => { });
}

function ytdPanel(Y, si) {
  if (!Y.rows.length) return "";
  const row = (r, cls) => `<tr class="${cls || ""}"><td class="l">${cls ? "Total" : `<i class="sw" style="background:${sportShade(si.get(r.type) ?? 9)}"></i>${esc(r.type)}`}</td>
    <td>${fnum(r.now)}<small> km</small></td><td class="muted">${fnum(r.prev)}<small> km</small></td><td class="muted">${esc(ytdChange(r.now, r.prev))}</td></tr>`;
  return `<section class="panel"><div class="phead"><h2>${Y.year} so far <span class="muted">against the same days of ${Y.year - 1}</span></h2></div>
    <table class="sports ytd"><thead><tr><th class="l">sport</th><th>${Y.year}</th><th>${Y.year - 1}</th><th>change</th></tr></thead>
    <tbody>${Y.rows.map(r => row(r)).join("")}${Y.rows.length > 1 ? row(Y.total, "tot") : ""}</tbody></table></section>`;
}

/** Progress, Map and Segments are per sport: with "All sports" picked, choose one. */
async function viewPickSport(page) {
  const app = $("#app"), seq = routeSeq, all = await loadActs(ALL);
  if (seq !== routeSeq) return;
  const order = sportOrder(S.types, all, Date.now());
  const what = { progress: "progress", map: "map", segments: "segments" }[page] || "progress";
  app.innerHTML = `<div class="head"><div><h1>Pick a sport</h1><div class="muted">${esc(what[0].toUpperCase() + what.slice(1))} is drawn for one sport at a time.</div></div></div>
    <div class="panel"><div class="pick-sports">${order.map((t, i) => { const x = S.types.find(y => y.type === t);
      return `<a href="#/${what}" data-sport="${esc(t)}"><i class="sw" style="background:${sportShade(i)}"></i><b>${esc(t)}</b><span class="muted">${f0(x.n)} sessions · ${fkm(x.km)} km</span></a>`; }).join("")}</div></div>`;
  app.querySelectorAll("a[data-sport]").forEach(a => a.addEventListener("click", e => {
    setSport(a.dataset.sport);
    if (location.hash === a.getAttribute("href")) { e.preventDefault(); route(); }  // same page: no hashchange
  }));
}
