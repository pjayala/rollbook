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

ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
EXPORTS = os.path.join(ROOT, "exports")
TOOL = os.path.join(ROOT, "bin", "rollbook")
ACT_EXT = (".fit", ".gpx", ".tcx", ".fit.gz", ".gpx.gz", ".tcx.gz")
MAX_BYTES = 4 * 1024 ** 3
CSV_HEAD = ["Activity ID", "Activity Date", "Activity Name", "Activity Type", "Filename"]


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
        dest = os.path.join(acts, name)
        if os.path.exists(dest):
            # stored before, but maybe not in the DB (e.g. after `db build --fresh`
            # of an export): still queue a build, which skips it if it's there
            with self.lock:
                if self.imports not in self.pending:
                    self.pending.append(self.imports)
            return 200, {"kind": "activity", "stored": name, "duplicate": True}
        os.replace(tmp, dest)
        index = os.path.join(self.imports, "activities.csv")
        new = not os.path.exists(index)
        title = re.sub(r"\.(fit|gpx|tcx)(\.gz)?$", "", name, flags=re.I).replace("_", " ")
        with open(index, "a", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            if new:
                w.writerow(CSV_HEAD)
            w.writerow(["", "", title, (sport or "").strip()[:40], f"activities/{name}"])
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

    def status(self):
        j = dict(self.job)
        if "log" in j:
            j["log"] = j["log"][-40:]
        return j

    def _count(self):
        import sqlite3
        try:
            db = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True)
            return db.execute("SELECT COUNT(*) FROM activities").fetchone()[0]
        except sqlite3.Error:
            return 0

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

    def _work(self, todo):
        j = self.job
        before = self._count()
        try:
            for path in todo:
                label = os.path.basename(path)
                if self._cmd(["db", "build", path], f"importing {label}"):
                    raise RuntimeError(next((x for x in reversed(j["log"]) if x.startswith("error")),
                                            f"db build failed for {label}"))
            j["added"] = self._count() - before
            if j["added"]:
                j["done"] = j["total"] = 0
                if self._cmd(["db", "enrich", "--missing"], "computing best efforts"):
                    raise RuntimeError("db enrich failed")
                j["phase"] = "computing dashboard metrics"
                self.M.warm()  # new activities only; cached ones are kept
            j.update(state="done", phase="done", finished=time.time())
        except Exception as e:
            j.update(state="error", phase="failed", error=str(e), added=self._count() - before)
