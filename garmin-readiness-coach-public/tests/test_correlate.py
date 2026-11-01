"""Correlation Explorer: Spearman (with ties), the interval and p-value, lag alignment, the 21-day
minimum, the "no weak findings" rule, wording, top patterns and the endpoints (demo data, Claude mocked).

Run from the project folder:  .venv/bin/python -m unittest discover -s tests -v
Uses a throwaway COACH_DATA_DIR and cache folder; never calls Garmin or Claude.
"""
import json
import math
import os
import random
import sys
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
os.environ.setdefault("COACH_DATA_DIR", tempfile.mkdtemp(prefix="coach-test-"))
sys.path.insert(0, str(ROOT))

import correlate as C  # noqa: E402
import store  # noqa: E402

assert store.DATA_DIR != (ROOT / ".data").resolve(), "tests must never run against the real data folder"
D0 = date(2026, 1, 1)
day = lambda i: (D0 + timedelta(days=i)).isoformat()
SLEEP, HRV = C.measure("sleep_h"), C.measure("hrv")
ENERGY = C.measure("checkin:energy", {"energy": "Energy"})
WORKOUT, LOAD = C.measure("workout"), C.measure("load")


def series(vals, start=0):
    return {day(start + i): float(v) for i, v in enumerate(vals) if v is not None}


class Statistics(unittest.TestCase):
    def test_spearman_textbook_example(self):
        iq = [106, 100, 86, 101, 99, 103, 97, 113, 112, 110]  # Wikipedia's IQ vs TV-hours example: rho = -29/165
        tv = [7, 27, 2, 50, 28, 29, 20, 12, 6, 17]
        self.assertAlmostEqual(C.spearman(iq, tv), -29 / 165, places=4)

    def test_perfect_monotone_and_constant(self):
        self.assertAlmostEqual(C.spearman([1, 2, 3, 4, 5], [2, 4, 8, 16, 32]), 1.0)  # monotone, not linear
        self.assertAlmostEqual(C.spearman([1, 2, 3, 4, 5], [9, 7, 5, 3, 1]), -1.0)
        self.assertIsNone(C.spearman([1, 2, 3], [5, 5, 5]))

    def test_ties_get_average_ranks(self):
        self.assertEqual(C.ranks([10, 20, 20, 30, 20]), [1.0, 3.0, 3.0, 5.0, 3.0])
        # by hand: ranks x = 1, 2.5, 2.5, 4 and y = 1, 3, 2, 4 -> 4.5 / sqrt(4.5 * 5)
        self.assertAlmostEqual(C.spearman([1, 2, 2, 3], [1, 3, 2, 4]), 4.5 / math.sqrt(22.5), places=10)

    def test_interval_and_p_value(self):
        lo, hi, p = C.interval(0.5, 30)  # Fisher z with se = sqrt(1.06/27)
        self.assertAlmostEqual(lo, 0.1596, places=3)
        self.assertAlmostEqual(hi, 0.7340, places=3)
        self.assertAlmostEqual(p, 0.00557, places=4)
        lo2, hi2, _ = C.interval(0.5, 300)
        self.assertLess(hi2 - lo2, hi - lo)  # more days, narrower interval
        self.assertAlmostEqual(C.interval(0.0, 50)[2], 1.0)

    def test_trend_line(self):
        self.assertEqual(C.trend([1, 2, 3], [3, 5, 7]), (2.0, 1.0))
        self.assertIsNone(C.trend([2, 2, 2], [1, 2, 3]))


class LagAlignment(unittest.TestCase):
    def test_lag_pairs_yesterdays_x_with_todays_y(self):
        xs, ys = series([10, 20, 30, 40]), series([1, 2, 3, 4])
        self.assertEqual(C.pairs(xs, ys, 0), [(day(0), 10.0, 1.0), (day(1), 20.0, 2.0), (day(2), 30.0, 3.0), (day(3), 40.0, 4.0)])
        self.assertEqual(C.pairs(xs, ys, 1), [(day(1), 10.0, 2.0), (day(2), 20.0, 3.0), (day(3), 30.0, 4.0)])
        self.assertEqual(C.pairs(xs, ys, 3), [(day(3), 10.0, 4.0)])

    def test_missing_days_on_either_side_drop_out(self):
        xs, ys = series([10, None, 30, 40, 50]), series([1, 2, 3, None, 5])
        self.assertEqual([k for k, *_ in C.pairs(xs, ys, 0)], [day(0), day(2), day(4)])  # day 1 lacks X, day 3 lacks Y
        self.assertEqual([k for k, *_ in C.pairs(xs, ys, 1)], [day(1), day(4)])  # day 2's X would be day 1's: missing

    def test_activity_measures_are_zero_on_loaded_days_and_garmin_gaps_stay_gaps(self):
        rows = {day(0): dict(activities=[dict(type="running", dist_m=5000, secs=1800)], hrv=None, load=80),
                day(1): dict(activities=[dict(type="walking", dist_m=2000, secs=1500)], hrv=60.0, load=None)}
        self.assertEqual(C.values(WORKOUT, rows, {}, {}), {day(0): 1.0, day(1): 0.0})  # a walk isn't a workout
        self.assertEqual(C.values(C.measure("run_mi"), rows, {}, {}), {day(0): 5000.0, day(1): 0.0})
        self.assertEqual(C.values(HRV, rows, {}, {}), {day(1): 60.0})
        self.assertEqual(C.values(LOAD, rows, {}, {}), {day(0): 80.0, day(1): 0.0})
        cis = {day(1): dict(day=day(1), ratings=dict(energy=1), note="private")}
        self.assertEqual(C.values(ENERGY, rows, cis, {"energy": "Energy"}), {day(1): 1.0})

    def test_a_lagged_relationship_is_found_only_at_its_lag(self):
        rnd = random.Random(4)
        load = [rnd.uniform(0, 200) for _ in range(80)]
        hrv = [60.0] + [70 - l / 10 + rnd.uniform(-2, 2) for l in load[:-1]]  # today's HRV follows yesterday's load
        xs, ys = series(load), series(hrv)
        self.assertTrue(C.analyze(LOAD, HRV, xs, ys, 1)["finding"])
        self.assertLess(C.analyze(LOAD, HRV, xs, ys, 1)["rho"], -0.8)
        self.assertFalse(C.analyze(LOAD, HRV, xs, ys, 0)["finding"])


class MinimumSample(unittest.TestCase):
    def test_20_days_is_not_enough_21_is(self):
        x = list(range(30))
        r20 = C.analyze(SLEEP, HRV, series(x[:20]), series(x[:20]), 0)
        self.assertEqual((r20["status"], r20["n"], r20["finding"]), ("not_enough", 20, False))
        self.assertIn("20 matched days of the 21", r20["message"])
        self.assertNotIn("rho", r20)
        r21 = C.analyze(SLEEP, HRV, series(x[:21]), series(x[:21]), 0)
        self.assertEqual((r21["status"], r21["n"]), ("ok", 21))

    def test_the_lag_counts_against_the_sample(self):
        x = list(range(22))
        self.assertEqual(C.analyze(SLEEP, HRV, series(x), series(x), 0)["status"], "ok")
        self.assertEqual(C.analyze(SLEEP, HRV, series(x), series(x), 2)["status"], "not_enough")  # 20 pairs once shifted

    def test_no_variation(self):
        r = C.analyze(WORKOUT, HRV, series([1] * 30), series(range(30)), 0)
        self.assertEqual(r["status"], "no_variation")
        self.assertIn("Workout", r["message"])


class HonestFindings(unittest.TestCase):
    def test_noise_is_not_a_finding(self):
        flagged = 0
        for seed in range(40):
            rnd = random.Random(seed)
            r = C.analyze(SLEEP, HRV, series([rnd.random() for _ in range(30)]), series([rnd.random() for _ in range(30)]), 0)
            flagged += r["finding"]
        self.assertLessEqual(flagged, 2)  # the |rho| >= 0.3 bar plus the interval keep chance hits rare

    def test_a_weak_but_significant_relationship_is_not_a_finding(self):
        rnd = random.Random(1)
        x = [rnd.gauss(0, 1) for _ in range(400)]
        y = [0.25 * a + rnd.gauss(0, 1) for a in x]
        r = C.analyze(SLEEP, HRV, series(x), series(y), 0)
        self.assertLess(r["p"], 0.001)
        self.assertLess(abs(r["rho"]), C.FINDING_RHO)
        self.assertFalse(r["finding"])
        self.assertIsNone(r["sentence"])
        self.assertTrue(r["summary"].startswith("No clear relationship"))
        self.assertEqual(r["confidence"], "Low")

    def test_strength_words(self):
        self.assertEqual([C.strength(v) for v in (0.05, -0.2, 0.35, -0.6)], ["negligible", "weak", "moderate", "strong"])


class Wording(unittest.TestCase):
    def finding(self, xm, ym, lag=0):
        rnd = random.Random(7)
        x = [rnd.choice([5.5, 6.0, 6.5, 7.2, 7.8, 8.4]) for _ in range(60)]
        if xm["kind"] == "binary":
            x = [float(v > 7) for v in x]
        y = [(1.0 if (v > 7 if xm["kind"] != "binary" else v) else -1.0) if rnd.random() < 0.85 else 0.0 for v in x]
        xs = series(x)
        ys = {(date.fromisoformat(k) + timedelta(days=lag)).isoformat(): v for k, v in series(y).items()}
        r = C.analyze(xm, ym, xs, ys, lag)
        self.assertTrue(r["finding"], r)
        return r

    def test_sleep_and_energy_reads_like_the_example(self):
        r = self.finding(SLEEP, ENERGY)
        self.assertRegex(r["sentence"], r"^On days with 7\+ hours of sleep, your energy was rated thumbs up on \d+% of those days, vs \d+% otherwise\.$")
        self.assertGreater(r["groups"]["with_pct"], r["groups"]["without_pct"])
        r1 = self.finding(SLEEP, ENERGY, lag=1)
        self.assertTrue(r1["sentence"].startswith("On days after 7+ hours of sleep"))

    def test_never_says_cause(self):
        texts = [self.finding(SLEEP, ENERGY)["summary"], self.finding(WORKOUT, ENERGY, 1)["summary"], C.REMINDER, C.EXPLAIN_SYSTEM]
        for t in texts[:2]:
            self.assertRegex(t, r"tends to|associat")
        for t in texts[:3]:
            self.assertNotRegex(t.lower(), r"\bcause[sd]?\b|leads to|because of")
        self.assertIn("isn’t causation", C.REMINDER)


class TopPatterns(unittest.TestCase):
    def test_only_pairs_that_pass_are_listed_strongest_first(self):
        rnd = random.Random(3)
        n = 90
        sleep = [rnd.uniform(5.5, 8.5) for _ in range(n)]
        ms = {k: C.measure(k) for k in ("sleep_h", "hrv", "load", "rhr")}
        s = dict(sleep_h=series(sleep), hrv=series([40 + 4 * v + rnd.uniform(-2, 2) for v in sleep]),
                 load=series([rnd.uniform(0, 200) for _ in range(n)]), rhr=series([rnd.uniform(45, 55) for _ in range(n)]))
        out = C.top(s, ms)
        self.assertEqual([(p["x"], p["y"], p["lag"]) for p in out], [("sleep_h", "hrv", 0)])
        self.assertNotIn("points", out[0])


class CoachPayload(unittest.TestCase):
    def test_derived_statistics_only(self):
        r = C.analyze(SLEEP, HRV, series(range(30)), series(range(30)), 0)
        p = C.coach_payload(r)
        self.assertEqual(set(p) - {"x_label", "y_label", "lag", "n", "rho", "ci", "strength", "confidence", "finding", "groups",
                                   "summary", "p_value", "lag_meaning", "thresholds"}, set())
        self.assertNotIn("points", json.dumps(p))


class Api(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.env = mock.patch.dict(os.environ, {"GARMIN_DEMO": "1", "ANTHROPIC_API_KEY": ""})
        cls.env.start()
        from fastapi.testclient import TestClient
        import server
        cls.server = server
        cls.patch = mock.patch.object(server, "CACHE", Path(tempfile.mkdtemp(prefix="coach-cache-")))
        cls.patch.start()
        cls.c = TestClient(server.app, base_url=server.LOCAL_URL, headers={"Origin": server.LOCAL_URL})

    @classmethod
    def tearDownClass(cls):
        cls.patch.stop()
        cls.env.stop()

    def setUp(self):
        store.checkins_delete_all()
        store.settings_put({"share_checkins": None})

    def test_options_and_compare(self):
        keys = {m["key"] for m in self.c.get("/api/insights/options").json()["measures"]}
        self.assertTrue({"sleep_h", "hrv", "load", "workout", "run_mi", "hours:tennis_v2", "checkin:energy"} <= keys)
        r = self.c.post("/api/insights/compare", json=dict(x="load", y="hrv", lag=1, range="90D")).json()
        self.assertEqual((r["status"], r["lag"]), ("ok", 1))
        self.assertGreaterEqual(r["n"], 85)
        self.assertEqual(self.c.post("/api/insights/compare", json=dict(x="sleep_h", y="nope")).status_code, 422)
        self.assertEqual(self.c.post("/api/insights/compare", json=dict(x="sleep_h", y="hrv", lag=4)).status_code, 422)

    def test_checkin_pairs_wait_for_21_days(self):
        today = date.today()
        for i in range(1, 21):
            store.checkin_put((today - timedelta(days=i)).isoformat(), ratings={"energy": 1 if i % 2 else -1})
        r = self.c.post("/api/insights/compare", json=dict(x="sleep_h", y="checkin:energy")).json()
        self.assertEqual((r["status"], r["n"]), ("not_enough", 20))

    def test_top_is_computed_once_a_day(self):
        a = self.c.get("/api/insights/top").json()
        with mock.patch.object(self.server.correlate, "top", side_effect=AssertionError("should be cached")):
            self.assertEqual(self.c.get("/api/insights/top").json(), a)

    def test_explain_respects_sharing_and_sends_only_statistics(self):
        today = date.today()
        for i in range(1, 40):
            store.checkin_put((today - timedelta(days=i)).isoformat(), ratings={"energy": 1 if i % 3 else -1}, note="very private")
        body = dict(x="sleep_h", y="checkin:energy")
        with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test"}), mock.patch.object(C, "explain", return_value="### ok") as ex:
            r = self.c.post("/api/insights/explain", json=body)
            self.assertEqual(r.status_code, 403)  # sharing is off
            ex.assert_not_called()
            store.settings_put({"share_checkins": True})
            r = self.c.post("/api/insights/explain", json=body)
            self.assertEqual((r.status_code, r.json()["text"]), (200, "### ok"))
            sent = C.coach_payload(ex.call_args.args[0])
            self.assertNotIn("very private", json.dumps(sent))
            self.assertNotIn("points", sent)
        self.assertEqual(self.c.post("/api/insights/explain", json=dict(x="sleep_h", y="hrv")).status_code, 409)  # no key


if __name__ == "__main__":
    unittest.main()
