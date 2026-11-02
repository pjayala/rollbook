# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Pedro Ayala. Rollbook is free software, see LICENSE.
"""End to end: build a database from a small export, then hit the dashboard API."""

import csv
import http.client
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import threading
import unittest
import urllib.error
import urllib.request
from unittest import mock

from helpers import SAMPLES, TOOL, tmpdir

import edits
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

    def test_version(self):
        r = run(self.home, "--version")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertRegex(r.stdout.strip(), r"^rollbook \d+\.\d+\.\d+$")
        with open(os.path.join(os.path.dirname(TOOL), "..", "CHANGELOG.md"), encoding="utf-8") as f:
            self.assertIn(f"## {r.stdout.split()[1]} ", f.read())  # every version has release notes


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
        for p in ("/app.js", "/map.js", "/compare.js", "/style.css", "/favicon.svg", "/favicon.ico"):
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

    def test_compare_series_moving_clock(self):
        """#16: the series' full-resolution moving clock ends at the session's moving time,
        however the series was downsampled; distance never runs backwards."""
        for aid in (1001, 1002):
            s = self.get(f"/api/activity/{aid}")
            mv = s["series_cols"].index("mv")
            self.assertEqual(s["series"][0][0], 0)
            self.assertEqual(s["series"][0][mv], 0)
            km = [r[1] for r in s["series"]]
            self.assertEqual(km, sorted(km))
            self.assertAlmostEqual(s["series"][-1][mv], s["moving_s"], delta=5)
        self.assertAlmostEqual(self.get("/api/activity/1001")["series"][-1][-1], 3068, delta=5)  # 51:08

    def test_best_efforts_in_list_and_session(self):
        """#6: list columns be<label> in seconds, the same as the session's metrics."""
        acts = {a["id"]: a for a in self.get("/api/activities?type=Inline%20Skate")}
        for aid in (1001, 1002):
            m = self.get(f"/api/activity/{aid}")["metrics"]
            for label in ("400", "1k", "5k", "10k"):
                self.assertGreater(acts[aid]["be" + label], 0)
                self.assertEqual(acts[aid]["be" + label], m["be"][label][0])
            self.assertIsNone(acts[aid]["be21k"])
            self.assertIsNone(acts[aid]["be42k"])
        self.assertAlmostEqual(acts[1001]["be1k"], 147.0, delta=1.0)  # Outdoor_cycle.gpx, see test_parsing

    def test_activity_ids_are_strict(self):
        for path, code in (("/api/activity/abc", 400), ("/api/activity/%C2%B2", 400), ("/api/activity/" + "1" * 16, 400),
                           ("/api/activity/999999", 404)):
            with self.assertRaises(urllib.error.HTTPError) as cm:
                self.get(path)
            self.assertEqual(cm.exception.code, code, path)

    def test_page_has_a_content_security_policy(self):
        with urllib.request.urlopen(self.url, timeout=60) as r:
            csp = r.headers.get("Content-Security-Policy", "")
        self.assertIn("script-src 'self'", csp)
        self.assertIn("frame-ancestors 'none'", csp)
        idx = self.get("/").decode()
        self.assertNotRegex(idx, r"<script>|<script [^>]*>[^<]+</script>|\son[a-z]+=")  # nothing inline for the CSP to block

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

    def get_host(self, path, host):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=60)
        try:
            c.putrequest("GET", path, skip_host=True)
            if host is not None:
                c.putheader("Host", host)
            c.endheaders()
            r = c.getresponse()
            return r.status, r.read()
        finally:
            c.close()

    def test_get_refuses_foreign_host(self):
        """DNS rebinding: evil.example resolving to 127.0.0.1 must not read the API."""
        paths = ("/", "/app.js", "/api/status", "/api/activities?type=Inline%20Skate",
                 "/api/activity/1001", "/api/import", "/tiles/osm/1/1/1.png")
        for p in paths:
            for h in (f"evil.example:{self.port}", "evil.example", f"127.0.0.1:{self.port + 1}",
                      f"127.0.0.1.evil.example:{self.port}", None):
                code, body = self.get_host(p, h)
                self.assertEqual(code, 403, (p, h))
                self.assertNotIn(b"series", body)
        for h in (f"127.0.0.1:{self.port}", f"localhost:{self.port}"):
            self.assertEqual(self.get_host("/api/status", h)[0], 200, h)
            self.assertEqual(self.get_host("/?app=tok", h)[0], 200, h)  # app launcher URL

    def test_profile_api(self):
        h = {"X-Rollbook": "1", "Content-Type": "application/json"}
        self.assertEqual(self.post("/api/profile", b'{"weight_kg": 70}')[0], 403)
        code, body = self.post("/api/profile", b'{"weight_kg": 70, "sex": "male", "birth_year": 1985}', h)
        self.assertEqual(code, 200, body)
        self.assertEqual(self.get("/api/profile")["weight_kg"], 70.0)
        self.assertEqual(self.post("/api/profile", b'{"weight_kg": 9999}', h)[0], 400)
        self.assertEqual(self.post("/api/profile", b'{"ftp": 20}', h)[0], 400)
        code, _ = self.post("/api/profile", b'{"weight_kg": 70, "sex": "male", "birth_year": 1985, "ftp": 185}', h)
        self.assertEqual((code, self.get("/api/profile")["ftp"]), (200, 185))
        a = next(x for x in self.get("/api/activities?type=Inline%20Skate") if x["id"] == 1001)
        self.assertGreater(a["kcal"], 300)
        self.assertTrue(a["kcal_src"].startswith("MET"))
        self.assertGreater(self.get("/api/activity/1001")["cal"]["kcal"], 300)

    def test_favorites(self):
        h = {"X-Rollbook": "1", "Content-Type": "application/json"}
        code, _ = self.post("/api/favorites", json.dumps({"kind": "sessions", "id": "1001", "on": True}).encode(), h)
        self.assertEqual(code, 200)
        self.assertIn("1001", self.get("/api/favorites")["sessions"])
        self.assertEqual(self.post("/api/favorites", b'{"kind":"x","id":"1","on":true}', h)[0], 400)

    def edit(self, body, headers=None):
        h = {"X-Rollbook": "1", "Content-Type": "application/json"} if headers is None else headers
        code, raw = self.post("/api/edit", json.dumps(body).encode(), h)
        return code, json.loads(raw) if raw.startswith(b"{") else raw

    def test_edit_needs_header_and_same_origin(self):
        body = {"id": "1001", "name": "x"}
        self.assertEqual(self.edit(body, {"Content-Type": "application/json"})[0], 403)
        self.assertEqual(self.edit(body, {"X-Rollbook": "1", "Origin": "https://evil.example"})[0], 403)
        self.assertEqual(self.edit(body, {"X-Rollbook": "1", "Host": "evil.example"})[0], 403)
        self.assertEqual(self.get("/api/activity/1001")["name"], "Outdoor cycle")
        self.assertEqual(self.edit({"id": "99"})[0], 404)
        self.assertEqual(self.edit({"id": "../1"})[0], 400)
        self.assertEqual(self.edit({"id": "1001", "name": "a" * 201})[0], 400)
        self.assertEqual(self.edit({"id": "1001", "type": 5})[0], 400)
        self.assertEqual(self.edit({"id": "1001", "delete": True, "remove_file": True})[0], 400)  # not an import
        self.assertFalse(self.get("/api/activity/1001")["hidden"])

    def test_edit_rename_sport_hide_restore(self):
        try:
            code, r = self.edit({"id": "1002", "name": "  Come  back ", "type": "Ride"})
            self.assertEqual(code, 200, r)
            self.assertEqual(r["edit"]["name"], "Come back")
            s = self.get("/api/activity/1002")
            self.assertEqual((s["name"], s["type"], s["orig_type"]), ("Come back", "Ride", "Inline Skate"))
            self.assertEqual([a["id"] for a in self.get("/api/activities?type=Ride")], [1002])
            self.assertEqual([a["id"] for a in self.get("/api/activities?type=Inline%20Skate")], [1001])
            self.assertEqual({t["type"]: t["n"] for t in self.get("/api/types")}, {"Inline Skate": 1, "Ride": 1})

            self.assertEqual(self.edit({"id": "1002", "hidden": True})[0], 200)
            self.assertEqual(self.get("/api/activities?type=Ride"), [])
            self.assertEqual([t["type"] for t in self.get("/api/types")], ["Inline Skate"])
            listed = self.get("/api/activities?type=Ride&hidden=1")
            self.assertEqual([(a["id"], a["hidden"]) for a in listed], [(1002, 1)])
            self.assertTrue(self.get("/api/activity/1002")["hidden"])  # still viewable, to restore it

            self.assertEqual(self.edit({"id": "1002", "delete": True})[0], 200)
            self.assertTrue(self.get("/api/activity/1002")["deleted"])
            self.assertEqual(self.edit({"id": "1002", "restore": True})[0], 200)
            self.assertEqual(len(self.get("/api/activities?type=Ride")), 1)
            with open(os.path.join(self.home, "edits.json")) as f:
                self.assertEqual(json.load(f)["sessions"]["1002"]["type"], "Ride")
        finally:  # back to the original: empty / original values drop the override
            self.edit({"id": "1002", "name": "", "type": "Inline Skate", "restore": True})
        self.assertNotIn("1002", self.get("/api/edits")["sessions"])
        self.assertEqual(self.get("/api/activity/1002")["name"], "The come back")

    # ---- every JSON POST reads its body whole or not at all (web/server.py _read_json)

    ENDPOINTS = ("/api/profile", "/api/edit", "/api/favorites", "/api/last_import/dismiss")

    def raw(self, path, length=None, body=b"", extra=()):
        """Status of a hand-made POST. The server speaks HTTP/1.0 (one request per
        connection), so "still usable" = it answers the next connection."""
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        try:
            c.putrequest("POST", path)
            hs = [("X-Rollbook", "1"), ("Content-Type", "application/json"), *extra]
            if length is not None:
                hs.append(("Content-Length", length))
            for k, v in hs:
                c.putheader(k, v)
            c.endheaders()
            c.send(body)
            r = c.getresponse()
            r.read()
            return r.status
        finally:
            c.close()

    def alive(self):
        self.assertEqual(self.get("/api/status").get("readonly"), False)

    def test_out_of_range_lengths(self):
        for path in self.ENDPOINTS:
            limit = 4096 if path.endswith("dismiss") else 65536
            big = b'{"pad":"' + b"x" * limit + b'"}'
            with self.subTest(path=path):
                self.assertEqual(self.raw(path, str(len(big)), big), 413)
                self.assertEqual(self.raw(path, "-1"), 400)  # used to read until EOF
                self.assertEqual(self.raw(path, "abc"), 400)
                self.assertEqual(self.raw(path), 411)  # no length: never guess the body is {}
                self.assertEqual(self.raw(path, None, b"2\r\n{}\r\n0\r\n\r\n",
                                          [("Transfer-Encoding", "chunked")]), 501)
        self.alive()

    def test_upload_negative_length(self):
        self.assertEqual(self.raw("/api/import/upload?name=a.gpx", "-1"), 400)  # used to read to EOF
        self.assertEqual(self.raw("/api/import/upload?name=a.gpx", "x"), 400)
        self.alive()

    def test_bad_json(self):
        for path in self.ENDPOINTS:
            for body in (b"not json", b"[1]", b'"x"', b'{"id": NaN}', b'{"a": Infinity}', b'{"a": 1e999}',
                         b"[" * 2000 + b"]" * 2000, b"\xff\xfe"):
                with self.subTest(path=path, body=body[:12]):
                    self.assertEqual(self.raw(path, str(len(body)), body), 400)
        self.assertEqual(self.get("/api/activity/1001")["name"], "Outdoor cycle")
        self.alive()

    def test_edit_bad_text_does_not_wedge_the_store(self):
        # a lone surrogate used to stay in memory and fail every later save
        body = b'{"id": "1001", "name": "\\ud800"}'
        self.assertEqual(self.raw("/api/edit", str(len(body)), body), 400)
        try:
            code, r = self.edit({"id": "1001", "name": "Caf\u00e9 \u2192 river"})
            self.assertEqual(code, 200, r)
            with open(os.path.join(self.home, "edits.json"), encoding="ascii") as f:  # escapes only
                self.assertEqual(json.load(f)["sessions"]["1001"]["name"], "Caf\u00e9 \u2192 river")
        finally:
            self.edit({"id": "1001", "name": ""})
        self.assertEqual(self.edit({"id": "9" * 19})[0], 400)  # beyond int64: refused, not a dropped connection

    def test_favorites_meta_is_checked(self):
        h = {"X-Rollbook": "1", "Content-Type": "application/json"}

        def fav(meta, on=True, key="ab12"):
            body = json.dumps({"kind": "segments", "id": key, "on": on, "meta": meta}).encode()
            return self.post("/api/favorites", body, h)[0]
        try:
            self.assertEqual(fav([1]), 200)
            self.assertEqual(self.get("/api/favorites")["segments"]["ab12"].keys(), {"added"})
            good = {"type": "Inline Skate", "start": [51.5, -0.1], "end": [51.6, -0.2], "m": 2500}
            self.assertEqual(fav({**good, "x": 1}), 200)
            self.assertEqual({k: v for k, v in self.get("/api/favorites")["segments"]["ab12"].items()
                              if k != "added"}, good)
            self.assertEqual(fav({"type": "x" * 41, "start": [91, 0], "end": [0, 0, 0], "m": {"a": [[[]]]}}), 200)
            self.assertEqual(self.get("/api/favorites")["segments"]["ab12"].keys(), {"added"})
            with mock.patch.object(server, "FAV_MAX", 1):
                self.assertEqual(fav({}, key="cd34"), 400)  # full
                self.assertEqual(fav(good), 200)  # re-starring an existing one is fine
        finally:
            fav(None, on=False)


class Stores(unittest.TestCase):
    """The small JSON stores: atomic 0600 writes, files that aren't an object."""

    def setUp(self):
        self.home = tmpdir(self)

    def write(self, name, raw):
        with open(os.path.join(self.home, name), "w", encoding="utf-8") as f:
            f.write(raw)

    def test_non_object_files_load_as_empty(self):
        import profile as athlete
        for raw in ("[]", "1", '"x"', "null", '{"sessions": []}', '{"sessions": {"1": 5}}', "{nope"):
            with self.subTest(raw=raw):
                self.write("edits.json", raw)
                self.write("favorites.json", raw)
                self.write("profile.json", raw)
                self.assertEqual(edits.Edits(self.home).all()["sessions"], {})
                fav = server.Favorites(os.path.join(self.home, "favorites.json")).get()
                self.assertEqual((fav["sessions"], fav["segments"]), ({}, {}))
                self.assertIsInstance(athlete.Profile(self.home).get(), dict)

    def test_write_is_atomic_and_private(self):
        import store
        path = os.path.join(self.home, "x.json")
        store.write_json(path, {"a": 1})
        self.assertEqual(store.load_object(path), {"a": 1})
        if os.name == "posix":
            self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
        with self.assertRaises(ValueError):
            store.write_json(path, {"a": float("nan")})
        self.assertEqual(store.load_object(path), {"a": 1})
        self.assertEqual([n for n in os.listdir(self.home) if n.endswith(".tmp")], [])

    def test_write_failure_raises_and_keeps_old_file(self):
        import store
        from unittest import mock
        path = os.path.join(self.home, "x.json")
        store.write_json(path, {"a": 1})
        with mock.patch.object(store.os, "replace", side_effect=OSError("locked")) as rep, \
                mock.patch.object(store.time, "sleep"):
            with self.assertRaises(OSError):
                store.write_json(path, {"a": 2})
        self.assertEqual(rep.call_count, 2)  # one retry (Windows: a reader holds the file)
        self.assertEqual(store.load_object(path), {"a": 1})
        self.assertEqual([n for n in os.listdir(self.home) if n.endswith(".tmp")], [])

    def test_retry_succeeds(self):
        import store
        from unittest import mock
        path = os.path.join(self.home, "x.json")
        real = os.replace
        calls = []

        def flaky(a, b):
            calls.append(a)
            if len(calls) == 1:
                raise PermissionError("locked")
            return real(a, b)
        with mock.patch.object(store.os, "replace", side_effect=flaky), mock.patch.object(store.time, "sleep"):
            store.write_json(path, {"a": 3})
        self.assertEqual(store.load_object(path), {"a": 3})

    def test_planted_symlink_never_followed(self):
        import store
        if not hasattr(os, "symlink") or os.name != "posix":
            self.skipTest("posix only")
        from unittest import mock
        victim = os.path.join(self.home, "victim")
        with open(victim, "w") as f:
            f.write("keep")
        path = os.path.join(self.home, "x.json")
        planted = path + ".planted.tmp"
        os.symlink(victim, planted)
        with mock.patch.object(store, "_tmp_name", return_value=planted):
            with self.assertRaises(OSError):
                store.write_json(path, {"a": 1})
        self.assertTrue(os.path.islink(planted))  # left alone, not written through
        with open(victim) as f:
            self.assertEqual(f.read(), "keep")

    def test_profile_file_values_checked(self):
        import profile as athlete
        self.write("profile.json", json.dumps({"hr_max": "190", "weight_kg": 70, "height_cm": 1e9,
                                               "sex": "robot", "name": ["x"], "ftp": True, "hr_rest": 50.0,
                                               "birth_year": 1980.5, "extra": 1}))
        p = athlete.Profile(self.home).get()
        self.assertEqual((p.get("hr_max"), p.get("weight_kg"), p.get("height_cm"), p.get("sex"), p.get("name"),
                          p.get("ftp"), p.get("hr_rest"), p.get("birth_year"), p.get("extra")),
                         (190, 70, None, None, None, None, 50, None, None))

    def test_profile_set_checks_and_write_failure(self):
        import profile as athlete
        import store
        pr = athlete.Profile(self.home)
        for bad in ({"name": [1] * 3}, {"name": {"a": 1}}, {"hr_max": True}, {"hr_max": "nan"},
                    {"weight_kg": "inf"}, {"sex": "robot"}, {"hr_max": [190]}, {"hr_rest": "60.5"},
                    {"hr_max": "1e999"}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                pr.set(bad)
        p = pr.set({"hr_max": "188", "hr_rest": "60.0", "name": " Ann "})  # form strings still fine
        self.assertEqual((p["hr_max"], p["hr_rest"], type(p["hr_rest"])), (188, 60, int))
        with mock.patch.object(store, "write_json", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                pr.set({"hr_max": 170})
        self.assertEqual((pr.get()["hr_max"], pr.get()["name"]), (188, "Ann"))

    def test_favorites_file_checked_on_load(self):
        segs = {f"s{i}": {"added": "2026-01-01"} for i in range(server.FAV_MAX + 5)}
        segs["ab"] = {"added": "x" * 50, "type": "Ride", "start": [1, 2], "m": "big", "junk": [[1]]}
        self.write("favorites.json", json.dumps({"sessions": {"../x": {}, "12": {"added": "2026-01-02", "z": 1},
                                                              "13": 5}, "segments": segs}))
        d = server.Favorites(os.path.join(self.home, "favorites.json")).get()
        self.assertEqual(d["sessions"], {"12": {"added": "2026-01-02"}})
        self.assertEqual(len(d["segments"]), server.FAV_MAX)
        self.assertNotIn("ab", d["segments"])  # past the cap (insertion order)
        self.write("favorites.json", json.dumps({"segments": {"ab": segs["ab"]}}))
        self.assertEqual(server.Favorites(os.path.join(self.home, "favorites.json")).get()["segments"]["ab"],
                         {"type": "Ride", "start": [1, 2]})

    def test_favorites_write_failure_keeps_memory(self):
        import store
        fav = server.Favorites(os.path.join(self.home, "favorites.json"))
        with mock.patch.object(store, "write_json", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                fav.set("sessions", "1", True)
        self.assertEqual(fav.get()["sessions"], {})

    def test_edit_write_failure_keeps_memory(self):
        export = os.path.join(self.home, "export")
        make_export(export)
        self.assertEqual(run(self.home, "db", "build", "--no-samples", export).returncode, 0)
        import store
        from unittest import mock
        e = edits.Edits(self.home)
        db = sqlite3.connect(f"file:{os.path.join(self.home, 'rollbook.db')}?mode=ro", uri=True)
        self.addCleanup(db.close)
        e.sync(db)
        with mock.patch.object(store, "write_json", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                e.update(db, 1001, {"name": "x"})
        self.assertEqual(e.get(1001), {})


class PowerBuild(unittest.TestCase):
    """Power through the whole pipeline, and an older DB without the power columns."""

    def setUp(self):
        from datetime import datetime, timedelta, timezone
        from helpers import write_fit
        self.home = tmpdir(self)
        self.export = os.path.join(self.home, "export")
        os.makedirs(os.path.join(self.export, "activities"))
        t0 = datetime(2026, 3, 1, 8, 0, tzinfo=timezone.utc)
        # indoor: no GPS, 30 min of 180 W with 30 s of 400 W in the middle
        pts = [(t0 + timedelta(seconds=i), None, None, 20.0, 130, 400 if 900 <= i < 930 else 180, 85) for i in range(1800)]
        write_fit(os.path.join(self.export, "activities", "ride.fit"), pts, session_sport=2,
                  session_fields={20: 184, 34: 190, 35: 451, 36: 950})
        with open(os.path.join(self.export, "activities.csv"), "w", newline="") as f:
            csv.writer(f).writerows([["Activity ID", "Activity Date", "Activity Name", "Activity Type", "Filename", "Average Watts"],
                                     ["2001", "", "ROUVY test", "Virtual Ride", "activities/ride.fit", "184"]])

    def test_power_in_db_and_api(self):
        r = run(self.home, "db", "build", self.export)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(run(self.home, "db", "enrich").returncode, 0)
        db = sqlite3.connect(os.path.join(self.home, "rollbook.db"))
        self.addCleanup(db.close)
        avg, mx, sw = db.execute("SELECT watts_avg, watts_max, s_avg_watts FROM activities").fetchone()
        self.assertAlmostEqual(avg, 184, delta=1)
        self.assertAlmostEqual(avg, sw, delta=1)
        self.assertEqual(mx, 400)
        self.assertEqual(db.execute("SELECT COUNT(watts) FROM samples").fetchone(), (1800,))
        db.close()
        M = server.Metrics(os.path.join(self.home, "rollbook.db"), os.path.join(self.home, "cache", "m.json"))
        con = server.connect(os.path.join(self.home, "rollbook.db"))
        self.addCleanup(con.close)
        a = next(x for x in server.api_activities(con, {"type": "Virtual Ride"}, M) if x["id"] == 2001)
        self.assertEqual(round(a["pw5s"]), 400)
        self.assertEqual(round(a["pw30s"]), 400)
        self.assertEqual(round(a["pw30m"]), round(a["watts_avg"]))
        self.assertEqual(sum(a["pw_hist"].values()), 1800)
        self.assertEqual((a["f_np"], a["f_ftp"], a["cad_avg"]), (190, 200, 85))
        self.assertAlmostEqual(a["pw20m"], (30 * 400 + 1170 * 180) / 1200, delta=1)
        self.assertGreater(a["np"], a["watts_avg"])
        s = server.api_activity(con, {}, M, 2001)
        w = s["series_cols"].index("watts")
        self.assertEqual(s["series_cols"][-1], "mv")
        self.assertTrue(all(isinstance(r[w], int) for r in s["series"]))

    def test_older_db_is_migrated_in_place(self):
        from helpers import load_tool
        old = load_tool().SCHEMA  # the schema as it was before the power columns
        import re
        old = re.sub(r"parse_error TEXT,.*?\n\);", "parse_error TEXT\n);", old, count=1, flags=re.S).replace(",\n  watts INTEGER", "")
        self.assertNotIn("watts", old.replace("s_avg_watts", "").replace("s_max_watts", ""))
        db = sqlite3.connect(os.path.join(self.home, "rollbook.db"))
        db.executescript(old)
        db.execute("INSERT INTO activities (id, filename) VALUES (1, 'activities/old.fit')")
        db.commit()
        db.close()
        r = run(self.home, "db", "build", self.export)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("db build --fresh", r.stderr)
        db = sqlite3.connect(os.path.join(self.home, "rollbook.db"))
        self.addCleanup(db.close)
        self.assertEqual(db.execute("SELECT COUNT(watts) FROM samples").fetchone(), (1800,))


class EditsStore(unittest.TestCase):
    """web/edits.py on its own: the SQL overlay, rebuild survival, deleting imports."""

    def setUp(self):
        self.home = tmpdir(self)

    def ro(self, e):
        db = sqlite3.connect(f"file:{os.path.join(self.home, 'rollbook.db')}?mode=ro", uri=True)
        e.sync(db)
        self.addCleanup(db.close)
        return db

    def test_survives_fresh_rebuild(self):
        export = os.path.join(self.home, "export")
        make_export(export)
        self.assertEqual(run(self.home, "db", "build", export).returncode, 0)
        e = edits.Edits(self.home)
        db = self.ro(e)
        e.update(db, 1001, {"name": "Riverside", "type": "Ride"})
        e.update(db, 1002, {"hidden": True})
        db.close()  # Windows can't delete an open DB file
        r = run(self.home, "db", "build", "--fresh", export)
        self.assertEqual(r.returncode, 0, r.stderr)
        e = edits.Edits(self.home)  # as a restarted server would
        db = self.ro(e)
        self.assertEqual(db.execute("SELECT id, name, type FROM activities").fetchall(), [(1001, "Riverside", "Ride")])
        self.assertEqual(db.execute("SELECT name, type FROM main.activities WHERE id=1001").fetchone(),
                         ("Outdoor cycle", "Inline Skate"))
        self.assertEqual(db.execute("SELECT COUNT(*) FROM activities_all WHERE hidden").fetchone(), (1,))

    def test_sync_follows_later_edits(self):
        export = os.path.join(self.home, "export")
        make_export(export)
        run(self.home, "db", "build", "--no-samples", export)
        e = edits.Edits(self.home)
        db = self.ro(e)
        self.assertEqual(db.execute("SELECT COUNT(*) FROM activities").fetchone(), (2,))
        e.update(db, 1001, {"hidden": True})
        e.sync(db)
        self.assertEqual(db.execute("SELECT COUNT(*) FROM activities").fetchone(), (1,))

    def test_delete_removes_import_file_and_reimport_restores(self):
        imp = os.path.join(self.home, "imports")
        os.makedirs(os.path.join(imp, "activities"))
        for n in ("a.gpx", "b.gpx"):
            shutil.copy(os.path.join(SAMPLES, "Outdoor_cycle.gpx" if n == "a.gpx" else "The_come_back.gpx"),
                        os.path.join(imp, "activities", n))
        with open(os.path.join(imp, "activities.csv"), "w", newline="") as f:
            csv.writer(f).writerows([["Activity ID", "Activity Date", "Activity Name", "Activity Type", "Filename"],
                                     ["", "", "a", "Inline Skate", "activities/a.gpx"],
                                     ["", "", "b", "Inline Skate", "activities/b.gpx"]])
        self.assertEqual(run(self.home, "db", "build", "--no-samples", imp).returncode, 0)
        aid = edits.import_id("activities/a.gpx")
        e = edits.Edits(self.home)
        db = self.ro(e)
        self.assertIsNotNone(e.import_file(aid, "activities/a.gpx"))
        self.assertIsNone(e.import_file(aid + 1, "activities/a.gpx"))  # id must match the path
        self.assertIsNone(e.import_file(edits.import_id("../x.gpx"), "../x.gpx"))
        ed, _ = e.update(db, aid, {"delete": True, "remove_file": True})
        self.assertTrue(ed["deleted"] and ed["file_removed"])
        self.assertFalse(os.path.exists(os.path.join(imp, "activities", "a.gpx")))
        with open(os.path.join(imp, "activities.csv")) as f:
            self.assertEqual([r[-1] for r in csv.reader(f)], ["Filename", "activities/b.gpx"])
        e.sync(db)
        self.assertEqual(db.execute("SELECT name FROM activities").fetchall(), [("b",)])
        e.forget_removed("activities/a.gpx")  # what the importer calls on re-upload
        self.assertEqual(e.get(aid), {})

    def test_delete_file_only_after_edit_saved(self):
        import store
        imp = os.path.join(self.home, "imports")
        os.makedirs(os.path.join(imp, "activities"))
        shutil.copy(os.path.join(SAMPLES, "Outdoor_cycle.gpx"), os.path.join(imp, "activities", "a.gpx"))
        with open(os.path.join(imp, "activities.csv"), "w", newline="") as f:
            csv.writer(f).writerows([["Activity ID", "Activity Date", "Activity Name", "Activity Type", "Filename"],
                                     ["", "", "a", "Inline Skate", "activities/a.gpx"]])
        self.assertEqual(run(self.home, "db", "build", "--no-samples", imp).returncode, 0)
        aid, f = edits.import_id("activities/a.gpx"), os.path.join(imp, "activities", "a.gpx")
        e = edits.Edits(self.home)
        db = self.ro(e)
        with mock.patch.object(store, "write_json", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                e.update(db, aid, {"delete": True, "remove_file": True})
        self.assertTrue(os.path.exists(f))  # the edit wasn't saved, so the file stays
        self.assertEqual(e.get(aid), {})
        with mock.patch.object(edits.os, "remove", side_effect=PermissionError(13, "in use")):
            with self.assertRaises(edits.RemoveFailed):
                e.update(db, aid, {"delete": True, "remove_file": True})
        self.assertTrue(e.get(aid)["deleted"])
        self.assertNotIn("file_removed", e.get(aid))  # it is still there: no false claim
        with open(os.path.join(imp, "activities.csv")) as fh:  # its row went first; a retry finishes
            self.assertEqual([r[-1] for r in csv.reader(fh)], ["Filename"])
        e.update(db, aid, {"delete": True, "remove_file": True})
        self.assertFalse(os.path.exists(f))
        self.assertTrue(e.get(aid)["file_removed"])


if __name__ == "__main__":
    unittest.main()
