"""Coach actions: the model can only *propose*. Each propose_* tool validates its input against real data
and stores a pending proposal; nothing changes until you press Apply. Apply runs the same server code
path as doing it by hand (the server registers those handlers), saves what it changed so Undo can put
it back, and logs it. Proposals expire after a week; Undo is offered for a day while the target
hasn't changed since.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Callable

import goals
import store
import workouts

EXPIRE_DAYS = 7
UNDO_HOURS = 24
KINDS = ("swap", "move", "goal", "workout", "checkin_note", "deload", "remember")
REASON = {"type": "string", "description": "One short sentence: why, citing the data."}
DATE = {"type": "string", "format": "date", "description": "YYYY-MM-DD"}


def _tool(name, description, props):
    props = dict(props, reason=REASON)
    return dict(name=name, description=description,
                input_schema={"type": "object", "properties": props, "required": list(props), "additionalProperties": False})


TOOLS = [
    _tool("propose_workout_swap", "Suggest swapping a planned session (session_ref from get_training_plan) for an easier, ~30% shorter one or "
          "a rest day. Only creates a button the athlete can press; nothing changes until they do.",
          dict(session_ref={"type": "string"}, to={"type": "string", "enum": ["easier", "rest"]})),
    _tool("propose_move_session", "Suggest moving a planned session to another day of its plan. Creates a button only.",
          dict(session_ref={"type": "string"}, to_date=DATE)),
    _tool("propose_goal", "Suggest a new goal. target uses the goal type's unit in the athlete's units (null for custom). Creates a button only.",
          dict(type={"type": "string", "enum": list(goals.TYPES)}, title={"type": "string"},
               target={"anyOf": [{"type": "number"}, {"type": "null"}]}, deadline=DATE)),
    _tool("propose_workout", "Suggest a structured workout the athlete can open in the Workout Builder, from a template or a plan "
          "session. template: one of easy_run, tempo, intervals, long_run, easy_ride, ride_intervals, tennis, strength, or 'plan' with "
          "session_ref. Creates a button only.",
          dict(template={"type": "string", "enum": list(workouts.TEMPLATE_ORDER) + ["plan"]}, session_ref={"type": "string"},
               day=DATE)),
    _tool("propose_checkin_note", "Suggest adding a short note to the athlete's daily check-in (e.g. how a session felt, in their words). "
          "Creates a button only.", dict(day=DATE, text={"type": "string"})),
    _tool("propose_deload", "Suggest a deload (about 10% lighter next time) for a lift in the active strength program. Creates a button only.",
          dict(exercise={"type": "string"})),
    _tool("propose_remember", "Suggest saving a short fact the athlete told you about themselves to coach memory (under 120 characters, "
          "third person). Creates a button only.", dict(text={"type": "string"})),
]
FOLLOWUPS_TOOL = dict(name="offer_followups", description="Offer 2-3 short follow-up questions the athlete might ask next (under 60 "
                      "characters each). Call once, at the end.",
                      input_schema={"type": "object", "properties": {"questions": {"type": "array", "items": {"type": "string"}}},
                                    "required": ["questions"], "additionalProperties": False})
TOOL_KIND = {"propose_workout_swap": "swap", "propose_move_session": "move", "propose_goal": "goal", "propose_workout": "workout",
             "propose_checkin_note": "checkin_note", "propose_deload": "deload", "propose_remember": "remember"}


class ActionError(ValueError):
    pass


@dataclass
class Handlers:
    """Server-supplied lookups (validate) and the manual code paths (apply/undo) per kind."""
    session: Callable[[str, str], dict | None]          # (event_id, workout_id) -> plan workout (with plan start / event date)
    lift: Callable[[str], dict | None]                  # exercise name -> {program_id, exercise_id, name}
    apply: dict                                          # kind -> fn(params) -> (result dict, undo state)
    undo: dict                                           # kind -> fn(params, undo state) -> None
    current: dict                                        # kind -> fn(params, result) -> comparable state (for "changed since")
    today: Callable[[], date] = date.today


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%dT%H:%M:%S")


def _summary(kind: str, p: dict, ctx: dict) -> str:
    if kind == "swap":
        return (f"Swap {ctx['title']} on {ctx['when']} for a rest day" if p["to"] == "rest"
                else f"Make {ctx['title']} on {ctx['when']} an easier, shorter session")
    if kind == "move":
        return f"Move {ctx['title']} from {ctx['when']} to {datetime.fromisoformat(p['to_date']):%a %b} {int(p['to_date'][8:])}"
    if kind == "goal":
        return f"Add goal: {p['title']}"
    if kind == "workout":
        return f"Open a {ctx.get('name', 'workout')} in the Workout Builder"
    if kind == "checkin_note":
        return f"Add to your {datetime.fromisoformat(p['day']):%b} {int(p['day'][8:])} check-in: “{p['text']}”"
    if kind == "deload":
        return f"Deload {ctx['name']}: about 10% lighter next session"
    return f"Remember: “{p['text']}”"


def propose(h: Handlers, tool: str, inp: dict, refs: dict, thread_id: str | None) -> dict:
    """Validate and store a pending proposal. Returns what the model sees (never applied here)."""
    kind = TOOL_KIND[tool]
    reason = " ".join(str(inp.get("reason") or "").split())[:240]
    p, ctx = {}, {}
    today = h.today()
    if kind in ("swap", "move") or (kind == "workout" and inp.get("template") == "plan"):
        target = refs.get(str(inp.get("session_ref")))
        if not target:
            raise ActionError("Unknown session_ref: call get_training_plan and use one of its session refs.")
        w = h.session(*target)
        if not w:
            raise ActionError("That plan session no longer exists.")
        if w.get("status") == "skipped" and kind != "workout":
            raise ActionError("That session is already skipped.")
        p.update(event_id=target[0], workout_id=target[1])
        d = date.fromisoformat(w["date"])
        ctx.update(title=w["title"].lower(), when=f"{d:%a %b} {d.day}")
        if kind in ("swap", "move") and w["date"] < today.isoformat():
            raise ActionError("That session is in the past.")
        if kind == "swap":
            p["to"] = inp["to"] if inp.get("to") in ("easier", "rest") else None
            if not p["to"]:
                raise ActionError("to must be easier or rest.")
            if p["to"] == "easier" and w.get("intensity") == "easy":
                raise ActionError("That session is already easy.")
        if kind == "move":
            try:
                nd = date.fromisoformat(str(inp.get("to_date")))
            except ValueError:
                raise ActionError("to_date must be a date.")
            if nd < today or not (w["plan_start"] <= nd.isoformat() <= w["event_date"]) or nd.isoformat() == w["date"]:
                raise ActionError(f"Move it to a future day between {w['plan_start']} and the event ({w['event_date']}).")
            p["to_date"] = nd.isoformat()
    if kind == "goal":
        if inp.get("type") not in goals.TYPES:
            raise ActionError("Unknown goal type.")
        try:
            dl = date.fromisoformat(str(inp.get("deadline")))
        except ValueError:
            raise ActionError("deadline must be a date.")
        if dl <= today:
            raise ActionError("The deadline must be in the future.")
        title = " ".join(str(inp.get("title") or "").split())[:80]
        if not title:
            raise ActionError("Give the goal a short title.")
        p.update(type=inp["type"], title=title, target=inp.get("target"), deadline=dl.isoformat())
    if kind == "workout":
        tpl = inp.get("template")
        if tpl != "plan" and tpl not in workouts.TEMPLATES:
            raise ActionError("Unknown template.")
        p.update(template=tpl, day=str(inp.get("day") or today.isoformat())[:10])
        ctx["name"] = workouts.TEMPLATES[tpl]["name"].lower() if tpl != "plan" else ctx.get("title", "planned session")
    if kind == "checkin_note":
        try:
            d = date.fromisoformat(str(inp.get("day")))
        except ValueError:
            raise ActionError("day must be a date.")
        if d > today or d < today - timedelta(days=30):
            raise ActionError("Check-in notes are for today or the last 30 days.")
        text = " ".join(str(inp.get("text") or "").split())[:300]
        if len(text) < 3:
            raise ActionError("The note is empty.")
        p.update(day=d.isoformat(), text=text)
    if kind == "deload":
        lift = h.lift(str(inp.get("exercise") or ""))
        if not lift:
            raise ActionError("That lift isn't in the active strength program.")
        p.update(program_id=lift["program_id"], exercise_id=lift["exercise_id"])
        ctx["name"] = lift["name"]
    if kind == "remember":
        text = " ".join(str(inp.get("text") or "").split())[:200]
        if len(text) < 3:
            raise ActionError("The fact is empty.")
        if any(n["text"].lower() == text.lower() for n in store.coach_notes()):
            raise ActionError("That's already in coach memory.")
        p["text"] = text
    prop = dict(id=uuid.uuid4().hex[:12], thread_id=thread_id, kind=kind, params=p, reason=reason, summary=_summary(kind, p, ctx),
                status="pending", created=_now(), applied_at=None, undo=None, result=None)
    store.proposal_put(prop)
    return prop


def _expired(p: dict, today: date) -> bool:
    return (today - date.fromisoformat(p["created"][:10])).days > EXPIRE_DAYS


def view(p: dict, today: date | None = None) -> dict:
    """What the page shows: the proposal plus whether Apply / Undo are available."""
    today = today or date.today()
    out = {k: p[k] for k in ("id", "kind", "summary", "reason", "status", "created", "applied_at", "result")}
    out["can_apply"] = p["status"] == "pending" and not _expired(p, today)
    if p["status"] == "pending" and _expired(p, today):
        out["status"] = "expired"
    out["can_undo"] = p["status"] == "applied" and bool(p.get("applied_at")) and \
        datetime.now() - datetime.fromisoformat(p["applied_at"]) < timedelta(hours=UNDO_HOURS)
    return out


def apply(h: Handlers, pid: str, log: Callable[[str], None] = lambda m: None) -> dict:
    p = store.proposal_get(pid)
    if not p:
        raise ActionError("Proposal not found.")
    if p["status"] != "pending":
        raise ActionError(f"This was already {p['status']}.")
    if _expired(p, h.today()):
        raise ActionError("This suggestion has expired; ask the coach again.")
    result, undo = h.apply[p["kind"]](p["params"])
    p.update(status="applied", applied_at=_now(), result=result, undo=undo,
             after=h.current[p["kind"]](p["params"], result) if p["kind"] in h.current else None)
    store.proposal_put(p)
    log(f"Applied: {p['summary']}")
    return p


def dismiss(pid: str) -> dict:
    p = store.proposal_get(pid)
    if not p:
        raise ActionError("Proposal not found.")
    if p["status"] != "pending":
        raise ActionError(f"This was already {p['status']}.")
    p["status"] = "dismissed"
    store.proposal_put(p)
    return p


def undo(h: Handlers, pid: str, log: Callable[[str], None] = lambda m: None) -> dict:
    p = store.proposal_get(pid)
    if not p or p["status"] != "applied":
        raise ActionError("There's nothing to undo.")
    if datetime.now() - datetime.fromisoformat(p["applied_at"]) > timedelta(hours=UNDO_HOURS):
        raise ActionError("Undo is only offered for a day; change it by hand instead.")
    if p["kind"] in h.current and p.get("after") is not None and h.current[p["kind"]](p["params"], p["result"]) != p["after"]:
        raise ActionError("It has been changed since, so undoing could overwrite your edit. Change it by hand instead.")
    h.undo[p["kind"]](p["params"], p.get("undo"), p.get("result"))
    p["status"] = "undone"
    store.proposal_put(p)
    log(f"Undone: {p['summary']}")
    return p
