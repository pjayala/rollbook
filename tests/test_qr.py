# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Pedro Ayala. Rollbook is free software, see LICENSE.
"""web/qr.py: the built-in QR encoder behind `rollbook web --lan`."""

import hashlib
import io
import unittest

import helpers  # noqa: F401  (puts web/ on sys.path)
import qr

# Symbols at full capacity (no terminator / padding choices left to the
# encoder), every mask, checked module for module against segno 1.6.6 and
# decoded with OpenCV when they were recorded.
GOLDEN = {
    (1, "https://rb.lan"): [
        "2311c534b68e0864", "a3adbeeda4fe45f9", "ae3f983d6a0287c7", "096098c8a571b398",
        "b8061b4abeb7ce69", "f2ba6e44c75c4594", "8d1b5a67a36429ec", "782d82e6235b4ecf"],
    (4, "https://192.168.100.200:8766/?t=AbCdEfGhIjKlMnOpQrStUv_-xyz123"): [
        "ebdbdf815beb8485", "6139dd78210c423c", "ed56c176da0fa4ef", "4fd60cf02953cc45",
        "5347a97177016a90", "698e7c0fd95bb4bb", "936286ba68d9fbc3", "ca5f5d9828c86a3c"],
    (7, "R" * 122): [
        "0ab6b6324457feab", "169c438c55030f66", "5ff4b4c6b5d5ef39", "ea9895cc1477f8e9",
        "e2a409e7b790225a", "c91d7af32f479585", "2dbbc3c03bc37167", "e8d61a9663abda46"],
    (10, "Z" * 213): [
        "7b672e99e21d97ee", "2ba449184da18cc9", "0e5ebb2fb0ca7326", "076363cf05e12366",
        "bf408924963cb3ff", "7d62eb0800385ce0", "f679e5126544f074", "51f4a5c096b77d14"],
}


def digest(rows):
    return hashlib.sha256("".join("1" if c else "0" for r in rows for c in r).encode()).hexdigest()[:16]


class QR(unittest.TestCase):
    def test_reed_solomon_reference_vectors(self):
        # ISO/IEC 18004 Annex I: "01234567", 1-M
        data = [0x10, 0x20, 0x0C, 0x56, 0x61, 0x80] + [0xEC, 0x11] * 5
        self.assertEqual(qr.rs_remainder(data, qr.rs_divisor(10)),
                         [0xA5, 0x24, 0xD4, 0xC1, 0xED, 0x36, 0xC7, 0x87, 0x2C, 0x55])
        # "HELLO WORLD", 1-M (alphanumeric data codewords)
        data = [32, 91, 11, 120, 209, 114, 220, 77, 67, 64, 236, 17, 236, 17, 236, 17]
        self.assertEqual(qr.rs_remainder(data, qr.rs_divisor(10)),
                         [196, 35, 39, 119, 235, 215, 231, 226, 93, 23])

    def test_byte_capacity_level_m(self):
        self.assertEqual([qr.capacity(v) for v in range(1, 11)],
                         [14, 26, 42, 62, 84, 106, 122, 152, 180, 213])
        for v, (ec, groups) in qr.BLOCKS_M.items():
            total = sum(n * (k + ec) for n, k in groups)
            size = 4 * v + 17
            self.assertLessEqual(total * 8, size * size)  # sanity: fits the symbol

    def test_golden_symbols(self):
        for (v, text), want in GOLDEN.items():
            self.assertEqual(len(text), qr.capacity(v))
            for m in range(8):
                rows = qr.encode(text, mask=m)
                self.assertEqual(len(rows), 4 * v + 17)
                self.assertEqual(digest(rows), want[m], f"version {v} mask {m}")

    def test_format_and_version_bits(self):
        # format info for level M, mask 0..7, read back from the top-left copy
        want = ["101010000010010", "101000100100101", "101111001111100", "101101101001011",
                "100010111111001", "100000011001110", "100111110010111", "100101010100000"]
        for m in range(8):
            rows = qr.encode("x", mask=m)
            cells = [(8, i) for i in range(6)] + [(8, 7), (8, 8), (7, 8)] + [(14 - i, 8) for i in range(9, 15)]
            bits = "".join("1" if rows[y][x] else "0" for x, y in cells)
            self.assertEqual(bits[::-1], want[m])
        # version 7 information block, 000111110010010100
        rows = qr.encode("R" * 122, mask=0)
        n = len(rows)
        bits = sum(rows[i // 3][n - 11 + i % 3] << i for i in range(18))
        self.assertEqual(bits, 0b000111110010010100)

    def test_function_patterns(self):
        rows = qr.encode("https://rb.lan")
        n = len(rows)
        finder = [[max(abs(dx), abs(dy)) != 2 for dx in range(-3, 4)] for dy in range(-3, 4)]
        for cx, cy in ((3, 3), (n - 4, 3), (3, n - 4)):
            self.assertEqual([[rows[cy + dy][cx + dx] for dx in range(-3, 4)] for dy in range(-3, 4)], finder)
        self.assertEqual([rows[6][x] for x in range(8, n - 8)], [x % 2 == 0 for x in range(8, n - 8)])
        self.assertTrue(rows[n - 8][8])  # dark module

    def test_picks_smallest_version_and_rejects_too_long(self):
        self.assertEqual(len(qr.encode("a" * 14)), 21)
        self.assertEqual(len(qr.encode("a" * 15)), 25)
        url = "https://192.168.178.123:8766/?t=" + "x" * 22  # a typical --lan URL
        self.assertEqual(len(qr.encode(url)), 33)  # version 4
        with self.assertRaises(ValueError):
            qr.encode("a" * 214)

    def test_render(self):
        rows = qr.encode("https://rb.lan")
        art = qr.render(rows).split("\n")
        self.assertEqual(len(art), (21 + 8 + 1) // 2)
        self.assertTrue(all(line.startswith("\x1b[30;47m") and line.endswith("\x1b[0m") for line in art))
        plain = qr.render(rows, quiet=2, ansi=False).split("\n")
        self.assertEqual((len(plain), len(plain[0])), (25, 50))
        self.assertTrue(qr.can_render_blocks(io.TextIOWrapper(io.BytesIO(), encoding="utf-8")))
        self.assertFalse(qr.can_render_blocks(io.TextIOWrapper(io.BytesIO(), encoding="cp1252")))


if __name__ == "__main__":
    unittest.main()
