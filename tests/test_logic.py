# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Pedro Ayala. Rollbook is free software, see LICENSE.
"""Dashboard logic that doesn't need a database: laps, tracks, conditions, names."""

import json
import math
import os
import unittest

from helpers import loop_rows, tmpdir

import geonames
import importer
import segments
import server


class Laps(unittest.TestCase):
    def rows(self, **kw):
        rows = loop_rows(**kw)
        return rows, [r[0] for r in rows], [r[6] for r in rows]

    def test_finds_a_circuit(self):
        rows, t, d = self.rows(laps=6, radius_m=60)  # ~377 m loop
        L = server.find_laps(rows, t, d)
        self.assertIsNotNone(L)
        self.assertGreaterEqual(len(L["laps"]), 5)
        self.assertAlmostEqual(L["lap_km"], 2 * math.pi * 60 / 1000, delta=0.02)
        for lap in L["laps"]:
            self.assertTrue(lap["counted"])
            self.assertAlmostEqual(lap["elapsed"], 2 * math.pi * 60 / 6, delta=2)

    def test_straight_line_has_no_laps(self):
        rows = [(i, 10.0 + i * 6 / 110540, 20.0, 50.0, 140, None, i * 6.0) for i in range(1500)]
        self.assertIsNone(server.find_laps(rows, [r[0] for r in rows], [r[6] for r in rows]))

    def test_out_and_back_is_not_laps(self):
        """Return legs pass the same points heading the other way: never laps."""
        out = [(10.0 + i * 6 / 110540, 20.0) for i in range(800)]
        path = out + out[::-1]
        rows = [(i, la, lo, 50.0, 140, None, i * 6.0) for i, (la, lo) in enumerate(path)]
        self.assertIsNone(server.find_laps(rows, [r[0] for r in rows], [r[6] for r in rows]))


class Tracks(unittest.TestCase):
    def test_compact_track_round_trip(self):
        rows = loop_rows(laps=1, radius_m=200)
        flat = server.compact_track(rows)
        pts = [p for p in segments.decode(flat) if p]
        self.assertGreater(len(pts), 50)
        # every decoded point lies on the circle (within 1e-5 deg quantisation)
        for la, lo in pts:
            self.assertTrue(any(abs(la - r[1]) < 2e-5 and abs(lo - r[2]) < 2e-5 for r in rows))

    def test_pen_up_on_jump(self):
        rows = [(0, 10.0, 20.0, 0, 0, 0, 0), (1, 10.0001, 20.0, 0, 0, 0, 0), (2, 10.0003, 20.0, 0, 0, 0, 0),
                (3, 10.02, 20.0, 0, 0, 0, 0), (4, 10.0202, 20.0, 0, 0, 0, 0)]
        self.assertIn(None, segments.decode(server.compact_track(rows)))

    def test_best_window(self):
        t = list(range(0, 1201))
        d = [i * 5.0 if i < 600 else 3000 + (i - 600) * 7.0 for i in t]  # 18 then 25.2 km/h
        self.assertAlmostEqual(server.best_window(t, d, 0, len(t) - 1, 600), 25.2, places=1)

    def test_power_grid_and_bests(self):
        # 10 min at 200 W, then 5 min at 300 W, sampled every 2 s, then a 60 s pause
        rows = [(t, None, None, None, None, None, t * 5.0, 200 if t < 600 else 300) for t in range(0, 900, 2)]
        rows.append((960, None, None, None, None, None, 4500.0, 100))
        t = [r[0] for r in rows]
        g = server.power_grid(rows, t)
        self.assertEqual(len(g), 961)
        self.assertEqual((g[1], g[601], g[905], g[930], g[960]), (200, 300, 300, 0, 100))  # held 2 s; >10 s gap = 0 W
        self.assertAlmostEqual(server.best_power(g, 300), 300)
        self.assertAlmostEqual(server.best_power(g, 600), (292 * 200 + 308 * 300) / 600)  # last reading held 10 s
        self.assertIsNone(server.best_power(g, 3600))
        self.assertGreater(server.normalized_power(g), sum(g) / len(g))  # variable effort: NP > average
        self.assertIsNone(server.power_grid([r[:7] for r in rows], t))  # no power column (older DB / tests)

    def test_zero_power_and_histogram(self):
        # 1 Hz: 100 s at 200 W, 20 s coasting at a recorded 0 W, a 60 s gap (no readings), 50 s at 100 W
        rows = [(i, None, None, None, None, None, 0.0, 200 if i < 100 else 0) for i in range(120)]
        rows += [(i, None, None, None, None, None, 0.0, 100) for i in range(180, 230)]
        t = [r[0] for r in rows]
        self.assertEqual(server.zero_power_s(rows, t), 19 + 10)  # 0 W at 100..119; the last holds 10 s into the gap
        g = server.power_grid(rows, t)
        h = server.power_hist(g)
        self.assertEqual(sum(h.values()), len(g))
        self.assertEqual((h[200], h[100]), (100, 50))
        self.assertIsNone(server.zero_power_s([r[:7] for r in rows], t))

    def test_ele_placeholder(self):
        rows = [(i, 10.0, 20.0, 150.0 if i % 5 == 0 else -1.0, None, None, i) for i in range(300)]
        self.assertEqual(server._ele_placeholder(rows), -1.0)
        flat = [(i, 10.0, 20.0, 1.0, None, None, i) for i in range(300)]
        self.assertIsNone(server._ele_placeholder(flat))


class Conditions(unittest.TestCase):
    def test_slope_sign_and_size(self):
        down = server.slope_pct("Inline Skate", 24, -0.1)
        self.assertGreater(down, 1.5)  # a skater gains ~2.5% on -0.1%
        self.assertLess(down, 4)
        self.assertAlmostEqual(server.slope_pct("Inline Skate", 24, 0.1), -down, places=6)
        self.assertAlmostEqual(server.slope_pct("Run", 12, 1.0), -3.0)
        self.assertEqual(server.slope_pct("Inline Skate", 24, None), 0.0)

    def test_tailwind(self):
        # wind from the west (270) pushes an eastbound (90) leg
        self.assertAlmostEqual(server.tailwind({"ws": 10, "wd": 270}, 90), 10, places=6)
        self.assertAlmostEqual(server.tailwind({"ws": 10, "wd": 270}, 270), -10, places=6)
        self.assertAlmostEqual(server.tailwind({"ws": 10, "wd": 0}, 90), 0, places=6)

    def test_fit_wind_recovers_coefficient(self):
        rows = []
        for i in range(60):
            tw = (i % 11) - 5.0  # tailwind on out leg; back gets -tw
            gap = 2.0 + 0.5 * (2 * tw)
            rows.append({"id": i, "cond": {"legs": {"out": {"tail": tw}, "back": {"tail": -tw}},
                                           "gap_pct": gap, "gap_slope": 0.0}})
        self.assertAlmostEqual(server.fit_wind("Inline Skate", rows, {}), 0.5, places=6)

    def test_fit_wind_needs_data(self):
        self.assertIsNone(server.fit_wind("Inline Skate", [], {}))

    def test_virtual_sessions_get_no_weather(self):
        self.assertIsNone(server.conditions({"type": "Virtual Ride", "datetime": "2026-01-01T08:00:00+00:00"},
                                            {"ll0": [10, 20], "route": "virtual"}))


class Names(unittest.TestCase):
    def test_sample_keeps_ends_and_caps_points(self):
        pts = [[10.0 + i * 0.001, 20.0] for i in range(200)]  # ~22 km
        s = geonames.sample(pts)
        self.assertEqual(s[0], pts[0])
        self.assertEqual(s[-1], pts[-1])
        self.assertLessEqual(len(s), geonames.MAX_POINTS)

    def test_describe(self):
        class Fake:
            def lookup(self, lat, lon):
                return {"road": "Main St", "area": "North" if lat > 10.05 else "South", "city": "Town"}
        seg = {"pts": [[10.0 + i * 0.001, 20.0] for i in range(100)]}
        p = geonames.describe(Fake(), seg)
        self.assertEqual((p["from"], p["to"], p["road"], p["city"]), ("South", "North", "Main St", "Town"))
        self.assertTrue(p["done"])

    def test_safe_upload_names(self):
        self.assertEqual(importer.safe_name("../../etc/passwd"), "passwd")
        self.assertEqual(importer.safe_name("C:\\x\\My ride.fit"), "My_ride.fit")
        self.assertEqual(importer.safe_name(".hidden"), "hidden")
        self.assertEqual(importer.safe_name(""), "upload")


class Favs(unittest.TestCase):
    def test_round_trip_and_validation(self):
        p = os.path.join(tmpdir(self), "favorites.json")
        f = server.Favorites(p)
        f.set("sessions", "123", True)
        f.set("segments", "abc123", True, {"type": "Run", "m": 1000, "evil": "x"})
        again = server.Favorites(p).get()
        self.assertIn("123", again["sessions"])
        self.assertEqual(again["segments"]["abc123"]["m"], 1000)
        self.assertNotIn("evil", again["segments"]["abc123"])
        f.set("sessions", "123", False)
        self.assertNotIn("123", server.Favorites(p).get()["sessions"])
        for kind, key in (("other", "1"), ("sessions", "../x"), ("sessions", "a" * 40)):
            with self.assertRaises(ValueError):
                f.set(kind, key, True)


class Segments(unittest.TestCase):
    def test_shared_route_becomes_segments_with_efforts(self):
        # 6 sessions on the same 3 km straight road, out only
        tracks = []
        for k in range(6):
            pts = [(10.0 + i * 15 / 110540 + k * 2e-6, 20.0 + k * 2e-6) for i in range(200)]
            tracks.append((k, pts))
        segs = segments.generate(tracks)
        self.assertTrue(segs)
        self.assertTrue(all(s["kind"] == "line" for s in segs))
        self.assertAlmostEqual(max(s["m"] for s in segs), 199 * 15, delta=300)
        # direction: one-way traffic only yields northbound segments
        self.assertTrue(all(s["bearing"] < 20 or s["bearing"] > 340 for s in segs))


if __name__ == "__main__":
    unittest.main()
