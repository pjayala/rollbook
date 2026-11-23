# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Pedro Ayala. Rollbook is free software, see LICENSE.
"""intervals.icu sync (web/intervals_web.py, `rollbook intervals`, POST
/api/intervals/sync), the synthetic-odometer fallback and the build's source
ranking, against a local fake of intervals.icu. No network."""

import base64
import csv
import json
import math
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
from datetime import date, datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

from helpers import TOOL, load_tool, tmpdir, write_fit

import api
import importer
import intervals_web as iv
import server
import strava_web as sw

KEY = "test-key-not-a-real-one"
T0 = datetime(2026, 10, 20, 8, 0, tzinfo=timezone.utc)
TODAY = date(2026, 11, 23)


def skate_points(t0, move_s=300, stop_s=120, speed=6.0, lat0=40.0, lon0=-3.5):
    """Move east, stop, move east again: 1 Hz, so the GPS distance is
    2 * move_s * speed and the stop is a real pause."""
    pts, x = [], 0.0
    for i in range(2 * move_s + stop_s):
        if i and not (move_s <= i < move_s + stop_s):
            x += speed
        pts.append((t0 + timedelta(seconds=i), lat0, lon0 + x / (111320 * math.cos(math.radians(lat0))), 650.0, 150))
    return pts


def fit_bytes(test, pts, linear=False):
    """A watch file, or intervals.icu's rebuild of it (linear=True: the total
    distance spread evenly over the elapsed time)."""
    p = os.path.join(tmpdir(test), "x.fit")
    dist = None
    if linear:
        total = 6.0 * (len(pts) - 120)
        dist = [total * i / (len(pts) - 1) for i in range(len(pts))]
    write_fit(p, pts, distance=dist)
    with open(p, "rb") as f:
        return f.read()


def item(num, t0, km, typ="Ride", name="Morning Ride", source="HUAWEI", strava_id=None):
    """Shaped like an /athlete/0/activities entry (the fields we read, plus noise)."""
    return {"id": f"i{num}", "start_date": t0.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "start_date_local": (t0 + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%S"),
            "type": typ, "name": name, "distance": km * 1000, "source": source, "strava_id": strava_id,
            "icu_training_load": 99}


class Fake:
    def __init__(self, acts, files):
        self.acts, self.files = acts, files
        self.keys = {KEY}
        self.seen = []  # (path, query, Authorization)
        self.fail = {}  # path -> status
        fake = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                u = urllib.parse.urlsplit(self.path)
                auth = self.headers.get("Authorization")
                fake.seen.append((u.path, u.query, auth))
                if u.path in fake.fail:
                    return self.reply(fake.fail[u.path], b"{}")
                ok = auth and auth.startswith("Basic ") and base64.b64decode(auth[6:]).decode() in {
                    f"API_KEY:{k}" for k in fake.keys}
                if not ok:
                    return self.reply(401, b'{"status":401}')
                if u.path == "/api/v1/athlete/0":
                    return self.reply(200, json.dumps({"id": "i787094", "name": "tester"}).encode())
                if u.path == "/api/v1/athlete/0/activities":
                    q = urllib.parse.parse_qs(u.query)
                    lo, hi = q["oldest"][0], q["newest"][0]
                    sl = [a for a in fake.acts if lo <= a["start_date"][:10] <= hi]
                    return self.reply(200, json.dumps(sl).encode())
                m = re.fullmatch(r"/api/v1/activity/i(\d+)/file", u.path)
                if m and int(m.group(1)) in fake.files:
                    return self.reply(200, fake.files[int(m.group(1))], "application/octet-stream")
                self.reply(404, b'{"status":404}')

            def reply(self, code, body, ctype="application/json"):
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


def client(fake, key=KEY):
    return iv.Client(key, base=fake.url, delay=0, sleep=lambda s: None)


def index(home):
    with open(os.path.join(home, "imports", "activities.csv"), newline="", encoding="utf-8") as f:
        return list(csv.reader(f))


def write_import(home, name, data, row):
    acts = os.path.join(home, "imports", "activities")
    os.makedirs(acts, exist_ok=True)
    with open(os.path.join(acts, name), "wb") as f:
        f.write(data)
    sw._append_row(os.path.join(home, "imports"), row + [f"activities/{name}"])


class Parsing(unittest.TestCase):
    def test_parse_key(self):
        for text in (KEY, f"  {KEY}\n", f"API_KEY:{KEY}", f"API_KEY: {KEY}"):
            self.assertEqual(iv.parse_key(text), KEY, text)
        for bad in ("", "short", "has spaces in it, not a key", "a;b=c" * 5, "x" * 129):
            with self.assertRaises(ValueError):
                iv.parse_key(bad)

    def test_parse_list(self):
        body = json.dumps([
            item(5, T0, 23.45, strava_id="21031709592"),
            {"id": "i6", "start_date": "2026-11-22T09:21:31+01:00", "type": "VirtualRide", "name": None,
             "distance": True, "source": "zwift"},
            {"id": "i7", "start_date_local": "2026-11-22T10:21:31", "type": 3},
            {"id": "event-9", "start_date": "2026-11-22T09:21:31Z"}, "junk", None,
        ]).encode()
        a, b, c = iv.parse_list(body)
        self.assertEqual((a["num"], a["start"], a["km"], a["strava_id"], a["source"]),
                         (5, T0, 23.45, 21031709592, "HUAWEI"))
        self.assertEqual((b["start"], b["km"], b["name"], b["type"], b["source"]),
                         (datetime(2026, 11, 22, 8, 21, 31, tzinfo=timezone.utc), None, "", "VirtualRide", "ZWIFT"))
        self.assertEqual((c["start"], c["type"]), (None, ""))  # a local time can't be placed: no time match
        for bad in (b"{}", b"<html>", b'{"a": 1}'):
            with self.assertRaises(iv.FormatChanged):
                iv.parse_list(bad)

    def test_type_label_and_sport_for(self):
        self.assertEqual(iv.type_label("VirtualRide"), "Virtual Ride")
        a = {"type": "Ride"}
        self.assertEqual(iv.sport_for(a, {}, {}), "Ride")
        self.assertEqual(iv.sport_for(a, {}, {"Ride": "Inline Skate"}), "Inline Skate")
        self.assertEqual(iv.sport_for(a, {"Ride": "Ride"}, {"Ride": "Inline Skate"}), "Ride")  # the user's wins
        self.assertEqual(iv.sport_for({"type": ""}, {}, {}), "Workout")

    def test_window(self):
        self.assertEqual(iv.window(TODAY), ("2025-11-23", "2026-11-24"))
        self.assertEqual(iv.window(TODAY, since="2026-11-01"), ("2026-11-01", "2026-11-24"))
        self.assertEqual(iv.window(TODAY, all_=True), ("1990-01-01", "2026-11-24"))

    def test_base_override_only_loopback(self):
        for val, want in (("http://127.0.0.1:9", "http://127.0.0.1:9"), ("https://evil.example", iv.BASE),
                          ("http://127.0.0.1.evil:9", iv.BASE)):
            code = "import sys; sys.path.insert(0, 'web'); import intervals_web; print(intervals_web.BASE)"
            r = subprocess.run([sys.executable, "-c", code], env=dict(os.environ, ROLLBOOK_INTERVALS_BASE=val),
                               capture_output=True, text=True, cwd=os.path.dirname(os.path.dirname(TOOL)))
            self.assertEqual(r.stdout.strip(), want if val.startswith("http://127") else "https://intervals.icu")


class Odometer(unittest.TestCase):
    """intervals.icu's rebuilt HUAWEI files: the distance channel is the total
    spread evenly, so the GPS track has to give distance and pauses."""

    @classmethod
    def setUpClass(cls):
        cls.rb = load_tool()

    def test_linear_detected_real_not(self):
        t = list(range(0, 600)) + list(range(605, 900, 5))
        lin = [5.97 * x for x in t]
        self.assertTrue(self.rb.linear_odometer(t, lin))
        real, d = [], 0.0  # a real odometer: speed varies (+-1 m/s around 6, a stop)
        for i, x in enumerate(t):
            d += 0 if 300 <= x < 360 else (6 + math.sin(i / 20)) * (x - t[i - 1] if i else 0)
            real.append(d)
        self.assertFalse(self.rb.linear_odometer(t, real))
        odd = list(lin)
        odd[1] += 40  # one odd step (the first record) in a rebuilt file: still made up
        odd[2:] = [v + 40 for v in odd[2:]]
        self.assertTrue(self.rb.linear_odometer(t, odd))
        self.assertFalse(self.rb.linear_odometer(t, [0.4 * x for x in t]))  # < 0.5 m/s: left alone
        self.assertTrue(self.rb.linear_odometer(t, lin[:400]))  # channels of unequal length
        self.assertFalse(self.rb.linear_odometer(t[:50], lin[:50]))  # too short to tell
        self.assertFalse(self.rb.linear_odometer(t, [0.0] * len(t)))  # stuck: the other rule
        self.assertFalse(self.rb.linear_odometer(t, [None] * len(t)))

    def test_file_gets_gps_distance_and_pauses(self):
        pts = skate_points(T0)
        d = tmpdir(self)
        for name, linear in (("watch.fit", False), ("rebuilt.fit", True)):
            with open(os.path.join(d, name), "wb") as f:
                f.write(fit_bytes(self, pts, linear))
        _, s_lin = self.rb.load_file(os.path.join(d, "rebuilt.fit"))
        c = self.rb.core(s_lin)
        # by hand: 2 x 300 s at 6 m/s = 3.6 km, a 120 s stop
        self.assertAlmostEqual(c["km"], 3.6, delta=0.02)
        self.assertGreaterEqual(c["pause"], 115)
        _, s_w = self.rb.load_file(os.path.join(d, "watch.fit"))
        self.assertEqual(self.rb.core(s_w)["pause"] > 100, True)


class Classify(unittest.TestCase):
    def test_rules(self):
        S = [(111, T0, 10.0, "Inline Skate"),
             (iv.ID_BASE + 3, T0 + timedelta(days=3), 5.0, "Inline Skate"),
             (21031709592, T0 + timedelta(days=5), 18.0, "Inline Skate")]
        acts = [
            {**iv.parse_list(json.dumps([item(1, T0 + timedelta(seconds=90), 10.3)]).encode())[0]},  # time + km
            {**iv.parse_list(json.dumps([item(2, T0 + timedelta(seconds=90), 12.0)]).encode())[0]},  # km 20 % off
            {**iv.parse_list(json.dumps([item(3, T0 + timedelta(days=3), 5)]).encode())[0]},  # by id
            {**iv.parse_list(json.dumps([item(4, T0 + timedelta(days=4), 5)]).encode())[0]},  # stored file
            {**iv.parse_list(json.dumps([item(5, T0 + timedelta(days=9), 5)]).encode())[0]},  # no file before
            {**iv.parse_list(json.dumps([item(6, T0 + timedelta(days=6), 18, strava_id=21031709592)]).encode())[0]},
            {**iv.parse_list(json.dumps([item(7, T0 + timedelta(days=7), 8, source="STRAVA")]).encode())[0]},
            {**iv.parse_list(json.dumps([item(8, T0 + timedelta(seconds=125), 10.0)]).encode())[0]},  # 125 s off
        ]
        new, pairs, n = iv.classify(acts, S, stored={4}, no_file={5})
        self.assertEqual([a["num"] for a in new], [2, 8])
        self.assertEqual([(a["num"], s) for a, s in pairs], [(1, "Inline Skate")])
        # 6 is in Rollbook under its Strava id (matched); 7 came from Strava, Rollbook doesn't have it
        self.assertEqual(dict(n), {"matched": 2, "stored": 2, "no_file": 1, "strava": 1})

    def test_match_across_midnight(self):
        S = [(1, datetime(2026, 11, 17, 23, 59, 30, tzinfo=timezone.utc), 7.0, "Inline Skate")]
        a = iv.parse_list(json.dumps([item(9, datetime(2026, 11, 18, 0, 0, 40, tzinfo=timezone.utc), 7.0)]).encode())
        self.assertEqual(len(iv.classify(a, S, set(), set())[1]), 1)

    def test_same_start_matches_whatever_the_distance(self):
        # HUAWEI's total vs Strava's GPS figure: 14.40 vs 13.66 km (5.4 %) on one real skate
        S = [(1, T0, 13.66, "Inline Skate")]
        same = iv.parse_list(json.dumps([item(9, T0, 14.40)]).encode())
        self.assertEqual(len(iv.classify(same, S, set(), set())[1]), 1)
        later = iv.parse_list(json.dumps([item(9, T0 + timedelta(seconds=30), 14.40)]).encode())
        self.assertEqual(len(iv.classify(later, S, set(), set())[0]), 1)

    def test_learn(self):
        R = {"type": "Ride"}
        W = {"type": "Walk"}
        pairs = [(R, "Inline Skate")] * 3 + [(R, "Ride")] + [(W, "Walk")] * 2
        self.assertEqual(iv.learn_sports(pairs), {"Ride": "Inline Skate"})  # 3 of 4; Walk has only 2
        self.assertEqual(iv.learn_sports([(R, "Inline Skate")] * 3 + [(R, "Ride")] * 3), {})  # 50 %: no majority


class Sync(unittest.TestCase):
    def setUp(self):
        self.home = tmpdir(self)
        self.db = os.path.join(self.home, "rollbook.db")
        self.files = {n: fit_bytes(self, skate_points(T0 + timedelta(days=n)), linear=True) for n in (1, 2, 3)}
        self.fake = Fake([item(n, T0 + timedelta(days=n), 3.6) for n in (1, 2, 3)] +
                         [item(4, T0 + timedelta(days=4), 0, typ="WeightTraining", name="Gym")], self.files)
        self.addCleanup(self.fake.close)
        self.logs = []

    def sync(self, **kw):
        return iv.sync(self.home, self.db, client(self.fake), today=TODAY, log=self.logs.append, **kw)

    def test_downloads_new_oldest_first_and_remembers_no_file(self):
        r = self.sync()
        self.assertEqual((r["new"], r["downloaded"], r["no_file"]), (4, 3, 1))
        rows = index(self.home)
        self.assertEqual(rows[0], sw.CSV_HEAD)
        self.assertEqual([x[0] for x in rows[1:]], [str(iv.ID_BASE + n) for n in (1, 2, 3)])
        self.assertEqual(rows[1][1:], [(T0 + timedelta(days=1)).isoformat(), "Morning Ride", "Ride",
                                       "activities/intervals-1.fit"])
        self.assertEqual(sorted(os.listdir(os.path.join(self.home, "imports", "activities"))),
                         ["intervals-1.fit", "intervals-2.fit", "intervals-3.fit"])
        self.assertEqual(iv.load_state(self.home)["no_file"], [4])
        self.fake.seen.clear()
        r = self.sync()  # stored files and the no-file one are known: nothing downloaded
        self.assertEqual((r["new"], r["downloaded"]), (0, 0))
        self.assertFalse([p for p, _, _ in self.fake.seen if p.endswith("/file")])

    def test_window_query(self):
        self.sync(since="2026-10-22")
        q = urllib.parse.parse_qs(next(q for p, q, _ in self.fake.seen if p.endswith("/activities")))
        self.assertEqual((q["oldest"], q["newest"]), (["2026-10-22"], ["2026-11-24"]))
        self.assertEqual(self.sync(since="2026-10-22")["downloaded"], 0)  # 2, 3 stored now; 1 is older

    def test_dry_run_and_max(self):
        r = self.sync(dry_run=True)
        self.assertEqual((r["new"], r["downloaded"]), (4, 0))
        self.assertFalse(os.path.exists(os.path.join(self.home, "imports")))
        self.assertEqual(self.sync(max_new=2)["downloaded"], 2)
        self.assertEqual([x[4] for x in index(self.home)[1:]], ["activities/intervals-1.fit", "activities/intervals-2.fit"])

    def test_sport_map_remembered_and_forgotten(self):
        self.sync(dry_run=True, sports={"Ride": "Inline Skate"})
        self.assertEqual(iv.status(self.home)["sports"], {"Ride": "Inline Skate"})
        self.sync(max_new=1)
        self.assertEqual(index(self.home)[1][3], "Inline Skate")
        self.sync(dry_run=True, sports={"Ride": ""})
        self.assertEqual(iv.status(self.home)["sports"], {})

    def test_bad_key_is_auth_expired_and_flagged(self):
        with self.assertRaises(iv.AuthExpired):
            iv.sync(self.home, self.db, client(self.fake, "wrongkey0123456789abc"), today=TODAY, log=self.logs.append)
        self.assertTrue(iv.status(self.home)["auth_expired"])
        self.sync()
        self.assertFalse(iv.status(self.home)["auth_expired"])

    def test_no_key_no_request(self):
        with self.assertRaises(iv.AuthExpired):
            client(self.fake, None).athlete()
        self.assertEqual(self.fake.seen, [])

    def test_server_errors(self):
        self.fake.fail["/api/v1/athlete/0/activities"] = 500
        c = iv.Client(KEY, base=self.fake.url, delay=0, sleep=lambda s: None)
        with self.assertRaises(iv.SyncError):
            c.activities("2026-01-01", "2026-12-01")
        self.assertEqual(len(self.fake.seen), 5)  # retried, then gave up
        self.fake.fail["/api/v1/athlete/0/activities"] = 400
        with self.assertRaises(iv.SyncError):
            c.activities("2026-01-01", "2026-12-01")

    def test_key_only_to_its_host(self):
        other = Fake([], {})
        self.addCleanup(other.close)

        class Bounce(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                self.send_response(302)
                self.send_header("Location", other.url + "/api/v1/athlete/0")
                self.send_header("Content-Length", "0")
                self.end_headers()

        h = ThreadingHTTPServer(("127.0.0.1", 0), Bounce)
        threading.Thread(target=h.serve_forever, daemon=True).start()
        self.addCleanup(lambda: (h.shutdown(), h.server_close()))
        with self.assertRaises(iv.AuthExpired):  # the other host never sees the key, so it says 401
            iv.Client(KEY, base=f"http://127.0.0.1:{h.server_address[1]}", delay=0).athlete()
        self.assertEqual([(p, a) for p, _, a in other.seen], [("/api/v1/athlete/0", None)])

    def test_key_file_private_and_status_never_reads_it(self):
        iv.save_key(self.home, KEY)
        if os.name == "posix":
            self.assertEqual(os.stat(iv.key_path(self.home)).st_mode & 0o777, 0o600)
        with mock.patch("builtins.open", side_effect=AssertionError("opened")):
            st = iv.status(self.home)
        self.assertTrue(st["configured"])
        self.assertNotIn(KEY, json.dumps(st))
        self.assertTrue(iv.forget_key(self.home))
        self.assertFalse(iv.status(self.home)["configured"])

    def test_bad_state_file(self):
        bad = json.dumps({"no_file": ["x", True, 5, -1, 10 ** 13], "sport_map": {"Ride": 3, "Walk": "Walk", "": "x"},
                          "learned": "nope", "last_result": {"a": {"nested": 1}, "b": "x" * 300, "c": 2}}).encode()
        for body, want in ((b"[1]", ([], {})), (b"not json", ([], {})), (bad, ([5], {"Walk": "Walk"})),
                           (json.dumps({"v": 99, "no_file": [5]}).encode(), ([], {}))):  # another version: dropped
            with open(iv.state_path(self.home), "wb") as f:
                f.write(body)
            s = iv.load_state(self.home)
            self.assertEqual((s["no_file"], s["sport_map"], s["learned"]), (*want, {}), body[:20])
        st = iv.status(self.home)
        self.assertEqual(st["last_result"], None)  # the v99 file was dropped whole
        with open(iv.state_path(self.home), "wb") as f:
            f.write(bad)
        self.assertEqual(iv.status(self.home)["last_result"], {"c": 2})  # nested / long values left out

    def test_failure_after_success_keeps_what_was_stored(self):
        self.fake.fail["/api/v1/activity/i3/file"] = 400
        with self.assertRaises(iv.SyncError):
            self.sync()
        self.assertEqual([x[4] for x in index(self.home)[1:]], ["activities/intervals-1.fit", "activities/intervals-2.fit"])
        self.assertEqual(iv.load_state(self.home)["no_file"], [])  # 4 was never reached
        del self.fake.fail["/api/v1/activity/i3/file"]
        self.fake.seen.clear()
        r = self.sync()
        self.assertEqual((r["downloaded"], r["no_file"]), (1, 1))
        self.assertEqual([p for p, _, _ in self.fake.seen if p.endswith("/file")],
                         ["/api/v1/activity/i3/file", "/api/v1/activity/i4/file"])

    def test_non_activity_200_is_retried_next_run(self):
        self.files[2] = b"<!DOCTYPE html><html>maintenance</html>"
        r = self.sync()
        self.assertEqual((r["downloaded"], r["skipped"], r["no_file"]), (2, 1, 1))
        self.assertNotIn(2, iv.load_state(self.home)["no_file"])
        self.files[2] = fit_bytes(self, skate_points(T0 + timedelta(days=2)), linear=True)
        self.assertEqual(self.sync()["downloaded"], 1)

    def test_time_budget_cuts_short_and_next_run_continues(self):
        ticks = iter([0, 0, 10, 10 ** 6])  # start, before file 1, before file 2, before file 3: over
        r = iv.sync(self.home, self.db, client(self.fake), today=TODAY, log=self.logs.append,
                    clock=lambda: next(ticks), budget_s=60)
        self.assertEqual((r["downloaded"], r.get("cut_short")), (2, True))
        self.assertIn("time budget used after 2 of 4", "\n".join(self.logs))
        r = self.sync()
        self.assertEqual((r["downloaded"], r["no_file"], r.get("cut_short")), (1, 1, None))

    def test_unreadable_db_downloads_nothing(self):
        with open(self.db, "wb") as f:
            f.write(b"this is not a sqlite database" * 100)
        with self.assertRaises(iv.SyncError):
            self.sync()
        self.assertFalse(os.path.exists(os.path.join(self.home, "imports")))
        os.remove(self.db)
        sqlite3.connect(self.db).close()  # a DB without the activities table: an aborted first build
        self.assertEqual(iv.db_sessions(self.db, self.home), [])

    def test_deleted_session_without_row_never_comes_back(self):
        # a deleted upload after `db build --fresh`: the DB row is gone, its edit keeps start / km
        sqlite3.connect(self.db).close()
        with open(os.path.join(self.home, "edits.json"), "w", encoding="utf-8") as f:
            json.dump({"sessions": {"123456": {"deleted": True, "hidden": True, "file_removed": True,
                                               "start": (T0 + timedelta(days=2, seconds=3)).isoformat(), "km": 3.4}}}, f)
        r = self.sync()
        self.assertEqual((r["new"], r["matched"]), (3, 1))
        self.assertNotIn("activities/intervals-2.fit", [x[4] for x in index(self.home)[1:]])

    def test_hostile_upstream_strings_and_dates(self):
        evil = "Evil\x1b[31m\nerror: fake line\r\u2028<b>"
        acts = iv.parse_list(json.dumps([
            {**item(1, T0, 3.6), "name": evil, "type": "Ri\nde"},
            {**item(2, T0, 3.6), "start_date": "0001-01-01T00:30:00+01:00"},
            {**item(3, T0, 3.6), "start_date": "9999-12-31T23:59:59-01:00"},
        ]).encode())
        self.assertEqual((acts[0]["name"], acts[0]["type"]), ("Evil [31m error: fake line  <b>", "Ri de"))
        self.assertEqual([x["start"] for x in acts[1:]], [None, None])
        S = [(1, datetime(1990, 1, 1, tzinfo=timezone.utc), 1.0, "Run")]
        self.assertEqual(len(iv.classify(acts, S, set(), set())[0]), 3)  # no crash, nothing matched
        nested = b"[" * 100000 + b"]" * 100000  # never a crash: a RecursionError (older Pythons) is FormatChanged
        try:
            self.assertEqual(iv.parse_list(nested), [])
        except iv.FormatChanged:
            pass

    def test_retry_after(self):
        H = lambda v: {"Retry-After": v}  # noqa: E731
        self.assertEqual([iv.retry_after(H("7"), 0), iv.retry_after(H("9999"), 0), iv.retry_after(H("\u00b2"), 0),
                          iv.retry_after(H(""), 1), iv.retry_after(None, 3)], [7, 120, 30, 60, 120])


def run(home, *args, stdin=None, base=None):
    env = dict(os.environ, ROLLBOOK_HOME=home)
    if base:
        env["ROLLBOOK_INTERVALS_BASE"] = base
    return subprocess.run([sys.executable, TOOL, *args], env=env, input=stdin, capture_output=True,
                          text=True, timeout=120)


def q(home, sql):
    db = sqlite3.connect(os.path.join(home, "rollbook.db"))
    try:
        return db.execute(sql).fetchall()
    finally:
        db.close()


class Cli(unittest.TestCase):
    def test_overlap_learns_sport_strava_copy_wins_later(self):
        home = tmpdir(self)
        # three skates already in Rollbook from Strava, as the watch recorded them
        for n in (1, 2, 3):
            write_import(home, f"strava-{100 + n}.fit", fit_bytes(self, skate_points(T0 + timedelta(days=n))),
                         [str(100 + n), "", "Outdoor cycle", "Inline Skate"])
        self.assertEqual(run(home, "db", "build", os.path.join(home, "imports"), "--no-weather").returncode, 0)
        race = T0 + timedelta(days=4)
        files = {n: fit_bytes(self, skate_points(T0 + timedelta(days=n)), linear=True) for n in (1, 2, 3, 4)}
        fake = Fake([item(n, T0 + timedelta(days=n), 3.6) for n in (1, 2, 3, 4)], files)
        self.addCleanup(fake.close)

        r = run(home, "intervals", "sync", base=fake.url)
        self.assertEqual(r.returncode, 3, r.stdout + r.stderr)
        self.assertIn("rollbook intervals auth", r.stderr)
        r = run(home, "intervals", "auth", stdin="not a key\n", base=fake.url)
        self.assertNotEqual(r.returncode, 0)
        r = run(home, "intervals", "auth", stdin="wrongkey0123456789abc\n", base=fake.url)
        self.assertEqual(r.returncode, 3)
        r = run(home, "intervals", "auth", stdin=f"API_KEY:{KEY}\n", base=fake.url)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("i787094", r.stdout)
        self.assertNotIn(KEY, r.stdout + r.stderr)

        r = run(home, "intervals", "sync", "--delay", "0", base=fake.url)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("1 new, 3 already in Rollbook", r.stdout)
        self.assertIn("Ride -> Inline Skate (learned from overlap)", r.stdout)
        self.assertEqual([p for p, _, _ in fake.seen if p.endswith("/file")], ["/api/v1/activity/i4/file"])
        rid = iv.ID_BASE + 4
        (typ, km, pause, fn), = q(home, f"SELECT type, km, pause_s, filename FROM activities WHERE id={rid}")
        self.assertEqual((typ, fn), ("Inline Skate", "activities/intervals-4.fit"))
        self.assertAlmostEqual(km, 3.6, delta=0.02)  # GPS, not the made-up odometer
        self.assertGreaterEqual(pause, 115)

        # again: nothing new
        r = run(home, "intervals", "sync", "--delay", "0", base=fake.url)
        self.assertIn("0 new, 4 already", r.stdout)
        # Strava finally takes the race: its watch file replaces the intervals.icu copy
        write_import(home, "strava-104.fit", fit_bytes(self, skate_points(race)), ["104", "", "Arganda", "Inline Skate"])
        r = run(home, "db", "build", os.path.join(home, "imports"), "--no-weather")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("replaces activities/intervals-4.fit", r.stderr)
        self.assertEqual(q(home, f"SELECT id FROM activities WHERE id IN ({rid}, 104)"), [(104,)])
        # a fresh rebuild keeps Strava's copy (intervals-* sorts first, strava-* replaces it)
        r = run(home, "db", "build", os.path.join(home, "imports"), "--fresh", "--no-weather")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(q(home, "SELECT COUNT(*) FROM activities"), [(4,)])
        self.assertEqual(q(home, f"SELECT COUNT(*) FROM activities WHERE id={rid}"), [(0,)])

    def test_intervals_copy_never_replaces_a_watch_file(self):
        home = tmpdir(self)
        write_import(home, "strava-101.fit", fit_bytes(self, skate_points(T0)), ["101", "", "a", "Inline Skate"])
        self.assertEqual(run(home, "db", "build", os.path.join(home, "imports"), "--no-weather").returncode, 0)
        write_import(home, "intervals-7.fit", fit_bytes(self, skate_points(T0), linear=True),
                     [str(iv.ID_BASE + 7), "", "b", "Inline Skate"])
        r = run(home, "db", "build", os.path.join(home, "imports"), "--no-weather")
        self.assertIn("intervals-7.fit: skipped, same session", r.stderr)
        self.assertEqual(q(home, "SELECT id FROM activities"), [(101,)])

    def test_strava_copy_with_other_km_replaces_and_edits_follow(self):
        """HUAWEI's odometer vs the GPS track differ by up to ~7 %: the watch's
        own start decides (edits.same_session), in the build and for edits."""
        home = tmpdir(self)
        pts = skate_points(T0)
        write_import(home, "intervals-7.fit", fit_bytes(self, pts, linear=True), [str(iv.ID_BASE + 7), "", "b", "Inline Skate"])
        self.assertEqual(run(home, "db", "build", os.path.join(home, "imports"), "--no-weather").returncode, 0)
        rid = iv.ID_BASE + 7
        with open(os.path.join(home, "edits.json"), "w", encoding="utf-8") as f:
            json.dump({"sessions": {str(rid): {"name": "Gandia", "start": T0.isoformat(), "km": 3.6}}}, f)
        # the watch's file: odometer 8 % over the GPS 3.6 km, start 5 s later
        later = [(t + timedelta(seconds=5), *rest) for t, *rest in pts]
        x, odo = 0.0, []
        for i in range(len(later)):
            x += 0 if (i == 0 or 300 <= i < 420) else 6.0 * 1.08
            odo.append(x)
        p = os.path.join(tmpdir(self), "w.fit")
        write_fit(p, later, distance=odo)
        with open(p, "rb") as f:
            write_import(home, "strava-104.fit", f.read(), ["104", "", "Outdoor cycle", "Inline Skate"])
        r = run(home, "db", "build", os.path.join(home, "imports"), "--no-weather")
        self.assertIn("strava-104.fit: replaces activities/intervals-7.fit", r.stderr)
        self.assertEqual(q(home, "SELECT id FROM activities"), [(104,)])
        import edits
        E = edits.Edits(home)
        db = sqlite3.connect(os.path.join(home, "rollbook.db"))
        self.addCleanup(db.close)
        E.sync(db)
        self.assertEqual(db.execute("SELECT name FROM activities").fetchall(), [("Gandia",)])

    def test_sync_args_validated(self):
        home = tmpdir(self)
        iv.save_key(home, KEY)
        for args, msg in ((["--max", "-1"], "--max"), (["--since", "2026-02-31"], "--since")):
            for src in ("intervals", "strava"):
                r = run(home, src, "sync", *args, base="http://127.0.0.1:9")
                self.assertNotEqual(r.returncode, 0, (src, args))
                self.assertIn(msg, r.stderr)

    def test_status_and_logout(self):
        home = tmpdir(self)
        r = run(home, "intervals", "status", "--offline")
        self.assertIn("none, run: rollbook intervals auth", r.stdout)
        r = run(home, "intervals", "auth", "--no-check", stdin=KEY + "\n")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("stored", run(home, "intervals", "status", "--offline").stdout)
        self.assertIn("removed", run(home, "intervals", "logout").stdout)
        self.assertFalse(os.path.exists(iv.key_path(home)))

    def test_sport_flag_validated(self):
        home = tmpdir(self)
        iv.save_key(home, KEY)
        r = run(home, "intervals", "sync", "--sport", "Ride", base="http://127.0.0.1:9")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("TYPE=SPORT", r.stderr)


class ImporterJob(unittest.TestCase):
    def setUp(self):
        self.home = tmpdir(self)
        self.imp = importer.Importer(os.path.join(self.home, "rollbook.db"), mock.Mock())

    def go(self, rc, log, ids):
        calls = []

        def cmd(args, phase, **kw):
            calls.append(list(args))
            self.imp.job["phase"] = phase
            if args[0] == "intervals":
                self.imp.job["log"] += log
                return rc
            return 0

        with mock.patch.object(self.imp, "_cmd", side_effect=cmd), \
                mock.patch.object(self.imp, "_ids", side_effect=ids):
            self.assertEqual(self.imp.sync("intervals")[0], 200)
            end = time.monotonic() + 10
            while self.imp.status()["state"] == "running" and time.monotonic() < end:
                time.sleep(0.01)
        return calls, self.imp.status()

    def test_downloads_then_builds(self):
        calls, st = self.go(0, ["downloaded 1, no file 0"], [{1}, {1, 2}])
        self.assertEqual(calls, [["intervals", "sync", "--no-build"], ["db", "build", self.imp.imports, "--no-weather"],
                                 ["db", "enrich", "--missing"]])
        self.assertEqual((st["state"], st["fetched"], st["source"], st["items"]), ("done", 1, "intervals", ["intervals.icu"]))

    def test_summary_source(self):
        import lastimport
        self.imp.last = lastimport.LastImport(os.path.join(self.home, "last_import.json"))
        self.go(0, ["downloaded 1, no file 0"], [{1}, {1, 2}])
        self.assertEqual(self.imp.last.get()["source"], "intervals")

    def test_auth_message(self):
        _, st = self.go(iv.EXIT_AUTH, ["error: key not accepted"], [{1}, {1}])
        self.assertEqual((st["state"], st["auth"]), ("error", True))
        self.assertIn("rollbook intervals auth", st["error"])


class SyncAll(unittest.TestCase):
    """Importer.sync("all"): every configured source, Strava first; one failing
    doesn't stop the other, the job fails only when all did."""

    def setUp(self):
        self.home = tmpdir(self)
        self.imp = importer.Importer(os.path.join(self.home, "rollbook.db"), mock.Mock())

    def go(self, rcs, ids):
        calls = []

        def cmd(args, phase, **kw):
            calls.append(list(args))
            if args[1] == "sync":
                rc, log = rcs[args[0]]
                self.imp.job["log"] += log
                return rc
            return 0

        with mock.patch.object(self.imp, "_cmd", side_effect=cmd), \
                mock.patch.object(self.imp, "_ids", side_effect=ids):
            code, _ = self.imp.sync("all")
            end = time.monotonic() + 10
            while self.imp.status()["state"] == "running" and time.monotonic() < end:
                time.sleep(0.01)
        return code, calls, self.imp.status()

    def test_nothing_set_up(self):
        self.assertEqual(self.imp.sync("all")[0], 400)

    def test_strava_first_then_intervals(self):
        sw.save_cookie(self.home, "goodsession0123456789abcdef")
        iv.save_key(self.home, KEY)
        code, calls, st = self.go({"strava": (0, ["downloaded 1, no file 0"]), "intervals": (0, ["downloaded 2, no file 0"])},
                                  [{1}, {1, 2, 3, 4}])
        self.assertEqual(code, 200)
        self.assertEqual([c[0] for c in calls[:3]], ["strava", "intervals", "db"])
        self.assertEqual((st["state"], st["fetched"], st["sources"]),
                         ("done", 3, {"strava": {"fetched": 1}, "intervals": {"fetched": 2}}))

    def test_one_fails_other_still_imported(self):
        sw.save_cookie(self.home, "goodsession0123456789abcdef")
        iv.save_key(self.home, KEY)
        _, calls, st = self.go({"strava": (sw.EXIT_AUTH, ["error: expired"]), "intervals": (0, ["downloaded 1, no file 0"])},
                               [{1}, {1, 2}])
        self.assertEqual(st["state"], "done")
        self.assertTrue(st["sources"]["strava"]["auth"])
        self.assertIn("rollbook strava auth", st["sources"]["strava"]["error"])
        self.assertEqual(st["sources"]["intervals"], {"fetched": 1})
        self.assertIn(["db", "build", self.imp.imports, "--no-weather"], calls)

    def test_all_fail_is_an_error(self):
        sw.save_cookie(self.home, "goodsession0123456789abcdef")
        iv.save_key(self.home, KEY)
        _, _, st = self.go({"strava": (4, ["error: can't reach Strava"]), "intervals": (4, ["error: can't reach intervals.icu"])},
                           [{1}, {1}])
        self.assertEqual(st["state"], "error")
        self.assertEqual(st["error"], "can't reach Strava; can't reach intervals.icu")

    def test_expired_source_skipped(self):
        sw.save_cookie(self.home, "goodsession0123456789abcdef")
        sw.save_state(self.home, {"auth_expired": True})
        iv.save_key(self.home, KEY)
        _, calls, st = self.go({"intervals": (0, ["downloaded 0, no file 0"])}, [{1}, {1}])
        self.assertEqual([c[0] for c in calls], ["intervals"])
        self.assertEqual(list(st["sources"]), ["intervals"])
        iv.save_state(self.home, {"auth_expired": True})
        self.assertEqual(self.imp.sync("all")[0], 400)  # both need a new login
        self.assertEqual(self.imp.configured(), [])

    def test_only_configured_and_partial(self):
        iv.save_key(self.home, KEY)
        _, calls, st = self.go({"intervals": (iv.EXIT_PARTIAL, ["downloaded 2, no file 0",
                                                               "error: stopped at the time budget"])}, [{1}, {1, 2}])
        self.assertEqual(calls[0][0], "intervals")
        self.assertEqual((st["state"], st["sources"]["intervals"]), ("done", {"fetched": 2, "partial": True}))


class Api(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = tempfile.mkdtemp(prefix="rollbook-test-intervals-")
        cls.home = os.path.join(cls.root, "data")
        os.makedirs(cls.home)
        os.makedirs(os.path.join(cls.home, "imports", "activities"))
        write_fit(os.path.join(cls.home, "imports", "activities", "a.fit"), skate_points(T0))
        sw._append_row(os.path.join(cls.home, "imports"), ["", "", "a", "Run", "activities/a.fit"])
        r = run(cls.home, "db", "build", os.path.join(cls.home, "imports"), "--no-weather")
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
        api.IMPORTER.offline = False

    def tearDown(self):
        iv.forget_key(self.home)
        api.IMPORTER.offline = True

    def req(self, path, method="GET", headers=None):
        r = urllib.request.Request(self.url.rstrip("/") + path, data=b"" if method == "POST" else None,
                                   method=method, headers=headers or {})
        try:
            with urllib.request.urlopen(r, timeout=30) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as e:
            return e.code, e.read()

    def test_status_never_shows_key(self):
        self.assertEqual(self.req("/api/intervals")[1]["configured"], False)
        iv.save_key(self.home, KEY)
        code, body = self.req("/api/intervals")
        self.assertEqual((code, body["configured"], body["offline"]), (200, True, False))
        self.assertNotIn(KEY, json.dumps(body))

    def test_sync_post_guarded(self):
        iv.save_key(self.home, KEY)
        port = self.httpd.server_address[1]
        for h in ({}, {"X-Rollbook": "1", "Origin": "https://evil.example"}, {"X-Rollbook": "1", "Host": "evil.example"}):
            self.assertEqual(self.req("/api/intervals/sync", "POST", h)[0], 403, h)
        with mock.patch.object(api.IMPORTER, "sync", return_value=(200, {"state": "running"})) as s:
            code, body = self.req("/api/intervals/sync", "POST", {"X-Rollbook": "1", "Origin": f"http://127.0.0.1:{port}"})
            self.assertEqual((code, body), (200, {"state": "running"}))
            s.assert_called_once_with("intervals")
            iv.forget_key(self.home)
            code, body = self.req("/api/intervals/sync", "POST", {"X-Rollbook": "1"})
            self.assertEqual(code, 400)
            self.assertIn(b"rollbook intervals auth", body)
            iv.save_key(self.home, KEY)
            api.IMPORTER.offline = True
            self.assertEqual(self.req("/api/intervals/sync", "POST", {"X-Rollbook": "1"})[0], 400)
            self.assertEqual(s.call_count, 1)

    def test_sync_all_post(self):
        for h in ({}, {"X-Rollbook": "1", "Origin": "https://evil.example"}):
            self.assertEqual(self.req("/api/sync", "POST", h)[0], 403, h)
        code, body = self.req("/api/sync", "POST", {"X-Rollbook": "1"})
        self.assertEqual(code, 400)  # nothing set up
        self.assertIn(b"rollbook intervals auth", body)
        with mock.patch.object(api.IMPORTER, "sync", return_value=(200, {"state": "running"})) as s:
            self.assertEqual(self.req("/api/sync", "POST", {"X-Rollbook": "1"})[0], 200)
            s.assert_called_once_with("all")
            api.IMPORTER.offline = True
            self.assertEqual(self.req("/api/sync", "POST", {"X-Rollbook": "1"})[0], 400)
            self.assertEqual(s.call_count, 1)


if __name__ == "__main__":
    unittest.main()
