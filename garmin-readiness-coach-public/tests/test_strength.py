"""Strength mode: progression and deload rules, plate math, warm-ups, e1RM, scheduling, autosave and
recovery of a workout in progress, and editing history without corrupting the progression.

Run from the project folder:  .venv/bin/python -m unittest discover -s tests -v
Uses a throwaway COACH_DATA_DIR and cache folder, demo mode; never calls Garmin or Anthropic.
"""
import importlib
import os
import sys
import tempfile
import time
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
os.environ.setdefault("COACH_DATA_DIR", tempfile.mkdtemp(prefix="coach-test-"))
sys.path.insert(0, str(ROOT))

import strength as S  # noqa: E402
import strength_lib as L  # noqa: E402

ST = S.settings_with_defaults(None)
LBS = lambda v: S.from_unit(v, "lb")  # noqa: E731
IN_LB = lambda kg: round(S.to_unit(kg, "lb"), 4)  # noqa: E731
MON = date(2026, 10, 26)  # a Monday


def program(**lift_overrides) -> dict:
    p = L.from_template("5x5", "us", "p1")
    for ex, kw in lift_overrides.items():
        p["lifts"][ex].update(kw)
    return p


def session(day, entries, workout="A", program_id="p1", sid=None, started=None) -> dict:
    """entries: [(exercise_id, weight_lb, [reps per set], sets_target, reps_target)]"""
    exs = []
    for ex_id, lb, reps, n, r in entries:
        ex = L.BY_ID[ex_id]
        exs.append(dict(exercise_id=ex_id, name=ex["name"], load=ex["load"], kg=LBS(lb), sets_target=n, reps_target=r,
                        sets=[dict(reps=x, kg=LBS(lb), failed=x is not None and x < r, note="") for x in reps],
                        skipped=False, extra=False, trimmed=False, warmups=[]))
    d = day if isinstance(day, str) else day.isoformat()
    return dict(id=sid or f"s{d}{workout}", day=d, program_id=program_id, workout_key=workout, started=started or 0, exercises=exs)


def squat(day, lb, reps, **kw):
    return session(day, [("squat", lb, reps, 5, 5)], **kw)


class ProgressionTest(unittest.TestCase):
    def states(self, p, sessions):
        return S.lift_states(p, sessions, ST, "lb")

    def test_starting_weights(self):
        st = self.states(program(), [])
        self.assertEqual(IN_LB(st["squat"]["next_kg"]), 45)
        self.assertEqual(IN_LB(st["deadlift"]["next_kg"]), 95)
        self.assertEqual(st["squat"]["status"], "new")

    def test_success_adds_the_increment_and_deadlift_adds_ten(self):
        p = program()
        st = self.states(p, [session(MON, [("squat", 45, [5] * 5, 5, 5), ("deadlift", 95, [5], 1, 5)], workout="B")])
        self.assertEqual(IN_LB(st["squat"]["next_kg"]), 50)
        self.assertEqual(IN_LB(st["deadlift"]["next_kg"]), 105)

    def test_failure_repeats_the_weight(self):
        st = self.states(program(), [squat(MON, 100, [5, 5, 5, 4, 3])])
        self.assertEqual(IN_LB(st["squat"]["next_kg"]), 100)
        self.assertEqual((st["squat"]["streak"], st["squat"]["events"][-1]["kind"]), (1, "fail"))

    def test_three_failures_deload_ten_percent_to_a_loadable_weight(self):
        p = program()
        fails = [squat(MON + timedelta(days=2 * i), 135, [5, 5, 5, 5, 3]) for i in range(3)]
        st = self.states(p, fails)["squat"]
        self.assertEqual(IN_LB(st["next_kg"]), 120)  # 121.5 rounds down to what the plates make
        self.assertEqual((st["streak"], st["events"][-1]["kind"], st["status"]), (0, "deload", "deloaded"))

    def test_a_success_resets_the_fail_streak(self):
        seq = [squat(MON, 100, [5, 5, 5, 5, 4]), squat(MON + timedelta(days=2), 100, [5] * 5),
               squat(MON + timedelta(days=4), 105, [5, 5, 5, 4, 4]), squat(MON + timedelta(days=7), 105, [5, 5, 5, 5, 3])]
        st = self.states(program(), seq)["squat"]
        self.assertEqual((IN_LB(st["next_kg"]), st["streak"]), (105, 2))  # two strikes since the success, not three

    def test_configurable_fail_limit_and_deload(self):
        p = program(squat=dict(fail_limit=2, deload_pct=20))
        st = self.states(p, [squat(MON, 200, [5, 5, 5, 5, 3]), squat(MON + timedelta(days=2), 200, [5, 5, 5, 5, 3])])["squat"]
        self.assertEqual(IN_LB(st["next_kg"]), 160)

    def test_small_increments(self):
        p = program(squat=dict(increment_kg=LBS(2.5)))
        st = self.states(p, [squat(MON, 135, [5] * 5)])["squat"]
        self.assertEqual(IN_LB(st["next_kg"]), 140)  # no 1.25 lb plates: the next weight the plates can make
        fine = dict(ST, plates_lb=ST["plates_lb"] + [[1.25, 1]])
        st = S.lift_states(p, [squat(MON, 135, [5] * 5)], fine, "lb")["squat"]
        self.assertEqual(IN_LB(st["next_kg"]), 137.5)

    def test_a_skipped_exercise_changes_nothing(self):
        s = squat(MON, 100, [5] * 5)
        s["exercises"][0]["skipped"] = True
        st = self.states(program(), [squat(MON - timedelta(days=2), 95, [5] * 5), s])["squat"]
        self.assertEqual(IN_LB(st["next_kg"]), 100)

    def test_trimmed_match_day_holds_the_weight(self):
        s = squat(MON, 100, [5, 5, 5])
        s["exercises"][0].update(trimmed=True, sets_target=3)
        st = self.states(program(), [squat(MON - timedelta(days=2), 95, [5] * 5), s])["squat"]
        self.assertEqual((IN_LB(st["next_kg"]), st["events"][-1]["kind"]), (100, "hold"))

    def test_adjustments_apply_in_order(self):
        p = program()
        p["adjustments"] = [dict(id="a1", exercise_id="squat", kind="set", kg=LBS(150), day=MON.isoformat(), t=10)]
        seq = [squat(MON, 135, [5] * 5, started=5)]  # the session came before the adjustment the same day
        self.assertEqual(IN_LB(self.states(p, seq)["squat"]["next_kg"]), 150)
        seq.append(squat(MON + timedelta(days=2), 150, [5] * 5, started=20))
        self.assertEqual(IN_LB(self.states(p, seq)["squat"]["next_kg"]), 155)

    def test_climbing_back_after_a_deload_is_progress_not_a_stall(self):
        d = lambda i: MON + timedelta(days=2 * i)  # noqa: E731
        miss = [5, 5, 5, 5, 3]
        seq = [squat(d(0), 200, [5] * 5), squat(d(1), 205, miss), squat(d(2), 205, miss), squat(d(3), 205, miss)]  # -> deload to 180
        seq += [squat(d(4), 180, [5] * 5), squat(d(5), 185, [5] * 5), squat(d(6), 190, [5, 5, 5, 5, 4])]
        self.assertEqual(self.states(program(), seq)["squat"]["status"], "progressing")  # below the old 205, but rising
        seq += [squat(d(7), 190, miss), squat(d(8), 190, miss)]  # a second deload: deloading isn't fixing it
        st = self.states(program(), seq)["squat"]
        self.assertEqual((st["events"][-1]["kind"], st["status"]), ("deload", "stalled"))

    def test_stalls_with_a_higher_fail_limit(self):
        p = program(squat=dict(fail_limit=6))
        d = lambda i: MON + timedelta(days=2 * i)  # noqa: E731
        seq = [squat(d(0), 200, [5] * 5)] + [squat(d(i), 205, [5, 5, 5, 5, 3]) for i in range(1, 5)]
        self.assertEqual(self.states(p, seq)["squat"]["status"], "stalled")  # 205 four times, no new high for 3

    def test_missed_time_restart_suggestions(self):
        last = MON.isoformat()
        self.assertEqual([S.restart_pct(last, MON + timedelta(days=d)) for d in (6, 7, 13, 14, 27, 28, 60)], [0, 10, 10, 20, 20, 30, 30])
        self.assertEqual(S.restart_pct(None, MON), 0)

    def test_restart_is_only_applied_when_accepted(self):
        p, seq = program(), [squat(MON, 135, [5] * 5)]
        later = MON + timedelta(days=15)
        keep = S.start(p, seq, ST, "lb", later, "B")
        restart = S.start(p, seq, ST, "lb", later, "B", restart=True)
        self.assertEqual(IN_LB(keep["exercises"][0]["kg"]), 140)
        self.assertEqual(IN_LB(restart["exercises"][0]["kg"]), 110)  # 140 x 0.8 = 112, down to 110
        self.assertIn("restart", restart["exercises"][0]["adjust"])

    def test_lighter_day_and_match_trim(self):
        p = L.from_template("tennis", "us", "p1")
        a = S.start(p, [], ST, "lb", MON, near_match=True)
        dl = a["exercises"][0]
        self.assertTrue(dl["trimmed"])
        self.assertEqual(dl["sets_target"], 2)
        light = S.start(program(), [squat(MON, 200, [5] * 5)], ST, "lb", MON + timedelta(days=2), "B", lighter=True)
        self.assertEqual(IN_LB(light["exercises"][0]["kg"]), 180)  # 205 x 0.9 = 184.5, rounded down to a loadable 180

    def test_stall_detection(self):
        ok = [dict(kg=100, kind="success"), dict(kg=105, kind="success"), dict(kg=110, kind="success"), dict(kg=115, kind="success")]
        self.assertFalse(S.stalled(ok))
        stuck = [dict(kg=115, kind="success"), dict(kg=115, kind="fail"), dict(kg=110, kind="fail"), dict(kg=115, kind="success")]
        self.assertTrue(S.stalled(stuck))  # three sessions without beating 115, whatever their outcome
        self.assertFalse(S.stalled(stuck[:3]))  # too few sessions to say


class PlatesTest(unittest.TestCase):
    LB = [[45, 4], [35, 1], [25, 1], [10, 2], [5, 2], [2.5, 1]]

    def test_standard_bar(self):
        self.assertEqual(S.plates(135, 45, self.LB)["per_side"], [45])
        self.assertEqual(S.plates(225, 45, self.LB)["per_side"], [45, 45])
        self.assertEqual(S.plates(185, 45, self.LB)["per_side"], [45, 25])
        self.assertEqual(S.plates(150, 45, self.LB)["per_side"], [45, 5, 2.5])

    def test_other_bars(self):
        self.assertEqual(S.plates(135, 35, self.LB)["per_side"], [45, 5])  # a 35 lb bar
        self.assertEqual(S.plates(60, 15, [[20, 2], [10, 2], [5, 2], [2.5, 2], [1.25, 2]])["per_side"], [20, 2.5])  # 15 kg bar
        self.assertEqual(S.plates(100, 20, [[25, 4], [15, 1], [10, 1], [5, 1], [2.5, 1], [1.25, 1]])["per_side"], [25, 15])

    def test_limited_plates_and_unloadable_targets(self):
        r = S.plates(315, 45, [[45, 1], [25, 2], [10, 2], [5, 1]])  # only one pair of 45s
        self.assertEqual((r["total"], r["exact"]), (285.0, False))  # counts are pairs: 45+25+25+10+10+5 = 120 a side at most
        self.assertEqual(r["per_side"], [45, 25, 25, 10, 10, 5])
        odd = S.plates(137, 45, self.LB)
        self.assertEqual((odd["total"], odd["exact"]), (135.0, False))
        top = S.plates(2000, 45, [[45, 2]])
        self.assertEqual(top["total"], 225.0)  # as much as the plates allow

    def test_empty_bar_and_below_bar(self):
        self.assertEqual(S.plates(45, 45, self.LB), dict(total=45, per_side=[], exact=True, below_bar=False))
        self.assertTrue(S.plates(30, 45, self.LB)["below_bar"])

    def test_snap_round_trips_exactly(self):
        for lb in (45, 50, 135, 140, 225, 317.5):
            self.assertEqual(IN_LB(S.snap(LBS(lb), "barbell", ST, "lb")), 315 if lb == 317.5 else lb)
        self.assertEqual(S.to_unit(S.snap(23.3, "dumbbell", ST, "kg"), "kg"), 24)  # 2 kg dumbbell steps
        self.assertEqual(S.snap(50, "bodyweight", ST, "lb"), 0)


class WarmupAndE1rmTest(unittest.TestCase):
    def test_warmups_ramp_to_the_work_weight(self):
        w = S.warmups(LBS(225), L.BY_ID["squat"], ST, "lb")
        lbs = [IN_LB(x["kg"]) for x in w]
        self.assertEqual(lbs[:2], [45, 45])
        self.assertEqual(lbs, sorted(lbs))
        self.assertLess(lbs[-1], 225)
        self.assertEqual([x["reps"] for x in w][-1], 2)

    def test_no_warmups_at_the_bar_or_for_dumbbells(self):
        self.assertEqual(S.warmups(LBS(45), L.BY_ID["squat"], ST, "lb"), [])
        self.assertEqual(S.warmups(LBS(50), L.BY_ID["db_bench"], ST, "lb"), [])
        light = [IN_LB(x["kg"]) for x in S.warmups(LBS(65), L.BY_ID["bench"], ST, "lb")]
        self.assertEqual(light[:2], [45, 45])
        self.assertTrue(all(x <= 55 for x in light))  # every ramp set at least 10 lb under the 65 lb work weight

    def test_epley(self):
        self.assertAlmostEqual(S.e1rm(100, 5), 116.6667, places=3)
        self.assertEqual(S.e1rm(100, 1), 100)
        self.assertIsNone(S.e1rm(100, 0))
        self.assertIsNone(S.e1rm(None, 5))


class ScheduleTest(unittest.TestCase):
    def test_a_b_rotation_and_next_date(self):
        p = program()
        n = S.next_workout(p, [], MON + timedelta(days=1))  # Tuesday, nothing logged
        self.assertEqual((n["workout_key"], n["date"]), ("A", (MON + timedelta(days=2)).isoformat()))
        seq = [squat(MON, 45, [5] * 5, workout="A")]
        self.assertEqual(S.next_workout(p, seq, MON)["workout_key"], "B")
        self.assertEqual(S.next_workout(p, seq, MON)["date"], (MON + timedelta(days=2)).isoformat())  # done today -> Wednesday
        seq.append(squat(MON + timedelta(days=2), 50, [5] * 5, workout="B"))
        self.assertEqual(S.next_workout(p, seq, MON + timedelta(days=3))["workout_key"], "A")

    def test_missed_day_is_reported_not_moved(self):
        p, seq = program(), [squat(MON, 45, [5] * 5)]
        n = S.next_workout(p, seq, MON + timedelta(days=3))  # Thursday; Wednesday was skipped
        self.assertEqual(n["missed"], (MON + timedelta(days=2)).isoformat())
        self.assertEqual(n["date"], (MON + timedelta(days=4)).isoformat())  # still the next scheduled day
        p["shift"] = {"from": n["missed"], "to": (MON + timedelta(days=3)).isoformat()}  # you accepted "do it today"
        self.assertEqual(S.next_workout(p, seq, MON + timedelta(days=3))["date"], (MON + timedelta(days=3)).isoformat())


class TrackingTest(unittest.TestCase):
    def test_series_cards_prs_and_milestones(self):
        seq = [squat(MON + timedelta(days=2 * i), 125 + 5 * i, [5] * 5) for i in range(4)]  # 125..140
        series = S.lift_series("squat", seq)
        self.assertEqual([IN_LB(r["kg"]) for r in series], [125, 130, 135, 140])
        card = S.lift_card(L.BY_ID["squat"], series, None, MON + timedelta(days=7))
        self.assertEqual(IN_LB(card["current_kg"]), 140)
        self.assertEqual(card["status"], "progressing")
        pr = S.prs(seq)
        self.assertNotIn(seq[0]["id"], pr)  # the first session sets the baseline, not a record
        self.assertEqual({x["kind"] for x in pr[seq[1]["id"]]}, {"weight", "e1rm"})
        ms = S.milestones(seq, "lb")
        self.assertEqual([m["value"] for m in ms[seq[2]["id"]]], [135])

    def test_rep_record_at_the_same_weight(self):
        seq = [squat(MON, 135, [5] * 5), squat(MON + timedelta(days=2), 135, [8, 5, 5, 5, 5])]
        kinds = {x["kind"] for x in S.prs(seq)[seq[1]["id"]]}
        self.assertIn("reps", kinds)

    def test_standards_are_optional_and_scaled(self):
        self.assertIsNone(S.standards("squat", 80, None, None))
        self.assertIsNone(S.standards("curl", 80, "men", None))
        lv = S.standards("squat", 80, "men", 30)
        self.assertEqual([x["level"] for x in lv], list(S.LEVELS))
        older = S.standards("squat", 80, "men", 55)
        self.assertLess(older[-1]["kg"], lv[-1]["kg"])

    def test_finish_excludes_paused_time(self):
        a = dict(started=1000.0, paused_s=120.0, paused_at=None, exercises=[], current=0, rest={"x": 1})
        self.assertEqual(S.finish(a, now=4000.0)["duration_s"], 2880)
        a2 = dict(a, paused_at=3900.0)  # still paused when finished
        self.assertEqual(S.finish(a2, now=4000.0)["duration_s"], 2780)
        self.assertNotIn("rest", S.finish(a, now=4000.0))

    def test_clean_active_clamps_and_caps(self):
        bad = dict(id="x", started=time.time() + 10 ** 6, exercises=[dict(exercise_id="squat", load="nuclear", kg=-5, sets_target=999,
                                                                          reps_target=0, sets=[dict(reps=10 ** 6, kg="heavy", note="n" * 999)] * 50)])
        c = S.clean_active(bad)
        e = c["exercises"][0]
        self.assertEqual((e["load"], e["kg"], e["sets_target"], e["reps_target"]), ("machine", 0, 20, 1))
        self.assertEqual(len(e["sets"]), 20)
        self.assertEqual(e["sets"][0]["reps"], 100)
        self.assertEqual(len(e["sets"][0]["note"]), 200)
        self.assertLessEqual(c["started"], time.time() + 61)


# --------------------------------------------------------------------------- API (demo mode, scratch data)
class ApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.env = mock.patch.dict(os.environ, {"GARMIN_DEMO": "1", "ANTHROPIC_API_KEY": ""})
        cls.env.start()
        import server
        from fastapi.testclient import TestClient
        cls.server, cls.c = server, TestClient(server.app, base_url=server.LOCAL_URL, headers={"Origin": server.LOCAL_URL})

    @classmethod
    def tearDownClass(cls):
        cls.env.stop()

    def setUp(self):
        import store
        self.store = store
        store.strength_active_clear()
        for p in store.strength_programs():
            store.strength_program_delete(p["id"])
        for s in store.strength_sessions():
            store.strength_session_delete(s["id"])
        store.settings_put({"units": "us"})

    def new_program(self):
        r = self.c.post("/api/strength/programs", json={"template": "5x5"})
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()

    def do_workout(self, pid, reps_for=lambda e, i: e["reps_target"]):
        a = self.c.post("/api/strength/start", json={"program_id": pid}).json()
        for e in a["exercises"]:
            for i, s in enumerate(e["sets"]):
                s["reps"] = reps_for(e, i)
                s["failed"] = s["reps"] < e["reps_target"]
        self.assertEqual(self.c.put("/api/strength/active", json=a).status_code, 200)
        r = self.c.post("/api/strength/active/finish")
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()

    def test_template_program_stores_kg_and_shows_lb(self):
        p = self.new_program()
        self.assertTrue(p["active"])
        self.assertAlmostEqual(p["lifts"]["squat"]["start_kg"], 20.41166, places=4)  # 45 lb, stored in kg
        a = self.c.post("/api/strength/start", json={"program_id": p["id"]}).json()
        sq = a["exercises"][0]
        self.assertEqual(IN_LB(sq["kg"]), 45)
        self.assertEqual(sq["plates"]["per_side"], [])
        self.assertEqual(a["unit"], "lb")

    def test_autosave_and_recovery_after_a_restart(self):
        p = self.new_program()
        a = self.c.post("/api/strength/start", json={"program_id": p["id"]}).json()
        a["exercises"][0]["sets"][0]["reps"] = 5
        a["exercises"][0]["sets"][1].update(reps=3, failed=True, note="lost balance")
        a["current"] = 1
        self.assertEqual(self.c.put("/api/strength/active", json=a).status_code, 200)
        import store
        importlib.reload(store)  # a fresh process reading the same database
        self.assertEqual(store.strength_active()["exercises"][0]["sets"][1]["note"], "lost balance")
        back = self.c.get("/api/strength/active").json()["active"]
        self.assertEqual((back["current"], back["exercises"][0]["sets"][0]["reps"]), (1, 5))
        self.assertEqual(self.c.post("/api/strength/start", json={"program_id": p["id"]}).status_code, 409)  # resume, don't double up

    def test_autosave_rejects_a_replaced_workout(self):
        p = self.new_program()
        a = self.c.post("/api/strength/start", json={"program_id": p["id"]}).json()
        self.assertEqual(self.c.put("/api/strength/active", json=dict(a, id="someone-else")).status_code, 409)

    def test_finish_saves_once_and_clears_the_active_workout(self):
        p = self.new_program()
        summ = self.do_workout(p["id"])
        self.assertIsNone(self.c.get("/api/strength/active").json()["active"])
        self.assertEqual(len(self.store.strength_sessions()), 1)
        self.assertEqual(self.c.post("/api/strength/active/finish").status_code, 404)
        self.assertEqual(summ["next"]["workout_key"], "B")
        nxt = {e["exercise_id"]: IN_LB(e["kg"]) for e in summ["next"]["exercises"]}
        self.assertEqual(nxt["squat"], 50)
        self.assertGreater(summ["volume_kg"], 0)

    def test_an_empty_workout_cant_be_finished(self):
        p = self.new_program()
        self.c.post("/api/strength/start", json={"program_id": p["id"]})
        self.assertEqual(self.c.post("/api/strength/active/finish").status_code, 422)

    def test_editing_history_replays_cleanly(self):
        p = self.new_program()
        for _ in range(4):  # A, B, A, B - all successful: squat 45, 50, 55, 60
            self.do_workout(p["id"])
        lifts = lambda: {c["exercise_id"]: c for c in self.c.get("/api/strength/lifts").json()["lifts"]}  # noqa: E731
        fresh = lambda: S.lift_states(self.store.strength_program_get(p["id"]), self.store.strength_sessions(),  # noqa: E731
                                      S.settings_with_defaults(None), "lb")
        self.assertEqual(IN_LB(lifts()["squat"]["next_kg"]), 65)
        last = self.store.strength_sessions()[-1]
        exs = last["exercises"]
        exs[0]["sets"][4].update(reps=2, failed=True)  # the last squat session was actually a miss
        preview = self.c.put(f"/api/strength/sessions/{last['id']}?dry_run=true", json={"exercises": exs}).json()
        self.assertFalse(preview["saved"])
        self.assertEqual(IN_LB(lifts()["squat"]["next_kg"]), 65)  # a preview changes nothing
        sq = next(c for c in preview["changes"] if c["exercise_id"] == "squat")
        self.assertEqual((IN_LB(sq["before_kg"]), IN_LB(sq["after_kg"])), (65, 60))
        self.c.put(f"/api/strength/sessions/{last['id']}", json={"exercises": exs})
        self.assertEqual(IN_LB(lifts()["squat"]["next_kg"]), 60)
        self.assertEqual(fresh()["squat"]["streak"], 1)
        # A mid-history correction: later sessions logged what was really lifted, so they still drive the
        # next weight, and the stored state always equals a replay from scratch.
        mid = self.store.strength_sessions()[1]
        mex = mid["exercises"]
        mex[1]["sets"][0].update(reps=1, failed=True)
        self.c.put(f"/api/strength/sessions/{mid['id']}", json={"exercises": mex})
        for ex_id, card in lifts().items():
            if card["in_program"]:
                self.assertAlmostEqual(card["next_kg"], fresh()[ex_id]["next_kg"], places=6)

    def test_deleting_the_only_session_returns_to_start(self):
        p = self.new_program()
        self.do_workout(p["id"])
        sid = self.store.strength_sessions()[0]["id"]
        dry = self.c.delete(f"/api/strength/sessions/{sid}?dry_run=true").json()
        self.assertFalse(dry["deleted"])
        self.assertEqual(len(self.store.strength_sessions()), 1)
        self.c.delete(f"/api/strength/sessions/{sid}")
        a = self.c.post("/api/strength/start", json={"program_id": p["id"]}).json()
        self.assertEqual((a["workout_key"], IN_LB(a["exercises"][0]["kg"])), ("A", 45))

    def test_stall_option_deload_is_an_adjustment(self):
        p = self.new_program()
        self.do_workout(p["id"])
        r = self.c.post(f"/api/strength/programs/{p['id']}/adjust", json={"exercise_id": "squat", "kind": "deload"}).json()
        self.assertEqual(r["adjustments"][0]["kind"], "deload")
        self.assertEqual(IN_LB(r["states"]["squat"]["next_kg"]), 45)  # 45 x 0.9 is under the bar -> the bar

    def test_program_editing_validates(self):
        p = self.new_program()
        good = dict(name="Mine", weekdays=[1, 3], workouts=[dict(key="A", name="Only", exercises=[dict(exercise_id="squat", sets=3, reps=5)])],
                    lifts={"squat": dict(start_kg=LBS(95), increment_kg=LBS(10), deload_pct=15, fail_limit=2)})
        r = self.c.put(f"/api/strength/programs/{p['id']}", json=good)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(list(r.json()["lifts"]), ["squat"])
        bad = dict(good, workouts=[dict(key="A", name="X", exercises=[dict(exercise_id="nope", sets=3, reps=5)])])
        self.assertEqual(self.c.put(f"/api/strength/programs/{p['id']}", json=bad).status_code, 422)
        self.assertEqual(self.c.put(f"/api/strength/programs/{p['id']}", json=dict(good, weekdays=[])).status_code, 422)
        dup = self.c.post(f"/api/strength/programs/{p['id']}/duplicate").json()
        self.assertEqual((dup["name"], dup["active"]), ("Mine (copy)", False))

    def test_future_dates_and_unknown_sessions(self):
        p = self.new_program()
        self.do_workout(p["id"])
        sid = self.store.strength_sessions()[0]["id"]
        tomorrow = (date.today() + timedelta(days=1)).isoformat()
        self.assertEqual(self.c.put(f"/api/strength/sessions/{sid}", json={"day": tomorrow, "exercises": []}).status_code, 422)
        self.assertEqual(self.c.delete("/api/strength/sessions/nope").status_code, 404)

    def test_plates_endpoint_snaps(self):
        r = self.c.get("/api/strength/plates", params={"kg": LBS(137)}).json()
        self.assertEqual((IN_LB(r["kg"]), r["plates"]["per_side"]), (135, [45]))


if __name__ == "__main__":
    unittest.main()
