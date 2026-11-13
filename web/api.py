# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Pedro Ayala. Rollbook is free software, see LICENSE.
"""The JSON API behind the dashboard: GET views and POST actions.

The stores are module globals set by server.start(); every handler takes
plain arguments, so the HTTP layer (server.py, http_base.py) only routes.
"""

import math
import re
import sqlite3

import analysis
import conditions as cond
import edits
import gear as gearmod
import geonames
import profile as athlete
import strava_web
import weather
from analysis import has_col

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


SEGS = None  # segments.SegmentStore, set in server.start()
IMPORTER = None  # importer.Importer, set in server.start()
LAST = None  # lastimport.LastImport, set in server.start()
FAVS = None  # Favorites, set in server.start()
GEO = None  # geonames.Geonames, set in server.start()
WX_JOB = None  # weather.OnDemand, set in server.start()
PROFILE = None  # athlete.Profile, set in server.start()
EDITS = edits.Edits(None)  # in-memory until start() points it at $ROLLBOOK_HOME/edits.json
GEAR = gearmod.Gear(None)  # likewise, $ROLLBOOK_HOME/gear.json


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
    ids = [r["id"] for r in rows]
    wx = weather.load(db, ids if len(ids) <= 500 else None)
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
        bw = analysis.clean_bw(m.get("bw"))  # best speed per window length, no positions
        r["bw"] = {k: v[0] for k, v in bw.items()} if bw else None
        be = m.get("be") or {}
        for (k, _), col in zip(analysis.BEST_EFFORTS, analysis.BE_KEYS):
            r[col] = (be.get(k) or [None])[0]
        r["hr_hist"] = m.get("hr_hist") or None
        p = m.get("pw") or {}
        for k, w in zip(analysis.POWER_KEYS, analysis.POWER_WINDOWS):
            r[k] = p.get(str(w))
        r["pw_hist"] = m.get("pw_hist")
        r["np"] = m.get("np")
        r["cond"] = cond.conditions(r, m, wx.get(r["id"]))
    # refit the wind coefficient on this sport's data, then recompute with it
    if typ:
        k = cond.fit_wind(typ, [r for r in rows if not r["hidden"]], ms)
        if k is not None and abs(k - cond.FITS.get(typ, -1)) > 1e-6:
            cond.FITS[typ] = k
            for r in rows:
                r["cond"] = cond.conditions(r, ms.get(r["id"], {}), wx.get(r["id"]))
    for r in rows:
        c = r["cond"] or {}
        r["temp"], r["wind"] = c.get("temp"), c.get("ws")
        r["rain"], r["aqi"] = c.get("precip_sum"), c.get("aqi")
        r["adj_neutral"] = c.get("adjusted")
    prof = PROFILE.get()
    res = GEAR.resolver(db)
    hx = hrmax_at(db, M, prof) if any(r["hr_hist"] for r in rows) else None
    for r in rows:
        if r["hr_hist"] and hx:
            r["hrmax"], r["hrmax_src"] = hx.at(r["date"])
        k = athlete.calories(r, prof) or {}
        r["kcal"], r["kcal_src"] = k.get("kcal"), k.get("source")
        r["gear_id"], r["gear_src"] = res.of(r["id"], r["type"], r["date"])
        r["gear_name"] = res.name(r["gear_id"])
    return rows


def api_activity(db, q, M, aid):
    row = db.execute("SELECT * FROM activities_all WHERE id=?", (aid,)).fetchone()
    if not row:
        return None
    act = dict(row)
    act["edit"] = EDITS.get(aid)
    act["import_file"] = EDITS.import_file(aid, act.get("filename")) is not None
    res = GEAR.resolver(db)
    act["gear_id"], act["gear_src"] = res.of(aid, act["type"], act.get("date"))
    act["gear_name"] = res.name(act["gear_id"])
    act["gear_parts"] = res.part_info(act["gear_id"], act.get("date"))
    dg, _ = res.default_of(act["type"], act.get("date"))  # what "Default" means for this session
    act["gear_default"] = {"id": dg, "name": res.name(dg)} if dg else None
    m = M.get_many(db, [(aid, act["samples"])])[aid]
    splits = [dict(r) for r in db.execute(
        "SELECT km, moving_s, elapsed_s, pause_s, kmh, hr FROM splits WHERE activity_id=? ORDER BY km",
        (aid,))]
    rows, t, d = analysis.load_track(db, aid)
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
    g = analysis.power_grid(rows, t)
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
        if dt > 0 and (d[i] - d[i - 1]) / dt >= analysis.THR:
            acc += dt
        mv[i] = acc

    def pt(i):
        r = rows[i]
        ll = analysis.valid_ll(r[1], r[2])
        return [t[i], round(d[i] / 1000, 4), round(speed[i], 2), r[4],
                None if r[3] is None else round(r[3], 1),
                round(r[1], 6) if ll else None, round(r[2], 6) if ll else None, watts(i), round(mv[i], 1)]

    step = max(1, math.ceil(n / analysis.SERIES_MAX))
    idx = list(range(0, n, step))
    if n and idx[-1] != n - 1:
        idx.append(n - 1)
    series = [pt(i) for i in idx]
    m = dict(m)
    m["bw"] = analysis.clean_bw(m.get("bw"))
    act["metrics"] = m
    if act.get("type") and act["type"] not in cond.FITS:
        api_activities(db, {"type": act["type"]}, M)  # fits this sport's wind coefficient
    hours = weather.load(db, [aid]).get(aid)
    act["cond"] = cond.conditions(act, m, hours)
    act["weather_hours"] = [v for _, v in hours] if act["cond"] and hours else []  # the details view
    # an outdoor session without weather rows: the page asks POST /api/weather to fetch them
    act["weather_missing"] = (not hours and bool(act.get("has_gps")) and weather.eligible(act.get("type"))
                              and m.get("route") not in ("indoor", "virtual"))
    act["cal"] = athlete.calories(act, PROFILE.get())
    if m.get("hr_hist"):  # zones against the max HR that applied on the session's date
        act["hrmax"], act["hrmax_src"] = hrmax_at(db, M, PROFILE.get()).at(act.get("date"))
    act["splits"] = splits
    act["series_cols"] = ["t", "km", "kmh", "hr", "ele", "lat", "lon", "watts", "mv"]
    act["series"] = series
    return act


TRACK_IDS_MAX = 50  # the Home page's recent feed asks for a dozen route thumbnails


def track_ids(v):
    """`?ids=1,2,3` -> distinct ints, at most TRACK_IDS_MAX; ValueError on anything else."""
    parts = str(v).split(",")
    if not 0 < len(parts) <= TRACK_IDS_MAX or not all(ACTIVITY_ID.fullmatch(p) for p in parts):
        raise ValueError("bad ids")
    return list(dict.fromkeys(int(p) for p in parts))


def api_tracks(db, q, M):
    typ = q.get("type")
    if q.get("ids") is not None:
        # specific sessions of any sport (the Home feed); through the edits overlay, so a
        # hidden / deleted id simply has no track
        try:
            want = track_ids(q["ids"])
        except ValueError:
            return {"error": f"ids: up to {TRACK_IDS_MAX} activity ids, comma-separated"}
        ids = db.execute(
            f"SELECT id, samples FROM activities WHERE id IN ({','.join('?' * len(want))}) AND has_gps "
            "AND parse_error IS NULL", want).fetchall()
    elif not typ:
        return {"error": "type required"}
    else:
        ids = db.execute(
            "SELECT id, samples FROM activities WHERE type=? AND has_gps AND parse_error IS NULL",
            (typ,)).fetchall()
    ms = M.get_many(db, ids)
    keep = [(aid, ms[aid]["trk"]) for aid, _ in ids if ms.get(aid, {}).get("trk")]
    return {"ids": [k for k, _ in keep], "trk": [v for _, v in keep]}


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


def hr_sessions(db, M):
    """Sessions with heart rate, with their HR histogram (metrics cache)."""
    rows = [dict(r) for r in db.execute(
        "SELECT id, date, type, hr_max, samples FROM activities WHERE hr_max > 0 AND parse_error IS NULL")]
    # cached only: this runs on every list / session / hrmax request, warm() fills the rest
    ms = M.get_many(db, [(r["id"], r["samples"]) for r in rows], compute=False) if rows else {}
    for r in rows:
        r["hist"] = (ms.get(r["id"]) or {}).get("hr_hist")
    return rows


def hrmax_at(db, M, prof):
    return athlete.HrmaxAt(prof, hr_sessions(db, M))


def api_hrmax(db, q, M):
    """Max HR for zones, and the suggestion from the last N months (profile hr_months,
    or ?months= to preview another window)."""
    p = PROFILE.get() if PROFILE else {}
    months = p.get("hr_months") or athlete.HR_MONTHS
    if q.get("months"):
        try:
            months = athlete._field("hr_months", q["months"])
        except ValueError:
            return {"error": "months must be 1-36"}
    est = athlete.hrmax_estimate(hr_sessions(db, M), months)
    bpm, src = athlete.hrmax_choice(p, est)
    return {"hrmax": bpm, "source": src, "estimate": est, "age": athlete.age_hrmax(p), "months": months,
            "history": p.get("hr_max_history") or []}


def clean(o):
    """JSON can't carry NaN/inf; SQLite REAL columns occasionally hold them."""
    if isinstance(o, float):
        return o if math.isfinite(o) else None
    if isinstance(o, dict):
        return {k: clean(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [clean(v) for v in o]
    return o


def _weather_id(v):
    if not ACTIVITY_ID.fullmatch(str(v)):
        raise ValueError("bad id")
    return int(v)


def api_weather_status(q):
    try:
        aid = _weather_id(q.get("id"))
    except ValueError:
        return {"error": "id required"}
    return WX_JOB.status(aid) if WX_JOB else {"state": "offline"}


def api_strava(db, q, M):  # never the cookie itself
    return {**strava_web.status(IMPORTER.home), "offline": IMPORTER.offline}


# GET /api/<name>: fn(db, q, M) -> JSON-able (passed through clean())
GET = {
    "types": api_types, "activities": api_activities, "status": api_status, "hrmax": api_hrmax,
    "tracks": api_tracks, "segments": api_segments,
    "favorites": lambda db, q, M: FAVS.get(),
    "edits": lambda db, q, M: EDITS.all(),
    "profile": lambda db, q, M: PROFILE.get(),
    "import": lambda db, q, M: IMPORTER.status(),
    "last_import": lambda db, q, M: api_last_import(db, LAST),
    "strava": api_strava,
    "gear": lambda db, q, M: GEAR.view(db),
    "weather": lambda db, q, M: api_weather_status(q),
}
ACTIVITY_ID = re.compile(r"[0-9]{1,15}")


# ------------------------------------------------------------------ POST
#
# Each action takes a Req and returns (status, JSON-able). The HTTP layer has
# already checked same origin + X-Rollbook (and --lan-write) and, for JSON
# actions, read the body (Req.body). ValueError / TypeError -> 400, OSError ->
# 500 "<what> not saved" (details logged, not sent).


class Req:
    def __init__(self, body, q, db, M, rfile=None, length=0):
        self.body, self.q, self._db, self.M, self.rfile, self.length = body, q, db, M, rfile, length

    def db(self):
        return self._db()


def post_profile(r):
    return 200, PROFILE.set(r.body)


def post_edit(r):
    aid = r.body.get("id")
    if not edits.ID_RE.fullmatch(str(aid)):
        raise ValueError("bad id")
    try:
        e, type_changed = EDITS.update(r.db(), int(aid), r.body)
    except KeyError:
        return 404, {"error": "no such activity"}
    except edits.RemoveFailed as e:  # the hide is saved; the page reloads it
        return 500, {"error": str(e), "edits": EDITS.all()}
    if type_changed:  # derive() treats Virtual sessions differently
        with r.M.lock:
            r.M.data.pop(str(aid), None)
            r.M.dirty += 1
    return 200, {"id": int(aid), "edit": e, "edits": EDITS.all()}


def post_favorites(r):
    b = r.body
    return 200, FAVS.set(b.get("kind"), b.get("id"), bool(b.get("on")), b.get("meta"))


def post_dismiss(r):  # POST only: viewing never dismisses
    res = LAST.dismiss(r.body.get("finished")) if LAST else None
    if res is None:
        return 404, {"error": "nothing to dismiss"}
    if res is False:  # a page from before a newer import: it must not hide that one unseen
        return 409, {"error": "a newer import arrived, reload"}
    return 200, {"dismissed": True}


def _guarded(fn):
    def run(r):
        try:
            return fn(r)
        except Exception as e:  # the job runs in the background; report, don't crash the request
            return 500, {"error": f"{type(e).__name__}: {e}"}
    return run


@_guarded
def post_upload(r):
    return IMPORTER.receive(r.rfile, r.length, r.q.get("name", ""), r.q.get("type"))


@_guarded
def post_import_run(r):
    return IMPORTER.run()


def post_strava_sync(r):
    if IMPORTER.offline:
        return 400, {"error": "rollbook runs with --offline: no Strava sync"}
    if not strava_web.status(IMPORTER.home)["configured"]:
        return 400, {"error": "no Strava cookie stored: in a terminal, run ./rollbook strava auth"}
    return IMPORTER.sync_strava()


def post_gear(r):
    try:
        out = GEAR.apply(r.db(), r.body)
    except gearmod.NotFound:
        return 404, {"error": "no such gear, part or session"}
    except gearmod.Conflict as e:
        return 409, {"error": str(e)}
    except gearmod.Broken as e:
        return 503, {"error": str(e)}
    return 200, {**out, "gear": GEAR.view(r.db())}


def post_weather(r):
    """Fetch the weather of one session that has none (see weather.OnDemand)."""
    aid = _weather_id(r.body.get("id"))
    db = r.db()
    row = db.execute("SELECT type, has_gps FROM activities_all WHERE id=?", (aid,)).fetchone()
    if not row:
        return 404, {"error": "no such activity"}
    if not row["has_gps"] or not weather.eligible(row["type"]):
        return 200, {"state": "none"}  # indoor: no weather to fetch, no process spawned
    if weather.load(db, [aid]).get(aid):
        return 200, {"state": "done"}
    return 200, WX_JOB.request(aid) if WX_JOB else {"state": "offline"}


JSON_LIMIT = 65536


class Post:
    """One POST action. limit: JSON body cap in bytes, or None for an action that
    reads the raw body itself (upload) or none; what: named in a failed-save error."""

    def __init__(self, fn, limit=JSON_LIMIT, what="change"):
        self.fn, self.limit, self.what = fn, limit, what


POST = {
    "/api/profile": Post(post_profile, what="profile"),
    "/api/edit": Post(post_edit, what="edit"),
    "/api/favorites": Post(post_favorites, what="favorite"),
    "/api/last_import/dismiss": Post(post_dismiss, 4096, what="summary"),
    "/api/import/upload": Post(post_upload, None),
    "/api/import/run": Post(post_import_run, None),
    "/api/strava/sync": Post(post_strava_sync, None),
    "/api/gear": Post(post_gear, what="gear"),
    "/api/weather": Post(post_weather, 4096, what="weather"),
}
