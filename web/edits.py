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

An import can replace a session with a copy under another id (an uploaded
file, later the export's copy with Strava's id: `db build` dedup). The
dashboard importer hands the edit over (`remap`); for builds outside it
(CLI `db build`, `strava sync`) each edit keeps the session's `start`, and an
edit whose id is gone re-attaches to the one session starting within 120 s
that has no edit of its own. That happens in memory and is saved with the
next edit: a GET never writes.
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

import store

NAME_MAX = 200
MAX_FILE = 8 << 20
TYPE_MAX = 40
OVERRIDES = ("name", "type")
REATTACH_S = 120  # same window as the importer's / `db build`'s dedup
REATTACH_KM = 0.05  # and the same distance tolerance
# bookkeeping keys: an edit with only these is no edit. start / km identify the
# session if an import replaces it under another id (see _reconcile)
META = ("updated", "start", "km")
ID_RE = _KEY = re.compile(r"[0-9]{1,18}")  # activity ids; longer would overflow SQLite's int64
_VERSIONS = itertools.count(1)  # process-wide, so two stores never share a version


class RemoveFailed(OSError):
    """The delete was saved (session hidden) but its import file is still there."""


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
    if re.search(r"[\ud800-\udfff]", v):  # lone surrogates can't be written as UTF-8
        raise ValueError(f"{what} is not valid text")
    return v


def _finite(x):
    return type(x) in (int, float) and abs(x) < 1e9  # NaN / inf compare False; no float() of a huge int


def _clean_edit(e):
    """One edit as loaded from the file, checked like a request: unknown or bad
    fields are dropped (a list as a name would fail every query)."""
    out = {}
    for k, limit in (("name", NAME_MAX), ("type", TYPE_MAX)):
        try:
            v = _text(e.get(k), limit, k)
        except ValueError:
            continue
        if v:
            out[k] = v
    for k in ("hidden", "deleted", "file_removed"):
        if e.get(k) is True:
            out[k] = True
    for k in ("updated", "start"):
        v = e.get(k)
        if isinstance(v, str) and len(v) <= 40:
            try:
                _text(v, 40, k)
            except ValueError:
                continue
            out[k] = v
    if _finite(e.get("km")):
        out["km"] = e["km"]
    return out


def _clean_sessions(sess):
    """Edits as loaded from the file: digit keys, valid values, real edits only."""
    if not isinstance(sess, dict):
        return {}
    out = {}
    for k, v in sess.items():
        if isinstance(k, str) and _KEY.fullmatch(k) and isinstance(v, dict):
            e = _clean_edit(v)
            if set(e) - set(META):
                out[k] = e
    return out


def _km_close(a, b):
    return abs(a - b) <= REATTACH_KM * max(abs(a), abs(b))  # both _finite


class Edits:
    def __init__(self, home):
        self.home = home
        self.path = os.path.join(home, "edits.json") if home else None
        self.lock = threading.Lock()
        self.data = (store.load_object(self.path, MAX_FILE) if self.path else None) or {}
        self.data["sessions"] = _clean_sessions(self.data.get("sessions"))
        self.version = next(_VERSIONS)
        self._dbsig = None
        self._rlock = threading.Lock()  # one _reconcile at a time, start to finish
        self._dirty = False  # _reconcile moved edits in memory; saved by the next write

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
        self._reconcile(db)
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
                    for k, e in self.data["sessions"].items()]
        db.execute("DELETE FROM temp.rollbook_edits")
        db.executemany("INSERT INTO temp.rollbook_edits VALUES (?,?,?,?,?)", rows)
        db.execute("UPDATE temp.rollbook_edits_v SET v=?", (ver,))
        db.commit()

    def _reconcile(self, db):
        """After the DB changed: move edits of vanished ids to their replacement
        (see the module doc) and note start / km on edits made before they were
        kept. In memory only; the next save writes it.

        A replacement must start within REATTACH_S and, when both distances are
        known, be within REATTACH_KM, as `db build` dedup requires; an edit that
        hides a session also needs both distances. Exactly one session without
        an edit of its own may qualify, else the edit stays where it is."""
        try:
            sig = db.execute("SELECT count(*), total(id), max(id) FROM main.activities").fetchone()
        except sqlite3.Error:
            return
        if sig == self._dbsig:  # the usual case: no lock taken
            return
        with self._rlock:  # a slower thread must not apply an older snapshot over a newer one
            try:
                sig = db.execute("SELECT count(*), total(id), max(id) FROM main.activities").fetchone()
                if sig == self._dbsig:
                    return
                rows = db.execute("SELECT id, datetime, julianday(datetime), km FROM main.activities").fetchall()
                with self.lock:
                    starts = {e["start"] for e in self.data["sessions"].values() if isinstance(e.get("start"), str)}
                jstart = {st: db.execute("SELECT julianday(?)", (st,)).fetchone()[0] for st in starts}
            except sqlite3.Error:  # no table yet, or a DB without start times: nothing to match on
                return
            info = {r[0]: (r[1], r[2], r[3] if _finite(r[3]) else None) for r in rows}
            with self.lock:
                self._dbsig = sig
                sess = {k: dict(e) for k, e in self.data["sessions"].items()}
                changed = False
                for k, e in sess.items():
                    dt, _, km = info.get(int(k), (None, None, None))
                    if isinstance(dt, str) and "start" not in e:
                        e["start"] = dt
                        changed = True
                    if km is not None and "km" not in e:
                        e["km"] = km
                        changed = True
                taken = {int(k) for k in sess}
                for k in [k for k, e in sess.items() if int(k) not in info]:
                    e = sess[k]
                    if not isinstance(e.get("start"), str) or e.get("file_removed"):
                        continue
                    t = jstart.get(e["start"])
                    if t is None:
                        continue
                    ekm = e.get("km") if _finite(e.get("km")) else None
                    near = []
                    for i, (_, ti, km) in info.items():
                        if i in taken or ti is None or abs(ti - t) * 86400 >= REATTACH_S:
                            continue
                        if ekm is not None and km is not None:
                            if not _km_close(ekm, km):
                                continue
                        elif e.get("hidden"):  # hiding the wrong session would make it vanish
                            continue
                        near.append(i)
                    if len(near) == 1:
                        sess[str(near[0])] = sess.pop(k)
                        taken.add(near[0])
                        changed = True
                if changed:
                    self.data = {**self.data, "sessions": sess}
                    self.version = next(_VERSIONS)
                    self._dirty = True

    def remap(self, mapping):
        """{old id: new id} from an import that replaced sessions: edits follow.
        Also saves what _reconcile already moved in memory (a request during
        the build may have got there first)."""
        with self.lock:
            sess = dict(self.data["sessions"])
            moved = False
            for old, new in mapping.items():
                e = sess.get(str(old))
                if e is not None and str(new) not in sess and not e.get("file_removed"):
                    sess[str(new)] = sess.pop(str(old))
                    moved = True
            if moved or self._dirty:
                self._commit(sess)

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
        row = db.execute("SELECT name, type, filename, datetime, km FROM main.activities WHERE id=?",
                         (aid,)).fetchone()
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
                cur["file_removed"] = True
            for k in META:
                cur.pop(k, None)
            sess = dict(self.data["sessions"])
            if cur:
                cur["updated"] = time.strftime("%Y-%m-%d")
                if isinstance(row[3], str):
                    cur["start"] = row[3]
                if _finite(row[4]):
                    cur["km"] = row[4]
                sess[str(aid)] = cur
            else:
                sess.pop(str(aid), None)
            self._commit(sess)  # the edit is on disk before the file goes
            if remove:
                try:
                    self._remove_file(remove, row[2])
                except OSError as err:
                    if os.path.exists(remove):  # still there: don't claim it was removed
                        cur = {k: v for k, v in cur.items() if k != "file_removed"}
                        try:
                            self._commit({**sess, str(aid): cur})
                        except OSError as e2:
                            raise err from e2
                    raise RemoveFailed(f"hidden, but the file was not removed: {err.strerror or err}") from err
            return dict(cur), cur.get("type") != before

    def forget_removed(self, rel):
        """A previously deleted import file was uploaded again: show it again."""
        key = str(import_id(rel))
        with self.lock:
            cur = self.data["sessions"].get(key)
            if not cur or not cur.get("file_removed"):
                return
            sess = dict(self.data["sessions"])
            cur = {k: v for k, v in cur.items() if k not in ("hidden", "deleted", "file_removed")}
            if set(cur) - set(META):
                sess[key] = cur
            else:
                sess.pop(key)
            self._commit(sess)

    def import_file(self, aid, filename):
        """Path of the stored single-file import behind this activity, or None."""
        if not self.home or not filename or import_id(filename) != aid:
            return None
        root = os.path.realpath(os.path.join(self.home, "imports", "activities"))
        p = os.path.realpath(os.path.join(self.home, "imports", filename))
        return p if os.path.dirname(p) == root and os.path.isfile(p) else None

    def _remove_file(self, path, filename):
        """Its activities.csv row first, the file last: a failure part way leaves
        the file, which a later delete can still remove."""
        index = os.path.join(self.home, "imports", "activities.csv")
        try:
            with open(index, newline="", encoding="utf-8-sig") as f:
                rows = list(csv.reader(f))
        except OSError:
            rows = []
        if rows and "Filename" in rows[0]:
            i = rows[0].index("Filename")
            keep = [rows[0]] + [r for r in rows[1:] if not (len(r) > i and r[i].strip() == filename)]
            tmp = index + ".tmp"
            with open(tmp, "w", newline="", encoding="utf-8") as f:
                csv.writer(f).writerows(keep)
            os.replace(tmp, index)
        os.remove(path)

    def _commit(self, sessions):
        """Write the edits with these sessions, then make them current; on a
        failed write (OSError) nothing changes. Caller holds the lock."""
        data = {**self.data, "sessions": sessions}
        if self.path:
            store.write_json(self.path, data)
        self.data = data
        self._dirty = False
        self.version = next(_VERSIONS)
