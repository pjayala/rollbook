# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Pedro Ayala. Rollbook is free software, see LICENSE.
"""Minimal QR code encoder (ISO/IEC 18004), stdlib only.

Just enough to put the `rollbook web --lan` URL on the terminal for a phone
camera: byte mode, error-correction level M, versions 1-10 (up to 213 bytes).
Structure follows the standard directly; the Reed-Solomon / placement code is
the textbook algorithm (as in Project Nayuki's reference implementation).

    print(qr.render(qr.encode("https://192.168.1.20:8766/?t=...")))
"""

import sys

# level M, per version: (EC codewords per block, [(blocks, data codewords per block), ...])
BLOCKS_M = {
    1: (10, [(1, 16)]), 2: (16, [(1, 28)]), 3: (26, [(1, 44)]), 4: (18, [(2, 32)]),
    5: (24, [(2, 43)]), 6: (16, [(4, 27)]), 7: (18, [(4, 31)]), 8: (22, [(2, 38), (2, 39)]),
    9: (22, [(3, 36), (2, 37)]), 10: (26, [(4, 43), (1, 44)]),
}
ALIGN = {1: [], 2: [6, 18], 3: [6, 22], 4: [6, 26], 5: [6, 30], 6: [6, 34],
         7: [6, 22, 38], 8: [6, 24, 42], 9: [6, 26, 46], 10: [6, 28, 50]}
ECL_M = 0  # format-info bits for level M (L=1, M=0, Q=3, H=2)


def capacity(version):
    """Data bytes that fit in byte mode at level M."""
    _, groups = BLOCKS_M[version]
    data_cw = sum(n * k for n, k in groups)
    count_bits = 8 if version <= 9 else 16
    return (data_cw * 8 - 4 - count_bits) // 8


# ---------------------------------------------------------------- GF(256) / RS


def gf_mul(x, y):
    """Multiply in GF(2^8) modulo x^8 + x^4 + x^3 + x^2 + 1 (0x11D)."""
    z = 0
    for i in reversed(range(8)):
        z = (z << 1) ^ ((z >> 7) * 0x11D)
        z ^= ((y >> i) & 1) * x
    return z


def rs_divisor(degree):
    """Generator polynomial coefficients (highest power first, leading 1 dropped)."""
    result = [0] * (degree - 1) + [1]
    root = 1
    for _ in range(degree):
        for j in range(degree):
            result[j] = gf_mul(result[j], root)
            if j + 1 < degree:
                result[j] ^= result[j + 1]
        root = gf_mul(root, 2)
    return result


def rs_remainder(data, divisor):
    result = [0] * len(divisor)
    for b in data:
        factor = b ^ result.pop(0)
        result.append(0)
        for i, coef in enumerate(divisor):
            result[i] ^= gf_mul(coef, factor)
    return result


# ---------------------------------------------------------------- encoding


def data_codewords(payload, version):
    """Byte-mode bit stream, terminated and padded to the version's data capacity."""
    _, groups = BLOCKS_M[version]
    total = sum(n * k for n, k in groups)
    bits = []

    def put(val, n):
        bits.extend((val >> i) & 1 for i in reversed(range(n)))

    put(0b0100, 4)
    put(len(payload), 8 if version <= 9 else 16)
    for b in payload:
        put(b, 8)
    put(0, min(4, total * 8 - len(bits)))  # terminator
    put(0, -len(bits) % 8)
    out = [int("".join(map(str, bits[i:i + 8])), 2) for i in range(0, len(bits), 8)]
    pad = 0xEC
    while len(out) < total:
        out.append(pad)
        pad ^= 0xEC ^ 0x11
    return out


def interleave(data, version):
    ec_len, groups = BLOCKS_M[version]
    div = rs_divisor(ec_len)
    blocks, i = [], 0
    for n, k in groups:
        for _ in range(n):
            blocks.append(data[i:i + k])
            i += k
    ecs = [rs_remainder(b, div) for b in blocks]
    out = []
    for j in range(max(len(b) for b in blocks)):
        out += [b[j] for b in blocks if j < len(b)]
    for j in range(ec_len):
        out += [e[j] for e in ecs]
    return out


class Matrix:
    def __init__(self, version):
        self.version = version
        self.size = n = version * 4 + 17
        self.dark = [[False] * n for _ in range(n)]
        self.func = [[False] * n for _ in range(n)]

    def set(self, x, y, dark):
        self.dark[y][x] = bool(dark)
        self.func[y][x] = True

    def function_patterns(self):
        n = self.size
        for i in range(n):  # timing
            self.set(6, i, i % 2 == 0)
            self.set(i, 6, i % 2 == 0)
        for cx, cy in ((3, 3), (n - 4, 3), (3, n - 4)):  # finders + separators
            for dy in range(-4, 5):
                for dx in range(-4, 5):
                    x, y = cx + dx, cy + dy
                    if 0 <= x < n and 0 <= y < n:
                        self.set(x, y, max(abs(dx), abs(dy)) not in (2, 4))
        pos = ALIGN[self.version]
        last = len(pos) - 1
        for i, ax in enumerate(pos):
            for j, ay in enumerate(pos):
                if (i, j) in ((0, 0), (0, last), (last, 0)):
                    continue  # overlaps a finder
                for dy in range(-2, 3):
                    for dx in range(-2, 3):
                        self.set(ax + dx, ay + dy, max(abs(dx), abs(dy)) != 1)
        self.format_bits(0)  # reserve; real mask written later
        self.version_bits()

    def format_bits(self, mask):
        n = self.size
        data = ECL_M << 3 | mask
        rem = data
        for _ in range(10):
            rem = (rem << 1) ^ ((rem >> 9) * 0x537)
        bits = (data << 10 | rem) ^ 0x5412
        b = lambda i: (bits >> i) & 1
        for i in range(6):
            self.set(8, i, b(i))
        self.set(8, 7, b(6))
        self.set(8, 8, b(7))
        self.set(7, 8, b(8))
        for i in range(9, 15):
            self.set(14 - i, 8, b(i))
        for i in range(8):
            self.set(n - 1 - i, 8, b(i))
        for i in range(8, 15):
            self.set(8, n - 15 + i, b(i))
        self.set(8, n - 8, True)  # the "dark module"

    def version_bits(self):
        if self.version < 7:
            return
        rem = self.version
        for _ in range(12):
            rem = (rem << 1) ^ ((rem >> 11) * 0x1F25)
        bits = self.version << 12 | rem
        for i in range(18):
            a, c = self.size - 11 + i % 3, i // 3
            self.set(a, c, (bits >> i) & 1)
            self.set(c, a, (bits >> i) & 1)

    def place(self, codewords):
        n, i, total = self.size, 0, len(codewords) * 8
        right = n - 1
        while right >= 1:
            if right == 6:
                right = 5  # skip the vertical timing column
            upward = ((right + 1) & 2) == 0
            for vert in range(n):
                y = n - 1 - vert if upward else vert
                for x in (right, right - 1):
                    if not self.func[y][x] and i < total:
                        self.dark[y][x] = bool((codewords[i >> 3] >> (7 - (i & 7))) & 1)
                        i += 1
            right -= 2

    def apply_mask(self, mask):
        f = MASKS[mask]
        for y in range(self.size):
            for x in range(self.size):
                if not self.func[y][x] and f(x, y):
                    self.dark[y][x] = not self.dark[y][x]

    def penalty(self):
        n, d, score = self.size, self.dark, 0
        lines = d + [[d[y][x] for y in range(n)] for x in range(n)]
        for line in lines:  # rule 1: runs of 5+
            run, prev = 0, None
            for v in line:
                run = run + 1 if v == prev else 1
                prev = v
                if run == 5:
                    score += 3
                elif run > 5:
                    score += 1
        for y in range(n - 1):  # rule 2: 2x2 blocks
            for x in range(n - 1):
                if d[y][x] == d[y][x + 1] == d[y + 1][x] == d[y + 1][x + 1]:
                    score += 3
        pat = (True, False, True, True, True, False, True)
        light4 = (False,) * 4
        for line in lines:  # rule 3: finder-like 1011101 with 4 light on a side
            t = tuple(line)
            padded = light4 + t + light4
            for i in range(len(padded) - 10):
                w = padded[i:i + 11]
                if w == pat + light4 or w == light4 + pat:
                    score += 40
        dark = sum(map(sum, d))  # rule 4: balance
        total = n * n
        score += ((abs(dark * 20 - total * 10) + total - 1) // total - 1) * 10
        return score


MASKS = (
    lambda x, y: (x + y) % 2 == 0,
    lambda x, y: y % 2 == 0,
    lambda x, y: x % 3 == 0,
    lambda x, y: (x + y) % 3 == 0,
    lambda x, y: (x // 3 + y // 2) % 2 == 0,
    lambda x, y: x * y % 2 + x * y % 3 == 0,
    lambda x, y: (x * y % 2 + x * y % 3) % 2 == 0,
    lambda x, y: ((x + y) % 2 + x * y % 3) % 2 == 0,
)


def encode(text, mask=None):
    """Return the QR symbol as a list of rows of booleans (True = dark)."""
    payload = text.encode("utf-8") if isinstance(text, str) else bytes(text)
    version = next((v for v in BLOCKS_M if capacity(v) >= len(payload)), None)
    if version is None:
        raise ValueError(f"too long for a version 1-10 QR code: {len(payload)} bytes (max {capacity(10)})")
    cw = interleave(data_codewords(payload, version), version)
    best = None
    for m in range(8) if mask is None else (mask,):
        q = Matrix(version)
        q.function_patterns()
        q.place(cw)
        q.apply_mask(m)
        q.format_bits(m)
        p = q.penalty()
        if best is None or p < best[0]:
            best = (p, q)
    assert best is not None
    return best[1].dark


def render(rows, quiet=4, ansi=True):
    """Terminal art, two module rows per text line.

    ansi=True draws black modules on an explicit white background, so it scans
    the same on dark and light terminal themes. ansi=False is the fallback for
    consoles without Unicode blocks: '##' for dark, light background assumed
    (phone scanners usually also read the inverted symbol on a dark theme).
    """
    n = len(rows)
    size = n + 2 * quiet
    grid = [[False] * size for _ in range(size)]
    for y in range(n):
        for x in range(n):
            grid[y + quiet][x + quiet] = rows[y][x]
    if not ansi:
        return "\n".join("".join("##" if v else "  " for v in r) for r in grid)
    if size % 2:
        grid.append([False] * size)
    chars = {(False, False): " ", (True, False): "\u2580", (False, True): "\u2584", (True, True): "\u2588"}
    out = []
    for y in range(0, len(grid), 2):
        line = "".join(chars[(grid[y][x], grid[y + 1][x])] for x in range(size))
        out.append("\x1b[30;47m" + line + "\x1b[0m")
    return "\n".join(out)


def can_render_blocks(stream=None):
    stream = stream or sys.stdout
    try:
        "\u2580\u2584\u2588".encode(getattr(stream, "encoding", None) or "ascii")
        return True
    except (UnicodeEncodeError, LookupError):
        return False
