"""Traceable answers (source descriptors, number checks, stale backstop), the proactive 'Coach noticed'
rules and limits, and the optional weather/calendar context (mocked HTTP; titles never kept).

Run from the project folder:  .venv/bin/python -m unittest discover -s tests -v
"""
import os
import sys
import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
os.environ.setdefault("COACH_DATA_DIR", tempfile.mkdtemp(prefix="coach-test-"))
sys.path.insert(0, str(ROOT))

import coach_trace as T  # noqa: E402
import coach_tools  # noqa: E402
import context_ext  # noqa: E402
import proactive as P  # noqa: E402
import store  # noqa: E402

assert store.DATA_DIR != (ROOT / ".data").resolve(), "tests must never run against the real data folder"
store.db().close()


def src(i, numbers, **kw):
    return dict(id=f"src{i}", kind=kw.get("kind", "metric"), label=kw.get("label", f"S{i}"), link={}, numbers=set(numbers),
                problem=kw.get("problem"))


class Numbers(unittest.TestCase):
    def test_supported_and_flagged(self):
        s = [src(1, {7.24, 55.0, 510.0, 0.62, 13207.0}), src(2, {150.0, 168.0})]
        r = T.check("Sleep averaged 7.2 h, HRV 55 ms, pace 8:30 /mi on 62% of days, 13,207 steps; zone 150–168 bpm. "
                    "You'll hit 4,100 next week.", s)
        self.assertEqual(r["unverified"], ["4,100"])
        self.assertIn("⟦4,100⟧", r["text"])
        self.assertEqual({c["id"] for c in r["chips"]}, {"src1", "src2"})

    def test_exemptions(self):
        r = T.check("On Oct 24 and 2026-10-20, try 3 easy runs in Zone 2, add 10% at most, lose 0.5-1% a week, "
                    "lights out by 10:30 pm, it's your 2nd week.", [])
        self.assertEqual(r["unverified"], [])

    def test_rounding_tolerance(self):
        s = [src(1, {61.37, 2583.0})]
        self.assertEqual(T.check("HRV 61.4 ms", s)["unverified"], [])
        self.assertEqual(T.check("HRV 61 ms", s)["unverified"], [])
        self.assertEqual(T.check("about 2,600 tokens", s)["unverified"], [])  # within 1% of a big number
        self.assertEqual(T.check("HRV 64 ms", s)["unverified"], ["64"])

    def test_question_numbers_count(self):
        self.assertEqual(T.check("Running 26.2 miles is a big goal.", [], extra=["Can I run 26.2 miles?"])["unverified"], [])

    def test_numbers_in_strings(self):
        self.assertEqual(T.numbers_in({"pace": "8:30", "t": "1:02:03", "x": [1, {"y": 2.5}], "b": True}), {510.0, 3723.0, 1.0, 2.5})

    def test_describe_labels_and_links(self):
        d = T.describe("get_daily_metrics", dict(start="2026-10-20", end="2026-10-26", metrics=["sleep_h"]), {"status": "ok"})
        self.assertEqual(d["label"], "Sleep, Oct 20 to 26")
        self.assertEqual(d["link"], dict(kind="metric", metric="sleep_h", start="2026-10-20", end="2026-10-26"))
        a = T.describe("get_activity_detail", dict(ref="a1"), {"activity": {"type": "running", "date": "2026-10-24"}}, activity_id=42)
        self.assertEqual((a["label"], a["link"]), ("Running on Oct 24", dict(kind="activity", id=42)))
        self.assertIsNone(T.describe("get_goals", {}, {"error": "x"}))

    def test_stale_backstop(self):
        bad = [src(1, {55.0}, problem="HRV: data is stale, latest data 2026-10-24")]
        note = T.stale_note(bad, "Your HRV is 55 ms.")
        self.assertIn("out of date", note["text"])
        self.assertEqual(note["chip"]["link"], {"kind": "status"})
        self.assertIsNone(T.stale_note(bad, "Your HRV data is stale since Oct 24."))
        self.assertIsNone(T.stale_note([src(1, {55.0})], "Your HRV is 55 ms."))

    def test_toolbox_registers_sources_and_feed_problems(self):
        rows = lambda a, b: {str(a + timedelta(days=i)): dict(hrv=50 + i) for i in range((b - a).days + 1)}
        s = coach_tools.Sources(today=lambda: date(2026, 10, 29), day_rows=rows, assessment=dict, activity_stats=lambda a: None,
                                body=lambda: None, dexa=lambda: None, goals=list, plans=list, strength=lambda *a: [], exercises=list,
                                checkins=lambda a, b: [], sharing=lambda: False, records=lambda: None, profile=dict, system=lambda: "us",
                                feeds=lambda: {"hrv": "HRV: sync failed, latest data 2026-10-27"})
        tb = coach_tools.Toolbox(s)
        out = tb.run("get_daily_metrics", dict(start="2026-10-25", end="2026-10-28", metrics=["hrv"]))
        self.assertIn("sync failed", out["data_feed_problem"])
        self.assertEqual(tb.sources[0]["label"], "HRV, Oct 25 to 28")
        self.assertIn(53.0, tb.sources[0]["numbers"])
        self.assertTrue(tb.sources[0]["problem"])


# ------------------------------- proactive rules -------------------------------- #
def hist(hrv=(60, 58, 55, 52), sleep=(7.5, 7.4, 7.6)):
    d0 = date.today() - timedelta(days=len(hrv) - 1)
    return [dict(date=(d0 + timedelta(days=i)).isoformat(), hrv=v, sleep_h=sleep[i % len(sleep)]) for i, v in enumerate(hrv)]


class Proactive(unittest.TestCase):
    def setUp(self):
        store.settings_put(dict(notice_enabled=True, notice_illness=False, notice_snooze={}, notice_quiet_start="22:00", notice_quiet_end="07:00"))
        with store.db() as con:
            con.execute("DELETE FROM coach_notices")
        self.noon = datetime.combine(date.today(), datetime.min.time()).replace(hour=12)

    def test_rules_fire_only_on_notable_data(self):
        self.assertEqual(P.rule_hrv_decline(hist())["type"], "hrv_decline")
        self.assertIsNone(P.rule_hrv_decline(hist(hrv=(60, 58, 59, 52))))
        self.assertEqual(P.rule_short_sleep(hist(sleep=(6.1, 5.9, 6.2)))["facts"]["avg_h"], 6.1)
        self.assertIsNone(P.rule_short_sleep(hist()))
        self.assertTrue(P.rule_load_spike(dict(strain=dict(ratio=1.7))))
        self.assertIsNone(P.rule_load_spike(dict(strain=dict(ratio=1.1))))
        self.assertEqual(len(P.rule_lift_stall([dict(exercise_id="squat", name="Back squat", status="stalled", sessions_since_progress=3, kg=100),
                                                dict(exercise_id="bench", name="Bench", status="progressing", kg=80)])), 1)
        self.assertEqual(len(P.rule_goal_behind([dict(goal=dict(id="g1"), label="Weight", status="behind", status_text="Behind"),
                                                 dict(goal=dict(id="g2"), label="Steps", status="on_track", status_text="On track")])), 1)
        self.assertTrue(P.rule_dexa_lean_drop(dict(change_vs_previous_scan=dict(lean_loss_with_fat_loss=True), latest_scan=dict(date="2026-10-01"))))

    def inputs(self):
        return dict(history=hist(sleep=(6.0, 5.8, 6.1)), assessment=dict(strain=dict(ratio=1.8)),
                    lifts=[dict(exercise_id="squat", name="Back squat", status="stalled", sessions_since_progress=3, kg=100)])

    def test_two_a_day_and_dedupe(self):
        first = P.today_notices(self.inputs(), now=self.noon)
        self.assertEqual(len(first), 2)
        self.assertEqual({n["type"] for n in first}, {"hrv_decline", "load_spike"})  # severity first
        again = P.today_notices(self.inputs(), now=self.noon)
        self.assertEqual([n["id"] for n in again], [n["id"] for n in first])  # same two, no third
        tomorrow = self.noon + timedelta(days=1)
        nxt = P.today_notices(self.inputs(), now=tomorrow)
        self.assertNotIn("hrv_decline", {n["type"] for n in nxt})  # same finding isn't repeated for a week

    def test_snooze_quiet_hours_illness(self):
        P.snooze("hrv_decline", 7)
        self.assertNotIn("hrv_decline", {n["type"] for n in P.today_notices(self.inputs(), now=self.noon)})
        with store.db() as con:
            con.execute("DELETE FROM coach_notices")
        self.assertEqual(P.today_notices(self.inputs(), now=self.noon.replace(hour=23)), [])  # quiet hours: nothing new
        store.settings_put(dict(notice_illness=True))
        self.assertEqual(P.today_notices(self.inputs(), now=self.noon), [])
        with self.assertRaises(ValueError):
            P.snooze("made_up", 7)

    def test_ai_phrasing_falls_back_to_template(self):
        out = P.today_notices(self.inputs(), now=self.noon, phrase=lambda f: (_ for _ in ()).throw(RuntimeError("down")))
        self.assertTrue(all(n["source"] == "rules" and n["text"] for n in out))
        with store.db() as con:
            con.execute("DELETE FROM coach_notices")
        out = P.today_notices(self.inputs(), now=self.noon, phrase=lambda f: "Worded by the coach.")
        self.assertEqual(out[0]["text"], "Worded by the coach.")


# -------------------------------- outside context -------------------------------- #
ICS = """BEGIN:VCALENDAR
BEGIN:VEVENT
SUMMARY:Dentist with Dr. Secret
LOCATION:123 Private St
ATTENDEE:mailto:someone@example.com
DTSTART:20261030T150000Z
DTEND:20261030T160000Z
END:VEVENT
BEGIN:VEVENT
SUMMARY:Standup
DTSTART;TZID=America/New_York:20261026T090000
DTEND;TZID=America/New_York:20261026T093000
RRULE:FREQ=DAILY;COUNT=10
END:VEVENT
BEGIN:VEVENT
SUMMARY:Cancelled thing
STATUS:CANCELLED
DTSTART:20261030T120000Z
DTEND:20261030T130000Z
END:VEVENT
END:VCALENDAR
"""


class Context(unittest.TestCase):
    def setUp(self):
        store.settings_put(dict(ctx_weather_on=False, ctx_calendar_on=False, ctx_calendar_file=None, ctx_weather_city=None))
        context_ext._mem.clear()

    def test_ics_keeps_times_only(self):
        blocks = context_ext.busy_blocks(ICS, date(2026, 10, 29), date(2026, 10, 31))
        self.assertTrue(blocks)
        self.assertTrue(all(set(b) == {"start", "end", "all_day"} for b in blocks))
        self.assertNotIn("Secret", str(blocks))
        self.assertEqual(sum(1 for b in blocks if b["start"].startswith("2026-10-30")), 2)  # dentist + standup, not the cancelled one

    def test_calendar_tool_off_by_default_and_free_windows(self):
        self.assertEqual(context_ext.calendar(date(2026, 10, 30), date(2026, 10, 30))["status"], "no_data")
        f = Path(tempfile.mkdtemp()) / "cal.ics"
        f.write_text(ICS)
        store.settings_put(dict(ctx_calendar_on=True, ctx_calendar_file=str(f)))
        r = context_ext.calendar(date(2026, 10, 30), date(2026, 10, 30))
        self.assertEqual(r["status"], "ok")
        self.assertTrue(r["days"][0]["busy"] and r["days"][0]["free"])
        self.assertNotIn("Dentist", str(r))

    def test_weather_sends_only_rounded_city_coordinates(self):
        geo = mock.Mock(return_value=mock.Mock(raise_for_status=lambda: None, json=lambda: dict(results=[dict(
            name="Portland", admin1="Oregon", country="United States", latitude=45.52345, longitude=-122.67621)])))
        city = context_ext.set_city("Portland, US", get=geo)
        self.assertEqual((city["lat"], city["lon"]), (45.5, -122.7))
        self.assertEqual(geo.call_args.kwargs["params"]["name"], "Portland, US")
        store.settings_put(dict(ctx_weather_on=True))
        today = date.today()
        wx = mock.Mock(return_value=mock.Mock(raise_for_status=lambda: None, json=lambda: dict(daily=dict(
            time=[today.isoformat()], temperature_2m_max=[61], temperature_2m_min=[44], precipitation_probability_max=[70],
            precipitation_sum=[0.2], wind_speed_10m_max=[12], weather_code=[61]))))
        r = context_ext.forecast(today, today, "us", get=wx)
        params = wx.call_args.kwargs["params"]
        self.assertEqual((params["latitude"], params["longitude"]), (45.5, -122.7))
        self.assertNotIn("Portland", str(params))
        self.assertEqual(r["days"][0]["high_f"], 61)
        self.assertNotIn("Portland", str(r))

    def test_tools_absent_when_off(self):
        s = coach_tools.Sources(today=date.today, day_rows=lambda a, b: {}, assessment=dict, activity_stats=lambda a: None, body=lambda: None,
                                dexa=lambda: None, goals=list, plans=list, strength=lambda *a: [], exercises=list, checkins=lambda a, b: [],
                                sharing=lambda: False, records=lambda: None, profile=dict, system=lambda: "us")
        names = {t["name"] for t in coach_tools.definitions(s, chat=True)}
        self.assertFalse({"get_weather", "get_busy_blocks", "propose_goal"} & names)  # no handlers -> no proposals either


if __name__ == "__main__":
    unittest.main()
