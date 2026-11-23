// SPDX-License-Identifier: GPL-3.0-or-later
// Copyright (C) 2026 Pedro Ayala. Rollbook is free software, see LICENSE.
"use strict";
/* Boot: loaded last, after every other script, then starts the app. */

// In the desktop app the server stops once its window is gone. pagehide also
// fires on reload, so the launcher waits a few seconds for a new page. The
// ?app=<token> is per launcher, so closing a second window that reuses this
// server doesn't stop the launcher that owns it.
if (new URLSearchParams(location.search).has("app"))
  addEventListener("pagehide", () => navigator.sendBeacon(
    "/api/bye?app=" + encodeURIComponent(new URLSearchParams(location.search).get("app"))));

/** The sport selector from S.types: "All sports" first; keeps the picked sport if it still exists. */
function fillSports() {
  const sel = $("#type"), n = S.types.reduce((s, t) => s + t.n, 0);
  sel.innerHTML = `<option value="${ALL}">All sports (${n})</option>` + S.types.map(t => `<option value="${esc(t.type)}">${esc(t.type)} (${t.n})</option>`).join("");
  if (!isAll() && !S.types.some(t => t.type === S.type)) S.type = S.types[0]?.type || ALL;
  sel.value = S.type;
}
async function boot() {
  let st;
  [S.types, , S.favs, st] = await Promise.all([api("types"), loadHrmax(), api("favorites"), api("status").catch(() => ({})),
    loadGear().catch(() => null)]);
  // opened from a phone via `rollbook web --lan` (without --lan-write): the
  // server refuses every change, so don't offer any
  S.readonly = !!st.readonly;
  document.body.classList.toggle("readonly", S.readonly);
  const sel = $("#type");
  fillSports();
  sel.onchange = () => {
    S.type = localStorage.type = sel.value;
    if (/^#\/(session|compare)\//.test(location.hash)) location.hash = isAll() ? "#/sessions" : "#/progress";
    else if (location.hash.startsWith("#/segment/")) location.hash = "#/segments";
    else route();
  };
  fillRange();
  $("#range").onchange = e => setRange(e.target.value);
  navInit();
  sideInit();
  addEventListener("hashchange", route);
  // compare keeps its replay state: redraw its charts at the new width instead of re-rendering
  let rt; addEventListener("resize", () => { clearTimeout(rt); rt = setTimeout(() => {
    if (expanded) redrawCharts(expanded.panel, true, expanded.natural);  // keep the enlarged panel open
    else if (/^#\/compare\//.test(location.hash)) redrawCharts($("#app"));
    else if (!/^#\/(map|segment)/.test(location.hash)) route();
  }, 200); });
  if (!S.readonly) initImport();
  else $("#import-btn").replaceWith(Object.assign(document.createElement("span"),
    { className: "muted small", textContent: "read-only", title: "opened over the network; start rollbook with --lan-write to allow changes" }));
  await api("profile").then(setProfileBtn).catch(() => { });  // FTP / weight before the first page
  pollStatus();
  route();
}
boot();
