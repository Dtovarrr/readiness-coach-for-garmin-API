"""Training plans for events: deterministic structure, safety checks, daily adjustments.

Everything here is rule-based and testable; the coach only writes explanations around it. Values are
metric (distance plans in km, time plans in minutes); the page converts for display.

Rules the generator keeps:
  - weeks run Monday-Sunday from this week to race week;
  - weekly volume rises at most PROGRESSION (10%) over the last full (non-recovery) week, up to a peak
    target for the event; every RECOVERY_EVERY-th week (3rd with an injury flag) drops to ~70%;
  - the long session grows at most 10% a week and stays under a cap for the distance;
  - hard days never sit next to each other, one rest day a week is always kept, and the last day
    before the event is a rest day;
  - a taper: ~75% then ~50% of peak for long events, one lighter week otherwise.
Training guidance, not medical advice.
"""
from __future__ import annotations

import math
import uuid
from datetime import date, timedelta

import units

PROGRESSION = 1.10
RECOVERY_EVERY = 4
RECOVERY_EVERY_INJURED = 3
RECOVERY_FACTOR = 0.70
LONG_SHARE = 0.30
LOW_VOLUME = 0.60  # recent volume under 60% of the plan's starting week is flagged
MIN_EASY_KM, MIN_EASY_MIN = 3.0, 20.0
KINDS = ("run", "ride", "tennis", "general")
PRIORITIES = ("A", "B", "C")
DISCLAIMER = "Training guidance, not medical advice. Listen to your body and adjust."
INJURY_NOTE = ("You mentioned soreness or an injury: check with a doctor or physio before returning to structured training, "
               "and stop any session that brings the pain back.")

# Distance tiers (km): floor = smallest sensible starting week, peak target, long-session cap, minimum weeks
RUN_TIERS = [  # (up to km, name, floor, peak, long cap, min weeks, taper weeks)
    (5.5, "5K", 12, 32, 12, 4, 1),
    (11, "10K", 16, 42, 16, 6, 1),
    (22, "half marathon", 20, 55, 21, 10, 2),
    (1e9, "marathon", 30, 72, 32, 16, 2),
]
RIDE_TIERS = [
    (50, "ride up to 50 km", 50, 150, 60, 4, 1),
    (100, "century-length ride", 80, 220, 100, 6, 1),
    (1e9, "long ride", 100, 300, 160, 10, 2),
]
TIME_TIERS = {  # minutes per week: floor, peak, long cap, min weeks, taper weeks
    "tennis": (120, 300, 150, 2, 1),
    "general": (90, 240, 120, 2, 1),
}

# Weekly templates: offset from the long day -> slot. Hard slots (long, q1, q2) are never adjacent.
TEMPLATES = {
    3: [(0, "long"), (-3, "q1"), (-5, "easy")],
    4: [(0, "long"), (-2, "easy"), (-4, "q1"), (-6, "easy")],
    5: [(0, "long"), (-1, "easy"), (-3, "q2"), (-5, "q1"), (-6, "easy")],
    6: [(0, "long"), (-1, "easy"), (-2, "q2"), (-4, "q1"), (-5, "easy"), (-6, "easy")],
}
WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
FOCUS = {
    "Base": "Easy aerobic volume and one controlled quality session; build the habit before the intensity.",
    "Build": "Volume rises gently and quality sessions get more specific to the event.",
    "Peak": "Your biggest weeks: keep easy days truly easy so the key sessions land.",
    "Recovery": "A lighter week to absorb the training: about 30% less, no hard intervals.",
    "Taper": "Less volume, a little intensity to stay sharp. Sleep and fuel well.",
    "Race week": "Short and sharp early in the week, a rest day before the event, then go.",
    "Event week": "Light and sharp early in the week, a rest day before the event, then enjoy it.",
}


def monday(d: date) -> date:
    return d - timedelta(days=d.weekday())


def time_based(kind: str) -> bool:
    return kind in ("tennis", "general")


def tier(event: dict) -> dict:
    """The parameters for this event: floor/peak/long cap in km (run, ride) or minutes, min weeks, taper."""
    kind = event["kind"]
    if time_based(kind):
        floor, peak, cap, mw, taper = TIME_TIERS[kind]
        return dict(name=kind, floor=floor, peak=peak, long_cap=cap, min_weeks=mw, taper=taper)
    km = (event.get("distance_m") or 0) / 1000
    for up_to, name, floor, peak, cap, mw, taper in (RUN_TIERS if kind == "run" else RIDE_TIERS):
        if km <= up_to:
            return dict(name=name, floor=floor, peak=peak, long_cap=max(cap, km * 0.75 if kind == "run" else cap), min_weeks=mw, taper=taper)
    raise ValueError("unreachable")


# --------------------------------- volumes ---------------------------------- #
def volumes(n: int, start: float, peak: float, taper: int, recover_every: int) -> list[tuple[float, str]]:
    """(target volume, phase) per week; the last week is the event week. Build weeks rise at most 10%
    over the last full week and never above `peak`; every `recover_every`-th week recovers. `taper`
    counts the lighter weeks at the end including the event week (2 only with 8+ weeks to go)."""
    if n <= 0:
        return []
    taper = 2 if taper >= 2 and n >= 8 else 1
    build = n - taper
    peak = max(peak, start)  # never ask an already-fit athlete to do less than they do now
    out, last_full = [], start
    for i in range(build):
        if i == 0:
            v, phase = start, "Base"
        elif i % recover_every == recover_every - 1 and i < build - 1:
            v, phase = last_full * RECOVERY_FACTOR, "Recovery"
        else:
            v = min(peak, last_full * PROGRESSION)
            phase = "Base" if i < 2 else "Peak" if v >= 0.97 * peak else "Build"
        if phase != "Recovery":
            last_full = v
        out.append((v, phase))
    top = max([v for v, _ in out] or [start])
    if taper == 2:
        out.append((top * 0.75, "Taper"))
    out.append((top * (0.5 if taper == 2 else 0.6), "Event week"))
    return out


# --------------------------------- sessions --------------------------------- #
def _id() -> str:
    return uuid.uuid4().hex[:10]


def _wk(kind: str, d: date, wtype: str, title: str, intensity: str, amount: float, week: int, **detail) -> dict:
    w = dict(id=_id(), date=d.isoformat(), sport=kind, type=wtype, title=title, intensity=intensity, week=week,
             status="planned", note="", detail=detail)
    if time_based(kind) or wtype in ("strength", "mobility"):
        w["minutes"] = round(amount)
    else:
        w["distance_m"] = round(amount * 1000, -2)  # nearest 100 m
    return w


def _quality(kind: str, slot: str, phase: str, injured: bool, tier_: dict, week: int, wk_ratio: float):
    """(type, title, intensity, share of the week, detail) for a quality slot in this phase."""
    no_hard = injured or phase in ("Recovery",) or (phase == "Base" and week < 2)
    if kind == "run":
        reps = {"5K": (6, 800), "10K": (5, 1000), "half marathon": (4, 1600), "marathon": (3, 2000)}[tier_["name"]]
        tempo = round(15 + 15 * wk_ratio)
        if (slot == "q2" and no_hard) or phase == "Recovery":  # one controlled quality session in base/recovery weeks
            return "easy", "Easy run + strides", "easy", 0.14, dict(strides=6)
        if slot == "q2" or (slot == "q1" and no_hard):
            return "tempo", "Tempo run", "moderate" if no_hard else "hard", 0.18, dict(tempo_min=tempo if not no_hard else 15)
        return "intervals", "Intervals", "hard", 0.16, dict(reps=reps[0], rep_m=reps[1], recovery="2–3 min easy jog")
    if kind == "ride":
        if (slot == "q2" and no_hard) or phase == "Recovery":
            return "easy", "Easy spin", "easy", 0.12, {}
        if slot == "q2" or (slot == "q1" and no_hard):
            return "tempo", "Tempo ride", "moderate" if no_hard else "hard", 0.18, dict(blocks="2 × 15 min at tempo")
        return "intervals", "Ride intervals", "hard", 0.15, dict(reps=5, rep_min=4, recovery="4 min easy")
    if kind == "tennis":
        if slot == "q1" and not no_hard:
            return "conditioning", "Court conditioning", "hard", 0.15, dict(blocks="8 × 30 s sprints / lateral shuffles, 90 s rest")
        if slot == "q1":
            return "drills", "Footwork & drills", "moderate", 0.18, {}
        return "drills", "Drills: serve, return, patterns", "moderate", 0.2, {}
    if slot == "q1" and not no_hard:
        return "intervals", "Intervals (bike, row or run)", "hard", 0.15, dict(reps=6, rep_min=2, recovery="2 min easy")
    return "strength", "Strength", "moderate", 0.18, dict(focus="full body, 2–3 sets")


def _easy(kind: str, i: int) -> tuple[str, str, str]:
    if kind == "run":
        return "easy", "Easy run", "easy"
    if kind == "ride":
        return "easy", "Easy ride", "easy"
    if kind == "tennis":
        return ("strength", "Strength & mobility", "easy") if i % 2 else ("hitting", "Easy hitting", "easy")
    return ("mobility", "Mobility & core", "easy") if i % 2 else ("cardio", "Easy cardio", "easy")


def _long(kind: str) -> tuple[str, str, str]:
    return {"run": ("long", "Long run", "moderate"), "ride": ("long", "Long ride", "moderate"),
            "tennis": ("match", "Match play", "hard"), "general": ("long", "Long cardio session", "moderate")}[kind]


def week_sessions(kind: str, start: date, end: date, v: float, phase: str, week: int, days: int, long_day: int,
                  long_amt: float, tier_: dict, injured: bool, wk_ratio: float, first: date) -> list[dict]:
    """One week's workouts from the template, on days between `first` and `end`."""
    slots = sorted(((long_day + off) % 7, slot) for off, slot in TEMPLATES[days])
    plan, easy_i = [], 0
    q = {s: _quality(kind, s, phase, injured, tier_, week, wk_ratio) for _, s in slots if s in ("q1", "q2")}
    used = long_amt + sum(share * v for _, _, _, share, _ in q.values())
    easy_slots = [wd for wd, s in slots if s == "easy" and first <= start + timedelta(days=wd) <= end]
    min_easy = MIN_EASY_MIN if time_based(kind) else MIN_EASY_KM
    easy_amt = (v - used) / len(easy_slots) if easy_slots else 0
    while easy_slots and easy_amt < min_easy:  # too little volume for every easy slot: rest instead
        easy_slots.pop()
        easy_amt = (v - used) / len(easy_slots) if easy_slots else 0
    for wd, s in slots:
        d = start + timedelta(days=wd)
        if not first <= d <= end:
            continue
        if s == "long":
            t, title, inten = _long(kind)
            plan.append(_wk(kind, d, t, title, inten if phase != "Recovery" else "easy", long_amt, week))
        elif s in q:
            t, title, inten, share, detail = q[s]
            plan.append(_wk(kind, d, t, title, inten, share * v, week, **detail))
        elif wd in easy_slots:
            t, title, inten = _easy(kind, easy_i)
            easy_i += 1
            amt = easy_amt if t not in ("strength", "mobility") or not time_based(kind) else min(easy_amt, 45)
            plan.append(_wk(kind, d, t, title, inten, amt, week, **({"strides": 4} if kind == "run" and phase in ("Build", "Peak") and easy_i == 1 else {})))
    return plan


def event_week(kind: str, event: dict, start: date, first: date, v: float, week: int, days: int) -> list[dict]:
    """Race week: a short sharpener 4 days out, easy days, a rest day before the event, then the event."""
    ev = date.fromisoformat(event["date"])
    out = []
    if ev >= first:
        dist = (event.get("distance_m") or 0) / 1000
        race = _wk(kind, ev, "race", event["name"], "race", dist, week)
        if not dist or time_based(kind):
            race.pop("distance_m", None)
            race["minutes"] = None
        out.append(race)
    pre = [ev - timedelta(days=k) for k in range(2, 7) if ev - timedelta(days=k) >= max(start, first)]  # never the day before
    count = min(days - 1, len(pre))
    if count <= 0:
        return out
    sharp = ev - timedelta(days=4) if ev - timedelta(days=4) in pre else None
    chosen = ([sharp] if sharp else []) + [d for d in pre if d != sharp][:count - (1 if sharp else 0)]
    min_amt = MIN_EASY_MIN if time_based(kind) else MIN_EASY_KM
    each = max(min_amt, v / len(chosen))
    for d in sorted(chosen):
        if d == sharp:
            title = {"run": "Race-pace sharpener", "ride": "Openers", "tennis": "Sharp hitting & serves", "general": "Short sharp session"}[kind]
            out.append(_wk(kind, d, "sharpener", title, "moderate", max(min_amt, each * 0.8), week,
                           **({"reps": 3, "rep_min": 3, "recovery": "at race pace, 2 min easy between"} if kind in ("run", "ride") else {})))
        else:
            t, title, _ = _easy(kind, 0)
            out.append(_wk(kind, d, t, title, "easy", max(min_amt, each * 0.7), week))
    return sorted(out, key=lambda w: w["date"])


# --------------------------------- generator -------------------------------- #
def generate(event: dict, inputs: dict, today: date) -> dict:
    """The whole plan. inputs: current_weekly (km or min), longest_recent (km or min), days (3-6),
    long_day (0=Mon..6=Sun), injured (bool). Raises ValueError for C events or past dates."""
    if event.get("priority") not in ("A", "B"):
        raise ValueError("Plans are built for A and B events; C events are tune-ups you fit around your training.")
    ev = date.fromisoformat(event["date"])
    if ev < today:
        raise ValueError("That event has already happened.")
    kind, t = event["kind"], tier(event)
    days = max(3, min(6, int(inputs.get("days") or 4)))
    long_day = int(inputs.get("long_day") if inputs.get("long_day") is not None else 5) % 7
    injured = bool(inputs.get("injured"))
    current = max(0.0, float(inputs.get("current_weekly") or 0))
    longest = max(0.0, float(inputs.get("longest_recent") or 0))
    n = (monday(ev) - monday(today)).days // 7 + 1
    trained = current >= 0.8 * t["peak"]
    start = max(current, t["floor"])
    if injured:
        start = max(t["floor"] * 0.8, min(start, current * 0.8 if current else t["floor"] * 0.8))
    taper = t["taper"] if event["priority"] == "A" else min(1, t["taper"])
    vols = volumes(n, start, t["peak"], taper, RECOVERY_EVERY_INJURED if injured else RECOVERY_EVERY)
    workouts, weeks = [], []
    prev_long = None
    top = max([v for v, _ in vols] or [start])
    for i, (v, phase) in enumerate(vols):
        ws = monday(today) + timedelta(weeks=i)
        we = ws + timedelta(days=6)
        first = max(ws, today)
        if phase == "Event week":
            phase = "Race week" if kind in ("run", "ride") else "Event week"
            wk = event_week(kind, event, ws, first, v, i, days)
            long_amt = None
        else:
            want = min(t["long_cap"], LONG_SHARE * v)
            if prev_long is None:
                long_amt = min(want, max(longest * PROGRESSION, (MIN_EASY_MIN if time_based(kind) else MIN_EASY_KM) * 1.5)) if longest else want
            elif phase == "Recovery":
                long_amt = prev_long * 0.75
            else:
                long_amt = min(want, prev_long * PROGRESSION)
            if phase != "Recovery":
                prev_long = long_amt
            wk = week_sessions(kind, ws, we, v, phase, i, days, long_day, long_amt, t, injured, v / top if top else 0, first)
        workouts += wk
        weeks.append(dict(index=i, start=ws.isoformat(), end=we.isoformat(), phase=phase, focus=FOCUS[phase], target=round(v, 1),
                          long=round(long_amt, 1) if long_amt else None, partial=first > ws))
    plan = dict(event_id=event.get("id"), kind=kind, unit="min" if time_based(kind) else "km", tier=t["name"], generated=today.isoformat(),
                inputs=dict(current_weekly=round(current, 1), longest_recent=round(longest, 1), days=days, long_day=long_day,
                            injured=injured, start=round(start, 1), peak=round(max(t["peak"], start), 1), trained=trained),
                weeks=weeks, workouts=sorted(workouts, key=lambda w: w["date"]), regenerated=None)
    return plan


# ---------------------------------- totals ---------------------------------- #
def amount(w: dict, unit: str) -> float:
    """A workout's contribution to weekly volume in the plan's unit (races don't count)."""
    if w["type"] == "race" or w.get("status") == "skipped":
        return 0.0
    if unit == "km":
        return (w.get("distance_m") or 0) / 1000
    return float(w.get("minutes") or 0)


def week_totals(plan: dict) -> list[float]:
    tot = {wk["index"]: 0.0 for wk in plan["weeks"]}
    by_start = {wk["start"]: wk["index"] for wk in plan["weeks"]}
    for w in plan["workouts"]:
        k = monday(date.fromisoformat(w["date"])).isoformat()
        if k in by_start:
            tot[by_start[k]] += amount(w, plan["unit"])
    return [round(tot[i], 2) for i in sorted(tot)]


# ---------------------------------- safety ---------------------------------- #
def overlaps(plan: dict, others: list[tuple[str, dict]], today: date) -> dict[str, list[str]]:
    """Other plans' names -> upcoming days where both plans have a hard session."""
    mine = {w["date"] for w in plan["workouts"] if hard(w) and w.get("status") != "skipped" and w["date"] >= today.isoformat()}
    out = {}
    for name, p in others:
        both = sorted(mine & {w["date"] for w in p["workouts"] if hard(w) and w.get("status") != "skipped"})
        if both:
            out[name] = both
    return out


def check(plan: dict, event: dict, today: date, system: str = units.DEFAULT, overlaps: dict | None = None) -> list[dict]:
    """Warnings (level, kind, text): too soon, recent volume far below the start, unsafe weekly jumps
    (after your edits too), the injury note and clashes with another plan. Always ends with the
    guidance disclaimer."""
    out = []
    t, inp = tier(event), plan["inputs"]
    fmt = (lambda v: f"{v:.0f} min") if plan["unit"] == "min" else (lambda v: units.fmt_dist(v * 1000, system, 0))
    weeks_left = len(plan["weeks"])
    need = max(2, t["min_weeks"] // 2) if inp.get("trained") else t["min_weeks"]
    if weeks_left < need:
        out.append(dict(level="warn", kind="too_soon", text=(
            f"{weeks_left} week{'s' if weeks_left != 1 else ''} is short for a {t['name']}: about {need}+ weeks is typical from your "
            "current base. This plan is shortened and won't build much fitness; consider making it a B or C event, or treat it "
            "as a steady effort rather than a race.")))
    if inp["current_weekly"] < LOW_VOLUME * inp["start"]:
        out.append(dict(level="warn", kind="low_volume", text=(
            f"Your last 4 weeks averaged {fmt(inp['current_weekly'])} a week, but the plan starts at {fmt(inp['start'])}. "
            "Ease in: shorten the first week's sessions if they feel hard, and don't make up missed ones.")))
    tot = week_totals(plan)
    for i in range(1, len(tot)):
        if plan["weeks"][i]["phase"] in ("Race week", "Event week", "Taper"):
            continue
        # a part-week (the plan started mid-week) is judged by its planned target, not what's left of it
        prior = [plan["weeks"][j]["target"] if plan["weeks"][j].get("partial") else tot[j] for j in range(max(0, i - 2), i)]
        ref = max(prior)
        slack = 5 if plan["unit"] == "min" else 0.5  # sessions are rounded (100 m / 1 min); that's not a jump
        if ref > 0 and tot[i] > ref * PROGRESSION + slack:
            out.append(dict(level="caution", kind="increase", week=i, text=(
                f"Week {i + 1} ({fmt(tot[i])}) is {round((tot[i] / ref - 1) * 100)}% more than the week before; "
                "more than ~10% a week raises injury risk. Consider moving or shortening a session.")))
    if inp.get("injured"):
        out.append(dict(level="caution", kind="injury", text=INJURY_NOTE))
    for name, days in (overlaps or {}).items():
        out.append(dict(level="caution", kind="overlap", text=(
            f"Your plan for {name} also has a hard session on {len(days)} of the same day{'s' if len(days) != 1 else ''} "
            f"(first: {date.fromisoformat(days[0]):%b %-d}). Two hard sessions in a day add up fast: move one, or make one event a C.")))
    out.append(dict(level="info", kind="disclaimer", text=DISCLAIMER))
    return out


# -------------------------------- done / missed ------------------------------ #
SPORT_MATCH = {"run": ("run",), "ride": ("cycl", "bik", "ride"), "tennis": ("tennis", "pickle", "padel", "squash", "badminton"),
               "general": ("",)}


def matches(w: dict, a: dict) -> bool:
    t = (a.get("type") or "").lower()
    if w["type"] in ("strength", "mobility"):
        return any(x in t for x in ("strength", "yoga", "pilates", "mobility", "training"))
    if w["sport"] == "general":
        return "walk" not in t
    return any(x in t for x in SPORT_MATCH[w["sport"]])


def completion(w: dict, acts: list[dict]) -> dict | None:
    """The activity that completes a planned workout: same day, matching sport, at least half the
    planned distance or duration."""
    for a in acts or []:
        if not matches(w, a):
            continue
        if w.get("distance_m"):
            if (a.get("dist_m") or 0) >= 0.5 * w["distance_m"]:
                return a
        elif w.get("minutes"):
            if (a.get("secs") or 0) >= 0.5 * w["minutes"] * 60:
                return a
        else:
            return a
    return None


def statuses(plan: dict, acts_by_day: dict[str, list[dict]], today: date) -> dict[str, dict]:
    """wid -> dict(state: done|missed|skipped|planned|today, activity). Never stored; recomputed."""
    out = {}
    for w in plan["workouts"]:
        if w.get("status") == "skipped":
            out[w["id"]] = dict(state="skipped")
            continue
        a = completion(w, acts_by_day.get(w["date"]))
        if a:
            out[w["id"]] = dict(state="done", activity={k: a.get(k) for k in ("id", "name", "type", "dist_m", "secs")})
        elif w["date"] < today.isoformat():
            out[w["id"]] = dict(state="missed")
        else:
            out[w["id"]] = dict(state="today" if w["date"] == today.isoformat() else "planned")
    return out


def hard(w: dict) -> bool:
    return w.get("intensity") in ("hard", "race") or w["type"] in ("long", "match", "race")


def free_day(plan: dict, after: date, until: date, event_day: date, skip_id: str | None = None) -> date | None:
    """The first day in [after, until] with no session, not the day before the event, and not next to
    a hard session (so a moved or rescheduled workout never creates back-to-back hard days)."""
    by_day: dict[str, list[dict]] = {}
    for w in plan["workouts"]:
        if w["id"] != skip_id and w.get("status") != "skipped":
            by_day.setdefault(w["date"], []).append(w)
    d = after
    while d <= until:
        k = d.isoformat()
        near = [x for dd in (d - timedelta(days=1), d + timedelta(days=1)) for x in by_day.get(dd.isoformat(), [])]
        if k not in by_day and d < event_day - timedelta(days=1) and not any(hard(x) for x in near):
            return d
        d += timedelta(days=1)
    return None


def missed(plan: dict, st: dict[str, dict], event_day: date, today: date) -> list[dict]:
    """Missed workouts from the last 7 days, each with a reschedule day in its own week if there's a
    sensible free one (never crammed next to hard days); otherwise only skipping is offered."""
    out = []
    for w in plan["workouts"]:
        if st.get(w["id"], {}).get("state") != "missed" or w["date"] < (today - timedelta(days=7)).isoformat() or w.get("dismissed_missed"):
            continue
        week_end = monday(date.fromisoformat(w["date"])) + timedelta(days=6)
        slot = free_day(plan, today, min(week_end, event_day), event_day, skip_id=w["id"]) if w["type"] != "race" else None
        out.append(dict(workout=w, reschedule=slot.isoformat() if slot else None))
    return out


# ------------------------------- daily adjustment ------------------------------ #
def easier_changes(w: dict) -> dict:
    """The easier, ~30% shorter version of a session (used by readiness suggestions and coach proposals)."""
    easier = dict(type="easy" if w["sport"] in ("run", "ride") else ("hitting" if w["sport"] == "tennis" else "cardio"),
                  intensity="easy", title=("Easy " + {"run": "run", "ride": "ride", "tennis": "hitting", "general": "cardio"}[w["sport"]]))
    if w.get("distance_m"):
        easier["distance_m"] = round(w["distance_m"] * 0.7, -2)
    if w.get("minutes"):
        easier["minutes"] = round(w["minutes"] * 0.7)
    return easier


def suggest(w: dict, assessment: dict, plan: dict, event_day: date, today: date) -> dict | None:
    """A suggestion for today's planned workout given this morning's readiness, or None. Suggestions
    only; nothing changes unless you accept."""
    if not w or w["type"] == "race" or w.get("status") == "skipped" or w.get("dismissed") == today.isoformat() or w.get("adjusted"):
        return None
    zone, score, flags = assessment.get("zone"), assessment.get("readiness"), assessment.get("flags") or []
    is_hard = hard(w)
    low = zone == "recover" or (zone == "caution" and is_hard) or (flags and is_hard)
    if not low:
        return None
    why = f"Readiness is {score} ({ {'recover': 'recover', 'caution': 'take it easy'}.get(zone, zone)})" if score is not None else "Readiness is low"
    if flags:
        why += f" and {flags[0][0].lower() + flags[0][1:]}"
    week_end = monday(today) + timedelta(days=6)
    move = free_day(plan, today + timedelta(days=1), min(week_end, event_day), event_day, skip_id=w["id"])
    opts = []
    if is_hard or zone == "recover":
        opts.append(dict(action="easier", label="Swap to an easier, shorter session", changes=easier_changes(w)))
    if move:
        opts.append(dict(action="move", label=f"Move it to {WEEKDAYS[move.weekday()]}", changes=dict(date=move.isoformat())))
    opts.append(dict(action="skip", label="Take a rest day instead", changes=dict(status="skipped")))
    return dict(workout_id=w["id"], text=f"{why}. Today's {w['title'].lower()} is a {'hard' if is_hard else 'planned'} session.", options=opts)


# --------------------------------- editing ---------------------------------- #
EDITABLE = ("date", "type", "title", "intensity", "distance_m", "minutes", "note", "status")


def edit(plan: dict, wid: str, changes: dict, reason: str | None = None) -> dict:
    w = next((x for x in plan["workouts"] if x["id"] == wid), None)
    if not w:
        raise KeyError(wid)
    for k, v in changes.items():
        if k in EDITABLE:
            w[k] = v
    if reason:
        w["adjusted"] = reason
    plan["workouts"].sort(key=lambda x: x["date"])
    return w


def regenerate(plan: dict, event: dict, inputs: dict, today: date) -> dict:
    """A new plan from today (e.g. after the event date changed), keeping everything before today."""
    kept = [w for w in plan["workouts"] if w["date"] < today.isoformat()]
    fresh = generate(event, inputs, today)
    fresh["workouts"] = kept + fresh["workouts"]
    fresh["weeks"] = [w for w in plan["weeks"] if w["end"] < monday(today).isoformat()] + fresh["weeks"]
    fresh["regenerated"] = today.isoformat()
    return fresh


# ---------------------------------- wording --------------------------------- #
def rule_overview(plan: dict, event: dict, system: str = units.DEFAULT) -> str:
    inp, wks = plan["inputs"], plan["weeks"]
    fmt = (lambda v: f"{v:.0f} min") if plan["unit"] == "min" else (lambda v: units.fmt_dist(v * 1000, system, 0))
    rec = sum(1 for w in wks if w["phase"] == "Recovery")
    taper = sum(1 for w in wks if w["phase"] == "Taper")
    lines = [f"### The plan", f"- **{len(wks)} weeks** to {event['name']} ({plan['tier']}), {inp['days']} sessions a week with your long session on {WEEKDAYS[inp['long_day']]}.",
             f"- **Volume:** starts at {fmt(inp['start'])} a week (you've averaged {fmt(inp['current_weekly'])}) and builds by no more than 10% a week toward about {fmt(max(w['target'] for w in wks))}.",
             f"- **Recovery:** {rec} lighter week{'s' if rec != 1 else ''} to absorb the work" + (", every third week because you flagged soreness or an injury." if inp.get("injured") else ", every fourth week."),
             f"- **Taper:** {taper + 1} lighter week{'s' if taper else ''} at the end so you arrive fresh." if taper else "- **Event week:** short and sharp, with a rest day before.",
             "", "### How to use it",
             "- Easy means conversational. Most of the plan is easy on purpose.",
             "- Each morning the plan is checked against your readiness; low readiness gets a suggestion to go easier or move the session. You decide.",
             "- Missed a session? Skip it or move it within the same week. Don't stack sessions to catch up.",
             "", f"_{DISCLAIMER}_"]
    return "\n".join(lines)


def coach_payload(plan: dict, event: dict, warnings: list[dict]) -> dict:
    """Derived numbers only; the injury note's text is never included, only that one was flagged."""
    unit = plan["unit"]
    vol = (lambda v: dict(km=round(v, 1))) if unit == "km" else (lambda v: dict(minutes=round(v)))
    return dict(event=dict(kind=event["kind"], tier=plan["tier"], priority=event["priority"], weeks_until=len(plan["weeks"]),
                           distance_km=round((event.get("distance_m") or 0) / 1000, 2) or None),
                inputs=dict(days_per_week=plan["inputs"]["days"], long_day=WEEKDAYS[plan["inputs"]["long_day"]],
                            soreness_or_injury_flagged=plan["inputs"]["injured"],
                            **{f"recent_weekly_{k}": v for k, v in vol(plan["inputs"]["current_weekly"]).items()},
                            **{f"plan_start_weekly_{k}": v for k, v in vol(plan["inputs"]["start"]).items()}),
                weeks=[dict(n=w["index"] + 1, phase=w["phase"], **{f"target_{k}": v for k, v in vol(w["target"]).items()}) for w in plan["weeks"]],
                warnings=[w["text"] for w in warnings if w["kind"] != "disclaimer"])


PLAN_SYSTEM = (
    "You are an evidence-minded endurance and sports coach in a personal dashboard. You receive a training plan's structure, "
    "built by rules (weeks, phases, weekly targets, the athlete's recent volume, days per week, warnings). Do not change "
    "the structure or invent sessions; explain it. Write ~180-260 words of markdown with exactly these ### headings: "
    "'The idea' (how the phases build toward the event), 'Key sessions' (what the long, tempo/threshold and interval or "
    "event-specific sessions are for, and how hard they should feel), 'Staying healthy' (easy days easy, the 10% guideline, "
    "recovery weeks, sleep; address each warning given). If soreness_or_injury_flagged is true, recommend checking with a "
    "doctor or physio before returning to training. End with: training guidance, not medical advice."
)


def claude_overview(plan: dict, event: dict, warnings: list[dict], system: str = units.DEFAULT) -> str:
    import coach
    return coach._claude(PLAN_SYSTEM + units.instruction(system),
                         f"DATA:\n{coach._data(coach_payload(plan, event, warnings), system)}\n\nExplain this plan.",
                         feature="plan_overview", essential=False)
