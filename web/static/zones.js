// SPDX-License-Identifier: GPL-3.0-or-later
// Copyright (C) 2026 Pedro Ayala. Rollbook is free software, see LICENSE.
"use strict";
/* Heart-rate zones (% of HRmax), FTP choice and power zones (% of FTP), the zone strip. */

// HR zones as % of HRmax; bounds are lower edges of Z2..Z5
// grey warming into the heart-rate rose (PAL.hr), so a zone reads as intensity
const ZONES = [{ n: "Z1", lo: 0, c: "#5c5c5c" }, { n: "Z2", lo: .75, c: "#86636f" }, { n: "Z3", lo: .84, c: "#b05d7a" },
  { n: "Z4", lo: .90, c: "#d95a82" }, { n: "Z5", lo: .95, c: "#ff5c8a" }];
function zoneSecs(hist, hrmax) {
  const z = ZONES.map(() => 0);
  if (!hist || !hrmax) return z;
  for (const [bpm, s] of Object.entries(hist)) {
    const r = +bpm / hrmax; let k = 0;
    for (let i = 0; i < ZONES.length; i++) if (r >= ZONES[i].lo) k = i;
    z[k] += s;
  }
  return z;
}
const zonePct = i => { const lo = Math.round(ZONES[i].lo * 100), hi = i + 1 < ZONES.length ? Math.round(ZONES[i + 1].lo * 100) : null;
  return i === 0 ? `${ZONES[i].n} <${hi}%` : hi ? `${ZONES[i].n} ${lo}–${hi}%` : `${ZONES[i].n} ${lo}%+`; };
const zoneLabel = (i, hrmax) => {
  const lo = Math.round(ZONES[i].lo * hrmax), hi = i + 1 < ZONES.length ? Math.round(ZONES[i + 1].lo * hrmax) - 1 : null;
  return i === 0 ? `${ZONES[i].n} <${hi + 1}` : hi ? `${ZONES[i].n} ${lo}–${hi}` : `${ZONES[i].n} ${lo}+`;
};

/** Average watts: recorded in the file, else Strava's figure for indoor rides (its outdoor one is an estimate). */
const pwAvg = a => isNum(a.watts_avg) ? a.watts_avg : /^Virtual/.test(a.type || "") && isNum(a.s_avg_watts) ? a.s_avg_watts : null;
const FTP_DAYS = 180;
/** Threshold power (FTP) at time t, as {w, src, note}, in order of preference:
 * the profile's FTP; the one the training app used on the latest ride up to t
 * (ROUVY writes NP and IF into each file, FTP = NP / IF: `f_ftp`, `self` wins for
 * its own ride); else 95% of the best 20-min power over the previous FTP_DAYS. */
function ftpAt(acts, t = Date.now(), self) {
  if (S.profile && S.profile.ftp) return { w: S.profile.ftp, src: "profile", note: "your profile" };
  const app = self && isNum(self.f_ftp) ? self : acts.filter(a => isNum(a.f_ftp) && a._t <= t + 60e3 && a._t > t - FTP_DAYS * DAY).reduce((b, a) => !b || a._t > b._t ? a : b, null);
  if (app) { const n = /rouvy/i.test(app.name || "") ? "ROUVY" : /whoosh/i.test(app.name || "") ? "MyWhoosh" : "your training app"; return { w: app.f_ftp, src: "app", note: `set in ${n}` }; }
  const b = acts.filter(a => isNum(a.pw20m) && a._t <= t && a._t > t - FTP_DAYS * DAY).reduce((m, a) => Math.max(m, a.pw20m), 0);
  return b ? { w: 0.95 * b, src: "estimate", note: `95% of best 20 min, last ${FTP_DAYS / 30} months` } : null;
}
const ftpEst = (acts, t, self) => (ftpAt(acts, t, self) || {}).w ?? null;
// Power zones as % of FTP (Coggan's 7 levels); hi = upper edge
const PZONES = [{ n: "Z1", hi: .55, c: "#5a5a5a", d: "active recovery" }, { n: "Z2", hi: .75, c: "#7d7166", d: "endurance" },
  { n: "Z3", hi: .90, c: "#a0785a", d: "tempo" }, { n: "Z4", hi: 1.05, c: "#c77a42", d: "threshold" }, { n: "Z5", hi: 1.20, c: "#ff7a1a", d: "VO2max" },
  { n: "Z6", hi: 1.50, c: "#ffa25e", d: "anaerobic" }, { n: "Z7", hi: Infinity, c: "#ffd0a6", d: "neuromuscular" }];
/** Seconds per power zone from the server's {watts: s} histogram (1 W bins). */
function pzSecs(hist, ftp) {
  const z = PZONES.map(() => 0);
  if (!hist || !ftp) return z;
  for (const [k, sec] of Object.entries(hist)) z[PZONES.findIndex(p => +k <= p.hi * ftp)] += sec;
  return z;
}
const pzLabel = (i, ftp) => { const lo = i ? Math.round(PZONES[i - 1].hi * ftp) + 1 : 0, hi = PZONES[i].hi * ftp; return `${PZONES[i].n} ${isFinite(hi) ? `${lo}–${Math.round(hi)}` : `${lo}+`} W`; };
/** ROUVY-style strip: one column per zone, bar height by share of time. */
function zoneStrip(secs, zones, label) {
  const tot = secs.reduce((x, y) => x + y, 0) || 1, mx = Math.max(...secs) / tot || 1;
  return `<div class="zstrip">` + secs.map((v, i) => { const f = v / tot; return `<div class="zcol" title="${esc(label(i))} · ${dur(v, true)}">
    <div class="zbar"><i style="height:${(f / mx * 100).toFixed(1)}%;background:${zones[i].c}"></i></div><b>${zones[i].n}</b><span>${f >= .005 ? (f * 100).toFixed(0) + " %" : ""}</span></div>`; }).join("") + `</div>`;
}
