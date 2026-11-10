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



def sess(i, date, held_at, spike=None, secs=40, typ="Inline Skate"):
    """A session whose HR histogram holds `held_at` bpm for `secs` s, plus an optional 2 s spike."""
    hist = {str(held_at): secs, str(held_at - 20): 1800}
    if spike:
        hist[str(spike)] = 2
    return {"id": i, "date": date, "type": typ, "hr_max": float(spike or held_at), "hist": hist}


class MaxHr(unittest.TestCase):
    def test_peak_needs_the_hold(self):
        self.assertEqual(ath.hr_peak({"173": 2, "164": 40, "150": 900}), 164)
        self.assertEqual(ath.hr_peak({"160": 10, "158": 25}), 158)  # 35 s at or above 158
        self.assertIsNone(ath.hr_peak({}))
        self.assertIsNone(ath.hr_peak({"250": 600}))  # glitch values never count

    def test_estimate_ignores_spikes_and_the_window(self):
        rows = [sess(1, "2026-09-11", 164), sess(2, "2026-10-07", 157, spike=173), sess(3, "2025-06-22", 172)]
        e = ath.hrmax_estimate(rows, 12)
        self.assertEqual((e["bpm"], e["top"][0]["id"], e["recorded"], e["n"]), (164, 1, 173.0, 2))
        self.assertEqual((e["from"], e["to"]), ("2025-10-07", "2026-10-07"))
        self.assertEqual(ath.hrmax_estimate(rows, 24)["bpm"], 172)

    def test_window_ends_at_the_newest_session(self):
        e = ath.hrmax_estimate([sess(1, "2019-03-31", 180)], 1)
        self.assertEqual((e["bpm"], e["from"]), (180, "2019-02-28"))  # clamped to the month
        self.assertEqual(ath._months_before("2024-08-31", 6), "2024-02-29")
        self.assertEqual(ath._months_before("2026-01-15", 13), "2024-12-15")

    def test_choice_and_age(self):
        e = ath.hrmax_estimate([sess(1, "2026-09-11", 164, spike=173)], 12)
        self.assertEqual(ath.hrmax_choice({"hr_max": 160}, e), (160, "profile"))
        self.assertEqual(ath.hrmax_choice({}, e), (164, "estimate"))
        self.assertEqual(ath.hrmax_choice({}, {**e, "bpm": None}), (173.0, "recorded"))
        self.assertEqual(ath.hrmax_choice({}, None), (None, None))
        self.assertIsNone(ath.hrmax_estimate([]))
        self.assertEqual(ath.age_hrmax({"birth_year": 1980}), round(208 - 0.7 * ath.profile_age({"birth_year": 1980})))
        self.assertIsNone(ath.age_hrmax({}))

    def test_months_field(self):
        self.assertEqual(ath.validate({"hr_months": "6"}), {"hr_months": 6})
        self.assertEqual(ath.validate({"hr_months": 99}), {})



class MaxHrByDate(unittest.TestCase):
    ROWS = [sess(1, "2025-06-22", 172), sess(2, "2026-09-11", 164), sess(3, "2026-10-07", 157, spike=173)]

    def test_history_dates_the_profile_value(self):
        p = {"hr_max": 166, "hr_max_history": [{"from": ath.BEGIN, "bpm": 160}, {"from": "2026-11-10", "bpm": 166}]}
        h = ath.HrmaxAt(p, self.ROWS)
        self.assertEqual(h.at("2026-11-09"), (160, "profile"))
        self.assertEqual(h.at("2026-11-10"), (166, "profile"))

    def test_no_profile_value_then_suggestion_as_of_that_date(self):
        p = {"hr_max": 170, "hr_max_history": [{"from": "2026-11-10", "bpm": 170}]}
        h = ath.HrmaxAt(p, self.ROWS)
        self.assertEqual(h.at("2025-07-01"), (172, "estimate"))  # June 2025 is in its 12 months
        self.assertEqual(h.at("2026-10-07"), (164, "estimate"))  # the 172 is out of the window, the spike ignored
        self.assertEqual(h.at("2025-01-01"), (None, None))
        cleared = {"hr_max_history": [{"from": ath.BEGIN, "bpm": 160}, {"from": "2026-01-01", "bpm": None}]}
        self.assertEqual(ath.HrmaxAt(cleared, self.ROWS).at("2026-10-07"), (164, "estimate"))

    def test_without_history_the_profile_value_applies_everywhere(self):
        self.assertEqual(ath.HrmaxAt({"hr_max": 160}, self.ROWS).at("2020-01-01"), (160, "profile"))

    def test_set_records_changes(self):
        home = tmpdir(self)
        pr = ath.Profile(home)
        pr.set({"hr_max": 160}, today="2026-01-01")
        self.assertEqual(pr.get()["hr_max_history"], [{"from": "2026-01-01", "bpm": 160}])
        pr.set({"hr_max": 160, "weight_kg": 70}, today="2026-02-01")  # unchanged: no entry
        pr.set({"hr_max": 164}, today="2026-03-01")
        pr.set({"hr_max": 165}, today="2026-03-01")  # same day: replaced
        pr.set({}, today="2026-04-01")  # cleared
        self.assertEqual(ath.Profile(home).get()["hr_max_history"],
                         [{"from": "2026-01-01", "bpm": 160}, {"from": "2026-03-01", "bpm": 165}, {"from": "2026-04-01", "bpm": None}])

    def test_legacy_value_kept_for_the_past(self):
        home = tmpdir(self)
        with open(os.path.join(home, "profile.json"), "w") as f:
            f.write('{"hr_max": 160}')
        pr = ath.Profile(home)
        pr.set({"hr_max": 166}, today="2026-11-10")
        self.assertEqual(pr.get()["hr_max_history"], [{"from": ath.BEGIN, "bpm": 160}, {"from": "2026-11-10", "bpm": 166}])

    def test_apply_to_all_past(self):
        home = tmpdir(self)
        pr = ath.Profile(home)
        pr.set({"hr_max": 160}, today="2026-01-01")
        pr.set({"hr_max": 166, "hr_max_all": True}, today="2026-03-01")
        self.assertEqual(pr.get()["hr_max_history"], [{"from": ath.BEGIN, "bpm": 166}])
        self.assertEqual(ath.HrmaxAt(pr.get(), []).at("2025-01-01"), (166, "profile"))
        pr.set({"hr_max_all": True}, today="2026-04-01")  # cleared for all: the suggestion everywhere
        self.assertEqual(ath.HrmaxAt(pr.get(), []).at("2026-05-01"), (None, None))

    def test_history_compacted_and_oldest_kept(self):
        h = [{"from": "2026-01-01", "bpm": 160}, {"from": "2026-01-02", "bpm": 160}, {"from": "2026-01-03", "bpm": 165}]
        self.assertEqual(ath.clean_history(h), [{"from": "2026-01-01", "bpm": 160}, {"from": "2026-01-03", "bpm": 165}])
        many = [{"from": ath.BEGIN, "bpm": 150}] + [{"from": f"{2000 + i // 365:04d}-{1 + i % 12:02d}-{1 + i % 28:02d}",
                                                      "bpm": 160 + i % 2} for i in range(2000)]
        out = ath.clean_history(many)
        self.assertEqual((len(out) <= ath.HISTORY_MAX, out[0]["from"]), (True, ath.BEGIN))

    def test_odd_history_dates_rejected(self):
        for bad in ("2026-01- 1", "\u0662\u0660\u0662\u0666-01-01", "2026-1-01"):
            self.assertEqual(ath.clean_history([{"from": bad, "bpm": 160}]), [], bad)

    def test_peak_skips_bad_histogram_entries(self):
        self.assertEqual(ath.hr_peak({"x": 5, "160": "a", "155": 40, "-3": 99, "150": True}), 155)
        self.assertEqual(ath.hr_peak({164: 40, 150: 900}), 164)  # a fresh derive() has int keys
        self.assertIsNone(ath.hr_peak(["nope"]))

    def test_history_from_file_checked(self):
        bad = [{"from": "x", "bpm": 160}, {"from": "2026-01-01", "bpm": 999}, "y", {"from": "2026-02-30", "bpm": 160},
               {"from": "2026-03-01", "bpm": 170}, {"from": "2026-02-01", "bpm": None}]
        self.assertEqual(ath.validate({"hr_max_history": bad})["hr_max_history"],
                         [{"from": "2026-02-01", "bpm": None}, {"from": "2026-03-01", "bpm": 170}])


if __name__ == "__main__":
    unittest.main()
