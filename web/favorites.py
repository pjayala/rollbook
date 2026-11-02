# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Pedro Ayala. Rollbook is free software, see LICENSE.

import json
import re
import threading
import time

import edits
import store

FAV_MAX = 5000  # per kind
FAV_KEY = re.compile(r"[0-9a-zA-Z]{1,24}")
STORE_MAX = 8 << 20  # edits / profile / favorites file size read at start


class Favorites:
    """Starred sessions and segments, in $ROLLBOOK_HOME/favorites.json.

    Server-side rather than localStorage so the app window and a browser tab
    share them. Segments also keep type/start/end/length: segment ids hash
    their geometry and change when new data regenerates them, and the page
    uses these to re-find a starred segment.
    """

    def __init__(self, path):
        self.path, self.lock = path, threading.Lock()
        d = store.load_object(path, STORE_MAX) or {}
        self.data = {}
        for k in ("sessions", "segments"):  # the file is checked like a request
            b = d.get(k) if isinstance(d.get(k), dict) else {}
            ok = {}
            for i, v in b.items():
                if len(ok) < FAV_MAX and FAV_KEY.fullmatch(i) and isinstance(v, dict):
                    added = v.get("added")
                    ok[i] = {**({"added": added} if isinstance(added, str) and len(added) <= 10 else {}),
                             **(self._meta(v) if k == "segments" else {})}
            self.data[k] = ok

    def get(self):
        with self.lock:
            return json.loads(json.dumps(self.data))

    @staticmethod
    def _meta(meta):
        """What a starred segment keeps to be re-found: sport, end points, length."""
        if not isinstance(meta, dict):
            return {}
        out = {}
        t = meta.get("type")
        if isinstance(t, str) and 0 < len(t) <= edits.TYPE_MAX:
            out["type"] = t
        for k in ("start", "end"):
            p = meta.get(k)
            if (isinstance(p, list) and len(p) == 2 and all(type(x) in (int, float) for x in p)
                    and -90 <= p[0] <= 90 and -180 <= p[1] <= 180):
                out[k] = p
        m = meta.get("m")
        if type(m) in (int, float) and 0 < m < 1e6:
            out["m"] = m
        return out

    def set(self, kind, key, on, meta=None):
        if kind not in ("sessions", "segments") or not FAV_KEY.fullmatch(str(key)):
            raise ValueError("bad favorite")
        with self.lock:
            data = json.loads(json.dumps(self.data))
            bucket = data[kind]
            if on:
                if str(key) not in bucket and len(bucket) >= FAV_MAX:
                    raise ValueError(f"at most {FAV_MAX} favorite {kind}")
                bucket[str(key)] = {"added": time.strftime("%Y-%m-%d"), **self._meta(meta)}
            else:
                bucket.pop(str(key), None)
            store.write_json(self.path, data)  # memory changes only once it is on disk
            self.data = data
            return json.loads(json.dumps(data))
