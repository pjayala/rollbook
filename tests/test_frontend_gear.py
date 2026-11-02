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
from test_frontend_power import _src, extract_const

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
        pre = "\n".join(extract_const(app, n) for n in ("esc", "isNum", "f2", "fkm"))
        body = (pre + "\nlet $ = () => null; const ICON = {}; const S = {};\n" + gjs + "\n"
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

    def test_user_text_escaped(self):
        view = {"rev": 1, "kinds": KINDS, "actions": ["service"], "sport_group": {}, "due": ["p1"], "defaults": {},
                "gear": {"g1": {"name": EVIL, "sports": [EVIL], "parts": [{"part": "p1", "from": "2026-01-01", "to": None}]}},
                "parts": {"p1": {"kind": "wheels", "name": EVIL, "brand": EVIL, "specs": {"profile": EVIL}, "service_km": 10,
                                 "services": [], "retired": None}},
                "totals": {"gear": {}, "parts": {"p1": {"km_total": 20, "km_since_service": 20, "sessions": 1, "due": True,
                                                        "service_left": -10, "last_service": EVIL}}}}
        html = self.js("partRows(['p1']) + gearChip({gear_id: 'g1', gear_name: " + json.dumps(EVIL) + ", gear_src: 'default', "
                       "gear_parts: [{kind: 'wheels', name: " + json.dumps(EVIL) + ", specs: {}}]})", view)
        self.assertNotIn("<img", html)
        self.assertNotIn("\"'", html)
        self.assertIn("&lt;img", html)


if __name__ == "__main__":
    unittest.main()
