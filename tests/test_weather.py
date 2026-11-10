# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Pedro Ayala. Rollbook is free software, see LICENSE.
"""Weather (web/weather.py): Open-Meteo cache and fetcher, the `weather` table
(`db weather`, run by `db build`), reading it back, and the wind / slope split
in conditions.conditions(). No network."""

import csv
import datetime as dt
import io
import json
import math
import os
import shutil
import socket
import sqlite3
import subprocess
import sys
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request
from unittest import mock

from helpers import SAMPLES, TOOL, load_tool, tmpdir

import conditions
import weather

UTC = dt.timezone.utc
TODAY = dt.date(2026, 10, 16)
CELL = "10.0,20.0"
NW, NA = len(weather.WX), len(weather.AQ)
WI = {c: i for i, (c, _) in enumerate(weather.WX)}


def _no_net(*a, **k):
    raise AssertionError("real network access attempted")


def wx_row(h, ws=10.0, wd=270.0):
    """One hour of weather values in WX order: temp = hour, ws / wd as given."""
    r = [None] * NW
    for k, v in {"temp": float(h), "feels": h - 1.0, "rh": 60.0, "dew": 5.0, "precip": 0.1 * h, "rain": 0.1 * h,
                 "snow": 0.0, "code": 3 if h < 12 else 61, "cloud": 50.0, "pmsl": 1015.0, "psurf": 1010.0,
                 "ws": ws, "wd": wd, "gust": ws + 5}.items():
        r[WI[k]] = v
    return r


def wx_day(ws=10.0, wd=270.0):
    return [wx_row(h, ws, wd) for h in range(24)]


def aq_day():
    return [[5.0 + h, 10.0 + h, 60.0, 20.0 + h] for h in range(24)]


def hourly(pairs, days, row):
    out = {"time": [], **{v: [] for _, v in pairs}}
    for d in days:
        for h in range(24):
            out["time"].append(f"{d.isoformat()}T{h:02d}:00")
            for (_, v), x in zip(pairs, row(d, h)):
                out[v].append(x)
    return {"hourly": out}


class Responder:
    """Fake urlopen: records requests, answers like Open-Meteo for the dates asked.
    answers: optional list consumed in order (bytes body or an exception)."""

    def __init__(self, ws=10.0, archive_null_from=None, answers=None):
        self.reqs, self.timeouts, self.ws, self.null_from = [], [], ws, archive_null_from
        self.answers = list(answers or [])

    def __call__(self, req, timeout=None):
        self.reqs.append(req)
        self.timeouts.append(timeout)
        if self.answers:
            a = self.answers.pop(0)
            if isinstance(a, BaseException):
                raise a
            return io.BytesIO(a)
        url, q = self.url(-1), self.query(-1)
        if "start_date" in q:
            a, b = dt.date.fromisoformat(q["start_date"]), dt.date.fromisoformat(q["end_date"])
        else:
            a = TODAY - dt.timedelta(days=int(q["past_days"]))
            b = TODAY + dt.timedelta(days=int(q["forecast_days"]) - 1)
        days = [a + dt.timedelta(days=i) for i in range((b - a).days + 1)]
        if url == weather.AIR:
            body = hourly(weather.AQ, days, lambda d, h: aq_day()[h])
        elif url == weather.ARCHIVE and self.null_from:
            body = hourly(weather.WX, days, lambda d, h: [None] * NW if d >= self.null_from else wx_row(h, self.ws))
        else:
            body = hourly(weather.WX, days, lambda d, h: wx_row(h, self.ws + (100 if url == weather.FORECAST else 0)))
        return io.BytesIO(json.dumps(body).encode())

    def url(self, i):
        return self.reqs[i].full_url.split("?", 1)[0]

    def query(self, i):
        return dict(urllib.parse.parse_qsl(self.reqs[i].full_url.split("?", 1)[1]))


class Base(unittest.TestCase):
    def setUp(self):
        self.path = os.path.join(tmpdir(self), "cache", "weather.json")
        self.sleep = mock.Mock()
        self.log = []
        for p in (mock.patch("urllib.request.urlopen", _no_net), mock.patch.object(socket.socket, "connect", _no_net)):
            p.start()
            self.addCleanup(p.stop)

    def cache(self):
        return weather.Cache(self.path)

    def fetcher(self, cache, resp):
        return weather.Fetcher(cache, today=TODAY, log=self.log.append, urlopen=resp, sleep=self.sleep)

    def seed(self, kind, cell, date, rows, at=None):
        c = self.cache()
        c.put(kind, cell, date.isoformat(), rows, at or date + dt.timedelta(days=30))
        c.save()


# ------------------------------------------------------------------ cache


class CacheTests(Base):
    def test_cell_of(self):
        self.assertEqual(weather.cell_of(10.04, 20.06), "10.0,20.1")
        self.assertEqual(weather.cell_of(10.049, 19.96), CELL)
        self.assertEqual(weather.cell_of(-33.87, 151.21), "-33.9,151.2")
        self.assertEqual(weather.cell_of(-0.04, -0.04), "0.0,0.0")  # no "-0.0" key

    def test_missing_corrupt_and_v1_files_start_empty(self):
        self.assertEqual(self.cache().d, {"v": 2, "wx": {}, "aq": {}})
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        for body in ("{not json", "[1, 2]", json.dumps({CELL: {"2026-01-01": [[1, 2, 3, 4]] * 24}})):
            with open(self.path, "w") as f:
                f.write(body)
            self.assertEqual(self.cache().d["wx"], {}, body)

    def test_roundtrip_private_and_compact(self):
        d = TODAY - dt.timedelta(days=30)
        self.seed("wx", CELL, d, wx_day())
        if os.name != "nt":
            self.assertEqual(os.stat(self.path).st_mode & 0o777, 0o600)
        with open(self.path) as f:
            self.assertNotIn("\n ", f.read())  # no indent: the file gets big
        vals, fin = self.cache().hour("wx", CELL, dt.datetime(d.year, d.month, d.day, 8, tzinfo=UTC))
        self.assertEqual(vals[WI["temp"]], 8.0)
        self.assertTrue(fin)

    def test_need(self):
        c = self.cache()
        old, recent = TODAY - dt.timedelta(days=30), TODAY - dt.timedelta(days=2)
        self.assertTrue(c.need("wx", CELL, old, TODAY))
        self.assertFalse(c.need("wx", CELL, TODAY + dt.timedelta(days=1), TODAY))
        self.assertFalse(c.need("aq", CELL, dt.date(2012, 12, 31), TODAY))  # no air quality before 2013
        self.assertTrue(c.need("aq", CELL, dt.date(2013, 1, 1), TODAY))
        c.put("wx", CELL, old.isoformat(), wx_day(), old + dt.timedelta(days=8))
        self.assertFalse(c.need("wx", CELL, old, TODAY))  # final
        c.put("wx", CELL, recent.isoformat(), wx_day(), TODAY)
        self.assertFalse(c.need("wx", CELL, recent, TODAY))  # provisional, but fetched today
        self.assertTrue(c.need("wx", CELL, recent, TODAY + dt.timedelta(days=1)))  # refreshed next day
        c.put("wx", CELL, old.isoformat(), wx_day(), old + dt.timedelta(days=7))
        self.assertTrue(c.need("wx", CELL, old, TODAY))  # fetched within 7 days of itself: provisional

    def test_nested_garbage_dropped_on_read(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        d = TODAY - dt.timedelta(days=30)
        good = {"at": TODAY.isoformat(), "h": wx_day()}
        with open(self.path, "w") as f:
            json.dump({"v": 2, "wx": {"bad": [1], "str": "x", CELL: {d.isoformat(): good, "x": 5}}, "aq": {}}, f)
        c = self.cache()
        self.assertEqual(set(c.d["wx"]), {CELL})
        self.assertIsNone(c.day("wx", "bad", d))
        c.put("wx", "bad", d.isoformat(), wx_day(), TODAY)
        c.save()  # no crash merging with the file
        self.assertIsNotNone(self.cache().day("wx", CELL, d))

    def test_periodic_save_failure_still_stores_rows(self):
        db = make_db()
        for i in range(25):  # > SAVE_EVERY requests
            add_activity(db, i + 1, dt.datetime(2026, 9, 1, 8, tzinfo=UTC), track=[(0, 10.0 + i, 20.0), (600, 10.0 + i, 20.0)])
        c = self.cache()
        with mock.patch.object(weather.store, "write_json", side_effect=OSError("disk full")):
            r = weather.fill(db, c, self.fetcher(c, Responder()), log=self.log.append)
        self.assertEqual(len(r["stored"]), 25)

    def test_far_future_start_skipped(self):
        db = make_db()
        add_activity(db, 1, dt.datetime(9999, 12, 31, 23, 30, tzinfo=UTC), track=[(0, 10.0, 20.0), (7200, 10.0, 20.0)])
        add_activity(db, 2, dt.datetime(2026, 9, 1, 8, tzinfo=UTC), track=[(0, 10.0, 20.0), (600, 10.0, 20.0)])
        c = self.cache()
        r = weather.fill(db, c, self.fetcher(c, Responder()), log=self.log.append)
        self.assertEqual(r["stored"], {2})

    def test_save_failure_still_stores_rows(self):
        db = make_db()
        add_activity(db, 1, dt.datetime(2026, 9, 1, 8, tzinfo=UTC), track=[(0, 10.0, 20.0), (600, 10.0, 20.0)])
        c = self.cache()
        with mock.patch.object(weather.store, "write_json", side_effect=OSError("disk full")):
            r = weather.fill(db, c, self.fetcher(c, Responder()), log=self.log.append)
        self.assertEqual(r["stored"], {1})
        self.assertTrue(any("cache not saved" in x for x in self.log))

    def test_hour_malformed_and_null(self):
        d = TODAY - dt.timedelta(days=30)
        h = dt.datetime(d.year, d.month, d.day, 5, tzinfo=UTC)
        c = self.cache()
        day = wx_day()
        day[5] = [None] * NW
        c.put("wx", CELL, d.isoformat(), day, TODAY)
        self.assertEqual(c.hour("wx", CELL, h), (None, True))
        self.assertIsNotNone(c.hour("wx", CELL, h + dt.timedelta(hours=1))[0])
        for bad in ({"at": "x", "h": wx_day()}, {"at": TODAY.isoformat(), "h": wx_day()[:5]}, [1], None):
            c.d["wx"][CELL][d.isoformat()] = bad
            self.assertEqual(c.hour("wx", CELL, h), (None, False))
            self.assertTrue(c.need("wx", CELL, d, TODAY))
        c.d["wx"][CELL][d.isoformat()] = {"at": TODAY.isoformat(), "h": [[1, 2]] * 24}
        self.assertEqual(c.hour("wx", CELL, h)[0], None)


class ConcurrentSave(Base):
    def test_save_keeps_what_another_process_wrote(self):
        a, b = self.cache(), self.cache()
        d1, d2 = TODAY - dt.timedelta(days=30), TODAY - dt.timedelta(days=31)
        a.put("wx", CELL, d1.isoformat(), wx_day(), TODAY)
        a.save()
        b.put("aq", "11.0,20.0", d2.isoformat(), aq_day(), TODAY)
        b.save()  # b never saw a's day
        c = self.cache()
        self.assertIsNotNone(c.day("wx", CELL, d1))
        self.assertIsNotNone(c.day("aq", "11.0,20.0", d2))
        b.put("wx", CELL, d1.isoformat(), wx_day(ws=50.0), TODAY + dt.timedelta(days=1))  # a newer fetch wins
        b.save()
        self.assertEqual(self.cache().day("wx", CELL, d1)["h"][0][WI["ws"]], 50.0)


class OnDemandTests(unittest.TestCase):
    def make(self, offline=False, busy=False):
        j = weather.OnDemand("/home", "/tool", offline, busy=lambda: busy)
        j.offline = offline  # the tests' ROLLBOOK_OFFLINE=1 would make every job offline
        return j

    def test_offline_env(self):
        with mock.patch.dict(os.environ, {"ROLLBOOK_OFFLINE": "1"}):
            self.assertTrue(weather.OnDemand("/h", "/t").offline)
        with mock.patch.dict(os.environ, {"ROLLBOOK_OFFLINE": "0"}):
            self.assertFalse(weather.OnDemand("/h", "/t").offline)

    def test_backfill_then_ids(self):
        j = self.make()
        calls = []
        with mock.patch.object(weather.subprocess, "run", lambda a, **k: calls.append(a[2:]) or mock.Mock(returncode=0)), \
                mock.patch.object(weather.threading, "Thread") as th:
            j.backfill()
            j.request(5)
            self.assertEqual(th.call_count, 1)
            j._work()
        # the page waiting on 5 goes first, then the long backfill
        self.assertEqual(calls, [["db", "weather", "--id", "5"], ["db", "weather", "--missing"]])
        self.assertFalse(j.running)
        self.make(offline=True).backfill()  # offline: nothing (would raise: no thread patched otherwise)

    def test_crash_releases_queued_ids(self):
        j = self.make()
        with mock.patch.object(weather.subprocess, "run", side_effect=MemoryError), \
                mock.patch.object(weather.threading, "Thread"):
            for i in range(25):  # two batches: the second is still queued when the first crashes
                j.request(i)
            with self.assertRaises(MemoryError):
                j._work()
        self.assertEqual((j.queue, j.status(24)), ([], {"state": "error"}))

    def test_dead_worker_does_not_wedge(self):
        j = self.make()
        with mock.patch.object(weather.subprocess, "run", side_effect=MemoryError), \
                mock.patch.object(weather.threading, "Thread"):
            j.request(9)
            with self.assertRaises(MemoryError):
                j._work()
        self.assertEqual((j.running, j.status(9)), (False, {"state": "error"}))

    def test_offline_and_busy(self):
        self.assertEqual(self.make(offline=True).request(1), {"state": "offline"})
        self.assertEqual(self.make(offline=True).status(1), {"state": "offline"})
        self.assertEqual(self.make(busy=True).request(1), {"state": "busy"})

    def run_job(self, j, rc=0, exc=None):
        calls = []

        def fake_run(args, **kw):
            calls.append((args, kw))
            if exc:
                raise exc
            return mock.Mock(returncode=rc)
        with mock.patch.object(weather.subprocess, "run", fake_run), \
                mock.patch.object(weather.threading, "Thread") as th:
            first = j.request(7)
            again = j.request(7)
            j.request(8)
            self.assertEqual(th.call_count, 1)  # one worker for both
            self.assertEqual((first, again), ({"state": "queued"}, {"state": "queued"}))
            j._work()
        return calls

    def test_runs_db_weather_for_the_ids_once(self):
        j = self.make()
        calls = self.run_job(j)
        self.assertEqual(len(calls), 1)
        args, kw = calls[0]
        self.assertEqual(args[1:], ["/tool", "db", "weather", "--id", "7", "--id", "8"])
        self.assertEqual(kw["env"]["ROLLBOOK_HOME"], "/home")
        self.assertEqual(j.status(7), {"state": "done"})
        self.assertFalse(j.running)
        with mock.patch.object(weather.threading, "Thread") as th:
            self.assertEqual(j.request(7), {"state": "done"})  # not again within RETRY_S
            th.assert_not_called()
            j.state[7]["at"] -= j.RETRY_S + 1
            self.assertEqual(j.request(7), {"state": "queued"})
            th.assert_called_once()

    def test_failure(self):
        j = self.make()
        self.run_job(j, rc=1)
        self.assertEqual(j.status(8), {"state": "error"})
        j2 = self.make()
        self.run_job(j2, exc=subprocess.TimeoutExpired("x", 1))
        self.assertEqual(j2.status(7), {"state": "error"})
        self.assertEqual(self.make().status(3), {"state": "idle"})


class ParseTests(unittest.TestCase):
    def test_good(self):
        body = hourly(weather.AQ, [TODAY], lambda d, h: aq_day()[h])
        days = weather.parse(body, weather.AQ)
        self.assertEqual(list(days), [TODAY])
        self.assertEqual(days[TODAY][3], [8.0, 13.0, 60.0, 23.0])

    def test_malformed(self):
        good = hourly(weather.AQ, [TODAY], lambda d, h: aq_day()[h])
        missing = json.loads(json.dumps(good))
        del missing["hourly"]["ozone"]
        short = json.loads(json.dumps(good))
        short["hourly"]["pm10"].pop()
        badtime = json.loads(json.dumps(good))
        badtime["hourly"]["time"][0] = "yesterday"
        for b in (None, [], {"error": True}, {"hourly": {"time": "x"}}, missing, short, badtime):
            self.assertIsNone(weather.parse(b, weather.AQ))

    def test_non_numbers_are_null(self):
        body = hourly(weather.AQ, [TODAY], lambda d, h: ["x", True, float("inf"), 3])
        self.assertEqual(weather.parse(body, weather.AQ)[TODAY][0], [None, None, None, 3])

    def test_ranges(self):
        d = lambda n: TODAY + dt.timedelta(days=n)  # noqa: E731
        self.assertEqual(weather.ranges([d(0), d(3), d(10), d(30)]), [(d(0), d(10)), (d(30), d(30))])
        long = [d(i * 5) for i in range(30)]
        rs = weather.ranges(long)
        self.assertTrue(all((b - a).days < weather.MAX_RANGE for a, b in rs))
        self.assertEqual(sum(1 for x in long if any(a <= x <= b for a, b in rs)), 30)


# ------------------------------------------------------------------ fetcher


class FetchTests(Base):
    def test_archive_and_air_requests(self):
        d = TODAY - dt.timedelta(days=30)
        c = self.cache()
        resp = Responder()
        self.fetcher(c, resp).run({("wx", CELL): {d, d + dt.timedelta(days=2)}, ("aq", CELL): {d}})
        self.assertEqual(sorted(resp.url(i) for i in range(2)), sorted([weather.ARCHIVE, weather.AIR]))
        wq = resp.query([resp.url(i) for i in range(2)].index(weather.ARCHIVE))
        self.assertEqual((wq["latitude"], wq["longitude"]), ("10.0", "20.0"))
        self.assertEqual((wq["start_date"], wq["end_date"]), (d.isoformat(), (d + dt.timedelta(days=2)).isoformat()))
        self.assertEqual(wq["hourly"], ",".join(v for _, v in weather.WX))
        self.assertEqual((wq["timezone"], wq["wind_speed_unit"]), ("UTC", "kmh"))
        self.assertEqual(resp.reqs[0].get_header("User-agent"), weather.UA)
        self.assertEqual(resp.timeouts, [60, 60])
        self.sleep.assert_called_with(weather.GAP_S)
        fresh = self.cache()  # saved
        for k in range(3):
            self.assertIsNotNone(fresh.day("wx", CELL, d + dt.timedelta(days=k)))
        self.assertEqual(fresh.day("wx", CELL, d)["at"], TODAY.isoformat())
        self.assertEqual(fresh.hour("aq", CELL, dt.datetime(d.year, d.month, d.day, 2, tzinfo=UTC))[0][0], 7.0)

    def test_newest_first(self):
        resp = Responder()
        self.fetcher(self.cache(), resp).run({("wx", CELL): {TODAY - dt.timedelta(days=400), TODAY - dt.timedelta(days=20)}})
        self.assertEqual([resp.query(i)["start_date"] for i in range(2)],
                         [(TODAY - dt.timedelta(days=20)).isoformat(), (TODAY - dt.timedelta(days=400)).isoformat()])

    def test_recent_holes_from_the_forecast(self):
        c = self.cache()
        resp = Responder(archive_null_from=TODAY - dt.timedelta(days=1))
        self.fetcher(c, resp).run({("wx", CELL): {TODAY - dt.timedelta(days=3), TODAY}})
        self.assertEqual([resp.url(0), resp.url(1)], [weather.ARCHIVE, weather.FORECAST])
        q = resp.query(1)
        self.assertEqual((q["past_days"], q["forecast_days"]), ("2", "1"))
        self.assertNotIn("start_date", q)
        ws = lambda d: c.day("wx", CELL, d)["h"][9][WI["ws"]]  # noqa: E731
        self.assertEqual(ws(TODAY - dt.timedelta(days=3)), 10.0)  # archive
        self.assertEqual(ws(TODAY), 110.0)  # forecast filled the archive's nulls

    def test_old_nulls_never_go_to_the_forecast(self):
        d = TODAY - dt.timedelta(days=30)
        resp = Responder(archive_null_from=d)
        c = self.cache()
        self.fetcher(c, resp).run({("wx", CELL): {d}})
        self.assertEqual(len(resp.reqs), 1)
        self.assertEqual(c.hour("wx", CELL, dt.datetime(d.year, d.month, d.day, tzinfo=UTC)), (None, False))
        self.assertFalse(c.need("wx", CELL, d, TODAY))  # nothing there: not again this week ...
        self.assertTrue(c.need("wx", CELL, d, TODAY + dt.timedelta(days=7)))  # ... but next week

    def test_failed_refresh_keeps_the_cached_day(self):
        d = TODAY - dt.timedelta(days=1)  # provisional: refreshed today
        c = self.cache()
        c.put("wx", CELL, d.isoformat(), wx_day(ws=7.0), TODAY - dt.timedelta(days=1))
        err = urllib.error.HTTPError(weather.ARCHIVE, 400, "bad", {}, None)
        f = self.fetcher(c, Responder(answers=[err, err]))
        f.run({("wx", CELL): {d}})
        e = c.day("wx", CELL, d)
        self.assertEqual(e["h"][9][WI["ws"]], 7.0)  # not blanked
        self.assertEqual(e["tried"], TODAY.isoformat())
        self.assertFalse(c.need("wx", CELL, d, TODAY))  # tried today: not again today
        self.assertTrue(c.need("wx", CELL, d, TODAY + dt.timedelta(days=1)))

    def test_empty_recent_day_settles_then_weekly_then_given_up(self):
        d0 = TODAY - dt.timedelta(days=1)
        c = self.cache()
        asked = []
        for k in range(0, 90):
            day = TODAY + dt.timedelta(days=k)
            if c.need("aq", CELL, d0, day):
                asked.append(k)
                c.update("aq", CELL, d0.isoformat(), None, day)
        daily = [k for k in asked if k <= 7]
        self.assertEqual(daily, list(range(0, 8)))  # provisional: daily until settled (day 8 after it)
        later = [k for k in asked if k > 7]
        self.assertTrue(later and all(b - a >= 7 for a, b in zip([7] + later, later)))  # then weekly
        self.assertLess(max(asked), weather.GIVE_UP_DAYS + 8)  # then never
        self.assertTrue(c.done(c.day("aq", CELL, d0), d0))

    def test_provisional_day_whose_refresh_never_comes_is_given_up(self):
        d = TODAY - dt.timedelta(days=1)
        c = self.cache()
        c.put("wx", CELL, d.isoformat(), wx_day(), d)  # fetched on the day: provisional, with values
        asked = []
        for k in range(0, 120):
            day = TODAY + dt.timedelta(days=k)
            if c.need("wx", CELL, d, day):
                asked.append(k)
                c.update("wx", CELL, d.isoformat(), None, day)  # every refresh fails
        self.assertLessEqual(max(asked), weather.GIVE_UP_DAYS + weather.EMPTY_RETRY_DAYS)
        self.assertTrue(c.done(c.day("wx", CELL, d), d))
        self.assertEqual(c.day("wx", CELL, d)["h"][9][WI["ws"]], 10.0)  # kept

    def test_bad_tried_is_ignored(self):
        d = TODAY - dt.timedelta(days=40)
        c = self.cache()
        c.put("wx", CELL, d.isoformat(), [[None] * NW for _ in range(24)], d + dt.timedelta(days=10))
        for bad in ("x", "9", 5, "2026-13-99"):
            c.d["wx"][CELL][d.isoformat()]["tried"] = bad
            self.assertTrue(c.need("wx", CELL, d, TODAY))  # no crash, as if never retried

    def test_settled_answer_replaces_forecast_hours(self):
        d = TODAY - dt.timedelta(days=9)
        c = self.cache()
        c.put("wx", CELL, d.isoformat(), wx_day(ws=99.0), d + dt.timedelta(days=1))  # provisional (forecast)
        new = wx_day(ws=5.0)
        new[20] = [None] * NW
        c.update("wx", CELL, d.isoformat(), new, TODAY)
        self.assertIsNone(c.day("wx", CELL, d)["h"][20][WI["ws"]])  # the forecast value is not kept
        self.assertTrue(c.done(c.day("wx", CELL, d), d))

    def test_out_of_range_values_dropped(self):
        body = hourly(weather.WX, [TODAY], lambda d, h: [1e300] * NW)
        self.assertEqual(weather.parse(body, weather.WX)[TODAY][0], [None] * NW)
        d = TODAY - dt.timedelta(days=30)
        c = self.cache()
        row = wx_row(8)
        row[WI["ws"]] = 1e300
        c.put("wx", CELL, d.isoformat(), [row] * 24, TODAY)  # e.g. a tampered cache
        vals, _ = c.hour("wx", CELL, dt.datetime.combine(d, dt.time(8), UTC))
        self.assertIsNone(vals[WI["ws"]])
        self.assertEqual(vals[WI["temp"]], 8.0)

    def test_partial_answer_keeps_old_hours(self):
        d = TODAY - dt.timedelta(days=2)
        c = self.cache()
        c.put("wx", CELL, d.isoformat(), wx_day(ws=7.0), TODAY - dt.timedelta(days=1))
        new = wx_day(ws=9.0)
        for h in range(12, 24):
            new[h] = [None] * NW
        c.update("wx", CELL, d.isoformat(), new, TODAY)
        e = c.day("wx", CELL, d)
        self.assertEqual((e["h"][3][WI["ws"]], e["h"][20][WI["ws"]]), (9.0, 7.0))

    def test_deadline_stops_the_run(self):
        t = [0.0]
        resp = Responder()

        def answer(req, timeout=None):
            t[0] += 100
            return resp(req, timeout)
        c = self.cache()
        f = weather.Fetcher(c, today=TODAY, log=self.log.append, urlopen=answer, sleep=self.sleep,
                            deadline=150, clock=lambda: t[0])
        f.run({("wx", f"{i}.0,20.0"): {TODAY - dt.timedelta(days=30)} for i in range(5)})
        self.assertEqual(len(resp.reqs), 2)
        self.assertIn("time", f.aborted)
        self.assertEqual(sum(c.day("wx", f"{i}.0,20.0", TODAY - dt.timedelta(days=30)) is not None for i in range(5)), 2)

    def test_hostile_answers_skipped(self):
        bad_time = json.dumps({"hourly": {"time": [{}], **{v: [1] for _, v in weather.WX}}}).encode()
        huge_int = json.dumps({"hourly": {"time": [f"{TODAY.isoformat()}T00:00"], **{v: [10 ** 400] for _, v in weather.WX}}}).encode()
        resp = Responder(answers=[bad_time, huge_int])
        c = self.cache()
        f = self.fetcher(c, resp)
        f.run({("wx", f"{i}.0,20.0"): {TODAY - dt.timedelta(days=30 + i)} for i in range(2)})
        self.assertIsNone(f.aborted)

    def test_rate_limit_waits_then_retries(self):
        err = urllib.error.HTTPError(weather.ARCHIVE, 429, "Too Many Requests", {"Retry-After": "90"}, None)
        resp = Responder(answers=[err])
        c = self.cache()
        f = self.fetcher(c, resp)
        f.run({("wx", CELL): {TODAY - dt.timedelta(days=30)}})
        self.assertEqual(len(resp.reqs), 2)
        self.sleep.assert_any_call(90)
        self.assertIsNone(f.aborted)
        self.assertIsNotNone(c.day("wx", CELL, TODAY - dt.timedelta(days=30)))

    def test_persistent_5xx_skips_that_range_older_ones_still_fetched(self):
        err = urllib.error.HTTPError(weather.ARCHIVE, 503, "busy", {}, None)
        old, new = TODAY - dt.timedelta(days=400), TODAY - dt.timedelta(days=30)
        resp = Responder()
        real, calls = resp.__call__, []

        def answer(req, timeout=None):
            calls.append(req)
            if len(calls) <= 4:  # the newest range: 503 every time (four tries)
                raise err
            return real(req, timeout)
        c = self.cache()
        f = self.fetcher(c, resp)
        f.urlopen = answer
        f.run({("wx", CELL): {old, new}})
        self.assertIsNone(f.aborted)
        self.assertEqual(len(calls), 5)
        self.assertIsNotNone(c.hour("wx", CELL, dt.datetime(old.year, old.month, old.day, tzinfo=UTC))[0])
        self.assertTrue(c.need("wx", CELL, new, TODAY + dt.timedelta(days=7)))  # retried later

    def test_5xx_everywhere_aborts(self):
        err = urllib.error.HTTPError(weather.ARCHIVE, 502, "bad gateway", {}, None)
        resp = Responder(answers=[err] * 100)
        f = self.fetcher(self.cache(), resp)
        f.run({("wx", f"{i}.0,20.0"): {TODAY - dt.timedelta(days=30)} for i in range(10)})
        self.assertIn("502", f.aborted)
        self.assertEqual(len(resp.reqs), 4 * weather.MAX_BAD)

    def test_incomplete_read_is_network_trouble(self):
        import http.client

        class Broken:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self, n=-1):
                raise http.client.IncompleteRead(b"")
        resp = Responder()
        n = []

        def answer(req, timeout=None):
            n.append(1)
            return Broken() if len(n) == 1 else resp(req, timeout)
        c = self.cache()
        f = self.fetcher(c, answer)
        f.run({("wx", CELL): {TODAY - dt.timedelta(days=30)}})
        self.assertIsNone(f.aborted)
        self.assertEqual(len(n), 2)  # retried
        self.assertIsNotNone(c.hour("wx", CELL, dt.datetime.combine(TODAY - dt.timedelta(days=30), dt.time(), UTC))[0])

    def test_oversized_or_deep_answer_skipped(self):
        big = b"[" * 200000 + b"]" * 200000
        resp = Responder(answers=[b" " * (weather.MAX_BODY + 10), big])
        c = self.cache()
        f = self.fetcher(c, resp)
        f.run({("wx", f"{i}.0,20.0"): {TODAY - dt.timedelta(days=30)} for i in range(2)})
        self.assertIsNone(f.aborted)
        self.assertEqual(len(resp.reqs), 2)

    def test_wait_capped(self):
        err = urllib.error.HTTPError(weather.ARCHIVE, 429, "slow down", {"Retry-After": "500"}, None)
        resp = Responder(answers=[err])
        f = weather.Fetcher(self.cache(), today=TODAY, log=self.log.append, urlopen=resp, sleep=self.sleep, max_wait=60)
        f.run({("wx", CELL): {TODAY - dt.timedelta(days=30)}})
        self.sleep.assert_any_call(60)

    def test_persistent_rate_limit_aborts_the_run_but_keeps_what_it_has(self):
        err = urllib.error.HTTPError(weather.ARCHIVE, 429, "busy", {}, None)
        old, new = TODAY - dt.timedelta(days=400), TODAY - dt.timedelta(days=30)
        resp = Responder()
        f = self.fetcher(self.cache(), resp)
        real = resp.__call__
        resp.answers = []
        calls = []

        def answer(req, timeout=None):
            calls.append(req)
            if len(calls) > 1:  # the first (newest) range works, then 503 for good
                raise err
            return real(req, timeout)
        f.urlopen = answer
        f.run({("wx", CELL): {old, new}})
        self.assertEqual(len(calls), 1 + 4)
        self.assertIn("429", f.aborted)
        self.assertIsNotNone(self.cache().day("wx", CELL, new))  # saved despite the abort
        self.assertTrue(any("stopped" in x for x in self.log))

    def test_client_error_skips_that_range_only(self):
        err = urllib.error.HTTPError(weather.ARCHIVE, 400, "bad", {}, None)
        resp = Responder(answers=[err])
        c = self.cache()
        self.fetcher(c, resp).run({("wx", CELL): {TODAY - dt.timedelta(days=30)}, ("wx", "11.0,20.0"): {TODAY - dt.timedelta(days=300)}})
        self.assertEqual(len(resp.reqs), 2)
        d = TODAY - dt.timedelta(days=30)
        self.assertIsNone(c.hour("wx", CELL, dt.datetime.combine(d, dt.time(), UTC))[0])
        self.assertFalse(c.need("wx", CELL, d, TODAY))  # not again on every build
        self.assertIsNotNone(c.day("wx", "11.0,20.0", TODAY - dt.timedelta(days=300)))

    def test_network_down_gives_up_after_a_few_errors(self):
        resp = Responder(answers=[urllib.error.URLError("down")] * 20)
        f = self.fetcher(self.cache(), resp)
        f.run({("wx", f"{i}.0,20.0"): {TODAY - dt.timedelta(days=30)} for i in range(10)})
        self.assertEqual(len(resp.reqs), weather.MAX_FAILS)
        self.assertIn("network", f.aborted)
        self.assertFalse(os.path.exists(self.path))  # nothing to save

    def test_malformed_answers_do_not_stop_the_run(self):
        bad = json.dumps({"hourly": {"time": [f"{TODAY.isoformat()}T00:00"]}}).encode()
        resp = Responder(answers=[bad, b"oops"])
        c = self.cache()
        f = self.fetcher(c, resp)
        f.run({("wx", f"{i}.0,20.0"): {TODAY - dt.timedelta(days=30 + i)} for i in range(3)})
        self.assertEqual(len(resp.reqs), 3)
        self.assertIsNone(f.aborted)
        self.assertEqual(sum(c.hour("wx", f"{i}.0,20.0", dt.datetime.combine(TODAY - dt.timedelta(days=30 + i),
                                                                               dt.time(), UTC))[0] is not None
                             for i in range(3)), 1)


# ------------------------------------------------------------------ the weather table


def make_db(tool=None):
    tool = tool or load_tool()
    db = sqlite3.connect(":memory:")
    db.executescript(tool.SCHEMA)
    db.executescript(weather.DDL)
    return db


def add_activity(db, aid, start, typ="Inline Skate", track=None, has_gps=1, elapsed=None):
    track = track or []
    db.execute("INSERT INTO activities (id, type, datetime, elapsed_s, has_gps) VALUES (?,?,?,?,?)",
               (aid, typ, start.isoformat(), elapsed if elapsed is not None else (track[-1][0] if track else 0), has_gps))
    db.executemany("INSERT INTO samples (activity_id, t, lat, lon) VALUES (?,?,?,?)", [(aid, *p) for p in track])


class Fill(Base):
    START = dt.datetime(2026, 9, 1, 8, 40, tzinfo=UTC)

    def setUp(self):
        super().setUp()
        self.db = make_db()
        # 8:40 -> 10:10, crossing from cell 10.0,20.0 into 10.0,20.2 after an hour
        trk = [(t, 10.0, 20.0 if t < 3600 else 20.2) for t in range(0, 5401, 60)]
        add_activity(self.db, 1, self.START, track=trk)
        add_activity(self.db, 2, self.START, typ="Virtual Ride", track=trk)
        add_activity(self.db, 3, self.START, typ="Run", has_gps=0)
        add_activity(self.db, 4, self.START, typ="Run", track=[(0, 0.0, 0.0), (60, None, None)])

    def rows(self, aid=1):
        return self.db.execute("SELECT t, temp, ws, pm25, final FROM weather WHERE activity_id=? ORDER BY t", (aid,)).fetchall()

    def test_hours_and_positions(self):
        hs = weather.hours_of(self.START, 5400)
        self.assertEqual([h.hour for h in hs], [8, 9, 10, 11])
        self.assertEqual([h.hour for h in weather.hours_of(dt.datetime(2026, 1, 1, 9, 0, tzinfo=UTC), 3600)], [9, 10])
        self.assertEqual(len(weather.hours_of(self.START, 30 * 86400)), weather.MAX_HOURS + 1)
        pos = weather.positions([(0, 1, 1), (1200, 2, 2), (5400, 3, 3)], self.START, hs)
        self.assertEqual(pos, [(1, 1), (2, 2), (3, 3), (3, 3)])

    def test_offline_from_the_cache(self):
        d = self.START.date()
        for cell in (CELL, "10.0,20.2"):
            self.seed("wx", cell, d, wx_day(ws=10 if cell == CELL else 30))
            self.seed("aq", cell, d, aq_day())
        r = weather.fill(self.db, self.cache(), None, log=self.log.append, today=TODAY)
        self.assertEqual(r, {"sessions": 1, "without": 0, "aborted": None, "stored": {1}})
        rows = self.rows()
        self.assertEqual([x[0] for x in rows], [f"2026-09-01T{h:02d}:00:00+00:00" for h in (8, 9, 10, 11)])
        self.assertEqual([x[2] for x in rows], [10.0, 10.0, 30.0, 30.0])  # each hour at that hour's position
        self.assertEqual([x[1] for x in rows], [8.0, 9.0, 10.0, 11.0])
        self.assertEqual(rows[0][3], 13.0)
        self.assertEqual({x[4] for x in rows}, {1})
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM weather WHERE activity_id != 1").fetchone()[0], 0)
        v = self.db.execute("SELECT hours, temp_min, temp_max, final FROM v_weather").fetchone()
        self.assertEqual(v, (4, 8.0, 11.0, 1))

    def test_missing_hour_marks_the_session_provisional(self):
        self.seed("wx", CELL, self.START.date(), wx_day())  # the second cell isn't cached
        weather.fill(self.db, self.cache(), None, log=self.log.append, today=TODAY)
        rows = self.rows()
        self.assertEqual(len(rows), 2)
        self.assertEqual({x[4] for x in rows}, {0})
        self.assertIn("offline", self.log[0])

    def test_fetch_only_what_is_needed_then_nothing(self):
        resp = Responder()
        c = self.cache()
        weather.fill(self.db, c, self.fetcher(c, resp), log=self.log.append)
        self.assertEqual(len(resp.reqs), 4)  # two cells x weather / air
        self.assertEqual({(resp.query(i)["longitude"], resp.query(i)["start_date"]) for i in range(4)},
                         {("20.0", "2026-09-01"), ("20.2", "2026-09-01")})
        self.assertEqual({x[4] for x in self.rows()}, {1})
        resp2 = Responder()
        r = weather.fill(self.db, c, self.fetcher(c, resp2), missing=True, log=self.log.append)
        self.assertEqual((resp2.reqs, r["sessions"]), ([], 0))  # nothing missing
        r = weather.fill(self.db, self.cache(), self.fetcher(self.cache(), resp2), missing=False, log=self.log.append)
        self.assertEqual((resp2.reqs, r["sessions"]), ([], 1))  # rewritten from the cache

    def test_provisional_session_refreshed(self):
        recent = dt.datetime(2026, 10, 15, 8, 0, tzinfo=UTC)
        db = make_db()
        add_activity(db, 9, recent, track=[(0, 10.0, 20.0), (1800, 10.0, 20.0)])
        resp = Responder()
        c = self.cache()
        weather.fill(db, c, self.fetcher(c, resp), log=self.log.append)
        self.assertEqual({r[0] for r in db.execute("SELECT final FROM weather")}, {0})
        n = len(resp.reqs)
        weather.fill(db, c, self.fetcher(c, resp), log=self.log.append)
        self.assertEqual(len(resp.reqs), n)  # same day: from the cache
        later = weather.Fetcher(c, today=dt.date(2026, 10, 30), log=self.log.append, urlopen=resp, sleep=self.sleep)
        weather.fill(db, c, later, log=self.log.append)
        self.assertGreater(len(resp.reqs), n)
        self.assertEqual({r[0] for r in db.execute("SELECT final FROM weather")}, {1})

    def test_ids_only_those_sessions_even_if_stored(self):
        for cell in (CELL, "10.0,20.2"):
            self.seed("wx", cell, self.START.date(), wx_day())
        add_activity(self.db, 5, self.START, track=[(0, 10.0, 20.0), (600, 10.0, 20.0)])
        r = weather.fill(self.db, self.cache(), None, log=self.log.append, today=TODAY, ids=[5])
        self.assertEqual(r["sessions"], 1)
        self.assertEqual({x[0] for x in self.db.execute("SELECT DISTINCT activity_id FROM weather")}, {5})
        r = weather.fill(self.db, self.cache(), None, log=self.log.append, today=TODAY, ids=[5, 2])
        self.assertEqual(r["sessions"], 1)  # 2 is a virtual ride: never

    def test_no_write_lock_held_while_fetching(self):
        path = os.path.join(tmpdir(self), "w.db")
        db = sqlite3.connect(path)
        db.executescript(load_tool().SCHEMA)
        add_activity(db, 1, self.START, track=[(0, 10.0, 20.0), (600, 10.0, 20.0)])
        db.commit()
        other = sqlite3.connect(path, timeout=0)

        def answer(req, timeout=None):  # another writer during the fetch must not block
            other.execute("INSERT INTO splits (activity_id, km) VALUES (1, (SELECT COUNT(*) FROM splits))")
            other.commit()
            return Responder()(req, timeout)
        c = self.cache()
        weather.fill(db, c, self.fetcher(c, answer), log=self.log.append)
        self.assertEqual(other.execute("SELECT COUNT(*) FROM splits").fetchone()[0], 2)
        other.close()
        db.close()

    def test_gaps_for_good_do_not_keep_a_session_provisional(self):
        d = self.START.date()
        day = wx_day()
        day[9] = [None] * NW  # an hour the archive never has
        for cell in (CELL, "10.0,20.2"):
            self.seed("wx", cell, d, day)
            self.seed("aq", cell, d, [[None] * NA for _ in range(24)], at=d + dt.timedelta(days=weather.GIVE_UP_DAYS + 1))
        weather.fill(self.db, self.cache(), None, log=self.log.append, today=TODAY)
        self.assertEqual({x[4] for x in self.rows()}, {1})
        r = weather.fill(self.db, self.cache(), self.fetcher(self.cache(), Responder()), missing=True, log=self.log.append)
        self.assertEqual(r["sessions"], 0)  # not planned again

    def test_rows_of_removed_sessions_dropped(self):
        self.db.execute("INSERT INTO weather (activity_id, t, final) VALUES (77, '2026-01-01T00:00:00+00:00', 1)")
        weather.fill(self.db, self.cache(), None, log=self.log.append, today=TODAY)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM weather WHERE activity_id=77").fetchone()[0], 0)

    def test_ddl_matches_cols(self):
        cols = [r[1] for r in self.db.execute("PRAGMA table_info(weather)")]
        self.assertEqual(cols, ["activity_id", "t", *weather.COLS, "final"])

    def test_load_without_table(self):
        self.assertEqual(weather.load(sqlite3.connect(":memory:")), {})


# ------------------------------------------------------------------ reading


def hours(*rows, t0=dt.datetime(2026, 6, 1, 8, tzinfo=UTC)):
    """[(epoch, values)] one hour apart from t0; rows are dicts merged over a default."""
    base = {k: None for k in weather.COLS}
    out = []
    for i, r in enumerate(rows):
        ts = (t0 + dt.timedelta(hours=i)).timestamp()
        out.append((ts, {**base, "temp": 10.0, "ws": 10.0, "wd": 270.0, "gust": 20.0, "final": 1, **r}))
    return out


T0 = dt.datetime(2026, 6, 1, 8, tzinfo=UTC).timestamp()


class Reading(unittest.TestCase):
    def test_at_interpolates(self):
        h = hours({"temp": 10.0, "precip": 1.0, "code": 3}, {"temp": 20.0, "precip": 2.0, "code": 61})
        v = weather.at(h, T0 + 900)
        self.assertAlmostEqual(v["temp"], 12.5)
        self.assertEqual(v["precip"], 2.0)  # the hour that covers 8:15 is 9:00's
        self.assertEqual(v["code"], 3)
        self.assertEqual(weather.at(h, T0 + 2700)["code"], 61)
        self.assertEqual(weather.at(h, T0)["precip"], 1.0)

    def test_wind_direction_as_a_vector(self):
        h = hours({"wd": 350.0}, {"wd": 10.0})
        self.assertAlmostEqual(weather.at(h, T0 + 1800)["wd"] % 360, 0.0, places=6)
        self.assertAlmostEqual(weather.at(hours({"wd": 90.0}, {"wd": 180.0}), T0 + 1800)["wd"], 135.0, places=6)

    def test_ends_and_out_of_range(self):
        h = hours({"temp": 10.0}, {"temp": 20.0})
        self.assertEqual(weather.at(h, T0 - 1800)["temp"], 10.0)
        self.assertEqual(weather.at(h, T0 + 3600 + 1800)["temp"], 20.0)
        self.assertIsNone(weather.at(h, T0 - 4000))
        self.assertIsNone(weather.at(h, T0 + 3 * 3600))
        self.assertIsNone(weather.at([], T0))
        self.assertEqual(weather.at(hours({"temp": None}, {"temp": 20.0}), T0 + 600)["temp"], None)

    def test_summary(self):
        h = hours({"temp": 10.0, "precip": 0.5, "gust": 15.0, "aqi": 30.0, "code": 1},
                  {"temp": 14.0, "precip": 1.0, "gust": 25.0, "aqi": 50.0, "code": 63},
                  {"temp": 12.0, "precip": 7.0, "gust": 40.0, "aqi": 10.0, "code": 95, "final": 0})
        s = weather.summary(h, T0 + 1800, 3600)  # 8:30 -> 9:30
        self.assertAlmostEqual(s["temp"], 14.0)
        self.assertEqual((s["temp_min"], s["temp_max"]), (12.0, 14.0))
        self.assertEqual(s["precip_sum"], 8.0)  # 9:00 and 10:00 cover 8:30 -> 9:30
        self.assertEqual(s["gust_max"], 32.5)
        self.assertEqual(s["code"], 95)  # 10:00 is within half an hour of the end
        self.assertEqual(s["final"], 0)
        self.assertIsNone(weather.summary(h, T0 + 86400, 60))

    def test_start_ts(self):
        self.assertEqual(weather.start_ts("2026-06-01T08:00:00"), T0)
        self.assertEqual(weather.start_ts("2026-06-01T10:00:00+02:00"), T0)
        self.assertIsNone(weather.start_ts(None))
        self.assertIsNone(weather.start_ts("soon"))


# ------------------------------------------------------------------ conditions


class Conditions(unittest.TestCase):
    R = {"type": "Inline Skate", "datetime": "2026-06-01T08:00:00"}
    M = {"ll0": [10.0, 20.0], "route": "out-back", "dur_s": 3600, "t_turn": 1600,
         "out_b10": 26.0, "back_b10": 24.0, "bearing_out": 90}

    def cond(self, wind, r=None, m=None, fits=None, n=2):
        h = hours(*([wind] * n)) if wind else []
        with mock.patch.dict(conditions.FITS, fits or {}, clear=True):
            return conditions.conditions(r or self.R, m or self.M, h)

    def test_indoor_or_no_rows_gives_nothing(self):
        h = hours({}, {})
        self.assertIsNone(conditions.conditions({"type": "Ride", "datetime": self.R["datetime"]},
                                                {"ll0": [10, 20], "route": "indoor"}, h))
        self.assertIsNone(conditions.conditions({"type": "Workout", "datetime": self.R["datetime"]}, {"ll0": [10, 20]}, h))
        self.assertIsNone(conditions.conditions({"type": "Virtual Run", "datetime": self.R["datetime"]}, {"ll0": [10, 20]}, h))
        self.assertIsNone(conditions.conditions(self.R, {"route": "out-back"}, h))  # no position
        self.assertIsNone(conditions.conditions(self.R, self.M, None))
        self.assertIsNone(conditions.conditions(self.R, self.M))

    def test_plain_route_only_weather(self):
        c = self.cond({"temp": 15.0, "ws": 10.0, "wd": 270.0, "gust": 20.0, "rh": 70.0}, m={**self.M, "route": "line"})
        self.assertEqual((c["temp"], c["ws"], c["wd"], c["gust"], c["rh"]), (15.0, 10.0, 270.0, 20.0, 70.0))
        self.assertNotIn("legs", c)

    def test_tailwind_out_leg(self):
        c = self.cond({"temp": 15.0, "ws": 10.0, "wd": 270.0, "gust": 20.0})
        L = c["legs"]
        self.assertAlmostEqual(L["out"]["tail"], 10, places=9)
        self.assertAlmostEqual(L["back"]["tail"], -10, places=9)
        self.assertAlmostEqual(c["wind_k"], conditions.WIND_K_DEFAULT["Inline Skate"])
        self.assertAlmostEqual(L["out"]["wind_pct"], 4.0, places=9)
        self.assertAlmostEqual(c["gap_pct"], 100 / 12, places=9)
        self.assertAlmostEqual(c["gap_wind"], 8.0, places=9)
        self.assertEqual(c["gap_slope"], 0.0)
        self.assertAlmostEqual(c["gap_other"], 100 / 12 - 8, places=9)
        self.assertAlmostEqual(L["out"]["adj_b10"], 25.0, places=9)
        self.assertAlmostEqual(L["back"]["adj_b10"], 25.0, places=9)
        self.assertAlmostEqual(c["adjusted"], 25.0, places=9)

    def test_each_leg_gets_its_own_hour(self):
        # 8:00 calm, 9:00 westerly 20: the out leg (mid 8:13) sees ~4.4, the back (mid 8:43) ~14.4
        h = hours({"ws": 0.0, "wd": 270.0}, {"ws": 20.0, "wd": 270.0})
        c = conditions.conditions(self.R, self.M, h)
        self.assertAlmostEqual(c["legs"]["out"]["tail"], 20 * 800 / 3600, places=6)
        self.assertAlmostEqual(c["legs"]["back"]["tail"], -20 * 2600 / 3600, places=6)

    def test_headwind_out_leg(self):
        c = self.cond({"temp": 15.0, "ws": 10.0, "wd": 90.0, "gust": 20.0})
        self.assertAlmostEqual(c["legs"]["out"]["tail"], -10, places=9)
        self.assertAlmostEqual(c["legs"]["out"]["adj_b10"], 26 / 0.96, places=9)
        self.assertAlmostEqual(c["legs"]["back"]["adj_b10"], 24 / 1.04, places=9)
        self.assertAlmostEqual(c["gap_wind"], -8.0, places=9)

    def test_crosswind_is_neutral(self):
        c = self.cond({"temp": 15.0, "ws": 10.0, "wd": 0.0, "gust": 20.0})
        for leg in ("out", "back"):
            self.assertAlmostEqual(c["legs"][leg]["tail"], 0, places=9)
        self.assertAlmostEqual(c["adjusted"], 25.0, places=9)

    def test_speed_without_direction_no_legs(self):
        c = self.cond({"temp": 15.0, "ws": 5.0, "wd": None})
        self.assertNotIn("legs", c)

    def test_fit_wind_nan(self):
        rows = [{"id": i, "cond": {"legs": {"out": {"tail": float(i)}, "back": {"tail": 0.0}},
                                   "gap_pct": float("inf"), "gap_slope": 0.0}} for i in range(40)]
        self.assertIsNone(conditions.fit_wind("Inline Skate", rows, {}))

    def test_no_wind_values_no_legs(self):
        c = self.cond({"temp": 15.0, "ws": None, "wd": None})
        self.assertEqual(c["temp"], 15.0)
        self.assertNotIn("legs", c)

    def test_fitted_k_overrides_default(self):
        c = self.cond({"temp": 15.0, "ws": 10.0, "wd": 270.0, "gust": 20.0}, fits={"Inline Skate": 1.0})
        self.assertEqual(c["wind_k"], 1.0)
        self.assertAlmostEqual(c["legs"]["out"]["wind_pct"], 10.0, places=9)

    def test_slope_part(self):
        m = {**self.M, "g_out": -0.1, "g_back": 0.1}
        c = self.cond({"temp": 15.0, "ws": 0.0, "wd": 0.0, "gust": 0.0}, m=m)
        so, sb = conditions.slope_pct("Inline Skate", 26.0, -0.1), conditions.slope_pct("Inline Skate", 24.0, 0.1)
        self.assertGreater(so, 0)
        self.assertLess(sb, 0)
        self.assertAlmostEqual(c["gap_slope"], so - sb, places=9)
        self.assertAlmostEqual(c["legs"]["out"]["adj_b10"], 26 / (1 + so / 100), places=9)

    def test_tailwind_quartering(self):
        self.assertAlmostEqual(conditions.tailwind({"ws": 10, "wd": 225}, 90), 10 * 2 ** -0.5, places=9)


# ------------------------------------------------------------------ CLI, dashboard, MCP


def run(home, *args):
    env = dict(os.environ, ROLLBOOK_HOME=home, PYTHONIOENCODING="utf-8", ROLLBOOK_OFFLINE="1")
    return subprocess.run([sys.executable, TOOL, *args], env=env, capture_output=True, text=True, timeout=300)


class EndToEnd(unittest.TestCase):
    """Build from a sample ride, fill the table from a seeded cache, read it back."""

    AID = 1001

    @classmethod
    def setUpClass(cls):
        import tempfile
        cls.home = tempfile.mkdtemp(prefix="rollbook-test-wx-")
        exp = os.path.join(cls.home, "export")
        os.makedirs(os.path.join(exp, "activities"))
        shutil.copy(os.path.join(SAMPLES, "Outdoor_cycle.gpx"), os.path.join(exp, "activities", "a.gpx"))
        with open(os.path.join(exp, "activities.csv"), "w", newline="") as f:
            csv.writer(f).writerows([["Activity ID", "Activity Name", "Activity Type", "Filename"],
                                     [cls.AID, "ride", "Inline Skate", "activities/a.gpx"]])
        cls.export = exp
        r = run(cls.home, "db", "build", "--no-weather", exp)
        assert r.returncode == 0, r.stdout + r.stderr
        assert "weather:" not in r.stdout, r.stdout
        cls.db = os.path.join(cls.home, "rollbook.db")
        # seed the cache for every cell / day the ride touches, then let `db weather` read it
        db = sqlite3.connect(cls.db)
        start = weather.start_ts(db.execute("SELECT datetime FROM activities").fetchone()[0])
        trk = db.execute("SELECT t, lat, lon FROM samples WHERE lat IS NOT NULL ORDER BY t").fetchall()
        db.close()
        t0 = dt.datetime.fromtimestamp(start, UTC)
        hs = weather.hours_of(t0, trk[-1][0])
        c = weather.Cache(os.path.join(cls.home, "cache", "weather.json"))
        for h, (lat, lon) in zip(hs, weather.positions(trk, t0, hs)):
            c.put("wx", weather.cell_of(lat, lon), h.date().isoformat(), wx_day(ws=12.0), h.date() + dt.timedelta(days=30))
            c.put("aq", weather.cell_of(lat, lon), h.date().isoformat(), aq_day(), h.date() + dt.timedelta(days=30))
        c.save()
        cls.nhours = len(hs)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.home, True)

    def count(self):
        db = sqlite3.connect(self.db)
        try:
            return db.execute("SELECT COUNT(*), MIN(final) FROM weather").fetchone()
        finally:
            db.close()

    def test_1_db_weather_fills_from_the_cache(self):
        self.assertEqual(self.count(), (0, None))
        r = run(self.home, "db", "weather", "--missing")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("weather: 1 sessions stored", r.stdout)
        self.assertEqual(self.count(), (self.nhours, 1))

    def test_2_rebuild_of_the_session_looks_it_up_again(self):
        run(self.home, "db", "weather")
        r = run(self.home, "db", "build", "--fresh", self.export)  # ROLLBOOK_OFFLINE: cache only
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("weather: 1 sessions stored", r.stdout)
        self.assertEqual(self.count(), (self.nhours, 1))

    def test_4_opening_a_session_without_weather_fetches_it(self):
        import api
        import server
        db = sqlite3.connect(self.db)
        db.execute("DELETE FROM weather")
        db.commit()
        db.close()
        httpd, M, url = server.start(self.db, os.path.join(self.home, "cache", "web_metrics.json"), 0, offline=False)
        api.WX_JOB.offline = False  # the tests' ROLLBOOK_OFFLINE: the subprocess still reads only the cache
        threading.Thread(target=httpd.serve_forever, daemon=True).start()

        def get(p):
            with urllib.request.urlopen(url + p, timeout=60) as r:
                return json.loads(r.read())

        def post(body, headers=None):
            req = urllib.request.Request(url + "api/weather", data=json.dumps(body).encode(), method="POST",
                                         headers=headers if headers is not None else
                                         {"X-Rollbook": "1", "Content-Type": "application/json"})
            try:
                with urllib.request.urlopen(req, timeout=60) as r:
                    return r.status, json.loads(r.read())
            except urllib.error.HTTPError as e:
                return e.code, None
        try:
            a = get(f"api/activity/{self.AID}")
            self.assertTrue(a["weather_missing"])
            self.assertIsNone(a["cond"])
            self.assertEqual(post({"id": self.AID}, {})[0], 403)  # no X-Rollbook
            self.assertEqual(post({"id": 424242})[0], 404)
            self.assertEqual(post({"id": "x"})[0], 400)
            self.assertEqual(post({"id": self.AID}), (200, {"state": "queued"}))
            for _ in range(300):  # the subprocess inherits ROLLBOOK_OFFLINE: fills from the seeded cache
                st = get(f"api/weather?id={self.AID}")["state"]
                if st not in ("queued", "running"):
                    break
                threading.Event().wait(0.1)
            self.assertEqual(st, "done")
            a = get(f"api/activity/{self.AID}")
            self.assertFalse(a["weather_missing"])
            self.assertEqual(a["cond"]["ws"], 12.0)
            self.assertEqual(post({"id": self.AID}), (200, {"state": "done"}))  # no second run
        finally:
            httpd.shutdown()
            httpd.server_close()
            api.WX_JOB = None
        self.assertEqual(self.count(), (self.nhours, 1))

    def test_6_edited_sport_and_exit_codes(self):
        run(self.home, "db", "weather")
        db = sqlite3.connect(self.db)
        db.execute("DELETE FROM weather")
        db.commit()
        db.close()
        with open(os.path.join(self.home, "edits.json"), "w") as f:
            json.dump({"sessions": {str(self.AID): {"type": "Workout"}}}, f)
        try:
            r = run(self.home, "db", "weather", "--id", str(self.AID))
            self.assertEqual(r.returncode, 2, r.stdout + r.stderr)  # edited to indoor: none, says so
            self.assertEqual(self.count(), (0, None))
            r = run(self.home, "db", "weather", "--id", "4242")
            self.assertEqual(r.returncode, 2)
        finally:
            os.remove(os.path.join(self.home, "edits.json"))
        with open(os.path.join(self.home, "edits.json"), "w") as f:
            json.dump({"sessions": {str(self.AID): {"hidden": True, "deleted": True}}}, f)
        try:
            run(self.home, "db", "weather", "--missing")
            self.assertEqual(self.count(), (0, None))  # deleted: never in bulk
            r = run(self.home, "db", "weather", "--id", str(self.AID))
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)  # its own page may still ask
        finally:
            os.remove(os.path.join(self.home, "edits.json"))
        self.assertEqual(self.count(), (self.nhours, 1))

    def test_7_post_for_indoor_or_filled_spawns_nothing(self):
        import api
        import server
        run(self.home, "db", "weather")
        httpd, M, url = server.start(self.db, os.path.join(self.home, "cache", "web_metrics.json"), 0, offline=False)
        api.WX_JOB.offline = False
        api.WX_JOB.request = mock.Mock(side_effect=AssertionError("spawned"))
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        try:
            req = urllib.request.Request(url + "api/weather", data=json.dumps({"id": self.AID}).encode(), method="POST",
                                         headers={"X-Rollbook": "1", "Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=60) as r:
                self.assertEqual(json.loads(r.read()), {"state": "done"})  # has rows already
        finally:
            httpd.shutdown()
            httpd.server_close()
            api.WX_JOB = None

    def test_8_mcp_hides_surface_pressure(self):
        import mcp_server
        run(self.home, "db", "weather")
        for allow in (False, True):
            rb = mcp_server.Rollbook(self.db, os.path.join(self.home, "cache", "web_metrics.json"), allow_gps=allow)
            try:
                rb.sync()
                out = rb.t_activity({"id": self.AID})
                q = rb.t_query({"sql": "SELECT psurf, pmsl FROM weather LIMIT 1"})
            finally:
                rb.close()
            self.assertEqual("psurf" in out["weather"]["hours"][0], allow)
            self.assertEqual(q["rows"][0][0] is not None, allow)
            self.assertIsNotNone(q["rows"][0][1])

    def test_9_unreadable_edits_no_bulk_run(self):
        p = os.path.join(self.home, "edits.json")
        with open(p, "w") as f:
            f.write("{broken")
        try:
            r = run(self.home, "db", "weather")
            self.assertEqual(r.returncode, 3, r.stdout + r.stderr)
            self.assertIn("skipped", r.stderr)
            self.assertEqual(run(self.home, "db", "weather", "--id", str(self.AID)).returncode, 0)
        finally:
            os.remove(p)

    def test_5_offline_server_never_fetches(self):
        import server
        httpd, M, url = server.start(self.db, os.path.join(self.home, "cache", "web_metrics.json"), 0, offline=True)
        try:
            import api
            self.assertEqual(api.WX_JOB.request(self.AID), {"state": "offline"})
        finally:
            httpd.server_close()

    def test_3_dashboard_and_mcp_read_the_table(self):
        run(self.home, "db", "weather")
        import mcp_server
        import server
        httpd, M, url = server.start(self.db, os.path.join(self.home, "cache", "web_metrics.json"), 0, offline=True)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        try:
            with urllib.request.urlopen(f"{url}api/activity/{self.AID}", timeout=60) as r:
                a = json.loads(r.read())
            with urllib.request.urlopen(f"{url}api/activities?type=Inline%20Skate", timeout=60) as r:
                lst = json.loads(r.read())
        finally:
            httpd.shutdown()
            httpd.server_close()
        c = a["cond"]
        self.assertEqual(c["ws"], 12.0)
        for k in ("temp", "feels", "rh", "cloud", "pmsl", "pm25", "aqi", "temp_min", "temp_max", "precip_sum", "final"):
            self.assertIsNotNone(c.get(k), k)
        self.assertIn("legs", c)  # the sample ride is an out-and-back
        self.assertEqual(len(a["weather_hours"]), self.nhours)  # the details view
        self.assertEqual(a["weather_hours"][0]["ws"], 12.0)
        self.assertEqual(lst[0]["temp"], c["temp"])
        self.assertEqual(lst[0]["aqi"], c["aqi"])
        rb = mcp_server.Rollbook(self.db, os.path.join(self.home, "cache", "web_metrics.json"))
        try:
            rb.sync()  # the edits overlay (activities_all), as Server.call does
            out = rb.t_activity({"id": self.AID})
        finally:
            rb.close()
        self.assertEqual(out["weather"]["session"]["ws"], 12.0)
        self.assertEqual(len(out["weather"]["hours"]), self.nhours)
        self.assertNotIn("lat", json.dumps(out["weather"]))


if __name__ == "__main__":
    unittest.main()
