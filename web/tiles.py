# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Pedro Ayala. Rollbook is free software, see LICENSE.
"""Basemap tiles: proxied through the server and cached on disk forever, so the
browser never talks to a third party and a once-viewed area keeps working
offline. Fetches follow the OSM tile usage policy (identifying User-Agent, at
most UPSTREAM at a time, each tile fetched once).
"""

import os
import threading
import time
import urllib.error
import urllib.request

TILE_SOURCES = {  # CARTO needs an API key now; the dark look is a CSS filter client-side
    "osm": "https://tile.openstreetmap.org/{z}/{x}/{y}.png",
}
TILE_UA = "Rollbook/1.0 (personal training dashboard, single-user; tiles cached; +https://github.com/pjayala/rollbook)"
TILE_MAXZ = 19


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
