# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Pedro Ayala. Rollbook is free software, see LICENSE.
"""Browser newRecords() (#17): which records an import set, cut out of app.js and run under node."""

import json
import os
import shutil
import subprocess
import unittest
from datetime import datetime, timedelta, timezone

from helpers import tmpdir
from test_frontend_power import _src, extract_const, extract_function

NODE = shutil.which("node")
NOW = datetime(2026, 10, 1, 9, 0, tzinfo=timezone.utc)
SK = "Inline Skate"


def act(id, days_ago=0.0, type=SK, hours=0.0, **kw):
    """A session as the browser holds it: `datetime` ISO, `_t` noon of that day (as dateOf)."""
    t = NOW - timedelta(days=days_ago) + timedelta(hours=hours)
    noon = datetime(t.year, t.month, t.day, 12, tzinfo=timezone.utc)
    a = {"id": id, "type": type, "name": f"s{id}", "datetime": t.isoformat(), "date": t.date().isoformat(),
         "_t": int(noon.timestamp() * 1000)}
    a.update(kw)
    return a


def olds(n=5, start=100, **kw):
    """n older sessions, 10..10+n days ago, ids start.."""
    return [act(start + i, 10 + i, **{k: (v(i) if callable(v) else v) for k, v in kw.items()}) for i in range(n)]


@unittest.skipUnless(NODE, "node not installed")
class NewRecords(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        src = _src()
        cls.prelude = "\n".join([
            extract_const(src, "isNum"), extract_const(src, "DAY"),
            "function median(xs) { xs = xs.filter(isNum).sort((a, b) => a - b); if (!xs.length) return null; const m = xs.length >> 1; return xs.length % 2 ? xs[m] : (xs[m - 1] + xs[m]) / 2; }",
            extract_const(src, "whenOf"), extract_const(src, "cmpWhen"), extract_const(src, "BE"), extract_const(src, "RECORD_METRICS"),
            extract_const(src, "recRound"), extract_const(src, "NR"),
            extract_function(src, "groupCircuits"), extract_function(src, "newRecords"),
        ])

    def run_js(self, acts, new_ids, segs=None):
        path = os.path.join(tmpdir(self), "h.js")
        body = (self.prelude + "\n"
                f"const ACTS = {json.dumps(acts)};\n"
                f"const SEGS = {json.dumps(segs or {})};\n"
                "const byId = new Map(ACTS.map(a => [a.id, a]));\n"
                "for (const t in SEGS) for (const sg of SEGS[t]) sg.efforts = sg.efforts.map(([aid, el]) => ({ aid, el, a: byId.get(aid) }));\n"
                f"const r = newRecords(ACTS, {json.dumps(new_ids)}, SEGS);\n"
                "process.stdout.write(JSON.stringify(r.map(x => ({ scope: x.scope, kind: x.kind, k: x.k, aid: x.aid, value: x.value, prev: x.prev, type: x.type, sgid: x.sgid || null }))));\n")
        with open(path, "w", encoding="utf-8") as f:
            f.write(body)
        p = subprocess.run([NODE, path], capture_output=True, encoding="utf-8", timeout=60)
        self.assertEqual(p.returncode, 0, p.stderr)
        return json.loads(p.stdout)

    def recs(self, acts, new_ids, k="best10min_kmh", segs=None):
        return [(r["scope"], r["aid"]) for r in self.run_js(acts, new_ids, segs) if r["k"] == k]

    # ---- best efforts by distance (#6): lower is better

    def test_best_effort_lower_is_better(self):
        out = self.run_js(olds(be5k=lambda i: 900 + i) + [act(1, 1, be5k=880.0)], [1])
        self.assertEqual([(r["scope"], r["k"], r["aid"], r["value"], r["prev"], r["type"]) for r in out if r["k"] == "be5k"],
                         [("all", "be5k", 1, 880.0, 900, SK)])

    def test_best_effort_must_beat_after_rounding(self):
        base = olds(be5k=lambda i: 900 + i)
        self.assertEqual(self.recs(base + [act(1, 1, be5k=899.6)], [1], k="be5k"), [])
        self.assertEqual(self.recs(base + [act(1, 1, be5k=899.4)], [1], k="be5k"), [("all", 1)])

    def test_best_effort_never_on_virtual(self):
        vr = [act(100 + i, 10 + i, type="Virtual Ride", be5k=600.0 + i) for i in range(5)] + [act(1, 1, type="Virtual Ride", be5k=500.0)]
        self.assertEqual(self.recs(vr, [1], k="be5k"), [])

    # ---- all-time / at the time

    def test_all_time_record(self):
        out = self.run_js(olds(best10min_kmh=lambda i: 20 + i) + [act(1, 1, best10min_kmh=25.0)], [1])
        self.assertEqual([(r["scope"], r["k"], r["aid"], r["value"], r["prev"]) for r in out],
                         [("all", "best10min_kmh", 1, 25.0, 24)])

    def test_not_better_is_nothing(self):
        self.assertEqual(self.recs(olds(best10min_kmh=lambda i: 20 + i) + [act(1, 1, best10min_kmh=23.0)], [1]), [])

    def test_needs_five_older_sessions_with_the_value(self):
        base = olds(4, best10min_kmh=20.0)
        self.assertEqual(self.recs(base + [act(1, 1, best10min_kmh=30.0)], [1]), [])
        nulls = olds(5, best10min_kmh=20.0) + olds(5, start=200, best10min_kmh=None)
        self.assertEqual(self.recs(nulls[:4] + nulls[5:] + [act(1, 1, best10min_kmh=30.0)], [1]), [])  # un-enriched rows don't count
        self.assertEqual(self.recs(nulls + [act(1, 1, best10min_kmh=30.0)], [1]), [("all", 1)])

    def test_first_import_claims_nothing(self):
        for step in (1, -1):  # improving and worsening batches: "best at the time" only counts history
            acts = [act(i, 20 - step * i, best10min_kmh=20.0 + i) for i in range(1, 12)]
            self.assertEqual(self.run_js(acts, [a["id"] for a in acts]), [], step)

    def test_ties_and_display_precision(self):
        base = olds(best10min_kmh=lambda i: 24.0 if i == 0 else 20.0)
        for v, want in ((24.0, []), (24.004, []), (24.01, [("all", 1)])):
            self.assertEqual(self.recs(base + [act(1, 1, best10min_kmh=v)], [1]), want, v)
        pw = olds(type="Virtual Ride", pw20m=lambda i: 200.0)
        self.assertEqual(self.recs(pw + [act(1, 1, type="Virtual Ride", pw20m=200.4)], [1], "pw20m"), [])
        self.assertEqual(self.recs(pw + [act(1, 1, type="Virtual Ride", pw20m=200.6)], [1], "pw20m"), [("all", 1)])

    def test_two_new_beat_the_old_best(self):
        base = olds(best10min_kmh=20.0)
        # 1 (earlier, 22) beat everything before it; 2 (later, 25) is the record
        self.assertEqual(self.recs(base + [act(1, 3, best10min_kmh=22.0), act(2, 1, best10min_kmh=25.0)], [1, 2]),
                         [("all", 2), ("then", 1)])
        # later and slower than the new record: nothing
        self.assertEqual(self.recs(base + [act(1, 3, best10min_kmh=25.0), act(2, 1, best10min_kmh=22.0)], [1, 2]),
                         [("all", 1)])

    def test_tied_new_sessions_reported_once_earliest(self):
        base = olds(best10min_kmh=20.0)
        self.assertEqual(self.recs(base + [act(2, 1, best10min_kmh=25.0), act(1, 3, best10min_kmh=25.0)], [1, 2]),
                         [("all", 1)])

    def test_same_day_ordered_by_start_time_not_noon(self):
        base = olds(best10min_kmh=20.0)
        am = act(9, 1, hours=-2, best10min_kmh=25.0)   # morning
        pm = act(3, 1, hours=6, best10min_kmh=24.5)    # afternoon, lower id: _t/id order would put it first
        self.assertEqual(am["_t"], pm["_t"])
        self.assertEqual(self.recs(base + [am, pm], [9, 3]), [("all", 9)])

    def test_then_only_near_the_newest_older_session(self):
        base = olds(best10min_kmh=lambda i: 20 + i)  # newest older session: 10 days ago
        recent_record = act(2, 1, best10min_kmh=30.0)
        for days, want in ((35, [("all", 2), ("then", 1)]), (50, [("all", 2)])):  # window: 10 + 30 days
            backfilled = act(1, days, best10min_kmh=22.0)
            older = [act(300 + i, days + 1 + i, best10min_kmh=21.0) for i in range(5)]  # it beats these
            self.assertEqual(self.recs(base + older + [backfilled, recent_record], [1, 2]), want, days)

    def test_string_ids_match(self):
        self.assertEqual(self.recs(olds(best10min_kmh=20.0) + [act(1, 1, best10min_kmh=25.0)], ["1"]), [("all", 1)])

    # ---- which metrics

    def test_virtual_power_only(self):
        for vt in ("Virtual Ride", "Virtual Run"):
            acts = olds(type=vt, best10min_kmh=40.0, km=30.0, pw20m=180.0) + \
                [act(1, 1, type=vt, best10min_kmh=58.0, km=60.0, pw20m=200.0)]
            ks = [r["k"] for r in self.run_js(acts, [1])]
            self.assertEqual(ks, ["pw20m"], vt)

    def test_sports_kept_apart(self):
        rides = [act(200 + i, 10 + i, type="Ride", best10min_kmh=40.0) for i in range(5)]
        skate = olds(best10min_kmh=20.0) + [act(1, 1, best10min_kmh=25.0)]
        out = self.run_js(rides + skate, [1])
        self.assertEqual([(r["k"], r["type"]) for r in out], [("best10min_kmh", SK)])

    def test_excluded_metrics_never_announced(self):
        acts = olds(kmh_moving=20.0, ascent_m=10.0, moving_s=3600.0, best30_kmh=20.0, kmh_max=30.0, km=20.0) + \
            [act(1, 1, kmh_moving=30.0, ascent_m=999.0, moving_s=9999.0, best30_kmh=40.0, kmh_max=60.0, km=21.0)]
        self.assertEqual([r["k"] for r in self.run_js(acts, [1])], ["km"])

    def test_wind_neutral_needs_older_out_and_backs(self):
        acts = olds(best10min_kmh=30.0) + [act(1, 1, best10min_kmh=20.0, wind_neutral=21.0)]
        self.assertEqual(self.run_js(acts, [1]), [])

    def test_order_all_first_then_group(self):
        acts = olds(best10min_kmh=20.0, wind_neutral=19.0, km=10.0, b60=18.0) + \
            [act(1, 3, best10min_kmh=21.0), act(2, 1, km=50.0, wind_neutral=25.0, b60=19.0, best10min_kmh=20.5)]
        out = [(r["scope"], r["k"]) for r in self.run_js(acts, [1, 2])]
        self.assertEqual(out, [("all", "best10min_kmh"), ("all", "wind_neutral"), ("all", "b60"), ("all", "km")])

    # ---- laps

    def lapper(self, id, days, best, **kw):
        return act(id, days, laps_n=6, lap_km=0.19, lap_center=[45.0, 7.0], best_lap_s=best, **kw)

    def test_best_lap(self):
        acts = [self.lapper(100 + i, 10 + i, 60.0 + i) for i in range(3)] + [self.lapper(1, 1, 58.0)]
        out = [(r["scope"], r["kind"], r["aid"], r["value"], r["prev"]) for r in self.run_js(acts, [1])]
        self.assertEqual(out, [("all", "lap", 1, 58.0, 60.0)])
        self.assertEqual(self.run_js(acts[1:3] + [self.lapper(1, 1, 50.0)], [1]), [])  # 2 earlier only
        self.assertEqual(self.run_js(acts[:3] + [self.lapper(1, 1, 60.4)], [1]), [])  # same second

    def test_lap_on_another_circuit_is_not_compared(self):
        acts = [self.lapper(100 + i, 10 + i, 60.0) for i in range(3)] + \
            [act(1, 1, laps_n=6, lap_km=0.19, lap_center=[45.01, 7.0], best_lap_s=40.0)]
        self.assertEqual(self.run_js(acts, [1]), [])

    # ---- segments

    def seg(self, id, efforts, m=2500, full=False):
        return {"id": id, "m": m, "full": full, "kind": "line", "efforts": efforts}

    def test_segment_record_needs_five_other_sessions(self):
        base = olds(6)
        eff = [[a["id"], 400 + i] for i, a in enumerate(base)]
        new = act(1, 1)
        out = self.run_js(base + [new], [1], {SK: [self.seg("s1", eff + [[1, 390], [1, 420]])]})
        self.assertEqual([(r["scope"], r["kind"], r["sgid"], r["value"], r["prev"]) for r in out],
                         [("all", "segment", "s1", 390, 400)])
        self.assertEqual(self.run_js(base + [new], [1], {SK: [self.seg("s1", eff[:4] + [[1, 390]])]}), [])

    def test_segment_flood_capped_per_session(self):
        base = olds(6)
        eff = [[a["id"], 400] for a in base] + [[1, 300]]
        segs = [self.seg(f"f{i}", eff, m=5000 + i * 1000, full=True) for i in range(5)] + [self.seg("p", eff, m=2500)]
        out = [r["sgid"] for r in self.run_js(base + [act(1, 1)], [1], {SK: segs}) if r["kind"] == "segment"]
        self.assertEqual(out, ["p", "f4"])  # the piece, then the longest full one

    def test_segment_effort_of_a_hidden_session_ignored(self):
        base = olds(5)
        eff = [[a["id"], 400] for a in base] + [[999, 100], [1, 390]]  # 999 not in the list (hidden)
        out = self.run_js(base + [act(1, 1)], [1], {SK: [self.seg("s1", eff)]})
        self.assertEqual([(r["sgid"], r["prev"]) for r in out], [("s1", 400)])


if __name__ == "__main__":
    unittest.main()
