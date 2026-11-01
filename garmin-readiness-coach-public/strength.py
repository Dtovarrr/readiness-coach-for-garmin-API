"""Strength mode: linear progression, plates, warm-ups, schedule, tracking and PRs (pure logic, no I/O).

Loads are stored in kg (CLAUDE.md: store metric). Every target is snapped to a weight you can actually
load in your display unit - plates on your bar, your dumbbell steps, your machine's stack steps - so a
5 lb jump from 135 lb stays exactly 140 lb after the kg round trip.

Progression is never stored: a lift's next weight is replayed from its finished sessions (what was
actually lifted) plus the adjustments you accepted (deloads, restarts, "set next weight"), in order.
Editing or deleting a past session therefore recomputes everything consistently.
"""
from __future__ import annotations

import math
import statistics as st
import time
import uuid
from datetime import date, timedelta
from functools import lru_cache

import strength_lib as lib

LB = 0.45359237
STALL_SESSIONS = 3  # this many sessions in a row without a heavier working weight is a stall
RESTART = ((28, 30), (14, 20), (7, 10))  # (days off, % lighter) for the missed-time restart suggestion
LIGHTER_PCT = 10  # the "low readiness" lighter-day option
MILESTONES = {"lb": (95, 135, 185, 225, 275, 315, 365, 405, 455, 495), "kg": (40, 60, 80, 100, 120, 140, 160, 180, 200, 220)}
LEVELS = ("Beginner", "Novice", "Intermediate", "Advanced")
# Rough bodyweight multiples for reaching each level - round-number rules of thumb, deliberately
# approximate (they vary a lot with training age, height and technique).
STANDARDS = {
    "men": {"squat": (0.75, 1.25, 1.5, 2.0), "bench": (0.5, 0.75, 1.0, 1.5), "deadlift": (1.0, 1.5, 2.0, 2.5),
            "ohp": (0.35, 0.55, 0.7, 0.9), "row": (0.5, 0.7, 0.9, 1.2)},
    "women": {"squat": (0.5, 0.75, 1.0, 1.5), "bench": (0.25, 0.5, 0.7, 1.0), "deadlift": (0.6, 1.0, 1.25, 1.75),
              "ohp": (0.25, 0.35, 0.5, 0.65), "row": (0.3, 0.45, 0.65, 0.85)},
}
AGE_FACTOR = ((60, 0.8), (50, 0.88), (40, 0.95))  # (age from, multiplier)

DEFAULT_SETTINGS = dict(
    bar_lb=45.0, bar_kg=20.0,
    # plates in the display unit: [plate, pairs available] (a pair = one plate per side)
    plates_lb=[[45, 4], [35, 1], [25, 1], [10, 2], [5, 2], [2.5, 1]],
    plates_kg=[[25, 4], [20, 1], [15, 1], [10, 1], [5, 1], [2.5, 1], [1.25, 1]],
    db_step_lb=5.0, db_step_kg=2.0, machine_step_lb=5.0, machine_step_kg=2.5,
    rest_s=90, rest_fail_s=180, sound=True, notify=True, warmups=True,
    safety_dismissed=False, standards=None, age=None, overlay_weight=False, overlay_lean=False,
)


def unit_of(system: str) -> str:
    return "lb" if system == "us" else "kg"


def to_unit(kg: float, unit: str) -> float:
    return kg / LB if unit == "lb" else kg


def from_unit(v: float, unit: str) -> float:
    return v * LB if unit == "lb" else v


def settings_with_defaults(s: dict | None) -> dict:
    out = dict(DEFAULT_SETTINGS)
    out.update({k: v for k, v in (s or {}).items() if k in DEFAULT_SETTINGS})
    return out


def _q(x: float) -> int:  # quarter units, so 1.25 / 2.5 / 0.5 plates are exact integers
    return int(round(x * 4))


# --------------------------------------------------------------------------- plates & loadable weights
@lru_cache(maxsize=32)
def _side_sums(plates: tuple[tuple[float, int], ...]) -> dict[int, tuple[float, ...]]:
    """Every per-side load you can make (quarter units) -> the fewest plates that make it (heaviest first)."""
    best: dict[int, tuple[float, ...]] = {0: ()}
    for w, n in sorted(plates, reverse=True):
        if w <= 0 or n <= 0:
            continue
        new = dict(best)
        for s, combo in best.items():
            for k in range(1, int(n) + 1):
                t, cand = s + k * _q(w), combo + (w,) * k
                if t not in new or len(cand) < len(new[t]):
                    new[t] = cand
        best = new
    return best


def _plates_key(settings: dict, unit: str) -> tuple[tuple[float, int], ...]:
    return tuple((float(w), int(n)) for w, n in settings[f"plates_{unit}"])


def plates(target: float, bar: float, available: list | tuple) -> dict:
    """Plates per side for `target` (display unit) on `bar`: the closest loadable total (ties go
    lighter), the plates for one side, and whether it's exact."""
    sums = _side_sums(tuple((float(w), int(n)) for w, n in available))
    if target <= bar:
        return dict(total=bar, per_side=[], exact=abs(target - bar) < 1e-9, below_bar=target < bar - 1e-9)
    want = (target - bar) / 2
    side = min(sums, key=lambda s: (abs(s / 4 - want), s))
    total = bar + 2 * side / 4
    return dict(total=total, per_side=list(sums[side]), exact=abs(total - target) < 1e-9, below_bar=False)


def snap(kg: float, load: str, settings: dict, unit: str, mode: str = "nearest") -> float:
    """The loadable weight nearest to `kg` (mode="down": at most `kg`; mode="above": the lightest one
    heavier than `kg`), returned in kg."""
    v = to_unit(max(kg, 0.0), unit)
    if load == "bodyweight":
        return 0.0
    if load == "barbell":
        bar = settings[f"bar_{unit}"]
        sums = _side_sums(_plates_key(settings, unit))
        totals = sorted(bar + 2 * s / 4 for s in sums)
        if mode == "down":
            cands = [t for t in totals if t <= v + 1e-9]
            out = cands[-1] if cands else bar
        elif mode == "above":
            out = next((t for t in totals if t > v + 1e-9), totals[-1])
        else:
            out = min(totals, key=lambda t: (abs(t - v), t))
    else:
        step = settings[f"db_step_{unit}" if load == "dumbbell" else f"machine_step_{unit}"]
        n = (math.floor(v / step + 1e-9) if mode == "down" else math.floor(v / step + 1e-9) + 1 if mode == "above"
             else math.floor(v / step + 0.5))
        out = max(step, n * step)
    return from_unit(out, unit)


def warmups(work_kg: float, ex: dict, settings: dict, unit: str) -> list[dict]:
    """Ramp-up sets for a barbell lift: the empty bar twice, then about 40/60/80% for 5/3/2 reps,
    snapped down to loadable weights; sets too close to the work weight are dropped."""
    if ex.get("load") != "barbell":
        return []
    bar = from_unit(settings[f"bar_{unit}"], unit)
    if work_kg <= bar + 1e-6:
        return []
    out = [dict(kg=bar, reps=5), dict(kg=bar, reps=5)] if ex["id"] != "deadlift" else [dict(kg=bar, reps=5)]
    min_gap = from_unit(10 if unit == "lb" else 5, unit)
    for pct, reps in ((0.4, 5), (0.6, 3), (0.8, 2)):
        w = snap(work_kg * pct, "barbell", settings, unit, "down")
        if w > out[-1]["kg"] + 1e-6 and w < work_kg - min_gap + 1e-6:
            out.append(dict(kg=w, reps=reps))
    return [dict(x, done=False) for x in out]


def e1rm(kg: float | None, reps: int | None) -> float | None:
    """Epley estimated one-rep max: w x (1 + reps/30); a single is just the weight."""
    if not kg or not reps or reps < 1:
        return None
    return kg if reps == 1 else kg * (1 + reps / 30)


# --------------------------------------------------------------------------- results & progression
def work_result(entry: dict) -> dict | None:
    """Outcome of one exercise in a finished session: None if it wasn't done at all (skipped), else
    whether every target work set hit its reps at the work weight."""
    sets = entry.get("sets") or []
    done = [s for s in sets if s.get("reps") is not None]
    if entry.get("skipped") or not done:
        return None
    n, reps = int(entry.get("sets_target") or len(sets)), int(entry.get("reps_target") or 0)
    good = [s for s in sets[:n] if s.get("reps") is not None and s["reps"] >= reps and not s.get("failed")]
    return dict(kg=float(entry.get("kg") or 0), success=len(good) >= n, trimmed=bool(entry.get("trimmed")))


def _timeline(program: dict, sessions: list[dict]) -> list[tuple]:
    items = [((s["day"], s.get("started") or 0, 0), "session", s) for s in sessions if s.get("program_id") == program["id"]]
    items += [((a["day"], a.get("t") or 0, 1), "adjust", a) for a in program.get("adjustments") or []]
    return sorted(items, key=lambda x: x[0])


def lift_states(program: dict, sessions: list[dict], settings: dict, unit: str, custom: list[dict] | None = None) -> dict[str, dict]:
    """exercise id -> {next_kg, last_kg, last_day, streak, status, events}, replayed from scratch."""
    out = {}
    timeline = _timeline(program, sessions)
    for ex_id, rule in (program.get("lifts") or {}).items():
        ex = lib.lookup(ex_id, custom) or dict(id=ex_id, load="machine")
        load = ex.get("load", "machine")
        inc, limit, deload = float(rule.get("increment_kg") or 0), max(1, int(rule.get("fail_limit") or 3)), float(rule.get("deload_pct") or 10)
        s = dict(exercise_id=ex_id, next_kg=snap(float(rule.get("start_kg") or 0), load, settings, unit), last_kg=None,
                 last_day=None, streak=0, events=[], start_kg=float(rule.get("start_kg") or 0))
        for _, kind, item in timeline:
            if kind == "adjust":
                if item.get("exercise_id") != ex_id:
                    continue
                s["next_kg"], s["streak"] = snap(float(item["kg"]), load, settings, unit), 0
                s["events"].append(dict(day=item["day"], kind=item.get("kind", "set"), kg=s["next_kg"], note=item.get("note")))
                continue
            for entry in item.get("exercises") or []:
                if entry.get("exercise_id") != ex_id or entry.get("extra"):
                    continue
                r = work_result(entry)
                if r is None:
                    continue
                s["last_kg"], s["last_day"] = r["kg"], item["day"]
                ev = dict(day=item["day"], session_id=item["id"], kg=r["kg"])
                if r["trimmed"]:  # a lighter day next to a match: hold the weight, don't count it either way
                    ev["kind"] = "hold"
                elif r["success"]:
                    nxt = snap(r["kg"] + inc, load, settings, unit)
                    if inc > 0 and nxt <= r["kg"] + 1e-6:  # a jump smaller than your plates can make: go to the next loadable step
                        nxt = snap(r["kg"], load, settings, unit, "above")
                    s["next_kg"], s["streak"] = nxt, 0
                    ev["kind"] = "success"
                else:
                    s["streak"] += 1
                    if s["streak"] >= limit:
                        s["next_kg"] = snap(r["kg"] * (1 - deload / 100), load, settings, unit, "down")
                        s["streak"] = 0
                        ev.update(kind="deload", streak=limit)
                    else:
                        s["next_kg"] = r["kg"]
                        ev.update(kind="fail", streak=s["streak"])
                ev["next_kg"] = s["next_kg"]
                s["events"].append(ev)
                break  # one result per lift per session
        s["status"] = status(s["events"])
        out[ex_id] = s
    return out


def status(events: list[dict]) -> str:
    done = [e for e in events if e["kind"] in ("success", "fail", "deload", "hold")]
    if not done:
        return "new"
    if sum(1 for e in done[-10:] if e["kind"] == "deload") >= 2:  # deloading isn't fixing it
        return "stalled"
    if any(e["kind"] in ("deload", "restart") for e in events[-2:]):
        return "deloaded"
    if stalled(since_reset(events)):
        return "stalled"
    return "progressing"


def since_reset(events: list[dict]) -> list[dict]:
    """Session results after the last deload or manual weight change: climbing back after a deload
    is progress, not a stall, so stalls are judged only against the current run."""
    last = max((i for i, e in enumerate(events) if e["kind"] in ("deload", "set", "restart")), default=-1)
    return [e for e in events[last + 1:] if e["kind"] in ("success", "fail", "hold")]


def stalled(done: list[dict]) -> bool:
    """STALL_SESSIONS or more sessions in a row without a new high working weight."""
    best, run = None, 0
    for e in done:
        if best is None or e["kg"] > best + 1e-6:
            best, run = e["kg"], 0
        else:
            run += 1
    return run >= STALL_SESSIONS


def restart_pct(last_day: str | None, today: date) -> int:
    """How much lighter to suggest after time off (0 = no suggestion)."""
    if not last_day:
        return 0
    gap = (today - date.fromisoformat(last_day)).days
    return next((pct for days, pct in RESTART if gap >= days), 0)


# --------------------------------------------------------------------------- schedule
def next_workout(program: dict, sessions: list[dict], today: date) -> dict | None:
    ws = program.get("workouts") or []
    if not ws:
        return None
    mine = [s for s in sessions if s.get("program_id") == program["id"]]
    keys = [w["key"] for w in ws]
    last = mine[-1] if mine else None
    i = (keys.index(last["workout_key"]) + 1) % len(ws) if last and last.get("workout_key") in keys else 0
    days = sorted(set(int(d) for d in (program.get("schedule") or {}).get("weekdays") or [0, 2, 4] if 0 <= int(d) <= 6)) or [0, 2, 4]
    done_today = any(s["day"] == today.isoformat() for s in mine)
    d = today + timedelta(days=1 if done_today else 0)
    while d.weekday() not in days:
        d += timedelta(days=1)
    missed = None
    if last and not done_today:  # a scheduled day between the last session and today that passed without one
        x = today - timedelta(days=1)
        while x.isoformat() > last["day"]:
            if x.weekday() in days:
                missed = x.isoformat()
                break
            x -= timedelta(days=1)
    shift = (program.get("shift") or {})
    if shift.get("to") and shift.get("from") == missed and shift["to"] >= today.isoformat():
        d = date.fromisoformat(shift["to"])
    w = ws[i]
    return dict(workout_key=w["key"], name=w["name"], date=d.isoformat(), missed=missed,
                last_day=last["day"] if last else None)


# --------------------------------------------------------------------------- starting a workout
def start(program: dict, sessions: list[dict], settings: dict, unit: str, today: date, workout_key: str | None = None,
          restart: bool = False, lighter: bool = False, near_match: bool = False, custom: list[dict] | None = None) -> dict:
    """A fresh in-progress session for the next (or the given) workout of `program`."""
    nxt = next_workout(program, sessions, today) or {}
    key = workout_key or nxt.get("workout_key")
    w = next(x for x in program["workouts"] if x["key"] == key)
    states = lift_states(program, sessions, settings, unit, custom)
    mine = [s for s in sessions if s.get("program_id") == program["id"]]
    rpct = restart_pct(mine[-1]["day"] if mine else None, today)
    exercises = []
    for item in w["exercises"]:
        ex = lib.lookup(item["exercise_id"], custom) or dict(id=item["exercise_id"], name=item["exercise_id"], load="machine")
        stt = states.get(ex["id"])
        kg = stt["next_kg"] if stt else snap(0, ex["load"], settings, unit)
        note = []
        if restart and rpct and stt and stt["last_day"]:
            kg = snap(kg * (1 - rpct / 100), ex["load"], settings, unit, "down")
            note.append(f"restart −{rpct}%")
        if lighter:
            kg = snap(kg * (1 - LIGHTER_PCT / 100), ex["load"], settings, unit, "down")
            note.append(f"lighter −{LIGHTER_PCT}%")
        sets_n, trimmed = int(item["sets"]), False
        if near_match and item.get("trim_near_match"):
            sets_n, trimmed = max(1, sets_n - 1 if sets_n <= 3 else math.ceil(sets_n / 2)), True
            note.append("trimmed for a match nearby")
        exercises.append(dict(
            exercise_id=ex["id"], name=ex["name"], load=ex["load"], kg=kg, planned_kg=stt["next_kg"] if stt else kg,
            sets_target=sets_n, reps_target=int(item["reps"]), rest_s=int(item.get("rest_s") or settings["rest_s"]),
            rest_fail_s=int(item.get("rest_fail_s") or settings["rest_fail_s"]),
            warmups=warmups(kg, ex, settings, unit) if item.get("warmup", True) and settings.get("warmups", True) else [],
            sets=[dict(reps=None, kg=kg, failed=False, note="") for _ in range(sets_n)], skipped=False, extra=False,
            trimmed=trimmed, adjust=", ".join(note) or None,
            increment_kg=float(((program.get("lifts") or {}).get(ex["id"]) or {}).get("increment_kg") or 0)))
    return dict(id=uuid.uuid4().hex[:12], program_id=program["id"], program_name=program["name"], workout_key=w["key"],
                workout_name=w["name"], day=today.isoformat(), started=time.time(), paused_s=0.0, paused_at=None,
                current=0, notes="", restart_pct=rpct if restart else 0, lighter=lighter, exercises=exercises)


def extra_exercise(ex: dict, settings: dict, unit: str, sets: int = 3, reps: int = 10, kg: float | None = None) -> dict:
    kg = snap(kg or 0, ex["load"], settings, unit)
    return dict(exercise_id=ex["id"], name=ex["name"], load=ex["load"], kg=kg, planned_kg=kg, sets_target=sets, reps_target=reps,
                rest_s=int(ex.get("rest_s") or settings["rest_s"]), rest_fail_s=settings["rest_fail_s"], warmups=[],
                sets=[dict(reps=None, kg=kg, failed=False, note="") for _ in range(sets)], skipped=False, extra=True,
                trimmed=False, adjust=None, increment_kg=0)


def _num(v, lo, hi, default=None):
    try:
        x = float(v)
    except (TypeError, ValueError):
        return default
    return default if math.isnan(x) else min(hi, max(lo, x))


def clean_active(x: dict) -> dict:
    """Trust nothing from the page: keep known fields, clamp numbers, cap sizes and text."""
    ex_out = []
    for e in (x.get("exercises") or [])[:30]:
        sets = []
        for s in (e.get("sets") or [])[:20]:
            reps = s.get("reps")
            sets.append(dict(reps=None if reps is None else int(_num(reps, 0, 100, 0)), kg=_num(s.get("kg"), 0, 1000, 0.0),
                             failed=bool(s.get("failed")), note=str(s.get("note") or "")[:200]))
        warm = [dict(kg=_num(w.get("kg"), 0, 1000, 0.0), reps=int(_num(w.get("reps"), 0, 100, 0)), done=bool(w.get("done")))
                for w in (e.get("warmups") or [])[:8]]
        ex_out.append(dict(exercise_id=str(e.get("exercise_id") or "")[:60], name=str(e.get("name") or "")[:80],
                           load=e.get("load") if e.get("load") in lib.LOADS else "machine", kg=_num(e.get("kg"), 0, 1000, 0.0),
                           planned_kg=_num(e.get("planned_kg"), 0, 1000, 0.0), sets_target=int(_num(e.get("sets_target"), 1, 20, 1)),
                           reps_target=int(_num(e.get("reps_target"), 1, 100, 5)), rest_s=int(_num(e.get("rest_s"), 0, 900, 90)),
                           rest_fail_s=int(_num(e.get("rest_fail_s"), 0, 900, 180)), warmups=warm, sets=sets,
                           skipped=bool(e.get("skipped")), extra=bool(e.get("extra")), trimmed=bool(e.get("trimmed")),
                           warmups_skipped=bool(e.get("warmups_skipped")),
                           adjust=str(e["adjust"])[:80] if e.get("adjust") else None, swapped_from=str(e.get("swapped_from") or "")[:60] or None,
                           increment_kg=_num(e.get("increment_kg"), 0, 100, 0.0)))
    now = time.time()
    return dict(id=str(x.get("id") or uuid.uuid4().hex[:12])[:40], program_id=str(x.get("program_id") or "")[:40] or None,
                program_name=str(x.get("program_name") or "")[:80], workout_key=str(x.get("workout_key") or "")[:10],
                workout_name=str(x.get("workout_name") or "")[:80], day=str(x.get("day") or date.today().isoformat())[:10],
                started=_num(x.get("started"), 0, now + 60, now), paused_s=_num(x.get("paused_s"), 0, 86400 * 2, 0.0),
                paused_at=_num(x.get("paused_at"), 0, now + 60) if x.get("paused_at") else None,
                current=int(_num(x.get("current"), 0, 29, 0)), notes=str(x.get("notes") or "")[:500],
                restart_pct=int(_num(x.get("restart_pct"), 0, 50, 0)), lighter=bool(x.get("lighter")), exercises=ex_out,
                rest=_rest(x.get("rest")))


def _rest(r) -> dict | None:
    """The running rest timer, so a reopened window picks it up (numbers and flags only)."""
    if not isinstance(r, dict) or _num(r.get("ends"), 0, 1e11) is None:
        return None
    return dict(ends=_num(r.get("ends"), 0, 1e11), total=_num(r.get("total"), 1, 900, 90), failed=bool(r.get("failed")), over=bool(r.get("over")))


def finish(active: dict, now: float | None = None) -> dict:
    """The session to keep: pauses closed, duration net of pauses, never-touched sets dropped from extras."""
    now = now or time.time()
    x = {k: v for k, v in active.items() if k not in ("rest", "current")}
    paused = x.get("paused_s") or 0
    if x.get("paused_at"):
        paused += now - x["paused_at"]
    x.update(finished=now, paused_s=round(paused, 1), paused_at=None, duration_s=round(max(0, now - x["started"] - paused)))
    return x


# --------------------------------------------------------------------------- tracking
def done_sets(entry: dict) -> list[dict]:
    return [s for s in entry.get("sets") or [] if s.get("reps")]


def volume(session: dict) -> float:
    """Work-set tonnage in kg (completed reps x weight; bodyweight moves count 0)."""
    return sum(s["reps"] * (s.get("kg") or 0) for e in session.get("exercises") or [] for s in done_sets(e))


def lift_series(ex_id: str, sessions: list[dict]) -> list[dict]:
    """Per session that did this exercise: working weight, top set, e1RM, volume, outcome."""
    out = []
    for s in sessions:
        for e in s.get("exercises") or []:
            if e.get("exercise_id") != ex_id:
                continue
            sets = done_sets(e)
            if not sets:
                continue
            r = work_result(e)
            top = max(sets, key=lambda z: ((z.get("kg") or 0), z["reps"]))
            best = max((e1rm(z.get("kg"), z["reps"]) or 0 for z in sets), default=0) or None
            out.append(dict(day=s["day"], session_id=s["id"], kg=float(e.get("kg") or 0), top_kg=top.get("kg") or 0, top_reps=top["reps"],
                            e1rm=round(best, 2) if best else None, volume_kg=round(sum(z["reps"] * (z.get("kg") or 0) for z in sets), 1),
                            reps=[z.get("reps") for z in e.get("sets") or []], success=bool(r and r["success"]),
                            trimmed=bool(e.get("trimmed")), extra=bool(e.get("extra")), program_id=s.get("program_id")))
            break
    return out


def _at_or_before(series: list[dict], day: str) -> dict | None:
    xs = [r for r in series if r["day"] <= day]
    return xs[-1] if xs else None


def lift_card(ex: dict, series: list[dict], state: dict | None, today: date) -> dict:
    cur = series[-1] if series else None
    change = {}
    for n in (30, 90):
        then = _at_or_before(series, (today - timedelta(days=n)).isoformat()) or (series[0] if series else None)
        change[f"d{n}"] = round(cur["kg"] - then["kg"], 3) if cur and then and then is not cur else (0.0 if cur else None)
    best = max(series, key=lambda r: (r["top_kg"], r["top_reps"]), default=None)
    e1 = max((r["e1rm"] for r in series if r["e1rm"]), default=None)
    st_ = state["status"] if state else ("new" if not series else
                                         "stalled" if stalled([dict(kg=r["kg"], kind="success" if r["success"] else "fail") for r in series]) else "progressing")
    return dict(exercise_id=ex["id"], name=ex["name"], load=ex.get("load"), current_kg=cur["kg"] if cur else None,
                next_kg=state["next_kg"] if state else None, last_day=cur["day"] if cur else None, sessions=len(series),
                change_kg=change, best=dict(kg=best["top_kg"], reps=best["top_reps"], day=best["day"]) if best else None,
                e1rm=round(e1, 2) if e1 else None, status=st_, streak=state["streak"] if state else 0,
                spark=[r["kg"] for r in series[-12:]])


def stall_options(card: dict, rule: dict | None, unit: str) -> list[dict]:
    if card["status"] != "stalled" or not card.get("current_kg"):
        return []
    opts = [dict(key="deload", label="Deload 10% and build back up", kg=card["current_kg"] * 0.9)]
    opts.append(dict(key="reps", label="Switch to 3×5 for a while (less fatigue per session)"))
    inc = float((rule or {}).get("increment_kg") or 0)
    small = from_unit(2.5 if unit == "lb" else 1.25, unit)
    if inc > small + 1e-6:
        opts.append(dict(key="increment", label="Use smaller jumps from here", increment_kg=small))
    return opts


def prs(sessions: list[dict]) -> dict[str, list[dict]]:
    """session id -> records set in it (vs every earlier session): heaviest set, best e1RM, most reps
    at a weight (at least as heavy as the earlier best reps were done with)."""
    best_kg: dict[str, float] = {}
    best_e1: dict[str, float] = {}
    best_reps: dict[str, list[tuple[float, int]]] = {}
    out: dict[str, list[dict]] = {}
    for s in sessions:
        found = []
        for e in s.get("exercises") or []:
            ex_id, sets = e.get("exercise_id"), [z for z in done_sets(e) if not z.get("failed") or z["reps"]]
            if not sets:
                continue
            first = ex_id not in best_kg
            hk = max(z.get("kg") or 0 for z in sets)
            if hk > 0 and not first and hk > best_kg[ex_id] + 1e-6:
                found.append(dict(exercise_id=ex_id, name=e.get("name"), kind="weight", kg=hk, prev_kg=best_kg[ex_id]))
            he = max((e1rm(z.get("kg"), z["reps"]) or 0 for z in sets), default=0)
            if he and not first and he > best_e1.get(ex_id, 0) + 1e-6:
                found.append(dict(exercise_id=ex_id, name=e.get("name"), kind="e1rm", kg=round(he, 2), prev_kg=round(best_e1.get(ex_id, 0), 2)))
            prev = best_reps.get(ex_id, [])
            for z in sets:
                k = z.get("kg") or 0
                beaten = [r for kk, r in prev if kk >= k - 1e-6]
                if not first and beaten and z["reps"] > max(beaten) and not any(f["kind"] == "reps" and f["exercise_id"] == ex_id for f in found):
                    found.append(dict(exercise_id=ex_id, name=e.get("name"), kind="reps", kg=k, reps=z["reps"], prev_reps=max(beaten)))
            best_kg[ex_id] = max(best_kg.get(ex_id, 0), hk)
            best_e1[ex_id] = max(best_e1.get(ex_id, 0), he)
            best_reps.setdefault(ex_id, []).extend((z.get("kg") or 0, z["reps"]) for z in sets)
        if found:
            out[s["id"]] = found
    return out


def milestones(sessions: list[dict], unit: str) -> dict[str, list[dict]]:
    """session id -> round-number weights lifted for the first time (e.g. a first 225 lb squat)."""
    hit: dict[str, set] = {}
    out: dict[str, list[dict]] = {}
    for s in sessions:
        for e in s.get("exercises") or []:
            ex_id = e.get("exercise_id")
            top = max((to_unit(z.get("kg") or 0, unit) for z in done_sets(e)), default=0)
            for m in MILESTONES[unit]:
                if top + 1e-6 >= m and m not in hit.setdefault(ex_id, set()):
                    hit[ex_id].add(m)
                    if e.get("load") == "barbell":
                        out.setdefault(s["id"], []).append(dict(exercise_id=ex_id, name=e.get("name"), value=m, unit=unit))
    for sid in out:  # only the biggest new milestone per lift per session
        best: dict[str, dict] = {}
        for m in out[sid]:
            if m["exercise_id"] not in best or m["value"] > best[m["exercise_id"]]["value"]:
                best[m["exercise_id"]] = m
        out[sid] = list(best.values())
    return out


def standards(ex_id: str, bodyweight_kg: float | None, table: str | None, age: int | None) -> list[dict] | None:
    """Approximate level thresholds (kg) for a lift, or None when off / not a benchmarked lift."""
    t = STANDARDS.get(table or "", {}).get(ex_id)
    if not t or not bodyweight_kg:
        return None
    f = next((m for a, m in AGE_FACTOR if age and age >= a), 1.0)
    return [dict(level=lvl, kg=round(bodyweight_kg * r * f, 1)) for lvl, r in zip(LEVELS, t)]


def summary(session: dict, history: list[dict], states_after: dict, nxt: dict | None, unit: str, program: dict | None) -> dict:
    """The end-of-workout screen: duration, volume, sets, records, milestones and what's next."""
    all_ = history + [session]
    pr = prs(all_).get(session["id"], [])
    ms = milestones(all_, unit).get(session["id"], [])
    done = [z for e in session["exercises"] for z in e["sets"] if z.get("reps") is not None]
    failed = sum(1 for e in session["exercises"] if not e.get("extra") and (r := work_result(e)) and not r["success"])
    nxt_items = []
    if nxt and program:
        w = next((x for x in program["workouts"] if x["key"] == nxt["workout_key"]), None)
        for item in (w or {}).get("exercises", []):
            s = states_after.get(item["exercise_id"])
            ex = lib.lookup(item["exercise_id"]) or dict(name=item["exercise_id"])
            nxt_items.append(dict(exercise_id=item["exercise_id"], name=ex["name"], sets=item["sets"], reps=item["reps"],
                                  kg=s["next_kg"] if s else None))
    return dict(session_id=session["id"], duration_s=session.get("duration_s"), volume_kg=round(volume(session), 1),
                sets_done=len(done), lifts_failed=failed, prs=pr, milestones=ms,
                next=dict(nxt, exercises=nxt_items) if nxt else None)
