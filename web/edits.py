# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Pedro Ayala. Rollbook is free software, see LICENSE.
"""Session edits from the dashboard: rename, change sport, hide, delete.

The server opens the DB read-only and `db build --fresh` recreates it, so edits
live beside it in $ROLLBOOK_HOME/edits.json, keyed by activity id. Ids are
stable across rebuilds: Strava's own id from activities.csv, or for imported
single files the SHA-1 of their path.

Every server connection gets the edits as a TEMP table plus two TEMP views:

  activities      shadows main.activities (temp objects win name lookup):
                  name/type overridden, hidden rows left out, so every existing
                  query (lists, sport counts, tracks, segments, HR max) sees them
  activities_all  the same with hidden rows, plus hidden / deleted /
                  orig_name / orig_type

"Delete" = hide, plus optionally removing the stored import file and its row
in imports/activities.csv. The DB row stays until the next fresh build.
"""

import csv
import hashlib
import itertools
import json
import os
import re
import sqlite3
import threading
import time

NAME_MAX = 200
TYPE_MAX = 40
OVERRIDES = ("name", "type")
_VERSIONS = itertools.count(1)  # process-wide, so two stores never share a version


def import_id(rel):
    """Activity id `db build` gives a file without a CSV id (see bin/rollbook)."""
    return int(hashlib.sha1(rel.encode()).hexdigest()[:12], 16) % (10 ** 12)


def _text(v, limit, what):
    if v is None:
        return ""
    if not isinstance(v, str):
        raise ValueError(f"{what} must be text")
    v = " ".join(v.split())
    if len(v) > limit:
        raise ValueError(f"{what} is longer than {limit} characters")
    if re.search(r"[\x00-\x1f\x7f]", v):
        raise ValueError(f"{what} has control characters")
    return v


class Edits:
    def __init__(self, home):
        self.home = home
        self.path = os.path.join(home, "edits.json") if home else None
        self.lock = threading.Lock()
        self.data = {}
        if self.path:
            try:
                with open(self.path, encoding="utf-8") as f:
                    self.data = json.load(f)
            except (OSError, ValueError):
                self.data = {}
        if not isinstance(self.data.get("sessions"), dict):
            self.data["sessions"] = {}
        self.version = next(_VERSIONS)

    # ------------------------------------------------------------ read

    def get(self, aid):
        with self.lock:
            return dict(self.data["sessions"].get(str(aid), {}))

    def all(self):
        with self.lock:
            return json.loads(json.dumps(self.data))

    # ------------------------------------------------------------ SQL overlay

    def sync(self, db):
        """Install / refresh the TEMP overlay on one connection (cheap when current)."""
        try:
            v, ncols = db.execute("SELECT v, ncols FROM temp.rollbook_edits_v").fetchone()
        except sqlite3.OperationalError:
            if not self._create(db):
                return
            v, ncols = 0, None
        if ncols is not None and ncols != self._ncols(db):
            # `db build` migrations / `db enrich` added columns since the views were
            # made (they list columns explicitly): rebuild them
            for kind, name in (("VIEW", "activities"), ("VIEW", "activities_all"),
                               ("TABLE", "rollbook_edits"), ("TABLE", "rollbook_edits_v")):
                db.execute(f"DROP {kind} IF EXISTS temp.{name}")
            if not self._create(db):
                return
            v = 0
        if v == self.version:
            return
        with self.lock:
            ver = self.version
            rows = [(int(k), e.get("name"), e.get("type"), 1 if e.get("hidden") else None,
                     1 if e.get("deleted") else None)
                    for k, e in self.data["sessions"].items() if k.isdigit()]
        db.execute("DELETE FROM temp.rollbook_edits")
        db.executemany("INSERT INTO temp.rollbook_edits VALUES (?,?,?,?,?)", rows)
        db.execute("UPDATE temp.rollbook_edits_v SET v=?", (ver,))
        db.commit()

    @staticmethod
    def _ncols(db):
        return len(db.execute("PRAGMA main.table_info(activities)").fetchall())

    @staticmethod
    def _create(db):
        cols = [r[1] for r in db.execute("PRAGMA main.table_info(activities)")]
        if not cols:
            return False
        db.execute("CREATE TEMP TABLE rollbook_edits (id INTEGER PRIMARY KEY, name TEXT, type TEXT, "
                   "hidden INTEGER, deleted INTEGER)")
        db.execute("CREATE TEMP TABLE rollbook_edits_v (v INTEGER, ncols INTEGER)")
        db.execute("INSERT INTO temp.rollbook_edits_v VALUES (0, ?)", (len(cols),))
        sel = ", ".join(f'COALESCE(e."{c}", a."{c}") AS "{c}"' if c in OVERRIDES else f'a."{c}"'
                        for c in cols)
        src = "FROM main.activities a LEFT JOIN temp.rollbook_edits e ON e.id = a.id"
        db.execute(f"CREATE TEMP VIEW activities AS SELECT {sel} {src} WHERE e.hidden IS NULL")
        db.execute(f"CREATE TEMP VIEW activities_all AS SELECT {sel}, "
                   f"COALESCE(e.hidden, 0) AS hidden, COALESCE(e.deleted, 0) AS deleted, "
                   f"a.name AS orig_name, a.type AS orig_type {src}")
        db.commit()
        return True

    # ------------------------------------------------------------ write

    def update(self, db, aid, body):
        """Apply one edit request; returns (edit, type_changed).

        body keys, all optional: name, type ("" or the original = revert),
        hidden (bool), delete (bool) with remove_file (bool), restore (bool).
        Raises KeyError for an unknown activity, ValueError for a bad request.
        """
        if not isinstance(body, dict):
            raise ValueError("expected a JSON object")
        row = db.execute("SELECT name, type, filename FROM main.activities WHERE id=?", (aid,)).fetchone()
        if row is None:
            raise KeyError(aid)
        orig = {"name": row[0], "type": row[1]}
        new = {}
        for k, limit in (("name", NAME_MAX), ("type", TYPE_MAX)):
            if k in body:
                new[k] = _text(body[k], limit, k)
        remove = None
        if body.get("delete") and body.get("remove_file"):
            remove = self.import_file(aid, row[2])
            if remove is None:
                raise ValueError("not an imported file, nothing to remove")
        with self.lock:
            cur = dict(self.data["sessions"].get(str(aid), {}))
            before = cur.get("type")
            for k, v in new.items():
                if v and v != orig[k]:
                    cur[k] = v
                else:
                    cur.pop(k, None)
            if "hidden" in body:
                if body["hidden"]:
                    cur["hidden"] = True
                else:
                    for k in ("hidden", "deleted"):
                        cur.pop(k, None)
            if body.get("restore"):
                for k in ("hidden", "deleted"):
                    cur.pop(k, None)
            if body.get("delete"):
                cur["hidden"] = cur["deleted"] = True
            if remove:
                self._remove_file(remove, row[2])
                cur["file_removed"] = True
            cur.pop("updated", None)
            if cur:
                cur["updated"] = time.strftime("%Y-%m-%d")
                self.data["sessions"][str(aid)] = cur
            else:
                self.data["sessions"].pop(str(aid), None)
            self._save()
            return dict(cur), cur.get("type") != before

    def forget_removed(self, rel):
        """A previously deleted import file was uploaded again: show it again."""
        key = str(import_id(rel))
        with self.lock:
            cur = self.data["sessions"].get(key)
            if not cur or not cur.get("file_removed"):
                return
            for k in ("hidden", "deleted", "file_removed"):
                cur.pop(k, None)
            if not set(cur) - {"updated"}:
                self.data["sessions"].pop(key)
            self._save()

    def import_file(self, aid, filename):
        """Path of the stored single-file import behind this activity, or None."""
        if not self.home or not filename or import_id(filename) != aid:
            return None
        root = os.path.realpath(os.path.join(self.home, "imports", "activities"))
        p = os.path.realpath(os.path.join(self.home, "imports", filename))
        return p if os.path.dirname(p) == root and os.path.isfile(p) else None

    def _remove_file(self, path, filename):
        os.remove(path)
        index = os.path.join(self.home, "imports", "activities.csv")
        try:
            with open(index, newline="", encoding="utf-8-sig") as f:
                rows = list(csv.reader(f))
        except OSError:
            return
        if not rows or "Filename" not in rows[0]:
            return
        i = rows[0].index("Filename")
        keep = [rows[0]] + [r for r in rows[1:] if not (len(r) > i and r[i].strip() == filename)]
        tmp = index + ".tmp"
        with open(tmp, "w", newline="", encoding="utf-8") as f:
            csv.writer(f).writerows(keep)
        os.replace(tmp, index)

    def _save(self):
        """Caller holds the lock."""
        self.version = next(_VERSIONS)
        if not self.path:
            return
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.data, f, indent=1, ensure_ascii=False)
        os.replace(tmp, self.path)
