// SPDX-License-Identifier: GPL-3.0-or-later
// Copyright (C) 2026 Pedro Ayala. Rollbook is free software, see LICENSE.
"use strict";
/* Personal records and the records a fresh import set (#17). */

// ------------------------------------------------------------------ records, and new ones after an import (#17)

// best efforts by distance (#6): the server's metrics `be` {label: [s, t0, m0]}, list columns be<label> (seconds)
const BE = [{ k: "400", n: "400 m", m: 400 }, { k: "1k", n: "1 km", m: 1000 }, { k: "5k", n: "5 km", m: 5000 },
  { k: "10k", n: "10 km", m: 10000 }, { k: "21k", n: "Half marathon", short: "21.1 km", m: 21097.5 }, { k: "42k", n: "Marathon", short: "42.2 km", m: 42195 }];

/** Higher-is-better per-session records. grp orders the new-records list; news:false
 * = never announced as new (kmh_moving isn't comparable across 2025-08, watch
 * climbing runs 2-10x high); speed = speed/distance, meaningless on Virtual sports
 * (simulated gradients). */

const RECORD_METRICS = [
  { k: "best5min_kmh", n: "Best 5 min", u: "km/h", grp: 6, speed: true },
  { k: "best10min_kmh", n: "Best 10 min", u: "km/h", grp: 1, speed: true },
  { k: "best20min_kmh", n: "Best 20 min", u: "km/h", grp: 6, speed: true },
  { k: "b30", n: "Best 30 min", u: "km/h", grp: 6, speed: true },
  { k: "b60", n: "Best 60 min", u: "km/h", grp: 6, speed: true },
  { k: "wind_neutral", n: "Best wind-neutral 10 min", u: "km/h", grp: 2, speed: true },
  { k: "kmh_moving", n: "Fastest avg (≥10 km)", u: "km/h", minKm: 10, news: false },
  ...BE.map(b => ({ k: "be" + b.k, n: `Best ${b.short || b.n}`, u: "s", grp: 6, speed: true, low: true })),
  { k: "km", n: "Longest", u: "km", grp: 7, speed: true },
  { k: "moving_s", n: "Longest moving time", u: "", news: false },
  { k: "ascent_m", n: "Most climbing", u: "m", news: false },
  { k: "pw5s", n: "Best 5 s power", u: "W", grp: 5, power: true },
  { k: "pw30s", n: "Best 30 s power", u: "W", grp: 5, power: true },
  { k: "pw1m", n: "Best 1 min power", u: "W", grp: 5, power: true },
  { k: "pw5m", n: "Best 5 min power", u: "W", grp: 5, power: true },
  { k: "pw20m", n: "Best 20 min power", u: "W", grp: 5, power: true },
  { k: "pw30m", n: "Best 30 min power", u: "W", grp: 5, power: true },
  { k: "pw60m", n: "Best 60 min power", u: "W", grp: 5, power: true },
  { k: "np", n: "Highest weighted power", u: "W", grp: 5, power: true },
];
const recFmt = (m, v) => m.u === "s" ? dur(v, v >= 3600) : m.k === "moving_s" ? dur(v, true) : m.u === "W" ? f0(v) : f2(v);
/** A value as shown: a record must still be better after this rounding. */
const recRound = (u, v) => u === "W" || u === "s" ? Math.round(v) : Math.round(v * 100) / 100;
const NR = { minPrior: 5, minPriorLap: 3, segMinOthers: 5, thenDays: 30, segPerSession: 2, cap: 10 };

/** Records set by newly imported sessions, best first, uncapped.
 * acts: every loaded session of the sports involved; newIds: ids of the import;
 * segsByType: {type: segments from loadSegs} (efforts {aid, el, a}).
 * scope "all" = best of the sport now, strictly beating every older session's best;
 * "then" = faster than everything dated before it (not all-time), only for sessions
 * within NR.thenDays of the newest older one, so a backfill of old files stays quiet.
 * Nothing is claimed against fewer than NR.minPrior older sessions with the value. */
function newRecords(acts, newIds, segsByType) {
  const isNew = new Set([...newIds].map(Number)), out = [], segRecs = [];
  const byType = new Map();
  for (const a of acts) { if (!byType.has(a.type)) byType.set(a.type, []); byType.get(a.type).push(a); }
  // items: [{a, v}] of one sport, oldest first
  const judge = (base, items, minPrior, lower) => {
    const r = [], old = items.filter(i => !isNew.has(+i.a.id)), fresh = items.filter(i => isNew.has(+i.a.id));
    if (!fresh.length) return r;
    const beats = (x, y) => lower ? recRound(base.u, x) < recRound(base.u, y) : recRound(base.u, x) > recRound(base.u, y);
    const pick = xs => xs.reduce((b, i) => !b || (lower ? i.v < b.v : i.v > b.v) ? i : b, null);  // tie: earliest
    const rec = (scope, i, p) => ({ ...base, scope, aid: i.a.id, a: i.a, value: i.v, prev: p.v, prevAid: p.a.id, lower: !!lower });
    let top = null;
    if (old.length < minPrior) return r;  // no history (e.g. a first import of this sport): no claims at all
    const p = pick(old), c = pick(fresh);
    if (beats(c.v, p.v)) r.push(rec("all", top = c, p));
    const lastOld = whenOf(old[old.length - 1].a);
    for (const n of fresh) {
      if (n === top || whenOf(n.a) < lastOld - NR.thenDays * DAY) continue;
      const before = items.filter(i => i !== n && cmpWhen(i.a, n.a) < 0);
      if (before.length < minPrior) continue;
      const p = pick(before);
      if (beats(n.v, p.v)) r.push(rec("then", n, p));
    }
    return r;
  };
  for (const [type, list0] of byType) {
    const list = list0.slice().sort(cmpWhen);
    if (!list.some(a => isNew.has(+a.id))) continue;
    const virtual = /^Virtual/.test(type || "");
    for (const m of RECORD_METRICS) {
      if (m.news === false || (virtual && m.speed)) continue;
      const items = list.filter(a => isNum(a[m.k]) && a[m.k] > 0).map(a => ({ a, v: a[m.k] }));
      out.push(...judge({ kind: "metric", k: m.k, n: m.n, u: m.u, grp: m.grp, type }, items, NR.minPrior, !!m.low));
    }
    if (!virtual) for (const c of groupCircuits(list)) {
      const items = c.a.map(a => ({ a, v: a.best_lap_s }));
      out.push(...judge({ kind: "lap", k: "best_lap_s", n: "Best lap", u: "s", grp: 4, type, lap_km: median(c.a.map(a => a.lap_km)) }, items, NR.minPriorLap, true));
    }
    for (const sg of (segsByType && segsByType[type]) || []) {
      const per = new Map();
      for (const e of sg.efforts) if (e.a) { const b = per.get(e.aid); if (!b || e.el < b.el) per.set(e.aid, e); }
      const items = [...per.values()].sort((x, y) => cmpWhen(x.a, y.a)).map(e => ({ a: e.a, v: e.el }));
      segRecs.push(...judge({ kind: "segment", k: "el", n: "Segment", u: "s", grp: 3, type, sgid: sg.id, sg, m: sg.m, full: !!sg.full }, items, NR.segMinOthers, true));
    }
  }
  // "Full" segments overlap each other and the pieces: one fast session would PR on
  // a dozen. Keep NR.segPerSession per session and scope, pieces first, then longest.
  const kept = new Map();
  for (const s of segRecs.sort((x, y) => (x.full - y.full) || (y.m - x.m))) {
    const key = s.aid + "/" + s.scope, n = kept.get(key) || 0;
    if (n < NR.segPerSession) { kept.set(key, n + 1); out.push(s); }
  }
  return out.sort((x, y) => ((x.scope === "all" ? 0 : 1) - (y.scope === "all" ? 0 : 1)) || (x.grp - y.grp) || (whenOf(y.a) - whenOf(x.a)));
}

async function postDismiss(finished) {
  const r = await fetch("/api/last_import/dismiss", { method: "POST", headers: { "X-Rollbook": "1", "Content-Type": "application/json" }, body: JSON.stringify({ finished }) });
  if (r.status === 409) return "stale";
  if (!r.ok) throw new Error((await r.json().catch(() => ({}))).error || r.status);
  return "ok";
}

/** Hide a records panel and void any draw still in flight for it (segments load late). */
function nrCancel(host) { if (host) { host._nrGen = (host._nrGen || 0) + 1; host.hidden = true; } }

/** The last import's new sessions ({id, type}) a page shows: one sport's, or all (sport null). */
function nrScope(news, sport) { return sport ? news.filter(x => x && x.type === sport) : news; }
/** The loaded sessions behind ids, newest first. */
function nrSessions(acts, ids) {
  const want = new Set(ids.map(Number));
  return acts.filter(a => want.has(+a.id)).sort((x, y) => cmpWhen(y, x));
}

/** The last import's summary in host (Import dialog, or the Progress page with
 * opt.dismiss). Ids from the server only select among sessions and segments the
 * API returned; every name is escaped (Strava, OSM and file names are not ours).
 * Resolves after the first draw; segment PRs are added when segments are ready. */
async function newRecordsPanel(host, opt = {}) {
  if (!host) return;
  const gen = host._nrGen = (host._nrGen || 0) + 1, live = () => host._nrGen === gen && host.isConnected;
  let li;
  try { li = S.lastImport ||= await api("last_import"); } catch { if (live()) host.hidden = true; return; }
  if (!live()) return;
  if (!li || !li.finished || (opt.dismiss && li.dismissed)) { host.hidden = true; return; }
  const sport = typeof opt.sport === "string" ? opt.sport : null;
  const news = nrScope((li.new || []).filter(x => okAid(x.id)), sport);
  const total = li.new_total || 0;
  if (sport ? !news.length : !news.length && !total && !li.replaced) { host.hidden = true; return; }
  const ids = news.map(x => Number(x.id)), types = [...new Set(news.map(x => x.type).filter(t => typeof t === "string"))];
  const when = new Date(li.finished * 1000);
  const count = sport ? `${news.length} ${esc(sport)} of ${total} session${total === 1 ? "" : "s"} imported`
    : `${total} session${total === 1 ? "" : "s"} imported${li.replaced ? ` · ${li.replaced} replaced by the export's copy` : ""}`;
  const head = `<div class="toolbar"><h2 style="margin:0">New since the last import <span class="muted">${esc(fdate(when))} · ${count}</span></h2>
    ${opt.dismiss ? `<button class="btn small nr-x" title="Dismiss" aria-label="Dismiss">${ICON.close}</button>` : ""}</div>`;
  let acts = [];
  try { acts = (await Promise.all(types.map(t => loadActs(t)))).flat(); } catch { }
  const segs = {};
  const draw = pending => {
    if (!live()) return;
    let body;
    if (li.partial) body = `<p class="muted small">The import stopped with an error before it finished, so these sessions weren't compared for records.</p>`;
    else if (li.first && !news.length) body = `<p class="muted small">First import: nothing to compare with yet. Records start with the next one.</p>`;
    else {
      const recs = newRecords(acts, ids, segs), shown = recs.slice(0, NR.cap);
      const val = r => r.u === "s" ? dur(r.value, r.value >= 3600) : `${r.u === "W" ? f0(r.value) : f2(r.value)} ${esc(r.u)}`;
      const gain = r => r.u === "s" ? `−${dur(r.prev - r.value)}` : `+${r.u === "W" ? f0(r.value - r.prev) : f2(r.value - r.prev)} ${esc(r.u)}`;
      const what = r => r.kind === "segment" ? (n => `${esc(n)}${n.includes(flen(r.m)) ? "" : ` <span class="muted small">${flen(r.m)}</span>`}`)(segName(r.sg))
        : r.kind === "lap" ? `Best lap <span class="muted small">${r.lap_km < 1 ? f0(r.lap_km * 1000) + " m" : f2(r.lap_km) + " km"} circuit</span>` : esc(r.n);
      body = recs.length ? `<table><thead><tr><th class="l"></th><th class="l">record</th>${sport ? "" : `<th class="l">sport</th>`}<th>value</th><th>gain</th><th class="l">session</th></tr></thead><tbody>` +
        shown.map((r, i) => `<tr class="click" data-i="${i}"><td class="l"><span class="pill ${r.scope === "all" ? "ob" : ""}">${r.scope === "all" ? "record" : "best at the time"}</span></td>
          <td class="l">${what(r)}</td>${sport ? "" : `<td class="l muted">${esc(r.type)}</td>`}<td>${val(r)}</td><td class="up">${gain(r)}</td><td class="l">${esc(fdate(r.a._d))} · ${esc(r.a.name)}</td></tr>`).join("") + `</tbody></table>` +
        (recs.length > shown.length ? `<p class="muted small">+${recs.length - shown.length} more</p>` : "")
        : `<p class="muted small">No new records${pending ? " yet" : ""}.</p>`;
      if (pending) body += `<p class="muted small">checking segments…</p>`;
      // one sport's page: also the sessions themselves, so one without a record still shows
      if (sport) {
        const ss = nrSessions(acts, ids);
        host._sess = ss.slice(0, NR.cap);
        if (ss.length) body += `<p class="small nr-sess">Imported: ` + host._sess.map((a, i) =>
          `<a href="#" data-s="${i}">${esc(fdate(a._d))} · ${esc(a.name)}</a>`).join(", ") +
          (ss.length > host._sess.length ? ` <span class="muted">+${ss.length - host._sess.length} more</span>` : "") + `</p>`;
      }
      body += `<div class="note">"record" = best of that sport now; "best at the time" = faster than every earlier session. Virtual rides: power only.</div>`;
      host._recs = shown;
    }
    host.hidden = false;
    host.innerHTML = head + body;
    host.querySelectorAll("tr[data-i]").forEach(tr => tr.onclick = () => {
      const r = host._recs[+tr.dataset.i]; if (!r) return;
      if ($("#import-dlg").open) $("#import-dlg").close();
      if (r.kind === "segment" && okSgid(r.sgid)) { setSport(r.type); location.hash = "#/segment/" + r.sgid; }
      else if (okAid(r.aid)) go(r.aid);
    });
    host.querySelectorAll("a[data-s]").forEach(el => el.onclick = e => {
      e.preventDefault();
      const a = (host._sess || [])[+el.dataset.s];
      if (a && okAid(a.id)) go(a.id);
    });
    const x = host.querySelector(".nr-x");
    if (x) x.onclick = async () => {
      if (S.readonly) return;
      try {
        const res = await postDismiss(li.finished);
        S.lastImport = null;
        nrCancel(host);  // also voids the segments redraw still pending
        if (res === "stale") return newRecordsPanel(host, opt);  // a newer import: show that one
      } catch (e) { x.title = "could not dismiss: " + e.message; }
    };
  };
  draw(!li.partial && types.length > 0);
  if (li.partial || !types.length) return;
  // in the background: the Import dialog must not stay busy while segments compute
  Promise.all(types.map(t => segsOnce(t).then(s => { segs[t] = s; }).catch(() => { }))).then(() => draw(false));
}
