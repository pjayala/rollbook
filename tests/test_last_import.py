# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Pedro Ayala. Rollbook is free software, see LICENSE.
"""The new-records summary (#17): web/lastimport.py store, GET /api/last_import
and POST /api/last_import/dismiss, including the hostile-input and CSRF cases."""

import glob
import json
import os
import shutil
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from unittest import mock

from helpers import ROOT, tmpdir
from test_end_to_end import make_export, run

import lastimport
import server


class Store(unittest.TestCase):
    def setUp(self):
        self.path = os.path.join(tmpdir(self), "last_import.json")

    def fresh(self):
        return lastimport.LastImport(self.path)

    def raw(self, body):
        with open(self.path, "wb") as f:
            f.write(body if isinstance(body, bytes) else json.dumps(body).encode())

    def test_record_and_reload(self):
        s = self.fresh()
        self.assertIsNone(s.get())
        d = s.record([3, 1, 2], ["export_1.zip"], "upload")
        self.assertEqual((d["new_ids"], d["new_total"], d["dismissed"]), ([3, 1, 2], 3, False))
        self.assertEqual(self.fresh().get(), s.get())

    def test_file_is_0600_and_no_tmp_left(self):
        self.fresh().record([1], ["x"])
        if os.name == "posix":
            self.assertEqual(os.stat(self.path).st_mode & 0o777, 0o600)
        self.assertEqual(glob.glob(self.path + "*.tmp"), [])

    def test_undismissed_merges(self):
        s = self.fresh()
        a = s.record([1, 2], ["a.zip"], "upload")
        b = s.record([2, 3], ["Strava"], "strava", replaced=1)
        self.assertEqual((b["new_ids"], b["new_total"], b["items"], b["source"], b["replaced"]),
                         ([1, 2, 3], 4, ["a.zip", "Strava"], "mixed", 1))
        self.assertGreater(b["finished"], a["finished"])

    def test_partial_describes_the_newest_import(self):
        s = self.fresh()
        self.assertTrue(s.record([1], ["a"], partial=True)["partial"])
        self.assertFalse(s.record([2], ["b"])["partial"])  # a clean rerun finished the job
        self.assertTrue(s.record([3], ["c"], partial=True)["partial"])

    def test_replacement_follows_an_unseen_session(self):
        # bare upload 11 announced; before dismissal an export replaces it with 5011
        s = self.fresh()
        s.record([10, 11], ["a.fit"])
        d = s.record([], ["export.zip"], replaced=1, remap={11: 5011})
        self.assertEqual((d["new_ids"], d["new_total"], d["replaced"]), ([10, 5011], 2, 0))
        # once seen, a replacement is just counted
        s.dismiss(d["finished"])
        d = s.record([], ["export.zip"], replaced=1, remap={10: 5010})
        self.assertEqual((d["new_ids"], d["new_total"], d["replaced"]), ([], 0, 1))

    def test_dismissed_is_replaced(self):
        s = self.fresh()
        a = s.record([1, 2], ["a"])
        self.assertTrue(s.dismiss(a["finished"]))
        b = s.record([9], ["b"])
        self.assertEqual((b["new_ids"], b["items"], b["dismissed"]), ([9], ["b"], False))

    def test_dismiss_needs_current_finished(self):
        s = self.fresh()
        self.assertIsNone(s.dismiss(1.0))
        a = s.record([1], ["a"])
        b = s.record([2], ["b"])  # a newer import arrived after the page loaded a
        self.assertIs(s.dismiss(a["finished"]), False)
        for bad in (None, "x", float("nan"), [b["finished"]], True):
            self.assertIs(s.dismiss(bad), False, bad)
        self.assertFalse(s.get()["dismissed"])
        self.assertIs(s.dismiss(b["finished"]), True)
        self.assertIs(s.dismiss(b["finished"]), True)  # idempotent
        self.assertTrue(self.fresh().get()["dismissed"])

    def test_first_import_keeps_count_not_ids(self):
        d = self.fresh().record(range(1, 2000), ["x"], first=True)
        self.assertEqual((d["new_ids"], d["new_total"], d["first"]), ([], 1999, True))

    def test_ids_capped_newest_kept(self):
        d = self.fresh().record(range(1, 1201), ["x"])
        self.assertEqual((len(d["new_ids"]), d["new_ids"][0], d["new_ids"][-1], d["new_total"]), (500, 701, 1200, 1200))
        self.assertLess(os.path.getsize(self.path), lastimport.MAX_FILE)

    def test_items_capped(self):
        d = self.fresh().record([1], ["n" * 500] * 80)
        self.assertEqual((len(d["items"]), len(d["items"][0])), (50, 120))

    def test_bad_ids_dropped(self):
        d = self.fresh().record([1, 0, -5, 2 ** 53, 7], ["x"])
        self.assertEqual(d["new_ids"], [1, 7])

    def test_hostile_files_ignored(self):
        good = {"finished": 1.5, "source": "upload", "items": ["a"], "new_ids": [1], "new_total": 1, "dismissed": False}
        cases = [b"[]", b'"x"', b"{", b"\xff\xfe\x00junk", b"null", b"1e999",
                 b" " * (lastimport.MAX_FILE + 1),
                 {**good, "new_ids": ['1"><img src=x onerror=alert(1)>']}, {**good, "new_ids": [-1]},
                 {**good, "new_ids": [2 ** 60]}, {**good, "new_ids": [1.5]}, {**good, "new_ids": [True]},
                 {**good, "items": [{"a": 1}]}, {**good, "items": ["x" * 121]}, {**good, "source": "<b>"},
                 {**good, "dismissed": "no"}, {**good, "finished": "now"}, {**good, "new_total": -1},
                 {k: v for k, v in good.items() if k != "finished"}]
        for body in cases:
            with self.subTest(body=str(body)[:60]):
                self.raw(body)
                self.assertIsNone(self.fresh().get())
        self.raw(good)
        self.assertEqual(self.fresh().get()["new_ids"], [1])

    def test_corrupt_file_replaced_cleanly(self):
        self.raw(b"{nope")
        s = self.fresh()
        s.record([4], ["x"])
        with open(self.path, encoding="utf-8") as f:
            self.assertEqual(json.load(f)["new_ids"], [4])

    def test_write_failure_keeps_old_file_and_memory_consistent(self):
        s = self.fresh()
        s.record([1], ["a"])
        with open(self.path, "rb") as f:
            before = f.read()
        with mock.patch.object(lastimport.os, "replace", side_effect=OSError("locked")), \
                mock.patch.object(lastimport.time, "sleep"):
            d = s.record([2], ["b"])  # must not raise
        self.assertEqual(d["new_ids"], [1, 2])
        with open(self.path, "rb") as f:
            self.assertEqual(f.read(), before)
        self.assertEqual(glob.glob(self.path + "*.tmp"), [])

    def test_concurrent_dismiss_and_record(self):
        s = self.fresh()
        cur = s.record([1], ["a"])
        errors = []

        def dismisser():
            try:
                for _ in range(50):
                    g = s.get()
                    s.dismiss(g["finished"])
            except Exception as e:  # pragma: no cover
                errors.append(e)

        def recorder():
            try:
                for i in range(50):
                    s.record([100 + i], ["b"])
            except Exception as e:  # pragma: no cover
                errors.append(e)
        ts = [threading.Thread(target=f) for f in (dismisser, dismisser, recorder)]
        for t in ts:
            t.start()
        for t in ts:
            t.join(30)
        self.assertEqual(errors, [])
        self.assertIsNotNone(cur)
        on_disk = self.fresh().get()
        self.assertEqual(on_disk, s.get())  # the file always parses and matches memory
        self.assertEqual(glob.glob(self.path + "*.tmp"), [])


class Api(unittest.TestCase):
    """Served by web/server.py over a two-session export (ids 1001, 1002, Inline Skate)."""

    @classmethod
    def setUpClass(cls):
        cls.home = tempfile.mkdtemp(prefix="rollbook-test-last-")
        make_export(os.path.join(cls.home, "export"))
        r = run(cls.home, "db", "build", os.path.join(cls.home, "export"))
        assert r.returncode == 0, r.stdout + r.stderr
        cls.db = os.path.join(cls.home, "rollbook.db")
        cls.httpd, cls.M, cls.url = server.start(cls.db, os.path.join(cls.home, "cache", "web_metrics.json"), 0, offline=True)
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()
        cls.port = cls.httpd.server_address[1]

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        shutil.rmtree(cls.home, True)

    def setUp(self):
        server.LAST.data = None

    def get(self, path):
        with urllib.request.urlopen(self.url.rstrip("/") + path, timeout=60) as r:
            return json.loads(r.read())

    def post(self, path, body=b"", headers=None, method="POST"):
        req = urllib.request.Request(self.url.rstrip("/") + path, data=body if method == "POST" else None,
                                     method=method, headers=headers or {})
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()

    def dismiss(self, finished, headers=None):
        h = {"X-Rollbook": "1", "Content-Type": "application/json"} if headers is None else headers
        return self.post("/api/last_import/dismiss", json.dumps({"finished": finished}).encode(), h)

    def edit(self, body):
        code, raw = self.post("/api/edit", json.dumps(body).encode(), {"X-Rollbook": "1", "Content-Type": "application/json"})
        self.assertEqual(code, 200, raw)

    def test_nothing_yet(self):
        self.assertEqual(self.get("/api/last_import"), {})
        self.assertEqual(self.dismiss(1.0)[0], 404)

    def test_lists_visible_sessions_with_sport(self):
        server.LAST.record([1002, 1001, 424242], ["x"])
        li = self.get("/api/last_import")
        self.assertEqual(li["new"], [{"id": 1002, "type": "Inline Skate"}, {"id": 1001, "type": "Inline Skate"}])
        self.assertNotIn("new_ids", li)
        self.edit({"id": "1002", "hidden": True})
        self.edit({"id": "1001", "type": "Run"})
        try:
            self.assertEqual(self.get("/api/last_import")["new"], [{"id": 1001, "type": "Run"}])
        finally:
            self.edit({"id": "1002", "hidden": False})
            self.edit({"id": "1001", "type": ""})
        self.assertEqual(len(self.get("/api/last_import")["new"]), 2)

    def test_get_never_dismisses(self):
        d = server.LAST.record([1001], ["x"])
        self.get("/api/last_import")
        self.get("/api/last_import")
        code, _ = self.post("/api/last_import/dismiss", method="GET")
        self.assertEqual(code, 404)
        self.assertFalse(self.get("/api/last_import")["dismissed"])
        self.assertEqual(self.get("/api/last_import")["finished"], d["finished"])

    def test_dismiss_csrf_and_rebinding(self):
        d = server.LAST.record([1001], ["x"])
        ok, port = {"X-Rollbook": "1", "Content-Type": "application/json"}, self.port
        bad = [{}, {"X-Rollbook": "0"}, {**ok, "Origin": "https://evil.example"}, {**ok, "Origin": "null"},
               {**ok, "Origin": f"http://evil.example:{port}"}, {**ok, "Host": "evil.example"},
               {**ok, "Host": f"evil.example:{port}"}, {**ok, "Host": f"127.0.0.1:{port + 1}"}]
        for h in bad:
            with self.subTest(headers=h):
                self.assertEqual(self.dismiss(d["finished"], h)[0], 403)
        self.assertFalse(server.LAST.get()["dismissed"])
        self.assertEqual(self.dismiss(d["finished"], {**ok, "Origin": f"http://127.0.0.1:{port}"})[0], 200)
        self.assertTrue(self.get("/api/last_import")["dismissed"])

    def test_dismiss_bodies(self):
        d = server.LAST.record([1001], ["x"])
        h = {"X-Rollbook": "1", "Content-Type": "application/json"}
        for body in (b"[1]", b"not json", b'"x"'):
            self.assertEqual(self.post("/api/last_import/dismiss", body, h)[0], 400, body)
        self.assertEqual(self.dismiss(d["finished"] - 1)[0], 409)  # stale page
        self.assertEqual(self.post("/api/last_import/dismiss", b"{}", h)[0], 409)
        big = json.dumps({"finished": d["finished"], "pad": "x" * 10000}).encode()
        self.assertEqual(self.post("/api/last_import/dismiss", big, h)[0], 413)  # body capped at 4 KiB
        self.assertFalse(server.LAST.get()["dismissed"])
        code, raw = self.dismiss(d["finished"])
        self.assertEqual((code, json.loads(raw)), (200, {"dismissed": True}))

    def test_items_not_served(self):
        server.LAST.record([1001], ["export_123456_2026-10-01.zip"])
        self.assertNotIn("items", self.get("/api/last_import"))

    def raw_post(self, length_header, body):
        import http.client
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        try:
            c.putrequest("POST", "/api/last_import/dismiss")
            for k, v in (("X-Rollbook", "1"), ("Content-Type", "application/json"), ("Content-Length", length_header)):
                c.putheader(k, v)
            c.endheaders()
            c.send(body)
            r = c.getresponse()
            r.read()
            code = r.status
            if not r.will_close:  # keep-alive: the next request must still parse
                c.request("GET", "/api/status")
                self.assertEqual(c.getresponse().status, 200)
            return code
        finally:
            c.close()

    def test_body_length_out_of_range(self):
        d = server.LAST.record([1001], ["x"])
        body = json.dumps({"finished": d["finished"], "pad": "x" * 6000}).encode()
        self.assertEqual(self.raw_post(str(len(body)), body), 413)
        self.assertEqual(self.raw_post("-1", b""), 400)
        self.assertEqual(self.raw_post("abc", b""), 400)
        self.assertFalse(server.LAST.get()["dismissed"])
        ok = json.dumps({"finished": d["finished"]}).encode()
        self.assertEqual(self.raw_post(str(len(ok)), ok), 200)

    def test_corrupt_file_at_start_is_empty_not_500(self):
        path = os.path.join(self.home, "last_import.json")
        with open(path, "w") as f:
            f.write('{"finished": 1, "new_ids": ["<img src=x>"]}')
        server.LAST = lastimport.LastImport(path)
        self.assertEqual(self.get("/api/last_import"), {})


class McpPrivacy(unittest.TestCase):
    def test_mcp_never_reads_last_import(self):
        with open(os.path.join(ROOT, "web", "mcp_server.py"), encoding="utf-8") as f:
            src = f.read()
        self.assertNotIn("last_import", src)
        self.assertNotIn("lastimport", src)


class FrontendEscaping(unittest.TestCase):
    """newRecordsPanel puts names from Strava, OSM and file names into innerHTML:
    every one of them must go through esc()."""

    def test_names_are_escaped(self):
        import re
        from test_frontend_power import _src
        m = re.search(r"^async function newRecordsPanel\(.*?^}$", _src(), re.M | re.S)
        self.assertIsNotNone(m, "newRecordsPanel not found in app.js")
        fn = m.group(0)
        for raw in ("r.a.name", "r.type", "r.n", "n", "fdate(when)", "fdate(r.a._d)"):
            self.assertIn("esc(%s)" % raw, fn, raw)
            self.assertNotIn("${%s}" % raw, fn, raw)
        self.assertNotIn("${r.aid}", fn)  # navigation goes through data-i + okAid, not raw ids in markup
        self.assertNotIn("${r.sgid}", fn)


if __name__ == "__main__":
    unittest.main()
