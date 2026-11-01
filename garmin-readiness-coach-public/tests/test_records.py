"""Personal records: best-effort sources (exact vs estimated), every record family, previous bests,
filters, badges, new-record detection, the bad-data filters on both sides of each threshold, the
incremental history sync (fake Garmin client) and the endpoints in demo mode.

Run from the project folder:  .venv/bin/python -m unittest discover -s tests -v
Uses a throwaway COACH_DATA_DIR and cache folder; never calls Garmin.
"""
import os
import sys
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
os.environ.setdefault("COACH_DATA_DIR", tempfile.mkdtemp(prefix="coach-test-"))
sys.path.insert(0, str(ROOT))

import records as R  # noqa: E402
import records_sync as S  # noqa: E402
import store  # noqa: E402
from garmin_client import GarminError  # noqa: E402

assert store.DATA_DIR != (ROOT / ".data").resolve(), "tests must never run against the real data folder"
TODAY = date(2026, 10, 28)
_ids = iter(range(1, 10**6))


def act(day, typ="running", dist=8000.0, secs=2700.0, splits=None, **kw):
    d = day if isinstance(day, str) else day.isoformat()
    a = dict(id=next(_ids), name="Morning run", type=typ, start=f"{d} 07:00:00", date=d, dist_m=dist, secs=secs,
             gain_m=None, load=None, hr=None, max_hr=None, speed_ms=dist / secs if dist else None, splits=splits or {})
    a.update(kw)
    return a


def rec(groups, key):
    return next((r for g in groups.values() for r in g if r["key"] == key), None)


def compute(acts, days=None, **kw):
    return R.compute(acts, days or {}, TODAY, **kw)


# ------------------------------- detection ----------------------------------- #
class BestEfforts(unittest.TestCase):
    def test_split_is_exact_and_whole_run_is_estimated(self):
        a = act("2026-05-01", dist=8000, secs=2600, splits={"5k": 1500.0})
        b = act("2026-06-01", dist=5200, secs=1500)  # no split data: 1500 * 5000/5200 = 1442.3 s
        r = rec(compute([a, b]), "5k")
        self.assertEqual((r["value"], r["source"], r["estimated"], r["activity_id"]), (1442.3, "whole", True, b["id"]))
        self.assertIn("whole run", r["note"])
        self.assertEqual((r["prev"]["value"], r["prev"]["activity_id"]), (1500.0, a["id"]))
        self.assertEqual(r["improvement"], dict(abs=57.7, pct=3.85))
        exact = rec(compute([a]), "5k")
        self.assertEqual((exact["source"], exact["estimated"]), ("split", False))

    def test_whole_run_estimate_limits(self):
        far = act("2026-06-01", dist=7600, secs=2100)  # more than 1.5x 5K: its average says little about a 5K
        self.assertIsNone(rec(compute([far]), "5k"))
        self.assertIsNotNone(rec(compute([act("2026-06-01", dist=7400, secs=2100)]), "5k"))
        has = act("2026-06-01", dist=5100, secs=1300, splits={"mile": 400.0})  # Garmin split data exists but no 5K split
        self.assertIsNone(rec(compute([has]), "5k"))
        short = act("2026-06-01", dist=4990, secs=1300)
        self.assertIsNone(rec(compute([short]), "5k"))

    def test_all_distances_and_half(self):
        a = act("2026-04-11", dist=22000, secs=7600, splits={"mile": 480.0, "5k": 1650.0, "10k": 3400.0, "half": 7300.0})
        g = compute([a])
        self.assertEqual([rec(g, k)["value"] for k in ("mile", "5k", "10k", "half")], [480.0, 1650.0, 3400.0, 7300.0])

    def test_indoor_runs_dont_set_running_records(self):
        g = compute([act("2026-06-01", typ="treadmill_running", dist=10000, secs=2400, splits={"5k": 1150.0})])
        self.assertEqual(g["running"], [])
        self.assertIsNotNone(rec(g, "longest"))  # still counts as an activity


class RunningRecords(unittest.TestCase):
    def test_longest_elevation_and_pace_threshold(self):
        fast_short = act("2026-06-01", dist=4800, secs=1300, gain_m=30)  # just under 3 miles: no pace record
        steady = act("2026-06-08", dist=4900, secs=1500, gain_m=80)
        longest = act("2026-06-15", dist=16000, secs=5600, gain_m=50)
        g = compute([fast_short, steady, longest])
        self.assertEqual(rec(g, "longest_run")["activity_id"], longest["id"])
        self.assertEqual(rec(g, "run_gain")["value"], 80)
        pace = rec(g, "run_pace")
        self.assertEqual(pace["activity_id"], steady["id"])  # 4,900 m > 3 miles; 4,800 m isn't
        self.assertAlmostEqual(pace["value"], 1500 / 4900, places=4)
        self.assertTrue(pace["lower"])


class AnyActivity(unittest.TestCase):
    def test_duration_load_and_hr_minimum(self):
        acts = [act("2026-06-01", typ="cycling", dist=40000, secs=5400, load=180.0, hr=140.0),
                act("2026-06-02", typ="strength_training", dist=0, secs=599, hr=175.0),  # too short for the HR record
                act("2026-06-03", typ="tennis_v2", dist=2000, secs=4000, load=120.0, hr=160.0)]
        g = compute(acts)
        self.assertEqual(rec(g, "longest")["value"], 5400)
        self.assertEqual(rec(g, "load")["value"], 180.0)
        self.assertEqual(rec(g, "hr")["value"], 160.0)


class WeeklyMonthly(unittest.TestCase):
    def test_week_across_the_year_boundary_and_months(self):
        acts = [act("2025-12-29", dist=10000), act("2026-01-02", dist=12000), act("2026-01-05", dist=15000),
                act("2026-01-20", typ="walking", dist=6000, secs=4000)]
        g = compute(acts)
        wk = rec(g, "week_distance")
        self.assertEqual((wk["value"], wk["date"], wk["end"]), (22000.0, "2025-12-29", "2026-01-04"))
        self.assertIsNone(wk["prev"])  # the first week is the best; the later 15 km week never beat it
        mo = rec(g, "month_distance")
        self.assertEqual((mo["value"], mo["date"]), (27000.0, "2026-01-01"))  # the walk isn't "miles"
        self.assertEqual(rec(g, "month_workouts")["value"], 2)  # walks aren't workouts

    def test_active_minutes_from_daily_data(self):
        days = {"2026-03-01": dict(im=30), "2026-03-02": dict(im=100), "2026-03-03": dict(im=50), "2026-03-10": dict(im=120)}
        g = compute([], days)
        self.assertEqual(rec(g, "week_im")["value"], 150)  # Sun 1 Mar is the tail of a week we only partly have
        self.assertEqual(rec(g, "month_im")["value"], 300)

    def test_a_partial_first_week_or_month_is_never_a_best(self):
        acts = [act("2026-01-20", dist=30000, secs=9000), act("2026-02-10", dist=5000, secs=1500)]
        mo = rec(compute(acts), "month_distance")
        self.assertEqual((mo["value"], mo["date"], mo["prev"]), (5000.0, "2026-02-01", None))  # January began before the history
        wk = rec(compute(acts), "week_distance")
        self.assertEqual(wk["date"], "2026-02-09")  # the history starts on Tue 20 Jan, so that week is partial

    def test_streaks_with_gaps_and_short_walks(self):
        d = lambda n: (date(2026, 3, 1) + timedelta(days=n)).isoformat()
        acts = [act(d(i)) for i in (0, 1, 2)] + [act(d(i)) for i in (5, 6, 7, 8)]
        acts += [act(d(9), typ="walking", dist=1500, secs=1100)]  # an 18-minute walk doesn't keep the streak
        acts += [act(d(10), typ="walking", dist=2500, secs=1300)]  # a 22-minute walk starts a new one
        r = rec(compute(acts), "streak")
        self.assertEqual((r["value"], r["date"], r["end"]), (4, d(5), d(8)))
        self.assertEqual((r["prev"]["value"], r["prev"]["date"]), (3, d(0)))
        self.assertEqual([s["value"] for s in R.streaks(acts)], [3, 4, 1])


class Health(unittest.TestCase):
    def test_best_and_lowest(self):
        days = {"2026-01-01": dict(steps=12000, hrv=60, sleep_score=80, rhr=50, weight_kg=81.0),
                "2026-02-01": dict(steps=15000, hrv=55, sleep_score=88, rhr=47, weight_kg=79.5),
                "2026-03-01": dict(steps=9000, hrv=66, sleep_score=70, rhr=49, weight_kg=80.0)}
        g = compute([], days)
        self.assertEqual([rec(g, k)["value"] for k in ("steps", "hrv", "sleep", "rhr", "weight")], [15000, 66, 88, 47, 79.5])
        self.assertEqual(rec(g, "rhr")["improvement"], dict(abs=3, pct=6.0))


class Filters(unittest.TestCase):
    def test_period_and_type(self):
        old = act("2025-06-01", dist=21000, secs=7000)
        this_year = act("2026-02-01", dist=15000, secs=5000)
        ride = act("2026-03-01", typ="road_biking", dist=60000, secs=7200)
        self.assertEqual(rec(compute([old, this_year]), "longest_run")["activity_id"], old["id"])
        self.assertEqual(rec(compute([old, this_year], period="year"), "longest_run")["activity_id"], this_year["id"])
        self.assertIsNone(rec(compute([old, this_year], period="year"), "longest_run")["prev"])  # previous best within the period only
        self.assertEqual(rec(compute([old, this_year], period="12m"), "longest_run")["activity_id"], this_year["id"])
        g = compute([old, this_year, ride], fam="ride")
        self.assertEqual(g["running"], [])
        self.assertEqual(rec(g, "longest")["activity_id"], ride["id"])
        self.assertEqual(rec(compute([old, this_year, ride], fam="run"), "longest")["activity_id"], old["id"])

    def test_families(self):
        for t, f in (("running", "run"), ("trail_running", "run"), ("road_biking", "ride"), ("lap_swimming", "swim"),
                     ("tennis_v2", "racket"), ("pickleball", "racket"), ("strength_training", "strength"), ("yoga", "other")):
            self.assertEqual(R.family(t), f, t)
        self.assertTrue(R.is_indoor_run("treadmill_running"))
        self.assertFalse(R.is_indoor_run("trail_running"))


class Badges(unittest.TestCase):
    def test_current_holders_and_new_window(self):
        old = act((TODAY - timedelta(days=60)).isoformat(), dist=16000, secs=5600)
        new = act((TODAY - timedelta(days=10)).isoformat(), dist=6000, secs=1600, splits={"5k": 1300.0})
        beaten = act((TODAY - timedelta(days=90)).isoformat(), dist=6000, secs=1800, splits={"5k": 1400.0})
        b = R.badges([old, new, beaten], {}, TODAY)
        self.assertTrue(b[str(new["id"])]["new"])
        self.assertIn("Fastest 5K", b[str(new["id"])]["records"])
        self.assertFalse(b[str(old["id"])]["new"])
        self.assertIn("Longest run", b[str(old["id"])]["records"])
        self.assertNotIn(str(beaten["id"]), b)  # its 5K was beaten, and it holds nothing else

    def test_new_records_only_after_first_sync_and_only_recent(self):
        base = [act("2026-06-01", dist=6000, secs=1800, splits={"5k": 1450.0})]
        before = R.compute(base, {}, TODAY)
        self.assertEqual(R.new_records(before, None, TODAY), [])  # first sync: nothing is "new"
        seen = R.seen_state(before)
        self.assertEqual(R.new_records(before, seen, TODAY), [])
        fresh = act((TODAY - timedelta(days=2)).isoformat(), dist=6000, secs=1700, splits={"5k": 1400.0})
        after = R.compute(base + [fresh], {}, TODAY)
        keys = {r["key"] for r in R.new_records(after, seen, TODAY)}
        self.assertIn("5k", keys)
        self.assertNotIn("longest_run", keys)  # same holder as before
        stale = act("2026-07-01", dist=6000, secs=1700, splits={"5k": 1400.0})  # a backfilled old activity
        self.assertEqual(R.new_records(R.compute(base + [stale], {}, TODAY), seen, TODAY), [])


class Progress(unittest.TestCase):
    def test_monthly_best_and_best_so_far(self):
        acts = [act("2026-01-05", splits={"5k": 1500.0}), act("2026-01-20", splits={"5k": 1480.0}),
                act("2026-02-10", splits={"5k": 1490.0}), act("2026-03-03", dist=5100, secs=1430)]
        p = R.progress(acts, TODAY)["5k"]
        self.assertEqual([(x["month"], x["value"], x["best"]) for x in p],
                         [("2026-01", 1480.0, 1480.0), ("2026-02", 1490.0, 1480.0), ("2026-03", 1402.0, 1402.0)])
        self.assertEqual([x["estimated"] for x in p], [False, False, True])


# ------------------------------ bad-data filters ------------------------------ #
class DataQuality(unittest.TestCase):
    def check(self, a, kept=True, issue=None):
        c, issues = R.clean_activity(a)
        self.assertEqual(c is not None, kept, issues)
        if issue:
            self.assertIn(issue, issues)
        else:
            self.assertEqual(issues, [])
        return c

    def test_world_record_floor(self):
        c = self.check(act("2026-06-01", dist=10000, secs=3000, splits={"5k": 754.0}), issue="effort_world_record")
        self.assertIsNone(c["splits"]["5k"])
        # 756 s is legal for a 5K inside a 5.2 km run averaging 5.9 m/s (just under the 6 m/s cap)
        self.assertEqual(self.check(act("2026-06-01", dist=5200, secs=880, splits={"5k": 756.0}))["splits"]["5k"], 756.0)

    def test_effort_spike_relative_to_the_run(self):
        # 10 km in 3000 s (3.33 m/s); a mile may be at most ~1.6x that pace
        self.check(act("2026-06-01", dist=10000, secs=3000, splits={"mile": 300.0}), issue="effort_spike")
        self.check(act("2026-06-01", dist=10000, secs=3000, splits={"mile": 310.0}))
        # a 5K inside a 5.2 km run can barely differ from the average
        self.check(act("2026-06-01", dist=5200, secs=1560, splits={"5k": 1300.0}), issue="effort_spike")
        self.check(act("2026-06-01", dist=5200, secs=1560, splits={"5k": 1420.0}))

    def test_effort_longer_than_the_run(self):
        self.check(act("2026-06-01", dist=4800, secs=1500, splits={"5k": 1500.0}), issue="effort_longer")

    def test_impossible_average_speeds(self):
        self.check(act("2026-06-01", dist=6100, secs=1000), kept=False, issue="impossible_pace")  # 6.1 m/s run
        self.check(act("2026-06-01", dist=5900, secs=1000))
        self.check(act("2026-06-01", typ="cycling", dist=26000, secs=1000), kept=False, issue="impossible_pace")
        self.check(act("2026-06-01", typ="cycling", dist=24000, secs=1000))
        self.check(act("2026-06-01", typ="walking", dist=3100, secs=1000), kept=False, issue="impossible_pace")
        self.check(act("2026-06-01", typ="walking", dist=2900, secs=1000))
        self.check(act("2026-06-01", typ="tennis_v2", dist=9000, secs=1000))  # no speed limit for court sports

    def test_duration_bounds(self):
        self.check(act("2026-06-01", dist=0, secs=59), kept=False, issue="too_short")
        self.check(act("2026-06-01", dist=0, secs=60))
        self.check(act("2026-06-01", dist=0, secs=86401), kept=False, issue="too_long")

    def test_field_level_issues_keep_the_activity(self):
        c = self.check(act("2026-06-01", dist=1000, secs=600, gain_m=260), issue="elevation")
        self.assertIsNone(c["gain_m"])
        self.assertEqual(self.check(act("2026-06-01", dist=1000, secs=600, gain_m=240))["gain_m"], 240)
        self.assertIsNone(self.check(act("2026-06-01", hr=29.0), issue="heart_rate")["hr"])
        self.assertIsNone(self.check(act("2026-06-01", hr=231.0), issue="heart_rate")["hr"])
        self.assertEqual(self.check(act("2026-06-01", hr=230.0))["hr"], 230.0)
        self.assertIsNone(self.check(act("2026-06-01", load=1501.0), issue="load")["load"])
        self.assertEqual(self.check(act("2026-06-01", load=1500.0))["load"], 1500.0)

    def test_daily_ranges(self):
        days, issues = R.clean_days({"2026-06-01": dict(steps=100001, hrv=9, rhr=29, sleep_score=101),
                                     "2026-06-02": dict(steps=100000, hrv=10, rhr=30, sleep_score=100)})
        self.assertEqual(days["2026-06-01"], {})
        self.assertEqual(days["2026-06-02"], dict(steps=100000, hrv=10, rhr=30, sleep_score=100))
        self.assertEqual(issues["day_value"], 4)

    def test_weight_outliers(self):
        w = {"2026-03-01": 80.0, "2026-03-05": 80.5, "2026-03-09": 79.8, "2026-03-12": 89.0, "2026-03-14": 87.0}
        days, issues = R.clean_days({k: dict(weight_kg=v) for k, v in w.items()})
        self.assertIsNone(days["2026-03-12"]["weight_kg"])  # 11% above the median
        self.assertEqual(days["2026-03-14"]["weight_kg"], 87.0)  # 8.7%: kept
        self.assertEqual(issues["weight_outlier"], 1)
        lone, _ = R.clean_days({"2026-01-01": dict(weight_kg=80.0), "2026-01-03": dict(weight_kg=95.0)})
        self.assertEqual(lone["2026-01-03"]["weight_kg"], 95.0)  # too few weigh-ins nearby to judge

    def test_clean_summarises_what_was_ignored(self):
        acts = [act("2026-06-01", dist=6100, secs=1000), act("2026-06-02", dist=6200, secs=1000), act("2026-06-03", hr=300.0)]
        good, _, ignored, _ = R.clean(acts, {})
        self.assertEqual(len(good), 1)  # the bad HR is cleared, the activity kept
        self.assertEqual({x["kind"]: x["count"] for x in ignored}, dict(impossible_pace=2, heart_rate=1))
        self.assertTrue(all(x["text"] for x in ignored))

    def test_glitches_never_become_records(self):
        real = act("2026-06-01", dist=8000, secs=2700, splits={"mile": 480.0, "5k": 1600.0})
        spike = act("2026-06-05", dist=8000, secs=2700, splits={"mile": 200.0, "5k": 1580.0})
        car = act("2026-06-06", typ="running", dist=40000, secs=1800)
        good, days, _, _ = R.clean([real, spike, car], {})
        g = R.compute(good, days, TODAY)
        self.assertEqual(rec(g, "mile")["activity_id"], real["id"])
        self.assertEqual(rec(g, "5k")["activity_id"], spike["id"])  # its 5K is fine, only the mile was a spike
        self.assertEqual(rec(g, "longest_run")["activity_id"], real["id"])


# ----------------------------------- sync ------------------------------------- #
def item(i, d, typ="running", **kw):
    return dict(activityId=i, activityName="Run", activityType=dict(typeKey=typ), startTimeLocal=f"{d} 07:00:00",
                distance=5000.0, movingDuration=1500.0, duration=1520.0, averageHR=150, activityTrainingLoad=80.0, **kw)


class FakeGarmin:
    def __init__(self, items):
        self.items, self.calls = items, []

    def get(self, path, params=None):
        self.calls.append((path, dict(params or {})))
        if path == S.LIST:
            return self.items[params["start"]:params["start"] + params["limit"]]
        raise AssertionError(path)


class Sync(unittest.TestCase):
    def history(self, n, newest=date(2026, 10, 27)):
        return [item(10_000 - k, (newest - timedelta(days=k // 2)).isoformat()) for k in range(n)]  # 2 a day, newest first

    def test_trim(self):
        t = S.trim(item(7, "2026-10-01", fastestSplit_1609=420.0, fastestSplit_5000=1400.0, elevationGain=40.0))
        self.assertEqual((t["id"], t["type"], t["date"], t["secs"], t["gain_m"]), (7, "running", "2026-10-01", 1500.0, 40.0))
        self.assertEqual(t["splits"], {"mile": 420.0, "5k": 1400.0})

    def test_first_sync_pages_through_everything(self):
        g = FakeGarmin(self.history(250))
        seen = []
        acts = S.sync_activities(g.get, None, seen.append)
        self.assertEqual(len(acts), 250)
        self.assertEqual([p["start"] for _, p in g.calls], [0, 100, 200])
        self.assertEqual(seen, [100, 200, 250])

    def test_later_syncs_only_fetch_new_pages_and_keep_the_rest(self):
        full = self.history(250)
        cached = S.sync_activities(FakeGarmin(full).get, None)
        newer = [item(20_000 + k, "2026-10-28") for k in range(3)]
        g = FakeGarmin(newer + full)
        acts = S.sync_activities(g.get, cached)
        self.assertEqual(len(g.calls), 1)  # page 1 already reaches days we have
        self.assertEqual(len(acts), 253)
        self.assertEqual([a["id"] for a in acts[:3]], [20_000, 20_001, 20_002])

    def test_deleted_activities_inside_the_overlap_drop_out(self):
        full = self.history(250)
        cached = S.sync_activities(FakeGarmin(full).get, None)
        gone = full[1]["activityId"]  # yesterday's run, deleted on Garmin
        acts = S.sync_activities(FakeGarmin([x for x in full if x["activityId"] != gone]).get, cached)
        self.assertNotIn(gone, {a["id"] for a in acts})
        self.assertEqual(len(acts), 249)

    def test_daily_health_and_failed_ranges(self):
        def get(path, params=None):
            if "dailysummaries" in path:
                return {"data": [dict(calendarDate="2026-10-01", movement=dict(steps=12000), heartRate=dict(restingValue=48),
                                      intensityMinutes=dict(moderate=20, vigorous=10))]}
            if "weight" in path:
                return {"dailyWeightSummaries": [dict(summaryDate="2026-10-02", latestWeight=dict(weight=80500))]}
            if "hrv" in path:
                if path.endswith("2026-10-28"):
                    raise GarminError(f"GET {path} -> 400: {{}}")
                return {"hrvSummaries": [dict(calendarDate="2026-10-01", lastNightAvg=61)]}
            return [dict(calendarDate="2026-10-01", sleepScores=dict(overall=dict(value=84)))]
        steps = []
        days, failed = S.sync_days(get, {}, date(2026, 9, 4), date(2026, 10, 28), lambda d, t: steps.append((d, t)))
        self.assertEqual(days["2026-10-01"], dict(steps=12000, rhr=48, im=40, hrv=61, sleep_score=84))
        self.assertEqual(days["2026-10-02"]["weight_kg"], 80.5)
        self.assertEqual([(f["kind"], f["start"], f["end"], f["status"]) for f in failed],
                         [("hrv", "2026-10-02", "2026-10-28", 400)])  # the second 28-day HRV range: reported, not retried in a loop
        self.assertEqual(steps[-1][0], steps[-1][1])

    def test_incremental_health_starts_at_the_last_synced_day(self):
        calls = []
        get = lambda path, params=None: calls.append((path, params)) or {}
        S.sync(get, dict(v=S.VERSION, activities=[S.trim(item(1, "2026-01-01"))], days={}, days_through="2026-10-20"), TODAY)
        starts = {p["startDate"] for path, p in calls if p and "startDate" in p and "dailysummaries" in path}
        self.assertEqual(starts, {"2026-10-17"})  # last synced day minus the 3-day overlap


# ------------------------------------ API -------------------------------------- #
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

    def test_records_filters_and_validation(self):
        r = self.c.get("/api/records").json()
        self.assertEqual(set(r["records"]), {"running", "activity", "weekly", "health"})
        self.assertTrue({x["kind"] for x in r["ignored"]} >= {"impossible_pace", "effort_world_record", "weight_outlier"})
        self.assertIn("run", {t["family"] for t in r["types"]})
        self.assertTrue(r["progress"]["5k"])
        ride = self.c.get("/api/records?type=strength&period=12m").json()
        self.assertEqual(ride["records"]["running"], [])
        self.assertEqual(self.c.get("/api/records?period=forever").status_code, 422)
        self.assertEqual(self.c.get("/api/records?type=jetski").status_code, 422)

    def test_badges_match_the_activities_list(self):
        b = self.c.get("/api/records/badges").json()["badges"]
        fresh = [k for k, v in b.items() if v["new"]]
        self.assertTrue(fresh)
        listed = {str(a["id"]) for a in self.c.get("/api/activities").json()["activities"]}
        self.assertTrue(set(fresh) <= listed)

    def test_new_records_toast_once(self):
        store.settings_put({"records_seen_demo": None})
        self.assertEqual(self.c.get("/api/records/new").json(), dict(new=[]))  # first time: everything marked seen
        seen = store.settings()["records_seen_demo"]
        store.settings_put({"records_seen_demo": dict(seen, **{"5k": "old:1500.0"})})
        new = self.c.get("/api/records/new").json()["new"]
        self.assertEqual([r["key"] for r in new], ["5k"])
        self.c.post("/api/records/seen")
        self.assertEqual(self.c.get("/api/records/new").json(), dict(new=[]))

    def test_sync_status_in_demo(self):
        s = self.c.post("/api/records/sync").json()
        self.assertIn(s["state"], ("running", "done"))
        self.assertGreater(s["activities"], 100)


if __name__ == "__main__":
    unittest.main()
