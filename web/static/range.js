// SPDX-License-Identifier: GPL-3.0-or-later
// Copyright (C) 2026 Pedro Ayala. Rollbook is free software, see LICENSE.
"use strict";
/* The date range (S.range): parsing, spans, labels, the top-bar menu. */

// ------------------------------------------------------------------ date range
// S.range: "all" | "<days>" (the last N days) | "ytd" | "y<YYYY>" (a calendar year)
// | "c:<YYYY-MM-DD>:<YYYY-MM-DD>" (custom, both days included). Days are local.
const RANGE_DAYS = { 365: "Last 12 months", 180: "Last 6 months", 90: "Last 90 days", 30: "Last 30 days" };
const RANGE_RE = /^(all|365|180|90|30|ytd|y(\d{4})|c:(\d{4}-\d{2}-\d{2}):(\d{4}-\d{2}-\d{2}))$/;
/** A real calendar day "YYYY-MM-DD" (no 31 Feb) as local midnight ms, else null. */
function dayMs(s) {
  if (!/^\d{4}-\d{2}-\d{2}$/.test(s || "")) return null;
  const [y, m, d] = s.split("-").map(Number), t = new Date(y, m - 1, d);
  return t.getFullYear() === y && t.getMonth() === m - 1 && t.getDate() === d ? +t : null;
}
/** The range as [lo, hi) in ms (hi Infinity = up to now and beyond), or null = all time / not a valid range. */
function rangeSpan(r, now = Date.now()) {
  const m = RANGE_RE.exec(String(r));
  if (!m || r === "all") return null;
  if (RANGE_DAYS[r]) return { lo: now - (+r) * DAY, hi: Infinity };
  const y0 = y => +new Date(y, 0, 1);
  if (r === "ytd") return { lo: y0(new Date(now).getFullYear()), hi: Infinity };
  if (m[2]) return { lo: y0(+m[2]), hi: y0(+m[2] + 1) };
  const lo = dayMs(m[3]), last = dayMs(m[4]);
  if (lo == null || last == null || last < lo) return null;
  const end = new Date(last); end.setDate(end.getDate() + 1);  // setDate: a day is 23 or 25 h across DST
  return { lo, hi: +end };
}
/** The same length of time just before the range, for "vs previous" (null for all time / open spans' first day). */
function prevSpan(r, now = Date.now()) {
  const sp = rangeSpan(r, now);
  if (!sp) return null;
  if (/^y\d{4}$/.test(r)) { const y = +r.slice(1); return { lo: +new Date(y - 1, 0, 1), hi: sp.lo }; }
  if (r === "ytd") {  // the same days of last year (29 Feb -> 28 Feb)
    const t = new Date(now), y = t.getFullYear() - 1, d = Math.min(t.getDate(), new Date(y, t.getMonth() + 1, 0).getDate());
    return { lo: +new Date(y, 0, 1), hi: +new Date(y, t.getMonth(), d + 1) };
  }
  if (String(r).startsWith("c:")) {  // as many local days before it (setDate: DST-safe)
    const n = Math.round((sp.hi - sp.lo) / DAY), d = new Date(sp.lo); d.setDate(d.getDate() - n);
    return { lo: +d, hi: sp.lo };
  }
  return { lo: sp.lo - (now - sp.lo), hi: sp.lo };
}
function rangeLabel(r, now = Date.now()) {
  if (RANGE_DAYS[r]) return RANGE_DAYS[r];
  if (r === "ytd") return `This year`;
  if (/^y\d{4}$/.test(r)) return r.slice(1);
  const sp = rangeSpan(r, now);
  if (!sp) return "All time";
  const e = new Date(sp.hi); e.setDate(e.getDate() - 1);
  return `${fdate(new Date(sp.lo))} – ${fdate(e)}`;
}
/** "the previous 90 days", "2025", "the same days of 2025": what prevSpan is, in words. */
function prevLabel(r, now = Date.now()) {
  if (r === "ytd") return `the same days of ${new Date(now).getFullYear() - 1}`;
  if (/^y\d{4}$/.test(r)) return String(+r.slice(1) - 1);
  if (r === "365") return "the 12 months before";
  if (r === "180") return "the 6 months before";
  if (RANGE_DAYS[r]) return `the ${r} days before`;
  const sp = rangeSpan(r, now);
  return sp ? `the ${Math.round((Math.min(sp.hi, now) - sp.lo) / DAY)} days before` : "";
}
const inSpan = (sp, t) => !sp || (t >= sp.lo && t < sp.hi);
function inRange(acts) {
  const sp = rangeSpan(S.range);
  return sp ? acts.filter(a => inSpan(sp, a._t)) : acts;
}
/** The top-bar range menu: the fixed choices, plus the current year / custom range when one is set. */
function fillRange() {
  const sel = $("#range"); if (!sel) return;
  if (!rangeSpan(S.range) && S.range !== "all") S.range = "all";
  const fixed = ["all", "ytd", "365", "180", "90", "30"];
  const opts = fixed.includes(S.range) ? fixed : [...fixed, S.range];
  sel.innerHTML = opts.map(r => `<option value="${esc(r)}">${esc(rangeLabel(r))}</option>`).join("");
  sel.value = S.range;
}
/** Change the range everywhere (menu, storage) and redraw the page. */
function setRange(r) {
  S.range = rangeSpan(r) || r === "all" ? r : "all";
  localStorage.range = S.range;
  fillRange();
  if (!/^#\/(session|compare)\//.test(location.hash)) route();
}
