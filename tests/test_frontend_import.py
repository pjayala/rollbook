# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Pedro Ayala. Rollbook is free software, see LICENSE.
"""web/static/import.js under node: the Sync block's per-source lines and the
summary after a sync, with hostile strings from the server."""

import json
import os
import shutil
import subprocess
import unittest

from helpers import tmpdir
from test_frontend_power import _src, extract_const, extract_function

NODE = shutil.which("node")
EVIL = "<img src=x onerror=alert(1)>\"'"


@unittest.skipUnless(NODE, "node not installed")
class ImportJs(unittest.TestCase):
    def js(self, expr):
        app = _src()
        pre = "\n".join([*(extract_const(app, n) for n in ("esc", "isNum", "f0", "SYNC")),
                         *(extract_function(app, n) for n in ("whenLocal", "syncLine", "syncHtml", "syncSummary"))])
        path = os.path.join(tmpdir(self), "i.js")
        with open(path, "w", encoding="utf-8") as f:
            f.write(pre + f"\nprocess.stdout.write(JSON.stringify({expr}));\n")
        p = subprocess.run([NODE, path], capture_output=True, encoding="utf-8", timeout=60,
                           env=dict(os.environ, TZ="UTC"))
        self.assertEqual(p.returncode, 0, p.stderr)
        return json.loads(p.stdout)

    def test_lines(self):
        r = self.js("[null, {configured: false}, {configured: true, auth_expired: true},"
                    " {configured: true, last_sync: '2026-11-23T09:05:00+00:00'}, {configured: true}, {configured: true, last_sync: 'nope'}]"
                    ".map(s => syncLine(SYNC.get('strava'), s))")
        self.assertEqual([x["text"] for x in r], ["", "not set up: run ./rollbook strava auth in a terminal",
                                                 "login expired: run ./rollbook strava auth again (Sync skips it until then)",
                                                 "last synced 2026-11-23 09:05", "never synced", "last synced "])
        self.assertEqual([x["cls"] for x in r], ["muted", "muted", "down", "muted", "muted", "muted"])

    def test_offline_says_nothing_per_source(self):
        self.assertEqual(self.js("syncLine(SYNC.get('strava'), {configured: false}, true).text"), "")
        self.assertNotIn("data-sync", self.js("syncHtml({strava: {configured: true}}, true)"))

    def test_block_only_links_set_up_sources(self):
        html = self.js("syncHtml({strava: {configured: true}, intervals: {configured: false}})")
        self.assertEqual(html.count("data-sync="), 1)
        self.assertIn('data-sync="strava"', html)
        self.assertLess(html.index("Strava"), html.index("intervals.icu"))  # the order Sync runs them in

    def test_summary(self):
        j = {"sources": {"intervals": {"fetched": 1, "partial": True}, "strava": {"error": EVIL, "auth": True},
                         "__proto__": {"fetched": 9}, "x": {"fetched": EVIL}}}
        html = self.js(f"syncSummary({json.dumps(j)})")
        self.assertNotIn("<img", html)
        self.assertIn("&lt;img", html)
        self.assertIn("1 session Strava didn&#39;t have</span>", html)
        self.assertIn("time limit", html)
        self.assertLess(html.index("Strava</b>"), html.index("intervals.icu</b>"))
        self.assertEqual(html.count("<div>"), 2)  # unknown sources ("__proto__", "x") are not shown
        self.assertEqual(self.js("syncSummary({sources: {strava: {fetched: '<b>'}}}) + syncSummary({}) + syncSummary({sources: 5})"),
                         '<div><b>Strava</b>: <span class="muted">0 files downloaded</span></div>')


if __name__ == "__main__":
    unittest.main()
