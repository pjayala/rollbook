# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Pedro Ayala. Rollbook is free software, see LICENSE.
"""Strava web sync (web/strava_web.py, `rollbook strava`, POST /api/strava/sync),
against a local fake of strava.com. No network."""

import csv
import gzip
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

from helpers import TOOL, line_points, tmpdir, write_fit

import importer
import server
import strava_web as sw

GOOD = "goodsession0123456789abcdef"
T0 = datetime(2026, 10, 20, 8, 0, tzinfo=timezone.utc)


def fit_bytes(test, t0, n=600, speed=6.0):
    p = os.path.join(tmpdir(test), "x.fit")
    write_fit(p, line_points(n=n, t0=t0, speed_ms=speed))
    with open(p, "rb") as f:
        return f.read()


def model(aid, name, typ, t0):
    """Shaped like a /athlete/training_activities model (only the fields we read)."""
    return {"id": aid, "name": name, "type": typ.replace(" ", ""), "display_type": typ,
            "start_time": t0.strftime("%Y-%m-%dT%H:%M:%S+0000"), "distance_raw": 1234.5}


class Fake:
    """A tiny strava.com: the activity list, export_original, a login redirect."""

    def __init__(self, acts, files, per_page=2, redirect=None, rotate_to=None):
        self.acts, self.files, self.per_page = acts, files, per_page
        self.redirect = redirect or {}  # id -> absolute URL (another origin)
        self.cookies = {GOOD}
        self.public = False  # True: serve without a cookie (a file host on another origin)
        self.rotate_to = rotate_to
        self.seen = []  # (path, cookie header)
        fake = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                u = urllib.parse.urlsplit(self.path)
                fake.seen.append((u.path, self.headers.get("Cookie"), self.headers.get("X-Requested-With")))
                if u.path == "/login":
                    return self.reply(200, b"<html>log in</html>", "text/html")
                m = re.search(r"_strava4_session=([^;]+)", self.headers.get("Cookie") or "")
                if not fake.public and (not m or m.group(1) not in fake.cookies):
                    self.send_response(302)
                    self.send_header("Location", "/login")
                    self.send_header("Content-Length", "0")
                    return self.end_headers()
                extra = {}
                if fake.rotate_to and m and m.group(1) != fake.rotate_to:
                    fake.cookies.add(fake.rotate_to)
                    extra["Set-Cookie"] = f"_strava4_session={fake.rotate_to}; path=/; HttpOnly"
                if u.path == "/athlete/training_activities":
                    page = int(urllib.parse.parse_qs(u.query)["page"][0])
                    sl = fake.acts[(page - 1) * fake.per_page: page * fake.per_page]
                    body = json.dumps({"models": sl, "page": page, "perPage": fake.per_page,
                                       "total": len(fake.acts)}).encode()
                    return self.reply(200, body, "application/json", extra)
                m = re.fullmatch(r"/activities/(\d+)/export_original", u.path)
                if m:
                    aid = int(m.group(1))
                    if aid in fake.redirect:
                        self.send_response(302)
                        self.send_header("Location", fake.redirect[aid])
                        self.send_header("Content-Length", "0")
                        return self.end_headers()
                    if aid in fake.files:
                        data, name = fake.files[aid]
                        return self.reply(200, data, "application/octet-stream",
                                          {**extra, "Content-Disposition": f'attachment; filename="{name}"'})
                    return self.reply(200, b"<!DOCTYPE html><html>activity page</html>", "text/html", extra)
                self.reply(404, b"nope", "text/plain")

            def reply(self, code, body, ctype, extra=None):
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                for k, v in (extra or {}).items():
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(body)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


def client(fake, cookie=GOOD):
    return sw.Client(cookie, base=fake.url, delay=0, sleep=lambda s: None)


def index(home):
    with open(os.path.join(home, "imports", "activities.csv"), newline="", encoding="utf-8") as f:
        return list(csv.reader(f))


class Parsing(unittest.TestCase):
    def test_parse_cookie(self):
        for text in (GOOD, f"  {GOOD}\n", f"_strava4_session={GOOD}",
                     f"Cookie: CookieConsent={{x}}; _strava_cpra=opted_out; _strava4_session={GOOD}; _currentH=d3"):
            self.assertEqual(sw.parse_cookie(text), GOOD, text)
        for bad in ("", "short", "has spaces in it, not a cookie value", "a;b=c" * 5):
            with self.assertRaises(ValueError):
                sw.parse_cookie(bad)

    def test_parse_list(self):
        body = json.dumps({"models": [model(7, "Evening skate", "Inline Skate", T0),
                                      {"id": "8", "name": None, "type": "VirtualRide",
                                       "start_date_local_raw": 1790000000}],
                           "total": 40}).encode()
        acts, total = sw.parse_list(body)
        self.assertEqual(total, 40)
        self.assertEqual(acts[0], {"id": 7, "name": "Evening skate", "type": "Inline Skate",
                                   "start": "2026-10-20T08:00:00+0000"})
        self.assertEqual((acts[1]["id"], acts[1]["name"], acts[1]["type"]), (8, "", "Virtual Ride"))
        self.assertTrue(acts[1]["start"].startswith("2026-09-21"))
        self.assertEqual(sw.parse_list(b"[]"), ([], None))
        with self.assertRaises(sw.AuthExpired):
            sw.parse_list(b"\n<!DOCTYPE html><html>log in</html>")
        for junk in (b"{nope", b'{"rows": []}', b'{"models": [{"id": "x"}]}'):
            with self.assertRaises(sw.FormatChanged):
                sw.parse_list(junk)

    def test_sniff_ext(self):
        fit = fit_bytes(self, T0, n=5)
        self.assertEqual(sw.sniff_ext(fit), ".fit")
        self.assertEqual(sw.sniff_ext(gzip.compress(fit)), ".fit.gz")
        self.assertEqual(sw.sniff_ext(b"\xef\xbb\xbf  <?xml version='1.0'?><gpx creator='x'>"), ".gpx")
        self.assertEqual(sw.sniff_ext(gzip.compress(b"<?xml?>\n<TrainingCenterDatabase>")), ".tcx.gz")
        for other in (b"<!DOCTYPE html><html>", b"", b"\x1f\x8bbroken"):
            self.assertIsNone(sw.sniff_ext(other))

    def test_cookie_file_private(self):
        home = tmpdir(self)
        self.assertIsNone(sw.load_cookie(home))
        sw.save_cookie(home, GOOD)
        self.assertEqual(sw.load_cookie(home), GOOD)
        if os.name == "posix":
            self.assertEqual(os.stat(sw.cookie_path(home)).st_mode & 0o777, 0o600)
        self.assertNotIn(GOOD, json.dumps(sw.status(home)))
        self.assertTrue(sw.forget_cookie(home))
        self.assertFalse(sw.forget_cookie(home))

    def test_base_override_only_loopback(self):
        code = ("import sys; sys.path.insert(0, %r); import strava_web; print(strava_web.BASE)"
                % os.path.dirname(sw.__file__))
        for val, want in (("http://127.0.0.1:9", "http://127.0.0.1:9"), ("https://evil.example", sw.BASE),
                          ("http://127.0.0.1.evil.example:9", sw.BASE)):
            r = subprocess.run([sys.executable, "-c", code], env=dict(os.environ, ROLLBOOK_STRAVA_BASE=val),
                               capture_output=True, text=True, timeout=60)
            self.assertEqual(r.stdout.strip(), want, val)


class Sync(unittest.TestCase):
    def setUp(self):
        self.home = tmpdir(self)
        self.db = os.path.join(self.home, "rollbook.db")
        d = timedelta(days=1)
        # newest first, like the website; 2 per page
        self.acts = [model(105, "Manual yoga", "Yoga", T0 + 4 * d),
                     model(104, "Morning run", "Run", T0 + 3 * d),
                     model(103, "Evening skate", "Inline Skate", T0 + 2 * d),
                     model(102, "Old skate", "Inline Skate", T0 + d),
                     model(101, "Older skate", "Inline Skate", T0)]
        self.files = {i: (fit_bytes(self, T0 + (i - 101) * d), "Outdoor_cycle.fit") for i in (101, 102, 103, 104)}
        self.fake = Fake(self.acts, self.files)
        self.addCleanup(self.fake.close)
        self.logs = []

    def known(self, *ids):
        db = sqlite3.connect(self.db)
        db.execute("CREATE TABLE IF NOT EXISTS activities (id INTEGER PRIMARY KEY)")
        db.executemany("INSERT OR IGNORE INTO activities VALUES (?)", [(i,) for i in ids])
        db.commit()
        db.close()

    def sync(self, **kw):
        return sw.sync(self.home, self.db, client(self.fake), log=self.logs.append, **kw)

    def lists(self):
        return [p for p, _, _ in self.fake.seen if p == "/athlete/training_activities"]

    def test_downloads_new_oldest_first_and_stops_at_known_page(self):
        self.known(101, 102)
        r = self.sync()
        self.assertEqual(r, {"new": 3, "downloaded": 2, "no_file": 1, "dry_run": False})
        self.assertEqual(len(self.lists()), 3)  # page 3 (101) is the first with nothing new
        rows = index(self.home)
        self.assertEqual(rows[0], sw.CSV_HEAD)
        self.assertEqual([r[0] for r in rows[1:]], ["103", "104"])
        self.assertEqual(rows[1][2:], ["Evening skate", "Inline Skate", "activities/strava-103.fit"])
        acts = os.path.join(self.home, "imports", "activities")
        self.assertEqual(sorted(os.listdir(acts)), ["strava-103.fit", "strava-104.fit"])
        with open(os.path.join(acts, "strava-104.fit"), "rb") as f:
            self.assertEqual(f.read(), self.files[104][0])
        st = sw.load_state(self.home)
        self.assertEqual(st["no_file"], [105])
        self.assertTrue(st["last_sync"])
        # second run: nothing new, one list request, no downloads
        self.fake.seen.clear()
        self.assertEqual(self.sync()["new"], 0)
        self.assertEqual([p for p, _, _ in self.fake.seen], ["/athlete/training_activities"])

    def test_page_with_nothing_new_stops_paging(self):
        self.known(105, 104, 103, 102, 101)
        self.assertEqual(self.sync()["new"], 0)
        self.assertEqual(len(self.lists()), 1)
        self.fake.seen.clear()
        self.assertEqual(self.sync(all_pages=True)["new"], 0)
        self.assertEqual(len(self.lists()), 3)

    def test_since_and_dry_run(self):
        r = self.sync(since=(T0 + timedelta(days=3)).strftime("%Y-%m-%d"), dry_run=True)
        self.assertEqual((r["new"], r["downloaded"]), (2, 0))
        self.assertEqual(len(self.lists()), 2)  # page 2 holds an older one: stop there
        self.assertFalse(os.path.exists(os.path.join(self.home, "imports")))
        self.assertTrue(any("Morning run" in x for x in self.logs))
        self.assertNotIn("last_sync", sw.load_state(self.home))

    def test_max_takes_oldest_and_next_run_continues(self):
        r = self.sync(max_new=2)
        self.assertEqual(r["downloaded"], 2)
        self.assertEqual([x[0] for x in index(self.home)[1:]], ["101", "102"])
        r = self.sync(max_new=2)
        self.assertEqual([x[0] for x in index(self.home)[1:]], ["101", "102", "103", "104"])
        self.assertEqual(self.sync(max_new=2), {"new": 1, "downloaded": 0, "no_file": 1, "dry_run": False})
        self.assertEqual(self.sync()["new"], 0)  # 105 has no file: remembered, not retried

    def test_bad_cookie_is_auth_expired_and_flagged(self):
        with self.assertRaises(sw.AuthExpired):
            sw.sync(self.home, self.db, client(self.fake, "expiredsession0123456789"), log=self.logs.append)
        self.assertTrue(sw.status(self.home)["auth_expired"])
        self.assertTrue(self.sync()["new"])
        self.assertFalse(sw.status(self.home)["auth_expired"])

    def test_no_cookie(self):
        with self.assertRaises(sw.AuthExpired):
            client(self.fake, None).list_page(1)
        self.assertEqual(self.fake.seen, [])

    def test_rotated_session_is_saved(self):
        self.fake.rotate_to = "rotatedsession9876543210"
        sw.save_cookie(self.home, GOOD)
        self.known(101, 102, 103, 104)
        self.sync()
        self.assertEqual(sw.load_cookie(self.home), "rotatedsession9876543210")

    def test_headers_and_cookie_never_leave_strava_host(self):
        other = Fake([], {104: self.files[104]})
        self.addCleanup(other.close)
        other.public = True
        self.fake.redirect = {104: other.url + "/activities/104/export_original"}
        self.known(101, 102, 103)
        self.assertEqual(self.sync()["downloaded"], 1)
        lists = [s for s in self.fake.seen if s[0] == "/athlete/training_activities"]
        self.assertTrue(all(c == f"_strava4_session={GOOD}" and x == "XMLHttpRequest" for _, c, x in lists))
        self.assertEqual([(p, c) for p, c, _ in other.seen], [("/activities/104/export_original", None)])

    def test_login_redirect_keeps_cookie_same_host(self):
        c = client(self.fake, "wrongsession0123456789")
        with self.assertRaises(sw.AuthExpired):
            c.download(101)
        self.assertEqual([p for p, _, _ in self.fake.seen], ["/activities/101/export_original", "/login"])

    def test_429_backs_off_then_works(self):
        c, calls, sleeps = client(self.fake), [], []
        c.sleep = sleeps.append
        real = c.opener.open

        def flaky(req, timeout):
            calls.append(req.full_url)
            if len(calls) == 1:
                raise urllib.error.HTTPError(req.full_url, 429, "slow down", {"Retry-After": "7"}, None)
            return real(req, timeout=timeout)

        c.opener.open = flaky
        self.assertEqual(len(c.list_page(1)[0]), 2)
        self.assertIn(7, sleeps)


def run(home, *args, stdin=None, base=None):
    env = dict(os.environ, ROLLBOOK_HOME=home, PYTHONIOENCODING="utf-8")
    if base:
        env["ROLLBOOK_STRAVA_BASE"] = base
    return subprocess.run([sys.executable, TOOL, *args], env=env, input=stdin, capture_output=True,
                          text=True, timeout=120)


class Cli(unittest.TestCase):
    def test_auth_sync_build_then_export_replaces(self):
        home = tmpdir(self)
        d = timedelta(days=1)
        fake = Fake([model(9002, "Sunday skate", "Inline Skate", T0 + d), model(9001, "Saturday ride", "Ride", T0)],
                    {9001: (fit_bytes(self, T0), "Outdoor_cycle.fit"),
                     9002: (gzip.compress(fit_bytes(self, T0 + d)), "x.fit.gz")})
        self.addCleanup(fake.close)
        r = run(home, "strava", "sync", base=fake.url)
        self.assertEqual(r.returncode, sw.EXIT_AUTH, r.stderr)
        r = run(home, "strava", "auth", stdin="not a cookie\n", base=fake.url)
        self.assertNotEqual(r.returncode, 0)
        r = run(home, "strava", "auth", stdin="Cookie: _strava4_session=badsession0123456789\n", base=fake.url)
        self.assertEqual(r.returncode, sw.EXIT_AUTH, r.stderr)
        self.assertIsNone(sw.load_cookie(home))
        r = run(home, "strava", "auth", stdin=f"Cookie: a=b; _strava4_session={GOOD}; c=d\n", base=fake.url)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("2 activities on Strava", r.stdout)
        self.assertNotIn(GOOD, r.stdout + r.stderr)
        self.assertEqual(sw.load_cookie(home), GOOD)

        r = run(home, "strava", "sync", "--delay", "0", base=fake.url)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertNotIn(GOOD, r.stdout + r.stderr)
        db = os.path.join(home, "rollbook.db")
        con = sqlite3.connect(db)
        self.addCleanup(con.close)
        got = con.execute("SELECT id, name, type, filename, best10min_kmh IS NOT NULL FROM activities ORDER BY id").fetchall()
        self.assertEqual(got, [(9001, "Saturday ride", "Ride", "activities/strava-9001.fit", 1),
                               (9002, "Sunday skate", "Inline Skate", "activities/strava-9002.fit.gz", 1)])
        con.close()
        r = run(home, "strava", "sync", "--delay", "0", base=fake.url)
        self.assertIn("0 new on Strava", r.stdout)

        # the next full export has the same activity, with Strava's summary: it wins...
        exp = os.path.join(home, "export")
        os.makedirs(os.path.join(exp, "activities"))
        write_fit(os.path.join(exp, "activities", "9001.fit"), line_points(n=600, t0=T0))
        with open(os.path.join(exp, "activities.csv"), "w", newline="", encoding="utf-8") as f:
            csv.writer(f).writerows([["Activity ID", "Activity Name", "Activity Type", "Filename", "Distance"],
                                     ["9001", "Saturday ride", "Ride", "activities/9001.fit", "3594.0"]])
        r = run(home, "db", "build", exp)
        self.assertEqual(r.returncode, 0, r.stderr)
        q = "SELECT filename, s_distance_m FROM activities WHERE id=9001"
        con = sqlite3.connect(db)
        self.assertEqual(con.execute(q).fetchall(), [("activities/9001.fit", 3594.0)])
        con.close()
        # ...and rebuilding data/imports (any later sync or upload) doesn't bring the bare copy back
        r = run(home, "db", "build", os.path.join(home, "imports"))
        self.assertIn("activities/strava-9001.fit: skipped, already imported from an export as activities/9001.fit",
                      r.stderr)
        con = sqlite3.connect(db)
        self.assertEqual(con.execute(q).fetchall(), [("activities/9001.fit", 3594.0)])
        self.assertEqual(con.execute("SELECT COUNT(*) FROM activities").fetchone(), (2,))
        con.close()

    def test_status_offline(self):
        home = tmpdir(self)
        r = run(home, "strava", "status", "--offline")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("none, run: rollbook strava auth", r.stdout)


class ImporterJob(unittest.TestCase):
    def setUp(self):
        self.home = tmpdir(self)
        self.imp = importer.Importer(os.path.join(self.home, "rollbook.db"), mock.Mock())

    def go(self, rc, log, ids):
        calls = []

        def cmd(args, phase):
            calls.append(list(args))
            self.imp.job["phase"] = phase
            if args[0] == "strava":
                self.imp.job["log"] += log
                return rc
            return 0

        with mock.patch.object(self.imp, "_cmd", side_effect=cmd), \
                mock.patch.object(self.imp, "_ids", side_effect=ids):
            self.assertEqual(self.imp.sync_strava()[0], 200)
            end = time.monotonic() + 10
            while self.imp.status()["state"] == "running" and time.monotonic() < end:
                time.sleep(0.01)
        return calls, self.imp.status()

    def test_downloads_then_builds(self):
        calls, st = self.go(0, ["  1/1  x", "downloaded 1, no file 0"], [{1}, {1, 2}])
        self.assertEqual(calls, [["strava", "sync", "--no-build"], ["db", "build", self.imp.imports],
                                 ["db", "enrich", "--missing"]])
        self.assertEqual((st["state"], st["fetched"], st["added"], st["source"]), ("done", 1, 1, "strava"))

    def test_nothing_new_no_build(self):
        calls, st = self.go(0, ["0 new on Strava", "downloaded 0, no file 0"], [{1}, {1}])
        self.assertEqual(calls, [["strava", "sync", "--no-build"]])
        self.assertEqual((st["state"], st["added"]), ("done", 0))

    def test_auth_expired_message(self):
        calls, st = self.go(sw.EXIT_AUTH, ["error: Strava session expired"], [{1}, {1}])
        self.assertEqual((st["state"], st["auth"]), ("error", True))
        self.assertIn("rollbook strava auth", st["error"])

    def test_other_error_reported(self):
        _, st = self.go(sw.EXIT_NET, ["error: can't reach Strava: timed out"], [{1}, {1}])
        self.assertEqual(st["error"], "can't reach Strava: timed out")

    def test_busy(self):
        self.imp.job = {"state": "running"}
        self.assertEqual(self.imp.sync_strava()[0], 409)


class Api(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = tempfile.mkdtemp(prefix="rollbook-test-strava-")
        cls.home = os.path.join(cls.root, "data")
        os.makedirs(cls.home)
        exp = os.path.join(cls.home, "imports")
        os.makedirs(os.path.join(exp, "activities"))
        write_fit(os.path.join(exp, "activities", "a.fit"), line_points(n=300, t0=T0))
        with open(os.path.join(exp, "activities.csv"), "w", newline="", encoding="utf-8") as f:
            csv.writer(f).writerows([sw.CSV_HEAD, ["", "", "a", "Run", "activities/a.fit"]])
        r = run(cls.home, "db", "build", exp)
        assert r.returncode == 0, r.stderr
        cls.httpd, cls.M, cls.url = server.start(os.path.join(cls.home, "rollbook.db"),
                                                 os.path.join(cls.home, "cache", "web_metrics.json"), 0, offline=True)
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        shutil.rmtree(cls.root, True)

    def setUp(self):
        server.IMPORTER.offline = False  # started offline: no tiles / weather from tests

    def tearDown(self):
        sw.forget_cookie(self.home)
        server.IMPORTER.offline = True

    def req(self, path, method="GET", headers=None):
        r = urllib.request.Request(self.url.rstrip("/") + path, data=b"" if method == "POST" else None,
                                   method=method, headers=headers or {})
        try:
            with urllib.request.urlopen(r, timeout=30) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as e:
            return e.code, e.read()

    def test_status_never_shows_cookie(self):
        self.assertEqual(self.req("/api/strava")[1]["configured"], False)
        sw.save_cookie(self.home, GOOD)
        code, body = self.req("/api/strava")
        self.assertEqual((code, body["configured"], body["offline"]), (200, True, False))
        self.assertNotIn(GOOD, json.dumps(body))

    def test_sync_post_guarded(self):
        sw.save_cookie(self.home, GOOD)
        port = self.httpd.server_address[1]
        for h in ({}, {"X-Rollbook": "1", "Origin": "https://evil.example"}, {"X-Rollbook": "1", "Host": "evil.example"}):
            self.assertEqual(self.req("/api/strava/sync", "POST", h)[0], 403, h)
        with mock.patch.object(server.IMPORTER, "sync_strava", return_value=(200, {"state": "running"})) as s:
            code, body = self.req("/api/strava/sync", "POST", {"X-Rollbook": "1", "Origin": f"http://127.0.0.1:{port}"})
            self.assertEqual((code, body), (200, {"state": "running"}))
            s.assert_called_once_with()
            sw.forget_cookie(self.home)
            code, body = self.req("/api/strava/sync", "POST", {"X-Rollbook": "1"})
            self.assertEqual(code, 400)
            self.assertIn(b"rollbook strava auth", body)
            sw.save_cookie(self.home, GOOD)
            server.IMPORTER.offline = True
            self.assertEqual(self.req("/api/strava/sync", "POST", {"X-Rollbook": "1"})[0], 400)
            self.assertEqual(s.call_count, 1)


if __name__ == "__main__":
    unittest.main()
