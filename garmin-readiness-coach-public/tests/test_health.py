"""System status logic: error classification, stale detection, anomalies, the overall colour, the
rolling event log and the diagnostics redaction (no keys, tokens, names, birth dates or coordinates).

Run from the project folder:  .venv/bin/python -m unittest discover -s tests -v
Uses throwaway data/cache dirs; never calls Garmin or Anthropic.
"""
import os
import sys
import tempfile
import time
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest import mock

import httpx
import httpx2

ROOT = Path(__file__).resolve().parent.parent
os.environ.setdefault("COACH_DATA_DIR", tempfile.mkdtemp(prefix="coach-test-"))
sys.path.insert(0, str(ROOT))

import health  # noqa: E402
from garmin_client import TOKEN_PATH, GarminError  # noqa: E402

# Every test module is imported before any test runs, so this keeps the whole suite's status events
# (e.g. the export tests' "Backup" events) out of the real .cache/health log.
import server  # noqa: E402,F401
LOG_DIR = Path(tempfile.mkdtemp(prefix="coach-health-"))
health.init(LOG_DIR)

TODAY = date(2026, 10, 29)
W = "/weight-service/weight/range/2026-10-01/2026-10-29"


def view(**kw):
    return health.view(key_set=kw.get("key_set", True), demo=kw.get("demo", False), token_expires=None,
                       ai_key_set=kw.get("ai", False))


class Base(unittest.TestCase):
    def setUp(self):
        health.reset()
        health.init(LOG_DIR)


class ClassifyTest(Base):
    def test_garmin_errors_in_plain_words(self):
        self.assertEqual(health.classify(401, token=True)["kind"], "key_rejected")
        self.assertEqual(health.classify(403, token=True)["kind"], "key_rejected")
        self.assertEqual(health.classify(401)["kind"], "unauthorized")
        self.assertEqual(health.classify(429)["kind"], "rate_limited")
        self.assertEqual(health.classify(504)["kind"], "gateway_timeout")
        self.assertEqual(health.classify(500)["kind"], "server_error")
        self.assertEqual(health.classify(404)["kind"], "not_found")
        self.assertEqual(health.classify(400)["kind"], "bad_request")
        self.assertEqual(health.classify(ex=httpx.ReadTimeout("slow"))["kind"], "timeout")
        self.assertEqual(health.classify(ex=httpx.ConnectError("down"))["kind"], "network")
        self.assertEqual(health.classify(ex=GarminError("GARMIN_API_KEY is not set (see .env.example)."))["kind"], "no_key")
        self.assertEqual(health.classify(ex=GarminError("GET /x -> 503: "))["kind"], "server_error")  # status from the message

    def test_every_classification_says_what_to_try(self):
        for c in [health.classify(s) for s in (400, 401, 404, 429, 500, 504)] + [health.classify(401, token=True)]:
            self.assertTrue(c["title"] and c["explain"] and c["fix"], c)
        self.assertIn(".env", health.classify(401, token=True)["fix"])

    def test_anthropic_errors(self):
        C = health.classify_ai
        self.assertEqual(C(401, '{"error":{"type":"authentication_error"}}')["kind"], "invalid_key")
        self.assertEqual(C(402, '{"error":{"type":"billing_error"}}')["kind"], "billing")
        self.assertEqual(C(400, '{"error":{"type":"invalid_request_error","message":"Your credit balance is too low"}}')["kind"], "billing")
        self.assertEqual(C(404, '{"error":{"type":"not_found_error","message":"model: x"}}')["kind"], "model_not_found")
        self.assertEqual(C(429, "")["kind"], "rate_limited")
        self.assertEqual(C(529, '{"error":{"type":"overloaded_error"}}')["kind"], "overloaded")
        self.assertEqual(C(None, "", httpx.ConnectError("x"))["kind"], "network")
        self.assertEqual(C(None, "", httpx.ReadTimeout("x"))["kind"], "timeout")


class StaleTest(Base):
    def test_daily_feeds_go_stale_after_two_days(self):
        self.assertIsNone(health.stale("sleep", (TODAY - timedelta(days=2)).isoformat(), TODAY))
        self.assertIn("3 days", health.stale("sleep", (TODAY - timedelta(days=3)).isoformat(), TODAY))
        self.assertIsNone(health.stale("sleep", None, TODAY))

    def test_weight_follows_your_own_cadence(self):
        daily = [(TODAY - timedelta(days=d)).isoformat() for d in range(20, 5, -1)]  # every day, last one 6 days ago
        self.assertIn("6 days", health.stale("weight", daily[-1], TODAY, daily))
        weekly = [(TODAY - timedelta(days=d)).isoformat() for d in (40, 33, 26, 19, 12)]  # every 7 days, last 12 days ago
        self.assertIsNone(health.stale("weight", weekly[-1], TODAY, weekly))
        self.assertIsNone(health.stale("weight", daily[-1], TODAY, daily[-2:]))  # too few to know a cadence

    def test_activities_are_never_stale(self):
        self.assertIsNone(health.stale("activities", "2026-01-01", TODAY))


class AnomalyTest(Base):
    def test_zero_impossible_and_jumps(self):
        y, t = (TODAY - timedelta(days=1)).isoformat(), TODAY.isoformat()
        self.assertTrue(any("Zero" in a for a in health.anomalies("steps", [(y, 0), (t, 0)], TODAY)))
        self.assertEqual(health.anomalies("steps", [(t, 0)], TODAY), [])  # today is still filling in
        self.assertTrue(any("plausible" in a for a in health.anomalies("hrv", [(y, 400)], TODAY)))
        jump = health.anomalies("weight", [("2026-10-26", 90.0), ("2026-10-27", 95.5)], TODAY)
        self.assertTrue(any("jumped" in a for a in jump))
        self.assertEqual(health.anomalies("weight", [("2026-10-26", 90.0), ("2026-10-27", 90.4)], TODAY), [])

    def test_an_empty_daily_feed_is_flagged(self):
        found = health.evaluate("sleep", [], TODAY, TODAY - timedelta(days=6))
        self.assertTrue(any("Nothing came back" in a for a in found["anomalies"]))
        self.assertEqual(found["records"], 0)

    def test_weight_probe_evaluates_values_and_freshness(self):
        rows = [{"summaryDate": (TODAY - timedelta(days=d)).isoformat(), "latestWeight": {"weight": 92000.0 + d}}
                for d in range(4, 30)]
        found = health.evaluate("weight", {"dailyWeightSummaries": rows}, TODAY, TODAY - timedelta(days=89))
        self.assertEqual(found["records"], 26)
        self.assertEqual(found["latest"], (TODAY - timedelta(days=4)).isoformat())
        self.assertIn("4 days", found["stale"])  # weighs in daily, so 3 quiet days are allowed but not 4


class RecordingTest(Base):
    def test_failures_retries_and_recovery(self):
        err = GarminError(f"GET {W} -> 504: gateway", 504)
        for _ in range(3):
            health.record_call(W, None, 504, 8100, None, err, ["504"])
        v = view()
        f = next(x for x in v["feeds"] if x["name"] == "weight")
        self.assertEqual((f["status"], f["failures"], f["error"]["kind"], f["range"]), ("error", 3, "gateway_timeout", "2026-10-01 → 2026-10-29"))
        self.assertEqual(v["overall"], "red")  # repeated failures
        self.assertTrue(any("3 times in a row" in a for a in f["anomalies"]))
        health.record_call(W, None, 200, 120, {"dailyWeightSummaries": [{}, {}]}, None, ["504"])
        v = view()
        f = next(x for x in v["feeds"] if x["name"] == "weight")
        self.assertEqual((f["status"], f["records"], f["failures"]), ("ok", 2, 0))
        msgs = [e["message"] for e in health.events()]
        self.assertIn("Loading again", msgs)
        self.assertTrue(any(m.startswith("Retried after 504") for m in msgs))
        self.assertTrue(health.events("errors"))
        self.assertTrue(all(e["level"] == "warning" for e in health.events("warnings")))

    def test_token_rejected_is_red_with_a_fix(self):
        health.record_call(TOKEN_PATH, None, 401, 90, None, GarminError("Token request failed (401).", 401), [])
        v = view()
        self.assertEqual(v["overall"], "red")
        self.assertEqual(v["garmin"]["error"]["kind"], "key_rejected")
        self.assertIn("GARMIN_API_KEY", v["garmin"]["error"]["fix"])

    def test_unrelated_paths_are_ignored(self):
        health.record_call("/download-service/files/activity/1", None, 200, 10, None, None, [])
        self.assertTrue(all(f["last_attempt"] is None for f in view()["feeds"]))


class OverallTest(Base):
    def test_colours(self):
        health.record_call(TOKEN_PATH, None, 200, 50, None, None, [])
        self.assertEqual(view()["overall"], "green")
        self.assertEqual(view(demo=True, key_set=False)["overall"], "amber")
        health.record_call("/sleep-service/sleep/dailySleepSummariesByDate", {"startDate": "2026-10-23", "endDate": "2026-10-29"},
                           429, 30, None, GarminError("x -> 429", 429), [])
        self.assertEqual(view()["overall"], "amber")

    def test_missing_key_is_reported(self):
        v = view(key_set=False)
        self.assertEqual(v["garmin"]["error"]["kind"], "no_key")


class EventLogTest(Base):
    def test_rolls_at_100_and_collapses_repeats(self):
        for i in range(130):
            health.event("info", "Test", f"event {i}", now=1000 + i)
        evs = health.events()
        self.assertEqual(len(evs), 100)
        self.assertEqual(evs[0]["message"], "event 129")  # newest first
        health.event("error", "Test", "same", now=5000)
        health.event("error", "Test", "same", now=5010)
        self.assertEqual(health.events()[0]["count"], 2)
        self.assertEqual(len(health.events()), 100)

    def test_persists_to_the_cache_dir(self):
        health.event("warning", "Test", "kept")
        health.reset()
        health.init(LOG_DIR)
        self.assertEqual(health.events()[0]["message"], "kept")


class RedactionTest(Base):
    def test_report_leaks_no_secrets_or_personal_data(self):
        key, akey = "gk_live_ABCDEFGH12345678", "sk-ant-api03-SECRETSECRETSECRET"
        jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTYifQ.c2lnbmF0dXJlc2lnbmF0dXJl"
        body = (f'Authorization: Bearer {jwt} key={key} {{"displayName":"Jane Runner","birthDate":"1990-04-02",'
                f'"email":"jane@example.com","startLatitude":40.712812,"startLongitude":-74.006012}} at 40.712812, -74.006012 '
                f"ApiKey {key} x-api-key: {akey} date of birth 1990-04-02")
        with mock.patch.dict(os.environ, {"GARMIN_API_KEY": key, "ANTHROPIC_API_KEY": akey}):
            health.record_call(W, None, 400, 50, None, GarminError(f"GET {W} -> 400: {body}", 400), [])
            health.event("error", "Test", body)
            text = health.report(view())
        for secret in (key, akey, jwt, "Jane", "jane@example.com", "1990-04-02", "40.712812", "74.006012"):
            self.assertNotIn(secret, text, secret)
        self.assertIn("Weight & body composition", text)  # the useful parts survive

    def test_scrub_drops_identifying_keys(self):
        out = health.scrub({"displayName": "x", "birthDate": "y", "lat": 1.5, "nested": [{"longitude": 2, "ok": "fine"}], "token": "t"})
        self.assertEqual(out, {"nested": [{"ok": "fine"}]})


class AiTestTest(Base):
    def test_no_key_never_calls_out(self):
        ping = mock.Mock()
        res = health.test_ai(ping, mock.Mock(), key_set=False, model="claude-haiku-5-5")
        ping.assert_not_called()
        self.assertEqual(res["error"]["kind"], "no_key")

    def test_success_and_failure(self):
        import ai
        res = health.test_ai(mock.Mock(return_value=dict(model="claude-haiku-5-5", ms=120)), ai.classify, True, "claude-haiku-5-5")
        self.assertTrue(res["ok"])
        self.assertEqual(res["ms"], 120)
        import anthropic
        req = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
        err = anthropic.AuthenticationError("invalid x-api-key sk-ant-test-0000000000", response=httpx2.Response(401, request=req),
                                            body={"error": {"type": "authentication_error", "message": "invalid x-api-key"}})
        res = health.test_ai(mock.Mock(side_effect=err), ai.classify, True, "claude-haiku-5-5")
        self.assertEqual(res["error"]["kind"], "invalid_key")
        self.assertNotIn("sk-ant-test", str(res))


class LocalChecksTest(Base):
    def test_db_disk_and_backup(self):
        import sqlite3
        d = Path(tempfile.mkdtemp(prefix="coach-local-"))
        db = d / "x.db"
        sqlite3.connect(db).close()
        (d / "garmin-coach-export-20261029.zip").write_bytes(b"zip")
        out = health.local_checks(d, db, lambda: sqlite3.connect(db), [d], "127.0.0.1", 8765)
        self.assertTrue(out["db"]["ok"])
        self.assertGreater(out["disk"]["free"], 0)
        self.assertIsNotNone(out["backup"]["last"])
        self.assertEqual(out["server"]["listen"], "127.0.0.1:8765")
        broken = health.local_checks(d, db, mock.Mock(side_effect=sqlite3.OperationalError("locked")), [], "127.0.0.1", 8765)
        self.assertFalse(broken["db"]["ok"])
        self.assertIsNone(broken["backup"]["last"])


if __name__ == "__main__":
    unittest.main()
