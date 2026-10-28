# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Pedro Ayala. Rollbook is free software, see LICENSE.
"""Compare two sessions (#16): the pure helpers of compare.js and app.js's sameRoute, run under node."""

import json
import os
import shutil
import subprocess
import unittest

from helpers import ROOT, tmpdir
from test_frontend_power import _src, extract_const, extract_function

NODE = shutil.which("node")
COMPARE_JS = os.path.join(ROOT, "web", "static", "compare.js")
FUNCS = ["movingClock", "cmpTrack", "fracIndex", "lerpAt", "atDistance", "alignAt", "timeGap",
         "gapStats", "wonWhere", "fgap", "cmpSplits", "pickToggle", "parseCompareHash", "llDist"]


def rows(t, km, mv=None):
    """Series rows [t, km, kmh, hr, ele, lat, lon, watts, mv]; mv None = left for the browser."""
    return [[a, b, None, None, None, None, None, None, None if mv is None else mv[i]] for i, (a, b) in enumerate(zip(t, km))]


def steady(ms, km_total, dt=1, pause_at=None, pause_s=0):
    """Constant speed `ms` m/s to km_total, optionally standing still pause_s at pause_at km."""
    t, km, now, d, paused = [], [], 0, 0.0, False
    while d < km_total - 1e-9:
        t.append(now); km.append(round(d, 6))
        if pause_at is not None and not paused and d >= pause_at - 1e-9:
            paused = True
            for _ in range(int(pause_s // dt)):
                now += dt; t.append(now); km.append(round(d, 6))
        now += dt; d = min(km_total, d + ms * dt / 1000)
    t.append(now); km.append(round(d, 6))
    return rows(t, km)


@unittest.skipUnless(NODE, "node not installed")
class Compare(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        app = _src()
        with open(COMPARE_JS, encoding="utf-8") as f:
            cmp = f.read().replace("\r\n", "\n")
        cls.prelude = "\n".join([extract_const(app, "isNum"), "const CMP_THR = 0.5;", extract_const(cmp, "cmpClock"),
                                 *[extract_function(cmp, n) for n in FUNCS], extract_function(app, "sameRoute")])

    def js(self, expr):
        path = os.path.join(tmpdir(self), "h.js")
        with open(path, "w", encoding="utf-8") as f:
            f.write(self.prelude + "\nprocess.stdout.write(JSON.stringify((() => { " + expr + " })()));\n")
        p = subprocess.run([NODE, path], capture_output=True, encoding="utf-8", timeout=60)
        self.assertEqual(p.returncode, 0, p.stderr)
        return json.loads(p.stdout)

    def gap(self, a, b, mode="moving", step=0.1):
        return self.js(f"return timeGap(cmpTrack({json.dumps(a)}), cmpTrack({json.dumps(b)}), {json.dumps(mode)}, {step});")

    # ---- clocks

    def test_moving_clock_drops_a_pause(self):
        self.assertEqual(self.js("return movingClock([0,10,20,30,90,100], [0,.05,.10,.15,.15,.20]);"), [0, 10, 20, 30, 30, 40])

    def test_threshold_is_inclusive_like_db_build(self):
        # 5 m in 10 s is exactly 0.5 m/s: moving (>=), 4.9 m is not
        self.assertEqual(self.js("return [movingClock([0,10],[0,.005])[1], movingClock([0,10],[0,.0049])[1]];"), [10, 0])

    def test_duplicate_timestamps_are_harmless(self):
        self.assertEqual(self.js("return movingClock([0,10,10,20], [0,.01,.02,.03]);"), [0, 10, 10, 20])

    def test_server_moving_column_wins_over_recomputing(self):
        T = self.js(f"return cmpTrack({json.dumps(rows([0, 10, 20], [0, .05, .1], mv=[0, 3, 7]))}).mv;")
        self.assertEqual(T, [0, 3, 7])

    # ---- time at a distance

    def test_first_arrival_on_a_plateau(self):
        T = rows([0, 20, 80, 100], [0, .1, .1, .2])
        got = self.js(f"const T = cmpTrack({json.dumps(T)}); return [.1, .15, .25, -.1].map(x => atDistance(T, 'elapsed', x));")
        self.assertEqual(got[:2], [20, 90])
        self.assertEqual(got[2:], [None, None])

    def test_gps_jitter_backwards_counts_from_the_first_arrival(self):
        T = rows([0, 10, 20, 30], [0, .1, .09, .2])
        self.assertAlmostEqual(self.js(f"return atDistance(cmpTrack({json.dumps(T)}), 'elapsed', .095);"), 9.5)

    # ---- the gap

    def test_identical_sessions_have_no_gap(self):
        s = steady(5, 1.0)
        self.assertTrue(all(abs(p["g"]) < 1e-9 for p in self.gap(s, s)))

    def test_faster_reference_gives_a_linear_negative_gap(self):
        a, b = steady(5, 1.0), steady(6, 1.0)  # the reference is faster: the subject falls behind
        g = {round(p["x"], 3): p["g"] for p in self.gap(a, b)}
        self.assertAlmostEqual(g[0.5], -16.667, delta=0.05)
        self.assertAlmostEqual(g[0.9], -30.0, delta=0.05)  # 180 s against 150 s
        self.assertAlmostEqual(g[0.9] / g[0.3], 3, delta=0.01)  # linear in distance

    def test_gap_stops_at_the_shorter_session(self):
        pts = self.gap(steady(5, 1.0), steady(5, 0.6))
        self.assertAlmostEqual(pts[-1]["x"], 0.6, places=6)
        self.assertLessEqual(max(p["x"] for p in pts), 0.6 + 1e-9)

    def test_different_downsampling_steps_agree(self):
        full = steady(5, 2.0)
        thin = full[::5] + ([full[-1]] if (len(full) - 1) % 5 else [])
        self.assertTrue(all(abs(p["g"]) <= 0.5 for p in self.gap(full, thin)))

    def test_a_stop_counts_on_the_elapsed_clock_only(self):
        a, b = steady(5, 1.0), steady(5, 1.0, pause_at=0.5, pause_s=60)
        end = lambda mode: self.gap(a, b, mode)[-1]["g"]
        self.assertAlmostEqual(end("elapsed"), 60, delta=1.01)
        self.assertAlmostEqual(end("moving"), 0, delta=1.01)

    def test_gap_stats(self):
        pts = [{"x": i / 10, "g": g} for i, g in enumerate([0, 5, 10, 2, -10, -25, -15, -5])]
        st = self.js(f"return gapStats({json.dumps(pts)});")
        self.assertEqual(st["final"], -5)
        self.assertEqual(st["max"], {"x": 0.5, "g": -25})
        self.assertEqual(st["cross"], [{"x": 0.4, "to": -1}])

    def test_won_where_names_the_stretch_that_made_the_gap(self):
        pts = [{"x": i * .5, "g": g} for i, g in enumerate([0, 0, 0, 0, -20, -40, -40, -40, -40])]  # 0..4 km
        w = self.js(f"return wonWhere({json.dumps(pts)});")
        self.assertEqual((w["x0"], w["x1"], w["d"]), (1.5, 2.5, -40))
        self.assertIsNone(self.js("return wonWhere([{x:0,g:0},{x:1,g:2}]);"))  # under 5 s: nothing to explain

    # ---- alignment

    def test_align_at_a_gate_rezeroes_time_distance_and_moving_clock(self):
        T = rows([0, 10, 20, 30, 90, 100], [0, .05, .10, .15, .15, .20])
        a = self.js(f"return alignAt(cmpTrack({json.dumps(T)}), 25);")
        self.assertEqual(a["t"][0], 0)
        self.assertEqual(a["km"][0], 0)
        self.assertEqual(a["mv"][0], 0)
        self.assertEqual(a["t"][1], 5)  # old t=30
        self.assertAlmostEqual(a["km"][1], 0.025)
        self.assertEqual(len(a["t"]), 4)  # rows before t0 dropped
        self.assertEqual(a["mv"][-1], 15)  # 40 - 25

    def test_align_on_an_existing_row(self):
        T = rows([0, 10, 20], [0, .05, .10])
        a = self.js(f"return alignAt(cmpTrack({json.dumps(T)}), 10);")
        self.assertEqual((a["t"], a["km"]), ([0, 10], [0, 0.05]))

    # ---- splits add up to the gap

    def test_splits_add_up_to_the_final_gap(self):
        a, b = steady(5.5, 2.37), steady(5, 2.5, pause_at=1.2, pause_s=40)
        out = self.js(f"""const A = cmpTrack({json.dumps(a)}), B = cmpTrack({json.dumps(b)}), s = cmpSplits(A, B, 'elapsed');
            return {{ rows: s, total: s.reduce((t, r) => t + r.d, 0), end: Math.round(atDistance(A, 'elapsed', 2.37)) - Math.round(atDistance(B, 'elapsed', 2.37)) }};""")
        self.assertEqual([round(r["km"], 2) for r in out["rows"]], [1, 2, 2.37])
        self.assertEqual(out["total"], out["end"])

    # ---- formatting, selection, links

    def test_fgap(self):
        self.assertEqual(self.js("return [-72, 72, 0.4, -3725, null].map(fgap);"), ["−1:12", "+1:12", "0:00", "−1:02:05", "–"])

    def test_pick_toggle_keeps_the_last_two(self):
        got = self.js("return [pickToggle([], 1), pickToggle([1], 2), pickToggle([1, 2], 3), pickToggle([1, 2], 1)];")
        self.assertEqual(got, [[1], [1, 2], [2, 3], [2]])

    def test_parse_compare_hash(self):
        got = self.js("return [['12','34'], ['12',undefined], ['12','12'], ['ab','3'], ['1'.repeat(16),'2'], ['1<img>','2']].map(([a,b]) => parseCompareHash(a,b));")
        self.assertEqual(got[0], {"a": 12, "b": 34})
        self.assertTrue(all("error" in g for g in got[1:]))
        self.assertIn("different", got[2]["error"])

    def test_ll_dist(self):
        self.assertAlmostEqual(self.js("return llDist(52, 5, 52.001, 5);"), 111.2, delta=0.5)

    # ---- sameRoute: the filter the session page always used, moved into a function

    def test_same_route_characterisation(self):
        a = {"id": 1, "km": 8.0}
        m = {"route": "out-back", "crow_km": 3.0, "bearing_out": 359}
        peers = [
            {"id": 1, "km": 8, "route": "out-back", "crow_km": 3, "bearing_out": 359},           # itself
            {"id": 2, "km": 8, "route": "loop", "crow_km": 3, "bearing_out": 359},               # other kind
            {"id": 3, "km": 8, "route": "out-back", "crow_km": 3.59, "bearing_out": 359},        # 0.59 < max(0.6, 0.36): in
            {"id": 4, "km": 8, "route": "out-back", "crow_km": 3.61, "bearing_out": 359},        # out
            {"id": 5, "km": 8, "route": "out-back", "crow_km": 3, "bearing_out": 5},             # 6 degrees: in
            {"id": 6, "km": 8, "route": "out-back", "crow_km": 3, "bearing_out": 30},            # 31 degrees: out
            {"id": 7, "km": 9.9, "route": "out-back", "crow_km": 3, "bearing_out": 359},         # 1.9 km < 2: in
            {"id": 8, "km": 10.1, "route": "out-back", "crow_km": 3, "bearing_out": 359},        # out
            {"id": 9, "km": 8, "route": "out-back", "crow_km": None, "bearing_out": 359},        # no metrics yet: out
        ]
        self.assertEqual(self.js(f"return sameRoute({json.dumps(a)}, {json.dumps(m)}, {json.dumps(peers)}).map(p => p.id);"), [3, 5, 7])
        indoor = self.js(f"return sameRoute({json.dumps(a)}, {{route: 'indoor', crow_km: 0}}, [{{id: 2, km: 8, route: 'indoor', crow_km: 0}}]).length;")
        self.assertEqual(indoor, 0)


if __name__ == "__main__":
    unittest.main()
