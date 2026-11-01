"""Coach actions and conversations with a mocked Anthropic SDK (no network, scratch data dir):
proposals are only ever applied by the Apply route (through the same handlers as the manual UI), Undo
restores, Dismiss/expiry/double-apply are refused; conversations stream over SSE, older turns are
summarized, and feedback is stored and summarized without touching the profile.

Run from the project folder:  .venv/bin/python -m unittest discover -s tests -v
"""
import json
import os
import sys
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace as NS
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
os.environ.setdefault("COACH_DATA_DIR", tempfile.mkdtemp(prefix="coach-test-"))
os.environ.setdefault("COACH_CACHE_DIR", tempfile.mkdtemp(prefix="coach-cache-"))
sys.path.insert(0, str(ROOT))

import ai  # noqa: E402
import coach  # noqa: E402
import coach_actions  # noqa: E402
import coach_tools  # noqa: E402
import store  # noqa: E402

assert store.DATA_DIR != (ROOT / ".data").resolve(), "tests must never run against the real data folder"
KEY = "sk-ant-test-key-000000000000"


def usage(i=100, o=40):
    return NS(input_tokens=i, output_tokens=o, cache_read_input_tokens=0, cache_creation_input_tokens=0)


class FakeStream:
    """Context manager like the SDK's MessageStream: yields text events, then the final message."""

    def __init__(self, msg):
        self.msg = msg

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def __iter__(self):
        for b in self.msg.content:
            if b.type == "text":
                for i in range(0, len(b.text), 12):
                    yield NS(type="text", text=b.text[i:i + 12])

    def get_final_message(self):
        return self.msg


def say(text):
    return NS(stop_reason="end_turn", content=[NS(type="text", text=text)], usage=usage(), model="claude-sonnet-5-5")


def calls(*cs):
    return NS(stop_reason="tool_use", usage=usage(), model="claude-sonnet-5-5",
              content=[NS(type="tool_use", id=f"t{i}", name=n, input=inp) for i, (n, inp) in enumerate(cs)])


class Fake:
    def __init__(self, script):
        self.script, self.requests = list(script), []
        m = NS(create=self._create, stream=self._stream)
        self.messages, self.beta = m, NS(messages=m)

    def _next(self, kw):
        self.requests.append(json.loads(json.dumps(kw, default=lambda o: vars(o) if isinstance(o, NS) else str(o))))
        return self.script.pop(0)

    def _create(self, **kw):
        return self._next(kw)

    def _stream(self, **kw):
        return FakeStream(self._next(kw))


class Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ["GARMIN_DEMO"] = "1"
        from fastapi.testclient import TestClient
        import server
        cls.server, cls.c = server, TestClient(server.app, base_url=server.LOCAL_URL, headers={"Origin": server.LOCAL_URL})
        today = date.today()
        r = cls.c.post("/api/events", json=dict(name="Half", date=(today + timedelta(weeks=10)).isoformat(), kind="run",
                                                 distance_m=21097.5, priority="A")).json()
        cls.eid = r.get("id") or r["event"]["id"]
        cls.c.post(f"/api/events/{cls.eid}/plan")

    def setUp(self):
        self.env = mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": KEY, "COACH_MODEL": ""})
        self.env.start()
        ai._client, ai._failed = None, {}
        store.settings_put(dict(ai_cap_usd=None, ai_routes=None, ai_model_deep=None, ai_model_fast=None))

    def tearDown(self):
        self.env.stop()
        ai._client = None

    def fake(self, *script):
        f = Fake(script)
        p = mock.patch.object(ai, "make_client", return_value=f)
        p.start()
        self.addCleanup(p.stop)
        return f

    def plan(self):
        return store.plan_get(self.eid)

    def future_session(self, hard=False):
        today = date.today().isoformat()
        return next(w for w in self.plan()["workouts"] if w["date"] > today and w.get("status") != "skipped"
                    and (w.get("intensity") == "hard") == hard and w["type"] != "race")

    def toolbox(self):
        tb = coach_tools.Toolbox(self.server.coach_sources(chat=True), chat=True, thread_id=f"t-{self._testMethodName}")
        tb.run("get_training_plan", {})
        return tb

    def ref(self, tb, w):
        return next(k for k, v in tb.sessions.items() if v == (self.eid, w["id"]))


# ---------------------------------- proposals ---------------------------------- #
class Proposals(Base):
    def snapshot(self):
        return json.dumps([store.plan_get(self.eid), store.goals(), store.coach_notes(), store.checkins(), store.strength_programs(),
                           store.workouts()], sort_keys=True, default=str)

    def test_propose_tools_never_change_anything(self):
        tb, before = self.toolbox(), self.snapshot()
        w = self.future_session()
        later = (date.fromisoformat(w["date"]) + timedelta(days=1)).isoformat()
        outs = [tb.run("propose_move_session", dict(session_ref=self.ref(tb, w), to_date=later, reason="busy that day")),
                tb.run("propose_workout_swap", dict(session_ref=self.ref(tb, self.future_session(hard=True)), to="easier", reason="HRV low")),
                tb.run("propose_goal", dict(type="sleep_h", title="Sleep 7.5 h a night", target=7.5,
                                            deadline=(date.today() + timedelta(days=60)).isoformat(), reason="short sleep")),
                tb.run("propose_workout", dict(template="tempo", session_ref="", day=date.today().isoformat(), reason="good day")),
                tb.run("propose_checkin_note", dict(day=date.today().isoformat(), text="legs heavy on the hills", reason="you said so")),
                tb.run("propose_remember", dict(text="Prefers runs before 7 am", reason="you told me"))]
        self.assertTrue(all("proposal_id" in o for o in outs), outs)
        self.assertEqual(self.snapshot(), before)  # proposals only
        self.assertEqual([p["status"] for p in store.proposals(f"t-{self._testMethodName}")], ["pending"] * 6)

    def test_invalid_targets_are_refused(self):
        tb = self.toolbox()
        w = self.future_session()
        bad = [("propose_move_session", dict(session_ref="s999", to_date=date.today().isoformat(), reason="x"), "Unknown session_ref"),
               ("propose_move_session", dict(session_ref=self.ref(tb, w), to_date="2001-01-01", reason="x"), "Move it to a future day"),
               ("propose_goal", dict(type="weight", title="x", target=1, deadline="2001-01-01", reason="x"), "future"),
               ("propose_deload", dict(exercise="Underwater basket weaving", reason="x"), "isn't in the active"),
               ("propose_checkin_note", dict(day=(date.today() + timedelta(days=3)).isoformat(), text="hi there", reason="x"), "today or the last")]
        for name, inp, words in bad:
            with self.subTest(name=name):
                self.assertIn(words, tb.run(name, inp)["error"])
        tb2 = coach_tools.Toolbox(self.server.coach_sources(chat=False))
        self.assertIn("Unknown tool", tb2.run("propose_remember", dict(text="x y z", reason="r"))["error"])  # read-only contexts can't propose

    def test_apply_uses_the_manual_path_then_undo(self):
        tb = self.toolbox()
        w = self.future_session()
        later = (date.fromisoformat(w["date"]) + timedelta(days=1)).isoformat()
        pid = tb.run("propose_move_session", dict(session_ref=self.ref(tb, w), to_date=later, reason="busy"))["proposal_id"]
        with mock.patch.object(self.server, "patch_workout", wraps=self.server.patch_workout) as spy:
            v = self.c.post(f"/api/coach/proposals/{pid}/apply").json()
        spy.assert_called_once()
        self.assertEqual(spy.call_args.args[:2], (self.eid, w["id"]))
        self.assertEqual((v["status"], v["can_undo"]), ("applied", True))
        self.assertEqual(next(x for x in self.plan()["workouts"] if x["id"] == w["id"])["date"], later)
        self.assertEqual(self.c.post(f"/api/coach/proposals/{pid}/apply").status_code, 409)  # never twice
        self.assertEqual(self.c.post(f"/api/coach/proposals/{pid}/undo").json()["status"], "undone")
        self.assertEqual(next(x for x in self.plan()["workouts"] if x["id"] == w["id"])["date"], w["date"])

    def test_undo_refused_after_a_manual_edit(self):
        tb = self.toolbox()
        w = self.future_session(hard=True)
        pid = tb.run("propose_workout_swap", dict(session_ref=self.ref(tb, w), to="easier", reason="tired"))["proposal_id"]
        self.c.post(f"/api/coach/proposals/{pid}/apply")
        self.assertEqual(next(x for x in self.plan()["workouts"] if x["id"] == w["id"])["intensity"], "easy")
        self.c.patch(f"/api/plan/{self.eid}/workouts/{w['id']}", json=dict(note="I changed this myself", minutes=33))
        r = self.c.post(f"/api/coach/proposals/{pid}/undo")
        self.assertEqual(r.status_code, 409)
        self.assertIn("changed since", r.json()["detail"])

    def test_goal_remember_note_workout_round_trips(self):
        tb = self.toolbox()
        n_goals, n_notes = len(store.goals()), len(store.coach_notes())
        g = tb.run("propose_goal", dict(type="sleep_h", title="Sleep 7.5 h", target=7.5, deadline=(date.today() + timedelta(days=60)).isoformat(),
                                        reason="r"))["proposal_id"]
        m = tb.run("propose_remember", dict(text="Knee flares up on concrete", reason="r"))["proposal_id"]
        c = tb.run("propose_checkin_note", dict(day=date.today().isoformat(), text="felt strong", reason="r"))["proposal_id"]
        k = tb.run("propose_workout", dict(template="intervals", session_ref="", day=date.today().isoformat(), reason="r"))["proposal_id"]
        for pid in (g, m, c, k):
            self.assertEqual(self.c.post(f"/api/coach/proposals/{pid}/apply").json()["status"], "applied")
        self.assertEqual(len(store.goals()), n_goals + 1)
        self.assertIn("Knee flares up on concrete", [x["text"] for x in store.coach_notes()])
        self.assertIn("felt strong", store.checkin_get(date.today().isoformat())["note"])
        wid = store.proposal_get(k)["result"]["workout_id"]
        self.assertIsNotNone(store.workout_get(wid))
        for pid in (g, m, c, k):
            self.assertEqual(self.c.post(f"/api/coach/proposals/{pid}/undo").json()["status"], "undone")
        self.assertEqual((len(store.goals()), len(store.coach_notes())), (n_goals, n_notes))
        self.assertIsNone(store.workout_get(wid))
        self.assertNotIn("felt strong", (store.checkin_get(date.today().isoformat()) or {}).get("note") or "")

    def test_dismiss_and_expiry(self):
        tb = self.toolbox()
        pid = tb.run("propose_remember", dict(text="Likes hill repeats", reason="r"))["proposal_id"]
        self.assertEqual(self.c.post(f"/api/coach/proposals/{pid}/dismiss").json()["status"], "dismissed")
        self.assertEqual(self.c.post(f"/api/coach/proposals/{pid}/apply").status_code, 409)
        pid2 = tb.run("propose_remember", dict(text="Runs with a dog", reason="r"))["proposal_id"]
        p = store.proposal_get(pid2)
        p["created"] = (date.today() - timedelta(days=9)).isoformat() + "T08:00:00"
        store.proposal_put(p)
        self.assertEqual(coach_actions.view(store.proposal_get(pid2))["status"], "expired")
        self.assertEqual(self.c.post(f"/api/coach/proposals/{pid2}/apply").status_code, 409)


# -------------------------------- conversations -------------------------------- #
class Threads(Base):
    def events(self, r):
        out = []
        for blk in r.text.split("\n\n"):
            ev = next((l[7:] for l in blk.splitlines() if l.startswith("event: ")), None)
            data = next((l[6:] for l in blk.splitlines() if l.startswith("data: ")), None)
            if ev:
                out.append((ev, json.loads(data)))
        return out

    def test_stream_with_tools_proposal_chips_and_check(self):
        tid = self.c.post("/api/coach/threads", json={}).json()["id"]
        d1, d2 = (date.today() - timedelta(days=6)).isoformat(), date.today().isoformat()
        f = self.fake(calls(("get_daily_metrics", dict(start=d1, end=d2, metrics=["sleep_h"]))),
                      calls(("propose_remember", dict(text="Works night shifts on Fridays", reason="you said so")),
                            ("offer_followups", dict(questions=["How do I nap well?", "Move my long run?"]))),
                      say("Your sleep has been short. Try a nap. You slept 11.9 hours once."))
        r = self.c.post(f"/api/coach/threads/{tid}/ask", json=dict(question="I work night shifts on Fridays, how's my sleep?"))
        ev = self.events(r)
        kinds = [k for k, _ in ev]
        self.assertIn("text", kinds)
        self.assertIn("status", kinds)
        self.assertIn("proposal", kinds)
        done = dict(ev)["done"]
        self.assertEqual(done["followups"], ["How do I nap well?", "Move my long run?"])
        self.assertEqual(done["proposals"][0]["kind"], "remember")
        self.assertEqual(done["unverified"], ["11.9"])  # not in any tool result
        self.assertIn("⟦11.9⟧", done["text"])
        self.assertEqual(store.coach_notes() and [n for n in store.coach_notes() if "night shifts" in n["text"]], [])  # not saved
        t = self.c.get(f"/api/coach/threads/{tid}").json()
        self.assertEqual([m["role"] for m in t["messages"]], ["user", "assistant"])
        self.assertEqual(t["messages"][1]["meta"]["proposals"][0]["status"], "pending")
        self.assertTrue(f.requests[0]["system"][1]["cache_control"])

    def test_chips_come_from_contributing_sources(self):
        tid = self.c.post("/api/coach/threads", json={}).json()["id"]
        d1, d2 = (date.today() - timedelta(days=6)).isoformat(), date.today().isoformat()
        rows = self.server._chart_rows(date.fromisoformat(d1), date.fromisoformat(d2), False)[0]
        hrv = next(r["hrv"] for k, r in sorted(rows.items()) if r.get("hrv") is not None)
        self.fake(calls(("get_daily_metrics", dict(start=d1, end=d2, metrics=["hrv"])), ("get_goals", {})),
                  say(f"Your HRV was {hrv} ms at the start of the week."))
        done = dict(self.events(self.c.post(f"/api/coach/threads/{tid}/ask", json=dict(question="HRV?"))))["done"]
        self.assertEqual([c["kind"] for c in done["chips"]], ["metric"])  # get_goals contributed no number
        self.assertTrue(done["chips"][0]["label"].startswith("HRV, "))
        self.assertEqual(done["chips"][0]["link"]["metric"], "hrv")
        self.assertEqual(done["unverified"], [])

    def test_context_preload_and_chip(self):
        tid = self.c.post("/api/coach/threads", json={}).json()["id"]
        self.fake(say("Steady day."))
        done = dict(self.events(self.c.post(f"/api/coach/threads/{tid}/ask", json=dict(
            question="How was it?", context=dict(kind="day", day=date.today().isoformat(), label="Today")))))["done"]
        self.assertTrue(any(c["kind"] == "metric" for c in done["chips"]))

    def test_summarizes_older_turns(self):
        tid = self.c.post("/api/coach/threads", json={}).json()["id"]
        long = "word " * 900
        for i in range(10):
            store.message_add(dict(id=f"m{tid}{i}", thread_id=tid, role="user" if i % 2 == 0 else "assistant", text=f"turn {i} {long}",
                                   meta={}, created="2026-10-29T08:00:00"))
        f = self.fake(say("Summary: they asked about pacing and sleep."), say("Here's my answer."))
        ev = self.events(self.c.post(f"/api/coach/threads/{tid}/ask", json=dict(question="And now?")))
        self.assertIn("done", dict(ev))
        t = store.thread_get(tid)
        self.assertIn("pacing", t["summary"])
        self.assertGreater(t["summarized_upto"], 0)
        sent = f.requests[1]["messages"]
        self.assertIn("Summary of earlier in this conversation", sent[0]["content"])
        self.assertLessEqual(len(sent), coach.KEEP_MESSAGES + 1)
        self.assertNotIn("turn 0 ", json.dumps(sent))  # folded into the summary, not resent
        self.assertNotIn("Summary of earlier", json.dumps(f.requests[1]["system"]))  # system stays cacheable

    def test_rename_delete_and_no_key(self):
        tid = self.c.post("/api/coach/threads", json={}).json()["id"]
        self.assertEqual(self.c.patch(f"/api/coach/threads/{tid}", json=dict(title="Race week")).json()["title"], "Race week")
        with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": ""}):
            ev = self.events(self.c.post(f"/api/coach/threads/{tid}/ask", json=dict(question="hi")))
        self.assertEqual(ev[0][0], "error")
        self.assertEqual(self.c.delete(f"/api/coach/threads/{tid}").status_code, 200)
        self.assertEqual(self.c.get(f"/api/coach/threads/{tid}").status_code, 404)


# ----------------------------------- feedback ----------------------------------- #
class Feedback(Base):
    def test_store_summary_hint_and_profile_untouched(self):
        store.feedback_clear()
        store.coach_profile_put(dict(injuries="left knee"))
        before = store.coach_profile()
        for ref in ("a", "b"):
            self.c.post("/api/coach/feedback", json=dict(feature="thread", ref=ref, rating=-1, reasons=["too_generic"]))
        s = self.c.post("/api/coach/feedback", json=dict(feature="briefing", ref="2026-10-29", rating=1)).json()
        self.assertEqual((s["up"], s["down"], s["by_reason"]["too_generic"]), (1, 2, 2))
        self.assertIn("too generic", s["hint"])
        self.assertIn("too generic", coach.memory_block())
        self.assertEqual(store.coach_profile(), before)
        self.c.post("/api/coach/feedback", json=dict(feature="thread", ref="a", rating=0))  # cleared
        self.assertEqual(self.c.get("/api/coach/feedback").json()["down"], 1)
        self.assertEqual(self.c.delete("/api/coach/feedback").json()["up"], 0)
        self.assertEqual(self.c.post("/api/coach/feedback", json=dict(feature="thread", ref="x", rating=-1, reasons=["mean"])).status_code, 422)


if __name__ == "__main__":
    unittest.main()
