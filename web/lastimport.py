# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Pedro Ayala. Rollbook is free software, see LICENSE.
"""What the last dashboard import added, for the "new records" summary
(issue #17), in $ROLLBOOK_HOME/last_import.json.

The server only remembers which sessions arrived; the page works out the
records from data it already loads, so the summary follows later edits and
regenerated segments. Kept until dismissed: an import made before the
previous summary was dismissed merges into it instead of replacing it.

The file is treated as untrusted input on load (size cap, every field
type-checked; anything off = no summary), written 0600 and atomically, and
held in memory so a GET never opens it (Windows can't replace a file that
is open). The MCP server deliberately doesn't read it: `items` holds file
names, and tool output goes to a model provider.
"""

import json
import os
import threading
import time

MAX_FILE = 256 * 1024
MAX_IDS = 500     # a first import into a big DB stores none; others keep the newest 500
MAX_ITEMS = 50
MAX_ITEM_LEN = 120
MAX_ID = 2 ** 53  # beyond this a JS number loses precision -> wrong link
SOURCES = ("upload", "strava", "mixed")


def _id_ok(x):
    return type(x) is int and 0 < x < MAX_ID


def _num(x):
    return type(x) in (int, float) and x == x and abs(x) != float("inf")


def validate(d):
    """A clean copy of a stored summary, or None if anything is off."""
    if not isinstance(d, dict):
        return None
    try:
        out = {
            "finished": d["finished"], "source": d["source"], "items": d["items"],
            "new_ids": d["new_ids"], "new_total": d["new_total"], "replaced": d.get("replaced", 0),
            "partial": d.get("partial", False), "first": d.get("first", False),
            "dismissed": d["dismissed"],
        }
    except KeyError:
        return None
    ok = (_num(out["finished"]) and out["finished"] > 0 and out["source"] in SOURCES
          and isinstance(out["items"], list) and len(out["items"]) <= MAX_ITEMS
          and all(isinstance(s, str) and len(s) <= MAX_ITEM_LEN for s in out["items"])
          and isinstance(out["new_ids"], list) and len(out["new_ids"]) <= MAX_IDS
          and all(_id_ok(x) for x in out["new_ids"])
          and type(out["new_total"]) is int and 0 <= out["new_total"] < 10 ** 7
          and type(out["replaced"]) is int and 0 <= out["replaced"] < 10 ** 7
          and all(type(out[k]) is bool for k in ("partial", "first", "dismissed")))
    return out if ok else None


class LastImport:
    def __init__(self, path):
        self.path, self.lock = path, threading.Lock()
        self.data = self._load()

    def _load(self):
        try:
            if os.path.getsize(self.path) > MAX_FILE:
                return None
            with open(self.path, encoding="utf-8") as f:
                return validate(json.load(f))
        except (OSError, ValueError):  # missing, unreadable, not UTF-8, not JSON
            return None

    def get(self):
        with self.lock:
            return json.loads(json.dumps(self.data)) if self.data else None

    def record(self, new_ids, items, source="upload", replaced=0, partial=False, first=False, remap=None):
        """After an import that changed the DB. new_ids: ids oldest first (only the
        last MAX_IDS are kept); first=True: the DB was empty, nothing to compare;
        remap {old: new}: sessions an export copy replaced (same session, new id).
        partial describes this import only: a later clean one clears it."""
        ids = [int(x) for x in new_ids if _id_ok(int(x))]
        total = len(ids)
        if first:
            ids = []
        items = [str(s)[:MAX_ITEM_LEN] for s in items][:MAX_ITEMS]
        remap = {int(k): int(v) for k, v in (remap or {}).items() if _id_ok(int(k)) and _id_ok(int(v))}
        with self.lock:
            old = self.data
            if old and not old["dismissed"]:  # not seen yet: add to it
                # a replaced session that is still unseen stays new, under its new id
                carried = [remap.get(x, x) for x in old["new_ids"]]
                followed = sum(1 for x in old["new_ids"] if x in remap)
                seen = set(ids)
                ids = [x for x in carried if x not in seen] + ids
                total += old["new_total"]
                items = list(dict.fromkeys(old["items"] + items))[:MAX_ITEMS]
                source = old["source"] if old["source"] == source else "mixed"
                replaced = max(0, old["replaced"] + replaced - followed)
                first = first or old["first"]
            new = {"finished": time.time(), "source": source if source in SOURCES else "upload",
                   "items": items, "new_ids": ids[-MAX_IDS:], "new_total": total, "replaced": replaced,
                   "partial": bool(partial), "first": bool(first), "dismissed": False}
            if old and new["finished"] <= old["finished"]:  # keep it strictly increasing (dismiss key)
                new["finished"] = old["finished"] + 0.001
            self.data = new
            self._write()
            return dict(new)

    def dismiss(self, finished):
        """True = dismissed; False = `finished` is not the current summary (a stale
        page: a newer import arrived since); None = nothing to dismiss."""
        with self.lock:
            if not self.data:
                return None
            if not _num(finished) or finished != self.data["finished"]:
                return False
            if not self.data["dismissed"]:
                self.data = {**self.data, "dismissed": True}
                self._write()
            return True

    def _write(self):
        """Never raises: losing the summary must not fail the import."""
        tmp = f"{self.path}.{os.getpid()}.{threading.get_ident()}.tmp"
        body = json.dumps(self.data, indent=1).encode()
        for attempt in (0, 1):
            try:
                fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                with os.fdopen(fd, "wb") as f:
                    f.write(body)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(tmp, self.path)
                return True
            except OSError:  # Windows: replace fails while another process reads it
                try:
                    os.remove(tmp)
                except OSError:
                    pass
                if attempt == 0:
                    time.sleep(0.05)
        return False
