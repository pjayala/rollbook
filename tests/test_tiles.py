# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Pedro Ayala. Rollbook is free software, see LICENSE.
"""Tile proxy politeness: one upstream fetch per tile, at most TileCache.UPSTREAM at once."""

import threading
import time
import unittest
from unittest import mock

import test_end_to_end  # noqa: F401  (puts web/ on sys.path)
import server
from helpers import tmpdir


class _Resp:
    status = 200
    headers = {"Content-Type": "image/png"}

    def __init__(self, url):
        self.url = url

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return b"\x89PNG" + self.url.encode()


class Tiles(unittest.TestCase):
    def setUp(self):
        self.cache = server.TileCache(tmpdir(self), offline=False)
        self.calls, self.live, self.peak, self.lock = [], 0, 0, threading.Lock()

    def fake(self, req, timeout=None):
        with self.lock:
            self.calls.append(req.full_url); self.live += 1; self.peak = max(self.peak, self.live)
        time.sleep(0.05)
        with self.lock:
            self.live -= 1
        return _Resp(req.full_url)

    def burst(self, coords):
        out = {}
        with mock.patch.object(server.urllib.request, "urlopen", self.fake):
            ts = [threading.Thread(target=lambda c=c, i=i: out.__setitem__(i, self.cache.get("osm", *c))) for i, c in enumerate(coords)]
            for t in ts:
                t.start()
            for t in ts:
                t.join()
        return out

    def test_same_tile_is_fetched_once(self):
        out = self.burst([(5, 1, 2)] * 8)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(len(set(out.values())), 1)
        self.assertTrue(all(out.values()))

    def test_parallel_fetches_are_capped(self):
        self.burst([(6, x, 3) for x in range(10)])
        self.assertEqual(len(self.calls), 10)
        self.assertLessEqual(self.peak, server.TileCache.UPSTREAM)

    def test_user_agent_names_a_contact(self):
        self.assertIn("https://", server.TILE_UA)


if __name__ == "__main__":
    unittest.main()
