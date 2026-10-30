# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Pedro Ayala. Rollbook is free software, see LICENSE.
"""Rollbook dashboard: localhost UI for the rollbook.db built by `rollbook db build`.

Stdlib only. Started via `./rollbook web`; binds to 127.0.0.1 and opens the
database read-only. Static files live in web/static, JSON under /api.

Per-activity metrics that are too costly to compute per request (rolling
windows, out/back legs, wind asymmetry, HR histogram) are derived once from
the samples table and cached in $ROLLBOOK_HOME/cache/web_metrics.json, keyed by
activity id and invalidated when the sample count or METRICS_VERSION changes.
"""

import edits
import errno
import geonames
import profile as athlete
import gzip
import hmac
import ipaddress
import bisect
import json
import re
import math
import os
import secrets
import socket
import sqlite3
import ssl
import statistics
import strava_web
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import weather
from datetime import datetime, timedelta, timezone
from http.cookies import CookieError, SimpleCookie
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

METRICS_VERSION = 16
THR = 0.5  # m/s, pause threshold; must match bin/rollbook
WINDOWS = (30, 45, 60)  # minutes; 5/10/20 already live in the DB via `db enrich`
LEG_WINDOW = 10  # minutes, best effort per out/back leg
MAX_DT = 60  # s, longer sample gaps do not count towards HR time
HR_HOLD = 30  # s, HR is often on its own sparse records; carry it this long
SERIES_MAX = 2500
TRACK_STEP_M = 15  # map tracks keep a point every ~15 m
TRACK_JUMP_M = 500  # longer hops between kept points break the line
LAP_STEP_M = 10  # track resampled to this spacing for lap detection
LAP_GATE_R = 25  # m, how close a pass must come to the gate
LAP_CAND_M = 80  # m, spacing of candidate gates along the track
LAP_MIN_M = 150  # m, shortest lap considered (inline/athletics tracks are ~200-400 m)
LAP_HEAD_COS = 0.7  # pass heading must be within ~45 deg of the gate heading
TRACK_Q = 1e5  # lat/lon quantised to 1e-5 deg (~1 m), then delta-encoded
# best efforts by distance (#6): label, metres. Labels are the API keys (`be<label>`)
BEST_EFFORTS = (("400", 400.0), ("1k", 1000.0), ("5k", 5000.0), ("10k", 10000.0), ("21k", 21097.5), ("42k", 42195.0))
BE_KEYS = tuple("be" + k for k, _ in BEST_EFFORTS)
BE_MAX_MS = 30.0  # m/s; a window holding a faster step is a glitch (bin/rollbook MAX_MS), not an effort
POWER_WINDOWS = (5, 30, 60, 300, 1200, 1800, 3600)  # s, best mean power ("power curve" points)
POWER_KEYS = ("pw5s", "pw30s", "pw1m", "pw5m", "pw20m", "pw30m", "pw60m")  # list columns, same order
PW_BIN = 1  # W, power histogram bin (zones are drawn in the browser, where FTP is known;
#            coarser bins moved ROUVY's zone shares by up to 3 points)
POWER_HOLD = 10  # s, a reading counts until the next one; longer gaps are 0 W (stopped)

# Basemap tiles are the one thing fetched from the network: proxied through
# this server and cached on disk forever, so the browser never talks to a
# third party and a once-viewed area keeps working offline.
TILE_SOURCES = {  # CARTO needs an API key now; the dark look is a CSS filter client-side
    "osm": "https://tile.openstreetmap.org/{z}/{x}/{y}.png",
}
TILE_UA = "Rollbook/1.0 (personal training dashboard, single-user; tiles cached; +https://github.com/pjayala/rollbook)"
TILE_MAXZ = 19
# no inline scripts anywhere; templates use style="" attributes, so inline styles stay allowed
CSP = ("default-src 'self'; script-src 'self'; connect-src 'self'; img-src 'self' data: blob:; "
       "style-src 'self' 'unsafe-inline'; font-src 'self'; object-src 'none'; frame-ancestors 'none'; "
       "base-uri 'none'; form-action 'self'")
STATIC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
MIME = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
        ".css": "text/css; charset=utf-8", ".svg": "image/svg+xml",
        ".png": "image/png", ".ico": "image/x-icon", ".woff2": "font/woff2"}

# columns added later — by `db build` migrations, or by `db enrich` (best<N>min_kmh,
# absent until the first enrich): listed when the DB has them, else NULL
OPT_COLS = ("best5min_kmh", "best10min_kmh", "best20min_kmh",
            "watts_avg", "watts_max", "cad_avg", "cad_max", "kmh_max", "f_avg_watts", "f_np", "f_if", "f_tss", "f_ftp")
LIST_COLS = (
    "id, date, datetime, name, type, device, gear, km, elapsed_s, moving_s, pause_s, "
    "kmh_moving, kmh_elapsed, best30_kmh, "
    "hr_avg, hr_max, ascent_m, descent_m, s_avg_watts, s_max_watts, s_avg_cad, "
    "s_calories, s_rel_effort, km_diff_pct, samples, has_gps"
)


# ------------------------------------------------------------------ geometry


def haversine(a, b):
    r = 6371000.0
    p1, p2 = math.radians(a[0]), math.radians(b[0])
    dp, dl = p2 - p1, math.radians(b[1] - a[1])
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(min(1.0, math.sqrt(h)))


def bearing(a, b):
    p1, p2 = math.radians(a[0]), math.radians(b[0])
    dl = math.radians(b[1] - a[1])
    x = math.sin(dl) * math.cos(p2)
    y = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return (math.degrees(math.atan2(x, y)) + 360) % 360


def valid_ll(lat, lon):
    return lat is not None and lon is not None and not (abs(lat) < 1e-6 and abs(lon) < 1e-6)


# ------------------------------------------------------------------ metrics


def best_window(t, d, i0, i1, span):
    """Peak km/h over any `span`-second window inside samples [i0, i1].

    Same definition as `db enrich`: distance over elapsed time, window must
    cover at least 90% of the span.
    """
    best, lo = None, i0
    for hi in range(i0, i1 + 1):
        while t[hi] - t[lo] > span:
            lo += 1
        dt = t[hi] - t[lo]
        if dt >= span * 0.9 and dt > 0:
            v = (d[hi] - d[lo]) / dt * 3.6
            if best is None or v > best:
                best = v
    return best


def best_effort(t, d, D):
    """Fastest elapsed time over D metres anywhere in a track: (seconds, start t, start m).

    Time as a function of distance is piecewise linear, so f(x) = T_first(x + D) -
    T_last(x) is too, with breaks only where x or x + D is a sample's distance: its
    minimum is exact when f is evaluated at those breakpoints. On a plateau (a pause)
    the start is the moment it moves on and the end the first arrival; pauses inside
    the window count, as with Strava. Equal timestamps keep the last sample (no
    distance in zero time); a window holding a step faster than BE_MAX_MS (an
    odometer or GPS spike) is skipped. Ties keep the earliest start. None if the
    track is shorter than D.
    """
    tt, dd = [], []
    for a, b in zip(t, d):
        if b is None:
            continue
        b = max(b, dd[-1]) if dd else b  # distance never runs backwards
        if tt and a <= tt[-1]:
            dd[-1] = max(dd[-1], b)
            continue
        tt.append(a); dd.append(b)
    n = len(tt)
    if n < 2:
        return None
    total = dd[-1] - dd[0]
    if total < D * (1 - 1e-9):
        return None
    D = min(D, total)
    bad = [0]  # prefix count of impossible steps
    for k in range(n - 1):
        bad.append(bad[-1] + ((dd[k + 1] - dd[k]) / (tt[k + 1] - tt[k]) > BE_MAX_MS))

    def t_last(x):  # leaving distance x (the end of a plateau)
        i = bisect.bisect_right(dd, x) - 1
        if i >= n - 1:
            return tt[-1], n - 1
        return tt[i] + (x - dd[i]) / (dd[i + 1] - dd[i]) * (tt[i + 1] - tt[i]), i

    def t_first(y):  # first arrival at distance y
        j = bisect.bisect_left(dd, y)
        if j <= 0:
            return tt[0], 0
        if j >= n:
            return tt[-1], n - 1
        return tt[j - 1] + (y - dd[j - 1]) / (dd[j] - dd[j - 1]) * (tt[j] - tt[j - 1]), j

    hi_x = max(dd[0], dd[-1] - D)
    xs = sorted({x for x in dd if x <= hi_x + 1e-9} | {y - D for y in dd if y - D >= dd[0] - 1e-9})
    best = None
    for x in xs:
        x = max(dd[0], min(x, hi_x))
        t0, i = t_last(x)
        t1, j = t_first(x + D)
        if bad[j] - bad[i]:
            continue
        el = t1 - t0
        if el > 0 and (best is None or el < best[0] - 1e-9):
            best = (el, t0, x)
    return None if best is None else (round(best[0], 1), round(best[1], 1), round(best[2], 1))


def best_efforts(t, d, typ=""):
    """{label: [seconds, start t, start m]} for the BEST_EFFORTS a sport uses."""
    out = {}
    for k, D in BEST_EFFORTS:
        if k == "400" and "Ride" in typ:  # 400 m on a bike is noise
            continue
        r = best_effort(t, d, D)
        if r:
            out[k] = list(r)
    return out


def moving_kmh(t, d, i0, i1):
    mv = 0.0
    for i in range(i0, i1):
        dt = t[i + 1] - t[i]
        if dt > 0 and (d[i + 1] - d[i]) / dt >= THR:
            mv += dt
    return ((d[i1] - d[i0]) / 1000) / (mv / 3600) if mv else None, mv


def _ele_placeholder(rows):
    """A filler altitude (-1 or 0 m) interleaved with real readings, or None.

    It counts as filler if it is >= 20% of samples and the other readings sit
    > 10 m away from it, i.e. the session clearly wasn't at that height.
    """
    v = [r[3] for r in rows if r[3] is not None]
    if len(v) < 50:
        return None
    for cand in (-1.0, 0.0):
        n = sum(1 for e in v if e == cand)
        rest = [e for e in v if e != cand]
        if n >= 0.2 * len(v) and len(rest) >= 10 and abs(statistics.median(rest) - cand) > 10:
            return cand
    return None


def has_col(db, table, col, schema="main"):
    """Columns added by `db build` migrations are missing from an older DB.
    schema=None resolves like a query would (the edits overlay's TEMP view)."""
    pragma = f"PRAGMA {schema}.table_info({table})" if schema else f"PRAGMA table_info({table})"
    return any(r[1] == col for r in db.execute(pragma))


def load_track(db, aid):
    """Sample rows (t, lat, lon, ele, hr, cad, dist_m, watts), plus t and scaled distance."""
    km, km_calc = db.execute("SELECT km, km_calc FROM activities_all WHERE id=?", (aid,)).fetchone()
    scale = (km / km_calc) if (km and km_calc and km_calc > 0) else 1.0
    w = "watts" if has_col(db, "samples", "watts") else "NULL"
    rows = db.execute(
        f"SELECT t, lat, lon, ele, hr, cad, dist_m, {w} FROM samples WHERE activity_id=? ORDER BY t",
        (aid,),
    ).fetchall()
    t = [r[0] for r in rows]
    d, last = [], 0.0
    for r in rows:  # forward-fill so cumulative distance never has holes
        if r[6] is not None:
            last = r[6] * scale
        d.append(last)
    # HR arrives on its own records in many FIT files, so most samples carry
    # none; hold the last reading briefly instead of treating it as missing.
    fake = _ele_placeholder(rows)
    out, hr, ht = [], None, None
    for r in rows:
        if (r[3] == 0 and r[1] is None) or (fake is not None and r[3] == fake):
            # no-fix 0 m, or the GT 6 Pro's -1 m filler between real altitude
            # readings (it writes one about every 5 s): not a measurement
            r = (*r[:3], None, *r[4:])
        if r[4]:
            hr, ht = r[4], r[0]
        elif hr is not None and r[0] - ht <= HR_HOLD:
            r = (*r[:4], hr, *r[5:])
        out.append(r)
    return out, t, d


def compact_track(rows):
    """Downsampled GPS line as a flat delta-encoded int list.

    Layout: [lat0, lon0, dlat, dlon, ...] in 1e-5 degrees. A pair of (0, 0)
    deltas never occurs naturally (points are >= TRACK_STEP_M apart), so it is
    used as a pen-up marker before a jump.
    """
    out, last, plat, plon = [], None, 0, 0
    for r in rows:
        lat, lon = r[1], r[2]
        if not valid_ll(lat, lon):
            continue
        if last is not None:
            dy = (lat - last[0]) * 111320.0
            dx = (lon - last[1]) * 111320.0 * math.cos(math.radians(lat))
            dist = math.hypot(dx, dy)
            if dist < TRACK_STEP_M:
                continue
            if dist > TRACK_JUMP_M:
                out += [0, 0]
        qa, qo = round(lat * TRACK_Q), round(lon * TRACK_Q)
        out += [qa, qo] if not out else [qa - plat, qo - plon]
        plat, plon, last = qa, qo, (lat, lon)
    return out if len(out) >= 4 else None


def _resample(rows, t, d):
    """GPS points in local metres, resampled every LAP_STEP_M along the track.

    Returns [(x, y, t, d, s)], with t/d interpolated from the original
    samples and s the along-track GPS distance.
    """
    pts = [(i, r[1], r[2]) for i, r in enumerate(rows) if valid_ll(r[1], r[2])]
    if len(pts) < 30:
        return None, None
    lat0, lon0 = pts[0][1], pts[0][2]
    kx, ky = 111320.0 * math.cos(math.radians(lat0)), 110540.0
    xy = [((lo - lon0) * kx, (la - lat0) * ky, i) for i, la, lo in pts]
    x, y, i = xy[0]
    out, s = [(x, y, t[i], d[i], 0.0)], 0.0
    for (px, py, pi), (cx, cy, ci) in zip(xy, xy[1:]):
        L = math.hypot(cx - px, cy - py)
        if L > TRACK_JUMP_M:  # recording gap: jump, don't interpolate across it
            s += L
            out.append((cx, cy, t[ci], d[ci], s))
            continue
        n = int(L // LAP_STEP_M)
        for k in range(1, n + 1):
            f = k * LAP_STEP_M / L
            out.append((px + (cx - px) * f, py + (cy - py) * f,
                        t[pi] + (t[ci] - t[pi]) * f, d[pi] + (d[ci] - d[pi]) * f, s + k * LAP_STEP_M))
        s += L
        if L - n * LAP_STEP_M > 1e-6:
            out.append((cx, cy, t[ci], d[ci], s))
    return out, (lat0, lon0, kx, ky)


def _seg_stats(rows, t, d, t0, t1):
    """moving time, pause, hr, ascent for original samples within [t0, t1]."""
    mv = hrw = hrt = up = 0.0
    last_e = None
    for i in range(len(t) - 1):
        a, b = max(t[i], t0), min(t[i + 1], t1)
        if b <= a:
            if t[i] >= t1:
                break
            continue
        dt = t[i + 1] - t[i]
        if dt > 0 and (d[i + 1] - d[i]) / dt >= THR:
            mv += b - a
        hr = rows[i][4]
        if hr and dt <= MAX_DT:
            hrw += hr * (b - a)
            hrt += b - a
        e = rows[i][3]
        if e is not None:
            if last_e is not None and e > last_e:
                up += e - last_e
            last_e = e
    return mv, (hrw / hrt if hrt else None), up


def find_laps(rows, t, d):
    """Detect a repeated circuit: a gate passed >= 3 times in the same direction.

    Candidate gates are sampled along the track; for each, passes are
    crossings of the line through the gate perpendicular to its heading,
    within LAP_GATE_R and heading-aligned (so the back leg of an out-and-back
    never counts). The gate with the most laps of consistent length wins,
    ties go to lower length spread, then to the gate nearest the start.
    """
    R, geo = _resample(rows, t, d)
    if not R or R[-1][4] < 3 * LAP_MIN_M:
        return None
    n = len(R)
    W = 2  # heading over +-20 m
    head = []
    for j in range(n):
        a, b = R[max(0, j - W)], R[min(n - 1, j + W)]
        hx, hy = b[0] - a[0], b[1] - a[1]
        L = math.hypot(hx, hy)
        head.append((hx / L, hy / L) if L > 5 else None)
    cell = LAP_GATE_R
    grid = {}
    for j, p in enumerate(R):
        grid.setdefault((int(p[0] // cell), int(p[1] // cell)), []).append(j)
    best = None
    step = max(1, int(LAP_CAND_M / LAP_STEP_M))
    for c in range(W, n - W, step):
        hc = head[c]
        if hc is None:
            continue
        gx, gy = R[c][0], R[c][1]
        ix, iy = int(gx // cell), int(gy // cell)
        near = sorted(j for dx in (-1, 0, 1) for dy in (-1, 0, 1)
                      for j in grid.get((ix + dx, iy + dy), ())
                      if head[j] and head[j][0] * hc[0] + head[j][1] * hc[1] >= LAP_HEAD_COS
                      and math.hypot(R[j][0] - gx, R[j][1] - gy) <= LAP_GATE_R)
        if len(near) < 3:
            continue
        # group into passes, then find the actual gate-line crossing in each
        groups, g = [], [near[0]]
        for j in near[1:]:
            if j - g[-1] <= 3:
                g.append(j)
            else:
                groups.append(g)
                g = [j]
        groups.append(g)
        passes = []
        for g in groups:
            hit = None
            for j in range(max(0, g[0] - 1), min(n - 1, g[-1] + 1)):
                p, q = R[j], R[j + 1]
                ap = (p[0] - gx) * hc[0] + (p[1] - gy) * hc[1]
                aq = (q[0] - gx) * hc[0] + (q[1] - gy) * hc[1]
                if ap < 0 <= aq:
                    f = -ap / (aq - ap) if aq != ap else 0
                    hit = tuple(p[k] + (q[k] - p[k]) * f for k in (2, 3, 4))
                    break
            if hit is None:
                j = min(g, key=lambda j: abs((R[j][0] - gx) * hc[0] + (R[j][1] - gy) * hc[1]))
                hit = (R[j][2], R[j][3], R[j][4])
            if not passes or hit[2] - passes[-1][2] >= LAP_MIN_M:
                passes.append(hit)
        if len(passes) < 3:
            continue
        lens = [b[2] - a[2] for a, b in zip(passes, passes[1:])]
        med = statistics.median(lens)
        regular = sum(1 for L in lens if 0.8 * med <= L <= 1.25 * med)
        cv = statistics.pstdev(lens) / statistics.mean(lens)
        score = (regular, -round(cv, 2), -math.hypot(gx, gy))
        if regular >= 2 and (best is None or score > best[0]):
            best = (score, passes, c, med)
    if best is None:
        return None
    _, passes, c, med = best
    laps = []
    for k, (a, b) in enumerate(zip(passes, passes[1:])):
        mv, hr, up = _seg_stats(rows, t, d, a[0], b[0])
        km = (b[1] - a[1]) / 1000
        laps.append({
            "n": k + 1, "t0": a[0], "t1": b[0], "km": km, "elapsed": b[0] - a[0],
            "moving": mv, "pause": max(0.0, b[0] - a[0] - mv),
            "kmh": km / (mv / 3600) if mv else None,
            "kmh_elapsed": km / ((b[0] - a[0]) / 3600) if b[0] > a[0] else None,
            "hr": hr, "ascent": up,
            "regular": 0.8 * med <= (b[2] - a[2]) <= 1.25 * med,
        })
    # odometer cross-check: GPS geometry can look like a full lap while the
    # distance channel says otherwise (e.g. recording stopped mid-lap)
    med_km = statistics.median(x["km"] for x in laps if x["regular"])
    for x in laps:
        x["regular"] = x["regular"] and 0.85 * med_km <= x["km"] <= 1.15 * med_km
        # counted = usable for lap stats: regular and not dominated by a stop
        x["counted"] = bool(x["regular"] and x["kmh"] and x["pause"] <= max(10.0, 0.15 * x["elapsed"]))
    lat0, lon0, kx, ky = geo
    gx, gy = R[c][0], R[c][1]
    # circuit centre (mean of one counted lap's points): unlike the gate, it
    # doesn't depend on where along the loop the gate happened to be chosen
    ref = next((x for x in laps if x["counted"]), laps[0])
    lp = [(p[0], p[1]) for p in R if ref["t0"] <= p[2] <= ref["t1"]] or [(gx, gy)]
    mx, my = sum(p[0] for p in lp) / len(lp), sum(p[1] for p in lp) / len(lp)
    hc = head[c]
    first, last = passes[0], passes[-1]
    return {
        "gate": [lat0 + gy / ky, lon0 + gx / kx], "center": [lat0 + my / ky, lon0 + mx / kx], "gate_heading": (math.degrees(math.atan2(hc[0], hc[1])) + 360) % 360,
        "lap_km": statistics.median(x["km"] for x in laps if x["regular"]),
        "laps": laps,
        "pre": {"t1": first[0], "km": first[1] / 1000},
        "post": {"t0": last[0], "km": (d[-1] - last[1]) / 1000},
    }


def power_grid(rows, t):
    """Power on a 1 s grid from t[0]: each reading holds until the next one,
    for at most POWER_HOLD s (a paused trainer stops recording: that's 0 W).
    None when the session has no power readings."""
    pw = [(t[i], r[7]) for i, r in enumerate(rows) if len(r) > 7 and r[7] is not None]
    if len(pw) < 2:
        return None
    g = [0.0] * (int(t[-1] - t[0]) + 1)
    for (ta, wa), (tb, _) in zip(pw, pw[1:] + [(pw[-1][0] + 1, None)]):
        for s in range(int(ta - t[0]), int(min(tb, ta + POWER_HOLD) - t[0])):
            if s < len(g):
                g[s] = wa
    return g


def zero_power_s(rows, t):
    """Seconds spent at a recorded 0 W (not pedalling). Each reading counts
    until the next one, at most POWER_HOLD s, the same rule as power_grid; the
    0 W that power_grid puts into longer recording gaps is not counted."""
    pw = [(t[i], r[7]) for i, r in enumerate(rows) if len(r) > 7 and r[7] is not None]
    return sum(min(tb - ta, POWER_HOLD) for (ta, wa), (tb, _) in zip(pw, pw[1:] + [(pw[-1][0] + 1, None)])
               if wa == 0) if pw else None


def power_hist(g):
    """Seconds per PW_BIN-watt bin over the 1 s grid, {bin_start: s}."""
    h = {}
    for w in g:
        k = int(w // PW_BIN) * PW_BIN
        h[k] = h.get(k, 0) + 1
    return h


def best_power(g, span):
    """Highest mean power over any `span`-second window of the 1 s grid."""
    if not g or len(g) < span:
        return None
    acc = sum(g[:span])
    best = acc
    for i in range(span, len(g)):
        acc += g[i] - g[i - span]
        best = max(best, acc)
    return best / span


def normalized_power(g):
    """Coggan NP: 30 s rolling mean, 4th power, mean, 4th root (>= 5 min only)."""
    if not g or len(g) < 300:
        return None
    acc, q = sum(g[:30]), []
    q.append((acc / 30) ** 4)
    for i in range(30, len(g)):
        acc += g[i] - g[i - 30]
        q.append((acc / 30) ** 4)
    return (sum(q) / len(q)) ** 0.25


def derive(db, aid):
    rows, t, d = load_track(db, aid)
    out: dict = {"n": len(rows)}
    if len(rows) < 3:
        return out
    for w in WINDOWS:
        out[f"b{w}"] = best_window(t, d, 0, len(t) - 1, w * 60)

    # HR histogram, 1-bpm bins, time-weighted
    hist = {}
    for i in range(len(rows) - 1):
        hr = rows[i][4]
        dt = t[i + 1] - t[i]
        if hr and 0 < dt <= MAX_DT:
            hist[int(hr)] = hist.get(int(hr), 0) + dt
    out["hr_hist"] = hist

    g = power_grid(rows, t)
    if g:
        out["pw"] = {str(w): best_power(g, w) for w in POWER_WINDOWS}
        out["np"] = normalized_power(g)
        out["pw_hist"] = power_hist(g)
        out["pw_zero_s"] = zero_power_s(rows, t)

    typ = db.execute("SELECT type FROM activities_all WHERE id=?", (aid,)).fetchone()[0] or ""
    if typ.startswith("Virtual"):  # simulated GPS, no wind, gradients are fake
        out["route"] = "virtual"
        return out
    # before the indoor return: a treadmill run's distance counts too
    be = best_efforts(t, d, typ)
    if be:
        out["be"] = be
    valid = [i for i, r in enumerate(rows) if valid_ll(r[1], r[2])]
    if len(valid) < 20:
        out["route"] = "indoor"
        return out
    out["trk"] = compact_track(rows)
    out["ll0"] = [rows[valid[0]][1], rows[valid[0]][2]]
    out["dur_s"] = t[-1] - t[0]
    o = (rows[valid[0]][1], rows[valid[0]][2])
    far = max(valid, key=lambda i: haversine(o, (rows[i][1], rows[i][2])))
    endp = (rows[valid[-1]][1], rows[valid[-1]][2])
    farp = (rows[far][1], rows[far][2])
    crow = haversine(o, farp) / 1000
    endgap = haversine(o, endp) / 1000
    total_km = d[-1] / 1000
    out.update(crow_km=crow, end_gap_km=endgap, bearing_out=bearing(o, farp), turn_km=d[far] / 1000)
    laps = find_laps(rows, t, d)
    if laps:
        out["laps"] = laps

    if endgap > max(1.0, 0.25 * crow):
        out["route"] = "one-way"
        return out
    if total_km <= 0 or crow < 0.5 * total_km / 2 or far < valid[0] + 10 or far > valid[-1] - 10:
        out["route"] = "loop"
        return out
    out["route"] = "out-back"
    n = len(t) - 1
    vo, mo = moving_kmh(t, d, 0, far)
    vb, mb = moving_kmh(t, d, far, n)
    bo = best_window(t, d, 0, far, LEG_WINDOW * 60)
    bb = best_window(t, d, far, n, LEG_WINDOW * 60)
    # height gained on the out leg, drift-cancelling: barometric drift during the
    # session shifts (turn - start) and (end - turn) the same way, so half
    # their difference is the real height change
    real = [j for j, r in enumerate(rows) if r[3] is not None]

    def ele_at(i):
        """median of the ~10 real altitude readings nearest sample i, within 5 min
        (GT 6 Pro: a reading every ~5 s, and none at all before the first fix)"""
        import bisect
        k = bisect.bisect_left(real, i)
        near = sorted(real[max(0, k - 10): k + 10], key=lambda j: abs(t[j] - t[i]))[:10]
        w = [rows[j][3] for j in near if abs(t[j] - t[i]) <= 300]
        return statistics.median(w) if w else None
    e0, ef, e1 = ele_at(0), ele_at(far), ele_at(n)
    if None not in (e0, ef, e1) and d[far] > 500 and d[n] - d[far] > 500:
        h = ((ef - e0) - (e1 - ef)) / 2
        out.update(h_out=h, g_out=h / d[far] * 100, g_back=-h / (d[n] - d[far]) * 100)
    out.update(t_turn=t[far] - t[0])
    out.update(
        out_kmh=vo, back_kmh=vb, out_moving_s=mo, back_moving_s=mb,
        out_km=d[far] / 1000, back_km=(d[n] - d[far]) / 1000,
        out_b10=bo, back_b10=bb,
        asym_pct=(vo / vb - 1) * 100 if vo and vb else None,
        wind_neutral=(bo + bb) / 2 if bo and bb else None,
    )
    return out


class Metrics:
    """Lazy, disk-backed cache of derive() results."""

    def __init__(self, db_path, cache_path):
        self.db_path, self.path = db_path, cache_path
        self.lock = threading.Lock()
        self.data = {}
        self.dirty = 0
        self.progress = {"done": 0, "total": 0, "running": False}
        try:
            with open(self.path) as f:
                blob = json.load(f)
            if blob.get("version") == METRICS_VERSION:
                self.data = blob["items"]
        except (OSError, ValueError, KeyError):
            pass

    def save(self):
        with self.lock:
            if not self.dirty:
                return
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w") as f:
                json.dump({"version": METRICS_VERSION, "items": self.data}, f)
            os.replace(tmp, self.path)
            self.dirty = 0

    def get_many(self, db, ids_with_n):
        res = {}
        for aid, n in ids_with_n:
            key = str(aid)
            m = self.data.get(key)
            if m is None or m.get("n") != (n or 0):
                m = derive(db, aid)
                m["n"] = n or 0
                with self.lock:
                    self.data[key] = m
                    self.dirty += 1
            res[aid] = m
        if self.dirty >= 25:
            self.save()
        return res

    def warm(self):
        db = connect(self.db_path)
        ids = db.execute(
            "SELECT id, samples FROM activities ORDER BY type != 'Inline Skate', date DESC"
        ).fetchall()
        self.progress.update(total=len(ids), done=0, running=True)
        for i in range(0, len(ids), 20):
            self.get_many(db, ids[i:i + 20])
            self.progress["done"] = min(len(ids), i + 20)
        self.save()
        self.progress["running"] = False
        db.close()


# ------------------------------------------------------------------ API


def connect(path):
    db = sqlite3.connect(f"file:{path}?mode=ro", uri=True, check_same_thread=False)
    db.row_factory = sqlite3.Row
    EDITS.sync(db)  # `activities` is now the edited view, see web/edits.py
    return db


def api_types(db, q, M):
    rows = db.execute(
        "SELECT type, COUNT(*) n, ROUND(SUM(km),1) km, MIN(date) first, MAX(date) last "
        "FROM activities GROUP BY type ORDER BY n DESC"
    ).fetchall()
    return [dict(r) for r in rows]


def api_activities(db, q, M):
    typ = q.get("type")
    # hidden=1 also lists hidden / deleted sessions (flagged), so they can be restored
    pw = "".join(f", {c}" if has_col(db, "activities_all", c, None) else f", NULL AS {c}" for c in OPT_COLS)
    sql = f"SELECT {LIST_COLS}{pw}, hidden, deleted FROM activities_all WHERE parse_error IS NULL"
    if q.get("hidden") != "1":
        sql += " AND NOT hidden"
    args = []
    if typ:
        sql += " AND type=?"
        args.append(typ)
    sql += " ORDER BY datetime"
    rows = [dict(r) for r in db.execute(sql, args).fetchall()]
    ms = M.get_many(db, [(r["id"], r["samples"]) for r in rows])
    for r in rows:
        m = ms.get(r["id"], {})
        for k in ("b30", "b45", "b60", "route", "crow_km", "bearing_out", "asym_pct",
                  "wind_neutral", "out_kmh", "back_kmh", "out_b10", "back_b10"):
            r[k] = m.get(k)
        lp = m.get("laps")
        if lp:
            reg = [x for x in lp["laps"] if x.get("counted")]
            r["laps_n"] = len(lp["laps"])
            r["lap_km"] = lp["lap_km"]
            r["lap_gate"] = lp["gate"]
            r["lap_center"] = lp["center"]
            r["best_lap_s"] = min((x["elapsed"] for x in reg), default=None)
            r["best_lap_kmh"] = max((x["kmh_elapsed"] for x in reg if x["kmh_elapsed"]), default=None)
        be = m.get("be") or {}
        for (k, _), col in zip(BEST_EFFORTS, BE_KEYS):
            r[col] = (be.get(k) or [None])[0]
        r["hr_hist"] = m.get("hr_hist") or None
        p = m.get("pw") or {}
        for k, w in zip(POWER_KEYS, POWER_WINDOWS):
            r[k] = p.get(str(w))
        r["pw_hist"] = m.get("pw_hist")
        r["np"] = m.get("np")
        r["cond"] = conditions(r, m)
    # refit the wind coefficient on this sport's data, then recompute with it
    if typ:
        k = fit_wind(typ, [r for r in rows if not r["hidden"]], ms)
        if k is not None and abs(k - FITS.get(typ, -1)) > 1e-6:
            FITS[typ] = k
            for r in rows:
                r["cond"] = conditions(r, ms.get(r["id"], {}))
    for r in rows:
        c = r["cond"] or {}
        r["temp"], r["wind"] = c.get("temp"), c.get("ws")
        r["adj_neutral"] = c.get("adjusted")
    prof = PROFILE.get()
    for r in rows:
        k = athlete.calories(r, prof) or {}
        r["kcal"], r["kcal_src"] = k.get("kcal"), k.get("source")
    return rows


def api_activity(db, q, M, aid):
    row = db.execute("SELECT * FROM activities_all WHERE id=?", (aid,)).fetchone()
    if not row:
        return None
    act = dict(row)
    act["edit"] = EDITS.get(aid)
    act["import_file"] = EDITS.import_file(aid, act.get("filename")) is not None
    m = M.get_many(db, [(aid, act["samples"])])[aid]
    splits = [dict(r) for r in db.execute(
        "SELECT km, moving_s, elapsed_s, pause_s, kmh, hr FROM splits WHERE activity_id=? ORDER BY km",
        (aid,))]
    rows, t, d = load_track(db, aid)
    # centred ~30 s speed, robust to 1 Hz and 6 s sampling alike
    speed = []
    lo = hi = 0
    n = len(t)
    for i in range(n):
        while t[i] - t[lo] > 15 and lo < i:
            lo += 1
        while hi + 1 < n and t[hi + 1] - t[i] <= 15:
            hi += 1
        a, b = lo, hi
        if a == b:
            a, b = max(0, i - 1), min(n - 1, i + 1)
        dt = t[b] - t[a]
        speed.append((d[b] - d[a]) / dt * 3.6 if dt > 0 else 0.0)
    # power: centred 30 s mean on the 1 s grid, raw watts are too spiky to read
    g = power_grid(rows, t)
    if g:
        cs = [0.0]
        for v in g:
            cs.append(cs[-1] + v)

        def watts(i):
            k = int(t[i] - t[0])
            lo_, hi_ = max(0, k - 15), min(len(g), k + 16)
            return round((cs[hi_] - cs[lo_]) / (hi_ - lo_)) if hi_ > lo_ else None
    else:
        def watts(i):
            return None

    # cumulative moving seconds at full resolution (same rule as `db build`), so a
    # downsampled series still gives the true moving clock (compare page)
    mv, acc = [0.0] * n, 0.0
    for i in range(1, n):
        dt = t[i] - t[i - 1]
        if dt > 0 and (d[i] - d[i - 1]) / dt >= THR:
            acc += dt
        mv[i] = acc

    def pt(i):
        r = rows[i]
        ll = valid_ll(r[1], r[2])
        return [t[i], round(d[i] / 1000, 4), round(speed[i], 2), r[4],
                None if r[3] is None else round(r[3], 1),
                round(r[1], 6) if ll else None, round(r[2], 6) if ll else None, watts(i), round(mv[i], 1)]

    step = max(1, math.ceil(n / SERIES_MAX))
    idx = list(range(0, n, step))
    if n and idx[-1] != n - 1:
        idx.append(n - 1)
    series = [pt(i) for i in idx]
    act["metrics"] = m
    if act.get("type") and act["type"] not in FITS:
        api_activities(db, {"type": act["type"]}, M)  # fits this sport's wind coefficient
    act["cond"] = conditions(act, m)
    act["cal"] = athlete.calories(act, PROFILE.get())
    act["splits"] = splits
    act["series_cols"] = ["t", "km", "kmh", "hr", "ele", "lat", "lon", "watts", "mv"]
    act["series"] = series
    return act


def api_tracks(db, q, M):
    typ = q.get("type")
    if not typ:
        return {"error": "type required"}
    ids = db.execute(
        "SELECT id, samples FROM activities WHERE type=? AND has_gps AND parse_error IS NULL",
        (typ,)).fetchall()
    ms = M.get_many(db, ids)
    keep = [(aid, ms[aid]["trk"]) for aid, _ in ids if ms.get(aid, {}).get("trk")]
    return {"ids": [k for k, _ in keep], "trk": [v for _, v in keep]}


SEGS = None  # segments.SegmentStore, set in start()
IMPORTER = None  # importer.Importer, set in start()
LAST = None  # lastimport.LastImport, set in start()
FAVS = None  # Favorites, set in start()
GEO = None  # geonames.Geonames, set in start()
WX = None  # weather.Weather, set in start()
PROFILE = None  # athlete.Profile, set in start()
EDITS = edits.Edits(None)  # in-memory until start() points it at $ROLLBOOK_HOME/edits.json


class Favorites:
    """Starred sessions and segments, in $ROLLBOOK_HOME/favorites.json.

    Server-side rather than localStorage so the app window and a browser tab
    share them. Segments also keep type/start/end/length: segment ids hash
    their geometry and change when new data regenerates them, and the page
    uses these to re-find a starred segment.
    """

    def __init__(self, path):
        self.path, self.lock = path, threading.Lock()
        try:
            with open(path) as f:
                self.data = json.load(f)
        except (OSError, ValueError):
            self.data = {}
        self.data.setdefault("sessions", {})
        self.data.setdefault("segments", {})

    def get(self):
        with self.lock:
            return json.loads(json.dumps(self.data))

    def set(self, kind, key, on, meta=None):
        if kind not in ("sessions", "segments") or not re.fullmatch(r"[0-9a-zA-Z]{1,24}", str(key)):
            raise ValueError("bad favorite")
        with self.lock:
            bucket = self.data[kind]
            if on:
                m = {k: v for k, v in (meta or {}).items() if k in ("type", "start", "end", "m")}
                bucket[str(key)] = {"added": time.strftime("%Y-%m-%d"), **m}
            else:
                bucket.pop(str(key), None)
            tmp = self.path + ".tmp"
            with open(tmp, "w") as f:
                json.dump(self.data, f, indent=1)
            os.replace(tmp, self.path)
            return json.loads(json.dumps(self.data))


def api_segments(db, q, M):
    typ = q.get("type")
    if not typ:
        return {"error": "type required"}
    r = SEGS.get(db, typ)
    if r.get("status") == "ready":
        # names fill in as lookups complete; the page re-polls while pending
        r = {**r, "segments": [{**sg, "place": geonames.describe(GEO, sg)} for sg in r["segments"]]}
        r["names_pending"] = GEO.pending()
    return r


# ------------------------------------------------------------------ conditions
# Physics per sport for the slope term: speed change at constant power on a
# small grade, dv/v = -m g s / (Crr m g + 1.5 rho CdA v^2). Running uses an
# empirical ~3% speed per 1% grade instead. The wind term is fitted on the
# user's own out-and-back sessions (model wind at 10 m is not what a skater
# feels by a riverbank, so physics would overstate it ~5x).
PHYS = {"Inline Skate": (75, 0.008, 0.35), "Ride": (85, 0.005, 0.32), "Run": None, "Walk": None}
WIND_K_DEFAULT = {"Inline Skate": 0.4, "Ride": 0.5, "Run": 0.15, "Walk": 0.05}
NO_WEATHER = ("Virtual Ride", "Virtual Run", "Swim", "Workout", "Weight Training", "Yoga")
FITS = {}  # type -> wind coefficient, %/(km/h) per leg


def slope_pct(typ, v_kmh, g):
    """% speed change from grade g (%), e.g. -0.1% grade -> +2.5% for a skater."""
    if g is None or not v_kmh:
        return 0.0
    if typ in ("Run", "Walk", "Hike"):
        return -3.0 * g if typ == "Run" else -2.0 * g
    m, crr, cda = PHYS.get(typ) or PHYS["Inline Skate"]
    v = v_kmh / 3.6
    return -100 * (m * 9.81 * g / 100) / (crr * m * 9.81 + 1.5 * 1.2 * cda * v * v)


def tailwind(w, brg):
    """km/h of wind pushing along heading brg (wind direction is where it comes FROM)."""
    return -w["ws"] * math.cos(math.radians(w["wd"] - brg))


def conditions(r, m):
    """Weather and, for out-and-backs, the wind / slope split of the leg speeds."""
    typ = r.get("type") or ""
    if typ in NO_WEATHER or typ.startswith("Virtual") or not m.get("ll0") or m.get("route") in ("indoor", "virtual"):
        return None
    try:
        t0 = datetime.fromisoformat(r["datetime"])
    except (TypeError, ValueError):
        return None
    if t0.tzinfo is None:
        t0 = t0.replace(tzinfo=timezone.utc)
    lat, lon = m["ll0"]
    mid = WX.at(lat, lon, t0 + timedelta(seconds=(m.get("dur_s") or 0) / 2))
    if mid is None:
        return {"pending": True}
    c = {k: mid[k] for k in ("temp", "ws", "wd", "gust")}
    if m.get("route") != "out-back" or not m.get("out_b10") or not m.get("back_b10"):
        return c
    tt = m.get("t_turn") or (m.get("dur_s") or 0) / 2
    wo = WX.at(lat, lon, t0 + timedelta(seconds=tt / 2)) or mid
    wb = WX.at(lat, lon, t0 + timedelta(seconds=(tt + (m.get("dur_s") or tt * 2)) / 2)) or mid
    k = FITS.get(typ, WIND_K_DEFAULT.get(typ, 0.3))
    legs = {}
    for leg, w, brg in (("out", wo, m["bearing_out"]), ("back", wb, m["bearing_out"] + 180)):
        tw = tailwind(w, brg)
        se = slope_pct(typ, m[f"{leg}_b10"], m.get(f"g_{leg}"))
        we = k * tw
        legs[leg] = {"tail": tw, "grade": m.get(f"g_{leg}"), "wind_pct": we, "slope_pct": se,
                     "adj_b10": m[f"{leg}_b10"] / (1 + (we + se) / 100)}
    gap = (m["out_b10"] / m["back_b10"] - 1) * 100
    wpart = legs["out"]["wind_pct"] - legs["back"]["wind_pct"]
    spart = legs["out"]["slope_pct"] - legs["back"]["slope_pct"]
    c.update(legs=legs, gap_pct=gap, gap_wind=wpart, gap_slope=spart, gap_other=gap - wpart - spart,
             adjusted=(legs["out"]["adj_b10"] + legs["back"]["adj_b10"]) / 2, wind_k=k)
    return c


def fit_wind(typ, rows, ms):
    """Least-squares wind coefficient from this sport's out-and-backs (gap minus slope ~ a + k * tailwind diff)."""
    xs, ys = [], []
    for r in rows:
        c, m = r.get("cond"), ms.get(r["id"], {})
        if not c or "legs" not in c:
            continue
        L = c["legs"]
        xs.append(L["out"]["tail"] - L["back"]["tail"])
        ys.append(c["gap_pct"] - c["gap_slope"])
    if len(xs) < 30:
        return None
    mx, my = sum(xs) / len(xs), sum(ys) / len(ys)
    sxx = sum((x - mx) ** 2 for x in xs)
    if sxx <= 0:
        return None
    k = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / sxx
    return max(0.0, min(2.0, k))


def api_status(db, q, M):
    return dict(M.progress, cached=len(M.data), weather_pending=WX.pending() if WX else 0)


def api_last_import(db, store):
    """The last import's summary (see web/lastimport.py), {} if none. `new` lists
    {id, type} read through the edits overlay: hidden sessions drop out and a
    later sport change shows, so the page knows which sports to load. `items`
    (file names, an export's carries the athlete id) stays on disk: the page
    doesn't need it and this is readable over --lan."""
    d = store.get() if store else None
    if not d:
        return {}
    d.pop("items", None)
    ids, new = d.pop("new_ids"), []
    for k in range(0, len(ids), 500):  # old SQLite builds: <= 999 variables
        part = ids[k:k + 500]
        new += db.execute(f"SELECT id, type FROM activities WHERE id IN ({','.join('?' * len(part))})",
                          part).fetchall()
    order = {x: n for n, x in enumerate(ids)}
    d["new"] = [{"id": i, "type": t} for i, t in sorted(new, key=lambda r: order[r[0]])]
    return d


def api_hrmax(db, q, M):
    p = PROFILE.get() if PROFILE else {}
    if p.get("hr_max"):
        return {"hrmax": p["hr_max"], "source": "profile"}
    r = db.execute(
        "SELECT MAX(hr_max) FROM activities WHERE date >= date('now','-365 days') AND hr_max < 230"
    ).fetchone()[0]
    return {"hrmax": r}


# ------------------------------------------------------------------ server


class TileCache:
    UPSTREAM = 2  # parallel fetches from the tile server, per the OSM tile usage policy

    def __init__(self, root, offline):
        self.root, self.offline = root, offline
        self.failed = {}  # url -> time of last failure, avoids hammering when offline
        self.lock = threading.Lock()
        self.slots = threading.BoundedSemaphore(self.UPSTREAM)
        self.inflight = {}  # url -> Lock: one fetch per tile, the other requests wait for it

    def get(self, src, z, x, y):
        path = os.path.join(self.root, src, str(z), str(x), f"{y}.png")
        try:
            with open(path, "rb") as f:
                return f.read()
        except OSError:
            pass
        url = TILE_SOURCES[src].format(z=z, x=x, y=y)
        if self.offline or time.time() - self.failed.get(url, 0) < 60:
            return None
        with self.lock:
            tl = self.inflight.setdefault(url, threading.Lock())
        with tl:
            try:  # another request may have fetched it while this one waited
                with open(path, "rb") as f:
                    return f.read()
            except OSError:
                pass
            if time.time() - self.failed.get(url, 0) < 60:
                return None
            try:
                with self.slots:
                    return self._fetch(url, path)
            finally:
                with self.lock:
                    self.inflight.pop(url, None)

    def _fetch(self, url, path):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": TILE_UA})
            with urllib.request.urlopen(req, timeout=10) as r:
                if r.status != 200 or not r.headers.get("Content-Type", "").startswith("image/"):
                    raise urllib.error.URLError(f"status {r.status}")
                body = r.read()
        except (urllib.error.URLError, OSError, ValueError):
            now = time.time()
            with self.lock:
                self.failed[url] = now
                if len(self.failed) > 2000:  # forget old failures
                    self.failed = {u: ts for u, ts in self.failed.items() if now - ts < 60}
            return None
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = f"{path}.{threading.get_ident()}.tmp"
        with open(tmp, "wb") as f:
            f.write(body)
        os.replace(tmp, path)
        return body


# ------------------------------------------------------------------ --lan
#
# `rollbook web --lan` adds a second listener on 0.0.0.0 (HTTPS with a
# self-signed cert by default) for a phone on the same network. The loopback
# listener is untouched. On the LAN listener every request needs the token:
# `?t=<token>` once (sets an HttpOnly cookie, then redirects the token out of
# the URL), the cookie afterwards. Host must be one of this machine's names
# (DNS rebinding), and POSTs are refused unless --lan-write.

LAN_COOKIE = "rb_lan"
LAN_COOKIE_AGE = 365 * 86400
TLS_TIMEOUT_S = 60  # handshake and idle keep-alive on the LAN listener
LAN_READONLY_MSG = "read-only from the network; restart with `rollbook web --lan-write` to allow changes"


class Lan:
    def __init__(self, token, write=False, https=True, port=8766, names=(), cert=None):
        self.token, self.write, self.https, self.cert = token, write, https, cert
        self.cert_args = None  # (tls_dir, host, ips) for tls.ensure_cert, set by lan_setup
        self.bind = "0.0.0.0"
        self.names = list(dict.fromkeys(names))  # host names / IPs, no port
        self.scheme = "https" if https else "http"
        self.set_port(port)

    def set_port(self, port):
        self.port = port
        self.hosts = {f"{n}:{port}" for n in self.names}
        if (self.https and port == 443) or (not self.https and port == 80):
            self.hosts |= set(self.names)

    def ok_token(self, value):
        return bool(value) and hmac.compare_digest(value.encode("utf-8", "replace"), self.token.encode())

    def urls(self, with_token=True):
        """Phone URLs, one per LAN address (not loopback / .local)."""
        out = []
        for n in self.names:
            if n in ("127.0.0.1", "localhost") or n.endswith(".local"):
                continue
            out.append(f"{self.scheme}://{n}:{self.port}/" + (f"?t={self.token}" if with_token else ""))
        return out


def lan_ips():
    """This machine's IPv4 addresses that a phone could reach (no loopback / link-local)."""
    ips = []
    for probe in ("10.254.254.254", "192.168.254.254"):
        try:  # UDP connect sends nothing; it just picks the outgoing interface
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                s.connect((probe, 9))
                ips.append(s.getsockname()[0])
        except OSError:
            pass
    try:
        ips += [a[4][0] for a in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)]
    except OSError:
        pass
    good = []
    for a in ips:
        ip = ipaddress.ip_address(a)
        if not (ip.is_loopback or ip.is_link_local or ip.is_unspecified) and a not in good:
            good.append(a)
    return good


def lan_token(home, new=False):
    """Random token in $ROLLBOOK_HOME/lan_token, reused across restarts."""
    path = os.path.join(home, "lan_token")
    if not new:
        try:
            with open(path, encoding="ascii") as f:
                t = f.read().strip()
            if re.fullmatch(r"[A-Za-z0-9_-]{22,}", t):
                return t
        except (OSError, UnicodeDecodeError):
            pass
    t = secrets.token_urlsafe(16)
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="ascii") as f:
        f.write(t + "\n")
    os.replace(tmp, path)
    return t


def lan_setup(home, port=8766, write=False, https=True, new_token=False, new_cert=False, ips=None):
    """Token, names and (for https) the self-signed cert for a --lan listener."""
    import tls

    ips = lan_ips() if ips is None else list(ips)
    host = tls.safe_host(socket.gethostname())
    cert_args = (os.path.join(home, "tls"), host, ips)
    cert = tls.ensure_cert(*cert_args, force=new_cert) if https else None
    names = ips + [f"{host}.local", "localhost", "127.0.0.1"]
    lan = Lan(lan_token(home, new_token), write, https, port, names, cert)
    lan.cert_args = cert_args
    return lan


class TLSServer(ThreadingHTTPServer):
    """HTTPS: the handshake runs in the request's own thread, not the accept loop,
    so a slow or plain-HTTP client can't stall the listener."""

    def __init__(self, addr, handler, ctx):
        self.ssl_ctx = ctx
        super().__init__(addr, handler)

    def process_request_thread(self, request, client_address):
        try:
            request.settimeout(TLS_TIMEOUT_S)
            request = self.ssl_ctx.wrap_socket(request, server_side=True)
        except (ssl.SSLError, OSError):
            self.shutdown_request(request)
            return
        super().process_request_thread(request, client_address)


def tls_context(lan):
    import tls

    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    try:
        ctx.load_cert_chain(lan.cert["cert"], lan.cert["key"])
    except (ssl.SSLError, OSError):  # damaged / mismatched pair: make a new one
        lan.cert = tls.ensure_cert(*lan.cert_args, force=True)
        ctx.load_cert_chain(lan.cert["cert"], lan.cert["key"])
    return ctx


def lan_banner(lan, qr_mode="auto", out=None):
    """What to print on start: URLs, QR codes, the cert fingerprint, the warning."""
    import qr

    out = out or sys.stdout
    tty = hasattr(out, "isatty") and out.isatty()
    show_qr = qr_mode == "always" or (qr_mode == "auto" and tty)
    lines = [f"\nnetwork access ({'read-write' if lan.write else 'read-only'}), "
             f"{lan.scheme}://<this machine>:{lan.port}/ — open on your phone:"]
    urls = lan.urls()
    if not urls:
        lines.append("  no network address found (offline?); only https://localhost works")
    for u in urls:
        lines.append(f"  {u}")
        if show_qr:
            lines.append(qr.render(qr.encode(u), ansi=qr.can_render_blocks(out)))
    if lan.cert:
        lines += [f"certificate SHA-256 {lan.cert['fingerprint']}",
                  "  self-signed: the phone warns about it. Check this fingerprint in the browser's",
                  "  certificate details, then Advanced -> Proceed."]
    else:
        lines.append("WARNING: plain HTTP (--lan-http): the token and your data cross the network unencrypted.")
    lines.append(f"Anyone with this link on your network can {'view and change' if lan.write else 'view'} "
                 "your training data. Rotate it with --new-token.")
    print("\n".join(lines), file=out, flush=True)


def make_handler(db_path, M, tiles, lan=None):
    """Request handler class. lan=None: the loopback listener (trusted, as always);
    a Lan: the `--lan` listener, where every request needs the token cookie."""
    local = threading.local()

    def dbc():
        if not hasattr(local, "db"):
            local.db = connect(db_path)
        EDITS.sync(local.db)  # picks up edits made since this thread's last request
        return local.db

    class H(BaseHTTPRequestHandler):
        server_version = "rollbook"

        def log_message(self, format, *args):
            if os.environ.get("ROLLBOOK_WEB_LOG"):
                sys.stderr.write("%s - %s\n" % (self.address_string(), format % args))

        def send(self, code, body, ctype="application/json", cache="no-store"):
            if isinstance(body, str):
                body = body.encode()
            gz = (len(body) > 4096 and not ctype.startswith("image/")
                  and "gzip" in self.headers.get("Accept-Encoding", ""))
            if gz:
                body = gzip.compress(body, 5)
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            if gz:
                self.send_header("Content-Encoding", "gzip")
            self.send_header("Cache-Control", "max-age=86400" if ctype in ("image/svg+xml", "image/png", "image/x-icon", "font/woff2") else cache)
            self.send_header("X-Content-Type-Options", "nosniff")
            if ctype.startswith("text/html"):
                self.send_header("Content-Security-Policy", CSP)
            if lan:
                self.send_header("Referrer-Policy", "no-referrer")
                self.send_header("X-Frame-Options", "DENY")
            self.end_headers()
            self.wfile.write(body)

        def json(self, obj, code=200):
            self.send(code, json.dumps(obj, separators=(",", ":"), allow_nan=False,
                                       default=str))

        # ---------------------------------------------------- --lan gate

        def _lan_cookie(self):
            try:
                c = SimpleCookie(self.headers.get("Cookie") or "")
            except CookieError:
                return None
            return c[LAN_COOKIE].value if LAN_COOKIE in c else None

        def _lan_gate(self, u, q):
            """LAN listener only. True = request answered here (refused or redirected)."""
            if lan is None:
                return False
            if self.headers.get("Host") not in lan.hosts:
                self._reject(403, "unknown host name: open one of the addresses printed by "
                                  "`rollbook web --lan`\n", "text/plain")
                return True
            if "t" in q and self.command == "GET":
                if not lan.ok_token(q["t"]):
                    self.send(403, "wrong or old link: use the one printed by `rollbook web --lan`\n", "text/plain")
                    return True
                rest = urllib.parse.urlencode({k: v for k, v in q.items() if k != "t"})
                self.send_response(302)
                self.send_header("Location", u.path + (f"?{rest}" if rest else ""))
                self.send_header("Set-Cookie", f"{LAN_COOKIE}={lan.token}; Path=/; Max-Age={LAN_COOKIE_AGE}; "
                                               "HttpOnly; SameSite=Lax" + ("; Secure" if lan.https else ""))
                self.send_header("Referrer-Policy", "no-referrer")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return True
            if lan.ok_token(self._lan_cookie()):
                return False
            self._reject(403, "not signed in: open the link (or scan the QR code) printed by "
                              "`rollbook web --lan` once on this device\n", "text/plain")
            return True

        def do_GET(self):
            now = time.monotonic()
            self.server.last_request = now
            u = urllib.parse.urlparse(self.path)
            q = {k: v[-1] for k, v in urllib.parse.parse_qs(u.query).items()}
            p = u.path
            if self._lan_gate(u, q):
                return
            if lan is None and self.headers.get("Host") not in self._local_hosts():
                # DNS rebinding: a website whose name now resolves to 127.0.0.1 would
                # otherwise read every response (GPS tracks included) as same-origin
                return self.send(403, "unknown host name: open http://127.0.0.1:"
                                      f"{self.server.server_address[1]}/\n", "text/plain")
            if lan is None and "app" in q and not p.startswith(("/api/", "/tiles/")):
                # app launcher: a page load of window <token> after its bye = a reload
                self.server.app_pages[q["app"]] = now
            try:
                if p.startswith("/api/"):
                    return self.route_api(p[5:], q)
                if p.startswith("/tiles/"):
                    return self.tile(p[7:])
                return self.static(p)
            except BrokenPipeError:
                pass
            except Exception as e:  # keep the server alive, report to client
                self.json({"error": f"{type(e).__name__}: {e}"}, 500)

        def _local_hosts(self):
            port = self.server.server_address[1]
            return (f"127.0.0.1:{port}", f"localhost:{port}")

        def _same_origin(self):
            """Reject other websites (CSRF) and DNS-rebinding hosts."""
            host = self.headers.get("Host")
            origin = self.headers.get("Origin")
            if lan is not None:
                return host in lan.hosts and (origin is None or origin == f"{lan.scheme}://{host}")
            ours = self._local_hosts()
            if host not in ours:
                return False
            return origin is None or origin in tuple("http://" + o for o in ours)

        def _reject(self, code, body, ctype="application/json"):
            """Refuse a POST without breaking the connection: read (and drop) a
            small request body first, otherwise close instead of keeping alive."""
            try:
                n = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                n = -1  # unparseable: can't drain it, so close
            if 0 < n <= 32 * 1024 * 1024:
                while n > 0:
                    n -= len(self.rfile.read(min(n, 1 << 20)) or b"x" * n)
            else:
                self.close_connection = True
            return self.send(code, body, ctype)

        def do_POST(self):
            u = urllib.parse.urlparse(self.path)
            q = {k: v[-1] for k, v in urllib.parse.parse_qs(u.query).items()}
            if lan is not None:
                q.pop("t", None)  # the token is only accepted on a GET (link / QR code)
                if self._lan_gate(u, q):
                    return
                if not lan.write or u.path == "/api/bye":  # bye: only the desktop app window
                    return self._reject(403, json.dumps({"error": LAN_READONLY_MSG}))
            if u.path == "/api/bye":
                # the app window's page sends it from pagehide (a beacon, so no
                # custom header possible; Origin is always set for beacons)
                if not self._same_origin() or not self.headers.get("Origin"):
                    return self._reject(403, "forbidden", "text/plain")
                cb = getattr(self.server, "on_bye", None)
                if cb:  # before answering (it only stamps a time): a 204 means it was seen
                    cb(q.get("app"))  # which window: a reused server also serves other launchers' windows
                return self.send(204, b"", "text/plain")
            if u.path == "/api/profile":
                if not self._same_origin() or self.headers.get("X-Rollbook") != "1":
                    return self._reject(403, json.dumps({"error": "forbidden"}))
                try:
                    n = int(self.headers.get("Content-Length") or 0)
                    return self.json(clean(PROFILE.set(json.loads(self.rfile.read(min(n, 65536)) or b"{}"))))
                except (ValueError, TypeError, AttributeError) as e:
                    return self.json({"error": str(e)}, 400)
            if u.path == "/api/edit":
                if not self._same_origin() or self.headers.get("X-Rollbook") != "1":
                    return self._reject(403, json.dumps({"error": "forbidden"}))
                try:
                    n = int(self.headers.get("Content-Length") or 0)
                    b = json.loads(self.rfile.read(min(n, 65536)) or b"{}")
                    aid = b.get("id") if isinstance(b, dict) else None
                    if not re.fullmatch(r"\d{1,20}", str(aid)):
                        raise ValueError("bad id")
                    e, type_changed = EDITS.update(dbc(), int(aid), b)
                    if type_changed:  # derive() treats Virtual sessions differently
                        with M.lock:
                            M.data.pop(str(aid), None)
                            M.dirty += 1
                    return self.json({"id": int(aid), "edit": e, "edits": EDITS.all()})
                except KeyError:
                    return self.json({"error": "no such activity"}, 404)
                except (ValueError, TypeError, OSError) as e:
                    return self.json({"error": str(e)}, 400)
            if u.path == "/api/favorites":
                if not self._same_origin() or self.headers.get("X-Rollbook") != "1":
                    return self._reject(403, json.dumps({"error": "forbidden"}))
                try:
                    n = int(self.headers.get("Content-Length") or 0)
                    b = json.loads(self.rfile.read(min(n, 65536)) or b"{}")
                    return self.json(FAVS.set(b.get("kind"), b.get("id"), bool(b.get("on")), b.get("meta")))
                except (ValueError, TypeError) as e:
                    return self.json({"error": str(e)}, 400)
            if u.path == "/api/last_import/dismiss":  # POST only: viewing never dismisses
                if not self._same_origin() or self.headers.get("X-Rollbook") != "1":
                    return self._reject(403, json.dumps({"error": "forbidden"}))
                try:
                    n = int(self.headers.get("Content-Length") or 0)
                except ValueError:
                    n = -1
                if not 0 <= n <= 4096:  # drained or closed by _reject: the connection stays sane
                    return self._reject(413 if n > 0 else 400, json.dumps({"error": "body must be 0-4096 bytes"}))
                try:
                    b = json.loads(self.rfile.read(n) or b"{}")
                    if not isinstance(b, dict):
                        raise ValueError("expected an object")
                except (ValueError, TypeError) as e:
                    return self.json({"error": str(e)}, 400)
                r = LAST.dismiss(b.get("finished")) if LAST else None
                if r is None:
                    return self.json({"error": "nothing to dismiss"}, 404)
                if r is False:  # a page from before a newer import: it must not hide that one unseen
                    return self.json({"error": "a newer import arrived, reload"}, 409)
                return self.json({"dismissed": True})
            if u.path.startswith("/api/import/"):
                # custom header: a cross-site page can't send it without a CORS
                # preflight, which this server never approves
                if not self._same_origin() or self.headers.get("X-Rollbook") != "1":
                    return self._reject(403, json.dumps({"error": "forbidden"}))
                try:
                    if u.path == "/api/import/upload":
                        n = int(self.headers.get("Content-Length") or 0)
                        code, body = IMPORTER.receive(self.rfile, n, q.get("name", ""), q.get("type"))
                    elif u.path == "/api/import/run":
                        code, body = IMPORTER.run()
                    else:
                        code, body = 404, {"error": "unknown endpoint"}
                except Exception as e:
                    code, body = 500, {"error": f"{type(e).__name__}: {e}"}
                return self.json(body, code)
            if u.path == "/api/strava/sync":
                if not self._same_origin() or self.headers.get("X-Rollbook") != "1":
                    return self._reject(403, json.dumps({"error": "forbidden"}))
                if IMPORTER.offline:
                    return self.json({"error": "rollbook runs with --offline: no Strava sync"}, 400)
                if not strava_web.status(IMPORTER.home)["configured"]:
                    return self.json({"error": "no Strava cookie stored: in a terminal, run ./rollbook strava auth"}, 400)
                code, body = IMPORTER.sync_strava()
                return self.json(clean(body), code)
            self._reject(404, "not found", "text/plain")

        def route_api(self, p, q):
            db = dbc()
            simple = {"types": api_types, "activities": api_activities,
                      "status": api_status, "hrmax": api_hrmax, "tracks": api_tracks,
                      "segments": api_segments}
            if p in simple:
                r = clean(simple[p](db, q, M))
                if p == "status":
                    r["readonly"] = lan is not None and not lan.write
                return self.json(r)
            if p == "favorites":
                return self.json(FAVS.get())
            if p == "edits":
                return self.json(EDITS.all())
            if p == "profile":
                return self.json(clean(PROFILE.get()))
            if p == "import":
                return self.json(clean(IMPORTER.status()))
            if p == "last_import":
                return self.json(clean(api_last_import(db, LAST)))
            if p == "strava":  # never the cookie itself
                return self.json(clean({**strava_web.status(IMPORTER.home), "offline": IMPORTER.offline}))
            if p.startswith("activity/"):
                tail = p[len("activity/"):]
                if not re.fullmatch(r"[0-9]{1,15}", tail):
                    return self.json({"error": "bad id"}, 400)
                a = api_activity(db, q, M, int(tail))
                if a is None:
                    return self.json({"error": "not found"}, 404)
                return self.json(clean(a))
            return self.json({"error": "unknown endpoint"}, 404)

        def tile(self, p):
            parts = p.removesuffix(".png").split("/")
            if (len(parts) != 4 or parts[0] not in TILE_SOURCES
                    or not all(x.isdigit() for x in parts[1:])):
                return self.send(400, "bad tile", "text/plain")
            src, z, x, y = parts[0], *map(int, parts[1:])
            if z > TILE_MAXZ or x >= 2 ** z or y >= 2 ** z:
                return self.send(400, "bad tile", "text/plain")
            body = tiles.get(src, z, x, y)
            if body is None:
                return self.send(404, "tile unavailable", "text/plain")
            self.send(200, body, "image/png", "max-age=604800")

        def static(self, p):
            name = {"/": "index.html", "": "index.html"}.get(p, p.lstrip("/"))
            if "/" in name or name.startswith(".") or name not in os.listdir(STATIC):
                return self.send(404, "not found", "text/plain")
            ext = os.path.splitext(name)[1]
            with open(os.path.join(STATIC, name), "rb") as f:
                self.send(200, f.read(), MIME.get(ext, "application/octet-stream"))

    return H


def clean(o):
    """JSON can't carry NaN/inf; SQLite REAL columns occasionally hold them."""
    if isinstance(o, float):
        return o if math.isfinite(o) else None
    if isinstance(o, dict):
        return {k: clean(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [clean(v) for v in o]
    return o


def _port_answers(port):
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.5):
            return True
    except OSError:
        return False


def start(db_path, cache_path, port=8765, offline=False, lan=None):
    """Bind and return (httpd, metrics, url); caller runs httpd.serve_forever().
    With lan (a Lan from lan_setup), httpd.lan_httpd is the network listener,
    bound but not yet serving."""
    global SEGS, IMPORTER, FAVS, GEO, WX, PROFILE, EDITS, LAST
    import importer
    import lastimport
    import segments  # imports this module; must come after it is fully loaded

    EDITS = edits.Edits(os.path.dirname(os.path.abspath(db_path)))
    M = Metrics(db_path, cache_path)
    SEGS = segments.SegmentStore(db_path, M, os.path.join(os.path.dirname(cache_path), "web_segments.json"))
    IMPORTER = importer.Importer(db_path, M)
    IMPORTER.on_store = EDITS.forget_removed
    IMPORTER.offline = offline
    LAST = lastimport.LastImport(os.path.join(os.path.dirname(os.path.abspath(db_path)), "last_import.json"))
    IMPORTER.last = LAST
    PROFILE = athlete.Profile(os.path.dirname(os.path.abspath(db_path)))
    FAVS = Favorites(os.path.join(os.path.dirname(os.path.abspath(db_path)), "favorites.json"))
    threading.Thread(target=M.warm, daemon=True).start()
    tiles = TileCache(os.path.join(os.path.dirname(cache_path), "tiles"), offline)
    WX = weather.Weather(os.path.join(os.path.dirname(cache_path), "weather.json"), offline)
    GEO = geonames.Geonames(os.path.join(os.path.dirname(cache_path), "geonames.json"), offline)
    httpd = ThreadingHTTPServer(("127.0.0.1", port), make_handler(db_path, M, tiles))
    httpd.daemon_threads = True
    httpd.on_bye = None  # set by the app launcher: called with the window token when a page closes
    httpd.app_pages = {}  # window token -> monotonic time of its last page load
    httpd.lan_httpd = None
    if lan is not None:
        try:
            if lan.port and _port_answers(lan.port):
                # macOS lets 0.0.0.0:p bind (SO_REUSEADDR) while another process
                # holds 127.0.0.1:p; the two would then split the traffic
                raise OSError(errno.EADDRINUSE, f"port {lan.port} is already in use")
            h = make_handler(db_path, M, tiles, lan)
            addr = (lan.bind, lan.port)
            srv = TLSServer(addr, h, tls_context(lan)) if lan.https else ThreadingHTTPServer(addr, h)
        except BaseException:
            httpd.server_close()
            raise
        srv.daemon_threads = True
        srv.app_pages = {}
        lan.set_port(srv.server_address[1])
        httpd.lan_httpd = srv
    url = f"http://127.0.0.1:{httpd.server_address[1]}/"
    print(f"serving {url}  (db {db_path}, tiles {'cache only' if offline else 'online + cache'})")
    return httpd, M, url


def serve(db_path, cache_path, port=8765, open_browser=True, offline=False, lan=None, qr="auto"):
    httpd, M, url = start(db_path, cache_path, port, offline, lan)
    if httpd.lan_httpd:
        threading.Thread(target=httpd.lan_httpd.serve_forever, daemon=True).start()
        lan_banner(lan, qr)
    print("ctrl-c to stop", flush=True)
    if open_browser:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        M.save()
        if httpd.lan_httpd:
            httpd.lan_httpd.shutdown()
            httpd.lan_httpd.server_close()
        httpd.server_close()


if __name__ == "__main__":
    home = os.path.abspath(os.path.expanduser(os.environ.get("ROLLBOOK_HOME") or "~/.local/share/rollbook"))
    serve(os.path.join(home, "rollbook.db"), os.path.join(home, "cache", "web_metrics.json"),
          int(sys.argv[1]) if len(sys.argv) > 1 else 8765)
