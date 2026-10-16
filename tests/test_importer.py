# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Pedro Ayala. Rollbook is free software, see LICENSE.
"""Import: web/importer.py storage + build job, `db build` dedup, /api/import over HTTP."""

import csv
import hashlib
import io
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
import zipfile
from datetime import datetime, timedelta, timezone
from unittest import mock

from helpers import TOOL, WEB, line_points, tmpdir, write_fit

import edits
import importer
import server

T0 = datetime(2026, 5, 1, 8, 0, tzinfo=timezone.utc)


def run(home, *args):
    env = dict(os.environ, ROLLBOOK_HOME=home, PYTHONIOENCODING="utf-8")
    return subprocess.run([sys.executable, TOOL, *args], env=env, capture_output=True, text=True, timeout=120)


def zip_bytes(files):
    b = io.BytesIO()
    with zipfile.ZipFile(b, "w") as z:
        for name, data in files.items():
            z.writestr(name, data)
    return b.getvalue()


def export_zip(aid):
    """A minimal account export; a different aid gives different bytes."""
    return zip_bytes({"activities.csv": f"Activity ID,Filename\n{aid},activities/{aid}.gpx\n",
                      f"activities/{aid}.gpx": b"<gpx/>"})


def imports_dir(home, files, sport="Inline Skate"):
    """data/imports as the importer writes it. files: {name: (t0, speed_ms, n)}."""
    imp = os.path.join(home, "imports")
    os.makedirs(os.path.join(imp, "activities"), exist_ok=True)
    with open(os.path.join(imp, "activities.csv"), "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(importer.CSV_HEAD)
        for name, (t0, speed, n) in sorted(files.items()):
            write_fit(os.path.join(imp, "activities", name), line_points(n=n, t0=t0, speed_ms=speed))
            w.writerow(["", "", name, sport, f"activities/{name}"])
    return imp


def rows(db_path, sql, args=()):
    db = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        return db.execute(sql, args).fetchall()
    finally:
        db.close()  # Windows can't delete an open DB file


class StoreBase(unittest.TestCase):
    """An Importer on a temp home; ROOT/EXPORTS patched so the repo's exports/ is never touched."""

    def setUp(self):
        self.root = tmpdir(self)
        self.home = os.path.join(self.root, "data")
        os.makedirs(self.home)
        self.exports = os.path.join(self.root, "exports")
        self.imports = os.path.join(self.home, "imports")
        self.acts = os.path.join(self.imports, "activities")
        for attr, val in (("ROOT", self.root), ("EXPORTS", self.exports)):
            p = mock.patch.object(importer, attr, val)
            p.start()
            self.addCleanup(p.stop)
        self.imp = importer.Importer(os.path.join(self.home, "rollbook.db"), mock.Mock())

    def up(self, name, data, sport=None):
        return self.imp.receive(io.BytesIO(data), len(data), name, sport)

    def index(self):
        with open(os.path.join(self.imports, "activities.csv"), newline="", encoding="utf-8") as f:
            return list(csv.reader(f))

    @staticmethod
    def read(path):
        with open(path, "rb") as f:
            return f.read()

    def leftovers(self):
        return [n for d in (self.home, self.exports) if os.path.isdir(d)
                for n in os.listdir(d) if n.endswith(".part")]


class Store(StoreBase):
    def test_export_zip_stored_in_exports(self):
        data = export_zip(1)
        code, r = self.up("export_42.zip", data)
        self.assertEqual(code, 200, r)
        self.assertEqual(r, {"kind": "export", "stored": os.path.join("exports", "export_42.zip"), "files": 1})
        self.assertEqual(self.read(os.path.join(self.exports, "export_42.zip")), data)
        self.assertEqual(self.imp.pending, [os.path.join(self.exports, "export_42.zip")])
        self.assertEqual(self.leftovers(), [])

    def test_same_name_other_content_gets_numbered_stem(self):
        zips = [export_zip(i) for i in (1, 2, 3)]
        stored = [self.up("export_42.zip", z)[1]["stored"] for z in zips]
        self.assertEqual([os.path.basename(s) for s in stored],
                         ["export_42.zip", "export_42-2.zip", "export_42-3.zip"])
        for z, s in zip(zips, stored):
            self.assertEqual(self.read(os.path.join(self.root, s)), z)
        self.assertEqual(len(self.imp.pending), 3)

    def test_identical_reupload_reuses_stored_file(self):
        # current behaviour: same bytes -> the stem already holding them, no new -N, queued once
        a, b = export_zip(1), export_zip(2)
        self.up("e.zip", a)
        self.up("e.zip", b)
        self.assertEqual(self.up("e.zip", b)[1]["stored"], os.path.join("exports", "e-2.zip"))
        self.assertEqual(self.up("e.zip", a)[1]["stored"], os.path.join("exports", "e.zip"))
        self.assertEqual(sorted(os.listdir(self.exports)), ["e-2.zip", "e.zip"])
        self.assertEqual(len(self.imp.pending), 2)

    @unittest.expectedFailure  # BUG: stem is unique in exports/ only; cache/archives/<stem> is reused as-is
    def test_new_export_avoids_stem_already_extracted(self):
        stale = os.path.join(self.home, "cache", "archives", "e")
        os.makedirs(stale)
        open(os.path.join(stale, ".extracted"), "w").close()
        self.assertNotEqual(self.up("e.zip", export_zip(1))[1]["stored"], os.path.join("exports", "e.zip"))

    def test_upload_names_are_sanitised(self):
        self.assertEqual(self.up("..\\../a b/export x.zip", export_zip(1))[1]["stored"],
                         os.path.join("exports", "export_x.zip"))
        self.assertEqual(self.up("../../evil/Ride 1.gpx", b"x")[1]["stored"], "Ride_1.gpx")
        self.assertEqual(self.up(".hidden.gpx", b"y")[1]["stored"], "hidden.gpx")
        self.assertEqual(sorted(os.listdir(self.acts)), ["Ride_1.gpx", "hidden.gpx"])

    def test_bad_exports_rejected(self):
        self.assertEqual(self.up("e.zip", b"not a zip")[0], 415)
        self.assertEqual(self.up("e.zip", zip_bytes({"readme.txt": "x"}))[0], 415)
        self.assertEqual(self.up("notes.txt", b"x")[0], 415)
        self.assertEqual(os.listdir(self.exports), [])
        self.assertEqual(self.imp.pending, [])

    def test_zip_of_activity_files(self):
        z = zip_bytes({"rides/Ride_1.gpx": b"<gpx/>", "b.tcx.gz": b"tcx", "__MACOSX/rides/._Ride_1.gpx": b"x",
                       ".hidden.fit": b"x", "notes.txt": b"x"})
        self.assertEqual(self.up("bundle.zip", z, "Ride"),
                         (200, {"kind": "activities", "files": 2, "duplicate": False, "already": 0}))
        self.assertEqual(sorted(os.listdir(self.acts)), ["Ride_1.gpx", "b.tcx.gz"])
        self.assertEqual([r[3] for r in self.index()[1:]], ["Ride", "Ride"])
        self.assertEqual(self.up("bundle.zip", z, "Ride")[1],
                         {"kind": "activities", "files": 0, "duplicate": True, "already": 2})
        self.assertFalse(os.path.exists(os.path.join(self.exports, "bundle.zip")))
        self.assertEqual(self.leftovers(), [])

    def test_single_files_stored_with_index_row(self):
        names = [f"Evening_skate_{i}{ext}" for i, ext in enumerate(importer.ACT_EXT)]
        for i, n in enumerate(names):
            self.assertEqual(self.up(n, b"data%d" % i, "Inline Skate"), (200, {"kind": "activity", "stored": n}))
            self.assertEqual(self.read(os.path.join(self.acts, n)), b"data%d" % i)
        self.assertEqual(self.index(), [importer.CSV_HEAD] + [
            ["", "", f"Evening skate {i}", "Inline Skate", f"activities/{n}"] for i, n in enumerate(names)])
        self.assertEqual(self.imp.pending, [self.imports])
        self.assertEqual(self.leftovers(), [])

    def test_sport_and_title_normalised(self):
        self.up("Lunch_Run.GPX", b"x", "  Run  ")
        self.up("Spin.FIT.GZ", b"y", None)
        self.up("Long.tcx", b"z", "S" * 50)
        self.assertEqual([r[2:4] for r in self.index()[1:]],
                         [["Lunch Run", "Run"], ["Spin", ""], ["Long", "S" * 40]])

    def test_same_file_again_is_duplicate_but_requeued(self):
        self.imp.on_store = mock.Mock()
        self.up("Ride.fit", b"one", "Ride")
        self.imp.on_store.assert_called_once_with("activities/Ride.fit")
        self.imp.pending = []
        self.assertEqual(self.up("Ride.fit", b"one", "Ride"),
                         (200, {"kind": "activity", "stored": "Ride.fit", "duplicate": True}))
        self.assertEqual(len(self.index()), 2)
        self.assertEqual(self.imp.pending, [self.imports])
        self.imp.on_store.assert_called_once()

    def test_same_name_other_content_is_kept(self):
        # HUAWEI "Export Original" is always Outdoor_cycle.fit: the 2nd ride must not be dropped
        self.up("Outdoor_cycle.fit", b"ride one", "Ride")
        code, r = self.up("Outdoor_cycle.fit", b"ride two", "Ride")
        self.assertEqual((code, r), (200, {"kind": "activity", "stored": "Outdoor_cycle-2.fit"}))
        self.assertEqual(self.up("Outdoor_cycle.fit.gz", b"ride three")[1]["stored"], "Outdoor_cycle.fit.gz")
        self.assertEqual(self.up("Outdoor_cycle.fit.gz", b"ride four")[1]["stored"], "Outdoor_cycle-2.fit.gz")
        self.assertEqual(sorted(self.read(os.path.join(self.acts, n)) for n in os.listdir(self.acts)),
                         [b"ride four", b"ride one", b"ride three", b"ride two"])
        # both CSV rows keep the plain title, each points at its own file
        self.assertEqual([r[2:] for r in self.index()[1:3]],
                         [["Outdoor cycle", "Ride", "activities/Outdoor_cycle.fit"],
                          ["Outdoor cycle", "Ride", "activities/Outdoor_cycle-2.fit"]])

    def test_same_content_under_numbered_name_is_duplicate(self):
        self.up("Outdoor_cycle.fit", b"ride one")
        self.up("Outdoor_cycle.fit", b"ride two")
        for data, stored in ((b"ride two", "Outdoor_cycle-2.fit"), (b"ride one", "Outdoor_cycle.fit")):
            self.assertEqual(self.up("Outdoor_cycle.fit", data),
                             (200, {"kind": "activity", "stored": stored, "duplicate": True}))
        self.assertEqual(len(os.listdir(self.acts)), 2)
        self.assertEqual(len(self.index()), 3)

    def test_zip_with_same_name_other_content(self):
        self.up("Outdoor_cycle.fit", b"ride one")
        r = self.up("bundle.zip", zip_bytes({"a/Outdoor_cycle.fit": b"ride two", "b/Outdoor_cycle.fit": b"ride one"}))[1]
        self.assertEqual((r["files"], r["already"]), (1, 1))
        self.assertEqual(sorted(os.listdir(self.acts)), ["Outdoor_cycle-2.fit", "Outdoor_cycle.fit"])

    def test_rejections_leave_nothing_behind(self):
        self.assertEqual(self.imp.receive(io.BytesIO(b""), 0, "a.gpx", None)[0], 413)
        self.assertEqual(self.imp.receive(io.BytesIO(b"x"), importer.MAX_BYTES + 1, "a.gpx", None)[0], 413)
        self.assertEqual(self.imp.receive(io.BytesIO(b"abc"), 10, "a.gpx", None)[0], 400)
        self.assertEqual(self.imp.receive(io.BytesIO(b"abc"), 10, "e.zip", None)[0], 400)
        self.imp.job = {"state": "running"}
        self.assertEqual(self.up("b.gpx", b"x")[0], 409)
        self.assertEqual(self.leftovers(), [])
        self.assertFalse(os.path.exists(self.acts))
        self.assertEqual(self.imp.pending, [])


class Job(StoreBase):
    """run() / status() / _work() with the subprocess mocked: deterministic, no real build."""

    def wait(self, timeout=10):
        end = time.monotonic() + timeout
        while self.imp.status()["state"] == "running" and time.monotonic() < end:
            time.sleep(0.01)
        return self.imp.status()

    def build(self, ids, rc=0, log=None, gate=None):
        """Upload one file, run with _cmd/_ids mocked (ids: successive DB id sets);
        returns (calls, final status).
        Always gated: run() returns the live job dict, so an ungated job can finish before the assert."""
        calls = []
        gate = gate or threading.Event()

        def cmd(args, phase):
            calls.append(list(args))
            self.imp.job["phase"] = phase
            if log:
                self.imp.job["log"].append(log)
            if gate:
                gate.wait(10)
            return rc

        self.up("a.gpx", b"x", "Ride")
        with mock.patch.object(self.imp, "_cmd", side_effect=cmd), \
                mock.patch.object(self.imp, "_ids", side_effect=ids):
            code, st = self.imp.run()
            self.assertEqual((code, st["state"], st["items"]), (200, "running", ["imports"]))
            if gate:
                self.assertEqual(self.imp.run()[0], 409)
                self.assertEqual(self.up("b.gpx", b"y")[0], 409)
                gate.set()
            return calls, self.wait()

    def test_run_without_uploads(self):
        self.assertEqual(self.imp.run()[0], 400)

    def test_running_then_done(self):
        calls, st = self.build([{1}, {1, 2}])
        self.assertEqual((st["state"], st["phase"], st["added"]), ("done", "done", 1))
        self.assertIn("finished", st)
        self.assertEqual(calls, [["db", "build", self.imports], ["db", "enrich", "--missing"]])
        self.imp.M.warm.assert_called_once_with()
        self.assertEqual(self.imp.pending, [])
        self.assertEqual(self.imp.run()[0], 400)

    def test_nothing_new_skips_enrich(self):
        calls, st = self.build([{1, 2}, {1, 2}])
        self.assertEqual((st["state"], st["added"]), ("done", 0))
        self.assertEqual(calls, [["db", "build", self.imports]])
        self.imp.M.warm.assert_not_called()

    def test_replacement_still_enriched(self):
        # dedup: an export row replaces a bare import -> same count, new id, needs best efforts
        calls, st = self.build([{1, 2}, {1, 5001}])
        self.assertEqual((st["state"], st["added"]), ("done", 0))
        self.assertIn(["db", "enrich", "--missing"], calls)
        self.imp.M.warm.assert_called_once_with()

    def test_ids_reads_real_db(self):
        self.assertEqual(self.imp._ids(), set())  # no DB yet
        db = sqlite3.connect(self.imp.db_path)
        db.execute("CREATE TABLE activities (id INTEGER PRIMARY KEY)")
        db.executemany("INSERT INTO activities VALUES (?)", [(3,), (7,)])
        db.commit()
        db.close()
        self.assertEqual(self.imp._ids(), {3, 7})

    def test_build_failure_reports_error_line(self):
        calls, st = self.build([{1}, {1}], rc=1, log="error: boom")
        self.assertEqual((st["state"], st["phase"], st["error"], st["added"]), ("error", "failed", "error: boom", 0))
        self.assertEqual(len(calls), 1)
        self.imp.M.warm.assert_not_called()

    def test_cmd_streams_progress(self):
        p = mock.Mock()
        p.stdout = iter(["extracting e.zip -> x\n", "importing 3 activities (0 already present)\n", "\n",
                         "  2/3  ok=2 failed=0 duplicates=0\n"])
        p.wait.return_value = 0
        self.imp.job = {"log": []}
        with mock.patch.object(importer.subprocess, "Popen", return_value=p) as popen:
            self.assertEqual(self.imp._cmd(["db", "build", "x"], "importing x"), 0)
        args, kw = popen.call_args
        self.assertEqual(args[0], [sys.executable, importer.TOOL, "db", "build", "x"])
        self.assertEqual(kw["env"]["ROLLBOOK_HOME"], self.home)
        j = self.imp.job
        self.assertEqual((j["phase"], j["done"], j["total"], len(j["log"])), ("extracting export", 2, 3, 3))

    def test_status_log_trimmed(self):
        self.imp.job = {"state": "running", "log": [str(i) for i in range(100)]}
        st = self.imp.status()
        self.assertEqual((len(st["log"]), st["log"][-1], len(self.imp.job["log"])), (40, "99", 100))


class StableId(unittest.TestCase):
    """Id of a CSV-less file: SHA-1 of its path, not Python's salted hash()."""
    REL = "activities/Outdoor_cycle.fit"

    def test_is_sha1_of_path(self):
        want = int(hashlib.sha1(self.REL.encode()).hexdigest()[:12], 16) % 10 ** 12
        self.assertEqual(edits.import_id(self.REL), want)

    def test_same_across_processes_with_other_hash_seeds(self):
        code = (f"import sys; sys.path.insert(0, {WEB!r}); import edits; "
                f"print(edits.import_id({self.REL!r}), hash({self.REL!r}))")
        outs = []
        for seed in ("1", "2"):
            r = subprocess.run([sys.executable, "-c", code], env=dict(os.environ, PYTHONHASHSEED=seed),
                               capture_output=True, text=True, timeout=60)
            self.assertEqual(r.returncode, 0, r.stderr)
            outs.append(r.stdout.split())
        self.assertNotEqual(outs[0][1], outs[1][1])  # the seeds really changed hash()
        self.assertEqual(outs[0][0], outs[1][0])
        self.assertEqual(outs[0][0], str(edits.import_id(self.REL)))


class Dedup(unittest.TestCase):
    """`db build`: same session from another file = start < 2 min apart and km within 5%."""

    def make_export(self, home, t0, distance=None):
        root = os.path.join(home, "export")
        os.makedirs(os.path.join(root, "activities"))
        write_fit(os.path.join(root, "activities", "5001.fit"), line_points(n=600, t0=t0))
        head = ["Activity ID", "Activity Date", "Activity Name", "Activity Type", "Filename"]
        row = ["5001", "", "Evening skate", "Inline Skate", "activities/5001.fit"]
        if distance:
            head, row = head + ["Distance"], row + [distance]
        with open(os.path.join(root, "activities.csv"), "w", newline="", encoding="utf-8") as f:
            csv.writer(f).writerows([head, row])
        return root

    def test_window_and_distance_tolerance(self):
        home, s, day = tmpdir(self), timedelta(seconds=1), timedelta(days=1)
        imp = imports_dir(home, {
            "a1.fit": (T0, 6.0, 600),
            "a2.fit": (T0 + 110 * s, 6.0, 600),         # < 2 min, same km -> skipped
            "a3.fit": (T0 + 130 * s, 6.0, 600),         # > 2 min -> kept
            "b1.fit": (T0 + day, 6.0, 600),
            "b2.fit": (T0 + day + 30 * s, 6.18, 600),   # +3 % km -> skipped
            "b3.fit": (T0 + day + 30 * s, 6.6, 600),    # +10 % km -> kept
        })
        r = run(home, "db", "build", "--no-samples", imp)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("activities/a2.fit: skipped, same session as activities/a1.fit", r.stderr)
        self.assertIn("activities/b2.fit: skipped, same session as activities/b1.fit", r.stderr)
        got = rows(os.path.join(home, "rollbook.db"), "SELECT id, filename FROM activities ORDER BY filename")
        self.assertEqual(got, [(edits.import_id(f"activities/{n}"), f"activities/{n}")
                               for n in ("a1.fit", "a3.fit", "b1.fit", "b3.fit")])

    def test_export_metadata_replaces_bare_import(self):
        home = tmpdir(self)
        imp = imports_dir(home, {"skate.fit": (T0, 6.0, 600)})
        self.assertEqual(run(home, "db", "build", imp).returncode, 0)
        old, db = edits.import_id("activities/skate.fit"), os.path.join(home, "rollbook.db")
        r = run(home, "db", "build", self.make_export(home, T0 + timedelta(seconds=5), distance="3594.0"))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("activities/5001.fit: replaces activities/skate.fit", r.stderr)
        self.assertEqual(rows(db, "SELECT id, s_distance_m FROM activities"), [(5001, 3594.0)])
        for tbl in ("samples", "splits"):
            self.assertEqual(rows(db, f"SELECT COUNT(*) FROM {tbl} WHERE activity_id=?", (old,)), [(0,)])
        self.assertGreater(rows(db, "SELECT COUNT(*) FROM samples WHERE activity_id=5001")[0][0], 0)
        r = run(home, "db", "build", imp)  # the stored import must not come back
        self.assertIn("activities/skate.fit: skipped, same session as activities/5001.fit", r.stderr)
        self.assertEqual(rows(db, "SELECT id FROM activities"), [(5001,)])

    def test_export_without_distance_does_not_replace(self):
        # current behaviour: replacing needs a Distance value, not just a CSV row (see report)
        home = tmpdir(self)
        imp = imports_dir(home, {"skate.fit": (T0, 6.0, 600)})
        self.assertEqual(run(home, "db", "build", "--no-samples", imp).returncode, 0)
        r = run(home, "db", "build", "--no-samples", self.make_export(home, T0 + timedelta(seconds=5)))
        self.assertIn("activities/5001.fit: skipped, same session as activities/skate.fit", r.stderr)
        self.assertEqual(rows(os.path.join(home, "rollbook.db"), "SELECT id FROM activities"),
                         [(edits.import_id("activities/skate.fit"),)])


class OverlayColumns(unittest.TestCase):
    """The edits overlay views list columns explicitly: a column added later on the same
    connection (by `db enrich` / a `db build` migration) must show up, not stay hidden."""

    def test_overlay_picks_up_added_column(self):
        path = os.path.join(tmpdir(self), "rollbook.db")
        rw = sqlite3.connect(path)
        rw.execute("CREATE TABLE activities (id INTEGER PRIMARY KEY, name TEXT, type TEXT)")
        rw.execute("INSERT INTO activities VALUES (1, 'a', 'Ride')")
        rw.commit()
        ro = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        self.addCleanup(ro.close)
        self.addCleanup(rw.close)
        ed = edits.Edits(None)
        ed.sync(ro)
        self.assertEqual(ro.execute("SELECT name FROM activities").fetchall(), [("a",)])
        rw.execute("ALTER TABLE activities ADD COLUMN best10min_kmh REAL")
        rw.execute("UPDATE activities SET best10min_kmh = 21.5")
        rw.commit()
        ed.sync(ro)
        self.assertEqual(ro.execute("SELECT best10min_kmh FROM activities").fetchall(), [(21.5,)])
        self.assertEqual(ro.execute("SELECT best10min_kmh, hidden FROM activities_all").fetchall(), [(21.5, 0)])


class ImportApi(unittest.TestCase):
    """The dashboard flow over HTTP: upload, run, poll GET /api/import, reload the list."""

    @classmethod
    def setUpClass(cls):
        cls.root = tempfile.mkdtemp(prefix="rollbook-test-import-")
        cls.home = os.path.join(cls.root, "data")
        r = run(cls.home, "db", "build", imports_dir(cls.home, {"first.fit": (T0, 6.0, 600)}))
        assert r.returncode == 0, r.stdout + r.stderr
        # deliberately no `db enrich`: the dashboard must cope with a DB lacking best<N>min_kmh,
        # then show them once the import's `enrich --missing` adds the columns
        cls.patches = [mock.patch.object(importer, "ROOT", cls.root),
                       mock.patch.object(importer, "EXPORTS", os.path.join(cls.root, "exports"))]
        for p in cls.patches:
            p.start()
        cls.db = os.path.join(cls.home, "rollbook.db")
        cls.httpd, cls.M, cls.url = server.start(cls.db, os.path.join(cls.home, "cache", "web_metrics.json"),
                                                 0, offline=True)
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()
        cls.port = cls.httpd.server_address[1]

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        for p in cls.patches:
            p.stop()
        shutil.rmtree(cls.root, True)

    def get(self, path):
        with urllib.request.urlopen(self.url.rstrip("/") + path, timeout=60) as r:
            return json.loads(r.read())

    def post(self, path, body=b"", headers=None):
        req = urllib.request.Request(self.url.rstrip("/") + path, data=body, method="POST", headers=headers or {})
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()

    def poll(self, timeout=60):
        end = time.monotonic() + timeout
        st = self.get("/api/import")
        while st["state"] == "running" and time.monotonic() < end:
            time.sleep(0.05)
            st = self.get("/api/import")
        return st

    def test_import_posts_need_header_same_origin_local_host(self):
        ok, port = {"X-Rollbook": "1"}, self.port
        bad = [{}, {"X-Rollbook": "0"},
               {**ok, "Origin": "https://evil.example"}, {**ok, "Origin": "null"},
               {**ok, "Origin": f"http://evil.example:{port}"},
               {**ok, "Host": "evil.example"}, {**ok, "Host": f"evil.example:{port}"},
               {**ok, "Host": f"127.0.0.1:{port + 1}"}]
        for path in ("/api/import/upload?name=csrf.gpx&type=Run", "/api/import/run"):
            for h in bad:
                with self.subTest(path=path, headers=h):
                    self.assertEqual(self.post(path, b"<gpx/>", h)[0], 403)
        self.assertFalse(os.path.exists(os.path.join(self.home, "imports", "activities", "csrf.gpx")))
        for h in ({**ok, "Host": f"localhost:{port}"}, {**ok, "Origin": f"http://127.0.0.1:{port}"}):
            code, body = self.post("/api/import/run", b"", h)  # past the guard: 400 nothing uploaded
            self.assertNotEqual(code, 403, body)

    def test_upload_run_poll_then_reload(self):
        hdr = {"X-Rollbook": "1"}
        listed = self.get("/api/activities")  # un-enriched DB: no 500, best efforts NULL
        self.assertEqual([a["best10min_kmh"] for a in listed], [None])
        before = {a["id"] for a in listed}
        path = os.path.join(tmpdir(self), "x.fit")
        write_fit(path, line_points(n=900, t0=T0 + timedelta(days=3)))
        with open(path, "rb") as f:
            data = f.read()
        up = "/api/import/upload?name=Evening_Skate.fit&type=Inline%20Skate"
        code, body = self.post(up, data, hdr)
        self.assertEqual((code, json.loads(body)), (200, {"kind": "activity", "stored": "Evening_Skate.fit"}))
        code, body = self.post("/api/import/run", b"", hdr)
        st = json.loads(body)
        self.assertEqual((code, st["state"], st["items"]), (200, "running", ["imports"]))
        st = self.poll()
        self.assertEqual((st["state"], st["phase"], st["added"]), ("done", "done", 1), st)
        aid = edits.import_id("activities/Evening_Skate.fit")
        acts = {a["id"]: a for a in self.get("/api/activities")}
        self.assertEqual(set(acts) - before, {aid})
        self.assertEqual((acts[aid]["name"], acts[aid]["type"]), ("Evening Skate", "Inline Skate"))
        self.assertIsNotNone(acts[aid]["best10min_kmh"])  # enrich --missing ran
        # same file again: re-queued, built, nothing new
        self.assertTrue(json.loads(self.post(up, data, hdr)[1]).get("duplicate"))
        self.assertEqual(self.post("/api/import/run", b"", hdr)[0], 200)
        st = self.poll()
        self.assertEqual((st["state"], st["added"]), ("done", 0), st)


if __name__ == "__main__":
    unittest.main()
