# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Pedro Ayala. Rollbook is free software, see LICENSE.
"""End to end: build a database from a small export, then hit the dashboard API."""

import csv
import json
import os
import shutil
import subprocess
import sys
import threading
import unittest
import urllib.error
import urllib.request

from helpers import SAMPLES, TOOL, tmpdir

import server

IDS = {"Outdoor_cycle.gpx": 1001, "The_come_back.gpx": 1002}


def run(home, *args):
    env = dict(os.environ, ROLLBOOK_HOME=home, PYTHONIOENCODING="utf-8")
    return subprocess.run([sys.executable, TOOL, *args], env=env, capture_output=True, text=True, timeout=300)


def make_export(root):
    os.makedirs(os.path.join(root, "activities"))
    with open(os.path.join(root, "activities.csv"), "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["Activity ID", "Activity Date", "Activity Name", "Activity Type", "Filename"])
        for name, aid in IDS.items():
            shutil.copy(os.path.join(SAMPLES, name), os.path.join(root, "activities", name))
            w.writerow([aid, "", name[:-4].replace("_", " "), "Inline Skate", f"activities/{name}"])


class Build(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import tempfile
        cls.home = tempfile.mkdtemp(prefix="rollbook-test-db-")
        cls.export = os.path.join(cls.home, "export")
        make_export(cls.export)
        r = run(cls.home, "db", "build", cls.export)
        assert r.returncode == 0, r.stdout + r.stderr
        r = run(cls.home, "db", "enrich")
        assert r.returncode == 0, r.stdout + r.stderr
        cls.db = os.path.join(cls.home, "rollbook.db")

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.home, True)

    def query(self, sql):
        r = run(self.home, "db", "query", "--csv", sql)
        self.assertEqual(r.returncode, 0, r.stderr)
        return list(csv.reader(r.stdout.splitlines()))[1:]

    def test_imported_with_metadata(self):
        rows = self.query("SELECT id, type, ROUND(km, 3), best10min_kmh IS NOT NULL FROM activities ORDER BY id")
        self.assertEqual(rows, [["1001", "Inline Skate", "18.206", "1"], ["1002", "Inline Skate", "14.04", "1"]])

    def test_incremental_build_skips_existing(self):
        r = run(self.home, "db", "build", self.export)
        self.assertIn("importing 0 activities", r.stdout)

    def test_refuses_directory_without_activities_csv(self):
        r = run(self.home, "db", "build", SAMPLES)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("activities.csv", r.stderr)

    def test_same_session_from_another_file_is_skipped(self):
        imp = os.path.join(self.home, "imports")
        os.makedirs(os.path.join(imp, "activities"), exist_ok=True)
        shutil.copy(os.path.join(SAMPLES, "Outdoor_cycle.gpx"), os.path.join(imp, "activities", "copy.gpx"))
        with open(os.path.join(imp, "activities.csv"), "w", newline="") as f:
            csv.writer(f).writerows([["Activity ID", "Activity Name", "Activity Type", "Filename"],
                                     ["", "copy", "Inline Skate", "activities/copy.gpx"]])
        r = run(self.home, "db", "build", imp)
        self.assertIn("skipped, same session", r.stdout + r.stderr)
        self.assertEqual(self.query("SELECT COUNT(*) FROM activities"), [["2"]])

    def test_query_is_read_only(self):
        r = run(self.home, "db", "query", "DELETE FROM activities")
        self.assertNotEqual(r.returncode, 0)


class Api(Build):
    """Same database, served by web/server.py on a free port, offline."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.httpd, cls.M, cls.url = server.start(cls.db, os.path.join(cls.home, "cache", "web_metrics.json"), 0, offline=True)
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()
        cls.port = cls.httpd.server_address[1]

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        super().tearDownClass()

    def get(self, path):
        with urllib.request.urlopen(self.url.rstrip("/") + path, timeout=60) as r:
            body = r.read()
            return json.loads(body) if r.headers.get("Content-Type", "").startswith("application/json") else body

    def post(self, path, body=b"", headers=None):
        req = urllib.request.Request(self.url.rstrip("/") + path, data=body, method="POST", headers=headers or {})
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()

    def test_static_and_icons(self):
        self.assertIn(b"Rollbook", self.get("/"))
        for p in ("/app.js", "/map.js", "/style.css", "/favicon.svg", "/favicon.ico"):
            self.assertTrue(self.get(p))

    def test_path_traversal_blocked(self):
        with self.assertRaises(urllib.error.HTTPError):
            self.get("/../server.py")
        with self.assertRaises(urllib.error.HTTPError):
            self.get("/tiles/evil/1/1/1.png")

    def test_activities_and_session(self):
        types = self.get("/api/types")
        self.assertEqual(types[0]["type"], "Inline Skate")
        acts = self.get("/api/activities?type=Inline%20Skate")
        self.assertEqual(len(acts), 2)
        a = next(x for x in acts if x["id"] == 1001)
        self.assertEqual(a["route"], "out-back")
        self.assertAlmostEqual(a["out_kmh"], 22.5, delta=0.5)
        s = self.get("/api/activity/1001")
        self.assertGreater(len(s["series"]), 100)
        self.assertEqual(len(s["splits"]), 18)

    def test_import_needs_header_and_same_origin(self):
        gpx = open(os.path.join(SAMPLES, "The_come_back.gpx"), "rb").read()
        self.assertEqual(self.post("/api/import/upload?name=a.gpx", gpx)[0], 403)
        bad = {"X-Rollbook": "1", "Origin": "https://evil.example"}
        self.assertEqual(self.post("/api/import/upload?name=a.gpx", gpx, bad)[0], 403)
        code, body = self.post("/api/import/upload?name=notes.txt", b"hi", {"X-Rollbook": "1"})
        self.assertEqual(code, 415, body)

    def test_bye_needs_same_origin(self):
        self.assertEqual(self.post("/api/bye")[0], 403)
        self.assertEqual(self.post("/api/bye", b"", {"Origin": "https://evil.example"})[0], 403)
        self.assertEqual(self.post("/api/bye", b"", {"Origin": f"http://127.0.0.1:{self.port}"})[0], 204)

    def test_favorites(self):
        h = {"X-Rollbook": "1", "Content-Type": "application/json"}
        code, _ = self.post("/api/favorites", json.dumps({"kind": "sessions", "id": "1001", "on": True}).encode(), h)
        self.assertEqual(code, 200)
        self.assertIn("1001", self.get("/api/favorites")["sessions"])
        self.assertEqual(self.post("/api/favorites", b'{"kind":"x","id":"1","on":true}', h)[0], 400)


if __name__ == "__main__":
    unittest.main()
