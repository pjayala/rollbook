# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Pedro Ayala. Rollbook is free software, see LICENSE.
"""web/static/gear.js under node: spec lines, service state, filters, and escaping
of user text in the HTML it builds."""

import json
import os
import shutil
import subprocess
import unittest

from helpers import ROOT, tmpdir
from test_frontend_power import _src, extract_const, extract_function

GEAR_JS = os.path.join(ROOT, "web", "static", "gear.js")
NODE = shutil.which("node")
EVIL = "<img src=x onerror=alert(1)>\"'"

KINDS = {"wheels": {"label": "Wheels", "group": "skate", "specs": [
    {"k": "diameter_mm", "label": "Diameter", "type": "num", "unit": "mm", "min": 40, "max": 130},
    {"k": "hardness_a", "label": "Hardness", "type": "num", "unit": "A", "min": 70, "max": 100},
    {"k": "count", "label": "Count", "type": "int", "unit": None, "min": 1, "max": 12},
    {"k": "profile", "label": "Profile", "type": "text", "max": 30}]},
    "boots": {"label": "Boots", "group": "skate", "specs": [{"k": "size_eu", "label": "Size", "type": "num", "unit": None, "min": 20, "max": 52}]}}


@unittest.skipUnless(NODE, "node not installed")
class GearJs(unittest.TestCase):
    def js(self, expr, view=None):
        app = _src()
        with open(GEAR_JS, encoding="utf-8") as f:
            gjs = f.read().replace("\r\n", "\n")
        pre = "\n".join([*(extract_const(app, n) for n in ("esc", "isNum", "f2", "fkm")), extract_function(app, "median")])
        body = (pre + "\nlet $ = () => null; const ICON = {}; const S = {types: [{type: " + json.dumps(EVIL) + ", n: 1, km: 2, first: '2026-01-01'}]};\n" + gjs + "\n"
                + (f"S.gear = gearIndex({json.dumps(view)});\n" if view else "")
                + f"process.stdout.write(JSON.stringify({expr}));\n")
        path = os.path.join(tmpdir(self), "h.js")
        with open(path, "w", encoding="utf-8") as f:
            f.write(body)
        p = subprocess.run([NODE, path], capture_output=True, encoding="utf-8", timeout=60)
        self.assertEqual(p.returncode, 0, p.stderr)
        return json.loads(p.stdout)

    def test_spec_line(self):
        p = {"kind": "wheels", "specs": {"count": 8, "diameter_mm": 90, "hardness_a": 85, "profile": "round"}}
        self.assertEqual(self.js(f"specLine({json.dumps(KINDS)}, {json.dumps(p)})"), "90 mm · 85A · ×8 · round")
        self.assertEqual(self.js(f"specLine({json.dumps(KINDS)}, {{kind: 'boots', specs: {{size_eu: 43}}}})"), "EU 43")
        self.assertEqual(self.js(f"specLine({json.dumps(KINDS)}, {{kind: 'nope'}})"), "")

    def test_service_state(self):
        # the server decides due / left; the page only adds the amber band
        r = self.js("[[{service_km: 200}, {km_since_service: 50, service_left: 150, due: false}],"
                    " [{service_km: 200}, {km_since_service: 185, service_left: 15, due: false}],"
                    " [{service_km: 200}, {km_since_service: 200, service_left: 0, due: true}],"
                    " [{service_km: 200}, {km_since_service: 250, service_left: -50, due: true}],"
                    " [{}, {km_since_service: 250}], [{service_km: 200, retired: '2026-01-01'}, {km_since_service: 250, due: false}]]"
                    ".map(([p, t]) => serviceState(p, t))")
        self.assertEqual([x["cls"] for x in r], ["", "warn", "over", "over", "none", "none"])
        self.assertEqual([x["text"] for x in r[:4]], ["150 km left", "15 km left", "due", "50 km over"])

    def test_part_order_due_first(self):
        P = {"a": {"kind": "boots", "name": "B", "service_km": None}, "b": {"kind": "wheels", "name": "W", "service_km": 100},
             "c": {"kind": "frame", "name": "F", "service_km": 100}}
        T = {"b": {"due": True, "km_since_service": 120, "service_left": -20}, "c": {"km_since_service": 95, "service_left": 5}}
        r = self.js(f"partOrder(['boots', 'frame', 'wheels'], new Map(Object.entries({json.dumps(P)})), "
                    f"new Map(Object.entries({json.dumps(T)})))(['a', 'b', 'c'])")
        self.assertEqual(r, ["b", "c", "a"])

    def test_filter_and_picker(self):
        r = self.js("[gearFilterMatch({gear_id: 'a'}, ''), gearFilterMatch({gear_id: null}, 'none'),"
                    " gearFilterMatch({gear_id: 'a'}, 'none'), gearFilterMatch({gear_id: 'a'}, 'a'), gearFilterMatch({gear_id: 'b'}, 'a')]")
        self.assertEqual(r, [True, True, False, True, False])
        g = {"a": {"name": "A", "sports": ["Run"], "parts": []}, "b": {"name": "B", "sports": ["Run"], "retired": "2026-01-01", "parts": []},
             "c": {"name": "C", "sports": ["Ride"], "parts": []}}
        r = self.js(f"(x => [x.mine.map(g => g.id), x.other.map(g => g.id)])(gearFor(new Map(Object.entries({json.dumps(g)})), 'Run'))")
        self.assertEqual(r, [["a"], ["b", "c"]])

    def test_edit_dialog_gear_field(self):
        view = {"rev": 1, "kinds": KINDS, "actions": [], "sport_group": {}, "due": [], "defaults": {}, "parts": {},
                "totals": {"gear": {}, "parts": {}},
                "gear": {"g1": {"name": "Old", "sports": ["Inline Skate"], "parts": []},
                         "g2": {"name": "New", "sports": ["Inline Skate"], "parts": []}}}
        dom = ("const EL = {'#ed-gear': {value: '', innerHTML: ''}, '#ed-gear-row': {hidden: true}, '#ed-type': {value: ''}};"
               "$ = s => EL[s];")
        r = self.js("(() => {" + dom + r"""
            const a = {id: 7, type: 'Inline Skate', gear_id: 'g1', gear_src: 'default', gear_default: {id: 'g1', name: 'Old'}};
            const f = editGearField(a), sel = EL['#ed-gear'];
            const out = [sel.value, /Default \(Old\)/.test(sel.innerHTML), EL['#ed-gear-row'].hidden, f()];
            sel.value = 'g2'; out.push(f());
            sel.value = 'none'; out.push(f());
            const b = {id: 8, type: 'Inline Skate', gear_id: null, gear_src: 'explicit', gear_default: null};
            const g = editGearField(b); out.push(EL['#ed-gear'].value, g());
            EL['#ed-gear'].value = 'default'; out.push(g());
            return out; })()""", view)
        self.assertEqual(r, ["default", True, False, None, {"op": "assign", "ids": [7], "gear": "g2"},
                             {"op": "assign", "ids": [7], "gear": None}, "none", None,
                             {"op": "assign", "ids": [8], "gear": "default"}])

    def test_gear_compare(self):
        A = lambda g, b10, hr=None, km=10, date="2026-01-01", **kw: {"gear_id": g, "gear_name": g.upper() if g else None,  # noqa: E731
                                                                   "best10min_kmh": b10, "hr_avg": hr, "km": km, "date": date, **kw}
        acts = [A("a", 20, 140), A("a", 22, 150, date="2026-02-01"), A("a", None, 140), A("a", 21, 150),
                A("b", 24), A("b", 25, date="2025-12-01"), A(None, 30), A("b", 99, hidden=1),
                A("v", None, pw20m=200), A("v", None, pw20m=220)]
        r = self.js(f"gearCompare({json.dumps(acts)}, false)")
        self.assertEqual([(x["id"], x["n"], x["nv"], x["med"], x["best"]) for x in r][:2], [("b", 2, 2, 24.5, 25), ("a", 4, 3, 21, 22)])
        a = next(x for x in r if x["id"] == "a")
        self.assertEqual((a["nHr"], round(a["eff"], 3), a["first"], a["last"], a["km"]), (3, 14.286, "2026-01-01", "2026-02-01", 40))
        self.assertIsNone(next(x for x in r if x["id"] == "b")["eff"])  # no HR: no efficiency
        v = self.js(f"gearCompare({json.dumps(acts)}, true)")
        self.assertEqual(next(x for x in v if x["id"] == "v")["med"], 210)  # virtual: power, never simulated speed

    def test_timeline_rows(self):
        P = {"w": {"name": "W", "kind": "wheels", "services": [{"date": "2026-01-15", "action": "rotate"}, {"date": "2025-12-01", "action": "service"}]},
             "f": {"name": "F", "kind": "frame", "services": []}}
        mounts = [{"part": "w", "from": "2026-01-01", "to": None, "i": 0}, {"part": "f", "from": "2025-12-01", "to": "2026-01-01", "i": 1}]
        r = self.js(f"timelineRows({json.dumps(mounts)}, new Map(Object.entries({json.dumps(P)})), '2026-02-01')")
        self.assertEqual((r["lo"], r["hi"]), ("2025-12-01", "2026-02-01"))
        w, f = r["rows"]
        self.assertEqual((w["on"], f["on"], f["left"]), (True, False, 0))
        self.assertAlmostEqual(w["left"] + w["width"], 100, 3)
        self.assertAlmostEqual(f["left"] + f["width"], w["left"], 3)  # one ends where the next starts
        self.assertEqual([k["action"] for k in w["ticks"]], ["rotate"])  # only services while it was on
        self.assertEqual(self.js("timelineRows([], new Map(), '2026-02-01').rows"), [])
        # a fitting dated ahead stays inside the track; a service logged today on a part still on gets its tick
        P2 = {"w": {"name": "W", "kind": "wheels", "services": [{"date": "2026-02-01", "action": "service"}]}}
        r = self.js("timelineRows([{part: 'w', from: '2026-01-01', to: null, i: 0}, {part: 'w', from: '2026-03-01', to: null, i: 1}], "
                    f"new Map(Object.entries({json.dumps(P2)})), '2026-02-01')")
        self.assertEqual(r["hi"], "2026-03-01")
        self.assertTrue(all(x["left"] + x["width"] <= 100.0001 for x in r["rows"]))
        self.assertEqual(len(r["rows"][0]["ticks"]), 1)
        r = self.js("timelineRows([{part: 'w', from: '2026-02-01', to: null, i: 0}], "
                    f"new Map(Object.entries({json.dumps(P2)})), '2026-02-01').rows[0]")
        self.assertEqual((r["left"], r["width"]), (0, 0.6))  # fitted today, zero span: still a visible bar
        y = self.js("timelineRows([{part: 'w', from: '2023-12-20', to: null, i: 0}], "
                    f"new Map(Object.entries({json.dumps(P2)})), '2026-01-05').years.map(x => x.y)")
        self.assertEqual(y, [2025])  # 2024 (too close to the start) and 2026 (the end) dropped

    def test_metric_and_services_window(self):
        r = self.js("[gearMetric('Virtual Ride').key, gearMetric('Inline Skate').key, gearMetric(null).key, gearMetric('Run').unit]")
        self.assertEqual(r, ["pw20m", "best10min_kmh", "best10min_kmh", "km/h"])
        g = {"parts": [{"part": "w", "from": "2026-01-01", "to": "2026-02-01"}]}
        P = {"w": {"services": [{"date": "2025-12-01", "action": "service"}, {"date": "2026-01-15", "action": "rotate"},
                                {"date": "2026-02-01", "action": "replace"}]}}
        r = self.js(f"gearServices({json.dumps(g)}, new Map(Object.entries({json.dumps(P)}))).map(v => [v.i, v.action])")
        self.assertEqual(r, [[1, "rotate"]])  # only while it was on this gear

    def test_gear_diff_in_compare(self):
        with open(os.path.join(ROOT, "web", "static", "compare.js"), encoding="utf-8") as f:
            fn = extract_function(f.read().replace("\r\n", "\n"), "gearDiff")
        K = json.dumps(KINDS)
        w90 = {"id": "w1", "kind": "wheels", "name": "Hydrogen", "specs": {"diameter_mm": 90, "hardness_a": 85}}
        w110 = {"id": "w2", "kind": "wheels", "name": "Matter", "specs": {"diameter_mm": 110, "hardness_a": 86}}
        boots = {"id": "b", "kind": "boots", "name": "Next", "specs": {}}
        A = {"gear_id": "g1", "gear_name": "New", "gear_parts": [w110, boots]}
        B = {"gear_id": "g2", "gear_name": "Old", "gear_parts": [w90, boots]}
        r = self.js(f"(() => {{ {fn}; return [gearDiff({json.dumps(A)}, {json.dumps(B)}, {K}),"
                    f" gearDiff({json.dumps(A)}, {json.dumps(A)}, {K}), gearDiff({{}}, {{}}, {K}),"
                    f" gearDiff({json.dumps(A)}, {{gear_id: null}}, {K})]; }})()")
        self.assertEqual(r[0], {"same": False, "a": "New", "b": "Old",
                                "parts": [{"kind": "wheels", "a": "Matter 110 mm · 86A", "b": "Hydrogen 90 mm · 85A"}]})
        self.assertEqual((r[1]["same"], r[1]["parts"]), (True, []))
        self.assertEqual(r[2]["parts"], [])
        self.assertEqual(r[3]["parts"], [])  # nothing recorded on one side: unknown, not "different"
        same = {"id": "w9", "kind": "wheels", "name": "Matter", "specs": {"hardness_a": 86, "diameter_mm": 110}}  # keys reversed
        other_brand = {**w110, "id": "w8", "brand": "Undercover"}
        C = {"gear_id": "g3", "gear_name": "C", "gear_parts": [same]}
        D = {"gear_id": "g4", "gear_name": "D", "gear_parts": [other_brand]}
        r = self.js(f"(() => {{ {fn}; return [gearDiff({json.dumps(A)}, {json.dumps(C)}, {K}).parts, gearDiff({json.dumps(A)}, {json.dumps(D)}, {K}).parts.length,"
                    f" gearDiff({json.dumps(B)}, {json.dumps(A)}, {K}).parts.map(p => p.kind), gearDiff({json.dumps(A)}, {json.dumps(B)}, null).parts[0].a]; }})()")
        self.assertEqual(r, [[], 1, ["wheels"], "Matter 110 · 86"])

    def test_user_text_escaped(self):
        view = {"rev": 1, "kinds": KINDS, "actions": ["service"], "sport_group": {}, "due": ["p1"], "defaults": {},
                "gear": {"g1": {"name": EVIL, "sports": [EVIL], "parts": [{"part": "p1", "from": "2026-01-01", "to": None}]}},
                "parts": {"p1": {"kind": "wheels", "name": EVIL, "brand": EVIL, "specs": {"profile": EVIL}, "service_km": 10,
                                 "services": [{"date": "2026-01-10", "action": "service", "note": EVIL}], "retired": None}},
                "totals": {"gear": {}, "parts": {"p1": {"km_total": 20, "km_since_service": 20, "sessions": 1, "due": True,
                                                        "service_left": -10, "last_service": EVIL}}}}
        html = self.js("partRows(['p1']) + firstRun() + serviceLog(S.gear, S.gear.G.get('g1')) + "
                       "timelineHtml(timelineRows([{part: 'p1', from: '2026-01-01', to: null, i: 0}], S.gear.P, '2026-02-01')) + gearChip({gear_id: 'g1', gear_name: " + json.dumps(EVIL) + ", gear_src: 'default', "
                       "gear_parts: [{kind: 'wheels', name: " + json.dumps(EVIL) + ", specs: {}}]})", view)
        self.assertNotIn("<img", html)
        self.assertNotIn("\"'", html)
        self.assertIn("&lt;img", html)


if __name__ == "__main__":
    unittest.main()
