"""Regression: "weight didn't load ... retries in a minute" never recovered.

Root cause (reproduced live 2026-10-29): Garmin's weight service intermittently answers 504 Gateway
Timeout (Cloudflare gives up after ~8 s) on cold long-range requests. The Body tab asked for 365 days in
one call, the client never retried a 5xx, nothing kept the last good weigh-ins, and an errored calendar
month was cached for the full 10 minutes. These tests pin each layer of the fix.

Run from the project folder:  .venv/bin/python -m unittest discover -s tests -v
Uses throwaway data/cache dirs; never calls Garmin.
"""
import os
import sys
import tempfile
import time
import types
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest import mock

import httpx

ROOT = Path(__file__).resolve().parent.parent
os.environ.setdefault("COACH_DATA_DIR", tempfile.mkdtemp(prefix="coach-test-"))
sys.path.insert(0, str(ROOT))

import body  # noqa: E402
import calendar_data  # noqa: E402
import garmin_client  # noqa: E402
from garmin_client import GarminClient, GarminError  # noqa: E402

TODAY = date(2026, 10, 29)


def summary(d: date, grams: float = 92949.0) -> dict:
    """The shape /weight-service/weight/range returns per day (weights in grams)."""
    return {"summaryDate": d.isoformat(), "numOfWeightEntries": 1, "latestWeight": {
        "calendarDate": d.isoformat(), "weight": grams, "bmi": 27.8, "bodyFat": 28.3, "bodyWater": 52.4,
        "boneMass": 5440, "muscleMass": 36729}}


class FakeWeightService:
    """Behaves like the live service on a bad day: ranges over `max_days` time out with 504."""

    def __init__(self, max_days=31, always_fail=()):
        self.max_days, self.always_fail, self.calls = max_days, set(always_fail), []

    def get(self, path, params=None):
        self.calls.append(path)
        s, e = (date.fromisoformat(x) for x in path.rsplit("/", 2)[1:])
        if (e - s).days + 1 > self.max_days or s in self.always_fail:
            raise GarminError(f"GET {path} -> 504: {{\"title\":\"Error 504: Gateway time-out\"}}", 504)
        days = [s + timedelta(days=i) for i in range((e - s).days + 1)]
        return {"dailyWeightSummaries": [summary(d) for d in reversed(days) if d.toordinal() % 2 == 0]}


class FetchWeightTest(unittest.TestCase):
    def test_a_year_loads_in_short_blocks_even_when_long_ranges_504(self):
        svc = FakeWeightService()
        start = TODAY - timedelta(days=365)
        raw, failed = body.fetch_weight(svc.get, start, TODAY, TODAY)
        self.assertEqual(failed, [])
        days = [r["summaryDate"] for r in raw["dailyWeightSummaries"]]
        expected = [(start + timedelta(days=i)).isoformat() for i in range(366) if (start + timedelta(days=i)).toordinal() % 2 == 0]
        self.assertEqual(sorted(days), expected)  # merged, nothing outside [start, end], no duplicates
        self.assertEqual(len(days), len(set(days)))
        for p in svc.calls:  # every request is a short one
            s, e = (date.fromisoformat(x) for x in p.rsplit("/", 2)[1:])
            self.assertLessEqual((e - s).days + 1, body.WEIGHT_BLOCK_DAYS)

    def test_grams_become_kilograms(self):
        raw, _ = body.fetch_weight(FakeWeightService().get, TODAY - timedelta(days=3), TODAY, TODAY)
        rows = body.weight_series(raw)
        self.assertTrue(rows)
        self.assertEqual(rows[-1]["kg"], 92.95)
        self.assertEqual(rows[-1]["muscle_kg"], 36.7)

    def test_settled_blocks_are_cached_so_later_loads_fetch_only_recent_ones(self):
        cache = {}
        get_ = lambda kind, key: cache.get((kind, key))
        put_ = lambda kind, key, obj: cache.__setitem__((kind, key), obj)
        start = TODAY - timedelta(days=365)
        body.fetch_weight(FakeWeightService().get, start, TODAY, TODAY, get_, put_)
        svc = FakeWeightService()
        raw, failed = body.fetch_weight(svc.get, start, TODAY, TODAY, get_, put_)
        self.assertEqual(failed, [])
        self.assertLessEqual(len(svc.calls), 2)  # only the block(s) still filling in
        self.assertGreater(len(raw["dailyWeightSummaries"]), 150)

    def test_a_block_that_keeps_failing_leaves_the_rest_and_names_its_range(self):
        bad = body.weight_blocks(TODAY - timedelta(days=100), TODAY)[1][0]
        raw, failed = body.fetch_weight(FakeWeightService(always_fail={bad}).get, TODAY - timedelta(days=100), TODAY, TODAY)
        self.assertEqual(len(failed), 1)
        self.assertEqual((failed[0]["start"], failed[0]["status"]), (bad.isoformat(), 504))
        self.assertTrue(raw["dailyWeightSummaries"])  # the other blocks still came through
        msg = body.weight_error(failed)
        self.assertIn(bad.isoformat(), msg)
        self.assertIn("504", msg)

    def test_network_errors_are_contained_too(self):
        def get(path, params=None):
            raise httpx.ConnectError("offline")
        raw, failed = body.fetch_weight(get, TODAY - timedelta(days=10), TODAY, TODAY)
        self.assertEqual(raw["dailyWeightSummaries"], [])
        self.assertTrue(failed)


class ClientRetryTest(unittest.TestCase):
    def client(self, answers):
        seen = []

        def handler(req):
            if req.url.path.endswith("/token"):
                return httpx.Response(200, text="tok-abc", headers={"content-type": "text/plain"})
            seen.append(req.url.path)
            status, body_ = answers.pop(0)
            return httpx.Response(status, json=body_)

        c = GarminClient(api_key="test-key-not-real")
        c._http = httpx.Client(transport=httpx.MockTransport(handler))
        return c, seen

    def test_one_504_is_retried_and_recovers(self):
        calls = []
        c, seen = self.client([(504, {"title": "Error 504"}), (200, {"dailyWeightSummaries": []})])
        c.on_call = lambda *a: calls.append(a)
        with mock.patch.object(garmin_client.time, "sleep") as slept:
            self.assertEqual(c.get("/weight-service/weight/range/2026-10-01/2026-10-29"), {"dailyWeightSummaries": []})
        self.assertEqual(len(seen), 2)
        slept.assert_called_once_with(garmin_client.TRANSIENT_WAIT)
        data_call = [a for a in calls if a[0].startswith("/weight")][0]
        self.assertEqual((data_call[2], data_call[5], data_call[6]), (200, None, ["504"]))  # status, error, retries

    def test_a_second_504_raises_with_the_status(self):
        c, seen = self.client([(504, {}), (504, {})])
        with mock.patch.object(garmin_client.time, "sleep"), self.assertRaises(GarminError) as cm:
            c.get("/weight-service/weight/range/2026-10-01/2026-10-29")
        self.assertEqual(cm.exception.status, 504)
        self.assertEqual(len(seen), 2)  # bounded: one retry only

    def test_a_400_is_not_retried(self):
        c, seen = self.client([(400, {})])
        with mock.patch.object(garmin_client.time, "sleep") as slept, self.assertRaises(GarminError):
            c.get("/weight-service/weight/all/2025-10-29/2026-10-29")
        self.assertEqual(len(seen), 1)
        slept.assert_not_called()

    def test_a_rejected_key_is_not_retried_by_every_fetch(self):
        tokens = []

        def handler(req):
            tokens.append(req.url.path)
            return httpx.Response(401, text="")
        c = GarminClient(api_key="test-key-not-real")
        c._http = httpx.Client(transport=httpx.MockTransport(handler))
        for _ in range(5):
            with self.assertRaises(GarminError) as cm:
                c.get("/weight-service/weight/range/2026-10-01/2026-10-29")
            self.assertEqual(cm.exception.status, 401)
        self.assertEqual(len(tokens), 1)  # one token request, then the refusal is reused
        with self.assertRaises(GarminError):
            c.check_token(force=True)  # "Run checks now" always asks again
        self.assertEqual(len(tokens), 2)

    def test_204_is_empty_not_an_error(self):
        c, _ = self.client([(204, None)])
        self.assertIsNone(c.get("/weight-service/weight/range/2026-10-28/2026-10-29"))


class LastGoodWeightTest(unittest.TestCase):
    def setUp(self):
        import server
        self.server = server
        self.saved = dict(server._body)

    def tearDown(self):
        self.server._body.clear()
        self.server._body.update(self.saved)

    def test_a_failed_refresh_keeps_the_last_good_weigh_ins(self):
        s = self.server
        good = {"dailyWeightSummaries": [summary(TODAY - timedelta(days=i)) for i in range(5)]}
        self.assertIs(s._with_last_good_weight(good, [], {}), good)
        errors = {}
        failed = [dict(start="2026-10-01", end="2026-10-29", status=504, error="GET -> 504")]
        raw = s._with_last_good_weight({"dailyWeightSummaries": []}, failed, errors)
        self.assertEqual(len(raw["dailyWeightSummaries"]), 5)
        self.assertIn("504", errors["weight"])
        self.assertIn("showing weigh-ins saved at", errors["weight"])


class CalendarErrorTtlTest(unittest.TestCase):
    def test_an_errored_current_month_is_retried_after_a_minute_not_ten(self):
        def get(path, params=None):
            if path.startswith("/weight-service"):
                raise GarminError(f"GET {path} -> 504: ", 504)
            return []
        c = types.SimpleNamespace(get=get, chunked=lambda fetch, s, e, days=28: GarminClient.chunked(None, fetch, s, e, days))
        cache = {}
        data = calendar_data.month(c, TODAY.year, TODAY.month, TODAY, lambda k, key: cache.get((k, key)),
                                   lambda k, key, obj: cache.__setitem__((k, key), obj))
        self.assertIn("weight", data["errors"])
        hit = cache[("calendar", f"{TODAY:%Y-%m}")]
        self.assertFalse(hit["settled"])
        self.assertLessEqual(hit["fetched"], time.time() - calendar_data.CACHE_TTL + 61)
        with mock.patch.object(calendar_data.time, "time", return_value=time.time() + 61):
            _, fresh = calendar_data.cached(f"{TODAY:%Y-%m}", lambda k, key: cache.get((k, key)))
        self.assertFalse(fresh)


if __name__ == "__main__":
    unittest.main()
