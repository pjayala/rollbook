# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Pedro Ayala. Rollbook is free software, see LICENSE.
"""`rollbook mcp`: protocol, tools, read-only guarantees, GPS redaction."""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

from helpers import TOOL
from test_end_to_end import make_export, run

import mcp_server


class MCP(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.home = tempfile.mkdtemp(prefix="rollbook-test-mcp-")
        export = os.path.join(cls.home, "export")
        make_export(export)
        for args in (("db", "build", export), ("db", "enrich")):
            r = run(cls.home, *args)
            assert r.returncode == 0, r.stdout + r.stderr
        cls.db = os.path.join(cls.home, "rollbook.db")
        cls.cache = os.path.join(cls.home, "cache", "web_metrics.json")

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.home, True)

    def setUp(self):
        self.srv = mcp_server.Server(mcp_server.Rollbook(self.db, self.cache), "test")
        self.addCleanup(self.srv.rb.close)

    def rpc(self, method, params=None, mid=1):
        msg = {"jsonrpc": "2.0", "id": mid, "method": method}
        if params is not None:
            msg["params"] = params
        return self.srv.handle(msg)

    def call(self, name, ok=True, **args):
        res = self.rpc("tools/call", {"name": name, "arguments": args})["result"]
        self.assertEqual(res["isError"], not ok, res["content"][0]["text"])
        text = res["content"][0]["text"]
        return json.loads(text) if ok else text

    def test_profile_hr_max_file_not_an_object(self):
        path = os.path.join(self.home, "profile.json")
        self.addCleanup(lambda: os.path.exists(path) and os.remove(path))
        db_max = self.srv.rb.hrmax() if not os.path.exists(path) else None
        for raw in ("[]", '"x"', '{"hr_max": "abc"}', '{"hr_max": NaN}', "{bad", '{"hr_max": true}',
                    '{"hr_max": 300}', '{"hr_max": 1e999}', '{"hr_max": [190]}'):
            with open(path, "w") as f:
                f.write(raw)
            self.assertEqual(self.srv.rb.hrmax(), db_max, raw)  # falls back to the recorded max
        with open(path, "w") as f:
            f.write('{"hr_max": 188}')
        self.assertEqual(self.srv.rb.hrmax(), 188)

    # ------------------------------------------------------------ protocol

    def test_initialize_negotiates_version(self):
        r = self.rpc("initialize", {"protocolVersion": "2024-11-05", "capabilities": {}})["result"]
        self.assertEqual(r["protocolVersion"], "2024-11-05")
        self.assertIn("tools", r["capabilities"])
        self.assertIn("best10min_kmh", r["instructions"])
        r = self.rpc("initialize", {"protocolVersion": "1999-01-01"})["result"]
        self.assertEqual(r["protocolVersion"], mcp_server.PROTOCOLS[0])

    def test_tools_list_is_read_only(self):
        tools = self.rpc("tools/list")["result"]["tools"]
        self.assertEqual({t["name"] for t in tools},
                         {"sports", "schema", "query", "activities", "activity", "compare", "trend", "records", "gear"})
        for t in tools:
            self.assertTrue(t["annotations"]["readOnlyHint"])
            self.assertEqual(t["inputSchema"]["type"], "object")

    def test_errors(self):
        self.assertEqual(self.rpc("nope")["error"]["code"], -32601)
        self.assertIsNone(self.srv.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}))
        self.assertEqual(self.srv.handle({"id": 3})["error"]["code"], -32600)
        self.assertIn("unknown tool", self.call("nope", ok=False))
        self.assertIn("no activity", self.call("activity", ok=False, id=42))
        self.assertIn("order_by", self.call("activities", ok=False, order_by="name; DROP"))

    def test_stdio_subprocess(self):
        lines = [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "sports", "arguments": {}}},
        ]
        inp = "\n".join(json.dumps(m) for m in lines) + "\nnot json\n"
        env = dict(os.environ, ROLLBOOK_HOME=self.home)
        r = subprocess.run([sys.executable, TOOL, "mcp"], input=inp.encode(), env=env,
                           capture_output=True, timeout=60)
        out = [json.loads(x) for x in r.stdout.decode().splitlines()]
        self.assertEqual([m.get("id") for m in out], [1, 2, None])
        self.assertEqual(out[0]["result"]["serverInfo"]["name"], "rollbook")
        sports = json.loads(out[1]["result"]["content"][0]["text"])
        self.assertEqual(sports[0]["type"], "Inline Skate")
        self.assertEqual(out[2]["error"]["code"], -32700)

    # ------------------------------------------------------------ tools

    def test_sports_and_schema(self):
        s = self.call("sports")
        self.assertEqual((s[0]["type"], s[0]["n"]), ("Inline Skate", 2))
        names = {t.get("name") for t in self.call("schema")}
        self.assertTrue({"activities", "samples", "splits", "v_activities"} <= names)

    def test_activities_sorted_and_filtered(self):
        r = self.call("activities", sport="inline skate", order_by="km", limit=1)
        self.assertEqual(r["matching"], 2)
        row = dict(zip(r["columns"], r["rows"][0]))
        self.assertEqual((row["id"], row["km"]), (1001, 18.206))
        r = self.call("activities", search="come back")
        self.assertEqual([x[0] for x in r["rows"]], [1002])
        self.assertEqual(self.call("activities", sport="Run")["matching"], 0)

    def test_activity_detail(self):
        r = self.call("activity", id=1001)
        self.assertEqual(r["activity"]["km"], 18.206)
        self.assertEqual(len(r["splits"]), 18)
        self.assertEqual(r["derived"]["route"], "out-back")
        self.assertIn("wind_neutral", r["derived"])
        self.assertIn("hr_zones", r)
        self.assertNotIn("trk", r["derived"])
        self.assertNotIn("ll0", r["derived"])
        be = r["derived"]["best_efforts"]  # #6, no coordinates
        self.assertEqual(set(be), {"400", "1k", "5k", "10k"})
        self.assertGreater(be["5k"]["seconds"], 5 * be["1k"]["seconds"] - 1e-6)
        self.assertNotIn("splits", self.call("activity", id=1001, splits=False))

    def test_compare_trend_records(self):
        c = self.call("compare", ids=[1001, 1002])
        self.assertEqual([x["id"] for x in c], [1001, 1002])
        self.assertIn("best10min_kmh_per_bpm", c[0])
        self.assertIn("ids", self.call("compare", ok=False, ids=[1001]))
        t = self.call("trend", by="year")
        self.assertEqual(sum(p["n"] for p in t["periods"]), 2)
        rec = self.call("records", limit=1)
        self.assertEqual(rec["km"][0]["id"], 1001)
        self.call("activity", id=1001); self.call("activity", id=1002)
        rec = self.call("records", limit=2)
        if "best_effort_1k_s" in rec:  # derived records need the metrics cache
            per = rec["best_effort_1k_s"]["Inline Skate"]
            self.assertLessEqual(per[0]["seconds"], per[-1]["seconds"])  # fastest first

    # ------------------------------------------------------------ safety

    def test_query_basics(self):
        r = self.call("query", sql="SELECT id FROM activities ORDER BY id", max_rows=1)
        self.assertEqual((r["rows"], r["truncated"]), ([[1001]], True))
        r = self.call("query", sql="PRAGMA table_info(splits)")
        self.assertIn("kmh", [x[1] for x in r["rows"]])
        self.assertIn("statement", self.call("query", ok=False, sql="SELECT 1; SELECT 2"))

    def test_query_cannot_write(self):
        for sql in ("CREATE TEMP TABLE x(a)", "DROP VIEW temp.activities",
                    "DELETE FROM main.activities", "UPDATE activities SET name='x'",
                    f"ATTACH '{os.path.join(self.home, 'x.db')}' AS x",
                    "PRAGMA journal_mode=WAL", "PRAGMA query_only=0"):
            self.assertIn("sql:", self.call("query", ok=False, sql=sql), sql)
        self.assertFalse(os.path.exists(os.path.join(self.home, "x.db")))
        # the overlay survived
        self.assertEqual(self.call("sports")[0]["n"], 2)

    def test_gps_redacted_unless_allowed(self):
        sql = "SELECT COUNT(lat), COUNT(lon), COUNT(hr) FROM samples"
        lat, lon, hr = self.call("query", sql=sql)["rows"][0]
        self.assertEqual((lat, lon), (0, 0))
        self.assertGreater(hr, 0)
        self.assertEqual(self.call("query", sql="SELECT * FROM samples WHERE lat IS NOT NULL LIMIT 1")["rows"], [])
        self.srv.rb.allow_gps = True
        self.assertGreater(self.call("query", sql=sql)["rows"][0][0], 0)

    def test_metrics_cache_not_written(self):
        self.call("activity", id=1002)
        self.assertFalse(os.path.exists(self.cache))

    def test_dashboard_edits_applied_and_reloaded(self):
        path = os.path.join(self.home, "edits.json")
        self.addCleanup(lambda: os.path.exists(path) and os.remove(path))
        with open(path, "w") as f:
            json.dump({"sessions": {"1001": {"name": "Renamed"}, "1002": {"hidden": True}}}, f)
        r = self.call("activities")
        self.assertEqual([x[3] for x in r["rows"]], ["Renamed"])
        self.assertEqual(self.call("activities", include_hidden=True)["matching"], 2)
        self.assertEqual(self.call("query", sql="SELECT COUNT(*) FROM main.activities")["rows"], [[2]])
        os.remove(path)
        self.assertEqual(self.call("activities")["matching"], 2)


    def test_gear_tool(self):
        import gear
        path = os.path.join(self.home, "gear.json")
        self.addCleanup(lambda: [os.remove(p) for p in (path, path + ".bak") if os.path.exists(p)])
        self.assertEqual(self.call("gear")["gear"], [])  # no file: empty, not an error
        p, g = "a" * 16, "b" * 16
        with open(path, "w") as f:
            json.dump({**gear.empty(), "rev": 3,
                       "parts": {p: {"kind": "wheels", "name": "W", "brand": "Matter", "specs": {"diameter_mm": 110},
                                     "service_km": 10, "notes": "SECRET-NOTE", "bought": "2020-01-02",
                                     "services": [{"date": "2020-01-03", "action": "service", "note": "SECRET-SVC"}]}},
                       "gear": {g: {"name": "Skates", "sports": ["Inline Skate"], "notes": "SECRET-GEAR",
                                    "parts": [{"part": p, "from": "2020-01-01", "to": None}]}},
                       "defaults": {"Inline Skate": [{"gear": g, "since": "2020-01-01"}]}}, f)
        with open(path, "rb") as f:
            before = f.read()
        r = self.call("gear")
        self.assertEqual([(x["name"], x["sessions"], x["default_for"]) for x in r["gear"]], [("Skates", 2, ["Inline Skate"])])
        w = r["gear"][0]["parts"][0]
        self.assertEqual((w["name"], w["due"], w["km"] > 30), ("W", True, True))
        self.assertEqual(r["due"], ["W"])
        text = json.dumps(r) + json.dumps(self.call("activity", id=1001, splits=False))
        for secret in ("SECRET", "2020-01-02"):
            self.assertNotIn(secret, text)  # notes and the purchase date never reach the model
        self.assertEqual(w["last_service"], "2020-01-03")
        self.assertEqual(self.call("activity", id=1001, splits=False)["gear"]["name"], "Skates")
        self.assertEqual(self.call("gear", sport="Run")["gear"], [])
        with open(path, "rb") as f:
            self.assertEqual(f.read(), before)  # never written
        self.assertFalse(os.path.exists(path + ".bak"))
        with open(path, "w") as f:
            f.write("[]")
        self.assertIn("could not be loaded", self.call("gear", ok=False))
        self.assertNotIn("gear", self.call("activity", id=1001, splits=False))  # the rest still works

    def write_gear(self, data):
        import gear
        path = os.path.join(self.home, "gear.json")
        with open(path, "w") as f:
            json.dump({**gear.empty(), **data}, f)
        st = os.stat(path)
        os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 2_000_000_000))  # a new key even on coarse clocks
        self.addCleanup(lambda: os.path.exists(path) and os.remove(path))
        return path

    def test_gear_as_of_today_and_never_writes(self):
        import gear
        import store
        from unittest import mock
        p, q, g, h = "a" * 16, "c" * 16, "b" * 16, "d" * 16
        self.write_gear({
            "parts": {p: {"kind": "wheels", "name": "Now"}, q: {"kind": "wheels", "name": "Later"}},
            "gear": {g: {"name": "Old", "sports": ["Inline Skate"], "retired": "2026-01-01",
                         "parts": [{"part": p, "from": "2020-01-01", "to": None}]},
                     h: {"name": "Next", "sports": ["Inline Skate"], "parts": [{"part": q, "from": "2999-01-01", "to": None}]}},
            "defaults": {"Inline Skate": [{"gear": g, "since": "2020-01-01"}, {"gear": h, "since": "2999-01-01"}]},
            "sessions": {"1002": {"gear": h}}})
        with mock.patch.object(store, "write_json", side_effect=AssertionError("wrote")), \
                mock.patch.object(gear.Gear, "remap", side_effect=AssertionError("remap")):
            r = self.call("gear", include_retired=True)
            a = self.call("activity", id=1002, splits=False)
        by = {x["name"]: x for x in r["gear"]}
        self.assertEqual(by["Old"]["default_for"], [])  # retired: no longer anybody's default
        self.assertEqual(by["Next"]["default_for"], [])  # its default starts in the future
        self.assertEqual([x["name"] for x in by["Next"]["parts"]], [])  # fitted only from 2999
        self.assertEqual([x["name"] for x in r["spare_parts"]], ["Later"])
        self.assertEqual((a["gear"]["name"], a["gear"]["source"]), ("Next", "explicit"))
        self.assertEqual(self.call("gear", sport="inline skate")["gear"], [x for x in self.call("gear")["gear"]])

    def test_gear_broken_store_is_a_tool_error(self):
        self.write_gear({})
        with open(os.path.join(self.home, "gear.json"), "w") as f:
            f.write('{"v":')
        lines = [json.dumps({"jsonrpc": "2.0", "id": i, "method": m, "params": pa}) for i, (m, pa) in enumerate(
            [("tools/list", {}), ("tools/call", {"name": "gear", "arguments": {}}),
             ("tools/call", {"name": "activity", "arguments": {"id": 1001, "splits": False}})], 1)]
        srv = mcp_server.Server(mcp_server.Rollbook(self.db, self.cache), "test")
        self.addCleanup(srv.rb.close)
        out = []
        for ln in lines:
            r = srv.handle(json.loads(ln))
            out.append(json.dumps(r))
        msgs = [json.loads(x) for x in out]
        self.assertTrue(msgs[1]["result"]["isError"])
        self.assertNotIn("JSONDecodeError", msgs[1]["result"]["content"][0]["text"])
        self.assertFalse(msgs[2]["result"]["isError"])  # the other tools keep working


if __name__ == "__main__":
    unittest.main()
