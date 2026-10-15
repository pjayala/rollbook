# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Pedro Ayala. Rollbook is free software, see LICENSE.
"""Parsing and distance logic: the hand-verified fixtures must stay exact."""

import os
import unittest
from datetime import timedelta

from helpers import SAMPLES, gz_copy, line_points, load_tool, tmpdir, write_fit

rb = load_tool()


def analyse(path):
    meta, s = rb.load_file(path)
    c = rb.core(s)
    return meta, c


class Fixtures(unittest.TestCase):
    """Numbers from AGENTS.md "Regression fixtures"; any parsing change must keep them."""

    def check(self, name, km, elapsed, moving, pause, hr, best30):
        _, c = analyse(os.path.join(SAMPLES, name))
        self.assertEqual(f"{c['km']:.3f}", km)
        self.assertEqual(rb.hm(c["elapsed"]), elapsed)
        self.assertEqual(rb.hm(c["moving"]), moving)
        self.assertEqual(rb.hm(c["pause"]), pause)
        self.assertEqual(round(c["hr_avg"]), hr)
        self.assertEqual(f"{c['best30']:.2f}", best30)
        return c

    def test_outdoor_cycle(self):
        c = self.check("Outdoor_cycle.gpx", "18.206", "55:02", "51:08", "3:54", 145, "25.42")
        ob = rb.outback(c)
        self.assertEqual(f"{ob['out']['km']:.3f}", "8.761")
        self.assertEqual(f"{ob['out']['kmh']:.2f}", "22.53")
        self.assertEqual(f"{ob['back']['km']:.3f}", "9.445")
        self.assertEqual(f"{ob['back']['kmh']:.2f}", "20.39")

    def test_the_come_back(self):
        self.check("The_come_back.gpx", "14.040", "40:40", "38:42", "1:58", 154, "27.04")

    def test_gzip_is_transparent(self):
        d = tmpdir(self)
        dst = os.path.join(d, "x.gpx.gz")
        gz_copy(os.path.join(SAMPLES, "Outdoor_cycle.gpx"), dst)
        _, c = analyse(dst)
        self.assertEqual(f"{c['km']:.3f}", "18.206")

    def test_splits_cover_the_distance(self):
        _, c = analyse(os.path.join(SAMPLES, "Outdoor_cycle.gpx"))
        sp = rb.splits(c)  # (elapsed, moving, hr) per completed km
        self.assertEqual(len(sp), 18)
        self.assertTrue(all(lm <= lt for lt, lm, _ in sp))
        self.assertLess(sum(x[0] for x in sp), c["elapsed"])  # the last 0.2 km isn't a split


class Fit(unittest.TestCase):
    def test_little_and_big_endian_agree(self):
        d = tmpdir(self)
        pts = line_points(600)
        res = []
        for be in (False, True):
            p = os.path.join(d, f"{'be' if be else 'le'}.fit")
            write_fit(p, pts, big_endian=be)
            _, c = analyse(p)
            res.append(c)
        self.assertAlmostEqual(res[0]["km"], 599 * 6 / 1000, delta=0.02)
        self.assertAlmostEqual(res[0]["km"], res[1]["km"], places=6)
        self.assertEqual(res[0]["hr_avg"], 140)

    def test_sport_from_session_when_no_sport_message(self):
        """HUAWEI "Export Original": no sport message, sport only in the session."""
        d = tmpdir(self)
        p = os.path.join(d, "a.fit")
        write_fit(p, line_points(100), session_sport=2)
        meta, _ = analyse(p)
        self.assertEqual(meta["type"], "cycling")

    def test_sport_message_wins(self):
        d = tmpdir(self)
        p = os.path.join(d, "a.fit")
        write_fit(p, line_points(100), session_sport=2, sport_msg=30)
        meta, _ = analyse(p)
        self.assertEqual(meta["type"], "inline_skating")

    def test_samples_before_first_fix_keep_the_track(self):
        d = tmpdir(self)
        pts = line_points(300)
        pts = [(t, None, None, e, h) if i < 20 else (t, la, lo, e, h) for i, (t, la, lo, e, h) in enumerate(pts)]
        p = os.path.join(d, "a.fit")
        write_fit(p, pts)
        _, c = analyse(p)
        self.assertAlmostEqual(c["km"], (299 - 20) * 6 / 1000, delta=0.02)


class Power(unittest.TestCase):
    """Smart trainers (ROUVY / MyWhoosh) and power meters write watts in FIT field 7."""

    def test_fit_power_both_endians(self):
        d = tmpdir(self)
        pts = [(*p, 200 if i % 2 else 0) for i, p in enumerate(line_points(300))]
        pts[5] = (*pts[5][:5], None)  # a record without a reading
        for be in (False, True):
            p = os.path.join(d, f"{be}.fit")
            write_fit(p, pts, big_endian=be)
            _, s = rb.load_file(p)
            c = rb.core(s)
            self.assertIsNone(s["watts"]["data"][5])
            self.assertEqual(s["watts"]["data"][:4], [0, 200, 0, 200])  # 0 W is a reading, not "missing"
            self.assertAlmostEqual(c["watts_avg"], 299 * 100 / 299, delta=0.5)
            self.assertEqual(c["watts_max"], 200)

    def test_no_power_channel(self):
        d = tmpdir(self)
        p = os.path.join(d, "a.fit")
        write_fit(p, line_points(100))
        _, s = rb.load_file(p)
        self.assertNotIn("watts", s)
        self.assertIsNone(rb.core(s)["watts_avg"])
        _, s = rb.load_file(os.path.join(SAMPLES, "Outdoor_cycle.gpx"))
        self.assertNotIn("watts", s)

    def test_gpx_and_tcx_power_extensions(self):
        d = tmpdir(self)
        gpx = os.path.join(d, "a.gpx")
        with open(gpx, "w") as f:
            f.write('<?xml version="1.0"?><gpx xmlns="http://www.topografix.com/GPX/1/1"><trk><trkseg>' + "".join(
                f'<trkpt lat="10" lon="{20 + i * 1e-4}"><time>2026-01-01T08:00:{i:02d}Z</time>'
                f'<extensions><power>{150 + i}</power></extensions></trkpt>' for i in range(10)) + "</trkseg></trk></gpx>")
        _, s = rb.load_file(gpx)
        self.assertEqual(s["watts"]["data"][:3], [150, 151, 152])
        tcx = os.path.join(d, "a.tcx")
        ns = "http://www.garmin.com/xmlschemas/TrainingCenterDatabase/v2"
        ext = "http://www.garmin.com/xmlschemas/ActivityExtension/v2"
        with open(tcx, "w") as f:
            f.write(f'<?xml version="1.0"?><TrainingCenterDatabase xmlns="{ns}" xmlns:ns3="{ext}"><Activities>'
                    '<Activity Sport="Biking"><Lap><Track>' + "".join(
                        f'<Trackpoint><Time>2026-01-01T08:00:{i:02d}Z</Time><DistanceMeters>{i * 8}</DistanceMeters>'
                        f'<Extensions><ns3:TPX><ns3:Watts>{210 + i}</ns3:Watts></ns3:TPX></Extensions></Trackpoint>'
                        for i in range(10)) + "</Track></Lap></Activity></Activities></TrainingCenterDatabase>")
        _, s = rb.load_file(tcx)
        self.assertEqual(s["watts"]["data"][-1], 219)


class GpsLogic(unittest.TestCase):
    def test_fix_loss_is_spread_not_one_spike(self):
        """134 s without a fix once became a 149 km/h step and a bogus best-30 s."""
        pts = line_points(900, speed_ms=6.0)
        t = [int((p[0] - pts[0][0]).total_seconds()) for p in pts]
        ll = [[None, None] if 300 <= i < 434 else [p[1], p[2]] for i, p in enumerate(pts)]
        d = rb.cumulative_gps(t, ll)
        steps = [(d[i + 1] - d[i]) / (t[i + 1] - t[i]) for i in range(len(t) - 1)]
        self.assertLess(max(steps), 6.5)  # never faster than the real 6 m/s (+ rounding)
        self.assertAlmostEqual(d[-1], 899 * 6.0, delta=899 * 6.0 * 0.005)

    def test_gps_glitch_rejected(self):
        pts = line_points(200)
        t = list(range(200))
        ll = [[p[1], p[2]] for p in pts]
        ll[100] = [ll[100][0] + 0.05, ll[100][1]]  # 5 km teleport for one sample
        d = rb.cumulative_gps(t, ll)
        self.assertAlmostEqual(d[-1], 199 * 6.0, delta=199 * 6.0 * 0.005)

    def test_recording_gap_earns_no_distance(self):
        """Recording stopped, moved by car, resumed: the jump must not count."""
        pts = line_points(200) + [(p[0] + timedelta(minutes=30), p[1] + 0.3, p[2], p[3], p[4]) for p in line_points(200)]
        t = [int((p[0] - pts[0][0]).total_seconds()) for p in pts]
        ll = [[p[1], p[2]] for p in pts]
        d = rb.cumulative_gps(t, ll)
        self.assertAlmostEqual(d[-1], 2 * 199 * 6.0, delta=2 * 199 * 6.0 * 0.005)


class Elevation(unittest.TestCase):
    def test_filler_minus_one_is_dropped(self):
        """GT 6 Pro: a real altitude every ~5 s, -1 m on every other record."""
        real = [150 + (i % 50) / 25 for i in range(1000)]
        ele = [r if i % 5 == 0 else -1.0 for i, r in enumerate(real)]
        ll = [[10.0, 20.0]] * len(ele)
        out = rb.clean_ele(ele, ll)
        self.assertGreater(min(out), 149)
        self.assertLess(rb.climb(rb.smooth(out)), 30)  # was thousands of metres

    def test_zero_without_fix_is_dropped(self):
        ele = [0.0] * 10 + [100.0] * 90
        ll = [[None, None]] * 10 + [[10.0, 20.0]] * 90
        self.assertEqual(min(rb.clean_ele(ele, ll)), 100.0)

    def test_real_low_altitude_is_kept(self):
        ele = [0.0, 1.0, 2.0, 1.0] * 30  # a seaside session really at 0-2 m
        ll = [[10.0, 20.0]] * len(ele)
        self.assertEqual(rb.clean_ele(ele, ll), ele)

    def test_climb_hysteresis(self):
        self.assertEqual(rb.climb([0, 3, 0, 3, 0]), 0)  # noise below 5 m
        self.assertEqual(rb.climb([0, 10, 5, 20]), 25)


if __name__ == "__main__":
    unittest.main()
