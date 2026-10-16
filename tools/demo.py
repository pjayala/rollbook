#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Pedro Ayala. Rollbook is free software, see LICENSE.
"""Build a synthetic demo athlete for screenshots: nobody's real data.

    python3 tools/demo.py /tmp/rollbook-demo      # export + DB + seeded caches
    ROLLBOOK_HOME=/tmp/rollbook-demo bin/rollbook web --offline

Writes a Strava-style export (activities.csv + FIT files) into <home>/export,
builds and enriches <home>/rollbook.db, and seeds the weather and place-name
caches so conditions and segment names work with --offline (no network at
all, so no basemap either). Deterministic: same seed, same data.

Sessions, March 2025 to October 2026, with steady progress:
  Inline Skate  out-and-back on a harbour line (wind!) and a 1.4 km park loop
  Run           a 5 km loop
  Ride          a longer out-and-back
  Virtual Ride  indoor, ROUVY-style power / cadence and FIT power summary
"""

import csv
import json
import math
import os
import random
import struct
import subprocess
import sys
from datetime import datetime, timedelta, timezone

ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
TOOL = os.path.join(ROOT, "bin", "rollbook")

ORIGIN = (52.0, 5.0)  # a neutral reference point, not anyone's home
TZ = timezone(timedelta(hours=2))
FIT_EPOCH = datetime(1989, 12, 31, tzinfo=timezone.utc)
SEMI = 2 ** 31 / 180.0
K_WIND = 0.0046  # speed fraction per km/h of tailwind (what server.py fits on real data)

# ------------------------------------------------------------------ geometry (metres, x east / y north)

HARBOUR = [(0, 0), (900, 350), (2100, 420), (3300, 1100), (4600, 1350), (6000, 1300),
           (7400, 1900), (8600, 2600), (10200, 2900), (12000, 3600), (14000, 3900)]


def loop(cx, cy, w, h, n=200):
    """Rounded rectangle, counter-clockwise from the bottom middle."""
    pts = []
    for i in range(n):
        a = 2 * math.pi * i / n
        x, y = math.sin(a), -math.cos(a)
        k = 6  # superellipse: rectangle-ish with round corners
        sx = math.copysign(abs(x) ** (2 / k), x)
        sy = math.copysign(abs(y) ** (2 / k), y)
        pts.append((cx + sx * w / 2, cy + sy * h / 2))
    return pts + [pts[0]]


INLAND = [(2100, 420), (2300, 900), (3400, 1500), (4700, 1750), (6100, 1800), (7300, 2300), (8600, 2600)]
_LAKE = loop(6600, -300, 900, 700, n=80)[:-1]
_LAKE = _LAKE[40:] + _LAKE[:40] + [_LAKE[40]]  # start at the top middle, nearest the harbour
LAKE = [(6000, 1300), (6300, 700)] + _LAKE + [(6300, 700), (6000, 1300)]


def harbour_to(x_end):
    return [p for p in HARBOUR if p[0] <= x_end]


def harbour_from(x_start):
    return [p for p in HARBOUR if p[0] >= x_start]


# alternatives to the plain out-and-back: (name, polyline walked once)
SKATE_ROUTES = [
    ("Harbour and inland", harbour_to(2100) + INLAND[1:] + list(reversed(harbour_to(8600)))[1:]),
    ("Lake loop", harbour_to(6000) + LAKE[1:] + list(reversed(harbour_to(6000)))[1:]),
    ("Inland out and back", harbour_to(2100) + INLAND[1:] + list(reversed(INLAND))[1:] + list(reversed(harbour_to(2100)))[1:]),
]

PARK = loop(-1400, 1500, 520, 170)          # ~1.4 km
RUN = loop(-600, -900, 1600, 900)          # ~5 km
TO_PARK = [(0, 0), (-500, 500), (-1000, 1100), (-1400, 1415)]
TO_RUN = [(0, 0), (-300, -300), (-600, -450)]


def cum(pl):
    out, d = [0.0], 0.0
    for a, b in zip(pl, pl[1:]):
        d += math.hypot(b[0] - a[0], b[1] - a[1])
        out.append(d)
    return out


def at(pl, cl, d):
    """Point and heading (deg, 0 = north) at distance d along polyline pl."""
    d = max(0.0, min(d, cl[-1]))
    lo, hi = 0, len(cl) - 1
    while hi - lo > 1:
        m = (lo + hi) // 2
        if cl[m] <= d:
            lo = m
        else:
            hi = m
    a, b = pl[lo], pl[hi]
    f = (d - cl[lo]) / ((cl[hi] - cl[lo]) or 1)
    hd = math.degrees(math.atan2(b[0] - a[0], b[1] - a[1])) % 360
    return (a[0] + f * (b[0] - a[0]), a[1] + f * (b[1] - a[1])), hd


def to_ll(x, y):
    return (ORIGIN[0] + y / 110540, ORIGIN[1] + x / (111320 * math.cos(math.radians(ORIGIN[0]))))


def elevation(x, y):
    return 12 + 7 * math.sin(x / 2100) + 4 * math.cos(y / 1300) + 0.0012 * x  # gentle, slightly uphill east


# ------------------------------------------------------------------ FIT writer

def write_fit(path, recs, session=None):
    """recs: [(utc datetime, lat, lon, ele, hr, dist_m, watts, cad)]; lat None = indoor."""
    power = any(r[6] is not None for r in recs)
    gps = any(r[1] is not None for r in recs)
    e = "<"
    fields = [(253, 4, 0x86), (2, 2, 0x84), (3, 1, 0x02), (5, 4, 0x86)]
    if gps:
        fields[1:1] = [(0, 4, 0x85), (1, 4, 0x85)]
    if power:
        fields += [(7, 2, 0x84), (4, 1, 0x02)]
    body = bytes([0x40, 0, 0]) + struct.pack(e + "H", 20) + bytes([len(fields)]) + b"".join(bytes(f) for f in fields)
    for ts, lat, lon, ele, hr, dist, w, cad in recs:
        body += bytes([0]) + struct.pack(e + "I", int((ts - FIT_EPOCH).total_seconds()))
        if gps:
            body += struct.pack(e + "ii", round(lat * SEMI), round(lon * SEMI))
        body += struct.pack(e + "HBI", round((ele + 500) * 5), hr, round(dist * 100))
        if power:
            body += struct.pack(e + "HB", w, cad)
    if session:
        sf = sorted(session.items())
        body += (bytes([0x41, 0, 0]) + struct.pack(e + "H", 18) + bytes([len(sf)])
                 + b"".join(bytes((k, 2, 0x84)) for k, _ in sf))
        body += bytes([1]) + b"".join(struct.pack(e + "H", v) for _, v in sf)
    header = bytes([14, 0x10]) + struct.pack("<H", 2100) + struct.pack("<I", len(body)) + b".FIT" + b"\0\0"
    with open(path, "wb") as f:
        f.write(header + body)


# ------------------------------------------------------------------ sessions

class HR:
    """Heart rate following effort with a lag, plus drift."""

    def __init__(self, rnd, rest=58, top=188):
        self.v, self.rnd, self.rest, self.top = 95.0, rnd, rest, top

    def step(self, effort, t):
        target = self.rest + (self.top - self.rest) * min(0.95, 0.42 + 0.36 * effort) + min(8, t / 450)
        self.v += (target - self.v) / 25
        return max(60, min(200, round(self.v + self.rnd.gauss(0, 1.2))))


def gps_track(rnd, path, legs, base_ms, t0, wind, stops=(), lap_stop=None):
    """legs: [(polyline, start_m, end_m)], walked in order at base_ms with wind and stops."""
    ws, wd = wind
    recs, t, dist, hr = [], 0, 0.0, HR(rnd)
    stop_at = sorted(stops)
    gust = 0.0
    ox, oy = rnd.gauss(0, 3), rnd.gauss(0, 3)
    for pl, a, b in legs:
        cl = cum(pl)
        sgn = 1 if b >= a else -1
        d = a
        while (d - b) * sgn < 0:
            (x, y), hd = at(pl, cl, d)
            if sgn < 0:
                hd = (hd + 180) % 360
            tail = ws * math.cos(math.radians(hd - (wd + 180)))  # wind *from* wd
            gust += (rnd.gauss(0, 0.02) - gust * 0.05)
            v = base_ms * (1 + K_WIND * tail) * (1 + gust) * (0.85 if t < 120 else 1.0)
            if stop_at and dist >= stop_at[0][0]:
                for _ in range(stop_at[0][1]):  # standing still
                    la, lo = to_ll(x + ox + rnd.gauss(0, 1), y + oy + rnd.gauss(0, 1))
                    recs.append((t0 + timedelta(seconds=t), la, lo, elevation(x, y), hr.step(0.2, t), dist, None, None))
                    t += 1
                stop_at.pop(0)
            la, lo = to_ll(x + ox + rnd.gauss(0, 1.5), y + oy + rnd.gauss(0, 1.5))
            recs.append((t0 + timedelta(seconds=t), la, lo, elevation(x, y), hr.step(v / base_ms, t), dist, None, None))
            d += sgn * v
            dist += v
            t += 1
    return recs


def virtual(rnd, t0, minutes, ftp):
    """Indoor ride: warm-up, intervals, cool-down; returns records and the FIT session summary."""
    recs, dist, hr, ws = [], 0.0, HR(rnd, top=184), []
    n = minutes * 60
    reps = rnd.choice([(4, 480, 1.0), (5, 300, 1.08), (3, 720, 0.95), (8, 120, 1.18)])
    plan = [(600, 0.6)]
    for _ in range(reps[0]):
        plan += [(reps[1], reps[2]), (240, 0.55)]
    t = 0
    for dur, f in plan + [(max(300, n - sum(p[0] for p in plan) - 300), 0.72), (300, 0.5)]:
        for _ in range(dur):
            w = max(0, round(ftp * f * (1 + rnd.gauss(0, 0.06))))
            if rnd.random() < 0.004:
                w = 0  # coasting
            v = 7.9 * (w / 160) ** (1 / 3) if w else 6
            dist += v
            cad = 0 if w == 0 else max(60, min(115, round(88 + 10 * (f - 0.7) + rnd.gauss(0, 3))))
            recs.append((t0 + timedelta(seconds=t), None, None, 30 + 20 * math.sin(t / 900),
                         hr.step(f * 0.95, t), dist, w, cad))
            ws.append(w)
            t += 1
    avg = sum(ws) / len(ws)
    roll = [sum(ws[i - 30:i]) / 30 for i in range(30, len(ws))]
    np_ = (sum(r ** 4 for r in roll) / len(roll)) ** 0.25
    if_ = np_ / ftp
    tss = len(ws) / 3600 * if_ ** 2 * 100
    session = {20: round(avg), 21: max(ws), 34: round(np_), 36: round(if_ * 1000), 35: round(tss * 10)}
    return recs, session


# ------------------------------------------------------------------ the athlete's calendar

def plan(rnd):
    start, end = datetime(2025, 3, 3, tzinfo=TZ), datetime(2026, 10, 12, tzinfo=TZ)
    day, out = start, []
    while day < end:
        prog = (day - start) / (end - start)  # 0 -> 1: fitness gains
        summer = 0.5 - 0.5 * math.cos(2 * math.pi * (day.timetuple().tm_yday - 30) / 365)
        for _ in range(1):
            r = rnd.random()
            if day.weekday() in (1, 3, 5) and r < 0.35 + 0.5 * summer:
                out.append(("skate", day.replace(hour=rnd.choice([8, 9, 18, 19]), minute=rnd.randrange(60)), prog))
            elif day.weekday() in (0, 4) and r < 0.45:
                out.append(("run", day.replace(hour=7, minute=rnd.randrange(60)), prog))
            elif day.weekday() == 6 and r < 0.25 + 0.5 * summer:
                out.append(("ride", day.replace(hour=10, minute=rnd.randrange(60)), prog))
            elif day.weekday() in (2, 6) and r < 0.55 - 0.45 * summer:
                out.append(("virtual", day.replace(hour=19, minute=rnd.randrange(60)), prog))
        day += timedelta(days=1)
    return out


VIRTUAL_ROUTES = ["ROUVY - Lago di Garda", "ROUVY - Stelvio warm-up", "MyWhoosh - Tempo blocks",
                  "ROUVY - Alpe d'Huez", "MyWhoosh - Sweet spot 3x12", "ROUVY - Mallorca coast"]


def wind_for(rnd):
    return (max(2.0, rnd.gauss(14, 6)), rnd.choice([200, 230, 250, 270, 290, 310]) + rnd.gauss(0, 15))


def main(home):
    rnd = random.Random(20261016)
    exp = os.path.join(home, "export")
    os.makedirs(os.path.join(exp, "activities"), exist_ok=True)
    rows, weather = [], {}
    harb = cum(HARBOUR)
    for i, (kind, t0, prog) in enumerate(plan(rnd)):
        aid = 900000 + i
        utc = t0.astimezone(timezone.utc)
        wind = wind_for(rnd)
        session, desc = None, "HUAWEI WATCH GT 6 Pro"
        r = rnd.random()
        if kind == "skate" and r < 0.22:
            name, pl = rnd.choice(SKATE_ROUTES)
            recs = gps_track(rnd, pl, [(pl, 0, cum(pl)[-1])], (18.5 + 4.0 * prog + rnd.gauss(0, 0.6)) / 3.6,
                             utc, wind, [(rnd.uniform(800, 9000), rnd.randint(15, 60))])
            typ = "Inline Skate"
        elif kind == "skate" and r < 0.72:
            turn = rnd.uniform(6500, harb[-1] - 600)
            stops = [(rnd.uniform(800, 2 * turn - 800), rnd.randint(15, 70)) for _ in range(rnd.randint(0, 3))]
            recs = gps_track(rnd, HARBOUR, [(HARBOUR, 0, turn), (HARBOUR, turn, 0)],
                             (18.5 + 4.0 * prog + rnd.gauss(0, 0.6)) / 3.6, utc, wind, stops)
            name, typ = rnd.choice(["Harbour line", "Morning skate", "Evening skate", "Out and back"]), "Inline Skate"
        elif kind == "skate":
            laps = rnd.randint(8, 16)
            pc = cum(PARK)
            stop = [(cum(TO_PARK)[-1] + pc[-1] * rnd.uniform(2, laps - 2), 180)] if rnd.random() < 0.3 else []
            recs = gps_track(rnd, PARK, [(TO_PARK, 0, cum(TO_PARK)[-1])] + [(PARK, 0, pc[-1])] * laps
                             + [(TO_PARK, cum(TO_PARK)[-1], 0)], (19 + 4.2 * prog + rnd.gauss(0, 0.5)) / 3.6,
                             utc, (wind[0] * 0.4, wind[1]), stop)
            name, typ = "Park laps", "Inline Skate"
        elif kind == "run":
            rl = cum(RUN)
            recs = gps_track(rnd, RUN, [(TO_RUN, 0, cum(TO_RUN)[-1]), (RUN, 0, rl[-1]),
                                        (TO_RUN, cum(TO_RUN)[-1], 0)],
                             (9.6 + 1.6 * prog + rnd.gauss(0, 0.3)) / 3.6, utc, (wind[0] * 0.3, wind[1]),
                             [(rnd.uniform(500, 4000), rnd.randint(10, 40))] if rnd.random() < 0.5 else [])
            name, typ = rnd.choice(["Morning run", "Easy run", "Park run"]), "Run"
        elif kind == "ride":
            recs = gps_track(rnd, HARBOUR, [(HARBOUR, 0, harb[-1]), (HARBOUR, harb[-1], 0)] * rnd.choice([1, 1, 2]),
                             (25 + 3.5 * prog + rnd.gauss(0, 0.8)) / 3.6, utc, wind,
                             [(rnd.uniform(2000, 20000), rnd.randint(20, 90)) for _ in range(rnd.randint(1, 3))])
            name, typ = rnd.choice(["Sunday ride", "Harbour ride", "Long ride"]), "Ride"
        else:
            ftp = round(172 + 38 * prog + rnd.gauss(0, 2))
            recs, session = virtual(rnd, utc, rnd.choice([45, 50, 60, 60, 75]), ftp)
            name, typ, desc = rnd.choice(VIRTUAL_ROUTES), "Virtual Ride", "Rouvy"
        fn = f"activities/{aid}.fit"
        write_fit(os.path.join(exp, fn), recs, session)
        rows.append([aid, t0.strftime("%b %-d, %Y, %-I:%M:%S %p") if os.name != "nt" else t0.isoformat(),
                     name, typ, desc, "", fn])
        if recs[0][1] is not None:  # outdoor: weather for the start cell, this UTC day
            cell = f"{round(ORIGIN[0], 1):.1f},{round(ORIGIN[1], 1):.1f}"
            day = weather.setdefault(cell, {})
            hours = [[round(12 + 8 * math.sin((h - 9) / 24 * 2 * math.pi) + rnd.gauss(0, 0.5), 1),
                      round(wind[0] * (0.8 + 0.4 * rnd.random()), 1), round(wind[1] % 360),
                      round(wind[0] * 1.7, 1)] for h in range(24)]
            day[utc.date().isoformat()] = hours

    with open(os.path.join(exp, "activities.csv"), "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["Activity ID", "Activity Date", "Activity Name", "Activity Type",
                    "Activity Description", "Activity Gear", "Filename"])
        w.writerows(rows)
    with open(os.path.join(exp, "profile.csv"), "w", newline="", encoding="utf-8") as f:
        csv.writer(f).writerows([["First Name", "Last Name", "Sex", "Weight"], ["Demo", "Athlete", "Female", "64"]])

    env = dict(os.environ, ROLLBOOK_HOME=home)
    for args in (["db", "build", exp], ["db", "enrich"]):
        subprocess.run([sys.executable, TOOL, *args], env=env, check=True, stdout=subprocess.DEVNULL)

    cache = os.path.join(home, "cache")
    os.makedirs(cache, exist_ok=True)
    with open(os.path.join(cache, "weather.json"), "w") as f:
        json.dump(weather, f)
    with open(os.path.join(cache, "geonames.json"), "w") as f:
        json.dump(place_names(), f)
    with open(os.path.join(home, "profile.json"), "w") as f:
        json.dump({"name": "Demo Athlete", "sex": "female", "birth_year": 1991, "height_cm": 169,
                   "weight_kg": 64, "hr_rest": 52, "hr_max": 192}, f)
    print(f"demo: {len(rows)} sessions in {home}")


def place_names():
    """Fictional streets and areas along each route, on geonames.py's ~55 m grid (and neighbours)."""
    grid = 2000
    named = [(HARBOUR, [(0, "Quay Road", "Old Harbour"), (3000, "Dyke Path", "Polder"),
                        (7000, "Canal Way", "Windmill Point"), (11000, "Seawall Path", "North Dunes")]),
             (TO_PARK + PARK, [(0, "Park Lane", "Lindenpark")]),
             (INLAND, [(0, "Mill Road", "Polder"), (3500, "Orchard Lane", "Appeldorp")]),
             (LAKE, [(0, "Lake Drive", "Meerzicht")]),
             (TO_RUN + RUN, [(0, "Heath Trail", "Heathfield")])]
    out = {}
    for pl, names in named:
        cl = cum(pl)
        d = 0.0
        while d <= cl[-1]:
            (x, y), _ = at(pl, cl, d)
            road, area = next((r, a) for s, r, a in reversed(names) if d >= s)
            la, lo = to_ll(x, y)
            for dy in (-1, 0, 1):
                for dx in (-1, 0, 1):
                    k = f"{(round(la * grid) + dy) / grid:.4f},{(round(lo * grid) + dx) / grid:.4f}"
                    out.setdefault(k, {"road": road, "area": area, "city": "Demotown"})
            d += 20
    return out


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit("usage: tools/demo.py <home dir>")
    main(os.path.abspath(sys.argv[1]))
