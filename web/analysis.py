# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Pedro Ayala. Rollbook is free software, see LICENSE.
"""Per-activity analysis from the samples table: geometry, best windows and
efforts, tracks, laps, power, and derive() with its disk cache (Metrics).

derive() results are cached in $ROLLBOOK_HOME/cache/web_metrics.json, keyed by
activity id and invalidated when the sample count or METRICS_VERSION changes:
bump it after changing derive().
"""

import bisect
import json
import math
import os
import sqlite3
import statistics
import threading

import store

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


CACHE_MAX = 512 << 20  # web_metrics.json; ~10 MB for 1900 sessions


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


def _connect_ro(path):
    db = sqlite3.connect(f"file:{path}?mode=ro", uri=True, check_same_thread=False)
    db.row_factory = sqlite3.Row
    return db


class Metrics:
    """Lazy, disk-backed cache of derive() results."""

    def __init__(self, db_path, cache_path, connect=None):
        """connect(db_path): the connection warm() reads through (the server's
        has the edits overlay); default a plain read-only one."""
        self.db_path, self.path = db_path, cache_path
        self.connect = connect or _connect_ro
        self.lock = threading.Lock()
        self.data = {}
        self.dirty = 0
        self.progress = {"done": 0, "total": 0, "running": False}
        blob = store.load_object(self.path, CACHE_MAX)  # None: missing, corrupt, too big, not an object
        items = blob.get("items") if blob and blob.get("version") == METRICS_VERSION else None
        if isinstance(items, dict):
            # an entry of the wrong shape is dropped (derive() redoes it); one with a bad `n` reads as stale
            self.data = {k: v for k, v in items.items()
                         if isinstance(k, str) and k.isdigit() and isinstance(v, dict)}

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

    def get_many(self, db, ids_with_n, compute=True):
        """compute=False: cached, current entries only (warm() fills the rest)."""
        res = {}
        for aid, n in ids_with_n:
            key = str(aid)
            m = self.data.get(key)
            if m is None or m.get("n") != (n or 0):
                if not compute:
                    continue
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
        db = self.connect(self.db_path)
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
