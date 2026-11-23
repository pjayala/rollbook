# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Pedro Ayala. Rollbook is free software, see LICENSE.
"""Session page parts under node: elevation helpers (elevation.js) and the one
selected stretch (sessionsel.js: pick / toggle / clear / preview) on a fake DOM.
Expected values are worked out by hand in the comments."""

import json
import os
import shutil
import subprocess
import unittest

from helpers import tmpdir
from test_frontend_power import _src, extract_const, extract_function

NODE = shutil.which("node")


def rows(t, km, hr=None, ele=None, w=None):
    """Series rows [t, km, kmh, hr, ele, lat, lon, watts]."""
    n = len(t)
    hr, ele, w = hr or [None] * n, ele or [None] * n, w or [None] * n
    return [[t[i], km[i], 20, hr[i], ele[i], None, None, w[i]] for i in range(n)]


class _Node(unittest.TestCase):
    NAMES_CONST = ()
    NAMES_FN = ()
    STUBS = ""

    @classmethod
    def setUpClass(cls):
        src = _src()
        cls.prelude = cls.STUBS + "\n".join([*(extract_const(src, n) for n in cls.NAMES_CONST),
                                             *(extract_function(src, n) for n in cls.NAMES_FN)])

    def js(self, body):
        """body: statements ending in `return <value>;`, run once."""
        path = os.path.join(tmpdir(self), "h.js")
        with open(path, "w", encoding="utf-8") as f:
            f.write(self.prelude + "\nprocess.stdout.write(JSON.stringify((() => {\n" + body + "\n})()));\n")
        p = subprocess.run([NODE, path], capture_output=True, encoding="utf-8", timeout=60)
        self.assertEqual(p.returncode, 0, p.stderr)
        return json.loads(p.stdout)


@unittest.skipUnless(NODE, "node not installed")
class Elevation(_Node):
    NAMES_CONST = ("isNum", "fnet", "fgrade")
    NAMES_FN = ("smoothEle", "eleClimb", "eleNetGrade", "eleGradeAt", "kmIdx", "idxRange", "stretchStats")

    def call(self, expr):
        return self.js("return " + expr + ";")

    def test_no_elevation(self):
        ser = rows([0, 1, 2], [0, .01, .02])
        self.assertIsNone(self.call(f"smoothEle({json.dumps(ser)})"))

    def test_median_drops_a_zero_dropout(self):
        ser = rows(list(range(7)), [i / 100 for i in range(7)], ele=[50, 50, 50, 0, 50, 50, 50])
        self.assertEqual(self.call(f"smoothEle({json.dumps(ser)})"), [50] * 7)

    def test_gap_interpolated_up_to_three_minutes(self):
        # 30 s apart: the +-15 s mean is each sample alone; the 7-sample median
        # reaches index 0 from 1..3 and index 8 from 5..7, so only 4 is empty: 60 s gap -> 150
        ser = rows(list(range(0, 270, 30)), [i / 10 for i in range(9)], ele=[100] + [None] * 7 + [200])
        self.assertEqual(self.call(f"smoothEle({json.dumps(ser)})"), [100, 100, 100, 100, 150, 200, 200, 200, 200])

    def test_gap_over_three_minutes_stays_empty(self):
        ser = rows(list(range(0, 900, 100)), [i / 10 for i in range(9)], ele=[100] + [None] * 7 + [200])
        self.assertIsNone(self.call(f"smoothEle({json.dumps(ser)})")[4])  # 200 s between 3 and 5

    def test_climb_hysteresis(self):
        # 0 -> 3 under 4 m, -> 6 up 6, -> 2 down 4, -> 10 up 8
        self.assertEqual(self.call("eleClimb([0, 3, 6, 2, 10], 0, 4)"), {"up": 14, "dn": 4})

    def test_net_and_grade(self):
        ser = rows([0, 100], [0, 2])
        self.assertEqual(self.call(f"eleNetGrade({json.dumps(ser)}, [100, 110], 0, 1)"), {"net": 10, "g": 0.5})
        short = rows([0, 10], [0, .05])  # 50 m: no grade
        self.assertEqual(self.call(f"eleNetGrade({json.dumps(short)}, [100, 110], 0, 1)"), {"net": 10, "g": None})
        self.assertEqual(self.call(f"eleNetGrade({json.dumps(ser)}, [100, null], 0, 1)"), {"net": None, "g": None})

    def test_grade_at_over_fifty_metres(self):
        # km step 1/32 (exact in binary), ele 2 m per row; j = 4: a = 2, b = 6, 125 m, +8 m -> 6.4 %
        ser = rows(list(range(10)), [i / 32 for i in range(10)])
        self.assertAlmostEqual(self.call(f"eleGradeAt({json.dumps(ser)}, {json.dumps([2 * i for i in range(10)])}, 4)"), 6.4)
        self.assertIsNone(self.call(f"eleGradeAt({json.dumps(ser)}, null, 4)"))

    def test_km_and_index_ranges(self):
        ser = rows(list(range(6)), [0, .5, 1, 1.5, 2, 2.5])
        self.assertEqual(self.call(f"kmIdx({json.dumps(ser)}, 1, 2)"), [2, 4])
        self.assertEqual(self.call(f"idxRange({json.dumps(ser)}, 1, 1, 2)"), [2, 4])
        self.assertIsNone(self.call(f"idxRange({json.dumps(ser)}, 1, 7, 8)"))

    def test_stretch_stats(self):
        # 10 s at 1 m/s moving, 10 s still, 80 s just over the 0.5 m/s threshold (moving);
        # the 80 s step is too long for HR / power: hr (100*10 + 120*10) / 20, power (200*10 + 100*10) / 20
        ser = rows([0, 10, 20, 100], [0, .01, .01, .0505], hr=[100, 120, None, None], w=[200, 100, 0, 0])
        self.assertEqual(self.call(f"stretchStats({json.dumps(ser)}, 0, 3)"), {"mv": 90, "hr": 110, "pw": 150})

    def test_formatting(self):
        self.assertEqual(self.call("[fnet(-3.4), fnet(12), fnet(null), fgrade(2), fgrade(-0.25), fgrade(NaN)]"),
                         ["−3 m", "+12 m", "", "+2.0%", "-0.3%", ""])


# a fake DOM for sessionSelection: the selection box, its clear button, table rows
DOM = r"""
const ICON = { close: "x" };
const box = { hidden: true, innerHTML: "" }, clearBtn = {};
let ROWS = [];
const $ = q => q === "#speed-sel" ? box : q === "#sel-clear" ? clearBtn : null;
const document = { querySelectorAll: () => ROWS };
const fakeRow = key => { const r = { key, cls: new Set(), classList: {
  remove(c) { r.cls.delete(c); }, toggle(c, on) { on ? r.cls.add(c) : r.cls.delete(c); } } }; return r; };
const fakeMap = calls => ({ highlightBy(col, lo, hi, fit) { calls.push([col, lo, hi, fit]); },
  highlight(t0, t1, f) { this.highlightBy(0, t0, t1, f); }, mark() {} });
function page(withMap) {
  const ser = Array.from({ length: 11 }, (_, j) => [j * 10, j / 10, 36, null, null, null, null, null]);
  const calls = [], bands = [], cleared = [];
  const P = { ser, xmode: "km", avg: 30, W: null, ele: null, map: withMap ? fakeMap(calls) : null,
    charts: { speed: { setBand: (lo, hi) => bands.push([lo, hi]) }, pw: null } };
  sessionSelection(P);
  P.onClear(() => cleared.push(1));
  return { P, calls, bands, cleared };
}
"""


@unittest.skipUnless(NODE, "node not installed")
class Selection(_Node):
    STUBS = DOM
    NAMES_CONST = ("isNum", "f0", "f2", "pct", "pace", "fnet", "fgrade", "SEL_ROWS")
    NAMES_FN = ("dur", "idxRange", "stretchStats", "eleNetGrade", "sessionSelection")

    def test_pick_toggles_and_highlights(self):
        r = self.js("""
          const { P, calls, bands } = page(true);
          const on = P.pick("km", 2, [1, 0.1, 0.2, false]);
          const a = [on, P.isPicked("km", 2), calls.at(-1), bands.at(-1), box.hidden];
          const off = P.pick("km", 2, [1, 0.1, 0.2, false]);  // click again
          return [a, [off, P.isPicked("km", 2), calls.at(-1), bands.at(-1), box.hidden]];""")
        # rows 1..2 (km 0.1 and 0.2) -> the band is drawn on the km axis; the null chart is skipped
        self.assertEqual(r[0], [True, True, [1, 0.1, 0.2, False], [0.1, 0.2], False])
        self.assertEqual(r[1], [False, False, [None, None, None, None], [None, None], True])

    def test_a_pick_drops_the_other_tables(self):
        r = self.js("""
          const { P, cleared } = page(true);
          const km = fakeRow(2); km.cls.add("sel"); ROWS = [km];
          P.pick("km", 2, [1, 0.1, 0.2, false]);
          P.pick("be", "1k", [1, 0, 1, true, { el: 120, km: 1 }]);
          return [P.isPicked("km", 2), P.isPicked("be", "1k"), km.cls.has("sel"), cleared.length];""")
        self.assertEqual(r, [False, True, False, 2])  # every pick clears first

    def test_stale_pick_never_turns_a_click_into_a_deselect(self):
        # lap 3, then km 2, then lap 3 again: lap 3 must be selected, not toggled off
        r = self.js("""
          const { P } = page(true);
          P.pick("lap", 3, [0, 10, 30, true]); P.pick("km", 2, [1, 0.1, 0.2, false]);
          return P.pick("lap", 3, [0, 10, 30, true]) && P.isPicked("lap", 3);""")
        self.assertTrue(r)
        r = self.js("""
          const { P } = page(true);
          P.pick("be", "1k", [1, 0, 1, true]); P.clearRowSel();  // e.g. a brush
          return P.pick("be", "1k", [1, 0, 1, true]);""")
        self.assertTrue(r)

    def test_mark_rows(self):
        r = self.js("""
          const { P } = page(true);
          const rs = [fakeRow(1), fakeRow(2), fakeRow(3)];
          P.pick("km", 2, [1, 0.1, 0.2, false]); P.markRows(rs, "km", tr => tr.key);
          return rs.map(x => x.cls.has("sel"));""")
        self.assertEqual(r, [False, True, False])

    def test_clear_button_and_x_mode(self):
        r = self.js("""
          const { P, bands, cleared } = page(true);
          P.pick("km", 2, [1, 0.1, 0.2, false]);
          P.xmode = "t"; P.paintSel(); const t = bands.at(-1);  // same rows, now on the time axis
          clearBtn.onclick();
          return [t, P.isPicked("km", 2), bands.at(-1), box.hidden, cleared.length];""")
        self.assertEqual(r, [[10, 20], False, [None, None], True, 2])

    def test_preview_keeps_the_pick(self):
        r = self.js("""
          const { P, calls } = page(true);
          P.pick("km", 2, [1, 0.1, 0.2, false]);
          P.preview(50, 80); const hover = calls.at(-1);
          P.preview(null); const back = calls.at(-1);
          return [hover, back, P.isPicked("km", 2), box.hidden];""")
        self.assertEqual(r, [[0, 50, 80, False], [1, 0.1, 0.2, False], True, False])
        r = self.js("""
          const { P, calls } = page(true);
          P.preview(50, 80); P.preview(null); return calls.at(-1);""")
        self.assertEqual(r, [None, None, None, False])

    def test_without_a_map(self):
        r = self.js("""
          const { P, bands } = page(false);
          P.hl.highlight(20, 40, true);  // laps / best speeds select by time
          return bands.at(-1);""")
        self.assertEqual(r, [0.2, 0.4])  # rows 2..4 (t 20..40), banded on the km axis

    def test_exact_stretch_in_the_box(self):
        r = self.js("""
          const { P } = page(true);
          P.pick("be", "1k", [1, 0.1, 0.2, true, { el: 120, km: 1 }]);
          return box.innerHTML;""")
        self.assertIn("2:00", r)        # time = the effort's exact 120 s, not the series' 10 s
        self.assertIn("30.00 km/h", r)  # 1 km in 120 s


if __name__ == "__main__":
    unittest.main()
