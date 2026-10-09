# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Pedro Ayala. Rollbook is free software, see LICENSE.
"""Place names for segments: road and area from OpenStreetMap's Nominatim.

Each segment is sampled at its start, end and every ~1 km in between; each
point is reverse-geocoded once and cached forever in
$ROLLBOOK_HOME/cache/geonames.json, keyed on a ~50 m grid so neighbouring
segments share lookups. Requests go out from one background thread at most
one per GAP_S seconds with an identifying User-Agent, per Nominatim's usage
policy. With --offline nothing is fetched and only cached names are used.

Like map tiles, this reveals the looked-up coordinates to the OSM servers.
"""

import json
import math
import os
import queue
import threading
import time
import urllib.parse
import urllib.request

URL = "https://nominatim.openstreetmap.org/reverse"
UA = "Rollbook/1.0 (personal training dashboard; reverse geocoding cached)"
GAP_S = 1.1
GRID = 2000  # key = coordinates rounded to 1/2000 deg (~55 m)
STEP_M = 1000
MAX_POINTS = 8
AREA_KEYS = ("suburb", "village", "town", "hamlet", "city_district", "quarter", "city", "municipality")
ROAD_KEYS = ("road", "footway", "cycleway", "path", "pedestrian")


def _key(lat, lon):
    return f"{round(lat * GRID) / GRID:.4f},{round(lon * GRID) / GRID:.4f}"


def _metres(a, b):
    dy = (b[0] - a[0]) * 110540
    dx = (b[1] - a[1]) * 111320 * math.cos(math.radians(a[0]))
    return math.hypot(dx, dy)


def sample(pts):
    """Start, end, and points every STEP_M along a [[lat, lon], ...] line."""
    out, acc, nxt = [pts[0]], 0.0, STEP_M
    for a, b in zip(pts, pts[1:]):
        acc += _metres(a, b)
        if acc >= nxt:
            out.append(b)
            nxt += STEP_M
    out.append(pts[-1])
    if len(out) > MAX_POINTS:  # keep ends, thin the middle evenly
        mid = out[1:-1]
        k = MAX_POINTS - 2
        out = [out[0]] + [mid[round(i * (len(mid) - 1) / (k - 1))] for i in range(k)] + [out[-1]]
    return out


class Geonames:
    def __init__(self, path, offline=False):
        self.path, self.offline = path, offline
        self.lock = threading.Lock()
        self.q, self.queued = queue.Queue(), set()
        try:
            with open(path) as f:
                self.cache = json.load(f)
        except (OSError, ValueError):
            self.cache = {}
        self.failures = 0
        if not offline:
            threading.Thread(target=self._worker, daemon=True).start()

    def lookup(self, lat, lon):
        """Cached place {road, area, city} or None; None queues a fetch."""
        k = _key(lat, lon)
        with self.lock:
            if k in self.cache:
                return self.cache[k]
            if not self.offline and k not in self.queued:
                self.queued.add(k)
                self.q.put((k, lat, lon))
        return None

    def pending(self):
        return 0 if self.offline else len(self.queued)

    def _worker(self):
        while True:
            k, lat, lon = self.q.get()
            place = self._fetch(lat, lon)
            with self.lock:
                self.queued.discard(k)
                if place is not None:
                    self.cache[k] = place
                    self._save()
            time.sleep(GAP_S if self.failures < 3 else 30)

    def _fetch(self, lat, lon):
        qs = urllib.parse.urlencode({"format": "jsonv2", "lat": f"{lat:.5f}", "lon": f"{lon:.5f}",
                                     "zoom": 17, "addressdetails": 1, "accept-language": "es,en"})
        try:
            req = urllib.request.Request(f"{URL}?{qs}", headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=15) as r:
                d = json.load(r)
        except (OSError, ValueError):
            self.failures += 1
            return None
        self.failures = 0
        a = d.get("address") or {}
        road = next((a[x] for x in ROAD_KEYS if a.get(x)), None)
        if not road and (d.get("category") == "highway" or d.get("type") in ("path", "footway", "cycleway")):
            road = d.get("name") or None
        return {"road": road,
                "area": next((a[x] for x in AREA_KEYS if a.get(x)), None),
                "city": a.get("city") or a.get("town") or a.get("village") or a.get("municipality")}

    def _save(self):  # caller holds the lock
        tmp = self.path + ".tmp"
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        with open(tmp, "w") as f:
            json.dump(self.cache, f)
        os.replace(tmp, self.path)


def describe(geo, seg):
    """{road, roads, from, to, city, done} for a segment, from cached places."""
    places = [geo.lookup(p[0], p[1]) for p in sample(seg["pts"])]
    got = [p for p in places if p]
    roads = {}
    for p in got:
        if p.get("road"):
            roads[p["road"]] = roads.get(p["road"], 0) + 1
    ranked = sorted(roads, key=lambda r: -roads[r])
    main = ranked[0] if ranked and roads[ranked[0]] >= max(2, 0.4 * len(got)) else None
    area = lambda p: p and (p.get("area") or p.get("city"))
    return {"road": main, "roads": ranked[:4], "from": area(places[0]), "to": area(places[-1]),
            "city": next((p["city"] for p in got if p.get("city")), None),
            "done": len(got) == len(places)}
