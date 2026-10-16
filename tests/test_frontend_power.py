# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Pedro Ayala. Rollbook is free software, see LICENSE.
"""Browser FTP choice (ftpAt) and Coggan power zones, cut out of app.js and run under node."""

import json
import os
import re
import shutil
import subprocess
import unittest

from helpers import ROOT, tmpdir

APP_JS = os.path.join(ROOT, "web", "static", "app.js")
NODE = shutil.which("node")
DAY = 86400000
T = 1790000000000  # fixed "now" (2026-09-21 UTC): deterministic, never Date.now()


def _src():
    with open(APP_JS, encoding="utf-8") as f:
        return f.read().replace("\r\n", "\n")  # Windows autocrlf checkout


def extract_const(src, name):
    """Top-level `const NAME = ...;` up to the first line ending in `;`."""
    m = re.search(r"^const %s = .*?;$" % re.escape(name), src, re.M | re.S)
    if not m:
        raise AssertionError("const %s not found in app.js" % name)
    return m.group(0)


def extract_function(src, name):
    """Top-level `function NAME(...) {` up to its closing `}` in column 0."""
    m = re.search(r"^function %s\(.*?^}$" % re.escape(name), src, re.M | re.S)
    if not m:
        raise AssertionError("function %s not found in app.js" % name)
    return m.group(0)


def act(days_ago, name="Ride", **kw):
    """An activity as the browser holds it: `_t` in ms."""
    a = {"_t": T - int(round(days_ago * DAY)), "name": name}
    a.update(kw)
    return a


@unittest.skipUnless(NODE, "node not installed")
class _Node(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        src = _src()
        cls.prelude = "\n".join([
            extract_const(src, "isNum"), extract_const(src, "DAY"), extract_const(src, "FTP_DAYS"),
            extract_function(src, "ftpAt"), extract_const(src, "ftpEst"),
            extract_const(src, "PZONES"), extract_function(src, "pzSecs"), extract_const(src, "pzLabel"),
        ])

    def js(self, calls):
        """calls: [(fn, args, profile)] -> results, all in one node process."""
        path = os.path.join(tmpdir(self), "harness.js")
        body = (
            "let S = {};\n" + self.prelude + "\n"
            "const FN = { ftpAt, ftpEst, pzSecs, pzLabel, zones: () => PZONES.map(z => [z.n, z.hi]) };\n"
            "const CALLS = " + json.dumps(calls) + ";\n"
            "process.stdout.write(JSON.stringify(CALLS.map(([fn, args, profile]) => {\n"
            "  S = { profile }; const r = FN[fn](...args); return r === undefined ? null : r; })));\n"
        )
        with open(path, "w", encoding="utf-8") as f:
            f.write(body)
        p = subprocess.run([NODE, path], capture_output=True, encoding="utf-8", timeout=60)
        self.assertEqual(p.returncode, 0, p.stderr)
        return json.loads(p.stdout)

    def ftp(self, acts, self_=None, profile=None, t=T):
        return self.js([("ftpAt", [acts, t, self_], profile)])[0]


class FtpAt(_Node):
    def test_profile_ftp_beats_everything(self):
        r = self.ftp([act(5, f_ftp=200, pw20m=300)], self_=act(0, f_ftp=190), profile={"ftp": 250})
        self.assertEqual((r["w"], r["src"], r["note"]), (250, "profile", "your profile"))

    def test_zero_or_missing_profile_ftp_falls_through(self):
        for prof in ({"ftp": 0}, {}, None):
            r = self.ftp([act(5, f_ftp=200)], profile=prof)
            self.assertEqual((r["w"], r["src"]), (200, "app"), prof)

    def test_rides_own_f_ftp_beats_newer_ones(self):
        r = self.ftp([act(1, f_ftp=210), act(3, f_ftp=205)], self_=act(10, "ROUVY - Lago di Garda", f_ftp=185))
        self.assertEqual((r["w"], r["src"], r["note"]), (185, "app", "set in ROUVY"))

    def test_rides_own_f_ftp_beats_bigger_estimate(self):
        r = self.ftp([act(1, pw20m=300)], self_=act(0, f_ftp=180))
        self.assertEqual((r["w"], r["src"]), (180, "app"))

    def test_self_without_f_ftp_uses_latest_up_to_t(self):
        acts = [act(100, f_ftp=147), act(5, "MyWhoosh ride", f_ftp=188), act(30, f_ftp=195)]
        r = self.ftp(acts, self_=act(0, pw20m=250))
        self.assertEqual((r["w"], r["note"]), (188, "set in MyWhoosh"))

    def test_generic_app_note(self):
        self.assertEqual(self.ftp([act(5, "Evening ride", f_ftp=190)])["note"], "set in your training app")

    def test_future_f_ftp_ignored_beyond_60s(self):
        base = act(5, f_ftp=188)
        later = lambda ms, w: {"_t": T + ms, "name": "Ride", "f_ftp": w}
        self.assertEqual(self.ftp([base, later(2 * 60000, 999)])["w"], 188)
        self.assertEqual(self.ftp([base, later(61000, 999)])["w"], 188)
        self.assertEqual(self.ftp([base, later(59000, 201)])["w"], 201)  # start-time tolerance

    def test_f_ftp_window_179_vs_180_vs_181_days(self):
        self.assertEqual(self.ftp([act(179, f_ftp=170)])["src"], "app")
        for d in (180, 181):
            r = self.ftp([act(d, f_ftp=170), act(10, pw20m=200)])
            self.assertEqual(r["src"], "estimate", d)
            self.assertAlmostEqual(r["w"], 190)

    def test_estimate_is_95pct_of_best_20min_in_window(self):
        acts = [act(10, pw20m=200), act(179, pw20m=240), act(181, pw20m=300), act(-1, pw20m=400)]
        r = self.ftp(acts)
        self.assertEqual(r["src"], "estimate")
        self.assertAlmostEqual(r["w"], 0.95 * 240)
        self.assertEqual(r["note"], "95% of best 20 min, last 6 months")

    def test_f_ftp_beats_bigger_estimate(self):
        r = self.ftp([act(20, f_ftp=180), act(1, pw20m=300)])
        self.assertEqual((r["w"], r["src"]), (180, "app"))

    def test_nothing_gives_null(self):
        self.assertIsNone(self.ftp([]))
        self.assertIsNone(self.ftp([act(5, f_ftp=None, pw20m="250"), act(400, pw20m=300)]))
        out = self.js([("ftpEst", [[], T, None], None), ("ftpEst", [[act(5, f_ftp=190)], T, None], None)])
        self.assertEqual(out, [None, 190])

    @unittest.expectedFailure  # bug: f_ftp 0 is isNum, blocks the 95% estimate (remove decorator once fixed)
    def test_zero_f_ftp_should_fall_back_to_estimate(self):
        r = self.ftp([act(5, f_ftp=0), act(1, pw20m=200)])
        self.assertEqual(r["src"], "estimate")


class PowerZones(_Node):
    def test_coggan_boundaries(self):
        z = self.js([("zones", [], None)])[0]
        self.assertEqual(z, [["Z1", .55], ["Z2", .75], ["Z3", .90], ["Z4", 1.05],
                             ["Z5", 1.20], ["Z6", 1.50], ["Z7", None]])  # Infinity -> null in JSON

    def test_pzsecs_bins_inclusive_upper_edges(self):
        # FTP 200: edges 110 / 150 / 180 / 210 / 240 / 300 W
        hist = {"0": 1, "110": 2, "111": 4, "150": 8, "151": 16, "180": 32, "181": 64, "210": 128,
                "211": 256, "240": 512, "241": 1024, "300": 2048, "301": 4096, "2000": 8192}
        z = self.js([("pzSecs", [hist, 200], None)])[0]
        self.assertEqual(z, [3, 12, 48, 192, 768, 3072, 12288])

    def test_pzsecs_without_hist_or_ftp_is_zeros(self):
        out = self.js([("pzSecs", [None, 200], None), ("pzSecs", [{"150": 5}, 0], None),
                       ("pzSecs", [{"150": 5}, None], None)])
        self.assertEqual(out, [[0] * 7] * 3)

    def test_labels(self):
        out = self.js([("pzLabel", [0, 200], None), ("pzLabel", [1, 200], None), ("pzLabel", [6, 200], None)])
        self.assertEqual(out, ["Z1 0\u2013110 W", "Z2 111\u2013150 W", "Z7 301+ W"])

    @unittest.expectedFailure  # bug: label rounds 137.5 -> 138 into Z1, pzSecs bins 138 W as Z2
    def test_label_agrees_with_binning_at_half_watt_edge(self):
        z, lab = self.js([("pzSecs", [{"138": 1}, 250], None), ("pzLabel", [0, 250], None)])
        self.assertEqual(z[0], 1, "138 W binned outside Z1 while label is %r" % lab)


if __name__ == "__main__":
    unittest.main()
