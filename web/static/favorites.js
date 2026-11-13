// SPDX-License-Identifier: GPL-3.0-or-later
// Copyright (C) 2026 Pedro Ayala. Rollbook is free software, see LICENSE.
"use strict";
/* Starred sessions and segments (server-side, data/favorites.json). */

// ------------------------------------------------------------------ favorites

// Stored server-side (data/favorites.json) so the app window and a browser
// tab share them. {sessions: {id: {...}}, segments: {id: {type, start, end, m}}}
S.favs = { sessions: {}, segments: {} };
const isFav = (kind, id) => !!S.favs[kind][String(id)];
async function setFav(kind, id, on, meta) {
  const r = await fetch("/api/favorites", { method: "POST", headers: { "X-Rollbook": "1", "Content-Type": "application/json" },
    body: JSON.stringify({ kind, id: String(id), on, meta }) });
  const j = await r.json();
  if (!r.ok) throw new Error(j.error || r.status);
  S.favs = j;
}
const segMeta = sg => ({ type: S.type, start: sg.pts[0], end: sg.pts[sg.pts.length - 1], m: Math.round(sg.m) });
/** A star toggle; `onchange(on)` runs after the server confirms. */
function starBtn(kind, id, meta, onchange) {
  const b = document.createElement("button");
  b.className = "star"; b.type = "button";
  const paint = () => { const on = isFav(kind, id); b.classList.toggle("on", on); b.innerHTML = ICON.star; b.title = on ? "Remove from favorites" : "Add to favorites"; b.setAttribute("aria-label", b.title); b.setAttribute("aria-pressed", on); };
  b.onclick = async e => {
    e.stopPropagation(); e.preventDefault();
    if (S.readonly) return;
    try { await setFav(kind, id, !isFav(kind, id), typeof meta === "function" ? meta() : meta); paint(); onchange && onchange(isFav(kind, id)); }
    catch (err) { b.title = "could not save: " + err.message; }
  };
  paint();
  return b;
}
/** Replace every <span data-star="kind:id"> placeholder under root with a live star. */
function mountStars(root, metaFor, onchange) {
  root.querySelectorAll("[data-star]").forEach(el => {
    const [kind, id] = el.dataset.star.split(":");
    el.replaceWith(starBtn(kind, id, () => metaFor && metaFor(kind, id), onchange));
  });
}
const starSlot = (kind, id) => `<span data-star="${kind}:${id}"></span>`;

function favPanel(host, all) {
  const fav = all.filter(a => isFav("sessions", a.id)).sort((x, y) => y._t - x._t);
  const segs = Object.entries(S.favs.segments).filter(([, f]) => f.type === S.type);
  if (!fav.length && !segs.length) { host.hidden = true; return; }
  host.hidden = false;
  host.innerHTML = `<h2>Favorites</h2><div class="grid g2"><div>${fav.length ? `<table><thead><tr><th>date</th><th class="l">session</th><th>km</th><th>moving</th><th>avg km/h</th><th>best 10</th></tr></thead><tbody>` +
    fav.map(a => `<tr class="click" data-id="${a.id}"><td class="l">${fdate(a._d)}</td><td class="l">${esc(a.name)}</td><td>${fkm(a.km)}</td><td>${dur(a.moving_s, true)}</td><td>${f2(a.kmh_moving)}</td><td>${f2(a.best10min_kmh)}</td></tr>`).join("") + `</tbody></table>` : `<p class="muted small">No starred ${esc(S.type)} sessions. Star one in the session list or on a session page.</p>`}</div>
    <div id="fav-segs">${segs.length ? `<p class="muted small">loading segments…</p>` : `<p class="muted small">No starred segments yet. Star one on the Segments page.</p>`}</div></div>`;
  host.querySelectorAll("tr[data-id]").forEach(tr => tr.onclick = () => go(tr.dataset.id));
  if (!segs.length) return;
  loadSegs(S.type).then(list => {
    const box = $("#fav-segs"); if (!box) return;
    const rows = list.filter(sg => isFav("segments", sg.id)).map(sg => ({ sg, st: segStats(sg) })).filter(r => r.st.n);
    box.innerHTML = rows.length ? `<table><thead><tr><th class="l">segment</th><th>sessions</th><th>best</th><th>km/h</th><th>latest</th></tr></thead><tbody>` +
      rows.map(r => `<tr class="click" data-sid="${r.sg.id}"><td class="l">${esc(segName(r.sg))}</td><td>${r.st.n}</td><td>${dur(r.st.pr.el, r.st.pr.el >= 3600)}</td><td>${f2(r.st.pr.kmh)}</td>
        <td>${dur(r.st.latest.el, r.st.latest.el >= 3600)} <span class="${r.st.latest === r.st.pr ? "up" : "muted"} small">${r.st.latest === r.st.pr ? "PR" : "+" + dur(r.st.latest.el - r.st.pr.el)}</span></td></tr>`).join("") + `</tbody></table>`
      : `<p class="muted small">Starred segments have no efforts in this range.</p>`;
    box.querySelectorAll("tr[data-sid]").forEach(tr => tr.onclick = () => { location.hash = "#/segment/" + tr.dataset.sid; });
  }).catch(() => { });
}
