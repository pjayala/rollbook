# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Pedro Ayala. Rollbook is free software, see LICENSE.
"""Historical weather and air quality for outdoor sessions, from Open-Meteo (free, no key).

`rollbook db weather` (run at the end of every `db build`) stores, for every
UTC hour a session touches, the model weather at the session's position at
that hour in the `weather` table: one row per (activity, hour). The dashboard
and the MCP server only read that table; they never fetch.

Sources, per ~0.1 deg cell and UTC day:
  weather      archive API (ERA5 / ECMWF IFS reanalysis); hours the archive
               doesn't have yet (the last days) from the forecast API's past_days
  air quality  air-quality API (CAMS), from 2013
Raw days are cached in $ROLLBOOK_HOME/cache/weather.json, so a `db build
--fresh` fetches nothing again. A day fetched within RECENT_DAYS of itself is
provisional: refetched (once a day) until the reanalysis has settled, and its
session rows carry final = 0 until then.

Like map tiles, a request reveals the cell and dates to Open-Meteo. The
weather table holds no coordinates.
"""

import bisect
import datetime as dt
import http.client
import math
import os
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

import store

ARCHIVE = "https://archive-api.open-meteo.com/v1/archive"
FORECAST = "https://api.open-meteo.com/v1/forecast"
AIR = "https://air-quality-api.open-meteo.com/v1/air-quality"
# (weather column, Open-Meteo hourly variable)
WX = (("temp", "temperature_2m"), ("feels", "apparent_temperature"), ("rh", "relative_humidity_2m"),
      ("dew", "dew_point_2m"), ("precip", "precipitation"), ("rain", "rain"), ("snow", "snowfall"),
      ("code", "weather_code"), ("cloud", "cloud_cover"), ("pmsl", "pressure_msl"),
      ("psurf", "surface_pressure"), ("ws", "wind_speed_10m"), ("wd", "wind_direction_10m"),
      ("gust", "wind_gusts_10m"))
AQ = (("pm25", "pm2_5"), ("pm10", "pm10"), ("o3", "ozone"), ("aqi", "european_aqi"))
KINDS = {"wx": WX, "aq": AQ}
COLS = tuple(c for c, _ in WX + AQ)
STEP = ("precip", "rain", "snow")  # sums over the preceding hour
UA = "Rollbook/1.0 (personal training dashboard; weather cached)"
CELL = 10  # 1/10 degree
RECENT_DAYS = 7  # fetched this soon after the day: provisional
AQ_FROM = dt.date(2013, 1, 1)  # CAMS air quality starts here
MAX_RANGE = 62  # days per request
MERGE_DAYS = 7  # needed days this close share a request
MAX_HOURS = 48  # per session
GAP_S = 1.0  # between requests
MAX_FAILS = 3  # consecutive network errors: give up for this run
SAVE_EVERY = 20  # requests between cache saves
MAX_BAD = 5  # ranges in a row Open-Meteo couldn't answer (5xx): give up for this run
MAX_BODY = 8 << 20  # an answer bigger than this is not Open-Meteo's
MAX_CACHE = 256 << 20
EMPTY_RETRY_DAYS = 7  # a day Open-Meteo had nothing for: asked again weekly ...
GIVE_UP_DAYS = 60  # ... until this long after it: then there will be nothing
# plausible range per column; anything outside is not weather (a bad answer or cache), stored as NULL
BOUNDS = {"temp": (-95, 65), "feels": (-110, 80), "dew": (-95, 65), "rh": (0, 100), "precip": (0, 500),
          "rain": (0, 500), "snow": (0, 1000), "code": (0, 99), "cloud": (0, 100), "pmsl": (300, 1100),
          "psurf": (300, 1100), "ws": (0, 500), "wd": (0, 360), "gust": (0, 600), "pm25": (0, 5000),
          "pm10": (0, 5000), "o3": (0, 5000), "aqi": (0, 1000)}
VERSION = 2
NO_WEATHER = ("Virtual Ride", "Virtual Run", "Swim", "Workout", "Weight Training", "Yoga")

DDL = """
CREATE TABLE IF NOT EXISTS weather (
  activity_id INTEGER,
  t      TEXT,      -- UTC hour, ISO 'YYYY-MM-DDTHH:00:00+00:00'
  temp   REAL,      -- air temperature at 2 m, degC
  feels  REAL,      -- apparent (feels-like) temperature, degC
  rh     REAL,      -- relative humidity at 2 m, %
  dew    REAL,      -- dew point at 2 m, degC
  precip REAL,      -- precipitation (rain + snow) over the preceding hour, mm
  rain   REAL,      -- rain over the preceding hour, mm
  snow   REAL,      -- snowfall over the preceding hour, cm
  code   INTEGER,   -- WMO weather code
  cloud  REAL,      -- cloud cover, %
  pmsl   REAL,      -- pressure at mean sea level, hPa
  psurf  REAL,      -- surface pressure, hPa
  ws     REAL,      -- wind speed at 10 m, km/h
  wd     REAL,      -- wind direction at 10 m, deg, where it comes FROM
  gust   REAL,      -- wind gusts at 10 m, km/h
  pm25   REAL,      -- PM2.5, ug/m3 (air quality: NULL before 2013)
  pm10   REAL,      -- PM10, ug/m3
  o3     REAL,      -- ozone, ug/m3
  aqi    REAL,      -- European AQI
  final  INTEGER,   -- 0 = provisional (recent), refreshed by the next `db weather`
  PRIMARY KEY (activity_id, t)
);
CREATE VIEW IF NOT EXISTS v_weather AS
  SELECT activity_id, COUNT(*) AS hours,
         ROUND(AVG(temp), 1) AS temp, MIN(temp) AS temp_min, MAX(temp) AS temp_max,
         ROUND(AVG(feels), 1) AS feels, ROUND(AVG(rh)) AS rh, ROUND(SUM(precip), 1) AS precip,
         ROUND(AVG(cloud)) AS cloud, ROUND(AVG(ws), 1) AS ws, MAX(gust) AS gust_max,
         ROUND(AVG(pmsl), 1) AS pmsl, ROUND(AVG(pm25), 1) AS pm25, ROUND(AVG(pm10), 1) AS pm10,
         ROUND(AVG(o3)) AS o3, MAX(aqi) AS aqi_max, MIN(final) AS final
  FROM weather GROUP BY activity_id;
"""


def _date(v):
    try:
        return dt.date.fromisoformat(v) if isinstance(v, str) and len(v) == 10 else None
    except ValueError:
        return None


def cell_of(lat, lon):
    return f"{round(lat * CELL) / CELL + 0.0:.1f},{round(lon * CELL) / CELL + 0.0:.1f}"


def eligible(typ):
    """Outdoor sports only: no weather for virtual / indoor-only types."""
    typ = typ or ""
    return typ not in NO_WEATHER and not typ.startswith("Virtual")


def _num(v):
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    try:
        return v if math.isfinite(v) else None
    except OverflowError:  # an int too big for a float
        return None


def _bounded(col, v):
    v = _num(v)
    lo, hi = BOUNDS.get(col, (-1e9, 1e9))
    return v if v is not None and lo <= v <= hi else None


def _today():
    return dt.datetime.now(dt.timezone.utc).date()


# ------------------------------------------------------------------ cache


class Cache:
    """Raw Open-Meteo days: {"v": 2, "wx" | "aq": {cell: {date: {"at": date fetched, "h": [24 rows]}}}}."""

    def __init__(self, path):
        self.path, self.dirty = path, 0
        self.d = self._read()
        self.mine = []  # (kind, cell, date) put since the last save

    def _read(self):
        d = store.load_object(self.path, MAX_CACHE) if os.path.exists(self.path) else None
        if not d or d.get("v") != VERSION or not all(isinstance(d.get(k), dict) for k in KINDS):
            return {"v": VERSION, **{k: {} for k in KINDS}}  # missing, corrupt or the v1 format: start over
        # entries are checked when read (day()); cells / days of the wrong type are dropped here
        return {"v": VERSION, **{k: {c: {x: e for x, e in days.items() if isinstance(e, dict)}
                                     for c, days in d[k].items() if isinstance(days, dict)} for k in KINDS}}

    def day(self, kind, cell, date):
        """The cached entry for that day, or None (also when malformed)."""
        days = self.d[kind].get(cell)
        e = days.get(date.isoformat()) if isinstance(days, dict) else None
        if not isinstance(e, dict) or not isinstance(e.get("h"), list) or len(e["h"]) != 24:
            return None
        try:
            dt.date.fromisoformat(e.get("at"))
        except (TypeError, ValueError):
            return None
        if "tried" in e and _date(e["tried"]) is None:
            e.pop("tried")  # unreadable: as if never retried
        return e

    @staticmethod
    def last(e):
        """Date of the last attempt for this day (answered or not)."""
        return max(dt.date.fromisoformat(e["at"]), _date(e.get("tried")) or dt.date.min)

    @staticmethod
    def empty(e):
        return not any(isinstance(r, list) and any(v is not None for v in r) for r in e["h"])

    @staticmethod
    def settled(e, date):
        return (dt.date.fromisoformat(e["at"]) - date).days > RECENT_DAYS

    def done(self, e, date):
        """Nothing more will come for this day: settled values, or still tried
        long after the day (Open-Meteo has nothing better: keep what there is)."""
        return (self.settled(e, date) and not self.empty(e)) or (self.last(e) - date).days >= GIVE_UP_DAYS

    def need(self, kind, cell, date, today):
        """Fetch this day? Missing; provisional and not tried today yet; or
        empty, last tried a week or more ago, and not given up on."""
        if date > today or (kind == "aq" and date < AQ_FROM):
            return False
        e = self.day(kind, cell, date)
        if e is None:
            return True
        if self.done(e, date):
            return False
        if (self.last(e) - date).days <= RECENT_DAYS:  # not yet tried after the reanalysis settles: daily
            return self.last(e) < today
        return (today - self.last(e)).days >= EMPTY_RETRY_DAYS  # an answer that never came: weekly

    def put(self, kind, cell, date, rows, today):
        self.d[kind].setdefault(cell, {})[date] = {"at": today.isoformat(), "h": rows}
        self.mine.append((kind, cell, date))
        self.dirty += 1

    def update(self, kind, cell, date, rows, today):
        """A fresh answer for a day, merged into what is cached: a value Open-Meteo
        now has wins, an hour it now lacks keeps the old value; no answer at all
        (rows None) never blanks a cached day, it only notes the attempt."""
        day = dt.date.fromisoformat(date)
        old = self.day(kind, cell, day)
        blank = rows is None or not any(any(v is not None for v in r) for r in rows)
        if old is None or (blank and self.empty(old)):
            # nothing to lose: store the (empty) answer, so the day can settle and be given up on
            self.put(kind, cell, date, rows or [[None] * len(KINDS[kind]) for _ in range(24)], today)
            return
        if blank:
            if old.get("tried") != today.isoformat():
                old["tried"] = today.isoformat()
                self.mine.append((kind, cell, date))
                self.dirty += 1
            return
        if (today - day).days > RECENT_DAYS:  # the settled answer: old hours may be forecast, don't keep them
            self.put(kind, cell, date, rows, today)
            return
        n = len(KINDS[kind])
        merged = [[a if a is not None else b for a, b in zip(new, oldr if isinstance(oldr, list) and len(oldr) == n else [None] * n)]
                  for new, oldr in zip(rows, old["h"])]
        self.put(kind, cell, date, merged, today)

    def hour(self, kind, cell, h):
        """(values, done) for that UTC hour: values None when not cached / all null;
        done = nothing more will come for that day (see done())."""
        e = self.day(kind, cell, h.date())
        if e is None:
            return None, False
        row = e["h"][h.hour]
        fin = self.done(e, h.date())
        if not isinstance(row, list) or len(row) != len(KINDS[kind]):
            return None, fin
        vals = [_bounded(c, v) for (c, _), v in zip(KINDS[kind], row)]
        return (None if all(v is None for v in vals) else vals), fin

    def save(self):
        """Write what this process fetched over the file as it is now: another
        process (an import, the dashboard's on-demand fetch) may have saved since."""
        if self.dirty:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            disk = self._read()
            for kind, cell, date in self.mine:
                disk[kind].setdefault(cell, {})[date] = self.d[kind][cell][date]
            for kind in KINDS:  # and keep everything else this process read
                for cell, days in self.d[kind].items():
                    for date, e in days.items():
                        disk[kind].setdefault(cell, {}).setdefault(date, e)
            store.write_json(self.path, disk, indent=None)
            self.d, self.mine, self.dirty = disk, [], 0


# ------------------------------------------------------------------ fetching


class Abort(Exception):
    """Open-Meteo unreachable or rate-limiting us: stop this run, the next continues."""


def parse(body, pairs):
    """Open-Meteo `hourly` -> {date: [24 rows of values]}; None if malformed."""
    h = body.get("hourly") if isinstance(body, dict) else None
    times = h.get("time") if isinstance(h, dict) else None
    if not isinstance(times, list):
        return None
    cols = [h.get(v) for _, v in pairs]
    if any(not isinstance(c, list) or len(c) != len(times) for c in cols):
        return None
    days = {}
    for i, t in enumerate(times):
        if not isinstance(t, str):
            return None
        try:
            d, hr = dt.date.fromisoformat(t[:10]), int(t[11:13])
        except (TypeError, ValueError):
            return None
        if not 0 <= hr < 24:
            return None
        days.setdefault(d, [None] * 24)[hr] = [_bounded(k, c[i]) for (k, _), c in zip(pairs, cols)]
    return {d: [r or [None] * len(pairs) for r in rows] for d, rows in days.items()}


def ranges(dates):
    """Sorted dates -> [(first, last)], close ones merged, at most MAX_RANGE days each."""
    out = []
    for d in sorted(dates):
        if out and (d - out[-1][1]).days <= MERGE_DAYS and (d - out[-1][0]).days < MAX_RANGE:
            out[-1][1] = d
        else:
            out.append([d, d])
    return [tuple(r) for r in out]


class Fetcher:
    def __init__(self, cache, today=None, log=print, urlopen=None, sleep=None, gap=GAP_S, max_wait=600,
                 deadline=None, clock=time.monotonic):
        self.cache, self.today, self.log, self.gap = cache, today or _today(), log, gap
        self.urlopen = urlopen or urllib.request.urlopen
        self.sleep = sleep or time.sleep
        self.max_wait = max_wait  # longest wait on a 429 / 5xx (the dashboard's run is short)
        self.deadline, self.clock = deadline, clock  # clock() past deadline: stop, store what we have
        self.fails, self.bad, self.requests, self.aborted = 0, 0, 0, None

    def run(self, needs):
        """needs: {(kind, cell): {dates}}. Newest ranges first, so an interrupted
        backfill has the recent sessions. Never raises for network trouble."""
        jobs = sorted(((b, kind, cell, a) for (kind, cell), ds in needs.items() for a, b in ranges(ds)),
                      reverse=True)
        if not jobs:
            return
        self.log(f"weather: {len(jobs)} requests to Open-Meteo for {len({c for _, _, c, _ in jobs})} places")
        try:
            for i, (b, kind, cell, a) in enumerate(jobs, 1):
                self.fetch(kind, cell, a, b)
                if i % SAVE_EVERY == 0:
                    try:
                        self.cache.save()
                    except OSError as e:
                        self.log(f"weather: cache not saved ({e})")
                if i % 10 == 0 or i == len(jobs):
                    self.log(f"  {i}/{len(jobs)} weather requests")
        except Abort as e:
            self.aborted = str(e)
            self.log(f"weather: stopped ({e}); the next `db weather` continues")
        finally:
            try:
                self.cache.save()
            except OSError as e:  # the rows can still be stored from memory
                self.log(f"weather: cache not saved ({e})")

    def fetch(self, kind, cell, a, b):
        lat, lon = cell.split(",")
        pairs = KINDS[kind]
        b = min(b, self.today)
        q = {"latitude": lat, "longitude": lon, "hourly": ",".join(v for _, v in pairs), "timezone": "UTC"}
        if kind == "wx":
            q["wind_speed_unit"] = "kmh"
            days = self.get(ARCHIVE, {**q, "start_date": a.isoformat(), "end_date": b.isoformat()}, pairs)
            days = self._fill_recent(q, pairs, days or {}, a, b)
        else:
            days = self.get(AIR, {**q, "start_date": a.isoformat(), "end_date": b.isoformat()}, pairs)
        for i in range((b - a).days + 1):
            # no answer: an uncached day is stored empty (asked again tomorrow if recent, else
            # weekly); a cached one is kept as it is
            d = a + dt.timedelta(days=i)
            self.cache.update(kind, cell, d.isoformat(), (days or {}).get(d), self.today)

    def _fill_recent(self, q, pairs, days, a, b):
        """Hours the archive has no wind / temperature for yet, from the forecast API."""
        ti, wi = [c for c, _ in pairs].index("temp"), [c for c, _ in pairs].index("ws")
        gap = lambda r: r[ti] is None or r[wi] is None  # noqa: E731
        holes = [d for d in (a + dt.timedelta(days=i) for i in range((b - a).days + 1))
                 if (self.today - d).days <= RECENT_DAYS and (d not in days or any(gap(r) for r in days[d]))]
        if not holes:
            return days
        f = self.get(FORECAST, {**q, "past_days": min(92, (self.today - min(holes)).days + 1),
                                "forecast_days": 1}, pairs) or {}
        for d in holes:
            if d in f:
                base = days.get(d) or [[None] * len(pairs) for _ in range(24)]
                days[d] = [fr if gap(ar) else ar for ar, fr in zip(base, f[d])]
        return days

    def get(self, url, params, pairs):
        """One request -> parse() result; None for an answer we can't use (skipped).
        429 / 5xx wait and retry; repeated failures raise Abort."""
        full = f"{url}?{urllib.parse.urlencode(params)}"
        for attempt in range(4):
            if self.deadline is not None and self.clock() >= self.deadline:
                raise Abort("out of time")
            try:
                req = urllib.request.Request(full, headers={"User-Agent": UA})
                with self.urlopen(req, timeout=60) as r:
                    raw = r.read(MAX_BODY + 1)
                self.fails = self.bad = 0
                self.requests += 1
                self.sleep(self.gap)
                try:
                    days = parse(store.loads(raw), pairs) if len(raw) <= MAX_BODY else None
                except Exception:  # noqa: BLE001  whatever came back, it isn't usable weather
                    days = None
                if days is None:
                    self.log(f"weather: unreadable answer for {params.get('latitude')},{params.get('longitude')}, skipped")
                return days
            except urllib.error.HTTPError as e:
                if e.code != 429 and e.code < 500:
                    self.log(f"weather: HTTP {e.code} for {params.get('latitude')},{params.get('longitude')}, skipped")
                    self.sleep(self.gap)
                    return None
                if attempt == 3:
                    if e.code == 429:
                        raise Abort("Open-Meteo answers HTTP 429 (rate limit)")
                    self.bad += 1  # a 5xx on this range: skip it, unless it's every range
                    if self.bad >= MAX_BAD:
                        raise Abort(f"Open-Meteo answers HTTP {e.code}")
                    break
                try:
                    wait = int((e.headers or {}).get("Retry-After") or 0)
                except (TypeError, ValueError):
                    wait = 0
                wait = min(self.max_wait, max(wait, min(self.max_wait, 30 * (attempt + 1))))
                self.log(f"weather: HTTP {e.code}, waiting {wait} s")
                self.sleep(wait)
            except (OSError, http.client.HTTPException) as e:  # URLError, timeouts, resets, IncompleteRead
                self.fails += 1
                if self.fails >= MAX_FAILS:
                    raise Abort(f"network: {getattr(e, 'reason', e)}")
                self.sleep(5)
        self.log(f"weather: no answer for {params.get('latitude')},{params.get('longitude')} "
                 f"{params.get('start_date', '')}, skipped; the next run retries")
        return None


# ------------------------------------------------------------------ sessions -> rows


def hours_of(t0, dur_s):
    """UTC hours from the one at/before the start to the one at/after the end."""
    a = t0.astimezone(dt.timezone.utc).replace(minute=0, second=0, microsecond=0)
    end = t0 + dt.timedelta(seconds=max(0.0, dur_s or 0.0))
    n = math.ceil((end - a).total_seconds() / 3600)
    return [a + dt.timedelta(hours=k) for k in range(min(MAX_HOURS, n) + 1)]


def _valid(lat, lon):
    return (lat is not None and lon is not None and -90 <= lat <= 90 and -180 <= lon <= 180
            and not (abs(lat) < 1e-9 and abs(lon) < 1e-9))


def positions(track, t0, hours):
    """Per hour, the (lat, lon) of the GPS fix nearest that time; track = [(t s, lat, lon)]."""
    ts = [r[0] for r in track]
    out = []
    for h in hours:
        x = (h - t0).total_seconds()
        i = bisect.bisect_left(ts, x)
        j = min(range(max(0, i - 1), min(len(ts), i + 1)), key=lambda k: abs(ts[k] - x))
        out.append((track[j][1], track[j][2]))
    return out


def fill(db, cache, fetcher=None, missing=True, log=print, today=None, ids=None):
    """Store weather rows for outdoor sessions; fetcher None = cache only (offline).
    missing: only sessions with no rows, or provisional / incomplete ones.
    ids: only these sessions (the dashboard opening one), whatever they have."""
    today = today or (fetcher.today if fetcher else _today())
    db.executescript(DDL)
    db.execute("DELETE FROM weather WHERE activity_id NOT IN "
               "(SELECT id FROM main.activities WHERE parse_error IS NULL AND has_gps = 1)")
    db.commit()  # never hold the write lock while fetching: an import may be writing too
    todo = _select(db, missing, ids)
    plans, needs = [], {}
    for aid, _typ, start, elapsed in todo:
        try:
            t0 = dt.datetime.fromisoformat(start)
            if t0.tzinfo is None:
                t0 = t0.replace(tzinfo=dt.timezone.utc)
            track = [r for r in db.execute("SELECT t, lat, lon FROM samples WHERE activity_id=? "
                                           "AND lat IS NOT NULL ORDER BY t", (aid,)) if _valid(r[1], r[2])]
            if not track:
                continue
            hs = hours_of(t0, min(max(track[-1][0], elapsed or 0), MAX_HOURS * 3600))
            cells = [cell_of(*p) for p in positions(track, t0, hs)]
        except (TypeError, ValueError, OverflowError):  # e.g. a start in year 9999: skip that one
            continue
        for h, c in zip(hs, cells):
            for kind in KINDS:
                if cache.need(kind, c, h.date(), today):
                    needs.setdefault((kind, c), set()).add(h.date())
        plans.append((aid, hs, cells))
    res = {"sessions": 0, "without": 0, "aborted": None, "stored": set()}
    if not plans:
        return res
    if needs and fetcher is None:
        log(f"weather: offline, {len(plans)} sessions from the cache only")
    elif needs:
        fetcher.run(needs)
        res["aborted"] = fetcher.aborted
    stored = 0
    for aid, hs, cells in plans:
        rows, complete = [], True
        for h, c in zip(hs, cells):
            wx, fw = cache.hour("wx", c, h)
            aq, fa = cache.hour("aq", c, h)
            if wx is None:
                complete = complete and fw  # a gap for good doesn't keep the session provisional
                continue
            fin = fw and (fa or h.date() < AQ_FROM)
            rows.append([aid, h.isoformat(), *wx, *(aq or [None] * len(AQ)), int(fin)])
        if not rows:
            continue
        if not complete:
            for r in rows:
                r[-1] = 0  # an hour is missing: try again next time
        db.execute("DELETE FROM weather WHERE activity_id=?", (aid,))
        db.executemany(f"INSERT INTO weather (activity_id, t, {', '.join(COLS)}, final) "
                       f"VALUES ({', '.join('?' * (len(COLS) + 3))})", rows)
        stored += 1
        res["stored"].add(aid)
    db.commit()
    log(f"weather: {stored} sessions stored" + (f", {len(plans) - stored} still without" if stored < len(plans) else ""))
    res.update(sessions=stored, without=len(plans) - stored)
    return res


def _select(db, missing, ids):
    """[(id, type, datetime, elapsed_s)] to look up. With the dashboard's edits
    overlay installed (`db weather` does), types are the edited ones and hidden
    sessions count too, like the session page that asked."""
    src = "activities_all" if db.execute(
        "SELECT 1 FROM sqlite_temp_master WHERE name = 'activities_all'").fetchone() else "activities"
    # a deleted session is only looked up when its page asks (--id); never in bulk
    alive = " AND NOT deleted" if src == "activities_all" and ids is None else ""
    rows = db.execute(f"SELECT id, type, datetime, elapsed_s FROM {src} "
                      f"WHERE parse_error IS NULL AND has_gps = 1{alive} ORDER BY datetime").fetchall()
    rows = [r for r in rows if eligible(r[1])]
    if ids is not None:
        want = set(ids)
        return [r for r in rows if r[0] in want]
    if not missing:
        return rows
    have = dict(db.execute("SELECT activity_id, MIN(final) FROM weather GROUP BY activity_id"))
    return [r for r in rows if have.get(r[0]) != 1]


# ------------------------------------------------------------------ reading (dashboard, MCP)


def start_ts(iso):
    """activities.datetime -> UTC epoch s (naive = UTC); None if unreadable."""
    try:
        t0 = dt.datetime.fromisoformat(iso)
    except (TypeError, ValueError):
        return None
    return (t0 if t0.tzinfo else t0.replace(tzinfo=dt.timezone.utc)).timestamp()


def load(db, ids=None):
    """{activity id: [(UTC epoch s, {"t": ISO hour, col: value, "final": 0|1})]}, hours in order.
    {} on a DB built before the weather table existed."""
    sql = f"SELECT activity_id, t, {', '.join(COLS)}, final FROM weather"
    args = []
    if ids is not None:
        ids = list(ids)
        if not ids:
            return {}
        sql += f" WHERE activity_id IN ({', '.join('?' * len(ids))})"
        args = ids
    try:
        cur = db.execute(sql + " ORDER BY activity_id, t", args)
    except sqlite3.OperationalError:
        return {}
    out = {}
    for r in map(tuple, cur):  # the dashboard's connections return sqlite3.Row
        try:
            ts = dt.datetime.fromisoformat(r[1]).timestamp()
        except (TypeError, ValueError):
            continue
        v = {"t": r[1], **dict(zip(COLS, r[2:2 + len(COLS)])), "final": r[-1]}
        out.setdefault(r[0], []).append((ts, v))
    return out


def at(hours, ts):
    """Values at UTC epoch ts: linear between the hours around it (wind direction
    as a vector), precipitation from the hour that covers ts, the weather code
    from the nearest hour. Within an hour of either end the end hour is used;
    None further out or without hours."""
    if not hours:
        return None
    xs = [h for h, _ in hours]
    if ts < xs[0] - 3600 or ts > xs[-1] + 3600:
        return None
    i = bisect.bisect_right(xs, ts)
    if i == 0 or i == len(xs):
        v = hours[0][1] if i == 0 else hours[-1][1]
        return {k: v.get(k) for k in COLS}
    (x0, a), (x1, b) = hours[i - 1], hours[i]
    f = (ts - x0) / (x1 - x0) if x1 > x0 else 0.0
    out = {}
    for k in COLS:
        p, q = a.get(k), b.get(k)
        if k in STEP:
            out[k] = q if f > 0 else p
        elif k == "code":
            out[k] = p if f < 0.5 else q
        elif p is None or q is None:
            out[k] = p if f < 0.5 else q
        else:
            out[k] = p + (q - p) * f
    if all(_num(x) is not None for x in (a.get("ws"), a.get("wd"), b.get("ws"), b.get("wd"))):
        u = (1 - f) * a["ws"] * math.sin(math.radians(a["wd"])) + f * b["ws"] * math.sin(math.radians(b["wd"]))
        w = (1 - f) * a["ws"] * math.cos(math.radians(a["wd"])) + f * b["ws"] * math.cos(math.radians(b["wd"]))
        if abs(u) > 1e-9 or abs(w) > 1e-9:
            out["wd"] = math.degrees(math.atan2(u, w)) % 360
    return out


def summary(hours, t0, dur):
    """The session's weather: values at its middle, plus temperature range,
    precipitation total, strongest gust, worst air and weather code over it."""
    end = t0 + max(0.0, dur or 0.0)
    mid = at(hours, t0 + (end - t0) / 2)
    if mid is None:
        return None
    pts = [p for p in (at(hours, t0), at(hours, end)) if p] + [v for h, v in hours if t0 <= h <= end]
    vals = lambda k: [p[k] for p in pts if _num(p.get(k)) is not None]  # noqa: E731
    covers = [v for h, v in hours if h > t0 and h - 3600 < end]  # precipitation is per preceding hour
    near = [v for h, v in hours if t0 - 1800 <= h <= end + 1800]
    c = dict(mid)
    temps, gusts, aqis = vals("temp"), vals("gust"), vals("aqi")
    codes = [v["code"] for v in near if _num(v.get("code")) is not None]
    rain = [v["precip"] for v in covers if _num(v.get("precip")) is not None]
    c.update(temp_min=min(temps) if temps else None, temp_max=max(temps) if temps else None,
             gust_max=max(gusts) if gusts else None, aqi_max=max(aqis) if aqis else None,
             precip_sum=sum(rain) if rain else None, code=max(codes) if codes else mid.get("code"),
             final=int(all(v.get("final") for _, v in hours)))
    return c


# ------------------------------------------------------------------ on demand (dashboard)


class OnDemand:
    """Background `rollbook db weather` runs for the dashboard (its DB connections
    are read-only, so a subprocess writes):
      request(id)  a session page opened with no weather: `--id <id>`; ids asked
                   for meanwhile go in the next run; each id once per RETRY_S, so
                   a page can't loop on one Open-Meteo has nothing for.
      backfill()   after a dashboard import: `--missing`, so the import itself
                   never waits on the network.
    One run at a time. Nothing with --offline / ROLLBOOK_OFFLINE=1; no
    requests while an import runs (its backfill follows)."""

    RETRY_S = 600
    TIMEOUT_S = 300  # an --id run; `db weather` caps its waits at 60 s there
    BACKFILL_TIMEOUT_S = 6 * 3600

    def __init__(self, home, tool, offline=False, busy=None):
        self.home, self.tool = home, tool
        self.offline = offline or os.environ.get("ROLLBOOK_OFFLINE") == "1"
        self.busy = busy or (lambda: False)
        self.lock = threading.Lock()
        self.state = {}  # id -> {"state": queued|running|done|error, "at": monotonic}
        self.queue, self.running, self.backfill_due = [], False, False

    def status(self, aid):
        if self.offline:
            return {"state": "offline"}
        with self.lock:
            st = self.state.get(aid)
            return {"state": st["state"]} if st else {"state": "idle"}

    def request(self, aid):
        if self.offline:
            return {"state": "offline"}
        if self.busy():
            return {"state": "busy"}
        with self.lock:
            st = self.state.get(aid)
            if st and (st["state"] in ("queued", "running") or time.monotonic() - st["at"] < self.RETRY_S):
                return {"state": st["state"]}
            self.state[aid] = {"state": "queued", "at": time.monotonic()}
            self.queue.append(aid)
            self._start()
        return {"state": "queued"}

    def backfill(self):
        if self.offline:
            return
        with self.lock:
            self.backfill_due = True
            self._start()

    def _start(self):  # under self.lock
        if not self.running:
            self.running = True
            threading.Thread(target=self._work, daemon=True).start()

    def _work(self):
        batch, clean = [], False
        try:
            while True:
                with self.lock:  # a session page waiting comes before the backfill
                    batch, self.queue = self.queue[:20], self.queue[20:]
                    fill_all = not batch and self.backfill_due
                    if fill_all:
                        self.backfill_due = False
                    if not batch and not fill_all:
                        self.running, clean = False, True
                        return
                    for aid in batch:
                        self.state[aid] = {"state": "running", "at": time.monotonic()}
                if fill_all:
                    self._run(["--missing"], self.BACKFILL_TIMEOUT_S)
                    continue
                ok = self._run([x for i in batch for x in ("--id", str(i))], self.TIMEOUT_S)
                with self.lock:
                    for aid in batch:
                        self.state[aid] = {"state": "done" if ok else "error", "at": time.monotonic()}
                batch = []
        finally:  # a dead thread must not leave `running` set: requests would queue forever
            if not clean:
                with self.lock:
                    for aid in batch + self.queue:
                        self.state[aid] = {"state": "error", "at": time.monotonic()}
                    self.queue, self.running = [], False

    def _run(self, extra, timeout):
        args = [sys.executable, self.tool, "db", "weather", *extra]
        env = dict(os.environ, ROLLBOOK_HOME=self.home)
        try:
            return subprocess.run(args, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                  timeout=timeout).returncode == 0
        except (OSError, subprocess.SubprocessError):
            return False
