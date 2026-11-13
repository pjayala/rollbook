# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Pedro Ayala. Rollbook is free software, see LICENSE.
"""Session page best speeds (the speed curve): peer context, rank and table, cut out of the dashboard scripts and run under node."""

import json
import os
import shutil
import subprocess
import unittest

from helpers import tmpdir
from test_frontend_power import _src, extract_const, extract_function

NODE = shutil.which("node")
DAY = 86400000
T = 1790000000000  # this session's start, fixed
EVIL = "<img src=x onerror=alert(1)>\"'"


def row(id, days_before, **bw):
    """A list row `days_before` this session; bw given as s600=24.0 etc."""
    t = T - days_before * DAY
    return {"id": id, "_t": t, "datetime": None, "bw": {k[1:]: v for k, v in bw.items()} or None}


@unittest.skipUnless(NODE, "node not installed")
class BestSpeeds(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        src = _src()
        cls.prelude = "\n".join([
            *(extract_const(src, n) for n in ("esc", "isNum", "f0", "f2", "pace", "DAY", "whenOf", "BW", "BW_OLD",
                                               "BW_MIN_PEERS", "bwTicks")),
            extract_function(src, "dur"), extract_function(src, "bwOf"), extract_function(src, "bwContext"),
            extract_function(src, "bwRank"), extract_function(src, "bwTable"),
        ])

    def js(self, expr):
        path = os.path.join(tmpdir(self), "h.js")
        with open(path, "w", encoding="utf-8") as f:
            f.write(self.prelude + f"\nprocess.stdout.write(JSON.stringify({expr}));\n")
        p = subprocess.run([NODE, path], capture_output=True, encoding="utf-8", timeout=60)
        self.assertEqual(p.returncode, 0, p.stderr)
        return json.loads(p.stdout)

    def ctx(self, peers, me=1):
        return self.js(f"(() => {{ const c = bwContext({json.dumps(peers)}, {me}, {T}); "
                       "return Object.fromEntries(Object.entries(c).map(([k, v]) => [k, { n: v.others.length, all: v.all && [v.all.v, v.all.p.id], d90: v.d90 }])); })()")

    def test_context_excludes_the_session_and_needs_three(self):
        peers = [row(1, 0, s600=30.0), row(2, 400, s600=25.0), row(3, 10, s600=24.0)]
        c = self.ctx(peers)["600"]
        self.assertEqual(c["n"], 2)  # this session (id 1) left out
        self.assertIsNone(c["all"])  # 2 < BW_MIN_PEERS
        c = self.ctx(peers + [row(4, 20, s600=23.0)])["600"]
        self.assertEqual(c["all"], [25.0, 2])

    def test_ninety_days_before_only(self):
        peers = [row(2, 90, s600=26.0), row(3, 89, s600=22.0), row(4, 1, s600=21.0), row(5, 0.5, s600=20.0),
                 row(6, -1, s600=29.0), row(7, 91, s600=28.0)]
        c = self.ctx(peers)["600"]
        self.assertEqual(c["d90"], 26.0)  # from exactly 90 days before, later sessions (id 6) and day 91 out
        self.assertEqual(c["all"], [29.0, 6])  # all time includes later sessions, as the best-efforts rank does

    def test_old_cache_rows_fall_back_but_never_to_best30_kmh(self):
        peers = [{"id": i, "_t": T - i * DAY, "bw": None, "best10min_kmh": 20.0 + i, "best30_kmh": 50.0, "b60": 18.0} for i in (2, 3, 4)]
        c = self.ctx(peers)
        self.assertEqual(c["600"]["all"], [24.0, 4])
        self.assertEqual(c["3600"]["n"], 3)
        self.assertEqual(c["30"]["n"], 0)

    def test_rank_after_rounding(self):
        self.assertEqual(self.js("bwRank(24.004, [24.0, 23.0])"), {"rank": 1, "of": 3, "gap": 0})  # tied when shown
        self.assertEqual(self.js("bwRank(24.5, [24.0, 23.0])"), {"rank": 1, "of": 3, "gap": 0.5})
        self.assertEqual(self.js("bwRank(23.5, [24.0, 23.0, 25.25])"), {"rank": 3, "of": 4, "gap": -1.75})
        self.assertEqual(self.js("bwRank(20, [])"), {"rank": 1, "of": 1, "gap": None})

    def test_ticks(self):
        self.assertNotIn(2700, self.js("bwTicks(600)"))
        self.assertEqual(self.js("bwTicks(300)"), [30, 60, 300, 600, 1800, 3600])

    def test_table_escapes_and_formats(self):
        rows = [{"n": EVIL, "s": 600, "v": 24.0, "rank": 1, "of": 5, "gap": 0.42},
                {"n": "30 s", "s": 30, "v": 36.0, "rank": 2, "of": 5, "gap": -1.5}]
        html = self.js(f"bwTable({json.dumps(rows)}, false)")
        self.assertNotIn("<img", html)
        self.assertIn("&lt;img", html)
        self.assertIn("<b>24.00</b>", html)  # the 10 min row in bold
        self.assertIn("4.00 km", html)  # 24 km/h for 10 min
        self.assertIn("0.42 ahead of 2nd", html)
        self.assertIn("−1.50", html)
        self.assertIn("2:30", html)  # pace at 24 km/h
        hidden = self.js(f"bwTable({json.dumps(rows)}, true)")
        self.assertIn("hidden", hidden)
        self.assertNotIn("ahead", hidden)


if __name__ == "__main__":
    unittest.main()
