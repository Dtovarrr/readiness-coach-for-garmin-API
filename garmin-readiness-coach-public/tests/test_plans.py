"""Training plans: progression (≤10%), recovery weeks, taper, long-session growth and caps, session
placement (no back-to-back hard days, rest day kept, long day honored), short timelines and other edge
cases, safety warnings, daily readiness suggestions, missed workouts, done-matching, regeneration and
the endpoints in demo mode (Claude never called).

Run from the project folder:  .venv/bin/python -m unittest discover -s tests -v
Uses a throwaway COACH_DATA_DIR and cache folder; never calls Garmin or Claude.
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

import plans as P  # noqa: E402
import store  # noqa: E402

assert store.DATA_DIR != (ROOT / ".data").resolve(), "tests must never run against the real data folder"
TODAY = date(2026, 10, 28)  # a Wednesday
HALF, MARATHON, FIVE_K = 21097.5, 42195.0, 5000.0


def event(kind="run", weeks=14, dist=HALF, priority="A", days=None):
    d = TODAY + (timedelta(days=days) if days is not None else timedelta(weeks=weeks))
    return dict(id="e1", name="Race", kind=kind, distance_m=dist if kind in ("run", "ride") else None, priority=priority, date=d.isoformat())


def plan(ev=None, **inp):
    base = dict(current_weekly=25, longest_recent=12, days=5, long_day=5, injured=False)
    base.update(inp)
    return P.generate(ev or event(), base, TODAY)


def full_weeks(p):
    return [(w, t) for w, t in zip(p["weeks"], P.week_totals(p)) if not w.get("partial")]


class Progression(unittest.TestCase):
    def test_volume_never_rises_more_than_10_percent_over_the_last_full_week(self):
        for weeks, dist, cur in ((14, HALF, 25), (20, MARATHON, 30), (10, 10000, 10), (8, FIVE_K, 0)):
            vols = P.volumes(weeks + 1, max(cur, 12), 55, 2, 4)
            last_full = None
            for v, phase in vols:
                if phase in ("Recovery", "Taper", "Event week"):
                    continue
                if last_full is not None:
                    self.assertLessEqual(v, last_full * 1.10 + 1e-9, (weeks, vols))
                last_full = v

    def test_reaches_but_never_exceeds_the_peak(self):
        vols = P.volumes(20, 20, 55, 2, 4)
        build = [v for v, ph in vols if ph not in ("Taper", "Event week")]
        self.assertAlmostEqual(max(build), 55)
        self.assertTrue(all(v <= 55 + 1e-9 for v in build))

    def test_an_already_fit_athlete_is_never_asked_to_do_less(self):
        vols = P.volumes(10, 80, 55, 2, 4)
        self.assertEqual(vols[0][0], 80)

    def test_generated_weeks_match_their_targets(self):
        p = plan()
        for w, total in full_weeks(p):
            if w["phase"] not in ("Race week",):
                self.assertAlmostEqual(total, w["target"], delta=max(1.0, 0.04 * w["target"]), msg=w)

    def test_long_session_grows_at_most_10_percent_and_respects_the_cap(self):
        p = plan(event(dist=MARATHON, weeks=20), current_weekly=50, longest_recent=25)
        longs = [w["long"] for w in p["weeks"] if w["long"] and w["phase"] != "Recovery"]
        for a, b in zip(longs, longs[1:]):
            self.assertLessEqual(b, a * 1.10 + 0.05)
        self.assertLessEqual(max(longs), 32)


class RecoveryWeeks(unittest.TestCase):
    def test_every_fourth_week_at_about_70_percent(self):
        vols = P.volumes(16, 20, 55, 2, 4)
        rec = [i for i, (_, ph) in enumerate(vols) if ph == "Recovery"]
        self.assertEqual(rec, [3, 7, 11])
        for i in rec:
            self.assertAlmostEqual(vols[i][0], vols[i - 1][0] * 0.70, places=6)

    def test_every_third_week_when_injured_and_gentler_start(self):
        p = plan(injured=True)
        rec = [w["index"] for w in p["weeks"] if w["phase"] == "Recovery"]
        self.assertEqual(rec[:2], [2, 5])
        self.assertLessEqual(p["inputs"]["start"], 25 * 0.8 + 0.01)
        self.assertFalse(any(w["type"] == "intervals" for w in p["workouts"]))  # no hard intervals while injured

    def test_the_week_after_recovery_rebuilds_from_the_last_full_week(self):
        vols = P.volumes(10, 20, 55, 1, 4)
        self.assertAlmostEqual(vols[4][0], vols[2][0] * 1.10)


class Taper(unittest.TestCase):
    def test_two_week_taper_for_a_half_or_longer(self):
        p = plan()
        self.assertEqual([w["phase"] for w in p["weeks"][-2:]], ["Taper", "Race week"])
        peak = max(w["target"] for w in p["weeks"][:-2])
        self.assertAlmostEqual(p["weeks"][-2]["target"], peak * 0.75, delta=0.2)
        self.assertAlmostEqual(p["weeks"][-1]["target"], peak * 0.5, delta=0.2)

    def test_one_week_taper_for_a_5k(self):
        p = plan(event(dist=FIVE_K, weeks=10))
        self.assertEqual(p["weeks"][-1]["phase"], "Race week")
        self.assertNotEqual(p["weeks"][-2]["phase"], "Taper")

    def test_race_on_event_day_and_a_rest_day_before(self):
        p = plan()
        race = [w for w in p["workouts"] if w["type"] == "race"]
        self.assertEqual(len(race), 1)
        self.assertEqual(race[0]["date"], event()["date"])
        self.assertEqual(race[0]["distance_m"], round(HALF, -2))
        day_before = (date.fromisoformat(event()["date"]) - timedelta(days=1)).isoformat()
        self.assertFalse([w for w in p["workouts"] if w["date"] == day_before])
        self.assertFalse([w for w in p["workouts"] if w["date"] > event()["date"]])

    def test_b_events_taper_for_one_week(self):
        p = plan(event(priority="B"))
        self.assertNotIn("Taper", [w["phase"] for w in p["weeks"]])


class Placement(unittest.TestCase):
    def check_week_rules(self, p):
        by_day = {}
        for w in p["workouts"]:
            by_day.setdefault(w["date"], []).append(w)
        self.assertTrue(all(len(v) == 1 for v in by_day.values()), "never two sessions a day")
        for k, ws in by_day.items():
            nxt = (date.fromisoformat(k) + timedelta(days=1)).isoformat()
            if P.hard(ws[0]) and ws[0]["type"] != "race" and nxt in by_day:
                self.assertFalse(P.hard(by_day[nxt][0]) and by_day[nxt][0]["type"] != "race", (k, ws[0]["title"], by_day[nxt][0]["title"]))
        for wk in p["weeks"]:
            n = sum(1 for w in p["workouts"] if wk["start"] <= w["date"] <= wk["end"])
            self.assertLessEqual(n, 6, "a rest day is always kept")

    def test_no_back_to_back_hard_days_for_every_template_and_long_day(self):
        for days in (3, 4, 5, 6):
            for long_day in range(7):
                p = plan(days=days, long_day=long_day)
                self.check_week_rules(p)
                longs = [w for w in p["workouts"] if w["type"] == "long"]
                self.assertTrue(longs and all(date.fromisoformat(w["date"]).weekday() == long_day for w in longs), (days, long_day))

    def test_three_vs_six_days(self):
        wk = lambda p: [w for w in p["workouts"] if p["weeks"][2]["start"] <= w["date"] <= p["weeks"][2]["end"]]
        self.assertEqual(len(wk(plan(days=3))), 3)
        self.assertEqual(len(wk(plan(days=6, current_weekly=45))), 6)
        self.assertEqual(len(wk(plan(days=9))), 6)  # clamped to 6: one rest day is kept

    def test_mix_of_session_types(self):
        types = {w["type"] for w in plan()["workouts"]}
        self.assertTrue({"easy", "tempo", "intervals", "long", "race"} <= types)

    def test_one_quality_session_in_base_weeks_and_none_in_recovery(self):
        for days in (5, 6):
            p = plan(days=days, current_weekly=40)
            for wk in p["weeks"]:
                q = [w for w in p["workouts"] if w["week"] == wk["index"] and w["type"] in ("tempo", "intervals")]
                if wk["phase"] == "Base":
                    self.assertLessEqual(len(q), 1, (days, wk))
                if wk["phase"] == "Recovery":
                    self.assertEqual(q, [], (days, wk))

    def test_no_intervals_in_the_first_base_weeks_or_recovery_weeks(self):
        p = plan()
        for w in p["workouts"]:
            wk = p["weeks"][w["week"]]
            if w["type"] == "intervals":
                self.assertNotIn(wk["phase"], ("Recovery",))
                self.assertGreaterEqual(wk["index"], 2)


class EdgeCases(unittest.TestCase):
    def test_event_in_nine_days(self):
        p = plan(event(days=9))
        self.assertEqual([w["phase"] for w in p["weeks"]], ["Base", "Race week"])
        self.assertTrue(any(w["type"] == "race" for w in p["workouts"]))
        self.assertIn("too_soon", [x["kind"] for x in P.check(p, event(days=9), TODAY)])

    def test_event_today_and_in_the_past(self):
        p = plan(event(days=0))
        self.assertEqual([w["type"] for w in p["workouts"]], ["race"])
        with self.assertRaises(ValueError):
            plan(event(days=-1))

    def test_zero_recent_volume_starts_at_the_floor_and_warns(self):
        p = plan(event(dist=FIVE_K, weeks=8), current_weekly=0, longest_recent=0)
        self.assertEqual(p["inputs"]["start"], 12)
        self.assertIn("low_volume", [x["kind"] for x in P.check(p, event(dist=FIVE_K, weeks=8), TODAY)])

    def test_marathon_in_six_weeks_is_flagged_but_still_planned(self):
        ev = event(dist=MARATHON, weeks=6)
        p = plan(ev, current_weekly=30)
        kinds = [x["kind"] for x in P.check(p, ev, TODAY)]
        self.assertIn("too_soon", kinds)
        self.assertEqual(p["weeks"][-1]["phase"], "Race week")
        self.assertNotIn("Taper", [w["phase"] for w in p["weeks"]])  # no room for a 2-week taper

    def test_already_trained_needs_fewer_weeks(self):
        ev = event(dist=MARATHON, weeks=9)
        self.assertNotIn("too_soon", [x["kind"] for x in P.check(plan(ev, current_weekly=65, longest_recent=30), ev, TODAY)])

    def test_c_events_get_no_plan(self):
        with self.assertRaises(ValueError):
            plan(event(priority="C"))

    def test_tennis_and_general_plans(self):
        for kind in ("tennis", "general"):
            ev = event(kind=kind, weeks=6, priority="B")
            p = plan(ev, current_weekly=150, longest_recent=90, days=4)
            self.assertEqual(p["unit"], "min")
            self.assertTrue(all(w.get("distance_m") is None for w in p["workouts"]))
            self.assertEqual(p["workouts"][-1]["type"], "race")
            self.assertEqual(p["weeks"][-1]["phase"], "Event week")
            types = {w["type"] for w in p["workouts"]}
            self.assertTrue(({"match", "drills"} if kind == "tennis" else {"long", "strength"}) <= types, types)

    def test_cycling_plan(self):
        p = plan(event(kind="ride", dist=100000, weeks=10), current_weekly=120, longest_recent=60)
        self.assertEqual(p["unit"], "km")
        self.assertTrue(any(w["title"] == "Long ride" for w in p["workouts"]))


class Warnings(unittest.TestCase):
    def test_a_clean_plan_only_carries_the_disclaimer(self):
        self.assertEqual([x["kind"] for x in P.check(plan(), event(), TODAY)], ["disclaimer"])

    def test_an_edit_that_jumps_a_week_is_flagged(self):
        p = plan()
        w = next(x for x in p["workouts"] if x["week"] == 4 and x["type"] == "easy")
        P.edit(p, w["id"], dict(distance_m=w["distance_m"] + 8000))
        warn = [x for x in P.check(p, event(), TODAY) if x["kind"] == "increase"]
        self.assertEqual([x["week"] for x in warn], [4])
        self.assertIn("more than ~10% a week", warn[0]["text"])

    def test_injury_adds_the_doctor_note(self):
        texts = [x["text"] for x in P.check(plan(injured=True), event(), TODAY)]
        self.assertTrue(any("doctor or physio" in t for t in texts))

    def test_overlapping_plans(self):
        a, b = plan(), plan(event(kind="tennis", weeks=8, priority="B"), current_weekly=150, longest_recent=90)
        ov = P.overlaps(a, [("Club Open", b)], TODAY)
        self.assertTrue(ov["Club Open"])
        self.assertIn("overlap", [x["kind"] for x in P.check(a, event(), TODAY, overlaps=ov)])


class Daily(unittest.TestCase):
    def setUp(self):
        self.p = plan()
        self.hard = next(w for w in self.p["workouts"] if w["type"] == "intervals")
        self.easy = next(w for w in self.p["workouts"] if w["type"] == "easy" and w["date"] > TODAY.isoformat())
        self.ev_day = date.fromisoformat(event()["date"])

    def test_hard_session_on_a_low_readiness_day_gets_a_suggestion(self):
        day = date.fromisoformat(self.hard["date"])
        s = P.suggest(self.hard, dict(readiness=40, zone="recover", flags=["HRV has dropped three nights running"]), self.p, self.ev_day, day)
        self.assertEqual(s["workout_id"], self.hard["id"])
        acts = [o["action"] for o in s["options"]]
        self.assertEqual(acts[0], "easier")
        self.assertIn("skip", acts)
        easier = s["options"][0]["changes"]
        self.assertEqual((easier["type"], easier["intensity"]), ("easy", "easy"))
        self.assertEqual(easier["distance_m"], round(self.hard["distance_m"] * 0.7, -2))
        mv = next((o for o in s["options"] if o["action"] == "move"), None)
        if mv:  # the new day is free, in the same week and not next to a hard day
            nd = date.fromisoformat(mv["changes"]["date"])
            self.assertEqual(P.monday(nd), P.monday(day))
            near = [w for w in self.p["workouts"] if abs((date.fromisoformat(w["date"]) - nd).days) <= 1 and w["id"] != self.hard["id"]]
            self.assertFalse(any(P.hard(w) for w in near))

    def test_no_suggestion_when_ready_or_easy_on_a_caution_day(self):
        day = date.fromisoformat(self.hard["date"])
        self.assertIsNone(P.suggest(self.hard, dict(readiness=85, zone="prime", flags=[]), self.p, self.ev_day, day))
        self.assertIsNone(P.suggest(self.easy, dict(readiness=55, zone="caution", flags=[]), self.p, self.ev_day, date.fromisoformat(self.easy["date"])))

    def test_dismissed_suggestions_stay_dismissed_and_nothing_changes_by_itself(self):
        day = date.fromisoformat(self.hard["date"])
        before = dict(self.hard)
        low = dict(readiness=40, zone="recover", flags=[])
        self.assertIsNotNone(P.suggest(self.hard, low, self.p, self.ev_day, day))
        self.assertEqual(self.hard, before)  # suggesting never edits the plan
        self.hard["dismissed"] = day.isoformat()
        self.assertIsNone(P.suggest(self.hard, low, self.p, self.ev_day, day))

    def test_races_are_never_second_guessed(self):
        race = next(w for w in self.p["workouts"] if w["type"] == "race")
        self.assertIsNone(P.suggest(race, dict(readiness=20, zone="recover", flags=["x"]), self.p, self.ev_day, self.ev_day))


class Missed(unittest.TestCase):
    def test_reschedule_only_to_a_valid_free_day_in_the_same_week(self):
        p = plan(days=4)  # long Sat, quality Tue, easy Thu and Sun
        w = next(x for x in p["workouts"] if x["week"] == 2 and x["type"] in ("tempo", "intervals"))  # week 3 is a recovery week
        today = date.fromisoformat(w["date"]) + timedelta(days=1)
        m = next(x for x in P.missed(p, P.statuses(p, {}, today), date.fromisoformat(event()["date"]), today) if x["workout"]["id"] == w["id"])
        nd = date.fromisoformat(m["reschedule"])
        self.assertEqual(P.monday(nd), P.monday(date.fromisoformat(w["date"])))
        self.assertGreaterEqual(nd, today)
        self.assertFalse([x for x in p["workouts"] if x["date"] == m["reschedule"]])  # never two in a day
        near = [x for x in p["workouts"] if abs((date.fromisoformat(x["date"]) - nd).days) == 1 and x["id"] != w["id"]]
        self.assertFalse(any(P.hard(x) for x in near))  # and never next to a hard day

    def test_no_crammed_reschedule_when_every_free_day_touches_a_hard_one(self):
        p = plan(days=3)  # long Sat, quality Wed, easy Mon: Tue/Thu/Fri/Sun all sit next to a hard day
        w = next(x for x in p["workouts"] if x["week"] == 2 and x["type"] == "easy")
        today = date.fromisoformat(w["date"]) + timedelta(days=1)
        m = next(x for x in P.missed(p, P.statuses(p, {}, today), date.fromisoformat(event()["date"]), today) if x["workout"]["id"] == w["id"])
        self.assertIsNone(m["reschedule"])

    def test_a_full_week_offers_only_skip(self):
        p = plan(days=6, current_weekly=45)
        wk = p["weeks"][2]
        last = max((x for x in p["workouts"] if x["week"] == 2 and not P.hard(x) and x["date"] < wk["end"]), key=lambda x: x["date"])
        today = date.fromisoformat(wk["end"])  # Sunday: nothing left in that week
        m = [x for x in P.missed(p, P.statuses(p, {}, today), date.fromisoformat(event()["date"]), today) if x["workout"]["id"] == last["id"]]
        self.assertTrue(m and m[0]["reschedule"] is None)

    def test_older_than_a_week_is_let_go(self):
        p = plan()
        far = date.fromisoformat(p["weeks"][5]["start"])
        self.assertTrue(all(m["workout"]["date"] >= (far - timedelta(days=7)).isoformat()
                            for m in P.missed(p, P.statuses(p, {}, far), date.fromisoformat(event()["date"]), far)))


class Matching(unittest.TestCase):
    def test_done_needs_the_right_sport_and_half_the_work(self):
        w = dict(id="w", date="2026-11-03", sport="run", type="easy", distance_m=8000, status="planned")
        self.assertEqual(P.completion(w, [dict(type="running", dist_m=4100, secs=1500)])["dist_m"], 4100)
        self.assertIsNone(P.completion(w, [dict(type="running", dist_m=3900, secs=1500)]))
        self.assertIsNone(P.completion(w, [dict(type="cycling", dist_m=30000, secs=3600)]))
        t = dict(id="t", date="2026-11-03", sport="tennis", type="match", minutes=60, status="planned")
        self.assertIsNotNone(P.completion(t, [dict(type="tennis_v2", dist_m=2000, secs=3000)]))

    def test_states(self):
        p = dict(workouts=[dict(id="a", date="2026-10-26", sport="run", type="easy", distance_m=5000, status="planned"),
                           dict(id="b", date="2026-10-27", sport="run", type="easy", distance_m=5000, status="planned"),
                           dict(id="c", date="2026-10-27", sport="run", type="tempo", distance_m=5000, status="skipped"),
                           dict(id="d", date="2026-10-28", sport="run", type="easy", distance_m=5000, status="planned"),
                           dict(id="e", date="2026-10-30", sport="run", type="easy", distance_m=5000, status="planned")])
        st = P.statuses(p, {"2026-10-26": [dict(type="running", dist_m=5200, secs=1700)]}, TODAY)
        self.assertEqual({k: v["state"] for k, v in st.items()}, dict(a="done", b="missed", c="skipped", d="today", e="planned"))


class Regenerate(unittest.TestCase):
    def test_keeps_the_past_and_rebuilds_to_the_new_date(self):
        p = plan()
        later = TODAY + timedelta(days=15)
        past = [w for w in p["workouts"] if w["date"] < later.isoformat()]
        moved = dict(event(), date=(TODAY + timedelta(weeks=18)).isoformat())
        q = P.regenerate(p, moved, dict(current_weekly=25, longest_recent=12, days=5, long_day=5), later)
        self.assertEqual([w["id"] for w in q["workouts"] if w["date"] < later.isoformat()], [w["id"] for w in past])
        self.assertEqual(next(w for w in q["workouts"] if w["type"] == "race")["date"], moved["date"])
        self.assertEqual(q["regenerated"], later.isoformat())


class Page(unittest.TestCase):
    def test_no_top_level_name_is_declared_twice(self):
        """Two `function loadInsights` once silently replaced the Calendar's check-in card loader."""
        import re
        from collections import Counter
        src = (ROOT / "static" / "index.html").read_text()
        names = re.findall(r"^(?:async )?function (\w+)\(", src, re.M) + re.findall(r"^(?:const|let) (\w+)=", src, re.M)
        self.assertEqual([n for n, c in Counter(names).items() if c > 1], [])


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
        for e in store.events():
            store.event_delete(e["id"])

    def new(self, **kw):
        body = dict(name="City Half", date=(date.today() + timedelta(weeks=12)).isoformat(), kind="run", distance_m=HALF, priority="A")
        body.update(kw)
        return self.c.post("/api/events", json=body)

    def test_event_crud_and_validation(self):
        e = self.new().json()
        self.assertEqual(self.c.get("/api/events").json()["events"][0]["id"], e["id"])
        self.assertEqual(self.new(date=(date.today() - timedelta(days=1)).isoformat()).status_code, 422)
        self.assertEqual(self.new(distance_m=None).status_code, 422)  # a running race needs its distance
        self.assertEqual(self.new(kind="tennis", category="tournament", distance_m=None).status_code, 200)
        self.assertEqual(self.c.delete(f"/api/events/{e['id']}").json(), dict(ok=True))
        self.assertEqual(self.c.delete(f"/api/events/{e['id']}").status_code, 404)

    def test_generate_view_edit_and_regenerate_on_date_change(self):
        e = self.new().json()
        v = self.c.post(f"/api/events/{e['id']}/plan").json()
        self.assertEqual(v["plan"]["weeks"][-1]["phase"], "Race week")
        self.assertEqual(v["plan"]["overview"]["source"], "rules")
        self.assertIn("10%", v["plan"]["overview"]["text"])
        w = next(x for x in v["plan"]["workouts"] if x["date"] > date.today().isoformat() and x["type"] != "race")
        v2 = self.c.patch(f"/api/plan/{e['id']}/workouts/{w['id']}", json=dict(status="skipped")).json()
        self.assertEqual(v2["statuses"][w["id"]]["state"], "skipped")
        self.assertEqual(self.c.patch(f"/api/plan/{e['id']}/workouts/{w['id']}", json=dict(date="2099-01-01")).status_code, 422)
        moved = self.c.put(f"/api/events/{e['id']}", json=dict(name="City Half", date=(date.today() + timedelta(weeks=16)).isoformat(),
                                                                 kind="run", distance_m=HALF, priority="A")).json()
        self.assertTrue(moved["regenerated"])
        self.assertEqual(len(self.c.get(f"/api/plan/{e['id']}").json()["plan"]["weeks"]), (P.monday(date.today() + timedelta(weeks=16)) - P.monday(date.today())).days // 7 + 1)

    def test_c_event_has_no_plan(self):
        e = self.new(priority="C").json()
        self.assertEqual(self.c.post(f"/api/events/{e['id']}/plan").status_code, 422)

    def test_today_and_calendar_overlay(self):
        e = self.new().json()
        self.c.post(f"/api/events/{e['id']}/plan")
        t = self.c.get("/api/training/today").json()
        self.assertEqual(t["events"][0]["days_left"], (date.fromisoformat(e["date"]) - date.today()).days)
        month = e["date"][:7]
        cal = self.c.get(f"/api/calendar?month={month}").json()
        self.assertEqual(cal["events"][e["date"]][0]["name"], "City Half")
        self.assertTrue(any(w["type"] == "race" for w in cal["planned"][e["date"]]))
        day = self.c.get(f"/api/calendar/day/{e['date']}").json()
        self.assertTrue(day["future"])
        self.assertEqual(day["events"][0]["id"], e["id"])

    def test_accepting_a_suggestion_applies_it_and_dismissing_remembers(self):
        e = self.new().json()
        p = self.c.post(f"/api/events/{e['id']}/plan").json()["plan"]
        today = date.today().isoformat()
        wid = next((w["id"] for w in p["workouts"] if w["date"] == today), None)
        if wid is None:  # make today's session a hard one for the test
            w = next(x for x in p["workouts"] if x["date"] > today and x["type"] != "race")
            self.c.patch(f"/api/plan/{e['id']}/workouts/{w['id']}", json=dict(date=today, intensity="hard"))
            wid = w["id"]
        else:
            self.c.patch(f"/api/plan/{e['id']}/workouts/{wid}", json=dict(intensity="hard"))
        low = dict(readiness=38, zone="recover", flags=[])
        with mock.patch.object(self.server, "_assessment", return_value=low):
            t = self.c.get("/api/training/today").json()
            s = next(x for x in t["today"] if x["workout"]["id"] == wid)["suggestion"]
            self.assertEqual(s["options"][0]["action"], "easier")
            after = self.c.post("/api/training/act", json=dict(event_id=e["id"], workout_id=wid, action="easier")).json()
            w = next(x for x in after["today"] if x["workout"]["id"] == wid)["workout"]
            self.assertEqual(w["intensity"], "easy")
            self.assertIn("readiness", w["adjusted"])
            self.assertIsNone(next(x for x in after["today"] if x["workout"]["id"] == wid)["suggestion"])


if __name__ == "__main__":
    unittest.main()
