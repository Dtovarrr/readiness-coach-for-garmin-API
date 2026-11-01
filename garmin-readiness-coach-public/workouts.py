"""Structured workouts: the step model, validation, estimates, templates, plan conversion, the
readiness-based suggestion and planned-vs-actual.

Everything is metric, like the rest of the app (seconds, meters, m/s); the page converts for display
through `Units`. A workout:

    {id, name, sport: run|ride|strength|timed, notes, scheduled: "YYYY-MM-DD" | None,
     source: {event_id, workout_id} | {suggested: day} | None, steps: [step | repeat]}
    step   = {kind: warmup|work|recovery|rest|cooldown, name, notes,
              dur: {type: time, sec} | {type: distance, m} | {type: open},
              target: {type: open} | {type: hr_zone, zone} | {type: hr_bpm, lo, hi} | {type: pace, lo, hi}}
    repeat = {kind: repeat, count, steps: [step, ...]}

Pace targets are stored as speeds: `lo` is the slow end (m/s) and `hi` the fast end, which is also
how the FIT file wants them. Repeats nest one level deep, which is what Garmin watches support.
Training guidance, not medical advice.
"""
from __future__ import annotations

import copy
import time
import uuid
from datetime import date

import units

SPORTS = ("run", "ride", "strength", "timed")
KINDS = ("warmup", "work", "recovery", "rest", "cooldown")
KIND_NAMES = {"warmup": "Warm up", "work": "Work", "recovery": "Recover", "rest": "Rest", "cooldown": "Cool down"}
TARGETS = ("open", "hr_zone", "hr_bpm", "pace")
MAX_FIT_STEPS = 50       # flattened, repeat markers included; watches handle ~50+ but stay well clear
MAX_REPEAT = 50
MAX_STEP_SEC = 6 * 3600
MAX_STEP_M = 100_000
HR_RANGE = (30, 230)
SPEED_RANGE = (0.5, 25.0)  # m/s: ~53:00 min/mi to ~56 mph
DEFAULT_EASY_MPS = {"run": units.M_MI / 600, "ride": 7.0}  # 10:00 min/mi, ~15.7 mph when there's no history


class WorkoutError(ValueError):
    pass


def new_id() -> str:
    return uuid.uuid4().hex[:10]


def now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def distance_sport(sport: str) -> bool:
    return sport in ("run", "ride")


# --------------------------------- validation -------------------------------- #
def _step(s: dict, sport: str, where: str) -> dict:
    kind = s.get("kind")
    if kind not in KINDS:
        raise WorkoutError(f"{where}: unknown step type.")
    d = s.get("dur") or {}
    if d.get("type") == "time":
        sec = d.get("sec")
        if not isinstance(sec, (int, float)) or not 5 <= sec <= MAX_STEP_SEC:
            raise WorkoutError(f"{where}: a timed step needs between 5 seconds and 6 hours.")
        dur = dict(type="time", sec=round(float(sec)))
    elif d.get("type") == "distance":
        m = d.get("m")
        if not distance_sport(sport):
            raise WorkoutError(f"{where}: distance steps are for running and cycling.")
        if not isinstance(m, (int, float)) or not 10 <= m <= MAX_STEP_M:
            raise WorkoutError(f"{where}: a distance step needs between 10 m and 100 km.")
        dur = dict(type="distance", m=round(float(m), 2))
    elif d.get("type") == "open":
        dur = dict(type="open")
    else:
        raise WorkoutError(f"{where}: choose a time, a distance or open (lap button).")
    t = s.get("target") or {"type": "open"}
    tt = t.get("type")
    if tt == "open":
        target = dict(type="open")
    elif tt == "hr_zone":
        if t.get("zone") not in (1, 2, 3, 4, 5):
            raise WorkoutError(f"{where}: heart rate zone must be 1 to 5.")
        target = dict(type="hr_zone", zone=t["zone"])
    elif tt in ("hr_bpm", "pace"):
        lo, hi = t.get("lo"), t.get("hi")
        rng = HR_RANGE if tt == "hr_bpm" else SPEED_RANGE
        if tt == "pace" and not distance_sport(sport):
            raise WorkoutError(f"{where}: pace targets are for running and cycling.")
        if not all(isinstance(x, (int, float)) and rng[0] <= x <= rng[1] for x in (lo, hi)):
            raise WorkoutError(f"{where}: the {'heart rate' if tt == 'hr_bpm' else 'pace'} range is out of bounds.")
        if lo >= hi:
            raise WorkoutError(f"{where}: the {'low heart rate must be under the high one' if tt == 'hr_bpm' else 'slow pace must be slower than the fast pace'}.")
        target = dict(type=tt, lo=round(lo) if tt == "hr_bpm" else round(lo, 4), hi=round(hi) if tt == "hr_bpm" else round(hi, 4))
    else:
        raise WorkoutError(f"{where}: unknown target.")
    return dict(kind=kind, name=(s.get("name") or "").strip()[:40], notes=(s.get("notes") or "").strip()[:120], dur=dur, target=target)


def validate(w: dict) -> dict:
    """A clean copy of the workout, or WorkoutError with a message the page can show."""
    sport = w.get("sport")
    if sport not in SPORTS:
        raise WorkoutError("Choose a sport.")
    name = (w.get("name") or "").strip()
    if not name:
        raise WorkoutError("Give the workout a name.")
    steps, n = [], 0
    raw = w.get("steps") or []
    if not raw:
        raise WorkoutError("Add at least one step.")
    for i, s in enumerate(raw, 1):
        if s.get("kind") == "repeat":
            count, kids = s.get("count"), s.get("steps") or []
            if not isinstance(count, int) or not 2 <= count <= MAX_REPEAT:
                raise WorkoutError(f"Step {i}: repeat 2 to {MAX_REPEAT} times.")
            if not kids:
                raise WorkoutError(f"Step {i}: a repeat needs steps inside it.")
            if any(k.get("kind") == "repeat" for k in kids):
                raise WorkoutError(f"Step {i}: repeats can't contain other repeats (watches don't support it).")
            steps.append(dict(kind="repeat", count=count, steps=[_step(k, sport, f"Step {i}.{j}") for j, k in enumerate(kids, 1)]))
            n += len(kids) + 1
        else:
            steps.append(_step(s, sport, f"Step {i}"))
            n += 1
    if n > MAX_FIT_STEPS:
        raise WorkoutError(f"That's {n} steps once repeats are written out; keep it to {MAX_FIT_STEPS}.")
    sched = w.get("scheduled") or None
    if sched:
        try:
            sched = date.fromisoformat(sched).isoformat()
        except (TypeError, ValueError):
            raise WorkoutError("The scheduled day isn't a date.")
    return dict(id=w.get("id") or new_id(), name=name[:60], sport=sport, notes=(w.get("notes") or "").strip()[:1000],
                scheduled=sched, source=w.get("source") or None, steps=steps,
                created=w.get("created") or now(), updated=now())


# ------------------------------- flattening ---------------------------------- #
def fit_steps(w: dict) -> list[dict]:
    """Steps in FIT order: a repeat's children, then a marker {kind: repeat, back_to, count}.
    `index` is the FIT message_index, which is also what the watch writes as `wkt_step_index` on laps."""
    out = []
    for s in w["steps"]:
        if s["kind"] == "repeat":
            start = len(out)
            for c in s["steps"]:
                out.append(dict(c, index=len(out)))
            out.append(dict(kind="repeat", index=len(out), back_to=start, count=s["count"]))
        else:
            out.append(dict(s, index=len(out)))
    return out


def timeline(w: dict) -> list[dict]:
    """Every step as it will be done (repeats expanded), each with its FIT index and rep number."""
    out = []
    for s in fit_steps(w):
        if s["kind"] != "repeat":
            out.append(dict(s, rep=None))
            continue
        block = [x for x in out if x["index"] >= s["back_to"]]
        for x in block:
            x["rep"] = (1, s["count"])
        for r in range(2, s["count"] + 1):
            out += [dict(x, rep=(r, s["count"])) for x in block]
    return out


# --------------------------------- estimates --------------------------------- #
def step_speed(s: dict, sport: str, easy_mps: float | None) -> float | None:
    """The speed a step is estimated at: the middle of a pace target, else your easy pace (a bit
    faster for work, slower for recovery). None for rest and non-distance sports."""
    if not distance_sport(sport) or s["kind"] == "rest":
        return None
    t = s["target"]
    if t["type"] == "pace":
        return (t["lo"] + t["hi"]) / 2
    easy = easy_mps or DEFAULT_EASY_MPS[sport]
    zone = t.get("zone") if t["type"] == "hr_zone" else None
    if s["kind"] == "work" or (zone or 0) >= 3:
        return easy * (1.08 + 0.04 * max(0, (zone or 3) - 3))
    if s["kind"] == "recovery":
        return easy * 0.85
    return easy


def estimate(w: dict, easy_mps: float | None = None) -> dict:
    """Total seconds and meters (meters None for strength/timed), plus how many open steps aren't counted."""
    secs, meters, open_n = 0.0, 0.0, 0
    for s in timeline(w):
        d, v = s["dur"], step_speed(s, w["sport"], easy_mps)
        if d["type"] == "time":
            secs += d["sec"]
            meters += d["sec"] * (v or 0)
        elif d["type"] == "distance":
            meters += d["m"]
            secs += d["m"] / v if v else 0
        else:
            open_n += 1
    return dict(sec=round(secs), m=round(meters) if distance_sport(w["sport"]) else None, open_steps=open_n)


# ---------------------------------- HR zones --------------------------------- #
DEMO_ZONES = [dict(trainingMethod="HR_MAX", sport="DEFAULT", maxHeartRateUsed=188, zone1Floor=94, zone2Floor=113,
                   zone3Floor=132, zone4Floor=150, zone5Floor=169)]


def heart_zones(raw, sport: str = "run") -> dict | None:
    """Zones from /biometric-service/heartRateZones for this sport (else DEFAULT):
    {method, max, zones: [{zone, lo, hi}]}. None when Garmin has no zones configured."""
    rows = [r for r in (raw if isinstance(raw, list) else [raw] if isinstance(raw, dict) else []) if isinstance(r, dict)]
    want = {"run": "RUNNING", "ride": "CYCLING"}.get(sport)
    row = next((r for r in rows if r.get("sport") == want), None) or next((r for r in rows if r.get("sport") == "DEFAULT"), None) \
        or (rows[0] if rows else None)
    if not row:
        return None
    floors = [row.get(f"zone{i}Floor") for i in range(1, 6)]
    mx = row.get("maxHeartRateUsed")
    if not all(isinstance(f, (int, float)) and f > 0 for f in floors) or not mx:
        return None
    zones = [dict(zone=i + 1, lo=int(floors[i]), hi=int(floors[i + 1]) - 1 if i < 4 else int(mx)) for i in range(5)]
    return dict(method=row.get("trainingMethod"), sport=row.get("sport"), max=int(mx), zones=zones)


def zone_range(zones: dict | None, zone: int) -> tuple[int, int] | None:
    z = next((x for x in (zones or {}).get("zones", []) if x["zone"] == zone), None)
    return (z["lo"], z["hi"]) if z else None


# --------------------------------- templates --------------------------------- #
def _s(kind, dur, target=None, name="", notes=""):
    return dict(kind=kind, name=name, notes=notes, dur=dur, target=target or dict(type="open"))


def _t(sec):
    return dict(type="time", sec=sec)


def _d(m):
    return dict(type="distance", m=m)


def _z(zone):
    return dict(type="hr_zone", zone=zone)


def _rep(count, *steps):
    return dict(kind="repeat", count=count, steps=list(steps))


TEMPLATES = {
    "easy_run": dict(name="Easy run", sport="run", notes="Conversational effort. Walk breaks are fine.",
                     steps=[_s("warmup", _t(300), _z(1)), _s("work", _t(1800), _z(2), "Easy"), _s("cooldown", _t(300), _z(1))]),
    "tempo": dict(name="Tempo run", sport="run", notes="Comfortably hard: you can speak a few words at a time.",
                  steps=[_s("warmup", _t(600), _z(2)), _s("work", _t(1200), _z(4), "Tempo"), _s("cooldown", _t(600), _z(1))]),
    "intervals": dict(name="Intervals 5 × 4 min", sport="run", notes="Hard but controlled; the last rep should match the first.",
                      steps=[_s("warmup", _t(600), _z(2)), _rep(5, _s("work", _t(240), _z(4), "Hard"), _s("recovery", _t(180), _z(1), "Easy")),
                             _s("cooldown", _t(600), _z(1))]),
    "long_run": dict(name="Long run", sport="run", notes="Steady and easy. Practise fueling if it runs past 75 minutes.",
                     steps=[_s("warmup", _d(units.M_MI), _z(1)), _s("work", _d(8 * units.M_MI), _z(2), "Steady"), _s("cooldown", _d(units.M_MI), _z(1))]),
    "easy_ride": dict(name="Easy ride", sport="ride", notes="Spin easy, high cadence.",
                      steps=[_s("warmup", _t(600), _z(1)), _s("work", _t(2700), _z(2)), _s("cooldown", _t(300), _z(1))]),
    "ride_intervals": dict(name="Ride intervals 5 × 4 min", sport="ride", notes="",
                           steps=[_s("warmup", _t(900), _z(2)), _rep(5, _s("work", _t(240), _z(4), "Hard"), _s("recovery", _t(240), _z(1), "Easy")),
                                  _s("cooldown", _t(600), _z(1))]),
    "tennis": dict(name="Timed court session", sport="timed", notes="Warm up, drill, play, cool down. Press lap to move on early.",
                   steps=[_s("warmup", _t(600), name="Warm-up rally"), _s("work", _t(1200), name="Drills"), _s("rest", _t(120)),
                          _s("work", _t(2400), name="Points / match play"), _s("cooldown", _t(300), name="Stretch")]),
    "strength": dict(name="Strength circuit", sport="strength", notes="",
                     steps=[_s("warmup", _t(300)), _rep(3, _s("work", _t(600), name="Circuit"), _s("rest", _t(120))), _s("cooldown", _t(300))]),
}
TEMPLATE_ORDER = ("easy_run", "tempo", "intervals", "long_run", "easy_ride", "ride_intervals", "tennis", "strength")


def from_template(key: str) -> dict:
    if key not in TEMPLATES:
        raise WorkoutError("Unknown template.")
    return validate(copy.deepcopy(TEMPLATES[key]))


# ------------------------------ from a plan session -------------------------- #
PLAN_SPORT = {"run": "run", "ride": "ride", "tennis": "timed", "general": "timed"}


def from_plan(pw: dict, event: dict | None = None) -> dict:
    """A plan session (plans.py) as structured steps. The plan only says what kind of session and how
    much; the structure here is a sensible default you can edit."""
    sport = "strength" if pw["type"] in ("strength", "mobility") else PLAN_SPORT.get(pw["sport"], "timed")
    det, typ, intensity = pw.get("detail") or {}, pw["type"], pw.get("intensity")
    dist, mins = pw.get("distance_m"), pw.get("minutes")
    main = _d(dist) if dist and distance_sport(sport) else _t(round((mins or 30) * 60))
    steps: list[dict]
    if typ == "intervals" and det.get("rep_m"):
        steps = [_s("warmup", _t(600), _z(2)), _rep(det.get("reps", 5), _s("work", _d(det["rep_m"]), _z(4), "Hard"),
                                                      _s("recovery", _t(150), _z(1), "Jog")), _s("cooldown", _t(600), _z(1))]
    elif typ == "intervals" and det.get("rep_min"):
        rec = 240 if sport == "ride" else 120
        steps = [_s("warmup", _t(600), _z(2)), _rep(det.get("reps", 5), _s("work", _t(det["rep_min"] * 60), _z(4), "Hard"),
                                                      _s("recovery", _t(rec), _z(1), "Easy")), _s("cooldown", _t(600), _z(1))]
    elif typ == "tempo" and det.get("tempo_min"):
        steps = [_s("warmup", _t(600), _z(2)), _s("work", _t(det["tempo_min"] * 60), _z(4 if intensity == "hard" else 3), "Tempo"),
                 _s("cooldown", _t(600), _z(1))]
    elif typ == "tempo" and sport == "ride":
        steps = [_s("warmup", _t(900), _z(2)), _rep(2, _s("work", _t(900), _z(3), "Tempo"), _s("recovery", _t(300), _z(1))),
                 _s("cooldown", _t(600), _z(1))]
    elif typ == "conditioning":
        steps = [_s("warmup", _t(600)), _rep(8, _s("work", _t(30), _z(5), "Sprint"), _s("rest", _t(90))), _s("cooldown", _t(300))]
    elif typ == "race":
        steps = [_s("work", main, name="Race")]
    elif typ in ("easy", "long", "cardio") and distance_sport(sport) or typ == "cardio":
        steps = [_s("work", main, _z(2), "Easy" if typ != "long" else "Steady")]
        if det.get("strides"):
            steps.append(_rep(det["strides"], _s("work", _t(20), name="Stride"), _s("recovery", _t(60), _z(1), "Walk/jog")))
    else:  # strength, mobility, drills, hitting, match: one timed block you can lap through
        steps = [_s("work", main, name=pw["title"][:40])]
    notes = " ".join(x for x in (pw.get("note") or "", f"From your {event['name']} plan." if event else "") if x)
    return validate(dict(name=pw["title"], sport=sport, notes=notes, steps=steps, scheduled=pw["date"],
                         source=dict(event_id=(event or {}).get("id"), workout_id=pw["id"])))


# ------------------------------ readiness suggestion ------------------------- #
def suggest_today(assessment: dict, today: date, sport: str = "run", plan_today: list[tuple[dict, dict]] | None = None) -> dict | None:
    """A workout for today from this morning's readiness (rule-based). If the plan has a session
    today, that's what's suggested, eased on a low-readiness day. Suggestions only: nothing is saved
    unless you accept."""
    zone, score = assessment.get("zone"), assessment.get("readiness")
    if zone is None:
        return None
    rs = f"Readiness is {score}" if score is not None else "Readiness"
    for ev, pw in plan_today or []:
        if pw.get("status") == "skipped" or pw["type"] == "race":
            continue
        w = from_plan(pw, ev)
        low = zone == "recover" or (zone == "caution" and pw.get("intensity") == "hard")
        if not low:
            return dict(workout=w, reason=f"{rs} ({zone}): today's planned {pw['title'].lower()} is on.", kind="plan")
        eased = ease(w)
        return dict(workout=eased, reason=f"{rs} ({zone}): your planned {pw['title'].lower()} is eased to an easy session "
                                          f"about 70% as long. Rest instead if you feel run down.", kind="plan_eased")
    if sport not in SPORTS:
        sport = "run"
    pick = {"prime": ("intervals", "ride_intervals", "you're fresh, so a quality session fits"),
            "good": ("tempo", "easy_ride", "a steady, moderately hard session fits"),
            "caution": ("easy_run", "easy_ride", "keep it easy today"),
            "recover": ("easy_run", "easy_ride", "rest, or at most a short, very easy session")}.get(zone)
    if not pick:
        return None
    key = pick[1] if sport == "ride" else pick[0] if sport == "run" else ("tennis" if sport == "timed" else "strength")
    w = from_template(key)
    if zone == "recover":
        w = ease(w, factor=0.6, zone=1)
    w.update(scheduled=today.isoformat(), source=dict(suggested=today.isoformat()), name=w["name"] + " (suggested)")
    return dict(workout=w, reason=f"{rs} ({zone}): {pick[2]}.", kind="readiness", template=key)


def ease(w: dict, factor: float = 0.7, zone: int = 2) -> dict:
    """An easier copy: work becomes steady zone `zone`, repeats drop to ~70%, durations shrink."""
    w = copy.deepcopy(w)

    def soft(s):
        d = s["dur"]
        if d["type"] == "time":
            d["sec"] = max(60, round(d["sec"] * factor / 30) * 30) if s["kind"] in ("work", "warmup", "cooldown") else d["sec"]
        elif d["type"] == "distance":
            d["m"] = round(d["m"] * factor, -1)
        if s["kind"] == "work":
            s["target"] = _z(zone) if distance_sport(w["sport"]) else dict(type="open")
            s["name"] = "Easy"
        return s
    out = []
    for s in w["steps"]:
        if s["kind"] == "repeat":
            s["count"] = max(2, round(s["count"] * factor))
            s["steps"] = [soft(x) for x in s["steps"]]
            out.append(s)
        else:
            out.append(soft(s))
    w["steps"] = out
    w["name"] = (w["name"] + " (easier)")[:60]
    return validate(w)


# ------------------------------- planned vs actual --------------------------- #
def in_target(target: dict, hr, speed, zones: dict | None) -> bool | None:
    t = target["type"]
    if t == "hr_zone":
        r = zone_range(zones, target["zone"])
        return None if not r or hr is None else r[0] <= hr <= r[1]
    if t == "hr_bpm":
        return None if hr is None else target["lo"] <= hr <= target["hi"]
    if t == "pace":
        return None if speed is None else target["lo"] <= speed <= target["hi"]
    return None


def _window(series: list[dict], t0: float, t1: float) -> list[dict]:
    return [p for p in series if p.get("t") is not None and t0 < p["t"] <= t1]


def _stats(pts: list[dict], target: dict, zones) -> dict:
    """Averages over the series points in a step window, with the share of time inside the target."""
    hr = [p["hr"] for p in pts if p.get("hr")]
    sp = [p["speed"] for p in pts if p.get("speed") is not None]
    hits = [in_target(target, p.get("hr"), p.get("speed"), zones) for p in pts]
    judged = [h for h in hits if h is not None]
    return dict(hr=round(sum(hr) / len(hr)) if hr else None, speed=round(sum(sp) / len(sp), 3) if sp else None,
                in_target=round(100 * sum(judged) / len(judged)) if judged else None)


def compare(w: dict, view: dict, zones: dict | None) -> dict:
    """Planned steps against what the activity recorded. Laps written by the watch while following the
    workout carry the step index, so those line up exactly; otherwise steps are laid over the activity
    by their planned time or distance (marked approximate)."""
    plan = timeline(w)
    series = view.get("series") or []
    laps = view.get("laps") or []
    rows, how = [], "laps"
    by_index = [lp for lp in laps if lp.get("step") is not None]
    if by_index:
        pool = {}
        for lp in by_index:
            pool.setdefault(lp["step"], []).append(lp)
        pairs = [(s, (pool.get(s["index"]) or [None]).pop(0) if pool.get(s["index"]) else None) for s in plan]
    elif laps and len(laps) in (len(plan), len(plan) + 1):  # +1: a short lap after the workout ended
        pairs = list(zip(plan, laps))
    else:
        how, pairs, t, d = "approx", [], 0.0, 0.0
        for s in plan:  # lay steps over the timeline by planned time or distance; open steps can't be placed
            dur = s["dur"]
            if dur["type"] == "time":
                t1 = t + dur["sec"]
            elif dur["type"] == "distance":
                hit = next((p for p in series if (p.get("d") or 0) >= d + dur["m"]), None)
                t1 = hit["t"] if hit else t
            else:
                pairs.append((s, None))
                continue
            seg = _window(series, t, t1)
            pairs.append((s, dict(t0=t, t1=t1, secs=t1 - t, dist=(seg[-1]["d"] - d) if seg and seg[-1].get("d") is not None else None) if seg else None))
            t = t1
            d = seg[-1]["d"] if seg and seg[-1].get("d") is not None else d
    for s, lp in pairs:
        row = dict(kind=s["kind"], name=s.get("name") or KIND_NAMES[s["kind"]], rep=s.get("rep"), dur=s["dur"], target=s["target"],
                   actual=None)
        if lp:
            st = _stats(_window(series, lp.get("t0", 0), lp.get("t1", 0)), s["target"], zones)
            row["actual"] = dict(secs=round(lp["secs"]) if lp.get("secs") is not None else None,
                                 m=round(lp["dist"]) if lp.get("dist") is not None else None,
                                 hr=lp.get("hr") or st["hr"], speed=lp.get("speed") or st["speed"], in_target=st["in_target"])
        rows.append(row)
    judged = [(r["actual"]["in_target"], r["actual"]["secs"] or 0) for r in rows if r["actual"] and r["actual"]["in_target"] is not None]
    tot = sum(x[1] for x in judged)
    overall = round(sum(p * s for p, s in judged) / tot) if tot else None
    done = sum(1 for r in rows if r["actual"])
    return dict(steps=rows, alignment=how, done_steps=done, total_steps=len(rows), in_target=overall,
                activity={k: view.get(k) for k in ("id", "name", "type", "start")}, summary=view.get("summary"))


def demo_activity(w: dict, day: str, zones: dict | None) -> dict:
    """A synthetic activity view (series + laps) that follows a structured workout, mostly on target,
    so planned-vs-actual can be explored with demo data (no Garmin key)."""
    import random
    rnd = random.Random(w["id"])
    series, laps, t, d = [], [], 0.0, 0.0
    for s in timeline(w):
        v = step_speed(s, w["sport"], None) or 0.0
        dur, tgt = s["dur"], s["target"]
        secs = dur["sec"] if dur["type"] == "time" else dur["m"] / v if dur["type"] == "distance" and v else 300
        if tgt["type"] == "hr_zone":
            lo, hi = zone_range(zones, tgt["zone"]) or (120, 140)
        elif tgt["type"] == "hr_bpm":
            lo, hi = tgt["lo"], tgt["hi"]
        else:
            lo, hi = {"warmup": (115, 130), "work": (150, 165), "recovery": (125, 138), "rest": (100, 120), "cooldown": (110, 125)}[s["kind"]]
        t0, d0, n = t, d, max(4, int(secs // 10))
        for i in range(n):
            sp = tgt["lo"] + (tgt["hi"] - tgt["lo"]) * rnd.uniform(-0.3, 1.2) if tgt["type"] == "pace" else v * rnd.uniform(0.95, 1.05)
            t += secs / n
            d += sp * secs / n
            ramp = i < n * 0.2  # HR lags at the start of each step
            hr = round(lo - rnd.uniform(2, 12)) if ramp else round(rnd.uniform(lo - 3, hi + 2))
            series.append(dict(t=round(t, 1), d=round(d, 1) if v else None, speed=round(sp, 3) if v else None, hr=hr))
        pts = [p for p in series if t0 < p["t"] <= t]
        laps.append(dict(t0=round(t0, 1), t1=round(t, 1), secs=round(t - t0), dist=round(d - d0, 1) if v else None, step=s["index"],
                         hr=round(sum(p["hr"] for p in pts) / len(pts)), speed=round((d - d0) / (t - t0), 3) if v else None))
    return dict(id=None, name=w["name"], type={"run": "running", "ride": "cycling"}.get(w["sport"], "training"),
                start=f"{day} 07:00:00", series=series, laps=laps,
                summary=dict(distance=round(d) if d else None, duration=round(t), avg_hr=round(sum(p["hr"] for p in series) / len(series))))
