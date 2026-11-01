"""The AI coach layer with a mocked Anthropic SDK client (no network, scratch data dir): the tool loop
(single call, several tools in one turn, chained turns, step limit, deadline), tool input validation,
privacy filtering of everything sent, the output cache key, structured-output validation and repair,
the rule-based fallbacks (no key, budget cap, API errors), usage/cost logging, coach memory, and the
refusal-fallback request shape.

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
sys.path.insert(0, str(ROOT))

import anthropic  # noqa: E402
import httpx2  # noqa: E402

import ai  # noqa: E402
import coach  # noqa: E402
import coach_schemas  # noqa: E402
import coach_tools  # noqa: E402
import health  # noqa: E402
import privacy  # noqa: E402
import store  # noqa: E402

assert store.DATA_DIR != (ROOT / ".data").resolve(), "tests must never run against the real data folder"
store.db().close()  # open (and create) the scratch DB first, as server.py does at import
TODAY = date(2026, 10, 29)
KEY = "sk-ant-test-key-000000000000"
SECRETS = ("Brooklyn Bridge", "987654321", "a very private check-in note", "1990-04-02", "40.71280", "-74.00600",
           "Jordan Example", "jordan@example.com", "my private goal note")


# ------------------------------- fake SDK client ------------------------------- #
def usage(i=100, o=50, cr=0, cw=0):
    return NS(input_tokens=i, output_tokens=o, cache_read_input_tokens=cr, cache_creation_input_tokens=cw)


def text(t, stop="end_turn", **u):
    return NS(stop_reason=stop, content=[NS(type="text", text=t)], usage=usage(**u), model="claude-sonnet-5-5")


def tool_calls(*calls, **u):
    return NS(stop_reason="tool_use", usage=usage(**u), model="claude-sonnet-5-5",
              content=[NS(type="tool_use", id=f"tu{i}", name=n, input=inp) for i, (n, inp) in enumerate(calls)])


def _plain(o):
    if isinstance(o, NS):
        return vars(o)
    return str(o)


class FakeClient:
    """Answers from a script; records every request as JSON (what would go over the wire)."""

    def __init__(self, script):
        self.script, self.requests, self.endpoints = list(script), [], []
        self.messages = NS(create=lambda **kw: self._create("messages", kw))
        self.beta = NS(messages=NS(create=lambda **kw: self._create("beta", kw)))

    def _create(self, endpoint, kw):
        self.endpoints.append(endpoint)
        self.requests.append(json.loads(json.dumps(kw, default=_plain)))
        r = self.script.pop(0)
        if isinstance(r, BaseException):
            raise r
        return r

    def wire(self) -> str:
        return json.dumps(self.requests)


def api_error(cls, status, etype):
    req = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    return cls(f"{etype} for {KEY}", response=httpx2.Response(status, request=req), body={"error": {"type": etype, "message": "x"}})


class Base(unittest.TestCase):
    def setUp(self):
        self.env = mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": KEY, "COACH_MODEL": "", "COACH_MODEL_DEEP": "", "COACH_MODEL_FAST": ""})
        self.env.start()
        ai._client = None
        ai._failed.clear()
        store.settings_put(dict(ai_model_deep=None, ai_model_fast=None, ai_routes=None, ai_cap_usd=None, share_checkins=False))
        store.coach_forget()

    def tearDown(self):
        self.env.stop()
        ai._client = None

    def fake(self, *script) -> FakeClient:
        f = FakeClient(script)
        p = mock.patch.object(ai, "make_client", return_value=f)
        p.start()
        self.addCleanup(p.stop)
        return f


# ------------------------------- data sources -------------------------------- #
def day_rows(a, b):
    out, d = {}, a
    while d <= b:
        out[d.isoformat()] = dict(date=d.isoformat(), readiness=70, hrv=55, sleep_h=7.2, rhr=52, load=90, steps=9000,
                                  activities=[dict(id=987654321, name="Brooklyn Bridge Run", type="running", start=f"{d} 07:00:00",
                                                   dist_m=8046.7, secs=2400, hr=150, load=120, te=3.1, lat=40.71280, lon=-74.00600)])
        d += timedelta(days=1)
    return out


def sources(sharing=False, **over):
    base = dict(
        today=lambda: TODAY, day_rows=day_rows, assessment=lambda: dict(readiness=70, zone="good"),
        activity_stats=lambda aid: dict(activity=dict(type="running", date="2026-10-28", distance_km=8.05, avg_pace_s_per_km=298,
                                                      name="Brooklyn Bridge Run"), comparison=dict(n=4, pace_trend="faster"),
                                        recovery_context={}) if aid == 987654321 else None,
        body=lambda: dict(series=[dict(date="2026-10-20", kg=80.0, fat=18.0, muscle_kg=35.0)], summary=None, fitness_age=None,
                          displayName="Jordan Example", birthDate="1990-04-02"),
        dexa=lambda: None, goals=lambda: [], plans=lambda: [], strength=lambda ex, a, b: [], exercises=lambda: ["Back squat"],
        checkins=lambda a, b: [dict(day="2026-10-27", ratings={"mood": 1}, tags=["Sore"], note="a very private check-in note")],
        sharing=lambda: sharing, records=lambda: None,
        profile=lambda: dict(injuries="left knee", email="jordan@example.com"), system=lambda: "us")
    base.update(over)
    return coach_tools.Sources(**base)


CHAT_OK = json.dumps(dict(answer="Easy 30 minutes today.", remember=[]))


# ---------------------------------- tool loop ---------------------------------- #
class ToolLoop(Base):
    def test_single_call_no_tools(self):
        f = self.fake(text(CHAT_OK))
        r = ai.call("chat", ["rules"], "hi", schema=coach_schemas.SCHEMAS["chat"], validate=coach_schemas.validator("chat"))
        self.assertEqual(r["data"]["answer"], "Easy 30 minutes today.")
        self.assertEqual((r["steps"], len(f.requests)), (1, 1))
        self.assertNotIn("tools", f.requests[0])

    def test_two_tools_in_one_turn_return_together(self):
        f = self.fake(tool_calls(("get_daily_metrics", dict(start="2026-10-20", end="2026-10-28", metrics=["hrv", "sleep_h"])),
                                 ("get_activities", dict(start="2026-10-25", end="2026-10-28", type="run"))),
                      text(CHAT_OK))
        tb = coach_tools.Toolbox(sources())
        r = ai.call("chat", ["rules"], "how am I?", schema=coach_schemas.SCHEMAS["chat"], validate=coach_schemas.validator("chat"),
                    tools=coach_tools.definitions(tb.src), run_tool=tb.run)
        self.assertEqual(r["steps"], 2)
        last = f.requests[1]["messages"][-1]
        self.assertEqual(last["role"], "user")
        self.assertEqual([b["tool_use_id"] for b in last["content"]], ["tu0", "tu1"])  # one message, both results
        res = json.loads(last["content"][0]["content"])
        self.assertEqual(res["status"], "ok")
        self.assertIn("summary", res)

    def test_chained_turns(self):
        f = self.fake(tool_calls(("get_activities", dict(start="2026-10-27", end="2026-10-28", type="any"))),
                      tool_calls(("get_activity_detail", dict(ref="a1"))), text(CHAT_OK))
        tb = coach_tools.Toolbox(sources())
        ai.call("chat", ["rules"], "my last run?", schema=coach_schemas.SCHEMAS["chat"], validate=coach_schemas.validator("chat"),
                tools=coach_tools.definitions(tb.src), run_tool=tb.run)
        detail = json.loads(f.requests[2]["messages"][-1]["content"][0]["content"])
        self.assertEqual(detail["status"], "ok")
        self.assertEqual(detail["activity"]["distance_mi"], 5.0)  # 8.05 km, in US units
        self.assertEqual(tb.calls, ["get_activities", "get_activity_detail"])

    def test_step_limit_forces_an_answer(self):
        loop = tool_calls(("get_goals", {}))
        f = self.fake(loop, loop, text(CHAT_OK))
        tb = coach_tools.Toolbox(sources())
        r = ai.call("chat", ["rules"], "q", schema=coach_schemas.SCHEMAS["chat"], validate=coach_schemas.validator("chat"),
                    tools=coach_tools.definitions(tb.src), run_tool=tb.run, max_steps=3)
        self.assertEqual(r["steps"], 3)
        self.assertNotIn("tool_choice", f.requests[1])
        self.assertEqual(f.requests[2]["tool_choice"], {"type": "none"})  # last step: answer with what you have

    def test_deadline_forces_an_answer(self):
        f = self.fake(text(CHAT_OK))
        tb = coach_tools.Toolbox(sources())
        ai.call("chat", ["rules"], "q", schema=coach_schemas.SCHEMAS["chat"], validate=coach_schemas.validator("chat"),
                tools=coach_tools.definitions(tb.src), run_tool=tb.run, deadline_s=-1)
        self.assertEqual(f.requests[0]["tool_choice"], {"type": "none"})

    def test_usage_summed_logged_and_priced(self):
        before = len(store.ai_usage())
        self.fake(tool_calls(("get_goals", {}), i=1200, o=40, cw=1500), text(CHAT_OK, i=300, o=120, cr=1500))
        tb = coach_tools.Toolbox(sources())
        r = ai.call("chat", ["rules"], "q", schema=coach_schemas.SCHEMAS["chat"], validate=coach_schemas.validator("chat"),
                    tools=coach_tools.definitions(tb.src), run_tool=tb.run)
        self.assertEqual(r["usage"], dict(input=1500, output=160, cache_read=1500, cache_write=1500))
        row = store.ai_usage()[-1]
        self.assertEqual(len(store.ai_usage()), before + 1)
        self.assertEqual((row["feature"], row["model"], row["steps"], row["ok"]), ("chat", "claude-sonnet-5-5", 2, 1))
        want = (1500 * 2.0 + 160 * 10.0 + 1500 * 2.5 + 1500 * 0.10) / 1e6  # sonnet 5.5: in, out, cache write, cache read
        self.assertAlmostEqual(row["cost_usd"], want, places=8)
        self.assertGreater(ai.usage_summary()["totals"]["cache_read"], 0)

    def test_cache_breakpoints_on_tools_and_system(self):
        f = self.fake(text(CHAT_OK))
        tb = coach_tools.Toolbox(sources())
        ai.call("chat", ["instructions", "memory"], "q", schema=coach_schemas.SCHEMAS["chat"], validate=coach_schemas.validator("chat"),
                tools=coach_tools.definitions(tb.src), run_tool=tb.run)
        req = f.requests[0]
        self.assertEqual(req["tools"][-1]["cache_control"], {"type": "ephemeral"})
        self.assertTrue(all(t["strict"] for t in req["tools"]))
        self.assertEqual([b.get("cache_control") for b in req["system"]], [{"type": "ephemeral"}] * 2)
        self.assertEqual(req["output_config"]["format"]["type"], "json_schema")

    def test_routing_and_refusal_fallback_shape(self):
        f = self.fake(text("A short takeaway."), text(CHAT_OK))
        ai.call("insights", ["rules"], "q")  # fast tier: haiku, no server-side fallback
        ai.call("chat", ["rules"], "q", schema=coach_schemas.SCHEMAS["chat"], validate=coach_schemas.validator("chat"))
        self.assertEqual((f.requests[0]["model"], f.endpoints[0]), ("claude-haiku-5-5", "messages"))
        self.assertEqual(f.requests[0]["output_config"]["effort"], "low")
        self.assertEqual((f.requests[1]["model"], f.endpoints[1]), ("claude-sonnet-5-5", "beta"))
        self.assertEqual((f.requests[1]["fallbacks"], f.requests[1]["betas"]), ("default", [ai.FALLBACK_BETA]))
        store.settings_put(dict(ai_model_deep="claude-opus-5-5", ai_routes={"insights": "deep"}))
        self.assertEqual(ai.route("insights"), ("claude-opus-5-5", "low"))


# ------------------------------ tool input checks ------------------------------ #
class ToolValidation(unittest.TestCase):
    def setUp(self):
        self.tb = coach_tools.Toolbox(sources())

    def err(self, name, inp, words):
        r = self.tb.run(name, inp)
        self.assertIn("error", r, r)
        self.assertIn(words, r["error"])

    def test_rejects_bad_input_without_raising(self):
        m = dict(metrics=["hrv"])
        self.err("get_daily_metrics", dict(start="yesterday", end="2026-10-28", **m), "date like")
        self.err("get_daily_metrics", dict(start="2026-10-28", end="2026-10-01", **m), "on or before")
        self.err("get_daily_metrics", dict(start="2026-01-01", end="2026-10-28", **m), "at most 180 days")
        self.err("get_daily_metrics", dict(start="2026-10-01", end="2026-10-28", metrics=["vo2max"]), "metrics must be")
        self.err("get_daily_metrics", dict(start="2026-10-01", end="2026-10-28"), "Missing field")
        self.err("get_daily_metrics", dict(start="2026-10-01", end="2026-10-28", metrics=["hrv"], sql="DROP"), "Unknown field")
        self.err("get_activity_detail", dict(ref="a99"), "Unknown ref")
        self.err("get_activities", dict(start="2026-10-01", end="2026-10-28", type="rowing"), "type must be")
        self.err("get_strength_history", dict(exercise="Bench press", start="2026-10-01", end="2026-10-28"), "exercise must be")
        self.err("delete_everything", {}, "Unknown tool")
        self.err("get_checkins", dict(start="2026-10-01", end="2026-10-28"), "Unknown tool")  # sharing off: no such tool

    def test_limits_and_status(self):
        r = self.tb.run("get_daily_metrics", dict(start="2026-05-05", end="2026-10-28", metrics=["hrv"]))
        self.assertIn("weekly_averages", r)  # long range comes back as weeks
        self.assertLessEqual(len(r["weekly_averages"]), 26)
        r = self.tb.run("get_activities", dict(start="2026-08-01", end="2026-10-28", type="run"))
        self.assertEqual(len(r["activities"]), coach_tools.MAX_ACTIVITIES)
        stale = coach_tools.Toolbox(sources(today=lambda: TODAY, day_rows=lambda a, b: {k: v for k, v in day_rows(a, b).items() if k <= "2026-10-20"}))
        self.assertEqual(stale.run("get_daily_metrics", dict(start="2026-10-10", end="2026-10-28", metrics=["hrv"]))["status"], "stale")
        empty = coach_tools.Toolbox(sources(day_rows=lambda a, b: {}))
        self.assertEqual(empty.run("get_activities", dict(start="2026-10-10", end="2026-10-28", type="any"))["status"], "no_data")


# ----------------------------------- privacy ----------------------------------- #
class Privacy(Base):
    def test_nothing_identifying_reaches_the_api(self):
        store.coach_profile_put(dict(injuries="Left knee, call me at jordan@example.com", sports="tennis"))
        f = self.fake(tool_calls(("get_activities", dict(start="2026-10-27", end="2026-10-28", type="any")),
                                 ("get_body_composition", dict(start="2026-10-01", end="2026-10-28")),
                                 ("get_user_profile", {})),
                      tool_calls(("get_activity_detail", dict(ref="a1"))), text(CHAT_OK))
        snap = dict(history=[dict(date="2026-10-28", hrv=55)], activities=[dict(id=987654321, name="Brooklyn Bridge Run", type="running")])
        a = dict(readiness=70, zone="good", recommendation="Moderate", components={}, strain={}, flags=[])
        prof = dict(goals=[dict(label="Run a half", notes="my private goal note")])
        coach.ask("How was my run?", snap, a, prof, "us", coach_tools.Toolbox(sources()))
        wire = f.wire()
        for s in SECRETS:
            self.assertNotIn(s, wire)
        self.assertIn("a1", wire)
        self.assertNotIn("get_checkins", wire)  # sharing off: the tool doesn't exist

    def test_checkins_only_when_sharing_ratings_only(self):
        f = self.fake(tool_calls(("get_checkins", dict(start="2026-10-20", end="2026-10-28"))), text(CHAT_OK))
        tb = coach_tools.Toolbox(sources(sharing=True))
        ai.call("chat", ["rules"], "q", schema=coach_schemas.SCHEMAS["chat"], validate=coach_schemas.validator("chat"),
                tools=coach_tools.definitions(tb.src), run_tool=tb.run)
        self.assertIn("get_checkins", [t["name"] for t in f.requests[0]["tools"]])
        res = json.loads(f.requests[1]["messages"][-1]["content"][0]["content"])
        self.assertEqual(res["checkins"][0]["ratings"], {"mood": 1})
        self.assertNotIn("private check-in note", f.wire())

    def test_payload_scrub(self):
        out = json.loads(coach._data(dict(displayName="Jordan Example", birthDate="1990-04-02", activity=dict(name="Brooklyn Bridge Run",
                                         id=987654321, distance_km=5.0, position_lat=40.71280), notes="my private goal note"), "us"))
        self.assertEqual(out, dict(activity=dict(distance_mi=3.11)))


# --------------------------------- output cache -------------------------------- #
class CacheKey(Base):
    def test_key_changes_only_when_inputs_do(self):
        k = lambda **kw: ai.cache_key("briefing", kw.get("i", {"hrv": 55}), kw.get("mem", "m1"), kw.get("x", "us"))
        self.assertEqual(k(), k())
        self.assertNotEqual(k(), k(i={"hrv": 56}))
        self.assertNotEqual(k(), k(mem="m2"))
        self.assertNotEqual(k(), k(x="metric"))
        self.assertEqual(ai.cache_key("x", {"a": 1, "b": 2}), ai.cache_key("x", {"b": 2, "a": 1}))  # canonical JSON
        base = k()
        store.settings_put(dict(ai_model_deep="claude-opus-5-5"))
        self.assertNotEqual(base, k())  # a different model is a different answer

    def test_cached_hit_refresh_and_failures(self):
        calls = []
        produce = lambda: calls.append(1) or dict(source="claude", text="answer")
        key = ai.cache_key("review", {"week": "2026-10-19"})
        self.assertFalse(ai.cached("review", key, produce).get("cached"))
        hit = ai.cached("review", key, produce)
        self.assertTrue(hit["cached"])
        self.assertEqual(len(calls), 1)  # unchanged data: no second call
        ai.cached("review", key, produce, refresh=True)
        self.assertEqual(len(calls), 2)  # Regenerate bypasses
        key2 = ai.cache_key("review", {"week": "2026-10-12"})
        ai.cached("review", key2, lambda: dict(source="rules", text="fallback"))
        self.assertIsNone(store.ai_cache_get(key2))  # fallbacks are never pinned


# ------------------------------- structured output ------------------------------ #
BRIEF = dict(verdict="Steady aerobic day", readiness_note="Readiness 70: HRV 55 ms in range, sleep 7.2 h.",
             plan=dict(type="Easy run", duration_min=45, intensity="easy", details="Zone 2, conversational."),
             recovery=["Bed by 10:30"], watch_outs=[], goals=[], reasons=["HRV in range"])


class Structured(Base):
    def test_validate(self):
        self.assertIsNone(coach_schemas.validate("briefing", BRIEF))
        self.assertIn("missing", coach_schemas.validate("briefing", {k: v for k, v in BRIEF.items() if k != "plan"}))
        self.assertIn("should be a list", coach_schemas.validate("briefing", dict(BRIEF, recovery="sleep")))
        self.assertIn("one of", coach_schemas.validate("briefing", dict(BRIEF, plan=dict(BRIEF["plan"], intensity="insane"))))
        self.assertIsNone(coach_schemas.validate("briefing", dict(BRIEF, plan=dict(BRIEF["plan"], intensity="Easy"))))  # case-insensitive
        self.assertIn("too long", coach_schemas.validate("briefing", dict(BRIEF, verdict="x" * 500)))
        self.assertIn("keep it to", coach_schemas.validate("chat", dict(answer="ok", remember=["a", "b", "c"])))
        self.assertIn("unexpected", coach_schemas.validate("chat", dict(answer="ok", remember=[], extra=1)))

    def test_one_repair_then_ok(self):
        f = self.fake(text(json.dumps(dict(BRIEF, recovery="sleep"))), text(json.dumps(BRIEF)))
        r = coach.briefing(dict(history=[dict(date="2026-10-29")], activities=[]), dict(readiness=70), None, "us")
        self.assertEqual(r["source"], "claude")
        self.assertEqual(len(f.requests), 2)
        self.assertIn("failed validation", f.requests[1]["messages"][-1]["content"])

    def test_malformed_twice_falls_back_to_rules(self):
        self.fake(text("not json at all"), text("{}"))
        a = coach.assess(dict(history=[dict(date="2026-10-29", hrv=55, sleep_score=80, bb_high=70, load=50)]))
        r = coach.briefing(dict(history=[dict(date="2026-10-29", hrv=55)], activities=[]), a, None, "us")
        self.assertEqual(r["source"], "rules")
        self.assertIn("didn't match", r["note"])
        self.assertEqual(store.ai_usage()[-1]["error_kind"], "malformed")


# --------------------------------- fallbacks ----------------------------------- #
class Fallbacks(Base):
    def test_no_key_uses_rules_and_never_builds_a_client(self):
        made = mock.Mock()
        with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": ""}), mock.patch.object(ai, "make_client", made):
            a = coach.assess(dict(history=[dict(date="2026-10-29", hrv=55, sleep_score=80, bb_high=70, load=50)]))
            self.assertEqual(coach.briefing(dict(history=[], activities=[]), a, None, "us")["source"], "rules")
            self.assertEqual(coach.ask("hi", dict(history=[]), a)["source"], "rules")
        made.assert_not_called()

    def test_cap_pauses_only_automatic_calls(self):
        store.settings_put(dict(ai_cap_usd=0.01))
        store.ai_usage_add(dict(ts="2026-10-29T08:00:00", day=date.today().isoformat(), feature="chat", model="claude-sonnet-5-5",
                                input=0, output=0, cache_read=0, cache_write=0, cost_usd=0.5, steps=1, ok=1, error_kind=None))
        f = self.fake(text(json.dumps(BRIEF)))
        a = dict(readiness=70, zone="good", recommendation="Moderate", components={k: dict(score=None, note="") for k in ("hrv", "sleep", "battery", "load")},
                 strain=dict(ratio=None), flags=[])
        auto = coach.briefing(dict(history=[], activities=[]), a, None, "us", essential=False)
        self.assertEqual(auto["source"], "rules")
        self.assertIn("cap", auto["note"])
        self.assertEqual(f.requests, [])
        clicked = coach.briefing(dict(history=[], activities=[]), a, None, "us", essential=True)
        self.assertEqual(clicked["source"], "claude")
        self.assertTrue(ai.usage_summary()["cap_reached"])

    def test_error_classes_in_plain_words(self):
        req = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
        cases = [(api_error(anthropic.AuthenticationError, 401, "authentication_error"), "invalid_key"),
                 (api_error(anthropic.APIStatusError, 402, "billing_error"), "billing"),
                 (api_error(anthropic.RateLimitError, 429, "rate_limit_error"), "rate_limited"),
                 (api_error(anthropic.OverloadedError, 529, "overloaded_error"), "overloaded"),
                 (api_error(anthropic.NotFoundError, 404, "not_found_error"), "model_not_found"),
                 (anthropic.APITimeoutError(request=req), "timeout"), (anthropic.APIConnectionError(request=req), "network")]
        for ex, kind in cases:
            with self.subTest(kind=kind):
                ai._client = None
                self.fake(ex)
                with self.assertRaises(ai.AIError) as cm:
                    ai.call("chat", ["rules"], "q")
                self.assertEqual(cm.exception.kind, kind)
                self.assertEqual(ai.last_status()["error"]["kind"], kind)
                self.assertNotIn(KEY, json.dumps(cm.exception.info))
        self.assertTrue(any("AI coach" in e["source"] for e in health.events()))

    def test_failed_automatic_call_backs_off(self):
        f = self.fake(api_error(anthropic.OverloadedError, 529, "overloaded_error"), text(CHAT_OK))
        for _ in range(3):  # three page opens: one real attempt, then no calls
            with self.assertRaises(ai.AIError) as cm:
                ai.call("chat", ["rules"], "same data", essential=False)
        self.assertEqual(cm.exception.kind, "backoff")
        self.assertEqual(len(f.requests), 1)
        ai.call("chat", ["rules"], "same data")  # a click (essential) still runs
        self.assertEqual(len(f.requests), 2)

    def test_refusal_is_a_fallback_not_a_crash(self):
        self.fake(text("", stop="refusal"))
        a = coach.assess(dict(history=[dict(date="2026-10-29", hrv=55, sleep_score=80, bb_high=70, load=50)]))
        r = coach.briefing(dict(history=[], activities=[]), a, None, "us")
        self.assertEqual(r["source"], "rules")


# ----------------------------------- memory ------------------------------------ #
class Memory(Base):
    @classmethod
    def setUpClass(cls):
        os.environ["GARMIN_DEMO"] = "1"
        os.environ.setdefault("COACH_CACHE_DIR", tempfile.mkdtemp(prefix="coach-cache-"))
        from fastapi.testclient import TestClient
        import server
        cls.server, cls.c = server, TestClient(server.app, base_url=server.LOCAL_URL, headers={"Origin": server.LOCAL_URL})

    def test_profile_notes_forget_and_export(self):
        c = self.c
        m = c.put("/api/coach/profile", json=dict(injuries="Left knee", sports="Tennis Tue/Sat", tone="direct")).json()
        self.assertIn("Left knee", m["prompt_preview"])
        self.assertIn("tone direct", m["prompt_preview"])
        n = c.post("/api/coach/notes", json=dict(text="Knee flares up after running on pavement")).json()["notes"][0]
        c.post("/api/coach/notes", json=dict(text="knee flares up after running on pavement"))  # duplicate ignored
        self.assertEqual(len(c.get("/api/coach/profile").json()["notes"]), 1)
        c.put(f"/api/coach/notes/{n['id']}", json=dict(text="Knee flares up on concrete"))
        self.assertIn("Knee flares up on concrete", coach.memory_block())
        meta = self.export_meta()
        self.assertEqual(meta["coach_notes"][0]["text"], "Knee flares up on concrete")
        self.assertEqual(meta["coach_profile"]["injuries"], "Left knee")
        self.assertEqual(c.delete(f"/api/coach/notes/{n['id']}").status_code, 200)
        self.assertEqual(c.post("/api/coach/forget").status_code, 422)  # needs confirm
        c.post("/api/coach/forget?confirm=true")
        self.assertEqual(coach.memory_block(), "")

    def export_meta(self):
        import zipfile
        j = self.c.get("/api/export?save=false")
        path = Path(self.server.CACHE / "export").glob("*.zip")
        with zipfile.ZipFile(sorted(path)[-1]) as z:
            return json.loads(z.read("metadata.json"))

    def test_chat_suggests_but_never_saves(self):
        self.fake(text(json.dumps(dict(answer="Noted - keep runs on trails.", remember=["Knee hurts after pavement runs"]))))
        r = self.c.post("/api/coach/ask", json=dict(question="My knee hurts after pavement runs, what should I do?")).json()
        self.assertEqual(r["remember"], ["Knee hurts after pavement runs"])
        self.assertEqual(store.coach_notes(), [])  # only saved through POST /api/coach/notes

    def test_memory_goes_in_the_cached_system_block(self):
        store.coach_profile_put(dict(injuries="Achilles tendinopathy"))
        f = self.fake(text(CHAT_OK))
        coach.ask("q", dict(history=[]), dict(readiness=70), None, "us")
        sys_blocks = f.requests[0]["system"]
        self.assertIn("Achilles", sys_blocks[1]["text"])
        self.assertEqual(sys_blocks[1]["cache_control"], {"type": "ephemeral"})
        self.assertNotIn("Achilles", sys_blocks[0]["text"])  # instructions stay cacheable when notes change


if __name__ == "__main__":
    unittest.main()
