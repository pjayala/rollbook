// SPDX-License-Identifier: GPL-3.0-or-later
// Copyright (C) 2026 Pedro Ayala. Rollbook is free software, see LICENSE.
"use strict";
/* Shared basics for every page: palette, icons, number / date formatting, the API fetch, the tooltip. */

// ------------------------------------------------------------------ palette
// One data hue, the urethane orange of the wheel, on the series that answers a
// chart's question; white for its trend; greys for context; red only for heart
// rate. Ordered ramps of a hue stand in for rainbow scales (speed, zones).
const PAL = { hue: "#ff7a1a", tint: "#ffb27a", deep: "#b5530d", ink: "#f2f2f2", soft: "#c8c8c8", grey: "#8c8c8c", dim: "#5a5a5a",
  hr: "#ff5c8a", start: "#3fc07a", end: "#e5484d", none: "#1c1c1c", na: "#4a4a4a" };

// outline icons, 16 px, 1.5 px stroke (style.css sizes them)
const ICON = {
  star: '<svg class="ico" viewBox="0 0 16 16" aria-hidden="true"><path d="M8 1.9l1.85 3.85 4.2.55-3.07 2.93.77 4.17L8 11.37 4.25 13.4l.77-4.17L1.95 6.3l4.2-.55z"/></svg>',
  expand: '<svg class="ico" viewBox="0 0 16 16" aria-hidden="true"><path d="M9.5 2.5h4v4M6.5 13.5h-4v-4M13.5 2.5L9 7M2.5 13.5L7 9"/></svg>',
  close: '<svg class="ico" viewBox="0 0 16 16" aria-hidden="true"><path d="M3.5 3.5l9 9M12.5 3.5l-9 9"/></svg>',
  play: '<svg class="ico" viewBox="0 0 16 16" aria-hidden="true"><path d="M5 3.2v9.6L12.8 8z"/></svg>',
  pause: '<svg class="ico" viewBox="0 0 16 16" aria-hidden="true"><path d="M5.5 3.5v9M10.5 3.5v9"/></svg>',
  swap: '<svg class="ico" viewBox="0 0 16 16" aria-hidden="true"><path d="M2.5 5h10M10 2.5L12.5 5 10 7.5M13.5 11h-10M6 8.5L3.5 11 6 13.5"/></svg>',
};

// ------------------------------------------------------------------ utils

const $ = (s, el = document) => el.querySelector(s);
const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const isNum = v => typeof v === "number" && isFinite(v);
const f1 = v => isNum(v) ? v.toFixed(1) : "–";
const f2 = v => isNum(v) ? v.toFixed(2) : "–";
const f0 = v => isNum(v) ? Math.round(v).toString() : "–";
const fkm = v => isNum(v) ? (v >= 100 ? v.toFixed(0) : v.toFixed(2)) : "–";
// 19,276: thousands separated, for the big Home figures
const fnum = v => isNum(v) ? Math.round(v).toLocaleString("en-US") : "–";
function dur(s, forceH) {
  if (!isNum(s)) return "–";
  s = Math.round(s);
  const h = Math.floor(s / 3600), m = Math.floor(s % 3600 / 60), ss = s % 60;
  const p = n => String(n).padStart(2, "0");
  return (h || forceH) ? `${h}:${p(m)}:${p(ss)}` : `${m}:${p(ss)}`;
}
const hours = s => isNum(s) ? (s / 3600).toFixed(s >= 360000 ? 0 : 1) + " h" : "–";
const pace = kmh => isNum(kmh) && kmh > 0 ? dur(3600 / kmh) : "–";  // min:ss per km
const pct = v => isNum(v) ? (v > 0 ? "+" : "") + v.toFixed(1) + "%" : "–";
const dateOf = a => new Date(a.date + "T12:00:00");
/** Start time in ms (`datetime` is ISO with offset); `_t` is noon of the day, too coarse
 * to order two sessions of one day. */
const whenOf = a => { const t = Date.parse(a.datetime || ""); return isFinite(t) ? t : a._t; };
const cmpWhen = (a, b) => whenOf(a) - whenOf(b) || (+a.id) - (+b.id);
const DAY = 86400000;
const MON = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
const fdate = d => `${d.getDate()} ${MON[d.getMonth()]} ${d.getFullYear()}`;
/** "YYYY-MM-DD" as a local date (new Date() would read it as UTC: a day early west of Greenwich) */
const dayOf = s => { const [y, m, d] = String(s).split("-").map(Number); return new Date(y, m - 1, d); };
const fmon = d => `${MON[d.getMonth()]} ${String(d.getFullYear()).slice(2)}`;
function median(xs) { xs = xs.filter(isNum).sort((a, b) => a - b); if (!xs.length) return null; const m = xs.length >> 1; return xs.length % 2 ? xs[m] : (xs[m - 1] + xs[m]) / 2; }
function quantile(xs, q) { xs = xs.filter(isNum).sort((a, b) => a - b); if (!xs.length) return null; const i = (xs.length - 1) * q, lo = Math.floor(i); return xs[lo] + (xs[Math.min(lo + 1, xs.length - 1)] - xs[lo]) * (i - lo); }
function rolling(pts, n) {  // rolling median over the previous n points with a value
  const out = [], buf = [];
  for (const p of pts) { if (!isNum(p.y)) continue; buf.push(p.y); if (buf.length > n) buf.shift(); if (buf.length >= Math.min(n, 3)) out.push({ x: p.x, y: median(buf) }); }
  return out;
}
const api = async p => { const r = await fetch("/api/" + p); const j = await r.json(); if (!r.ok) throw new Error(j.error || r.status); return j; };

// ------------------------------------------------------------------ tooltip

const tip = $("#tip");
function showTip(html, ev) {
  tip.innerHTML = html; tip.hidden = false;
  const w = tip.offsetWidth, h = tip.offsetHeight;
  let x = ev.clientX + 14, y = ev.clientY + 14;
  if (x + w > innerWidth - 8) x = ev.clientX - w - 14;
  if (y + h > innerHeight - 8) y = ev.clientY - h - 14;
  tip.style.left = x + "px"; tip.style.top = y + "px";
}
const hideTip = () => { tip.hidden = true; };
