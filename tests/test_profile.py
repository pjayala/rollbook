# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Pedro Ayala. Rollbook is free software, see LICENSE.
"""Athlete profile and the per-sport calorie models."""

import csv
import os
import unittest

from helpers import tmpdir

import profile as ath

ME = {"weight_kg": 68.0, "sex": "male", "birth_year": 1980, "height_cm": 175.0}


def act(typ, km, minutes, **kw):
    return {"type": typ, "km": km, "moving_s": minutes * 60, "date": "2026-10-01", **kw}


class Calories(unittest.TestCase):
    def test_needs_a_weight(self):
        self.assertIsNone(ath.calories(act("Run", 10, 50), {}))

    def test_run_is_about_one_kcal_per_kg_per_km(self):
        k = ath.calories(act("Run", 10, 50), ME)
        self.assertEqual(k["source"], "ACSM run")
        self.assertAlmostEqual(k["kcal"] / 68 / 10, 1.05, delta=0.15)

    def test_climbing_costs_more(self):
        flat = ath.calories(act("Run", 10, 50), ME)["kcal"]
        hill = ath.calories(act("Run", 10, 50, ascent_m=300), ME)["kcal"]
        self.assertGreater(hill, flat * 1.1)

    def test_skate_uses_met_by_speed(self):
        k = ath.calories(act("Inline Skate", 18, 48), ME)  # 22.5 km/h
        self.assertTrue(k["source"].startswith("MET"))
        self.assertAlmostEqual(k["kcal"], 600, delta=60)  # watch with HR said 560
        slow = ath.calories(act("Inline Skate", 12, 48), ME)["kcal"]
        self.assertLess(slow, k["kcal"])

    def test_power_only_for_indoor(self):
        """Strava exports estimated watts for outdoor rides: they must not count as measured power."""
        indoor = ath.calories(act("Virtual Ride", 27, 47, s_avg_watts=179), ME)
        self.assertEqual(indoor["source"], "power")
        self.assertAlmostEqual(indoor["kcal"], 179 * 47 * 60 / 0.24 / 4184, places=6)
        outdoor = ath.calories(act("Ride", 15, 44, s_avg_watts=82), ME)
        self.assertTrue(outdoor["source"].startswith("MET"))

    def test_weight_scales_linearly(self):
        a = ath.calories(act("Inline Skate", 18, 48), ME)["kcal"]
        b = ath.calories(act("Inline Skate", 18, 48), {**ME, "weight_kg": 136.0})["kcal"]
        self.assertAlmostEqual(b, 2 * a, places=6)

    def test_hr_cross_check_needs_age_and_sex(self):
        self.assertIsNotNone(ath.calories(act("Run", 10, 50, hr_avg=150), ME)["hr_kcal"])
        self.assertIsNone(ath.calories(act("Run", 10, 50, hr_avg=150), {"weight_kg": 68})["hr_kcal"])

    def test_bmr(self):
        # Mifflin-St Jeor: 10*68 + 6.25*175 - 5*age + 5
        age = ath.profile_age(ME)
        self.assertAlmostEqual(ath.bmr(ME), 680 + 1093.75 - 5 * age + 5, places=6)
        self.assertIsNone(ath.bmr({"weight_kg": 68}))


class ProfileStore(unittest.TestCase):
    def test_prefill_from_export_then_save(self):
        home = tmpdir(self)
        d = os.path.join(home, "cache", "archives", "export_1")
        os.makedirs(d)
        with open(os.path.join(d, "profile.csv"), "w", newline="") as f:
            csv.writer(f).writerows([["Athlete ID", "First Name", "Last Name", "Sex", "Weight"], ["1", "Ana", "B", "Female", "55.5"]])
        p = ath.Profile(home).get()
        self.assertEqual((p["name"], p["sex"], p["weight_kg"]), ("Ana B", "female", 55.5))
        saved = ath.Profile(home).set({"name": "Ana", "sex": "female", "weight_kg": "56", "birth_year": "1990", "height_cm": ""})
        self.assertEqual(saved["weight_kg"], 56.0)
        self.assertNotIn("height_cm", saved)
        self.assertEqual(ath.Profile(home).get()["birth_year"], 1990)  # persisted, no longer from the export

    def test_validation(self):
        P = ath.Profile(tmpdir(self))
        for bad in ({"weight_kg": "5000"}, {"weight_kg": "abc"}, {"sex": "robot"}, {"birth_year": "1700"}):
            with self.assertRaises(ValueError):
                P.set(bad)


if __name__ == "__main__":
    unittest.main()
