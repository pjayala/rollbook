# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Pedro Ayala. Rollbook is free software, see LICENSE.
"""The athlete profile and Rollbook's own calorie estimate, one model per sport.

Strava's and the watches' calorie numbers are not used: on this data the
watch agrees with the textbook rate only when it measured heart rate, and
Strava's fallback for skating without HR is ~2x it. So every session gets the
same per-sport formula, and Strava's figure is shown only as a reference.

Gross energy (kcal) per sport, in order of preference:
  power    rides with recorded power (watts_avg), or Strava's average watts
           indoors: work / 24% gross efficiency. Strava's outdoor watts are
           its own estimate, not used
  ACSM     run / walk / hike: ACSM metabolic equations from speed and grade
           (grade = ascent / distance), the standard oxygen-cost model
  MET      inline skate, outdoor ride without power, others: Compendium of
           Physical Activities MET by speed (or a fixed MET per activity)
  HR       Keytel et al. 2005 from average HR, weight, age and sex; shown as
           a cross-check only, it needs the birth year

Profile lives in $ROLLBOOK_HOME/profile.json; missing fields are prefilled
from the Strava export's profile.csv (name, sex, weight).
"""

import bisect
import csv
import glob
import json
import os
import threading
import time

EFF_POWER = 0.24
KCAL_PER_L_O2 = 5.0

# Compendium of Physical Activities (Ainsworth et al., 2011 / 2024 update):
# (km/h, MET) points, linearly interpolated, extrapolated at the ends
MET_BY_SPEED = {
    "Inline Skate": [(14.4, 5.0), (17.7, 7.5), (21.0, 9.8), (24.0, 12.3)],
    "Ride": [(16.0, 4.0), (19.2, 6.8), (22.4, 8.0), (25.6, 10.0), (30.4, 12.0)],
    "E-Bike Ride": [(16.0, 3.5), (25.0, 4.5)],
}
MET_FIXED = {"Swim": 7.0, "Kayaking": 5.0, "Canoeing": 5.0, "Rowing": 7.0, "Workout": 5.0, "Weight Training": 5.0,
             "Yoga": 2.5, "Ice Skate": 7.0, "Hike": 6.0, "Nordic Ski": 9.0, "Alpine Ski": 5.3, "Rock Climb": 7.5}
ACSM = {"Run": "run", "Virtual Run": "run", "Trail Run": "run", "Walk": "walk", "Hike": "walk"}

FIELDS = {  # name: (type, min, max)
    "name": (str, 0, 80), "sex": (str, 0, 10), "birth_year": (int, 1900, 2100),
    "height_cm": (float, 100, 250), "weight_kg": (float, 25, 300),
    "hr_rest": (int, 25, 120), "hr_max": (int, 100, 240),
    "ftp": (int, 50, 600),  # W, threshold power; overrides ROUVY's and the estimate
}


def _interp(tab, x):
    xs = [p[0] for p in tab]
    if x <= xs[0]:
        return max(1.5, tab[0][1] * x / xs[0]) if x > 0 else 1.5
    if x >= xs[-1]:
        (x0, y0), (x1, y1) = tab[-2], tab[-1]
        return y1 + (y1 - y0) / (x1 - x0) * (x - x1)
    i = bisect.bisect(xs, x)
    (x0, y0), (x1, y1) = tab[i - 1], tab[i]
    return y0 + (y1 - y0) * (x - x0) / (x1 - x0)


def acsm_kcal(kind, km, moving_s, ascent_m, weight):
    """ACSM running / walking equations (ml O2/kg/min), gross, uphill grade only."""
    mins = moving_s / 60
    v = km * 1000 / mins  # m/min
    grade = min(0.3, (ascent_m or 0) / (km * 1000)) if km else 0
    if kind == "run":
        vo2 = 0.2 * v + 0.9 * v * grade + 3.5
    else:
        vo2 = 0.1 * v + 1.8 * v * grade + 3.5
    return vo2 * weight * mins / 1000 * KCAL_PER_L_O2


def keytel_kcal(hr, mins, weight, age, sex):
    """Keytel 2005, kcal from average HR (no VO2max term). None if inputs are missing."""
    if not (hr and mins and weight and age) or sex not in ("male", "female"):
        return None
    if sex == "male":
        k = (-55.0969 + 0.6309 * hr + 0.1988 * weight + 0.2017 * age) / 4.184
    else:
        k = (-20.4022 + 0.4472 * hr - 0.1263 * weight + 0.074 * age) / 4.184
    return max(0.0, k * mins)


def calories(a, profile):
    """{kcal, source, hr_kcal} for an activity row, or None without a weight."""
    w = profile.get("weight_kg")
    if not w:
        return None
    typ = a.get("type") or ""
    km, mv = a.get("km") or 0, a.get("moving_s") or a.get("elapsed_s") or 0
    if mv < 60:
        return None
    out = {}
    # Strava exports *estimated* average watts for outdoor rides (and even some
    # skates) without a power meter, so its figure only counts indoors. Power
    # recorded in the file (watts_avg: trainer or power meter) is a measurement,
    # for any ride; watch "running power" isn't comparable, so not for runs.
    watts = a.get("watts_avg") if "Ride" in typ else None
    if not watts and typ.startswith("Virtual"):
        watts = a.get("s_avg_watts")
    if watts and watts > 20:
        out = {"kcal": watts * mv / EFF_POWER / 4184, "source": "power"}
    elif typ in ACSM and km > 0.2:
        out = {"kcal": acsm_kcal(ACSM[typ], km, mv, a.get("ascent_m"), w), "source": "ACSM " + ACSM[typ]}
    elif typ in MET_BY_SPEED and km > 0.2:
        met = _interp(MET_BY_SPEED[typ], km / (mv / 3600))
        out = {"kcal": met * w * mv / 3600, "source": f"MET {met:.1f}"}
    else:
        met = MET_FIXED.get(typ, 5.0)
        out = {"kcal": met * w * mv / 3600, "source": f"MET {met:.1f}"}
    age = profile_age(profile, a.get("date"))
    out["hr_kcal"] = keytel_kcal(a.get("hr_avg"), mv / 60, w, age, profile.get("sex"))
    return out


def profile_age(p, on=None):
    y = p.get("birth_year")
    if not y:
        return None
    year = int(on[:4]) if on else time.localtime().tm_year
    return max(5, year - y)


def bmr(p):
    """Mifflin-St Jeor resting energy, kcal/day; needs weight, height, age, sex."""
    w, h, age, sex = p.get("weight_kg"), p.get("height_cm"), profile_age(p), p.get("sex")
    if not (w and h and age) or sex not in ("male", "female"):
        return None
    return 10 * w + 6.25 * h - 5 * age + (5 if sex == "male" else -161)


class Profile:
    def __init__(self, home):
        self.path = os.path.join(home, "profile.json")
        self.home, self.lock = home, threading.Lock()
        try:
            with open(self.path) as f:
                self.data = json.load(f)
        except (OSError, ValueError):
            self.data = {}
        if not self.data:
            self.data = self._from_export()

    def _from_export(self):
        """Name, sex and weight from the newest extracted Strava export, if any."""
        files = sorted(glob.glob(os.path.join(self.home, "cache", "archives", "*", "profile.csv")), key=os.path.getmtime)
        if not files:
            return {}
        try:
            with open(files[-1], newline="", encoding="utf-8-sig") as f:
                row = next(csv.DictReader(f))
        except (OSError, StopIteration, csv.Error):
            return {}
        out = {"from_export": True}
        name = " ".join(x for x in (row.get("First Name"), row.get("Last Name")) if x)
        if name:
            out["name"] = name
        if (row.get("Sex") or "").lower() in ("male", "female"):
            out["sex"] = row["Sex"].lower()
        try:
            out["weight_kg"] = float(row.get("Weight") or "")
        except ValueError:
            pass
        return out

    def get(self):
        with self.lock:
            p = dict(self.data)
        p["age"] = profile_age(p)
        p["bmr"] = bmr(p)
        if p.get("weight_kg") and p.get("height_cm"):
            p["bmi"] = p["weight_kg"] / (p["height_cm"] / 100) ** 2
        return p

    def set(self, body):
        clean = {}
        for k, (typ, lo, hi) in FIELDS.items():
            v = body.get(k)
            if v in (None, ""):
                continue
            if typ is str:
                v = str(v).strip()[:hi]
                if k == "sex" and v not in ("male", "female", "other"):
                    raise ValueError("sex must be male, female or other")
            else:
                try:
                    v = typ(v)
                except (TypeError, ValueError):
                    raise ValueError(f"{k}: not a number")
                if not lo <= v <= hi:
                    raise ValueError(f"{k}: must be between {lo} and {hi}")
            clean[k] = v
        with self.lock:
            self.data = clean
            tmp = self.path + ".tmp"
            with open(tmp, "w") as f:
                json.dump(clean, f, indent=1)
            os.replace(tmp, self.path)
        return self.get()
