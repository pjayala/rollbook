# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Pedro Ayala. Rollbook is free software, see LICENSE.
"""Laps (regular vs counted, odometer check, min passes), circuit grouping
(app.js via node) and an opt-in cross-format check on the real FIT copy."""

import glob
import json
import math
import os
import shutil
import statistics
import subprocess
import unittest

from helpers import ROOT, load_tool, loop_rows, tmpdir

import server

CIRC = 2 * math.pi * 60.0  # loop_rows default radius: ~377 m
APP_JS = os.path.join(ROOT, "web", "static", "app.js")
REAL_FIT = sorted(glob.glob(os.path.join(ROOT, "data", "cache", "archives", "*", "activities", "21363091415.fit.gz")))


def split(rows):
    return rows, [r[0] for r in rows], [r[6] for r in rows]


def with_stop(laps=6, at_m=2.5 * CIRC, stop_s=180):
    """loop_rows with a stationary stop of stop_s seconds at along-track at_m."""
    rows = loop_rows(laps=laps)
    k = next(i for i, r in enumerate(rows) if r[6] >= at_m)
    stop = [(rows[k][0] + s,) + tuple(rows[k][1:]) for s in range(1, stop_s + 1)]
    tail = [(r[0] + stop_s,) + tuple(r[1:]) for r in rows[k + 1:]]
    return rows[:k + 1] + stop + tail, rows[k][0]


def with_odometer(factor, lo_m=2 * CIRC + 60, hi_m=2 * CIRC + 300, laps=6):
    """loop_rows whose distance channel over-reads by `factor` between lo_m and hi_m of GPS track."""
    rows, out, d = loop_rows(laps=laps), [], 0.0
    for i, r in enumerate(rows):
        if i:
            step = r[6] - rows[i - 1][6]
            d += step * (factor if lo_m < r[6] <= hi_m else 1.0)
        out.append(tuple(r[:6]) + (d,))
    t_mid = next(r[0] for r in rows if r[6] >= (lo_m + hi_m) / 2)
    return out, t_mid


def lap_at(L, t):
    return next(x for x in L["laps"] if x["t0"] <= t < x["t1"])


class StopInLap(unittest.TestCase):
    def setUp(self):
        rows, self.t_stop = with_stop()
        self.L = server.find_laps(*split(rows))
        self.assertIsNotNone(self.L)

    def test_stopped_lap_is_regular_but_not_counted(self):
        lap = lap_at(self.L, self.t_stop)
        self.assertGreater(lap["pause"], 150)
        self.assertGreater(lap["elapsed"], 200)
        self.assertTrue(lap["regular"])
        self.assertFalse(lap["counted"])
        others = [x for x in self.L["laps"] if x is not lap]
        self.assertTrue(others)
        for x in others:
            self.assertTrue(x["counted"], x)
            self.assertLess(x["pause"], 10)

    def test_stats_from_counted_only(self):
        """Mirrors goodLaps()/best_lap_s: stats over counted laps ignore the stop."""
        good = [x for x in self.L["laps"] if x["counted"]]
        el = [x["elapsed"] for x in good]
        lap_s = CIRC / 6
        self.assertAlmostEqual(min(el), lap_s, delta=2)
        self.assertAlmostEqual(max(el), lap_s, delta=2)  # slowest counted lap is not the stopped one
        self.assertAlmostEqual(statistics.median(el), lap_s, delta=2)
        self.assertLess(statistics.pstdev(el) / statistics.mean(el), 0.02)
        every = [x["elapsed"] for x in self.L["laps"]]
        self.assertGreater(statistics.pstdev(every) / statistics.mean(every), 0.3)  # would be skewed
        self.assertAlmostEqual(self.L["lap_km"], CIRC / 1000, delta=0.02)


class OdometerCheck(unittest.TestCase):
    def test_lap_with_odometer_far_off_is_not_regular(self):
        rows, t_mid = with_odometer(2.0)  # +240 m on a 377 m lap: ~+64%
        L = server.find_laps(*split(rows))
        self.assertIsNotNone(L)
        bad = lap_at(L, t_mid)
        self.assertGreater(bad["km"], 1.25 * CIRC / 1000)
        self.assertFalse(bad["regular"])
        self.assertFalse(bad["counted"])
        for x in L["laps"]:
            if x is not bad:
                self.assertTrue(x["regular"], x)
        self.assertAlmostEqual(L["lap_km"], CIRC / 1000, delta=0.02)

    def test_small_odometer_drift_stays_regular(self):
        rows, t_mid = with_odometer(1.1)  # +24 m: ~+6%
        L = server.find_laps(*split(rows))
        self.assertIsNotNone(L)
        self.assertTrue(lap_at(L, t_mid)["regular"])
        self.assertTrue(all(x["counted"] for x in L["laps"]))


class MinPasses(unittest.TestCase):
    def test_two_passes_is_not_laps(self):
        # 1.8 laps of 754 m: long enough to pass the 3*LAP_MIN_M guard, but every gate sees <= 2 passes
        # (exactly 2.0 laps ends on the start gate, which is a 3rd pass)
        self.assertIsNone(server.find_laps(*split(loop_rows(laps=1.8, radius_m=120))))

    def test_three_passes_is_two_laps(self):
        L = server.find_laps(*split(loop_rows(laps=2.2, radius_m=120)))
        self.assertIsNotNone(L)
        self.assertEqual(len(L["laps"]), 2)
        self.assertAlmostEqual(L["lap_km"], 2 * math.pi * 120 / 1000, delta=0.02)

    def test_too_short_track(self):
        self.assertIsNone(server.find_laps(*split(loop_rows(laps=1, radius_m=60))))


NODE_HARNESS = r"""
const fs = require("fs");
const src = fs.readFileSync(process.argv[2], "utf8").replace(/\r\n/g, "\n");
const acts = JSON.parse(fs.readFileSync(process.argv[3], "utf8"));
const i = src.indexOf("function circuits(");
const j = src.indexOf("\n}\n", i);
if (i < 0 || j < 0) throw new Error("circuits() not found in app.js");
let captured = null;
const median = a => { const s = [...a].sort((x, y) => x - y), m = s.length >> 1; return s.length % 2 ? s[m] : (s[m - 1] + s[m]) / 2; };
const stubs = { $: () => ({}), chart: (h, o) => { captured = o; }, median,
  f0: String, f2: String, dur: String, fdate: String, esc: String, go: () => {}, DAY: 86400000,
  CIRC_COLORS: ["a", "b", "c", "d", "e", "f"] };
const circuits = new Function(...Object.keys(stubs), src.slice(i, j + 2) + "\nreturn circuits;")(...Object.values(stubs));
const host = { hidden: false, innerHTML: "" };
circuits(host, acts.map(a => Object.assign({}, a, { _d: new Date(a._t) })));
const groups = captured ? captured.series.filter(s => s.kind === "dot").map(s => s.data.map(p => p.ref).sort()) : [];
process.stdout.write(JSON.stringify({ hidden: host.hidden, groups }));
"""


@unittest.skipUnless(shutil.which("node"), "node not installed")
class Circuits(unittest.TestCase):
    LAT, LON = 45.0, 7.0

    def act(self, id, km=0.4, north_m=0.0, east_m=0.0, center=True):
        # same metres->degrees as app.js near(): 111320 m/deg, cos(lat) east
        c = [self.LAT + north_m / 111320, self.LON + east_m / (111320 * math.cos(math.radians(self.LAT)))]
        return {"id": id, "laps_n": 5, "lap_km": km, "lap_center": c if center else None,
                "best_lap_s": 60, "best_lap_kmh": 20, "_t": 1700000000000 + id}

    def groups(self, acts):
        d = tmpdir(self)
        js, data = os.path.join(d, "h.js"), os.path.join(d, "acts.json")
        with open(js, "w", encoding="utf-8") as f:
            f.write(NODE_HARNESS)
        with open(data, "w", encoding="utf-8") as f:
            json.dump(acts, f)
        p = subprocess.run([shutil.which("node"), js, APP_JS, data], capture_output=True, text=True, timeout=60)
        self.assertEqual(p.returncode, 0, p.stderr)
        return sorted(json.loads(p.stdout)["groups"])

    def test_centre_within_25pct_of_lap_groups(self):  # 0.4 km -> 100 m
        self.assertEqual(self.groups([self.act(1), self.act(2, north_m=90)]), [[1, 2]])
        self.assertEqual(self.groups([self.act(1), self.act(2, east_m=90)]), [[1, 2]])

    def test_centre_beyond_25pct_separates(self):
        self.assertEqual(self.groups([self.act(1), self.act(2, north_m=110)]), [[1], [2]])
        self.assertEqual(self.groups([self.act(1), self.act(2, east_m=110)]), [[1], [2]])

    def test_60m_floor_for_small_laps(self):  # 0.19 km -> 25% = 47.5 m, floor 60 m
        self.assertEqual(self.groups([self.act(1, km=0.19), self.act(2, km=0.19, north_m=55)]), [[1, 2]])
        self.assertEqual(self.groups([self.act(1, km=0.19), self.act(2, km=0.19, north_m=65)]), [[1], [2]])

    def test_lap_length_within_12pct(self):
        self.assertEqual(self.groups([self.act(1), self.act(2, km=0.44)]), [[1, 2]])
        self.assertEqual(self.groups([self.act(1), self.act(2, km=0.46)]), [[1], [2]])

    def test_sessions_without_centre_ignored(self):
        self.assertEqual(self.groups([self.act(1), self.act(2), self.act(3, center=False)]), [[1, 2]])


@unittest.skipUnless(REAL_FIT, "real export not present (data/cache/archives/*/activities/21363091415.fit.gz)")
class RealFit(unittest.TestCase):
    def test_big_endian_fit_matches_gpx_fixture(self):
        rb = load_tool()
        _, s = rb.load_file(REAL_FIT[0])
        c = rb.core(s)
        self.assertEqual(f"{c['km']:.3f}", "18.205")
        self.assertEqual(rb.hm(c["elapsed"]), "55:02")
        self.assertEqual(rb.hm(c["moving"]), "51:08")


if __name__ == "__main__":
    unittest.main()
