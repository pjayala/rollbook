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

import store

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


CACHE_MAX = 32 << 20
MAX_ANSWER = 1 << 20


def _place(v):
    """A cached place {road, area, city}: each text or null; None for anything else."""
    if not isinstance(v, dict) or set(v) != {"road", "area", "city"}:
        return None
    out = {k: v.get(k) for k in ("road", "area", "city")}
    return out if all(x is None or (isinstance(x, str) and len(x) <= 200) for x in out.values()) else None


class Geonames:
    def __init__(self, path, offline=False):
        self.path, self.offline = path, offline
        self.lock = threading.Lock()
        self.q, self.queued = queue.Queue(), set()
        self.cache = {k: v for k, v in (store.load_object(path, CACHE_MAX) or {}).items()
                      if isinstance(k, str) and _place(v) is not None}
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
            try:
                place = self._fetch(lat, lon)
            except Exception:  # noqa: BLE001  one odd answer must not end segment naming
                place = None
                self.failures += 1
            with self.lock:
                self.queued.discard(k)
                if place is not None:
                    self.cache[k] = place
                    try:
                        self._save()
                    except OSError:
                        pass
            time.sleep(GAP_S if self.failures < 3 else 30)

    def _fetch(self, lat, lon):
        qs = urllib.parse.urlencode({"format": "jsonv2", "lat": f"{lat:.5f}", "lon": f"{lon:.5f}",
                                     "zoom": 17, "addressdetails": 1, "accept-language": "es,en"})
        try:
            req = urllib.request.Request(f"{URL}?{qs}", headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=15) as r:
                raw = r.read(MAX_ANSWER + 1)
            d = store.loads(raw) if len(raw) <= MAX_ANSWER else None
        except (OSError, ValueError, RecursionError):
            d = None
        if not isinstance(d, dict):
            self.failures += 1
            return None
        self.failures = 0
        a = d.get("address") if isinstance(d.get("address"), dict) else {}
        a = {x: v[:120] for x, v in a.items() if isinstance(v, str)}
        d = {x: (v[:120] if isinstance(v, str) else None) for x, v in d.items() if x in ("category", "type", "name")}
        road = next((a[x] for x in ROAD_KEYS if a.get(x)), None)
        if not road and (d.get("category") == "highway" or d.get("type") in ("path", "footway", "cycleway")):
            road = d.get("name") or None
        return {"road": road,
                "area": next((a[x] for x in AREA_KEYS if a.get(x)), None),
                "city": a.get("city") or a.get("town") or a.get("village") or a.get("municipality")}

    def _save(self):  # caller holds the lock
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        store.write_json(self.path, self.cache, indent=None)  # 0600: the keys are where you skate


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
