# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Pedro Ayala. Rollbook is free software, see LICENSE.
"""Shared helpers: load bin/rollbook as a module, synthetic activity files."""

import gzip
import math
import os
import shutil
import struct
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from importlib.machinery import SourceFileLoader
from importlib.util import module_from_spec, spec_from_loader

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
SAMPLES = os.path.join(ROOT, "samples")
TOOL = os.path.join(ROOT, "bin", "rollbook")
WEB = os.path.join(ROOT, "web")

# never touch the developer's real data
_HOME = tempfile.mkdtemp(prefix="rollbook-test-home-")
os.environ["ROLLBOOK_HOME"] = _HOME
if WEB not in sys.path:
    sys.path.insert(0, WEB)


def load_tool():
    """bin/rollbook has no .py suffix, so import it by path (once)."""
    if "rollbook_cli" in sys.modules:
        return sys.modules["rollbook_cli"]
    loader = SourceFileLoader("rollbook_cli", TOOL)
    mod = module_from_spec(spec_from_loader("rollbook_cli", loader))
    loader.exec_module(mod)
    sys.modules["rollbook_cli"] = mod
    return mod


def tmpdir(test):
    d = tempfile.mkdtemp(prefix="rollbook-test-")
    test.addCleanup(shutil.rmtree, d, True)
    return d


def gz_copy(src, dst):
    with open(src, "rb") as f, gzip.open(dst, "wb") as g:
        g.write(f.read())


# ---------------------------------------------------------------- FIT writer

FIT_EPOCH = datetime(1989, 12, 31, tzinfo=timezone.utc)
SEMI = 2 ** 31 / 180.0


def write_fit(path, points, big_endian=False, session_sport=None, sport_msg=None):
    """Minimal FIT file: optional sport (12) message, records (20), session (18).

    points: [(datetime, lat|None, lon|None, ele|None, hr|None)]. No CRC (the
    reader doesn't check it). Endianness is per definition message, like real
    devices.
    """
    e = ">" if big_endian else "<"
    arch = 1 if big_endian else 0
    body = b""

    def define(local, gmsg, fields):
        return (bytes([0x40 | local, 0, arch]) + struct.pack(e + "H", gmsg)
                + bytes([len(fields)]) + b"".join(bytes(f) for f in fields))

    if sport_msg is not None:
        body += define(2, 12, [(0, 1, 0x00)]) + bytes([2, sport_msg])
    body += define(0, 20, [(253, 4, 0x86), (0, 4, 0x85), (1, 4, 0x85), (2, 2, 0x84), (3, 1, 0x02)])
    for ts, lat, lon, ele, hr in points:
        body += bytes([0]) + struct.pack(
            e + "IiiHB",
            int((ts - FIT_EPOCH).total_seconds()),
            0x7FFFFFFF if lat is None else round(lat * SEMI),
            0x7FFFFFFF if lon is None else round(lon * SEMI),
            0xFFFF if ele is None else round((ele + 500) * 5),
            0xFF if hr is None else hr)
    if session_sport is not None:
        body += define(1, 18, [(5, 1, 0x00)]) + bytes([1, session_sport])
    header = bytes([14, 0x10]) + struct.pack("<H", 2100) + struct.pack("<I", len(body)) + b".FIT" + b"\0\0"
    with open(path, "wb") as f:
        f.write(header + body)


def line_points(n=600, start=(10.0, 20.0), speed_ms=6.0, heading_deg=90, t0=None, hr=140, ele=50.0):
    """n one-second samples along a straight line."""
    t0 = t0 or datetime(2026, 1, 1, 8, 0, tzinfo=timezone.utc)
    lat0, lon0 = start
    out = []
    for i in range(n):
        d = speed_ms * i
        dy = d * math.cos(math.radians(heading_deg))
        dx = d * math.sin(math.radians(heading_deg))
        out.append((t0 + timedelta(seconds=i), lat0 + dy / 110540,
                    lon0 + dx / (111320 * math.cos(math.radians(lat0))), ele, hr))
    return out


def loop_rows(laps=5, radius_m=60.0, speed_ms=6.0, centre=(10.0, 20.0), lead_m=0):
    """Server-style sample rows (t, lat, lon, ele, hr, cad, dist_m) going round a circle."""
    circ = 2 * math.pi * radius_m
    n = int((laps * circ + lead_m) / speed_ms)
    lat0, lon0 = centre
    rows = []
    for i in range(n + 1):
        d = i * speed_ms
        a = max(0.0, d - lead_m) / radius_m
        x, y = radius_m * math.sin(a), radius_m * (1 - math.cos(a)) - (lead_m - d if d < lead_m else 0)
        rows.append((i, lat0 + y / 110540, lon0 + x / (111320 * math.cos(math.radians(lat0))),
                     50.0, 140, None, d))
    return rows
