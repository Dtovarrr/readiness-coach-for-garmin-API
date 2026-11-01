"""Weekly Review: Monday-Sunday week boundaries, week-over-week comparisons, the 4-week load average,
records, coach privacy, and the review endpoints (demo data, caching, auto-generation, export).

Run from the project folder:  .venv/bin/python -m unittest discover -s tests -v
Uses a throwaway COACH_DATA_DIR and cache folder, never the real .data/ or .cache/, and never calls Claude.
"""
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
os.environ.setdefault("COACH_DATA_DIR", tempfile.mkdtemp(prefix="coach-test-"))
sys.path.insert(0, str(ROOT))

import review  # noqa: E402
import store  # noqa: E402
import units  # noqa: E402

assert store.DATA_DIR != (ROOT / ".data").resolve(), "tests must never run against the real data folder"
MON = date(2026, 10, 19)  # a Monday
INDS = [dict(id="mood", label="Mood", enabled=True), dict(id="energy", label="Energy", enabled=True),
        dict(id="focus", label="Focus", enabled=False)]


def D(d: date) -> str:
    return d.isoformat()


def rows(start: date, end: date, **per_day):
    """Loaded day rows; per_day values are callables of the date (None = no reading)."""
    out, d = {}, start
    while d <= end:
        out[D(d)] = dict(date=D(d), activities=[], **{k: f(d) for k, f in per_day.items()})
        d += timedelta(days=1)
    return out


def act(typ, dist_m=0, secs=3600, load=50, name="Morning session"):
    return dict(type=typ, dist_m=dist_m, secs=secs, load=load, name=name, te=3.0, hr=140)


def add(rs, d: date, *acts):
    rs[D(d)]["activities"] += list(acts)
    rs[D(d)]["load"] = sum(a["load"] for a in rs[D(d)]["activities"])


def goal_view(gtype, target, gid="g1", label=None, start=None, status="on_track"):
    return dict(goal=dict(id=gid, type=gtype, target=target, deadline="2027-01-01", notes="private goal note"),
                label=label or gtype, start=start, status=status, status_text=status.replace("_", " ").title(), progress_pct=40.0)


# ---------------------------------- weeks ------------------------------------ #
class Weeks(unittest.TestCase):
    def test_every_day_maps_to_its_monday_and_sunday(self):
        for i in range(7):
            self.assertEqual(review.week_of(MON + timedelta(days=i)), (MON, date(2026, 10, 25)))
        self.assertEqual(review.week_of(date(2026, 10, 26))[0], date(2026, 10, 26))  # next Monday starts a new week

    def test_last_completed_on_every_weekday(self):
        # Mon 26 Oct .. Sun 1 Nov: the week that has fully finished is always 19-25 Oct
        for i in range(7):
            self.assertEqual(review.last_completed(date(2026, 10, 26) + timedelta(days=i)), MON, i)
        self.assertEqual(review.last_completed(date(2026, 11, 2)), date(2026, 10, 26))  # the Monday after Sunday night

    def test_sunday_is_still_part_of_the_running_week(self):
        sunday = date(2026, 11, 1)
        self.assertEqual(review.last_completed(sunday), MON)
        self.assertEqual(review.monday(sunday), date(2026, 10, 26))

    def test_week_across_a_year_boundary(self):
        self.assertEqual(review.week_of(date(2026, 1, 1)), (date(2025, 12, 29), date(2026, 1, 4)))
        self.assertEqual(review.last_completed(date(2026, 1, 1)), date(2025, 12, 22))
        self.assertEqual(review.last_completed(date(2026, 1, 5)), date(2025, 12, 29))
        rv = review.build(rows(date(2025, 11, 1), date(2026, 1, 6)), [], INDS, [], date(2025, 12, 29), date(2026, 1, 6))
        self.assertEqual((rv["start"], rv["end"], rv["complete"]), ("2025-12-29", "2026-01-04", True))
        self.assertEqual(rv["prev_start"], "2025-12-22")

    def test_parse_week_snaps_and_bounds(self):
        today = date(2026, 10, 28)
        self.assertEqual(review.parse_week(None, today), MON)
        self.assertEqual(review.parse_week("2026-10-22", today), MON)  # a Thursday snaps to its Monday
        self.assertEqual(review.parse_week("2026-10-28", today), date(2026, 10, 26))  # the current week is allowed
        with self.assertRaises(ValueError):
            review.parse_week("2026-11-02", today)  # next week hasn't started
        with self.assertRaises(ValueError):
            review.parse_week("2025-01-01", today)  # more than a year back

    def test_current_week_is_partial(self):
        today = date(2026, 10, 28)  # Wednesday
        rv = review.build(rows(date(2026, 9, 1), today), [], INDS, [], date(2026, 10, 26), today)
        self.assertFalse(rv["complete"])
        self.assertTrue(rv["current"])
        self.assertIsNone(rv["next_start"])
        self.assertEqual(rv["loaded_days"], 3)
        self.assertEqual(sum(d["future"] for d in rv["highlights"]["days"]), 4)


# ------------------------------- comparisons --------------------------------- #
class Tiles(unittest.TestCase):
    def test_delta_direction_and_good(self):
        t = review.tile("hrv", 60.0, 55.0)
        self.assertEqual((t["delta"], t["direction"], t["good"]), (5.0, "up", True))
        t = review.tile("rhr", 52.0, 49.0)  # resting HR up is not good
        self.assertEqual((t["direction"], t["good"]), ("up", False))
        t = review.tile("rhr", 47.0, 49.0)
        self.assertEqual((t["direction"], t["good"]), ("down", True))

    def test_dead_band_is_flat(self):
        self.assertEqual(review.tile("hrv", 60.4, 60.0)["direction"], "flat")
        self.assertIsNone(review.tile("hrv", 60.4, 60.0)["good"])
        self.assertEqual(review.tile("load", 505.0, 500.0)["direction"], "flat")  # relative: 1% of last week
        self.assertEqual(review.tile("load", 560.0, 500.0)["direction"], "up")

    def test_neutral_metrics_have_no_good_or_bad(self):
        t = review.tile("load", 700.0, 500.0)
        self.assertEqual((t["direction"], t["good"]), ("up", None))
        t = review.tile("weight", 80.0, 81.0, lower=None)
        self.assertEqual((t["direction"], t["good"]), ("down", None))

    def test_missing_week_has_no_comparison(self):
        for v, p in ((None, 5.0), (5.0, None)):
            t = review.tile("hrv", v, p)
            self.assertEqual((t["delta"], t["direction"], t["good"]), (None, None, None))


class Training(unittest.TestCase):
    def setUp(self):
        self.rs = rows(MON - timedelta(days=60), MON + timedelta(days=6), load=lambda d: 0)

    def test_walks_are_not_workouts_and_only_distance_sports_count(self):
        add(self.rs, MON, act("running", 10000, 3000, 80), act("walking", 3000, 1800, 5))
        add(self.rs, MON + timedelta(days=2), act("strength_training", 0, 2700, 40), act("tennis_v2", 2500, 3600, 60))
        t = review.training(self.rs, MON, MON + timedelta(days=7))
        self.assertEqual((t["workouts"], t["walks"]), (3, 1))
        self.assertEqual(t["distance_m"], 10000)  # tennis court distance and the walk aren't "miles"
        self.assertAlmostEqual(t["hours"], (3000 + 2700 + 3600) / 3600)
        self.assertEqual(t["load"], 185)
        self.assertEqual([x["type"] for x in t["by_type"]][-1], "walking")  # walks listed last

    def test_load_vs_prior_four_week_average(self):
        for k, load in ((1, 400), (2, 300), (3, 500), (4, 200)):
            add(self.rs, MON - timedelta(weeks=k), act("running", 5000, 1800, load))
        add(self.rs, MON, act("running", 5000, 1800, 525))
        t = review.training(self.rs, MON, MON + timedelta(days=7))
        self.assertEqual((t["prior_avg_load"], t["prior_weeks"], t["load_ratio"]), (350.0, 4, 1.5))
        self.assertEqual([w["load"] for w in t["weeks"]], [200, 500, 300, 400, 525])
        self.assertTrue(t["weeks"][-1]["current"])
        lt = next(x for x in t["tiles"] if x["key"] == "load")
        self.assertEqual((lt["value"], lt["prev"], lt["delta"]), (525, 400, 125))

    def test_unloaded_weeks_are_left_out_of_the_average(self):
        for d in range(7):  # week -3 never loaded from Garmin; week -4 only partly (3 days)
            self.rs.pop(D(MON - timedelta(weeks=3) + timedelta(days=d)))
        for d in range(4):
            self.rs.pop(D(MON - timedelta(weeks=4) + timedelta(days=d)))
        for k in (1, 2):
            add(self.rs, MON - timedelta(weeks=k), act("running", 5000, 1800, 300))
        add(self.rs, MON, act("running", 5000, 1800, 300))
        t = review.training(self.rs, MON, MON + timedelta(days=7))
        self.assertEqual((t["prior_avg_load"], t["prior_weeks"], t["load_ratio"]), (300.0, 2, 1.0))

    def test_no_previous_week_means_no_arrows(self):
        rs = rows(MON, MON + timedelta(days=6), load=lambda d: 0)
        add(rs, MON, act("running", 5000))
        t = review.training(rs, MON, MON + timedelta(days=7))
        self.assertTrue(all(x["direction"] is None for x in t["tiles"]))
        self.assertIsNone(t["prior_avg_load"])


class Recovery(unittest.TestCase):
    def test_week_vs_previous_week_and_baseline(self):
        hrv = lambda d: 50.0 if d < MON else 56.0
        rhr = lambda d: 50.0 if d < MON else 48.0
        sleep = lambda d: 7.0 if d < MON else (8.0 if d.weekday() % 2 else 6.0)
        rs = rows(MON - timedelta(days=28), MON + timedelta(days=6), hrv=hrv, rhr=rhr, sleep_h=sleep, bb_high=lambda d: 70, bb_low=lambda d: 20)
        r = review.recovery(rs, MON, MON + timedelta(days=7))
        tiles = {t["key"]: t for t in r["tiles"]}
        self.assertEqual((r["hrv"], r["hrv_base"], tiles["hrv"]["base_delta"], tiles["hrv"]["good"]), (56.0, 50.0, 6.0, True))
        self.assertEqual((r["rhr"], r["rhr_base"], tiles["rhr"]["direction"], tiles["rhr"]["good"]), (48.0, 50.0, "down", True))
        self.assertEqual((r["nights"], r["nights_7h"]), (7, 3))
        self.assertEqual(tiles["sleep_sd"]["prev"], 0.0)  # last week was perfectly regular
        self.assertEqual((tiles["sleep_sd"]["direction"], tiles["sleep_sd"]["good"]), ("up", False))
        self.assertEqual(len(r["daily"]), 7)

    def test_too_few_nights_for_consistency(self):
        rs = rows(MON, MON + timedelta(days=6), sleep_h=lambda d: 7.5 if d.weekday() < 2 else None)
        self.assertIsNone(review.recovery(rs, MON, MON + timedelta(days=7))["sleep_sd"])


class Body(unittest.TestCase):
    def test_no_weigh_ins(self):
        b = review.body(rows(MON - timedelta(days=7), MON + timedelta(days=6), weight_kg=lambda d: None, fat=lambda d: None), MON, [])
        self.assertEqual((b["weight_kg"], b["weight_change_kg"], b["weighins"]), (None, None, 0))
        self.assertIsNone(b["tiles"][0]["direction"])

    def test_change_of_weekly_averages_and_goal_direction(self):
        kg = lambda d: {MON - timedelta(days=6): 80.0, MON - timedelta(days=2): 81.0, MON + timedelta(days=1): 79.6, MON + timedelta(days=5): 79.4}.get(d)
        rs = rows(MON - timedelta(days=7), MON + timedelta(days=6), weight_kg=kg, fat=lambda d: None)
        b = review.body(rs, MON, [])
        self.assertEqual((b["weight_kg"], b["weight_change_kg"], b["weighins"]), (79.5, -1.0, 2))
        self.assertIsNone(b["tiles"][0]["good"])  # no weight goal: no judgement
        lose = review.body(rs, MON, [goal_view("weight", 75.0, start=82.0)])
        gain = review.body(rs, MON, [goal_view("weight", 85.0, start=78.0)])
        self.assertTrue(lose["tiles"][0]["good"])
        self.assertFalse(gain["tiles"][0]["good"])


class Checkins(unittest.TestCase):
    def test_counts_per_indicator_this_week_vs_last(self):
        ci = [dict(day=D(MON + timedelta(days=i)), ratings=dict(mood=1, energy=-1 if i < 2 else 1, focus=1), note="secret", tags=[]) for i in range(4)]
        ci += [dict(day=D(MON - timedelta(days=i)), ratings=dict(mood=-1), note="also secret", tags=[]) for i in range(1, 4)]
        c = review.checkin_section(ci, INDS, MON)
        by = {i["id"]: i for i in c["indicators"]}
        self.assertNotIn("focus", by)  # switched off
        self.assertEqual((by["mood"]["up"], by["mood"]["down"], by["mood"]["prev_down"], by["mood"]["trend"]), (4, 0, 3, "up"))
        self.assertEqual((by["energy"]["up"], by["energy"]["down"], by["energy"]["trend"]), (2, 2, None))
        self.assertEqual((c["days"], c["prev_days"], c["up"], c["down"]), (4, 3, 6, 2))
        up, down = c["tiles"]
        self.assertEqual((up["value"], up["prev"], up["good"]), (6, 0, True))
        self.assertEqual((down["value"], down["prev"], down["good"]), (2, 3, True))
        self.assertNotIn("secret", json.dumps(c))


class Goals(unittest.TestCase):
    def test_weekly_and_trend_goals(self):
        rs = rows(MON - timedelta(days=7), MON + timedelta(days=6), sleep_h=lambda d: 7.2 if d >= MON else 6.5,
                  weight_kg=lambda d: 80.0 if d < MON else 79.5, steps=lambda d: 8000)
        add(rs, MON, act("running", 12000))
        add(rs, MON + timedelta(days=3), act("trail_running", 9000), act("cycling", 30000))
        gl = review.goal_section([goal_view("weekly_run_km", 20.0, "a"), goal_view("sleep_h", 7.5, "b"),
                                  goal_view("weight", 75.0, "c"), goal_view("steps", 7500, "d"), goal_view("custom", None, "e")], rs, MON)
        by = {g["id"]: g for g in gl}
        self.assertEqual((by["a"]["week"], by["a"]["prev"], by["a"]["week_status"]), (21.0, 0.0, "met"))
        self.assertEqual((by["b"]["week"], by["b"]["week_status"]), (7.2, "short"))
        self.assertEqual((by["c"]["week"], by["c"]["week_status"]), (79.5, "towards"))
        self.assertEqual(by["d"]["week_status"], "met")
        self.assertIsNone(by["e"]["week_status"])
        away = review.goal_section([goal_view("weight", 85.0, "c")], rs, MON)[0]
        self.assertEqual(away["week_status"], "away")


class Highlights(unittest.TestCase):
    def test_best_workout_skips_walks(self):
        rs = rows(MON, MON + timedelta(days=6))
        add(rs, MON, act("walking", 8000, 7200, 300), act("running", 8000, 2400, 120))
        self.assertEqual(review.highlights(rs, MON, MON + timedelta(days=7))["best"]["type"], "running")

    def test_records_need_history_and_beat_the_past_year(self):
        rs = rows(MON - timedelta(weeks=20), MON + timedelta(days=6), load=lambda d: 0)
        add(rs, MON - timedelta(weeks=10), act("running", 15000, 5000, 150))
        add(rs, MON, act("running", 16000, 5400, 160))
        h = review.highlights(rs, MON, MON + timedelta(days=7))
        labels = {r["label"]: r for r in h["records"]}
        self.assertEqual((labels["Longest run"]["value"], labels["Longest run"]["prev_best"]), (16000, 15000))
        self.assertNotIn("Longest ride", labels)
        short = rows(MON - timedelta(weeks=3), MON + timedelta(days=6), load=lambda d: 0)
        add(short, MON - timedelta(weeks=2), act("running", 5000))
        add(short, MON, act("running", 16000))
        self.assertEqual(review.highlights(short, MON, MON + timedelta(days=7))["records"], [])  # under 8 weeks of history


# --------------------------- coach payload + rules --------------------------- #
class Coach(unittest.TestCase):
    def review(self):
        rs = rows(MON - timedelta(weeks=6), MON + timedelta(days=6), hrv=lambda d: 55, rhr=lambda d: 50, sleep_h=lambda d: 7.1,
                  weight_kg=lambda d: 80.0, fat=lambda d: 20.0, load=lambda d: 0)
        for k in range(5):
            add(rs, MON - timedelta(weeks=k), act("running", 10000, 3600, 100, name="Run around Lake Merritt"))
        ci = [dict(day=D(MON), ratings=dict(mood=1), note="felt anxious about work", tags=["Busy day"])]
        return review.build(rs, ci, INDS, [goal_view("weight", 75.0, label="Lose weight")], MON, MON + timedelta(days=8))

    def test_payload_has_no_notes_names_or_goal_notes(self):
        rv = self.review()
        for share in (False, True):
            p = json.dumps(review.coach_payload(rv, share))
            for secret in ("felt anxious", "Lake Merritt", "private goal note", "Busy day"):
                self.assertNotIn(secret, p)

    def test_checkins_only_when_sharing_is_on(self):
        rv = self.review()
        self.assertNotIn("checkins", review.coach_payload(rv, False))
        ci = review.coach_payload(rv, True)["checkins"]
        self.assertEqual(ci["indicators"], [dict(label="Mood", up=1, neutral=0, down=0, prev_up=0, prev_down=0)])

    def test_payload_localizes_to_us_units(self):
        p = units.localize(review.coach_payload(self.review(), False), "us")
        self.assertAlmostEqual(p["training"]["distance_mi"], 6.21, places=2)
        self.assertAlmostEqual(p["body"]["weight_avg_lb"], 176.37, places=1)
        self.assertAlmostEqual(p["goals"][0]["target_lb"], 165.35, places=1)
        self.assertNotIn("distance_km", p["training"])

    def test_rule_review_has_the_three_sections_with_numbers(self):
        rv = self.review()
        text = review.rule_review(rv, "us")
        for h in ("### What went well", "### What to watch", "### Next week plan"):
            self.assertIn(h, text)
        self.assertIn("6.2 mi", text)
        self.assertNotIn("km", text)
        partial = review.rule_review(dict(rv, complete=False), "us")
        self.assertIn("still in progress", partial)


# ------------------------------------ API ------------------------------------ #
class Api(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.env = mock.patch.dict(os.environ, {"GARMIN_DEMO": "1", "ANTHROPIC_API_KEY": ""})
        cls.env.start()
        from fastapi.testclient import TestClient
        import server
        cls.server = server
        cls.cache = tempfile.mkdtemp(prefix="coach-cache-")
        cls.exports = Path(tempfile.mkdtemp(prefix="coach-export-"))
        cls.patches = [mock.patch.object(server, "CACHE", Path(cls.cache)), mock.patch.object(server, "EXPORT_DIR", cls.exports)]
        for p in cls.patches:
            p.start()
        cls.c = TestClient(server.app, base_url=server.LOCAL_URL, headers={"Origin": server.LOCAL_URL})

    @classmethod
    def tearDownClass(cls):
        for p in cls.patches:
            p.stop()
        cls.env.stop()

    def setUp(self):
        store.settings_put({"units": None, "share_checkins": None})

    def test_default_week_is_the_last_completed_one(self):
        r = self.c.get("/api/review").json()
        self.assertEqual(r["start"], review.last_completed(date.today()).isoformat())
        self.assertTrue(r["complete"])
        self.assertIsNone(r["loading"])
        old = (review.last_completed(date.today()) - timedelta(weeks=5)).isoformat()
        self.assertIsNone(self.c.get(f"/api/review?week={old}").json()["coach"])  # opening a week never writes a review by itself
        self.assertFalse((Path(self.cache) / "weekreview" / f"demo-{old}.json").exists())
        self.assertEqual(len(r["training"]["weeks"]), 5)
        mid = (date.fromisoformat(r["start"]) + timedelta(days=3)).isoformat()
        self.assertEqual(self.c.get(f"/api/review?week={mid}").json()["start"], r["start"])
        self.assertEqual(self.c.get("/api/review?week=2999-01-01").status_code, 422)
        self.assertEqual(self.c.get("/api/review?week=not-a-date").status_code, 422)

    def test_coach_review_is_cached_per_week_and_regenerates(self):
        week = (review.last_completed(date.today()) - timedelta(weeks=2)).isoformat()
        a = self.c.get(f"/api/review/coach?week={week}").json()
        self.assertEqual(a["source"], "rules")
        self.assertIn("### Next week plan", a["text"])
        self.assertTrue((Path(self.cache) / "weekreview" / f"demo-{week}.json").exists())  # demo reviews never mix with real ones
        with mock.patch.object(review, "rule_review", side_effect=AssertionError("should come from the cache")):
            self.assertEqual(self.c.get(f"/api/review/coach?week={week}").json(), a)
        self.assertEqual(self.c.get(f"/api/review?week={week}").json()["coach"], a)
        with mock.patch.object(review, "rule_review", return_value="### rewritten"):
            self.assertEqual(self.c.get(f"/api/review/coach?week={week}&refresh=true").json()["text"], "### rewritten")
        store.settings_put({"units": "metric"})  # a review written in other units is stale
        self.assertIsNone(self.c.get(f"/api/review?week={week}").json()["coach"])

    def test_claude_gets_the_payload_without_notes(self):
        week = review.last_completed(date.today()) - timedelta(weeks=3)
        store.checkin_put(week.isoformat(), ratings={"mood": 1}, note="a very private note", tags=["Sore"])
        store.settings_put({"share_checkins": True})
        sent = {}
        data = dict(headline="Solid week", went_well=["ok"], watch=[], next_week=["hold volume"], reasons=["load 300"])
        fake = lambda payload, system, essential=True: sent.update(payload=payload, system=system) or dict(
            source="claude", data=data, text="**Solid week**")
        try:
            with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"}), mock.patch.object(review, "claude_review", side_effect=fake):
                r = self.c.get(f"/api/review/coach?week={week}&refresh=true").json()
        finally:
            store.checkin_put(week.isoformat(), ratings={"mood": None}, note="", tags=[])
        self.assertEqual(r["source"], "claude")
        self.assertEqual(sent["system"], "us")
        self.assertIn("checkins", sent["payload"])
        self.assertNotIn("a very private note", json.dumps(sent["payload"]))

    def test_auto_generates_once_per_finished_week(self):
        week = review.last_completed(date.today()).isoformat()
        gate, calls = threading.Event(), []
        real = self.server._review_auto_worker

        def slow_worker(monday):
            calls.append(monday)
            gate.wait(5)
            real(monday)

        with mock.patch.object(self.server, "_review_auto_worker", side_effect=slow_worker):
            (Path(self.cache) / "weekreview" / f"demo-{week}.json").unlink(missing_ok=True)
            first = self.c.post("/api/review/auto").json()
            second = self.c.post("/api/review/auto").json()
            gate.set()
        self.assertEqual((first, second), (dict(week=week, status="started"), dict(week=week, status="running")))
        for _ in range(100):
            if self.c.post("/api/review/auto").json()["status"] == "ready":
                break
            time.sleep(0.05)
        self.assertEqual(self.c.post("/api/review/auto").json(), dict(week=week, status="ready"))
        self.assertEqual(len(calls), 1)
        self.assertIsNotNone(self.c.get("/api/review").json()["coach"])

    def test_export_saves_a_valid_file_under_a_fixed_name(self):
        week = review.last_completed(date.today())
        png = b"\x89PNG\r\n\x1a\n" + b"\0" * 64
        r = self.c.post("/api/review/export", data=dict(week=(week + timedelta(days=2)).isoformat(), fmt="png"),
                        files=dict(file=("../../evil.png", png, "image/png")))
        self.assertEqual(r.status_code, 200)
        self.assertEqual(Path(r.json()["path"]), self.exports / f"weekly-review-{week}.png")
        self.assertEqual((self.exports / f"weekly-review-{week}.png").read_bytes(), png)
        pdf = self.c.post("/api/review/export", data=dict(week=week.isoformat(), fmt="pdf"), files=dict(file=("x.pdf", b"%PDF-1.3\n%", "application/pdf")))
        self.assertEqual(pdf.status_code, 200)
        bad = [dict(fmt="pdf", body=png), dict(fmt="png", body=b"<html>"), dict(fmt="exe", body=png)]
        for b in bad:
            r = self.c.post("/api/review/export", data=dict(week=week.isoformat(), fmt=b["fmt"]), files=dict(file=("f", b["body"])))
            self.assertEqual(r.status_code, 422, b)
        self.assertEqual(sorted(p.name for p in self.exports.iterdir()), [f"weekly-review-{week}.pdf", f"weekly-review-{week}.png"])


if __name__ == "__main__":
    unittest.main()
