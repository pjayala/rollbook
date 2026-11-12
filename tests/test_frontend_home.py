# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Pedro Ayala. Rollbook is free software, see LICENSE.
"""Home page (#/, every sport): the pure helpers of home.js, run under node."""

import json
import os
import shutil
import subprocess
import unittest

from helpers import ROOT, tmpdir
from test_frontend_power import _src, extract_const, extract_function

NODE = shutil.which("node")
HOME_JS = os.path.join(ROOT, "web", "static", "home.js")
MAP_JS = os.path.join(ROOT, "web", "static", "map.js")
# `pace` ends in a comment, so its cut runs on through `pct` (the next line)
APP_CONSTS = ["isNum", "esc", "f0", "f1", "fkm", "fnum", "hours", "pace", "DAY", "MON", "fdate", "dayOf", "pwAvg",
              "RANGE_DAYS", "RANGE_RE", "inSpan", "ymd"]
APP_FUNCS = ["dur", "dayMs", "rangeSpan", "prevSpan", "rangeLabel", "prevLabel"]
HOME_CONSTS = ["SPORT_SHADES", "RUNLIKE", "sportShade"]
HOME_FUNCS = ["sportOrder", "mondayOf", "ribbonBuckets", "relDay", "yearToDate", "ytdChange", "thumbPath",
              "sessionFigure", "sportBest", "feedRow", "sportRow", "ytdPanel", "homeRanges", "prevNote"]
EVIL = '<img src=x onerror=alert(1)>"\'&'


def read(path):
    with open(path, encoding="utf-8") as f:
        return f.read().replace("\r\n", "\n")


@unittest.skipUnless(NODE, "node not installed")
class Home(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        app, home, mp = _src(), read(HOME_JS), read(MAP_JS)
        cls.prelude = "\n".join([*[extract_const(app, n) for n in APP_CONSTS], *[extract_function(app, n) for n in APP_FUNCS],
                                 extract_function(mp, "bboxOf"),
                                 *[extract_const(home, n) for n in HOME_CONSTS],
                                 *[extract_function(home, n) for n in HOME_FUNCS],
                                 # as loadActs holds them: _t = local noon of the day
                                 "const A = (date, type, km, x = {}) => ({ date, type, km, _t: +new Date(date + 'T12:00:00'), _d: new Date(date + 'T12:00:00'), ...x });",
                                 "const L = s => +new Date(s);"])  # a local time

    def js(self, expr, tz="Europe/Madrid"):
        path = os.path.join(tmpdir(self), "h.js")
        with open(path, "w", encoding="utf-8") as f:
            f.write(self.prelude + "\nprocess.stdout.write(JSON.stringify((() => { " + expr + " })()));\n")
        p = subprocess.run([NODE, path], capture_output=True, encoding="utf-8", timeout=60, env={**os.environ, "TZ": tz})
        self.assertEqual(p.returncode, 0, p.stderr)
        return json.loads(p.stdout)

    # ---- sport order and shades

    def test_order_by_recent_km_then_sessions(self):
        r = self.js("""const types = [{type: 'Run', n: 1514}, {type: 'Inline Skate', n: 270}, {type: 'Walk', n: 7}, {type: 'Swim', n: 2}];
          const acts = [A('2026-11-01', 'Inline Skate', 30), A('2026-10-01', 'Run', 10), A('2020-01-01', 'Run', 5000)];
          return sportOrder(types, acts, L('2026-11-12T12:00'));""")
        # skating leads the last year; Walk and Swim have none, so all-time sessions decide
        self.assertEqual(r, ["Inline Skate", "Run", "Walk", "Swim"])

    def test_shades_main_hue_first_then_grey_floor(self):
        r = self.js("return [0, 1, 2, 5, 6, 40].map(sportShade);")
        self.assertEqual(r, ["#ff7a1a", "#ffb27a", "#b5530d", "#5a5a5a", "#5a5a5a", "#5a5a5a"])

    # ---- the ribbon

    def test_weekly_buckets_are_local_mondays_across_dst(self):
        # Madrid leaves winter time on Sun 29 Mar 2026: buckets stay on Monday midnight
        r = self.js("""const R = ribbonBuckets([A('2026-03-01', 'Run', 5), A('2026-03-30', 'Run', 7), A('2026-03-29', 'Ride', 3), A('2026-03-30', 'Kayak', 9)],
            ['Run', 'Ride'], L('2026-07-20T09:00'));
          return { step: R.step, first: new Date(R.starts[0]).toString().slice(0, 24), mondays: R.starts.every(ms => { const d = new Date(ms); return d.getDay() === 1 && d.getHours() === 0; }),
            n: R.starts.length, last: new Date(R.starts.at(-1)).toDateString(), sum: R.km.flat().reduce((s, v) => s + v, 0),
            wk: R.km.map((r, i) => [new Date(R.starts[i]).toDateString(), ...r, R.n[i]]).filter(r => r[3]) };""")
        self.assertEqual(r["step"], "week")
        self.assertEqual(r["first"], "Mon Feb 23 2026 00:00:00")  # the Monday before Sun 1 Mar
        self.assertTrue(r["mondays"])
        self.assertEqual(r["last"], "Mon Jul 20 2026")  # today's week is drawn, empty
        self.assertEqual(r["n"], 22)  # 23 Feb .. 20 Jul: 21 weeks later + the first
        self.assertEqual(r["sum"], 15)  # the Kayak isn't in `order`
        self.assertEqual(r["wk"], [["Mon Feb 23 2026", 5, 0, 1], ["Mon Mar 23 2026", 0, 3, 1], ["Mon Mar 30 2026", 7, 0, 1]])

    def test_short_span_gets_day_buckets(self):
        r = self.js("""const R = ribbonBuckets([A('2026-10-30', 'Run', 4), A('2026-11-01', 'Run', 6)], ['Run'], L('2026-11-02T08:00'));
          return [R.step, R.starts.length, R.km.map(x => x[0])];""")
        self.assertEqual(r, ["day", 4, [4, 0, 6, 0]])

    def test_no_sessions_no_buckets(self):
        self.assertEqual(self.js("return ribbonBuckets([], ['Run'], L('2026-11-02T08:00')).starts;"), [])

    def test_ribbon_covers_the_range_not_just_its_sessions(self):
        # 30 days ending 12 Nov with one session on 1 Nov: day buckets from 13 Oct to 12 Nov
        r = self.js("""const R = ribbonBuckets([A('2026-11-01', 'Run', 5)], ['Run'], L('2026-11-12T10:00'), L('2026-10-13T10:00'));
          return [R.step, R.starts.length, new Date(R.starts[0]).toDateString(), R.km.findIndex(x => x[0] === 5)];""")
        self.assertEqual(r, ["day", 31, "Tue Oct 13 2026", 19])
        r = self.js("return ribbonBuckets([], ['Run'], L('2026-11-12T10:00'), L('2026-11-10T10:00')).starts.length;")
        self.assertEqual(r, 3)  # an empty range still has its days

    # ---- date ranges (app.js)

    def test_range_spans(self):
        r = self.js("""const now = L('2026-11-12T15:00'), f = x => x && [new Date(x.lo).toString().slice(4, 24), isFinite(x.hi) ? new Date(x.hi).toString().slice(4, 24) : 'open'];
          return ['all', 'ytd', 'y2025', 'c:2026-03-28:2026-03-29', '90'].map(r => f(rangeSpan(r, now)));""")
        self.assertEqual(r[0], None)
        self.assertEqual(r[1], ["Jan 01 2026 00:00:00", "open"])
        self.assertEqual(r[2], ["Jan 01 2025 00:00:00", "Jan 01 2026 00:00:00"])
        self.assertEqual(r[3], ["Mar 28 2026 00:00:00", "Mar 30 2026 00:00:00"])  # both days, across the DST change
        # the last N days are N × 24 h back, as before: 15:00 winter time is 16:00 summer time in August
        self.assertEqual(r[4], ["Aug 14 2026 16:00:00", "open"])

    def test_bad_ranges_are_all_time(self):
        r = self.js("""return ['c:2026-02-31:2026-03-01', 'c:2026-05-02:2026-05-01', 'c:2026-5-1:2026-05-02', '7', 'y26', '', null,
            'c:2026-01-01:2026-01-02<x>', '__proto__'].map(r => rangeSpan(r));""")
        self.assertEqual(r, [None] * 9)

    def test_previous_span_and_labels(self):
        r = self.js("""const now = L('2026-11-12T15:00'), d = x => new Date(x).toDateString();
          const p = r => { const s = prevSpan(r, now); return s && [d(s.lo), d(s.hi), prevLabel(r, now)]; };
          return [p('ytd'), p('y2025'), p('c:2026-05-01:2026-06-15'), p('all'), prevLabel('365', now),
            rangeLabel('c:2026-05-01:2026-06-15', now), rangeLabel('ytd', now), rangeLabel('y2024', now), rangeLabel('bogus', now)];""")
        self.assertEqual(r[0], ["Wed Jan 01 2025", "Thu Nov 13 2025", "the same days of 2025"])
        self.assertEqual(r[1], ["Mon Jan 01 2024", "Wed Jan 01 2025", "2024"])
        self.assertEqual(r[2], ["Mon Mar 16 2026", "Fri May 01 2026", "the 46 days before"])  # 1 May .. 15 Jun = 46 days; 46 days before 1 May, across DST
        self.assertIsNone(r[3])
        self.assertEqual(r[4:], ["the 12 months before", "1 May 2026 – 15 Jun 2026", "This year", "2024", "All time"])

    def test_previous_ytd_on_a_leap_day(self):
        r = self.js("const s = prevSpan('ytd', L('2028-02-29T09:00')); return new Date(s.hi).toDateString();")
        self.assertEqual(r, "Mon Mar 01 2027")  # 1 Jan .. 28 Feb 2027 included

    def test_home_range_choices(self):
        r = self.js("const n = L('2026-11-12T15:00'); return [homeRanges('all', n).map(x => x[0]), homeRanges('y2021', n).map(x => x[1])];")
        self.assertEqual(r[0], ["all", "ytd", "y2025", "365", "90", "30"])
        self.assertEqual(r[1], ["All time", "This year", "2025", "2021", "12 months", "90 days", "30 days"])

    def test_prev_note(self):
        r = self.js("return [prevNote(605, 337, ' km', 'the 46 days before'), prevNote(29, 18, '', true), prevNote(5, 1, '', '')];")
        self.assertEqual(r, ["<em>+79.5% <span>vs 337 km in the 46 days before</span></em>", "<em>+61.1% <span>vs 18</span></em>", ""])

    # ---- dates

    def test_relative_days(self):
        r = self.js("const t = L('2026-11-12T00:30'); return ['2026-11-12', '2026-11-11', '2026-11-09', '2026-11-06', '2026-11-05'].map(d => relDay(d, t));")
        self.assertEqual(r, ["Today", "Yesterday", "3 days ago", "6 days ago", "5 Nov 2026"])

    def test_year_to_date_on_a_leap_day(self):
        # today 29 Feb 2028: last year's span is 1 Jan .. 28 Feb 2027 (no 29 Feb), 1 Mar 2027 is out
        r = self.js("""return yearToDate([A('2028-01-03', 'Run', 10), A('2028-02-29', 'Run', 2.4), A('2027-02-28', 'Run', 8), A('2027-03-01', 'Run', 50),
            A('2027-12-31', 'Run', 99), A('2028-01-10', 'Ride', 20), A('2026-05-01', 'Swim', 1)], ['Ride', 'Run', 'Swim'], L('2028-02-29T18:00'));""")
        self.assertEqual(r["year"], 2028)
        self.assertEqual([(x["type"], x["now"], x["prev"]) for x in r["rows"]], [("Ride", 20, 0), ("Run", 12.4, 8)])
        self.assertEqual((r["total"]["now"], r["total"]["prev"]), (32.4, 8))

    def test_change_from_the_rounded_figures_shown(self):
        r = self.js("return [ytdChange(110, 100), ytdChange(99.6, 100.4), ytdChange(5, 0), ytdChange(0, 0), ytdChange(0, 7)];")
        self.assertEqual(r, ["+10.0%", "0.0%", "new", "–", "-100.0%"])

    # ---- thumbnails and figures

    def test_thumb_path_fits_and_keeps_aspect(self):
        # a 2:1 track in a 64 x 44 box with 4 px padding: scale = min(56/2, 36/1) = 28, centred
        r = self.js("return thumbPath([0, 0, 2, 0, 2, 1, NaN, NaN, 0, 1], 64, 44);")
        self.assertEqual(r, "M4.0 8.0L60.0 8.0L60.0 36.0M4.0 36.0")
        self.assertEqual(self.js("return thumbPath([NaN, NaN], 64, 44);"), "")

    def test_session_figure_by_sport(self):
        r = self.js("""return [sessionFigure({ type: 'Virtual Ride', watts_avg: 187.4, kmh_moving: 30 }), sessionFigure({ type: 'Run', kmh_moving: 12 }),
          sessionFigure({ type: 'Inline Skate', kmh_moving: 22.44 }), sessionFigure({ type: 'Swim' })];""")
        self.assertEqual(r, ["187<small> W</small>", "5:00<small> /km</small>", "22.4<small> km/h</small>", "–"])

    def test_best_is_power_indoors(self):
        r = self.js("""const xs = [A('2026-01-01', 'Virtual Ride', 30, { pw20m: 180, best10min_kmh: 55 }), A('2026-01-02', 'Virtual Ride', 30, { pw20m: 201 }),
            A('2026-01-03', 'Inline Skate', 20, { best10min_kmh: 25.07 })];
          return [sportBest(xs, 'Virtual Ride').html, sportBest(xs, 'Inline Skate').label, sportBest(xs, 'Run')];""")
        self.assertEqual(r, ["201<small> W</small>", "best 10 min", None])

    # ---- HTML builders escape everything

    def test_builders_escape_names(self):
        e = json.dumps(EVIL)
        r = self.js(f"""const a = A('2026-01-01', {e}, 3, {{ id: 7, name: {e}, moving_s: 600, kmh_moving: 18 }});
          return [feedRow(a, '#fff', null), feedRow(a, '#fff', [0, 0, 1, 1]),
            sportRow({{ type: {e} }}, 0, {{ n: 1, km: 3, mv: 600, last: '2026-01-01', best: null }}),
            ytdPanel({{ year: 2026, rows: [{{ type: {e}, now: 3, prev: 1 }}, {{ type: 'Run', now: 1, prev: 1 }}], total: {{ type: 'Total', now: 4, prev: 2 }} }}, new Map())];""")
        for html in r:
            self.assertNotIn("<img", html)
            self.assertNotIn("onerror=alert(1)>\"", html)
            self.assertIn("&lt;img src=x onerror=alert(1)&gt;&quot;&#39;&amp;", html)
        self.assertIn('href="#/session/7"', r[0])
        self.assertIn("<path d=\"M", r[1])
        self.assertIn("Total", r[3])
        self.assertIn("+100.0%", r[3])


if __name__ == "__main__":
    unittest.main()
