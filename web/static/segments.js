// SPDX-License-Identifier: GPL-3.0-or-later
// Copyright (C) 2026 Pedro Ayala. Rollbook is free software, see LICENSE.
"use strict";
/* Segments: names, loading, the Segments and Segment pages, a session's segment efforts. */

// ------------------------------------------------------------------ segments

const COMPASS = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"];
const compass = b => COMPASS[Math.round(((b % 360) + 360) % 360 / 45) % 8];
const flen = m => m < 1000 ? `${f0(m)} m` : `${f2(m / 1000)} km`;
const segNames = () => { try { const o = JSON.parse(localStorage.segNames || "{}"); return o && typeof o === "object" && !Array.isArray(o) ? o : {}; } catch { return {}; } };
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
  const sp = rangeSpan(S.range);
  const effs = sg.efforts.filter(e => inSpan(sp, e.a._t));
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
        for (const [q, col] of [[a, PAL.start], [b, PAL.end]]) { ctx.beginPath(); ctx.arc(...q, 6, 0, 7); ctx.fillStyle = col; ctx.fill(); ctx.lineWidth = 2; ctx.strokeStyle = "#000"; ctx.stroke(); }
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
  if (isAll()) return viewPickSport("segments");
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
    <div class="panel"><div class="toolbar"><label class="muted small"><input type="checkbox" id="segfav" ${favOnly ? "checked" : ""}> Favorites only</label><span class="muted small" id="segcnt"></span></div>
      <div class="scroll" style="max-height:640px"><table id="segtbl"></table></div></div>`;
  const sm = segMap($("#segmap"), rows.map(r => r.sg), { width: 3 });
  const cols = [["fav", `<span class="star on" title="Favorite">${ICON.star}</span>`, r => starSlot("segments", r.sg.id)], ["name", "segment", r => `${esc(segName(r.sg))}${r.sg.place && r.sg.place.roads.length ? `<div class="muted small">${esc(r.sg.place.roads.slice(0, 2).join(", "))}</div>` : ""}${r.sg.kind === "loop" ? ` <span class="pill loop">loop</span>` : ""}`, "l"],
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
  if (isAll()) return viewPickSport("segments");
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
    <div class="head"><div><h1>${starSlot("segments", sg.id)} <span id="sgname">${esc(segName(sg))}</span> <button class="btn small" id="rename" title="Rename this segment">Rename</button></h1>
      <div class="muted">${sg.place && sg.place.roads.length ? `via ${esc(sg.place.roads.slice(0, 3).join(", "))}${sg.place.city ? ` · ${esc(sg.place.city)}` : ""} · ` : ""}${sg.kind === "loop" ? "loop" : `heading ${compass(sg.bearing)} (${f0(sg.bearing)}°)`} · starts ${sg.pts[0][0].toFixed(4)}, ${sg.pts[0][1].toFixed(4)}${sg.parts?.length ? ` · made of ${sg.parts.length} shorter segments` : ""}</div></div>
      <div class="headnav">${sg.reverse ? `<a class="btn" href="#/segment/${sg.reverse.id}">⇄ opposite direction</a>` : ""}<a class="btn" href="#/segments">all segments</a></div></div>
    ${st.n ? cards([
      { k: "Length", v: flen(sg.m) },
      { k: "Sessions", v: st.n, s: `${st.effs.length} efforts` },
      { k: "Best", v: dur(st.pr.el, st.pr.el >= 3600), s: `${f2(st.pr.kmh)} km/h · <a href="#/session/${st.pr.aid}">${fdate(st.pr.a._d)}</a>`, hl: 1 },
      { k: "Median", v: dur(st.med, st.med >= 3600), s: `${f2(sg.m / st.med * 3.6)} km/h` },
      { k: "Latest", v: dur(st.latest.el, st.latest.el >= 3600), s: `${fdate(st.latest.a._d)} · ${st.latest === st.pr ? "PR" : "+" + dur(st.latest.el - st.pr.el) + " vs best"}` },
      { k: "Last 5 vs previous 10", v: isNum(trendRecent) && isNum(trendPrev) ? `<span class="${trendRecent >= trendPrev ? "up" : "down"}">${pct((trendRecent / trendPrev - 1) * 100)}</span>` : "–", s: "median speed" },
      ...(sg.reverse ? (() => { const r = segStats(sg.reverse); return r.n ? [{ k: "Opposite direction best", v: dur(r.pr.el, r.pr.el >= 3600), s: `${f2(r.pr.kmh)} km/h · ${pct((st.pr.kmh / r.pr.kmh - 1) * 100)} this way` }] : []; })() : []),
    ]) : `<p class="muted">No efforts in this date range.</p>`}
    <div class="grid g2 top">
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
  const sm = segMap($("#sgmap"), [sg], { markers: true, width: 5, color: () => PAL.hue });
  addEventListener("hashchange", () => sm.map.destroy(), { once: true });
  if (!st.n) return;
  chart($("#c-seg"), {
    x: { type: "time" }, y: { fmt: v => v.toFixed(0), label: "km/h" }, onClick: go,
    series: [{ name: "best per session", kind: "dot", color: PAL.hue, r: 3.5, data: st.best.map(e => ({ x: e.a._t, y: e.kmh, ref: e.aid, color: e === st.pr ? PAL.ink : null })) },
      { name: "rolling median", kind: "line", color: PAL.ink, width: 2, data: rolling(st.best.slice().sort((a, b) => a.a._t - b.a._t).map(e => ({ x: e.a._t, y: e.kmh })), 6), gapX: 60 * DAY }],
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

/** Session page panel: this session's efforts with rank among all sessions.
 * P: the session page; hovering a row previews it on the map and keeps the pick. */
async function sessionSegments(a, P) {
  const host = $("#p-segs");
  let segs;
  try { segs = await loadSegs(a.type, r => { host.hidden = false; host.innerHTML = `<h2>Segments</h2><p class="muted small">finding segments: ${esc(r.phase)}…</p>`; }); }
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
  if (!host.isConnected) return;  // left, or another session's page drew meanwhile
  if (!mine.length) { host.hidden = true; return; }
  host.hidden = false;
  mine.sort((x, y) => x.best.t0 - y.best.t0);
  host.innerHTML = `<h2>Segments <span class="muted">${f0(mine.length)} in this session · hover to show on the map</span></h2><div class="scroll"><table><thead><tr><th class="l">segment</th><th>length</th><th>time</th><th>km/h</th><th>HR</th><th>rank</th><th>vs best</th><th class="l"></th></tr></thead><tbody>` +
    mine.map(m => {
      const isPr = m.rank === 1, newPr = m.prBefore ? m.best.el < m.prBefore.el : true;
      return `<tr class="click${isPr ? " best" : ""}" data-sid="${esc(m.sg.id)}"><td class="l">${isFav("segments", m.sg.id) ? `<span class="star on">${ICON.star}</span> ` : ""}${esc(segName(m.sg))}${m.n > 1 ? ` <span class="muted small">×${f0(m.n)}</span>` : ""}</td><td>${flen(m.sg.m)}</td>
        <td>${dur(m.best.el, m.best.el >= 3600)}</td><td>${f2(m.best.kmh)}</td><td>${f0(m.best.hr)}</td><td>${f0(m.rank)} / ${f0(m.of)}</td>
        <td>${isPr ? "PR" : "+" + dur(m.best.el - m.pr.el)}</td><td class="l">${newPr && m.of > 1 ? `<span class="pill ob">PR at the time</span>` : ""}</td></tr>`;
    }).join("") + `</tbody></table></div><div class="note">Rank is among each session's best effort. "PR at the time" = fastest so far when this session happened.</div>`;
  host.querySelectorAll("tr[data-sid]").forEach(tr => {
    const m = mine.find(m => m.sg.id === tr.dataset.sid);
    tr.onclick = () => { location.hash = "#/segment/" + m.sg.id; };
    tr.onmouseenter = () => P.preview(m.best.t0, m.best.t0 + m.best.el);
    tr.onmouseleave = () => P.preview(null);
  });
}
