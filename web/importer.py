# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Pedro Ayala. Rollbook is free software, see LICENSE.
"""Import from the dashboard: upload files, then run `rollbook db build` +
`db enrich --missing` in a subprocess, with progress the page can poll.

Two kinds of upload:
  export    a Strava account export .zip (activities.csv at its root), or a
            zip of single activity files (handled like "activity"). Stored in exports/ under a stem that is new, because the
            extracted archive is reused by stem.
  activity  single .fit/.gpx/.tcx(.gz) files, e.g. Strava's per-activity
            "Export Original" (the file as the watch recorded it). Stored in data/imports/activities/
            with a row in data/imports/activities.csv carrying the sport the
            user picked, so the build gets a real type instead of the FIT
            sport name, and a name from the file name.
Both go through the normal incremental build, so anything already imported
(same activities/<file> path) is skipped.

The DB is written by the subprocess only; the server keeps its read-only
connections, which see the new rows once the build commits (WAL).
"""

import csv
import hashlib
import os
import re
import subprocess
import sys
import threading
import time
import zipfile

from strava_web import CSV_HEAD, EXIT_AUTH  # one index format for uploads and Strava sync

ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
EXPORTS = os.path.join(ROOT, "exports")
TOOL = os.path.join(ROOT, "bin", "rollbook")
ACT_EXT = (".fit", ".gpx", ".tcx", ".fit.gz", ".gpx.gz", ".tcx.gz")
MAX_BYTES = 4 * 1024 ** 3


def safe_name(name):
    name = os.path.basename(name.replace("\\", "/")).strip()
    name = re.sub(r"[^A-Za-z0-9._-]+", "_", name)
    return name.lstrip(".")[:120] or "upload"


def _sha1(path):
    h = hashlib.sha1()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


class Importer:
    def __init__(self, db_path, metrics):
        self.db_path, self.M = db_path, metrics
        self.home = os.path.dirname(os.path.abspath(db_path))
        self.imports = os.path.join(self.home, "imports")
        self.lock = threading.Lock()
        self.pending = []  # paths to build: export zips, and/or the imports dir
        self.job: dict = {"state": "idle"}
        self.on_store = None  # called with "activities/<name>" for each newly stored file
        self.offline = False  # `web --offline`: no Strava sync
        self.last = None  # lastimport.LastImport: what the last import added (new-records summary)

    # ---------------------------------------------------------------- upload

    def receive(self, rfile, length, name, sport):
        """Store one uploaded file; returns a dict for the client."""
        if self.job.get("state") == "running":
            return 409, {"error": "an import is already running"}
        if not length or length > MAX_BYTES:
            return 413, {"error": "empty or too large"}
        name = safe_name(name)
        low = name.lower()
        if low.endswith(".zip"):
            kind = "export"
        elif low.endswith(ACT_EXT):
            kind = "activity"
        else:
            return 415, {"error": f"{name}: expected a Strava export .zip or .fit/.gpx/.tcx(.gz)"}
        os.makedirs(EXPORTS, exist_ok=True)
        tmp = os.path.join(EXPORTS if kind == "export" else self.home, f".upload-{threading.get_ident()}.part")
        left = length
        with open(tmp, "wb") as f:
            while left:
                b = rfile.read(min(1 << 20, left))
                if not b:
                    break
                f.write(b)
                left -= len(b)
        if left:
            os.remove(tmp)
            return 400, {"error": "upload interrupted"}
        try:
            return self._store_export(tmp, name, sport) if kind == "export" else self._store_activity(tmp, name, sport)
        finally:
            if os.path.exists(tmp):
                os.remove(tmp)

    def _store_export(self, tmp, name, sport):
        if not zipfile.is_zipfile(tmp):
            return 415, {"error": f"{name} is not a zip file"}
        with zipfile.ZipFile(tmp) as z:
            names = z.namelist()
        if "activities.csv" not in names:
            # not an account export: accept a zip of single activity files
            # (e.g. several "Export Original" downloads zipped together)
            acts = [x for x in names if x.lower().endswith(ACT_EXT) and not x.endswith("/")
                    and not os.path.basename(x).startswith(".") and "__MACOSX" not in x]
            if not acts:
                return 415, {"error": f"{name}: neither a Strava account export (no activities.csv) "
                                      f"nor a zip of .fit/.gpx/.tcx files"}
            return self._store_zip_of_activities(tmp, acts, sport)
        n = sum(1 for x in names if x.startswith("activities/") and not x.endswith("/"))
        digest = _sha1(tmp)
        stem = os.path.splitext(name)[0]
        dest = os.path.join(EXPORTS, f"{stem}.zip")
        for k in range(1, 100):  # same content -> reuse; same name, other content -> new stem
            dest = os.path.join(EXPORTS, f"{stem}{'' if k == 1 else f'-{k}'}.zip")
            if not os.path.exists(dest):
                os.replace(tmp, dest)
                break
            if os.path.getsize(dest) == os.path.getsize(tmp) and _sha1(dest) == digest:
                break
        with self.lock:
            if dest not in self.pending:
                self.pending.append(dest)
        return 200, {"kind": "export", "stored": os.path.relpath(dest, ROOT), "files": n}

    def _store_zip_of_activities(self, tmp, members, sport):
        stored = dup = 0
        with zipfile.ZipFile(tmp) as z:
            for m in members:
                part = os.path.join(self.home, f".unzip-{threading.get_ident()}.part")
                with z.open(m) as src, open(part, "wb") as out:
                    while b := src.read(1 << 20):
                        out.write(b)
                _, r = self._store_activity(part, safe_name(m), sport)
                if os.path.exists(part):
                    os.remove(part)
                dup += bool(r.get("duplicate"))
                stored += not r.get("duplicate")
        return 200, {"kind": "activities", "files": stored, "duplicate": stored == 0, "already": dup}

    def _store_activity(self, tmp, name, sport):
        acts = os.path.join(self.imports, "activities")
        os.makedirs(acts, exist_ok=True)
        # same name, other content -> name-2.ext, -3…: HUAWEI "Export Original"
        # always names the file Outdoor_cycle.fit, so the name says nothing
        m = re.search(r"\.(fit|gpx|tcx)(\.gz)?$", name, flags=re.I)
        base, ext = (name[:m.start()], name[m.start():]) if m else os.path.splitext(name)
        digest = _sha1(tmp)
        for k in range(1, 1000):
            name = f"{base}{'' if k == 1 else f'-{k}'}{ext}"
            dest = os.path.join(acts, name)
            if not os.path.exists(dest):
                break
            if os.path.getsize(dest) == os.path.getsize(tmp) and _sha1(dest) == digest:
                # stored before, but maybe not in the DB (e.g. after `db build --fresh`
                # of an export): still queue a build, which skips it if it's there
                with self.lock:
                    if self.imports not in self.pending:
                        self.pending.append(self.imports)
                return 200, {"kind": "activity", "stored": name, "duplicate": True}
        else:
            return 409, {"error": f"{name}: too many different files with this name"}
        os.replace(tmp, dest)
        index = os.path.join(self.imports, "activities.csv")
        new = not os.path.exists(index)
        title = base.replace("_", " ")  # without the -N: both rides are "Outdoor cycle"
        with open(index, "a", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            if new:
                w.writerow(CSV_HEAD)
            w.writerow(["", "", title, (sport or "").strip()[:40], f"activities/{name}"])
        if self.on_store:  # e.g. un-delete a session whose file was removed earlier
            self.on_store(f"activities/{name}")
        with self.lock:
            if self.imports not in self.pending:
                self.pending.append(self.imports)
        return 200, {"kind": "activity", "stored": name}

    # ---------------------------------------------------------------- build

    def run(self):
        with self.lock:
            if self.job.get("state") == "running":
                return 409, self.status()
            if not self.pending:
                return 400, {"error": "nothing uploaded"}
            todo, self.pending = self.pending, []
            self.job = {"state": "running", "phase": "starting", "done": 0, "total": 0,
                        "log": [], "added": 0, "started": time.time(), "items": [os.path.basename(p) for p in todo]}
        threading.Thread(target=self._work, args=(todo,), daemon=True).start()
        return 200, self.status()

    def sync_strava(self):
        """`rollbook strava sync --no-build` in a subprocess (the cookie never
        enters this process), then the usual build of data/imports."""
        with self.lock:
            if self.job.get("state") == "running":
                return 409, self.status()
            self.job = {"state": "running", "phase": "starting", "done": 0, "total": 0, "log": [],
                        "added": 0, "started": time.time(), "items": ["Strava"], "source": "strava"}
        threading.Thread(target=self._work, args=([],), kwargs={"strava": True}, daemon=True).start()
        return 200, self.status()

    def status(self):
        j = dict(self.job)
        if "log" in j:
            j["log"] = j["log"][-40:]
        return j

    def _ids(self):
        """Activity ids in the DB. A set, not a count: an export replacing a bare
        import (dedup) swaps one id for another and leaves the count unchanged.
        Raw table, hidden sessions included: this is about what the build did.
        No DB yet = empty set; a DB that can't be read = None (unknown, not empty)."""
        import sqlite3
        if not os.path.exists(self.db_path):
            return set()
        try:
            db = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True)
            try:
                return {r[0] for r in db.execute("SELECT id FROM activities")}
            finally:
                db.close()
        except sqlite3.OperationalError as e:
            # a DB file with no activities table yet (an aborted first build) is empty;
            # anything else (locked, corrupt) is unknown
            return set() if "no such table" in str(e) else None
        except sqlite3.Error:
            return None

    def _starts(self):
        """{id: julian day of the start}. Taken before and after a build to tell a
        replacement (same session, new id, from an export) from a new session."""
        import sqlite3
        try:
            db = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True)
            try:
                return {r[0]: r[1] for r in db.execute("SELECT id, julianday(datetime) FROM activities")}
            finally:
                db.close()
        except sqlite3.Error:
            return {}

    def _record(self, before, starts0, after, partial=False):
        """Hand what this import added to the summary store; never raises.
        A new id whose start is within 2 min of a removed id's start is the
        same session from an export (`db build` dedup), so it isn't announced;
        if the removed one is in an unseen summary, the summary follows it."""
        if self.last is None or before is None or after is None or after == before:
            return
        try:
            starts = self._starts()
            gone = {i: starts0[i] for i in before - after if starts0.get(i) is not None}
            new, remap = [], {}
            for i in after - before:
                t = starts.get(i)
                old = next((g for g, tg in gone.items() if t is not None and abs(t - tg) * 86400 < 120), None)
                if old is not None:
                    remap[old] = i
                else:
                    new.append(i)
            new.sort(key=lambda i: (starts.get(i) or 0, i))  # oldest first: the store keeps the newest
            j = self.job
            if new or remap:
                self.last.record(new, j.get("items", []), j.get("source", "upload"), len(remap),
                                 partial=partial, first=not before, remap=remap)
            j["new_total"], j["replaced"] = len(new), len(remap)  # only once stored: the page then fetches it
        except Exception as e:  # the summary is a nicety: the import itself succeeded
            self.job.setdefault("log", []).append(f"new-records summary not saved: {e}")

    def _cmd(self, args, phase):
        j = self.job
        j["phase"] = phase
        env = dict(os.environ, ROLLBOOK_HOME=self.home, PYTHONUNBUFFERED="1")
        p = subprocess.Popen([sys.executable, TOOL, *args], env=env, stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT, text=True, errors="replace")
        for line in p.stdout:
            line = line.rstrip()
            if not line:
                continue
            j["log"].append(line)
            if len(j["log"]) > 200:
                del j["log"][:100]
            m = re.match(r"\s*(\d+)/(\d+)", line)
            if m:
                j["done"], j["total"] = int(m.group(1)), int(m.group(2))
            m = re.match(r"importing (\d+) activities", line)
            if m:
                j["done"], j["total"] = 0, int(m.group(1))
            if line.startswith("extracting"):
                j["phase"] = "extracting export"
        return p.wait()

    def _strava(self):
        """Download step of a Strava sync; returns how many files it stored."""
        j = self.job
        rc = self._cmd(["strava", "sync", "--no-build"], "checking Strava for new activities")
        if rc == EXIT_AUTH:
            j["auth"] = True
            raise RuntimeError("Strava login missing or expired: in a terminal, run ./rollbook strava auth")
        if rc:
            raise RuntimeError(next((x[len("error: "):] for x in reversed(j["log"]) if x.startswith("error")),
                                    "Strava sync failed"))
        m = next((re.match(r"downloaded (\d+)", x) for x in reversed(j["log"]) if x.startswith("downloaded")), None)
        j["fetched"] = int(m.group(1)) if m else 0
        return j["fetched"]

    def _work(self, todo, strava=False):
        j = self.job
        n = lambda s: len(s) if s is not None else 0  # noqa: E731  (None = DB unreadable)
        before = self._ids()
        starts0 = self._starts()
        try:
            if strava and self._strava():
                todo = [self.imports]
            for path in todo:
                label = os.path.basename(path)
                if self._cmd(["db", "build", path], f"importing {label}"):
                    raise RuntimeError(next((x for x in reversed(j["log"]) if x.startswith("error")),
                                            f"db build failed for {label}"))
            after = self._ids()
            j["added"] = max(0, n(after) - n(before))
            j["changed"] = after != before  # also true for a replacement (same count, new id)
            if after != before:  # new rows, or a replacement with a new id
                j["done"] = j["total"] = 0
                if self._cmd(["db", "enrich", "--missing"], "computing best efforts"):
                    raise RuntimeError("db enrich failed")
                j["phase"] = "computing dashboard metrics"
                self.M.warm()  # new activities only; cached ones are kept
            self._record(before, starts0, after)
            j.update(state="done", phase="done", finished=time.time())
        except Exception as e:
            after = self._ids()
            # rows an earlier step committed are in the DB: list them, but without
            # records (enrich / metrics may not have run)
            self._record(before, starts0, after, partial=True)
            j.update(state="error", phase="failed", error=str(e), added=max(0, n(after) - n(before)),
                     changed=after != before)
