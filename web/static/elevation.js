// SPDX-License-Identifier: GPL-3.0-or-later
// Copyright (C) 2026 Pedro Ayala. Rollbook is free software, see LICENSE.
"use strict";
/* A session's elevation for display: smoothing, climb, net change and grade over
 * series rows ([t, km, kmh, hr, ele, …]). Pure, no DOM. */

/** Elevation for display, one value (or null) per series row; null without any.
 * A 7-sample median removes dropouts (some FIT files write 0 m for a sample),
 * then a +-15 s mean takes out barometer jitter. Summed climbing from a watch
 * altimeter still drifts 2-10x above Strava's terrain-model ascent outdoors, so
 * totals use Strava's figure, and splits / selections show net change and grade,
 * which drift barely affects. */
function smoothEle(ser) {
  if (!ser.some(r => r[4] != null)) return null;
  const raw = ser.map(r => r[4]), med = raw.map((_, j) => {
    const w = raw.slice(Math.max(0, j - 3), j + 4).filter(v => v != null).sort((x, y) => x - y);
    return w.length ? w[w.length >> 1] : null;
  });
  const out = new Array(ser.length).fill(null);
  let lo = 0, hi = 0, sum = 0, cnt = 0;
  for (let j = 0; j < ser.length; j++) {
    while (hi < ser.length && ser[hi][0] - ser[j][0] <= 15) { if (med[hi] != null) { sum += med[hi]; cnt++; } hi++; }
    while (ser[j][0] - ser[lo][0] > 15) { if (med[lo] != null) { sum -= med[lo]; cnt--; } lo++; }
    out[j] = cnt ? sum / cnt : null;
  }
  // bridge gaps between sparse readings (GT 6 Pro: one every ~5 s, some
  // longer holes) by linear interpolation in time, up to 3 minutes
  let prev = -1;
  for (let j = 0; j < out.length; j++) {
    if (out[j] == null) continue;
    if (prev >= 0 && j - prev > 1 && ser[j][0] - ser[prev][0] <= 180)
      for (let k = prev + 1; k < j; k++) out[k] = out[prev] + (out[j] - out[prev]) * (ser[k][0] - ser[prev][0]) / ((ser[j][0] - ser[prev][0]) || 1);
    prev = j;
  }
  return out;
}

/** Climb / descent over series indices, 4 m hysteresis; the fallback when Strava has no ascent. */
function eleClimb(ele, i0, i1) {
  let up = 0, dn = 0, ref = null;
  for (let j = i0; j <= i1; j++) {
    const e = ele[j]; if (e == null) continue;
    if (ref == null) { ref = e; continue; }
    if (e - ref >= 4) { up += e - ref; ref = e; } else if (ref - e >= 4) { dn += ref - e; ref = e; }
  }
  return { up, dn };
}

/** Net change (m) and grade (%, only over > 100 m) over series indices. */
function eleNetGrade(ser, ele, i0, i1) {
  const dm = (ser[i1][1] - ser[i0][1]) * 1000;
  if (ele[i0] == null || ele[i1] == null) return { net: null, g: null };
  const net = ele[i1] - ele[i0];
  return { net, g: dm > 100 ? net / dm * 100 : null };
}

/** Grade % at row j, over +-50 m (null on less than 30 m or without elevation). */
function eleGradeAt(ser, ele, j) {
  if (!ele || ele[j] == null) return null;
  let a = j, b = j;
  while (a > 0 && ser[j][1] - ser[a][1] < 0.05) a--;
  while (b < ser.length - 1 && ser[b][1] - ser[j][1] < 0.05) b++;
  const dm = (ser[b][1] - ser[a][1]) * 1000;
  return dm > 30 && ele[a] != null && ele[b] != null ? (ele[b] - ele[a]) / dm * 100 : null;
}

/** km range [k0, k1] -> series index range [i0, i1]. */
function kmIdx(ser, k0, k1) {
  let i0 = 0, i1 = ser.length - 1;
  while (i0 < i1 && ser[i0][1] < k0) i0++;
  while (i1 > i0 && ser[i1][1] > k1) i1--;
  return [i0, i1];
}

const fnet = v => isNum(v) ? (v >= 0 ? "+" : "−") + Math.abs(v).toFixed(0) + " m" : "";
const fgrade = g => isNum(g) ? (g >= 0 ? "+" : "") + g.toFixed(1) + "%" : "";
