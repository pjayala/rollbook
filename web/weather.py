# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Pedro Ayala. Rollbook is free software, see LICENSE.
"""Historical weather for outdoor sessions, from Open-Meteo (free, no key).

Hourly temperature, wind speed / direction and gusts at 10 m, per ~0.1 deg
cell and UTC day, cached forever in $ROLLBOOK_HOME/cache/weather.json.
Older days come from the ERA5-based archive API; the last week (not yet in
the archive) from the forecast API's past_days. Requests run in one
background thread, batched per cell. With --offline nothing is fetched.

Like map tiles, a request reveals the cell and dates to Open-Meteo.
"""

import datetime as dt
import json
import os
import queue
import threading
import time
import urllib.parse
import urllib.request

ARCHIVE = "https://archive-api.open-meteo.com/v1/archive"
FORECAST = "https://api.open-meteo.com/v1/forecast"
VARS = ("temperature_2m", "wind_speed_10m", "wind_direction_10m", "wind_gusts_10m")
UA = "Rollbook/1.0 (personal training dashboard; weather cached)"
CELL = 10  # 1/10 degree
RECENT_DAYS = 7  # newer than this: forecast API
GAP_S = 1.0


def cell_of(lat, lon):
    return f"{round(lat * CELL) / CELL:.1f},{round(lon * CELL) / CELL:.1f}"


class Weather:
    def __init__(self, path, offline=False):
        self.path, self.offline = path, offline
        self.lock = threading.Lock()
        self.queued = {}  # cell -> set(dates)
        self.wake = threading.Event()
        try:
            with open(path) as f:
                self.data = json.load(f)  # {cell: {date: [[t,ws,wd,gust] x 24]}}
        except (OSError, ValueError):
            self.data = {}
        if not offline:
            threading.Thread(target=self._worker, daemon=True).start()

    def at(self, lat, lon, when):
        """{temp, ws, wd, gust} at the nearest hour (UTC datetime), or None (queued)."""
        when = when.astimezone(dt.timezone.utc) + dt.timedelta(minutes=30)
        c, d = cell_of(lat, lon), when.strftime("%Y-%m-%d")
        with self.lock:
            day = self.data.get(c, {}).get(d)
            if day is None:
                if not self.offline:
                    self.queued.setdefault(c, set()).add(d)
                    self.wake.set()
                return None
        h = day[when.hour] if day and len(day) > when.hour else None
        if not h or h[1] is None:
            return None
        return {"temp": h[0], "ws": h[1], "wd": h[2], "gust": h[3]}

    def pending(self):
        with self.lock:
            return sum(len(v) for v in self.queued.values())

    def _worker(self):
        while True:
            self.wake.wait()
            with self.lock:
                jobs, self.queued = self.queued, {}
                self.wake.clear()
            for c, dates in jobs.items():
                today = dt.datetime.now(dt.timezone.utc).date()
                ds = sorted(dt.date.fromisoformat(x) for x in dates)
                old = [x for x in ds if (today - x).days > RECENT_DAYS]
                new = [x for x in ds if (today - x).days <= RECENT_DAYS]
                while old:  # at most one year per archive request
                    chunk = [x for x in old if (x - old[0]).days < 366]
                    old = old[len(chunk):]
                    self._fetch(c, ARCHIVE, {"start_date": chunk[0], "end_date": chunk[-1]})
                if new:
                    self._fetch(c, FORECAST, {"past_days": min(92, (today - new[0]).days + 1), "forecast_days": 1})

    def _fetch(self, c, url, extra):
        lat, lon = c.split(",")
        qs = urllib.parse.urlencode({"latitude": lat, "longitude": lon, "hourly": ",".join(VARS),
                                     "timezone": "UTC", "wind_speed_unit": "kmh", **extra})
        try:
            req = urllib.request.Request(f"{url}?{qs}", headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=60) as r:
                h = json.load(r)["hourly"]
        except (OSError, ValueError, KeyError):
            time.sleep(30)
            return
        days = {}
        for i, t in enumerate(h["time"]):
            days.setdefault(t[:10], []).append([h[v][i] for v in VARS])
        with self.lock:
            dst = self.data.setdefault(c, {})
            for d, rows in days.items():
                if len(rows) == 24 and any(r[1] is not None for r in rows):
                    dst[d] = rows
            tmp = self.path + ".tmp"
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            with open(tmp, "w") as f:
                json.dump(self.data, f)
            os.replace(tmp, self.path)
        time.sleep(GAP_S)
