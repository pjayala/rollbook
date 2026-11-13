// SPDX-License-Identifier: GPL-3.0-or-later
// Copyright (C) 2026 Pedro Ayala. Rollbook is free software, see LICENSE.
"use strict";
/* Client state (S), the sport selector's model and the per-sport session / segment loaders. */

// ------------------------------------------------------------------ state

const S = { segs: {}, segsP: {}, lastImport: null, tracks: {}, mapView: {}, types: [], type: localStorage.type || "Inline Skate", range: localStorage.range || "all", acts: {}, hrmax: null, sort: { k: "date", dir: -1 }, q: "" };
// "All sports" in the sport selector: Home and Sessions show every sport;
// Progress, Map and Segments are per sport and ask for one (viewPickSport).
const ALL = "*";
const isAll = () => S.type === ALL;
/** A real sport for per-sport defaults (new gear, the gear table): the picked one, else the most used. */
const curSport = () => isAll() ? (S.types[0] && S.types[0].type) || "" : S.type;
/** Sessions of one sport (ALL: every sport); withHidden also lists hidden / deleted ones (flagged a.hidden / a.deleted). */
async function loadActs(type, withHidden) {
  const key = withHidden ? type + "\u0000hidden" : type;
  const q = [type === ALL ? "" : "type=" + encodeURIComponent(type), withHidden ? "hidden=1" : ""].filter(Boolean).join("&");
  if (!S.acts[key]) S.acts[key] = (await api("activities" + (q ? "?" + q : ""))).map(a => ({ ...a, _d: dateOf(a), _t: +dateOf(a) }));
  return S.acts[key];
}

const isSkate = () => /skate/i.test(S.type);

const okAid = id => /^\d{1,16}$/.test(String(id));
const okSgid = id => /^[0-9A-Za-z_-]{1,64}$/.test(String(id));
/** Segments of a sport, fetched once per data version (refreshData clears it), so
 * redraws don't start one poll loop each. */
const segsOnce = type => S.segsP[type] ||= loadSegs(type).catch(e => { delete S.segsP[type]; throw e; });
/** Sport switch that sticks: selector, state and storage agree. */
function setSport(t) {
  const sel = $("#type");
  if (sel && ![...sel.options].some(o => o.value === t)) return;
  S.type = localStorage.type = t;
  if (sel) sel.value = t;
}
