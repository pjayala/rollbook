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

import geonames
import gzip
import json
import re
import math
import os
import sqlite3
import statistics
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

METRICS_VERSION = 10
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

# Basemap tiles are the one thing fetched from the network: proxied through
# this server and cached on disk forever, so the browser never talks to a
# third party and a once-viewed area keeps working offline.
TILE_SOURCES = {  # CARTO needs an API key now; the dark look is a CSS filter client-side
    "osm": "https://tile.openstreetmap.org/{z}/{x}/{y}.png",
}
TILE_UA = "Rollbook/1.0 (personal training dashboard, single-user; tiles cached)"
TILE_MAXZ = 19
STATIC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
MIME = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
        ".css": "text/css; charset=utf-8", ".svg": "image/svg+xml",
        ".png": "image/png", ".ico": "image/x-icon"}

LIST_COLS = (
    "id, date, datetime, name, type, device, gear, km, elapsed_s, moving_s, pause_s, "
    "kmh_moving, kmh_elapsed, best30_kmh, best5min_kmh, best10min_kmh, best20min_kmh, "
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


def load_track(db, aid):
    km, km_calc = db.execute("SELECT km, km_calc FROM activities WHERE id=?", (aid,)).fetchone()
    scale = (km / km_calc) if (km and km_calc and km_calc > 0) else 1.0
    rows = db.execute(
        "SELECT t, lat, lon, ele, hr, cad, dist_m FROM samples WHERE activity_id=? ORDER BY t",
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

    typ = db.execute("SELECT type FROM activities WHERE id=?", (aid,)).fetchone()[0] or ""
    if typ.startswith("Virtual"):  # simulated GPS, no wind, gradients are fake
        out["route"] = "virtual"
        return out
    valid = [i for i, r in enumerate(rows) if valid_ll(r[1], r[2])]
    if len(valid) < 20:
        out["route"] = "indoor"
        return out
    out["trk"] = compact_track(rows)
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
    return db


def api_types(db, q, M):
    rows = db.execute(
        "SELECT type, COUNT(*) n, ROUND(SUM(km),1) km, MIN(date) first, MAX(date) last "
        "FROM activities GROUP BY type ORDER BY n DESC"
    ).fetchall()
    return [dict(r) for r in rows]


def api_activities(db, q, M):
    typ = q.get("type")
    sql = f"SELECT {LIST_COLS} FROM activities WHERE parse_error IS NULL"
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
        r["hr_hist"] = m.get("hr_hist") or None
    return rows


def api_activity(db, q, M, aid):
    row = db.execute("SELECT * FROM activities WHERE id=?", (aid,)).fetchone()
    if not row:
        return None
    act = dict(row)
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
    def pt(i):
        r = rows[i]
        ll = valid_ll(r[1], r[2])
        return [t[i], round(d[i] / 1000, 4), round(speed[i], 2), r[4],
                None if r[3] is None else round(r[3], 1),
                round(r[1], 6) if ll else None, round(r[2], 6) if ll else None]

    step = max(1, math.ceil(n / SERIES_MAX))
    idx = list(range(0, n, step))
    if n and idx[-1] != n - 1:
        idx.append(n - 1)
    series = [pt(i) for i in idx]
    act["metrics"] = m
    act["splits"] = splits
    act["series_cols"] = ["t", "km", "kmh", "hr", "ele", "lat", "lon"]
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
FAVS = None  # Favorites, set in start()
GEO = None  # geonames.Geonames, set in start()


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


def api_status(db, q, M):
    return dict(M.progress, cached=len(M.data))


def api_hrmax(db, q, M):
    r = db.execute(
        "SELECT MAX(hr_max) FROM activities WHERE date >= date('now','-365 days') AND hr_max < 230"
    ).fetchone()[0]
    return {"hrmax": r}


# ------------------------------------------------------------------ server


class TileCache:
    def __init__(self, root, offline):
        self.root, self.offline = root, offline
        self.failed = {}  # url -> time of last failure, avoids hammering when offline
        self.lock = threading.Lock()

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
        try:
            req = urllib.request.Request(url, headers={"User-Agent": TILE_UA})
            with urllib.request.urlopen(req, timeout=10) as r:
                if r.status != 200 or not r.headers.get("Content-Type", "").startswith("image/"):
                    raise urllib.error.URLError(f"status {r.status}")
                body = r.read()
        except (urllib.error.URLError, OSError, ValueError):
            with self.lock:
                self.failed[url] = time.time()
            return None
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = f"{path}.{threading.get_ident()}.tmp"
        with open(tmp, "wb") as f:
            f.write(body)
        os.replace(tmp, path)
        return body


def make_handler(db_path, M, tiles):
    local = threading.local()

    def dbc():
        if not hasattr(local, "db"):
            local.db = connect(db_path)
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
            self.send_header("Cache-Control", "max-age=86400" if ctype in ("image/svg+xml", "image/png", "image/x-icon") else cache)
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)

        def json(self, obj, code=200):
            self.send(code, json.dumps(obj, separators=(",", ":"), allow_nan=False,
                                       default=str))

        def do_GET(self):
            self.server.last_request = time.monotonic()  # app launcher: reload vs. closed window
            u = urllib.parse.urlparse(self.path)
            q = {k: v[-1] for k, v in urllib.parse.parse_qs(u.query).items()}
            p = u.path
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

        def _same_origin(self):
            """Reject other websites (CSRF) and DNS-rebinding hosts."""
            port = self.server.server_address[1]
            ours = (f"127.0.0.1:{port}", f"localhost:{port}")
            if self.headers.get("Host") not in ours:
                return False
            origin = self.headers.get("Origin")
            return origin is None or origin in tuple("http://" + o for o in ours)

        def do_POST(self):
            u = urllib.parse.urlparse(self.path)
            q = {k: v[-1] for k, v in urllib.parse.parse_qs(u.query).items()}
            if u.path == "/api/bye":
                # the app window's page sends it from pagehide (a beacon, so no
                # custom header possible; Origin is always set for beacons)
                if not self._same_origin() or not self.headers.get("Origin"):
                    return self.send(403, "forbidden", "text/plain")
                self.send(204, b"", "text/plain")
                cb = getattr(self.server, "on_bye", None)
                if cb:
                    cb()
                return
            if u.path == "/api/favorites":
                if not self._same_origin() or self.headers.get("X-Rollbook") != "1":
                    return self.json({"error": "forbidden"}, 403)
                try:
                    n = int(self.headers.get("Content-Length") or 0)
                    b = json.loads(self.rfile.read(min(n, 65536)) or b"{}")
                    return self.json(FAVS.set(b.get("kind"), b.get("id"), bool(b.get("on")), b.get("meta")))
                except (ValueError, TypeError) as e:
                    return self.json({"error": str(e)}, 400)
            if u.path.startswith("/api/import/"):
                # custom header: a cross-site page can't send it without a CORS
                # preflight, which this server never approves
                if not self._same_origin() or self.headers.get("X-Rollbook") != "1":
                    return self.json({"error": "forbidden"}, 403)
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
            self.send(404, "not found", "text/plain")

        def route_api(self, p, q):
            db = dbc()
            simple = {"types": api_types, "activities": api_activities,
                      "status": api_status, "hrmax": api_hrmax, "tracks": api_tracks,
                      "segments": api_segments}
            if p in simple:
                return self.json(clean(simple[p](db, q, M)))
            if p == "favorites":
                return self.json(FAVS.get())
            if p == "import":
                return self.json(clean(IMPORTER.status()))
            if p.startswith("activity/"):
                tail = p[len("activity/"):]
                if not tail.isdigit():
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


def start(db_path, cache_path, port=8765, offline=False):
    """Bind and return (httpd, metrics, url); caller runs httpd.serve_forever()."""
    global SEGS, IMPORTER, FAVS, GEO
    import importer
    import segments  # imports this module; must come after it is fully loaded

    M = Metrics(db_path, cache_path)
    SEGS = segments.SegmentStore(db_path, M, os.path.join(os.path.dirname(cache_path), "web_segments.json"))
    IMPORTER = importer.Importer(db_path, M)
    FAVS = Favorites(os.path.join(os.path.dirname(os.path.abspath(db_path)), "favorites.json"))
    threading.Thread(target=M.warm, daemon=True).start()
    tiles = TileCache(os.path.join(os.path.dirname(cache_path), "tiles"), offline)
    GEO = geonames.Geonames(os.path.join(os.path.dirname(cache_path), "geonames.json"), offline)
    httpd = ThreadingHTTPServer(("127.0.0.1", port), make_handler(db_path, M, tiles))
    httpd.daemon_threads = True
    httpd.on_bye = None  # set by the app launcher: called when the page's window closes
    url = f"http://127.0.0.1:{httpd.server_address[1]}/"
    print(f"serving {url}  (db {db_path}, tiles {'cache only' if offline else 'online + cache'})")
    return httpd, M, url


def serve(db_path, cache_path, port=8765, open_browser=True, offline=False):
    httpd, M, url = start(db_path, cache_path, port, offline)
    print("ctrl-c to stop")
    if open_browser:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        M.save()
        httpd.server_close()


if __name__ == "__main__":
    home = os.path.abspath(os.path.expanduser(os.environ.get("ROLLBOOK_HOME") or "~/.local/share/rollbook"))
    serve(os.path.join(home, "rollbook.db"), os.path.join(home, "cache", "web_metrics.json"),
          int(sys.argv[1]) if len(sys.argv) > 1 else 8765)
