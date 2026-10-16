# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Pedro Ayala. Rollbook is free software, see LICENSE.
"""Weather cache (web/weather.py) and the wind / slope split in server.conditions(). No network."""

import datetime as dt
import io
import json
import os
import socket
import threading
import types
import unittest
import urllib.error
import urllib.parse
from unittest import mock

from helpers import tmpdir

import server
import weather

UTC = dt.timezone.utc
NOW = dt.datetime(2026, 10, 16, 12, 0, tzinfo=UTC)
TODAY = NOW.date()
CELL = "10.0,20.0"


class _Stop(Exception):
    pass


class _OneShot:
    """Replaces Weather.wake: _worker() runs exactly one batch, then stops."""

    def __init__(self):
        self.calls = 0

    def set(self):
        pass

    def clear(self):
        pass

    def wait(self, timeout=None):
        self.calls += 1
        if self.calls > 1:
            raise _Stop


def _day(ws=10.0):
    if ws is None:
        return [[float(h), None, None, None] for h in range(24)]
    return [[float(h), ws + h, 270.0, ws + h + 5] for h in range(24)]


def _hourly(dates, ws=10.0):
    out = {"time": [], **{v: [] for v in weather.VARS}}
    for d in dates:
        for h, row in enumerate(_day(ws)):
            out["time"].append(f"{d.isoformat()}T{h:02d}:00")
            for v, x in zip(weather.VARS, row):
                out[v].append(x)
    return {"hourly": out}


def when(d, h=8, m=0):
    return dt.datetime(d.year, d.month, d.day, h, m, tzinfo=UTC)


class Responder:
    """Fake urlopen: records requests and answers like Open-Meteo for the dates asked."""

    def __init__(self, ws=10.0, body=None, exc=None):
        self.reqs, self.timeouts, self.ws, self.body, self.exc = [], [], ws, body, exc

    def __call__(self, req, timeout=None):
        self.reqs.append(req)
        self.timeouts.append(timeout)
        if self.exc is not None:
            raise self.exc
        if self.body is not None:
            return io.BytesIO(self.body)
        q = self.query(-1)
        if "start_date" in q:
            a, b = dt.date.fromisoformat(q["start_date"]), dt.date.fromisoformat(q["end_date"])
        else:
            a = TODAY - dt.timedelta(days=int(q["past_days"]))
            b = TODAY + dt.timedelta(days=int(q["forecast_days"]) - 1)
        days = [a + dt.timedelta(days=i) for i in range((b - a).days + 1)]
        return io.BytesIO(json.dumps(_hourly(days, self.ws)).encode())

    def url(self, i):
        return self.reqs[i].full_url.split("?", 1)[0]

    def query(self, i):
        return dict(urllib.parse.parse_qsl(self.reqs[i].full_url.split("?", 1)[1]))


def _no_net(*a, **k):
    raise AssertionError("real network access attempted")


class WeatherBase(unittest.TestCase):
    def setUp(self):
        self.path = os.path.join(tmpdir(self), "cache", "weather.json")
        self.threads = mock.Mock()
        self.sleep = mock.Mock()
        fake_threading = types.SimpleNamespace(Lock=threading.Lock, Event=threading.Event, Thread=self.threads)
        fake_dt = types.SimpleNamespace(datetime=types.SimpleNamespace(now=lambda tz=None: NOW),
                                        date=dt.date, timezone=dt.timezone, timedelta=dt.timedelta)
        for p in (mock.patch.object(weather, "threading", fake_threading),
                  mock.patch.object(weather, "time", types.SimpleNamespace(sleep=self.sleep)),
                  mock.patch.object(weather, "dt", fake_dt),
                  mock.patch("urllib.request.urlopen", _no_net),
                  mock.patch.object(socket.socket, "connect", _no_net)):
            p.start()
            self.addCleanup(p.stop)

    def net(self, resp):
        p = mock.patch("urllib.request.urlopen", resp)
        p.start()
        self.addCleanup(p.stop)
        return resp

    def make(self, offline=False):
        w = weather.Weather(self.path, offline=offline)
        w.wake = _OneShot()
        return w

    def run_worker(self, w):
        with self.assertRaises(_Stop):
            w._worker()

    def seed(self, data):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        with open(self.path, "w") as f:
            json.dump(data, f)


class CellAndDay(WeatherBase):
    def test_cell_of(self):
        self.assertEqual(weather.cell_of(10.04, 20.06), "10.0,20.1")
        self.assertEqual(weather.cell_of(10.049, 19.96), CELL)
        self.assertNotEqual(weather.cell_of(10.06, 20.0), weather.cell_of(10.04, 20.0))
        self.assertEqual(weather.cell_of(-33.87, 151.21), "-33.9,151.2")
        self.assertEqual(weather.cell_of(-0.04, -0.04), "0.0,0.0")  # no "-0.0" key

    def test_same_cell_shares_cache(self):
        d = TODAY - dt.timedelta(days=30)
        self.seed({CELL: {d.isoformat(): _day()}})
        w = self.make(offline=True)
        self.assertEqual(w.at(10.03, 19.98, when(d))["ws"], 18.0)
        self.assertIsNone(w.at(10.2, 20.0, when(d)))

    def test_nearest_hour_and_utc_day(self):
        d = TODAY - dt.timedelta(days=30)
        self.seed({CELL: {d.isoformat(): _day()}})
        w = self.make(offline=True)
        self.assertEqual(w.at(10, 20, when(d, 10, 29))["temp"], 10.0)
        self.assertEqual(w.at(10, 20, when(d, 10, 30))["temp"], 11.0)
        self.assertIsNone(w.at(10, 20, when(d, 23, 40)))  # rounds into the next UTC day
        nxt = d + dt.timedelta(days=1)
        plus2 = dt.datetime(nxt.year, nxt.month, nxt.day, 0, 10, tzinfo=dt.timezone(dt.timedelta(hours=2)))
        self.assertEqual(w.at(10, 20, plus2)["temp"], 22.0)  # 22:10 UTC on d

        online = self.make()
        self.assertIsNone(online.at(10, 20, when(d, 23, 40)))
        self.assertEqual(online.queued, {CELL: {nxt.isoformat()}})

    def test_queue_dedup(self):
        w = self.make()
        d = TODAY - dt.timedelta(days=30)
        w.at(10, 20, when(d, 8))
        w.at(10.01, 20, when(d, 15))
        self.assertEqual(w.pending(), 1)
        w.at(10, 20, when(d - dt.timedelta(days=1)))
        w.at(11, 20, when(d))
        self.assertEqual(w.pending(), 3)


class Cache(WeatherBase):
    def test_hit_no_fetch(self):
        d = TODAY - dt.timedelta(days=30)
        self.seed({CELL: {d.isoformat(): _day()}})
        resp = self.net(Responder())
        w = self.make()
        self.assertEqual(w.at(10, 20, when(d)), {"temp": 8.0, "ws": 18.0, "wd": 270.0, "gust": 23.0})
        self.assertEqual(w.pending(), 0)
        self.run_worker(w)
        self.assertEqual(resp.reqs, [])

    def test_miss_one_fetch_persisted_and_reloaded(self):
        d = TODAY - dt.timedelta(days=30)
        resp = self.net(Responder())
        w = self.make()
        self.threads.assert_called_once_with(target=w._worker, daemon=True)
        self.assertIsNone(w.at(10, 20, when(d)))
        self.assertIsNone(w.at(10, 20, when(d, 9)))
        self.assertFalse(os.path.exists(self.path))
        self.run_worker(w)

        self.assertEqual(len(resp.reqs), 1)
        self.assertEqual(resp.url(0), weather.ARCHIVE)
        q = resp.query(0)
        self.assertEqual((q["latitude"], q["longitude"]), ("10.0", "20.0"))
        self.assertEqual((q["start_date"], q["end_date"]), (d.isoformat(), d.isoformat()))
        self.assertEqual(q["hourly"], ",".join(weather.VARS))
        self.assertEqual((q["timezone"], q["wind_speed_unit"]), ("UTC", "kmh"))
        self.assertEqual(resp.reqs[0].get_header("User-agent"), weather.UA)
        self.assertEqual(resp.timeouts, [60])
        self.assertEqual(w.pending(), 0)
        self.assertEqual(w.at(10, 20, when(d))["ws"], 18.0)
        self.sleep.assert_called_with(weather.GAP_S)

        self.assertFalse(os.path.exists(self.path + ".tmp"))
        with open(self.path) as f:
            self.assertEqual(len(json.load(f)[CELL][d.isoformat()]), 24)
        fresh = weather.Weather(self.path, offline=True)
        self.assertEqual(fresh.at(10, 20, when(d)), w.at(10, 20, when(d)))

    def test_batch_per_cell_and_year_chunks(self):
        resp = self.net(Responder())
        w = self.make()
        a = TODAY - dt.timedelta(days=800)
        w.at(10, 20, when(a))
        w.at(10, 20, when(a + dt.timedelta(days=365)))
        w.at(11, 20, when(a))
        self.run_worker(w)
        self.assertEqual(len(resp.reqs), 2)  # one per cell
        mine = [resp.query(i) for i in range(2) if resp.query(i)["latitude"] == "10.0"][0]
        self.assertEqual(mine["end_date"], (a + dt.timedelta(days=365)).isoformat())

        resp2 = self.net(Responder())
        w2 = self.make()
        w2.at(12, 20, when(a))  # new cell: w2 shares the cache file, cell 10,20 is already filled
        w2.at(12, 20, when(a + dt.timedelta(days=366)))
        self.run_worker(w2)
        self.assertEqual(len(resp2.reqs), 2)  # > 1 year apart: two archive requests

    def test_corrupt_or_missing_file(self):
        self.assertEqual(self.make(offline=True).data, {})
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        with open(self.path, "w") as f:
            f.write("{not json")
        w = self.make(offline=True)
        self.assertEqual(w.data, {})
        self.assertIsNone(w.at(10, 20, when(TODAY)))


class Offline(WeatherBase):
    def test_offline_never_fetches_or_queues(self):
        d = TODAY - dt.timedelta(days=30)
        self.seed({CELL: {d.isoformat(): _day()}})
        w = self.make(offline=True)
        self.threads.assert_not_called()
        self.assertEqual(w.at(10, 20, when(d))["temp"], 8.0)
        self.assertIsNone(w.at(10, 20, when(d - dt.timedelta(days=1))))
        self.assertIsNone(w.at(50, 5, when(TODAY)))
        self.assertEqual(w.pending(), 0)
        self.assertEqual(w.queued, {})


class ApiChoice(WeatherBase):
    def fetch(self, *days):
        resp = self.net(Responder())
        w = self.make()
        for d in days:
            w.at(10, 20, when(d))
        self.run_worker(w)
        return resp, w

    def test_eight_days_ago_uses_archive(self):
        d = TODAY - dt.timedelta(days=8)
        resp, w = self.fetch(d)
        self.assertEqual([resp.url(0)], [weather.ARCHIVE])
        self.assertEqual(resp.query(0)["start_date"], d.isoformat())

    def test_seven_days_ago_uses_forecast(self):
        d = TODAY - dt.timedelta(days=7)
        resp, w = self.fetch(d)
        self.assertEqual([resp.url(0)], [weather.FORECAST])
        q = resp.query(0)
        self.assertEqual((q["past_days"], q["forecast_days"]), ("8", "1"))
        self.assertNotIn("start_date", q)
        self.assertEqual(w.at(10, 20, when(d))["ws"], 18.0)

    def test_today_uses_forecast(self):
        resp, w = self.fetch(TODAY)
        self.assertEqual(resp.query(0)["past_days"], "1")
        self.assertIsNotNone(w.at(10, 20, when(TODAY)))

    def test_mixed_old_and_recent(self):
        resp, _ = self.fetch(TODAY - dt.timedelta(days=8), TODAY - dt.timedelta(days=2))
        self.assertEqual([resp.url(0), resp.url(1)], [weather.ARCHIVE, weather.FORECAST])
        self.assertEqual(resp.query(1)["past_days"], "3")


class Failures(WeatherBase):
    def assert_not_cached_and_retried(self, resp, d, slept=30):
        w = self.make()
        w.at(10, 20, when(d))
        self.run_worker(w)  # no crash
        self.assertEqual(len(resp.reqs), 1)
        if slept:
            self.sleep.assert_called_once_with(slept)
        self.assertEqual(w.pending(), 0)
        self.assertIsNone(w.at(10, 20, when(d)))  # current behaviour: failure not cached ...
        self.assertEqual(w.pending(), 1)          # ... so the next lookup queues a retry
        return w

    def test_network_error(self):
        resp = self.net(Responder(exc=urllib.error.URLError("down")))
        self.assert_not_cached_and_retried(resp, TODAY - dt.timedelta(days=30))
        self.assertFalse(os.path.exists(self.path))

    def test_http_error(self):
        err = urllib.error.HTTPError(weather.ARCHIVE, 429, "Too Many Requests", {}, None)
        self.assert_not_cached_and_retried(self.net(Responder(exc=err)), TODAY - dt.timedelta(days=30))

    def test_bad_json(self):
        self.assert_not_cached_and_retried(self.net(Responder(body=b"oops")), TODAY - dt.timedelta(days=30))

    def test_missing_hourly(self):
        resp = self.net(Responder(body=b'{"error": true, "reason": "x"}'))
        self.assert_not_cached_and_retried(resp, TODAY - dt.timedelta(days=30))

    def test_all_null_day_not_cached(self):
        resp = self.net(Responder(ws=None))
        self.assert_not_cached_and_retried(resp, TODAY - dt.timedelta(days=30), slept=weather.GAP_S)

    def test_null_hour_in_cached_day_reads_none_without_queueing(self):
        # current behaviour; conditions() then reports {"pending": True} forever (suspected bug)
        d = TODAY - dt.timedelta(days=30)
        day = _day()
        day[8][1] = None
        self.seed({CELL: {d.isoformat(): day}})
        w = self.make()
        self.assertIsNone(w.at(10, 20, when(d, 8)))
        self.assertEqual(w.pending(), 0)
        self.assertIsNotNone(w.at(10, 20, when(d, 9)))

    @unittest.expectedFailure
    def test_malformed_hourly_does_not_kill_worker(self):
        # KeyError on a missing variable is raised outside _fetch's try and ends the worker thread
        body = json.dumps({"hourly": {"time": [f"{TODAY.isoformat()}T00:00"]}}).encode()
        self.net(Responder(body=body))
        w = self.make()
        w.at(10, 20, when(TODAY - dt.timedelta(days=30)))
        self.run_worker(w)


class StubWX:
    def __init__(self, w):
        self.w, self.calls = w, []

    def at(self, lat, lon, t):
        self.calls.append((lat, lon, t))
        return dict(self.w) if self.w else None


class Conditions(unittest.TestCase):
    R = {"type": "Inline Skate", "datetime": "2026-06-01T08:00:00"}
    M = {"ll0": [10.0, 20.0], "route": "out-back", "dur_s": 3600, "t_turn": 1600,
         "out_b10": 26.0, "back_b10": 24.0, "bearing_out": 90}

    def cond(self, wind, r=None, m=None, fits=None):
        self.wx = StubWX(wind)
        with mock.patch.object(server, "WX", self.wx), mock.patch.dict(server.FITS, fits or {}, clear=True):
            return server.conditions(r or self.R, m or self.M)

    def test_indoor_never_queries_weather(self):
        boom = mock.Mock(at=mock.Mock(side_effect=AssertionError("weather queried")))
        with mock.patch.object(server, "WX", boom):
            self.assertIsNone(server.conditions({"type": "Ride", "datetime": self.R["datetime"]},
                                                {"ll0": [10, 20], "route": "indoor"}))
            self.assertIsNone(server.conditions({"type": "Workout", "datetime": self.R["datetime"]}, {"ll0": [10, 20]}))
            self.assertIsNone(server.conditions({"type": "Virtual Run", "datetime": self.R["datetime"]}, {"ll0": [10, 20]}))
            self.assertIsNone(server.conditions(self.R, {"route": "out-back"}))  # no position

    def test_pending(self):
        self.assertEqual(self.cond(None), {"pending": True})

    def test_plain_route_only_weather(self):
        c = self.cond({"temp": 15, "ws": 10, "wd": 270, "gust": 20}, m={**self.M, "route": "line"})
        self.assertEqual(c, {"temp": 15, "ws": 10, "wd": 270, "gust": 20})

    def test_tailwind_out_leg(self):
        c = self.cond({"temp": 15, "ws": 10, "wd": 270, "gust": 20})
        t0 = dt.datetime(2026, 6, 1, 8, 0, tzinfo=UTC)  # naive datetime is read as UTC
        self.assertEqual([t for _, _, t in self.wx.calls],
                         [t0 + dt.timedelta(seconds=s) for s in (1800, 800, 2600)])
        L = c["legs"]
        self.assertAlmostEqual(L["out"]["tail"], 10, places=9)
        self.assertAlmostEqual(L["back"]["tail"], -10, places=9)
        self.assertAlmostEqual(c["wind_k"], server.WIND_K_DEFAULT["Inline Skate"])
        self.assertAlmostEqual(L["out"]["wind_pct"], 4.0, places=9)
        self.assertAlmostEqual(c["gap_pct"], 100 / 12, places=9)
        self.assertAlmostEqual(c["gap_wind"], 8.0, places=9)
        self.assertEqual(c["gap_slope"], 0.0)
        self.assertAlmostEqual(c["gap_other"], 100 / 12 - 8, places=9)
        self.assertAlmostEqual(L["out"]["adj_b10"], 25.0, places=9)
        self.assertAlmostEqual(L["back"]["adj_b10"], 25.0, places=9)
        self.assertAlmostEqual(c["adjusted"], 25.0, places=9)

    def test_headwind_out_leg(self):
        c = self.cond({"temp": 15, "ws": 10, "wd": 90, "gust": 20})
        self.assertAlmostEqual(c["legs"]["out"]["tail"], -10, places=9)
        self.assertAlmostEqual(c["legs"]["out"]["adj_b10"], 26 / 0.96, places=9)
        self.assertAlmostEqual(c["legs"]["back"]["adj_b10"], 24 / 1.04, places=9)
        self.assertAlmostEqual(c["gap_wind"], -8.0, places=9)

    def test_crosswind_is_neutral(self):
        c = self.cond({"temp": 15, "ws": 10, "wd": 0, "gust": 20})
        for leg in ("out", "back"):
            self.assertAlmostEqual(c["legs"][leg]["tail"], 0, places=9)
            self.assertAlmostEqual(c["legs"][leg]["wind_pct"], 0, places=9)
        self.assertAlmostEqual(c["adjusted"], 25.0, places=9)
        self.assertAlmostEqual(c["gap_other"], c["gap_pct"], places=9)

    def test_fitted_k_overrides_default(self):
        c = self.cond({"temp": 15, "ws": 10, "wd": 270, "gust": 20}, fits={"Inline Skate": 1.0})
        self.assertEqual(c["wind_k"], 1.0)
        self.assertAlmostEqual(c["legs"]["out"]["wind_pct"], 10.0, places=9)

    def test_slope_part(self):
        m = {**self.M, "g_out": -0.1, "g_back": 0.1}
        c = self.cond({"temp": 15, "ws": 0, "wd": 0, "gust": 0}, m=m)
        so, sb = server.slope_pct("Inline Skate", 26.0, -0.1), server.slope_pct("Inline Skate", 24.0, 0.1)
        self.assertGreater(so, 0)
        self.assertLess(sb, 0)
        self.assertAlmostEqual(c["gap_slope"], so - sb, places=9)
        self.assertAlmostEqual(c["legs"]["out"]["adj_b10"], 26 / (1 + so / 100), places=9)

    def test_tailwind_quartering(self):
        self.assertAlmostEqual(server.tailwind({"ws": 10, "wd": 225}, 90), 10 * 2 ** -0.5, places=9)


if __name__ == "__main__":
    unittest.main()
