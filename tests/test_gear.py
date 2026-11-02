# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Pedro Ayala. Rollbook is free software, see LICENSE.
"""Gear (#10): web/gear.py store, resolution and totals, and /api/gear over HTTP."""

import copy
import http.client
import json
import os
import shutil
import sqlite3
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from unittest import mock

from helpers import tmpdir
from test_end_to_end import make_export, run

import api
import gear
import server
import store

SK = "Inline Skate"


def mem_db(rows):
    """A main.activities like `db build` makes, plus the `activities` view the server queries.
    rows: (id, date, type, km, moving_s)."""
    db = sqlite3.connect(":memory:")
    db.execute("CREATE TABLE activities (id INTEGER PRIMARY KEY, date TEXT, datetime TEXT, type TEXT, km REAL, "
               "moving_s INT, elapsed_s INT, parse_error TEXT, name TEXT)")
    db.executemany("INSERT INTO activities VALUES (?,?,?,?,?,?,?,NULL,'s')",
                   [(i, d, d and f"{d}T08:00:00+00:00", t, km, mv, mv) for i, d, t, km, mv in rows])
    db.execute("CREATE TEMP VIEW activities AS SELECT * FROM main.activities")
    return db


ROWS = [(1, "2026-01-05", SK, 20.0, 3600), (2, "2026-02-05", SK, 30.0, 4000), (3, "2026-03-05", SK, 25.0, 3900),
        (4, "2026-03-06", "Run", 10.0, 3000), (5, "2026-03-07", "Virtual Ride", 40.0, 3600), (6, None, SK, 5.0, 600)]


class Store(unittest.TestCase):
    def setUp(self):
        self.home = tmpdir(self)
        self.db = mem_db(ROWS)
        self.addCleanup(self.db.close)
        self.g = gear.Gear(self.home)

    def op(self, **b):
        return self.g.apply(self.db, {"rev": self.g.data["rev"], **b})

    def skates(self, since="2026-01-01"):
        gid = self.op(op="gear.save", name="Skates", sports=[SK], default_since=since)["id"]
        wid = self.op(op="part.save", kind="wheels", name="W1", specs={"diameter_mm": 110, "hardness_a": 86, "count": 6},
                      service_km=40, mount_on=gid, installed="2026-01-01")["id"]
        return gid, wid

    def view(self):
        return self.g.view(self.db)

    # ---- resolution

    def test_default_explicit_none_and_back(self):
        gid, _ = self.skates("2026-02-01")
        r = self.g.resolver()
        self.assertEqual(r.of(1, SK, "2026-01-05"), (None, None))  # before the default's since
        self.assertEqual(r.of(2, SK, "2026-02-01"), (gid, "default"))  # since == date counts
        self.assertEqual(r.of(4, "Run", "2026-03-06"), (None, None))
        self.assertEqual(r.of(6, SK, None), (None, None))  # no date: no default
        self.op(op="assign", ids=[2], gear=None)
        self.assertEqual(self.g.resolver().of(2, SK, "2026-02-05"), (None, "explicit"))
        self.op(op="assign", ids=[2, 1], gear=gid)
        self.assertEqual(self.g.resolver().of(1, SK, "2026-01-05"), (gid, "explicit"))
        self.op(op="assign", ids=[1, 2], gear="default")
        self.assertEqual(self.g.data["sessions"], {})

    def test_default_history_keeps_older_sessions(self):
        a, _ = self.skates("2026-01-01")
        b = self.op(op="gear.save", name="New skates", sports=[SK])["id"]
        self.op(op="default.set", sport=SK, gear=b, since="2026-03-01")
        r = self.g.resolver()
        self.assertEqual([r.of(i, SK, d)[0] for i, d in ((1, "2026-01-05"), (3, "2026-03-05"))], [a, b])
        self.op(op="default.set", sport=SK, gear=a, since="2026-03-01")  # same day replaces
        self.assertEqual(self.g.data["defaults"][SK], [{"gear": a, "since": "2026-01-01"}, {"gear": a, "since": "2026-03-01"}])
        # repeats are kept: deleting a middle entry never merges the ones around it
        self.op(op="default.set", sport=SK, gear=b, since="2026-02-01")
        self.op(op="default.set", sport=SK, since="2026-02-01", delete=True)
        self.assertEqual(len(self.g.data["defaults"][SK]), 2)
        with self.assertRaises(gear.NotFound):
            self.op(op="default.set", sport=SK, since="2026-02-01", delete=True)
        self.op(op="default.set", sport=SK, gear=None, since="2026-03-01")
        self.assertEqual(self.g.resolver().of(3, SK, "2026-03-05"), (None, None))

    def test_retired_gear_is_no_longer_default(self):
        gid, _ = self.skates()
        self.op(op="gear.retire", id=gid, date="2026-02-05")
        r = self.g.resolver()
        self.assertEqual((r.of(1, SK, "2026-01-05")[0], r.of(2, SK, "2026-02-05")[0]), (gid, None))
        self.op(op="assign", ids=[3], gear=gid)  # an explicit pick still works
        self.assertEqual(self.g.resolver().of(3, SK, "2026-03-05"), (gid, "explicit"))

    # ---- parts and totals

    def test_totals_and_service(self):
        gid, wid = self.skates()
        v = self.view()
        t = dict(v["totals"]["gear"][gid])
        recent = t.pop("recent")
        self.assertEqual(t.pop("months"), {"2026-01": [20.0, 1], "2026-02": [30.0, 1], "2026-03": [25.0, 1]})
        self.assertEqual(t, {"sessions": 3, "km": 75.0, "moving_s": 11500, "first": "2026-01-05", "last": "2026-03-05"})
        self.assertEqual([(r["id"], r["src"]) for r in recent], [(3, "default"), (2, "default"), (1, "default")])
        t = v["totals"]["parts"][wid]
        self.assertEqual((t["km"], t["km_since_service"], t["due"], v["due"]), (75.0, 75.0, True, [wid]))
        self.op(op="part.service", id=wid, date="2026-02-05", action="rotate", note="")
        t = self.view()["totals"]["parts"][wid]
        self.assertEqual((t["km_since_service"], t["due"], t["service_left"]), (55.0, True, -15.0))  # service day counts after
        self.op(op="part.service", id=wid, date="2026-03-01", action="service", note="")
        t = self.view()["totals"]["parts"][wid]
        self.assertEqual((t["km_since_service"], t["due"], t["last_service"]), (25.0, False, "2026-03-01"))

    def test_due_at_exactly_the_interval_and_offset(self):
        gid, wid = self.skates()
        self.op(op="part.save", id=wid, service_km=75)
        self.assertTrue(self.view()["totals"]["parts"][wid]["due"])
        self.op(op="part.save", id=wid, service_km=100, km_offset=30)  # used part, never serviced
        t = self.view()["totals"]["parts"][wid]
        self.assertEqual((t["km_total"], t["km_since_service"], t["due"]), (105.0, 105.0, True))
        self.op(op="part.retire", id=wid, date="2026-03-10")
        self.assertFalse(self.view()["totals"]["parts"][wid]["due"])  # retired: never due

    def test_mount_dates_and_swap(self):
        gid, w1 = self.skates()
        self.op(op="mount.set", gear=gid, index=0, to="2026-02-05")  # off on the 5th: that day is the new one's
        w2 = self.op(op="part.save", kind="wheels", name="W2", mount_on=gid, installed="2026-02-05")["id"]
        t = self.view()["totals"]["parts"]
        self.assertEqual((t[w1]["km"], t[w2]["km"]), (20.0, 55.0))
        self.assertEqual(self.g.resolver().parts(gid, "2026-02-05"), [w2])
        other = self.op(op="gear.save", name="Other", sports=[SK])["id"]
        with self.assertRaises(ValueError):  # no install date
            self.op(op="mount.add", gear=other, part=w2)
        with self.assertRaises(ValueError):  # a part on two gears at once
            self.op(op="mount.add", gear=other, part=w2, **{"from": "2026-03-01"})
        self.op(op="mount.add", gear=other, part=w1, **{"from": "2026-02-05"})  # w1 is free again: fine
        with self.assertRaises(ValueError):
            self.op(op="mount.set", gear=gid, index=0, **{"from": "2026-02-05", "to": "2026-02-01"})

    def test_part_on_two_gears_adds_up(self):
        a, w = self.skates()
        self.op(op="mount.set", gear=a, index=0, to="2026-03-01")
        b = self.op(op="gear.save", name="B", sports=[SK])["id"]
        self.op(op="mount.add", gear=b, part=w, **{"from": "2026-03-01"})
        self.op(op="assign", ids=[3], gear=b)
        self.assertEqual(self.view()["totals"]["parts"][w]["km"], 75.0)

    def test_retire_part_closes_mounts(self):
        gid, wid = self.skates()
        self.op(op="part.retire", id=wid, date="2026-02-01")
        self.assertEqual(self.g.data["gear"][gid]["parts"][0]["to"], "2026-02-01")
        self.assertEqual(self.view()["totals"]["parts"][wid]["km"], 20.0)
        with self.assertRaises(ValueError):  # before it was installed
            self.op(op="part.retire", id=wid, date="2025-12-01")

    def test_trainer_km_not_on_tyre_service(self):
        bike = self.op(op="gear.save", name="Bike", sports=["Virtual Ride"], default_since="2026-01-01")["id"]
        tyre = self.op(op="part.save", kind="tyre", name="T", service_km=1000, mount_on=bike, installed="2026-01-01")["id"]
        chain = self.op(op="part.save", kind="chain", name="C", service_km=1000, mount_on=bike, installed="2026-01-01")["id"]
        t = self.view()["totals"]["parts"]
        self.assertEqual((t[tyre]["km"], t[tyre]["sessions"], t[tyre]["km_since_service"], t[chain]["km_since_service"]),
                         (0.0, 0, 0.0, 40.0))

    def test_hidden_sessions_left_out(self):
        gid, _ = self.skates()
        self.db.execute("DROP VIEW temp.activities")
        self.db.execute("CREATE TEMP VIEW activities AS SELECT * FROM main.activities WHERE id != 2")  # as edits hides
        self.assertEqual(self.view()["totals"]["gear"][gid]["sessions"], 2)

    # ---- references

    def test_delete_rules(self):
        gid, wid = self.skates()
        with self.assertRaises(gear.Conflict):
            self.op(op="part.delete", id=wid)  # it was on a gear
        with self.assertRaises(gear.Conflict):
            self.op(op="gear.delete", id=gid)  # it is a default
        spare = self.op(op="part.save", kind="bearings", name="B")["id"]
        self.op(op="part.delete", id=spare)
        g2 = self.op(op="gear.save", name="G2", sports=[SK])["id"]
        self.op(op="assign", ids=[3], gear=g2)
        with self.assertRaises(gear.Conflict):
            self.op(op="gear.delete", id=g2)
        self.g.data["sessions"] = {"999": {"gear": g2}}  # only a session that no longer exists
        self.op(op="gear.delete", id=g2)
        self.assertEqual(self.g.data["sessions"], {})
        for bad in ({"op": "part.save", "id": "0" * 16, "name": "x"}, {"op": "gear.retire", "id": "nope"},
                    {"op": "assign", "ids": [12345], "gear": gid}, {"op": "assign", "ids": [1], "gear": "f" * 16}):
            with self.subTest(bad=bad), self.assertRaises(gear.NotFound):
                self.op(**bad)

    # ---- validation

    def test_input_checks(self):
        gid, wid = self.skates()
        bad = [
            dict(op="part.save", kind="wheels", name="x", specs={"diameter_mm": float("nan")}),
            dict(op="part.save", kind="wheels", name="x", specs={"diameter_mm": True}),
            dict(op="part.save", kind="wheels", name="x", specs={"diameter_mm": 300}),
            dict(op="part.save", kind="wheels", name="x", specs={"count": 6.5}),
            dict(op="part.save", kind="wheels", name="x", specs={"evil": 1}),
            dict(op="part.save", kind="wheels", name="x", specs={"__proto__": 1}),
            dict(op="part.save", kind="frame", name="x", specs={"material": "wood"}),
            dict(op="part.save", kind="nope", name="x"),
            dict(op="part.save", kind="wheels", name=""),
            dict(op="part.save", kind="wheels", name="a\u202eb"),
            dict(op="part.save", kind="wheels", name="x" * 121),
            dict(op="part.save", kind="wheels", name="x", bought="20260101"),
            dict(op="part.save", kind="wheels", name="x", bought="2026-02-30"),
            dict(op="part.save", kind="wheels", name="x", bought="2026-W01-1"),
            dict(op="part.save", kind="wheels", name="x", service_km=0),
            dict(op="gear.save", name="x", sports=["__proto__"]),
            dict(op="gear.save", name="x", sports="Run"),
            dict(op="default.set", sport="constructor", gear=None, since="2026-01-01"),
            dict(op="part.service", id=wid, date="2026-01-01", action="paint"),
            dict(op="assign", ids=[True], gear=None),
            dict(op="assign", ids=list(range(1, 2002)), gear=None),
            dict(op="assign", ids=[1]),
            dict(op="mount.delete", gear=gid, index="0"),
            dict(op="nope"),
            dict(op=["part.save"]),
        ]
        before = copy.deepcopy(self.g.data)
        for b in bad:
            with self.subTest(b=str(b)[:60]), self.assertRaises((ValueError, gear.NotFound)):
                self.op(**b)
        self.assertEqual(self.g.data, before)  # nothing half-applied

    def test_ids_are_made_here(self):
        gid = self.op(op="gear.save", name="x", sports=[SK])["id"]
        self.assertRegex(gid, r"^[0-9a-f]{16}$")
        with self.assertRaises(gear.NotFound):  # a client-chosen id never creates
            self.op(op="gear.save", id="a" * 16, name="y")

    def test_stale_rev_conflicts(self):
        self.skates()
        with self.assertRaises(gear.Conflict):
            self.g.apply(self.db, {"op": "gear.save", "rev": 0, "name": "x"})
        with self.assertRaises(gear.Conflict):
            self.g.apply(self.db, {"op": "gear.save", "name": "x"})

    # ---- file

    def test_file_private_with_backup_and_reload(self):
        gid, wid = self.skates()
        path = os.path.join(self.home, "gear.json")
        if os.name == "posix":
            self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
        self.assertEqual(store.load_object(path + ".bak")["rev"], self.g.data["rev"] - 1)
        g2 = gear.Gear(self.home)
        self.assertIsNone(g2.error)
        self.assertEqual(g2.data, self.g.data)

    def test_bad_file_is_never_overwritten(self):
        path = os.path.join(self.home, "gear.json")
        good = gear.empty()
        cases = ["{nope", "[]", json.dumps({**good, "v": 2}), json.dumps({**good, "parts": {"x": {}}}),
                 json.dumps({**good, "gear": {"a" * 16: {"name": "g", "parts": [{"part": "b" * 16, "from": "2026-01-01"}]}}}),
                 json.dumps({**good, "rev": float("inf")}).replace("Infinity", "1e999")]
        for raw in cases:
            with self.subTest(raw=raw[:40]):
                with open(path, "w", encoding="utf-8") as f:
                    f.write(raw)
                g = gear.Gear(self.home)
                self.assertTrue(g.error)
                with self.assertRaises(gear.Broken):
                    g.apply(self.db, {"op": "gear.save", "rev": 0, "name": "x"})
                g.remap({1: 2})
                self.assertTrue(g.view(self.db)["readonly_store"])
                with open(path, encoding="utf-8") as f:
                    self.assertEqual(f.read(), raw)  # untouched
        with open(path, "wb") as f:
            f.write(b"x" * (gear.MAX_FILE + 1))
        g = gear.Gear(self.home)
        self.assertIn("too big", g.error_detail)
        self.assertNotIn(self.home, g.view(self.db)["error"])  # no paths to a client
        if os.name == "posix":
            os.remove(path)
            target = os.path.join(self.home, "elsewhere.json")
            with open(target, "w") as f:
                json.dump(good, f)
            os.symlink(target, path)
            self.assertTrue(gear.Gear(self.home).error)  # opened without following the link

    def test_write_failure_changes_nothing(self):
        self.skates()
        before = copy.deepcopy(self.g.data)
        with mock.patch.object(store, "write_json", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self.op(op="gear.save", name="x", sports=[SK])
        self.assertEqual(self.g.data, before)

    def test_index_ops_check_the_row(self):
        gid, wid = self.skates()
        self.op(op="part.service", id=wid, date="2026-02-01", action="rotate", note="")
        self.op(op="part.service", id=wid, date="2026-01-10", action="service", note="")  # sorts before: indices shift
        with self.assertRaises(gear.Conflict):  # the page still thinks index 0 is the rotation
            self.op(op="part.service", id=wid, delete=0, date="2026-02-01", action="rotate")
        self.assertEqual(len(self.g.data["parts"][wid]["services"]), 2)
        self.op(op="part.service", id=wid, delete=1, date="2026-02-01", action="rotate")
        self.assertEqual([v["action"] for v in self.g.data["parts"][wid]["services"]], ["service"])
        with self.assertRaises(gear.Conflict):
            self.op(op="mount.delete", gear=gid, index=0, part="f" * 16)
        self.op(op="mount.delete", gear=gid, index=0, part=wid)

    def test_untrusted_file_shapes_never_crash(self):
        path = os.path.join(self.home, "gear.json")
        p, g = "a" * 16, "b" * 16
        part = {"kind": "wheels", "name": "w"}
        cases = [{"parts": {p: {"kind": [], "name": "w"}}},
                 {"parts": {p: part}, "gear": {g: {"name": "g", "parts": [{"part": p, "from": "2026-01-01", "to": None},
                                                                          {"part": p, "from": "2026-01-01", "to": "2026-02-01"}]}}},
                 {"parts": {p: {**part, "services": [{"date": "2026-01-01", "action": "service", "note": [[[]]]}]}}},
                 {"sessions": {"1": 5}}, {"defaults": {"Run": [{"since": "2026-01-01", "gear": 7}]}}]
        for c in cases:
            with self.subTest(c=str(c)[:50]):
                with open(path, "w") as f:
                    json.dump({**gear.empty(), **c}, f)
                self.assertTrue(gear.Gear(self.home).error)

    def test_text_limit_after_normalising(self):
        gid = self.op(op="gear.save", name="x", sports=[SK])["id"]
        with self.assertRaises(ValueError):  # each one is 2 characters once normalised: 240 > 120
            self.op(op="gear.save", id=gid, name="\u0958" * 120)
        self.op(op="gear.save", id=gid, name="\u0958" * 60)
        self.assertIsNone(gear.Gear(self.home).error)  # what was accepted loads again

    def test_store_full_is_refused_before_writing(self):
        gid, wid = self.skates()
        with mock.patch.object(gear, "MAX_FILE", len(json.dumps(self.g.data, indent=1)) + 200):
            with self.assertRaises(ValueError):
                self.op(op="gear.save", id=gid, notes="x" * 1000)
        self.assertIsNone(gear.Gear(self.home).error)

    def test_op_edge_cases(self):
        gid, wid = self.skates()
        with self.assertRaises(ValueError):  # existing parts are fitted with mount.add
            self.op(op="part.save", id=wid, mount_on=gid, installed="2026-02-01")
        with self.assertRaises(ValueError):  # a new fitted part needs its install day
            self.op(op="part.save", kind="frame", name="F", mount_on=gid)
        with self.assertRaises(ValueError):  # no sport to be the default of
            self.op(op="gear.save", name="Nothing", sports=[], default_since="2026-01-01")
        with self.assertRaises(ValueError):
            self.op(op="assign", ids=[1], gear=5)
        self.op(op="part.save", id=wid, kind="frame")  # another kind: its old specs go
        self.assertEqual(self.g.data["parts"][wid]["specs"], {})
        with self.assertRaises(ValueError):  # fitted only from 2026-01-01
            self.op(op="part.retire", id=wid, date="2025-12-01")
        self.op(op="gear.retire", id=gid, date="2026-02-10")  # its parts come off
        self.assertEqual(self.g.data["gear"][gid]["parts"][0]["to"], "2026-02-10")
        self.op(op="gear.retire", id=gid, undo=True)
        self.assertIsNone(self.g.data["gear"][gid]["retired"])
        with self.assertRaises(ValueError):
            self.op(op="gear.retire", id=gid)  # a date, or undo

    def test_explicit_choice_on_session_without_date_or_km(self):
        gid, wid = self.skates()
        self.db.execute("UPDATE main.activities SET km=NULL, moving_s=0 WHERE id=1")
        self.op(op="assign", ids=[6, 1], gear=gid)
        t = self.view()["totals"]
        self.assertEqual((t["gear"][gid]["sessions"], t["gear"][gid]["km"]), (4, 60.0))
        self.assertEqual(t["parts"][wid]["sessions"], 3)  # no date: no parts that day

    def test_reconcile_then_any_write_saves_the_move(self):
        self.skates()
        self.op(op="assign", ids=[2], gear=None)
        self.db.execute("UPDATE main.activities SET id=5002 WHERE id=2")
        self.g.view(self.db)
        self.op(op="gear.save", name="other", sports=["Run"])
        self.assertIn("5002", store.load_object(os.path.join(self.home, "gear.json"))["sessions"])

    def test_concurrent_writes_one_wins(self):
        import threading as th
        self.skates()
        rev, out = self.g.data["rev"], []

        def w(n):
            db = mem_db(ROWS)
            try:
                self.g.apply(db, {"op": "gear.save", "rev": rev, "name": n})
                out.append("ok")
            except gear.Conflict:
                out.append("409")
        ts = [th.Thread(target=w, args=(f"g{i}",)) for i in range(4)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        self.assertEqual(sorted(out), ["409", "409", "409", "ok"])

    # ---- an import replaced a session under another id

    def test_choice_follows_replacement(self):
        gid, _ = self.skates()
        self.op(op="assign", ids=[2], gear=None)
        self.g.view(self.db)
        self.db.execute("UPDATE main.activities SET id=5002, datetime='2026-02-05T08:00:30+00:00' WHERE id=2")
        self.assertEqual(self.g.resolver(self.db).of(5002, SK, "2026-02-05"), (None, "explicit"))
        with open(os.path.join(self.home, "gear.json"), encoding="utf-8") as f:
            self.assertIn("2", json.load(f)["sessions"])  # a read never writes
        self.g.remap({2: 5002})  # the importer's hand-over saves it
        self.assertEqual(set(store.load_object(os.path.join(self.home, "gear.json"))["sessions"]), {"5002"})

    def test_remap_direct(self):
        self.skates()
        self.op(op="assign", ids=[2], gear=None)
        self.g.remap({2: 7})
        self.assertEqual(set(self.g.data["sessions"]), {"7"})


class RemapBoth(unittest.TestCase):
    def test_one_store_failing_does_not_stop_the_other(self):
        e, g = mock.Mock(), mock.Mock()
        e.remap.side_effect = OSError("disk full")
        with mock.patch.object(api, "EDITS", e), mock.patch.object(api, "GEAR", g):
            with self.assertRaises(OSError):  # reported to the importer's log
                server._remap_all({1: 2})
        g.remap.assert_called_once_with({1: 2})


class Http(unittest.TestCase):
    """/api/gear served over the two-session export (1001, 1002, Inline Skate)."""

    H = {"X-Rollbook": "1", "Content-Type": "application/json"}

    @classmethod
    def setUpClass(cls):
        cls.home = tempfile.mkdtemp(prefix="rollbook-test-gear-")
        make_export(os.path.join(cls.home, "export"))
        r = run(cls.home, "db", "build", "--no-samples", os.path.join(cls.home, "export"))
        assert r.returncode == 0, r.stdout + r.stderr
        db = os.path.join(cls.home, "rollbook.db")
        cls.httpd, cls.M, cls.url = server.start(db, os.path.join(cls.home, "cache", "web_metrics.json"), 0, offline=True)
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()
        cls.port = cls.httpd.server_address[1]

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        shutil.rmtree(cls.home, True)

    def setUp(self):
        for n in ("gear.json", "gear.json.bak"):
            if os.path.exists(os.path.join(self.home, n)):
                os.remove(os.path.join(self.home, n))
        api.GEAR = gear.Gear(self.home)

    def get(self, path):
        with urllib.request.urlopen(self.url.rstrip("/") + path, timeout=60) as r:
            return json.loads(r.read())

    def post(self, path, body, headers):
        req = urllib.request.Request(self.url.rstrip("/") + path, data=body, method="POST", headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            with e:
                return e.code, e.read()

    def edit(self, body):
        code, raw = self.post("/api/edit", json.dumps(body).encode(), self.H)
        return code, json.loads(raw)

    def raw(self, path, length, body):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        try:
            c.putrequest("POST", path)
            for k, v in (*self.H.items(), ("Content-Length", length)):
                c.putheader(k, v)
            c.endheaders()
            c.send(body)
            r = c.getresponse()
            r.read()
            return r.status
        finally:
            c.close()

    def g(self, body, headers=None):
        code, raw = self.post("/api/gear", json.dumps(body).encode(), self.H if headers is None else headers)
        return code, json.loads(raw) if raw.startswith(b"{") else raw

    def rev(self):
        return self.get("/api/gear")["rev"]

    def test_flow_and_session_fields(self):
        d0 = min(a["date"] for a in self.get("/api/activities?type=Inline%20Skate"))
        code, r = self.g({"op": "gear.save", "rev": 0, "name": "Skates", "sports": [SK], "default_since": d0})
        self.assertEqual(code, 200, r)
        gid = r["id"]
        code, r = self.g({"op": "part.save", "rev": 1, "kind": "wheels", "name": "W", "mount_on": gid, "installed": d0,
                          "specs": {"diameter_mm": 110}})
        self.assertEqual((code, r["gear"]["rev"]), (200, 2))
        rows = self.get("/api/activities?type=Inline%20Skate")
        self.assertEqual({(a["gear_id"], a["gear_src"], a["gear_name"]) for a in rows}, {(gid, "default", "Skates")})
        self.assertEqual(rows[0]["gear"], None)  # Strava's own column is untouched
        a = self.get("/api/activity/1001")
        self.assertEqual([p["name"] for p in a["gear_parts"]], ["W"])
        self.assertEqual(a["gear_default"], {"id": gid, "name": "Skates"})
        self.assertEqual(self.g({"op": "assign", "rev": 2, "ids": [1001], "gear": None})[0], 200)
        self.assertEqual(self.get("/api/activity/1001")["gear_src"], "explicit")
        v = self.get("/api/gear")
        self.assertEqual(v["totals"]["gear"][gid]["sessions"], 1)
        self.assertNotIn("sessions", v)

    def test_sport_edit_moves_session_off_default(self):
        d0 = min(a["date"] for a in self.get("/api/activities?type=Inline%20Skate"))
        gid = self.g({"op": "gear.save", "rev": 0, "name": "Skates", "sports": [SK], "default_since": d0})[1]["id"]
        try:
            self.assertEqual(self.edit({"id": "1002", "type": "Ride"})[0], 200)
            self.assertIsNone(self.get("/api/activity/1002")["gear_id"])
            self.assertEqual(self.get("/api/gear")["totals"]["gear"][gid]["sessions"], 1)
        finally:
            self.edit({"id": "1002", "type": ""})

    def test_errors(self):
        self.assertEqual(self.g({"op": "gear.save", "rev": 5, "name": "x"})[0], 409)
        self.assertEqual(self.g({"op": "gear.save", "rev": 0, "id": "a" * 16, "name": "x"})[0], 404)
        self.assertEqual(self.g({"op": "assign", "rev": 0, "ids": [42], "gear": None})[0], 404)
        self.assertEqual(self.g({"op": "nope", "rev": 0})[0], 400)
        self.assertEqual(self.g({"op": "gear.save", "rev": 0, "name": "<b>", "sports": ["x" * 41]})[0], 400)
        self.assertEqual(self.g({"op": "gear.save", "rev": 0, "name": "x"}, {"Content-Type": "application/json"})[0], 403)
        self.assertEqual(self.raw("/api/gear", str(70000), b"{" + b" " * 69998 + b"}"), 413)
        self.assertEqual(self.rev(), 0)
        with open(os.path.join(self.home, "gear.json"), "w") as f:
            f.write("[]")
        api.GEAR = gear.Gear(self.home)
        code, r = self.g({"op": "gear.save", "rev": 0, "name": "x"})
        self.assertEqual(code, 503)
        self.assertIn("could not be loaded", self.get("/api/gear")["error"])
        self.assertIsNone(self.get("/api/activity/1001")["gear_id"])  # the rest of the dashboard still works


if __name__ == "__main__":
    unittest.main()
