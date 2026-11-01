"""Read-only tools the coach can call, over data already on this computer.

Each tool validates its input, reads through `Sources` (thin wrappers over the server's existing
cached data functions; nothing new is fetched for the model), and returns a compact, computed
answer in the user's units with a `status` of ok / no_data / stale and row limits. Every result
goes through privacy.scrub before the model sees it, and activities are named by opaque refs.
Nothing here writes anything.
"""
from __future__ import annotations

import statistics as st
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Callable

import coach_actions
import coach_trace
import privacy
import units

MAX_RANGE_DAYS = 180
MAX_DAY_ROWS = 60
MAX_ACTIVITIES = 25
MAX_STRENGTH_ROWS = 40
STALE_DAYS = 2
METRICS = ("readiness", "hrv", "sleep_h", "sleep_score", "rhr", "bb_high", "bb_low", "stress", "steps", "load",
           "active_min", "weight_kg", "body_fat_pct")
_ROW_KEY = {"active_min": "im", "body_fat_pct": "fat"}  # tool name -> day-row field
ACT_TYPES = ("any", "run", "ride", "swim", "walk", "hike", "strength", "tennis", "other")
_ACT_MATCH = {"run": ("run",), "ride": ("cycl", "bik", "ride"), "swim": ("swim",), "walk": ("walk",), "hike": ("hik",),
              "strength": ("strength", "training"), "tennis": ("tennis", "pickle", "padel", "squash", "badminton")}


@dataclass
class Sources:
    """Callables supplied by the server (or a test). Each returns metric data, or None when it has none."""
    today: Callable[[], date]
    day_rows: Callable[[date, date], dict]
    assessment: Callable[[], dict]
    activity_stats: Callable[[int], dict | None]
    body: Callable[[], dict | None]
    dexa: Callable[[], dict | None]
    goals: Callable[[], list]
    plans: Callable[[], list]
    strength: Callable[[str, date, date], list]
    exercises: Callable[[], list]
    checkins: Callable[[date, date], list]
    sharing: Callable[[], bool]
    records: Callable[[], dict | None]
    profile: Callable[[], dict]
    system: Callable[[], str]
    feeds: Callable[[], dict] = lambda: {}                  # feed -> plain problem text, for feeds that are failing or stale
    weather: Callable[[date, date], dict] | None = None     # only when weather context is on
    calendar: Callable[[date, date], dict] | None = None    # only when calendar context is on
    handlers: "coach_actions.Handlers | None" = None        # proposals (chat threads only)


class ToolInputError(ValueError):
    pass


# ---------------------------------- schemas ---------------------------------- #
DATE = {"type": "string", "format": "date", "description": "YYYY-MM-DD"}


def _tool(name: str, description: str, props: dict) -> dict:
    return dict(name=name, description=description,
                input_schema={"type": "object", "properties": props, "required": list(props), "additionalProperties": False})


TOOLS = [
    _tool("get_daily_metrics", "Daily wellness metrics (readiness, HRV, sleep, resting HR, Body Battery, stress, steps, training load, "
          "intensity minutes, weight, body fat) for a date range. Over 60 days comes back as weekly averages.",
          dict(start=DATE, end=DATE, metrics={"type": "array", "items": {"type": "string", "enum": list(METRICS)}})),
    _tool("get_activities", "Workouts in a date range (type, duration, distance, pace, HR, load, training effect), newest first, at "
          "most 25. Each has a ref for the detail tools. type: any, run, ride, swim, walk, hike, strength, tennis or other.",
          dict(start=DATE, end=DATE, type={"type": "string", "enum": list(ACT_TYPES)})),
    _tool("get_activity_detail", "One workout in depth: splits, pacing, HR zones and drift, sets, recovery impact. Takes a ref from "
          "get_activities.", dict(ref={"type": "string"})),
    _tool("compare_similar_workouts", "How one workout compares with similar recent ones (pace, HR, efficiency trends). Takes a ref "
          "from get_activities.", dict(ref={"type": "string"})),
    _tool("get_body_composition", "Weight, body fat and muscle readings with trends for a date range, plus fitness age.",
          dict(start=DATE, end=DATE)),
    _tool("get_dexa_scans", "DEXA scan results (fat, lean and bone by region, visceral fat) and changes between scans.", {}),
    _tool("get_goals", "The athlete's goals: type, target, deadline.", {}),
    _tool("get_goal_progress", "Progress on each goal: current value, required vs. recent weekly rate, projection, status.", {}),
    _tool("get_training_plan", "Upcoming events and their training plans: this week's and next week's sessions with status.", {}),
    _tool("get_strength_history", "Strength sessions for one exercise (or 'all') in a date range: top sets, volume, estimated 1RM.",
          dict(exercise={"type": "string"}, start=DATE, end=DATE)),
    _tool("get_records", "Personal records (fastest times, longest efforts) and recent new bests.", {}),
    _tool("get_user_profile", "What the athlete told the coach about themselves: injuries, sports, equipment, schedule, preferences.", {}),
]
WEATHER_TOOL = _tool("get_weather", "Daily weather forecast (high/low, rain chance and amount, wind, conditions) for workout planning, "
                     "today up to 14 days ahead.", dict(start=DATE, end=DATE))
CALENDAR_TOOL = _tool("get_busy_blocks", "The athlete's busy times and free daytime windows (07:00-21:00) from their calendar, up to "
                      "14 days, for scheduling suggestions. Times only, no event details.", dict(start=DATE, end=DATE))
FEED_OF = {"hrv": "hrv", "sleep_h": "sleep", "sleep_score": "sleep", "bb_high": "body_battery", "bb_low": "body_battery",
           "stress": "stress", "steps": "steps", "weight_kg": "weight", "body_fat_pct": "weight"}
TOOL_FEEDS = {"get_activities": ["activities"], "get_activity_detail": ["activities"], "compare_similar_workouts": ["activities"],
              "get_body_composition": ["weight", "fitness_age"]}
STATUS_TEXT = {
    "get_daily_metrics": "Looking at your daily metrics…", "get_activities": "Looking at your workouts…",
    "get_activity_detail": "Reading that workout…", "compare_similar_workouts": "Comparing with similar workouts…",
    "get_body_composition": "Checking body composition…", "get_dexa_scans": "Reading your DEXA scans…", "get_goals": "Checking your goals…",
    "get_goal_progress": "Checking goal progress…", "get_training_plan": "Checking your training plan…",
    "get_strength_history": "Looking at your lifts…", "get_checkins": "Reading your check-in ratings…", "get_records": "Checking your records…",
    "get_user_profile": "Reading About me…", "get_weather": "Checking the forecast…", "get_busy_blocks": "Checking your calendar…",
    "offer_followups": "Finishing up…",
}
CHECKINS_TOOL = _tool("get_checkins", "Daily self-reported wellness check-in ratings (good / neutral / poor) and tags. Ratings only.",
                      dict(start=DATE, end=DATE))


def definitions(src: Sources, chat: bool = False) -> list[dict]:
    """The tool list for a request. Optional tools exist only while their setting is on; in a chat
    thread the coach also gets the propose_* tools (buttons only) and offer_followups."""
    out = TOOLS + ([CHECKINS_TOOL] if src.sharing() else []) + ([WEATHER_TOOL] if src.weather else []) + \
        ([CALENDAR_TOOL] if src.calendar else [])
    if chat and src.handlers:
        out = out + coach_actions.TOOLS + [coach_actions.FOLLOWUPS_TOOL]
    return out


# --------------------------------- validation -------------------------------- #
def _day(v, field: str) -> date:
    try:
        return date.fromisoformat(str(v))
    except ValueError:
        raise ToolInputError(f"{field} must be a date like 2026-10-01.")


def _range(inp: dict, today: date) -> tuple[date, date]:
    a, b = _day(inp.get("start"), "start"), _day(inp.get("end"), "end")
    if a > b:
        raise ToolInputError("start must be on or before end.")
    if b > today:
        b = today
    if (b - a).days + 1 > MAX_RANGE_DAYS:
        raise ToolInputError(f"Ask for at most {MAX_RANGE_DAYS} days at a time.")
    if a < today - timedelta(days=3 * 365):
        raise ToolInputError("Only the last three years are available.")
    return a, b


def _check_fields(name: str, inp, tool: dict) -> dict:
    if not isinstance(inp, dict):
        raise ToolInputError("Input must be an object.")
    props = tool["input_schema"]["properties"]
    extra = [k for k in inp if k not in props]
    if extra:
        raise ToolInputError(f"Unknown field(s) for {name}: {', '.join(extra)}.")
    missing = [k for k in props if k not in inp]
    if missing:
        raise ToolInputError(f"Missing field(s): {', '.join(missing)}.")
    return inp


# ----------------------------------- runner ---------------------------------- #
class Toolbox:
    """Runs tool calls for one conversation (holds the activity refs)."""

    def __init__(self, src: Sources, chat: bool = False, thread_id: str | None = None):
        self.src, self.chat, self.thread_id = src, chat, thread_id
        self.refs = privacy.Refs()
        self.sessions: dict[str, tuple] = {}  # "s1" -> (event id, plan workout id); never sent
        self.calls: list[str] = []
        self.sources: list[dict] = []          # what each read returned (for source chips and number checks)
        self.proposals: list[dict] = []
        self.followups: list[str] = []

    def session_ref(self, eid: str, wid: str) -> str:
        for k, v in self.sessions.items():
            if v == (eid, wid):
                return k
        k = f"s{len(self.sessions) + 1}"
        self.sessions[k] = (eid, wid)
        return k

    def run(self, name: str, inp) -> dict:
        tools = {t["name"]: t for t in definitions(self.src, self.chat)}
        self.calls.append(name)
        if name not in tools:
            return dict(error=f"Unknown tool {name}.")
        try:
            _check_fields(name, inp, tools[name])
            if name in coach_actions.TOOL_KIND:
                prop = coach_actions.propose(self.src.handlers, name, inp, self.sessions, self.thread_id)
                self.proposals.append(prop)
                return dict(proposal_id=prop["id"], shown_as=f"a button: {prop['summary']}. Nothing has changed; the athlete decides.")
            if name == "offer_followups":
                self.followups = [" ".join(str(q).split())[:80] for q in (inp.get("questions") or []) if str(q).strip()][:3]
                return dict(ok=True)
            out = getattr(self, "_" + name)(**inp)
        except (ToolInputError, coach_actions.ActionError) as ex:
            return dict(error=str(ex))
        except Exception as ex:  # a data problem must not end the conversation
            return dict(error=f"Couldn't read that data ({type(ex).__name__}).")
        problems = self._feed_problems(name, inp)
        if problems:
            out["data_feed_problem"] = "; ".join(problems)
        out = privacy.scrub(units.localize(out, self.src.system()), keep=frozenset({"ref", "session_ref"}))
        self._register(name, inp, out, problems)
        return out

    def _feed_problems(self, name: str, inp: dict) -> list[str]:
        feeds = self.src.feeds() or {}
        want = [FEED_OF[m] for m in inp.get("metrics") or [] if m in FEED_OF] if name == "get_daily_metrics" else TOOL_FEEDS.get(name, [])
        return [feeds[f] for f in dict.fromkeys(want) if feeds.get(f)]

    def _register(self, name: str, inp: dict, out: dict, problems: list[str]) -> None:
        aid = self.refs.resolve(str(inp.get("ref"))) if inp.get("ref") else None
        d = coach_trace.describe(name, inp, out, aid)
        if not d:
            return
        problem = "; ".join(problems) or (out.get("stale") if out.get("status") == "stale" else None)
        self.sources.append(dict(d, id=f"src{len(self.sources) + 1}", tool=name, numbers=coach_trace.numbers_in(out), problem=problem))

    # ---- helpers
    def _stale(self, latest: str | None) -> str | None:
        if not latest:
            return None
        gap = (self.src.today() - date.fromisoformat(latest[:10])).days
        return f"Latest data is from {latest[:10]} ({gap} days ago)." if gap > STALE_DAYS else None

    def _act(self, a: dict) -> dict:
        secs, dist = a.get("secs") or 0, a.get("dist_m") or 0
        pace = round(secs / dist * 1000) if dist > 200 and secs else None
        return {k: v for k, v in dict(
            ref=self.refs.ref(a["id"]) if a.get("id") is not None else None, date=str(a.get("start") or a.get("date") or "")[:10],
            type=a.get("type"), minutes=round(secs / 60, 1) if secs else None, dist_km=round(dist / 1000, 2) if dist > 200 else None,
            pace_s_per_km=pace, avg_hr=a.get("hr"), training_load=a.get("load"), aerobic_te=a.get("te")).items() if v is not None}

    # ---- tools
    def _get_daily_metrics(self, start, end, metrics):
        a, b = _range(dict(start=start, end=end), self.src.today())
        bad = [m for m in metrics if m not in METRICS]
        if bad or not metrics:
            raise ToolInputError(f"metrics must be some of: {', '.join(METRICS)}.")
        rows = self.src.day_rows(a, b) or {}
        keys = list(dict.fromkeys(metrics))
        days = []
        for k in sorted(rows):
            r = rows[k]
            vals = {m: r.get(_ROW_KEY.get(m, m)) for m in keys}
            if any(v is not None for v in vals.values()):
                days.append(dict(date=k, **{m: v for m, v in vals.items() if v is not None}))
        if not days:
            return dict(status="no_data", reason=f"No {', '.join(keys)} data between {a} and {b}.")
        out = dict(status="ok", start=str(a), end=str(b))
        if len(days) > MAX_DAY_ROWS:  # long ranges: weekly averages keep it compact
            weeks: dict = {}
            for d in days:
                wk = (date.fromisoformat(d["date"]) - timedelta(days=date.fromisoformat(d["date"]).weekday())).isoformat()
                weeks.setdefault(wk, []).append(d)
            out["weekly_averages"] = [dict(week_of=wk, **{m: round(st.mean(v), 2) for m in keys
                                                          if (v := [d[m] for d in ds if d.get(m) is not None])})
                                      for wk, ds in sorted(weeks.items())]
        else:
            out["days"] = days
        out["summary"] = {m: dict(avg=round(st.mean(v), 2), min=min(v), max=max(v), n=len(v))
                          for m in keys if (v := [d[m] for d in days if d.get(m) is not None])}
        if (s := self._stale(days[-1]["date"])):
            out.update(status="stale", stale=s)
        return out

    def _get_activities(self, start, end, type):
        if type not in ACT_TYPES:
            raise ToolInputError(f"type must be one of: {', '.join(ACT_TYPES)}.")
        a, b = _range(dict(start=start, end=end), self.src.today())
        acts = [x for r in (self.src.day_rows(a, b) or {}).values() for x in r.get("activities") or []]
        if type == "other":
            acts = [x for x in acts if not any(s in (x.get("type") or "").lower() for ss in _ACT_MATCH.values() for s in ss)]
        elif type != "any":
            acts = [x for x in acts if any(s in (x.get("type") or "").lower() for s in _ACT_MATCH[type])]
        acts.sort(key=lambda x: str(x.get("start") or ""), reverse=True)
        if not acts:
            return dict(status="no_data", reason=f"No {'' if type == 'any' else type + ' '}workouts between {a} and {b}.")
        return dict(status="ok", count=len(acts), shown=min(len(acts), MAX_ACTIVITIES), activities=[self._act(x) for x in acts[:MAX_ACTIVITIES]])

    def _resolve(self, ref):
        aid = self.refs.resolve(str(ref))
        if aid is None:
            raise ToolInputError("Unknown ref: call get_activities first and use one of its refs.")
        return aid

    def _get_activity_detail(self, ref):
        stats = self.src.activity_stats(self._resolve(ref))
        if not stats:
            return dict(status="no_data", reason="The detail file for that workout isn't available.")
        return dict(status="ok", ref=ref, **{k: v for k, v in stats.items() if k not in ("comparison", "similar_recent")})

    def _compare_similar_workouts(self, ref):
        stats = self.src.activity_stats(self._resolve(ref))
        comp = (stats or {}).get("comparison")
        if not comp:
            return dict(status="no_data", reason="Not enough similar workouts to compare yet.")
        return dict(status="ok", ref=ref, comparison=comp)

    def _get_body_composition(self, start, end):
        a, b = _range(dict(start=start, end=end), self.src.today())
        body = self.src.body() or {}
        pts = [p for p in body.get("series") or [] if str(a) <= str(p.get("date"))[:10] <= str(b)]
        s, fa = body.get("summary"), body.get("fitness_age")
        if not pts and not s and not fa:
            return dict(status="no_data", reason="No weight or body-composition readings yet.")
        out = dict(status="ok")
        if pts:
            step = max(1, len(pts) // 30)  # at most ~30 readings
            out["readings"] = [{k: v for k, v in dict(date=p.get("date"), weight_kg=p.get("kg"), body_fat_pct=p.get("fat"),
                                                       muscle_kg=p.get("muscle_kg")).items() if v is not None} for p in pts[::step]]
        if s:
            out["trend"] = dict(weight_7d_avg_kg=s["avg7"]["kg"], change_30d_kg=s["change_kg"]["d30"], change_90d_kg=s["change_kg"]["d90"],
                                trend_kg_per_week=s["rate"]["kg_per_week"], body_fat_7d_avg_pct=s["avg7"]["fat"], bmi=s["bmi"]["value"])
            if (st_ := self._stale(s["latest"]["date"])):
                out.update(status="stale", stale=st_)
        if fa:
            out["fitness_age"] = dict(fitness_age=fa["fitness_age"], actual_age=fa["age"], achievable=fa["achievable"],
                                      components=[{k: c[k] for k in ("label", "value", "target", "status")} for c in fa["components"]])
        return out

    def _get_dexa_scans(self):
        d = self.src.dexa()
        return dict(status="ok", **d) if d else dict(status="no_data", reason="No DEXA scans imported.")

    def _get_goals(self):
        gv = self.src.goals() or []
        if not gv:
            return dict(status="no_data", reason="No goals set.")
        return dict(status="ok", goals=[dict(type=v["goal"]["type"], label=v["label"][:120], unit=v["unit"], target=v["target"],
                                             deadline=v["goal"].get("deadline")) for v in gv[:12]])

    def _get_goal_progress(self):
        gv = self.src.goals() or []
        if not gv:
            return dict(status="no_data", reason="No goals set.")
        return dict(status="ok", goals=[dict(label=v["label"][:120], unit=v["unit"], start=v["start"], current=v["current"],
                                             target=v["target"], weeks_left=v["weeks_left"], progress_pct=v["progress_pct"],
                                             required_per_week=v["required_per_week"], recent_trend_per_week=v["actual_per_week"],
                                             projected_at_deadline=v["projected"], status=v["status_text"],
                                             warnings=[w["text"] for w in v.get("warnings") or []][:3]) for v in gv[:12]])

    def _get_training_plan(self):
        pl = self.src.plans() or []
        if not pl:
            return dict(status="no_data", reason="No upcoming events with a training plan.")
        today = self.src.today()
        horizon = (today + timedelta(days=13)).isoformat()
        out = []
        for ev, view in pl[:3]:
            ws = [w for w in view["plan"]["workouts"] if today.isoformat() <= w["date"] <= horizon]
            out.append(dict(event=dict(kind=ev["kind"], date=ev["date"], priority=ev.get("priority"), distance_km=round(ev["distance_m"] / 1000, 2)
                                       if ev.get("distance_m") else None, days_left=(date.fromisoformat(ev["date"]) - today).days),
                            next_two_weeks=[{k: v for k, v in dict(session_ref=self.session_ref(ev["id"], w["id"]), date=w["date"], type=w["type"],
                                                                   title=w["title"], intensity=w["intensity"],
                                                                   dist_km=round(w["distance_m"] / 1000, 1) if w.get("distance_m") else None,
                                                                   minutes=w.get("minutes"), state=view["statuses"].get(w["id"], {}).get("state")).items()
                                             if v is not None} for w in ws[:16]]))
        return dict(status="ok", plans=out)

    def _get_strength_history(self, exercise, start, end):
        a, b = _range(dict(start=start, end=end), self.src.today())
        known = self.src.exercises() or []
        ex = str(exercise).strip().lower()
        if ex != "all" and ex not in [e.lower() for e in known]:
            raise ToolInputError(f"exercise must be 'all' or one of: {', '.join(known[:30])}.")
        rows = self.src.strength(ex, a, b) or []
        if not rows:
            return dict(status="no_data", reason=f"No strength sessions for {exercise} between {a} and {b}.")
        return dict(status="ok", shown=min(len(rows), MAX_STRENGTH_ROWS), total=len(rows), sessions=rows[-MAX_STRENGTH_ROWS:])

    def _get_checkins(self, start, end):
        if not self.src.sharing():
            raise ToolInputError("Check-in sharing is off.")
        a, b = _range(dict(start=start, end=end), self.src.today())
        rows = self.src.checkins(a, b) or []
        if not rows:
            return dict(status="no_data", reason=f"No check-ins between {a} and {b}.")
        # ratings and tags only: notes never leave this computer
        return dict(status="ok", checkins=[dict(date=r["day"], ratings=r.get("ratings") or {}, tags=r.get("tags") or [])
                                           for r in rows[-MAX_DAY_ROWS:]])

    def _get_weather(self, start, end):
        a, b = _day(start, "start"), _day(end, "end")
        t = self.src.today()
        if a > b or a < t or (b - t).days > 15:
            raise ToolInputError("Ask for days from today up to 14 days ahead.")
        return self.src.weather(a, b)

    def _get_busy_blocks(self, start, end):
        a, b = _day(start, "start"), _day(end, "end")
        if a > b or (b - a).days > 13:
            raise ToolInputError("Ask for up to 14 days, start before end.")
        return self.src.calendar(a, b)

    def _get_records(self):
        r = self.src.records()
        return dict(status="ok", **r) if r else dict(status="no_data", reason="Records haven't been synced yet.")

    def _get_user_profile(self):
        p = self.src.profile() or {}
        return dict(status="ok", profile=p) if any(p.values()) else dict(status="no_data", reason="The athlete hasn't filled in About me.")
