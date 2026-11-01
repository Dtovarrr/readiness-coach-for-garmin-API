"""Regression: the Records 500 ("'<=' not supported between instances of 'int' and 'dict'").

Garmin's daily summaries nest steps as {"value", "goal", "distanceInMeters"}; the sync stored the whole
object and the range check crashed the whole Records request. These tests cover that exact shape, a
batch of activities with missing and strange values (skipped and counted, never fatal), readable API
errors with logged tracebacks (secrets redacted), and the health-range retry/backoff and failure detail.

Run from the project folder:  .venv/bin/python -m unittest discover -s tests -v
Uses a throwaway COACH_DATA_DIR and cache folder; never calls Garmin.
"""
import io
import json
import logging
import math
import os
import sys
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest import mock

import httpx

ROOT = Path(__file__).resolve().parent.parent
os.environ.setdefault("COACH_DATA_DIR", tempfile.mkdtemp(prefix="coach-test-"))
sys.path.insert(0, str(ROOT))

import applog  # noqa: E402
import records as R  # noqa: E402
import records_sync as S  # noqa: E402
import store  # noqa: E402
from garmin_client import GarminError  # noqa: E402

assert store.DATA_DIR != (ROOT / ".data").resolve(), "tests must never run against the real data folder"
TODAY = date(2026, 10, 28)
STEPS_OBJECT = {"goal": 10000, "value": 12345, "distanceInMeters": 9876.5}  # what Garmin actually sends


def run(i, d="2026-06-01", **kw):
    a = dict(id=i, name="Run", type="running", start=f"{d} 07:00:00", date=d, dist_m=8000.0, secs=2700.0,
             gain_m=40.0, load=90.0, hr=150.0, max_hr=175.0, speed_ms=2.96, splits={"5k": 1600.0})
    a.update(kw)
    return a


STRANGE = [
    run(1),                                                  # fine
    run(2, dist_m=None, speed_ms=None),                      # no distance (a manual entry)
    run(3, dist_m=0.0, splits={}),                           # zero distance
    run(4, secs=0.0),                                        # zero duration -> skipped
    run(5, secs=None),                                       # no duration -> skipped
    run(6, secs="1800", dist_m="5000"),                      # numbers as text -> treated as missing -> skipped (no duration)
    run(7, hr={"value": 151}, load={"value": 80}),           # nested numbers
    run(8, hr=float("nan"), gain_m=float("inf")),            # NaN / inf
    run(9, hr=True, load=[1, 2]),                            # bools and lists
    run(10, type=None),                                      # no type
    run(11, type=12345),                                     # a non-text type
    run(12, type="some_brand_new_sport_v3"),                 # an unknown type
    run(13, date="yesterday", start="yesterday 07:00"),      # a date that isn't one -> skipped
    run(14, date=None, start=None),                          # no date at all -> skipped
    run(15, date=None, start="2026-06-02T07:00:00.000"),     # ISO with a T: date taken from start
    run(16, splits="n/a"),                                   # splits not an object
    run(17, dist_m=12000.0, secs=4000.0, splits={"5k": "fast", "mile": None, "10k": {"value": 3300.0}}),
    run(18, dist_m=9e9, secs=3600.0),                        # GPS glitch: 9 million km -> ignored (impossible pace)
    run(19, dist_m=-500.0),                                  # negative distance
    None, "not an activity", 42,                             # not activities at all -> skipped
]


class StrangeActivities(unittest.TestCase):
    def test_nothing_crashes_and_the_unusable_are_skipped_with_reasons(self):
        good, days, ignored, skipped = R.clean(STRANGE, {})
        by_id = {s["id"]: s["reason"] for s in skipped}
        self.assertEqual(set(by_id) - {None}, {4, 5, 6, 13, 14})
        self.assertEqual(by_id[4], "no duration")
        self.assertIn("isn't a date", by_id[13])
        self.assertEqual(sum(1 for s in skipped if s["id"] is None), 3)  # None, the string and the int
        self.assertIn("impossible_pace", {x["kind"] for x in ignored})
        kept = {a["id"]: a for a in good}
        self.assertEqual((kept[7]["hr"], kept[7]["load"]), (151.0, 80.0))
        self.assertTrue(kept[8]["hr"] is None and kept[8]["gain_m"] is None)
        self.assertTrue(kept[9]["hr"] is None and kept[9]["load"] is None)
        self.assertEqual(kept[11]["type"], "12345")
        self.assertEqual(kept[15]["date"], "2026-06-02")
        self.assertEqual(kept[16]["splits"], {})
        self.assertEqual(kept[17]["splits"], {"10k": 3300.0})
        # and every record, filter, progress and badge computes on what's left
        for period in R.PERIODS:
            for fam in [None, *R.FAMILIES]:
                R.compute(good, days, TODAY, period, fam)
        R.progress(good, TODAY)
        R.badges(good, days, TODAY)

    def test_an_unexpected_failure_skips_one_activity_not_all(self):
        real = R.clean_activity
        boom = lambda a: (_ for _ in ()).throw(ZeroDivisionError("x")) if a["id"] == 2 else real(a)
        with mock.patch.object(R, "clean_activity", side_effect=boom), self.assertLogs("garmin_coach", "WARNING"):
            good, _, _, skipped = R.clean([run(1), run(2), run(3)], {})
        self.assertEqual([a["id"] for a in good], [1, 3])
        self.assertEqual(skipped[0]["reason"], "couldn't be processed (ZeroDivisionError)")


class StrangeDays(unittest.TestCase):
    def test_the_steps_object_that_caused_the_500(self):
        days, issues = R.clean_days({"2026-10-01": dict(steps=STEPS_OBJECT, rhr=48, im=40)})
        self.assertEqual(days["2026-10-01"], dict(steps=12345.0, rhr=48.0, im=40.0))
        self.assertEqual(sum(issues.values()), 0)
        g = R.compute([], days, TODAY)
        self.assertEqual(next(r for r in g["health"] if r["key"] == "steps")["value"], 12345.0)

    def test_unreadable_values_and_dates(self):
        days, issues = R.clean_days({"2026-10-01": dict(steps="lots", hrv=[60], rhr=float("nan"), sleep_score={"goal": 80}),
                                     "2026-10-02": None, "not-a-date": dict(steps=5000)})
        self.assertEqual(days["2026-10-01"], {})
        self.assertNotIn("not-a-date", days)
        self.assertEqual(issues["day_unreadable"], 5)


class SyncParsing(unittest.TestCase):
    def test_summary_unwraps_steps(self):
        out = {}
        put = lambda k, **f: out.setdefault(k, {}).update({x: v for x, v in f.items() if v is not None})
        S.parse("summary", {"data": [dict(calendarDate="2026-10-01", movement=dict(steps=STEPS_OBJECT),
                                          heartRate=dict(restingValue=47), intensityMinutes=dict(moderate=10, vigorous=5))]},
                date(2026, 10, 1), date(2026, 10, 1), put)
        self.assertEqual(out["2026-10-01"], dict(steps=12345.0, rhr=47.0, im=20.0))

    def test_old_caches_are_repaired_in_place(self):
        h = dict(v=S.VERSION, activities=[], days={"2026-10-01": dict(steps=STEPS_OBJECT)}, failed=1)
        S.upgrade(h)
        self.assertEqual(h["days"]["2026-10-01"]["steps"], 12345.0)
        self.assertEqual((h["failed"], h["failed_unknown"]), ([], 1))


class Retry(unittest.TestCase):
    def test_backoff_on_rate_limits_server_errors_and_timeouts(self):
        answers = [GarminError("GET x -> 429: {}"), GarminError("GET x -> 503: "), httpx.ReadTimeout("slow"), {"data": []}]
        waits = []
        get = lambda *a: (lambda x: (_ for _ in ()).throw(x) if isinstance(x, Exception) else x)(answers.pop(0))
        self.assertEqual(S.fetch(get, "summary", date(2026, 1, 1), date(2026, 1, 28), waits.append), {"data": []})
        self.assertEqual(waits, list(S.RETRY_WAITS))

    def test_a_rejected_request_is_not_retried(self):
        waits, calls = [], []
        def get(*a):
            calls.append(a)
            raise GarminError("GET x -> 400: {}")
        with self.assertRaises(GarminError):
            S.fetch(get, "hrv", date(2026, 1, 1), date(2026, 1, 28), waits.append)
        self.assertEqual((len(calls), waits), (1, []))

    def test_failures_name_the_endpoint_range_and_reason(self):
        def get(path, params=None):
            if path.startswith(S.ENDPOINTS["hrv"]):
                raise GarminError(f"GET {path} -> 400: {{}}")
            return {}
        _, failed = S.sync_days(get, {}, date(2026, 10, 1), date(2026, 10, 20), sleep=lambda s: None)
        self.assertEqual(failed, [dict(kind="hrv", label="HRV", endpoint=S.ENDPOINTS["hrv"], start="2026-10-01", end="2026-10-20",
                                       status=400, reason="Garmin rejected the request (400)",
                                       detail="GET /hrv-service/hrv/daily/2026-10-01/2026-10-20 -> 400: {}")])

    def test_failed_ranges_are_retried_on_the_next_sync(self):
        seen = []
        get = lambda path, params=None: seen.append((path, params)) or {}
        prev = [dict(kind="summary", start="2025-10-28", end="2026-10-27")]
        _, failed = S.sync_days(get, {}, TODAY, TODAY, retry=prev, sleep=lambda s: None)
        self.assertEqual(failed, [])
        self.assertIn((S.ENDPOINTS["summary"], {"startDate": "2025-10-28", "endDate": "2026-10-27"}), seen)


class Split(unittest.TestCase):
    """What Garmin did here: the full-year daily-summary range 2025-10-28..2026-10-27 answered 500 every
    time (fast, empty body) while both halves loaded fine."""
    def fake(self, calls):
        def get(path, params=None):
            calls.append(params and (params["startDate"], params["endDate"]))
            if path == S.ENDPOINTS["summary"]:
                s, e = date.fromisoformat(params["startDate"]), date.fromisoformat(params["endDate"])
                if (e - s).days + 1 > 300:
                    raise GarminError(f"GET {path} -> 500: ")
                return {"data": [dict(calendarDate=(s + timedelta(days=i)).isoformat(), movement=dict(steps=STEPS_OBJECT))
                                 for i in range((e - s).days + 1)]}
            return {}
        return get

    def test_a_5xx_on_a_long_range_is_split_not_retried(self):
        calls, waits = [], []
        failed_year = [dict(kind="summary", start="2025-10-28", end="2026-10-27")]
        days, failed = S.sync_days(self.fake(calls), {}, TODAY, TODAY, retry=failed_year, sleep=waits.append)
        self.assertEqual(failed, [])
        self.assertEqual(waits, [])  # no pointless waiting on an error that repeats
        self.assertIn(("2025-10-28", "2026-04-27"), calls)
        self.assertIn(("2026-04-28", "2026-10-27"), calls)
        self.assertEqual(sum(1 for k, d in days.items() if "2025-10-28" <= k <= "2026-10-27" and d.get("steps") == 12345.0), 365)

    def test_a_5xx_on_a_short_range_is_retried_with_backoff_then_reported(self):
        waits = []
        def get(path, params=None):
            raise GarminError(f"GET {path} -> 503: ")
        _, failed = S.sync_days(get, {}, date(2026, 10, 1), date(2026, 10, 20), sleep=waits.append)
        self.assertEqual(waits, list(S.RETRY_WAITS) * len(failed))
        self.assertEqual({f["reason"] for f in failed}, {"Garmin server error 503"})

    def test_new_syncs_ask_for_half_years(self):
        calls = []
        S.sync_days(self.fake(calls), {}, date(2025, 10, 28), date(2026, 10, 27), sleep=lambda s: None)
        self.assertTrue(all((date.fromisoformat(e) - date.fromisoformat(s)).days + 1 <= S.SUMMARY_DAYS for s, e in filter(None, calls)))

    def test_a_lost_range_from_an_old_sync_is_refilled_once(self):
        calls = []
        old = dict(v=S.VERSION, activities=[S.trim(dict(activityId=1, activityType=dict(typeKey="running"), startTimeLocal="2025-01-01 07:00:00",
                                                        distance=5000.0, movingDuration=1500.0))],
                   days={}, days_through="2026-10-27", failed=1)
        get = self.fake(calls)
        h = S.sync(lambda p, q=None: [] if p == S.LIST else get(p, q), old, TODAY)
        self.assertEqual(h["failed"], [])
        self.assertEqual(sum(1 for k in h["days"] if "2025-10-28" <= k <= "2026-10-27"), 365)  # the missing year is back
        calls.clear()
        S.sync(lambda p, q=None: [] if p == S.LIST else get(p, q), h, TODAY)
        self.assertFalse([c for c in calls if c and c[0] < "2026-10-01"])  # and isn't fetched again


class Logging(unittest.TestCase):
    def test_keys_and_tokens_never_reach_the_log(self):
        with mock.patch.dict(os.environ, {"GARMIN_API_KEY": "gk_SECRET_123456789", "ANTHROPIC_API_KEY": "sk-ant-SECRET987654321"}):
            buf = io.StringIO()
            h = logging.StreamHandler(buf)
            h.addFilter(applog.Redact())
            lg = logging.getLogger("redact-test")
            lg.addHandler(h)
            try:
                raise RuntimeError("auth failed for gk_SECRET_123456789 with Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.abc.def")
            except RuntimeError as ex:
                lg.error("call with x-api-key: sk-ant-SECRET987654321 failed", exc_info=ex)
            out = buf.getvalue()
        for secret in ("gk_SECRET_123456789", "sk-ant-SECRET987654321", "eyJhbGciOiJIUzI1NiJ9"):
            self.assertNotIn(secret, out)
        self.assertIn("RuntimeError", out)  # the traceback itself is still there
        self.assertIn("[GARMIN_API_KEY redacted]", out)


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
        cls.c = TestClient(server.app, base_url=server.LOCAL_URL, headers={"Origin": server.LOCAL_URL}, raise_server_exceptions=False)

    @classmethod
    def tearDownClass(cls):
        cls.patch.stop()
        cls.env.stop()

    def setUp(self):
        self.server._rec_mem.update(key=None, clean=None, all=None)

    def history(self, acts, days):
        return dict(v=S.VERSION, activities=acts, days=days, days_through=TODAY.isoformat(), synced=1.0, failed=[])

    def test_the_reported_crash_now_loads(self):
        days = {(TODAY - timedelta(days=i)).isoformat(): dict(steps=dict(STEPS_OBJECT, value=9000 + i), rhr=50) for i in range(30)}
        with mock.patch.object(self.server, "records_history", return_value=self.history(STRANGE, days)):
            r = self.c.get("/api/records")
            self.assertEqual(r.status_code, 200, r.text)
            j = r.json()
            self.assertEqual(next(x for x in j["records"]["health"] if x["key"] == "steps")["value"], 9029.0)
            self.assertEqual(len(j["skipped"]), 8)
            self.assertEqual(j["status"]["skipped"], 8)
            self.assertTrue(all(s["reason"] for s in j["skipped"]))

    def test_a_failure_returns_a_readable_message_and_logs_the_traceback(self):
        with mock.patch.object(R, "compute", side_effect=TypeError("bad compare")), self.assertLogs("garmin_coach", "ERROR") as logs:
            r = self.c.get("/api/records")
        self.assertEqual(r.status_code, 500)
        self.assertIn("TypeError", r.json()["detail"])
        self.assertIn("logs/readiness_coach.log", r.json()["detail"])
        self.assertIn("Traceback", "\n".join(logs.output))

    def test_any_other_endpoint_gets_a_readable_500_too(self):
        with mock.patch.object(self.server.records, "badges", side_effect=KeyError("boom")), self.assertLogs("garmin_coach", "ERROR"):
            r = self.c.get("/api/records/badges")
        self.assertEqual(r.status_code, 500)
        j = r.json()
        self.assertEqual(j["error"], "KeyError")
        self.assertIn("server.py", j["where"])
        self.assertNotIn("Internal Server Error", r.text)


if __name__ == "__main__":
    unittest.main()
