"""Check-in storage, validation, the 14-day insight minimum, mismatch flags, the care note and privacy.

Run from the project folder:  .venv/bin/python -m unittest discover -s tests -v
Uses a throwaway COACH_DATA_DIR, never the real .data/ folder.
"""
import io
import json
import os
import sys
import tempfile
import unittest
import zipfile
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
os.environ.setdefault("COACH_DATA_DIR", tempfile.mkdtemp(prefix="coach-test-"))
sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient  # noqa: E402

import checkins  # noqa: E402
import server  # noqa: E402
import store  # noqa: E402

assert store.DATA_DIR != (ROOT / ".data").resolve(), "tests must never run against the real data folder"
TODAY = date.today()
D = lambda n: (TODAY - timedelta(days=n)).isoformat()  # n days ago


def history(days=60, sleep=lambda i: 8.0, hrv=lambda i: 60.0):
    """Synthetic Garmin daily rows, oldest first (i = days ago)."""
    return [dict(date=D(i), sleep_h=sleep(i), hrv=hrv(i), rhr=50, bb_high=70, load=40 if i % 3 else 0)
            for i in range(days, -1, -1)]


class Base(unittest.TestCase):
    def setUp(self):
        store.checkins_delete_all()
        store.settings_put({"checkin_indicators": None, "care_dismissed": None, "share_checkins": None})


class Storage(Base):
    def test_partial_updates_merge_and_clear(self):
        store.checkin_put(D(1), ratings={"mood": 1})
        store.checkin_put(D(1), ratings={"energy": -1}, tags=["Sore"])
        store.checkin_put(D(1), ratings={"mood": None}, note="long day")
        self.assertEqual(store.checkin_get(D(1)) | {"updated": None},
                         dict(day=D(1), ratings={"energy": -1}, note="long day", tags=["Sore"], updated=None))

    def test_empty_day_is_removed(self):
        store.checkin_put(D(2), ratings={"mood": 0})
        self.assertIsNone(store.checkin_put(D(2), ratings={"mood": None}))
        self.assertIsNone(store.checkin_get(D(2)))

    def test_range_and_delete_all(self):
        for n in (1, 5, 9):
            store.checkin_put(D(n), ratings={"mood": 1})
        self.assertEqual([r["day"] for r in store.checkins(D(6), D(0))], [D(5), D(1)])
        self.assertEqual(store.checkins_delete_all(), 3)
        self.assertEqual(store.checkins(), [])


class Api(Base):
    @classmethod
    def setUpClass(cls):
        cls.c = TestClient(server.app, base_url=server.LOCAL_URL, headers={"Origin": server.LOCAL_URL})

    def put(self, day, **body):
        return self.c.put(f"/api/checkins/{day}", json=body)

    def test_saves_and_returns_the_day(self):
        j = self.put(D(0), ratings={"mood": 1, "stress": -1}, note="  felt\\n good  \nreally ", tags=["Sore"]).json()
        self.assertEqual(j["ratings"], {"mood": 1, "stress": -1})
        self.assertNotIn("\n", j["note"])  # one line
        self.assertEqual(j["tags"], ["Sore"])
        self.assertEqual(self.c.get(f"/api/checkins/{D(0)}").json()["ratings"]["mood"], 1)

    def test_validation(self):
        future = (TODAY + timedelta(days=1)).isoformat()
        for label, resp in {
            "future day": self.put(future, ratings={"mood": 1}),
            "unknown indicator": self.put(D(1), ratings={"nope": 1}),
            "rating out of range": self.put(D(1), ratings={"mood": 2}),
            "unknown tag": self.put(D(1), tags=["anything"]),
            "bad date": self.c.put("/api/checkins/2026-02-30", json={"ratings": {"mood": 1}}),
        }.items():
            with self.subTest(label):
                self.assertEqual(resp.status_code, 422)
        self.assertEqual(store.checkins(), [])

    def test_note_is_capped(self):
        self.assertEqual(len(self.put(D(1), note="x" * 900).json()["note"]), checkins.NOTE_MAX)

    def test_delete_all_needs_confirmation(self):
        self.put(D(1), ratings={"mood": 1})
        self.assertEqual(self.c.delete("/api/checkins").status_code, 422)
        self.assertEqual(self.c.delete("/api/checkins?confirm=true").json()["deleted"], 1)
        self.assertEqual(store.checkins(), [])

    def test_indicators_add_rename_disable(self):
        ids = [i["id"] for i in self.c.post("/api/checkins/indicators", json={"label": "Focus"}).json()["indicators"]]
        self.assertEqual(len(ids), 7)
        self.assertEqual(self.c.post("/api/checkins/indicators", json={"label": "focus"}).status_code, 422)  # duplicate
        inds = self.c.patch("/api/checkins/indicators/mood", json={"label": "Mental health", "enabled": False}).json()["indicators"]
        mood = next(i for i in inds if i["id"] == "mood")
        self.assertEqual((mood["label"], mood["enabled"]), ("Mental health", False))
        self.assertEqual(self.put(D(1), ratings={ids[-1]: 1}).status_code, 200)  # custom indicator can be rated

    def test_sharing_is_off_by_default(self):
        self.assertFalse(self.c.get("/api/settings").json()["share_checkins"])
        self.assertFalse(checkins.sharing())

    def test_export_includes_checkins(self):
        self.put(D(1), ratings={"mood": 1}, note="private note")
        z = zipfile.ZipFile(io.BytesIO(self.c.get("/api/export").content))
        meta = json.loads(z.read("metadata.json"))
        self.assertEqual(meta["checkins"][0]["note"], "private note")  # your own export keeps notes
        self.assertTrue(meta["checkin_indicators"])


class Insights(Base):
    def seed(self, n, start=0):
        """n check-in days; energy is up after 7h+ nights and down after short ones (strong pattern)."""
        for i in range(start, start + n):
            store.checkin_put(D(i), ratings={"energy": 1 if i % 2 == 0 else -1})

    def run_(self):
        return checkins.compute(store.checkins(), history(sleep=lambda i: 8.0 if i % 2 == 0 else 5.5),
                                checkins.indicators(), TODAY)

    def test_no_patterns_before_14_days(self):
        self.seed(13)
        out = self.run_()
        self.assertEqual((out["days"], out["ready"], out["patterns"]), (13, False, []))

    def test_patterns_from_14_days_phrased_as_tendencies(self):
        self.seed(14)
        out = self.run_()
        self.assertTrue(out["ready"])
        self.assertTrue(out["patterns"])
        sleep = next(p for p in out["patterns"] if "7+ hours of sleep" in p["text"])
        self.assertIn("tends to", sleep["text"])
        self.assertIn("more thumbs up", sleep["text"])
        for p in out["patterns"]:
            self.assertNotRegex(p["text"].lower(), r"\bcaus|because|leads to|results in")

    def test_weak_or_thin_data_gives_no_pattern(self):
        for i in range(20):  # energy unrelated to sleep
            store.checkin_put(D(i), ratings={"energy": 1 if i % 4 < 2 else -1})
        self.assertEqual(self.run_()["patterns"], [])

    def test_mismatch_low_energy_despite_good_sleep_and_hrv(self):
        for n in (0, 1, 2):
            store.checkin_put(D(n), ratings={"energy": -1})
        hist = history(hrv=lambda i: 70.0 if i < 3 else 55.0)
        out = checkins.compute(store.checkins(), hist, checkins.indicators(), TODAY)
        self.assertFalse(out["ready"])  # mismatches don't need 14 days
        self.assertEqual([(m["kind"], m["days"]) for m in out["mismatches"]], [("feels_worse", 3)])
        self.assertIn("rated energy low for 3 days", out["mismatches"][0]["text"])

    def test_no_mismatch_for_two_days_or_stale_streaks(self):
        for n in (0, 1):
            store.checkin_put(D(n), ratings={"energy": -1})
        self.assertEqual(checkins.compute(store.checkins(), history(), checkins.indicators(), TODAY)["mismatches"], [])
        store.checkins_delete_all()
        for n in (5, 6, 7):
            store.checkin_put(D(n), ratings={"energy": -1})
        self.assertEqual(checkins.compute(store.checkins(), history(), checkins.indicators(), TODAY)["mismatches"], [])


class Care(Base):
    def test_gentle_note_after_five_low_mood_days(self):
        for n in range(4):
            store.checkin_put(D(n), ratings={"mood": -1})
        self.assertIsNone(checkins.care(store.checkins(), TODAY))
        store.checkin_put(D(4), ratings={"mood": -1})
        note = checkins.care(store.checkins(), TODAY)
        self.assertEqual(note, dict(days=5, since=D(4)))
        store.settings_put({"care_dismissed": note["since"]})  # dismissed for this stretch
        self.assertIsNone(checkins.care(store.checkins(), TODAY))

    def test_a_gap_or_a_better_day_resets_the_streak(self):
        for n in (0, 1, 2, 4, 5, 6):  # day 3 missing
            store.checkin_put(D(n), ratings={"mood": -1})
        self.assertIsNone(checkins.care(store.checkins(), TODAY))
        store.checkin_put(D(3), ratings={"mood": 0})
        self.assertIsNone(checkins.care(store.checkins(), TODAY))


class Privacy(Base):
    def test_coach_payload_has_ratings_and_tags_but_never_notes(self):
        store.checkin_put(D(1), ratings={"mood": 1, "energy": -1}, note="SECRET-NOTE text", tags=["Traveling"])
        payload = checkins.coach_payload(store.checkins(), checkins.indicators(), TODAY, {"patterns": [], "mismatches": []})
        blob = json.dumps(payload)
        self.assertNotIn("SECRET-NOTE", blob)
        self.assertNotIn("note", json.dumps(payload["recent"]))
        self.assertEqual(payload["recent"][0]["ratings"], {"Mood": "good", "Energy": "poor"})
        self.assertEqual(payload["recent"][0]["tags"], ["Traveling"])


class Summary(Base):
    def test_month_counts_and_trend(self):
        this = TODAY.replace(day=1)
        last = (this - timedelta(days=1)).replace(day=1)
        for i in range(6):
            store.checkin_put((last + timedelta(days=i)).isoformat(), ratings={"mood": -1})
            store.checkin_put((this + timedelta(days=i)).isoformat() if this + timedelta(days=i) <= TODAY else D(0),
                              ratings={"mood": 1})
        mood = next(x for x in checkins.month_summary(this.isoformat()[:7])["indicators"] if x["id"] == "mood")
        self.assertEqual((mood["down"], mood["trend"]), (0, "up"))
        self.assertGreaterEqual(mood["up"], 1)


if __name__ == "__main__":
    unittest.main()
