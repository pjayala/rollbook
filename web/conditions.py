# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Pedro Ayala. Rollbook is free software, see LICENSE.
"""Weather and, for out-and-backs, the wind / slope split of the leg speeds."""

import math
from datetime import datetime, timedelta, timezone

WX = None  # weather.Weather, set by server.start()

# Physics per sport for the slope term: speed change at constant power on a
# small grade, dv/v = -m g s / (Crr m g + 1.5 rho CdA v^2). Running uses an
# empirical ~3% speed per 1% grade instead. The wind term is fitted on the
# user's own out-and-back sessions (model wind at 10 m is not what a skater
# feels by a riverbank, so physics would overstate it ~5x).
PHYS = {"Inline Skate": (75, 0.008, 0.35), "Ride": (85, 0.005, 0.32), "Run": None, "Walk": None}
WIND_K_DEFAULT = {"Inline Skate": 0.4, "Ride": 0.5, "Run": 0.15, "Walk": 0.05}
NO_WEATHER = ("Virtual Ride", "Virtual Run", "Swim", "Workout", "Weight Training", "Yoga")
FITS = {}  # type -> wind coefficient, %/(km/h) per leg


def slope_pct(typ, v_kmh, g):
    """% speed change from grade g (%), e.g. -0.1% grade -> +2.5% for a skater."""
    if g is None or not v_kmh:
        return 0.0
    if typ in ("Run", "Walk", "Hike"):
        return -3.0 * g if typ == "Run" else -2.0 * g
    m, crr, cda = PHYS.get(typ) or PHYS["Inline Skate"]
    v = v_kmh / 3.6
    return -100 * (m * 9.81 * g / 100) / (crr * m * 9.81 + 1.5 * 1.2 * cda * v * v)


def tailwind(w, brg):
    """km/h of wind pushing along heading brg (wind direction is where it comes FROM)."""
    return -w["ws"] * math.cos(math.radians(w["wd"] - brg))


def conditions(r, m):
    """Weather and, for out-and-backs, the wind / slope split of the leg speeds."""
    typ = r.get("type") or ""
    if typ in NO_WEATHER or typ.startswith("Virtual") or not m.get("ll0") or m.get("route") in ("indoor", "virtual"):
        return None
    try:
        t0 = datetime.fromisoformat(r["datetime"])
    except (TypeError, ValueError):
        return None
    if t0.tzinfo is None:
        t0 = t0.replace(tzinfo=timezone.utc)
    lat, lon = m["ll0"]
    mid = WX.at(lat, lon, t0 + timedelta(seconds=(m.get("dur_s") or 0) / 2))
    if mid is None:
        return {"pending": True}
    c = {k: mid[k] for k in ("temp", "ws", "wd", "gust")}
    if m.get("route") != "out-back" or not m.get("out_b10") or not m.get("back_b10"):
        return c
    tt = m.get("t_turn") or (m.get("dur_s") or 0) / 2
    wo = WX.at(lat, lon, t0 + timedelta(seconds=tt / 2)) or mid
    wb = WX.at(lat, lon, t0 + timedelta(seconds=(tt + (m.get("dur_s") or tt * 2)) / 2)) or mid
    k = FITS.get(typ, WIND_K_DEFAULT.get(typ, 0.3))
    legs = {}
    for leg, w, brg in (("out", wo, m["bearing_out"]), ("back", wb, m["bearing_out"] + 180)):
        tw = tailwind(w, brg)
        se = slope_pct(typ, m[f"{leg}_b10"], m.get(f"g_{leg}"))
        we = k * tw
        legs[leg] = {"tail": tw, "grade": m.get(f"g_{leg}"), "wind_pct": we, "slope_pct": se,
                     "adj_b10": m[f"{leg}_b10"] / (1 + (we + se) / 100)}
    gap = (m["out_b10"] / m["back_b10"] - 1) * 100
    wpart = legs["out"]["wind_pct"] - legs["back"]["wind_pct"]
    spart = legs["out"]["slope_pct"] - legs["back"]["slope_pct"]
    c.update(legs=legs, gap_pct=gap, gap_wind=wpart, gap_slope=spart, gap_other=gap - wpart - spart,
             adjusted=(legs["out"]["adj_b10"] + legs["back"]["adj_b10"]) / 2, wind_k=k)
    return c


def fit_wind(typ, rows, ms):
    """Least-squares wind coefficient from this sport's out-and-backs (gap minus slope ~ a + k * tailwind diff)."""
    xs, ys = [], []
    for r in rows:
        c, m = r.get("cond"), ms.get(r["id"], {})
        if not c or "legs" not in c:
            continue
        L = c["legs"]
        xs.append(L["out"]["tail"] - L["back"]["tail"])
        ys.append(c["gap_pct"] - c["gap_slope"])
    if len(xs) < 30:
        return None
    mx, my = sum(xs) / len(xs), sum(ys) / len(ys)
    sxx = sum((x - mx) ** 2 for x in xs)
    if sxx <= 0:
        return None
    k = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / sxx
    return max(0.0, min(2.0, k))
