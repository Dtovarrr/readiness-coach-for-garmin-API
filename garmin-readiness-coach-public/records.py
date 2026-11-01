"""Personal records: data-quality filters, record detection, previous bests, progress and badges.

Pure functions over two cached histories (records_sync.py builds them):
  activities: dict(id, name, type, start "YYYY-MM-DD HH:MM:SS", date, dist_m, secs (moving), gain_m,
              load, hr, max_hr, speed_ms, splits={"mile"|"5k"|"10k"|"half": seconds or None})
  days:       {iso date: dict(steps, rhr, im, hrv, sleep_score, weight_kg)}
Everything stays metric (m, s, kg); the page converts for display.

Bad data is set aside before anything is compared, with a reason the page can show. Running best
efforts come from Garmin's own fastest splits (exact); without them a run that covers the distance
is scaled from its whole-activity time and flagged as estimated.
"""
from __future__ import annotations

import logging
import math
import statistics as st
from collections import Counter
from datetime import date, timedelta

log = logging.getLogger("garmin_coach")

MILE = 1609.344
EFFORTS = {  # key -> distance (m), label
    "mile": (MILE, "Fastest mile"),
    "5k": (5000.0, "Fastest 5K"),
    "10k": (10000.0, "Fastest 10K"),
    "half": (21097.5, "Fastest half marathon"),
}
SPLIT_FIELDS = {"mile": "fastestSplit_1609", "5k": "fastestSplit_5000", "10k": "fastestSplit_10000", "half": "fastestSplit_21098"}
WORLD_RECORD = {"mile": 223.0, "5k": 755.0, "10k": 1571.0, "half": 3451.0}  # seconds; anything faster is a glitch
WHOLE_RUN_MAX = 1.5  # a run without splits estimates an effort only if it's at most 1.5x the distance
PACE_MIN_M = 3 * MILE  # "fastest average pace" needs a run over 3 miles
MAX_SPEED = {"run": 6.0, "ride": 25.0, "walk": 3.0, "hike": 3.0}  # m/s averages beyond these are impossible
MIN_SECS, MAX_SECS = 60, 24 * 3600
MAX_GRADE = 0.25  # elevation gain per metre of distance
HR_RANGE = (30, 230)
HR_MIN_SECS = 600  # "highest average HR" needs a 10-minute session
MAX_LOAD = 1500
DAY_RANGES = {"steps": (0, 100000), "hrv": (10, 250), "rhr": (30, 120), "sleep_score": (0, 100), "im": (0, 1440)}
WEIGHT_OUTLIER = 0.10  # a weigh-in this far from the median of the 30 days around it is dropped
STREAK_WALK_SECS = 20 * 60  # a walk makes an active day from 20 minutes
NEW_DAYS = 30  # a record set in the last 30 days is a "New PR"
PERIODS = ("all", "year", "12m")
FAMILIES = ("run", "ride", "swim", "walk", "hike", "strength", "racket", "other")


# --------------------------------- types ------------------------------------- #
def family(t: str | None) -> str:
    t = (t or "").lower()
    if "swim" in t:
        return "swim"
    if "run" in t or "track" in t:
        return "run"
    if "walk" in t:
        return "walk"
    if "hik" in t:
        return "hike"
    if any(x in t for x in ("cycl", "bik", "ride")):
        return "ride"
    if any(x in t for x in ("strength", "weight", "lift")):
        return "strength"
    if any(x in t for x in ("tennis", "pickle", "padel", "squash", "badminton", "racket", "racquet")):
        return "racket"
    return "other"


def is_indoor_run(t: str | None) -> bool:
    t = (t or "").lower()
    return family(t) == "run" and any(x in t for x in ("treadmill", "indoor", "virtual"))


def is_distance(t: str | None) -> bool:
    return family(t) in ("run", "ride", "walk", "hike", "swim")


# -------------------------------- data quality -------------------------------- #
ISSUE_TEXT = {
    "too_short": "activities shorter than a minute",
    "too_long": "activities longer than 24 hours",
    "impossible_pace": "activities with an impossible average pace or speed (GPS glitches)",
    "effort_world_record": "best efforts faster than the world record",
    "effort_spike": "best efforts far faster than the rest of the run (GPS spikes)",
    "effort_longer": "best efforts longer than the run itself",
    "elevation": "elevation readings steeper than any real route",
    "heart_rate": "heart-rate averages outside 30–230 bpm",
    "load": "training-load values over 1,500",
    "day_value": "daily readings outside a plausible range",
    "weight_outlier": "weigh-ins far from your usual weight (another person or a scale glitch)",
    "day_unreadable": "daily readings in a format the app couldn't read",
}
NUM_FIELDS = ("dist_m", "secs", "gain_m", "load", "hr", "max_hr", "speed_ms")


def number(v) -> float | None:
    """A finite number from whatever Garmin sent: ints/floats as they are, {"value": x} objects (as
    some summaries nest them) unwrapped; anything else (text, bools, NaN, lists) is None."""
    if isinstance(v, dict) and "value" in v:
        v = v["value"]
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
        return None
    return float(v)


def readable(a) -> tuple[dict | None, str | None]:
    """(normalised copy, None) or (None, why it can't be used). Numbers are coerced with number();
    a missing date or duration makes an activity unusable, other gaps just become None."""
    if not isinstance(a, dict):
        return None, "not an activity record"
    d = str(a.get("date") or a.get("start") or "")[:10]
    try:
        date.fromisoformat(d)
    except ValueError:
        return None, f"start date isn't a date ({str(a.get('start') or a.get('date') or 'missing')[:25]!r})"
    out = dict(a, date=d, type=str(a["type"]) if a.get("type") else None, start=str(a.get("start") or d),
               **{k: number(a.get(k)) for k in NUM_FIELDS})
    if not out["secs"]:
        return None, "no duration"
    sp = a.get("splits") if isinstance(a.get("splits"), dict) else {}
    out["splits"] = {k: number(sp.get(k)) for k in EFFORTS if number(sp.get(k))}
    return out, None


def effort_issue(key: str, secs: float | None, dist_m: float, moving_s: float) -> str | None:
    """Why a best effort can't be trusted, or None. The faster an effort is than the run's average
    the more suspicious it gets, and the closer it is to the whole run the less room it has."""
    if not secs or secs <= 0:
        return None
    d = EFFORTS[key][0]
    if secs < WORLD_RECORD[key]:
        return "effort_world_record"
    if dist_m and dist_m < d * 0.98:
        return "effort_longer"
    if dist_m and moving_s:
        f = min(1.0, d / dist_m)
        allowed = 1.1 + 0.6 * (1 - f)
        if d / secs > dist_m / moving_s * allowed:
            return "effort_spike"
    return None


def clean_activity(a: dict) -> tuple[dict | None, list[str]]:
    """(cleaned copy or None if unusable, issues). Whole-activity problems drop it; a bad field
    (elevation, HR, load, one best effort) is cleared and the rest is kept."""
    secs, dist, fam = a.get("secs") or 0, a.get("dist_m") or 0, family(a.get("type"))
    if secs < MIN_SECS:
        return None, ["too_short"]
    if secs > MAX_SECS:
        return None, ["too_long"]
    if fam in MAX_SPEED and dist and dist / secs > MAX_SPEED[fam]:
        return None, ["impossible_pace"]
    out, issues = dict(a, splits=dict(a.get("splits") or {})), []
    if out.get("gain_m") and dist and out["gain_m"] / dist > MAX_GRADE:
        out["gain_m"] = None
        issues.append("elevation")
    for k in ("hr", "max_hr"):
        if out.get(k) is not None and not HR_RANGE[0] <= out[k] <= HR_RANGE[1]:
            out[k] = None
            if k == "hr":
                issues.append("heart_rate")
    if out.get("load") is not None and not 0 <= out["load"] <= MAX_LOAD:
        out["load"] = None
        issues.append("load")
    if fam == "run":
        for k in EFFORTS:
            why = effort_issue(k, out["splits"].get(k), dist, secs)
            if why:
                out["splits"][k] = None
                issues.append(why)
    return out, issues


def clean_days(days: dict[str, dict]) -> tuple[dict[str, dict], Counter]:
    out, issues = {}, Counter()
    for k, d in days.items():
        try:
            date.fromisoformat(str(k))
        except ValueError:
            issues["day_unreadable"] += 1
            continue
        row = {}
        for f, raw in (d or {}).items() if isinstance(d, dict) else ():
            v = number(raw)
            if raw is not None and v is None:
                issues["day_unreadable"] += 1
                continue
            lo_hi = DAY_RANGES.get(f)
            if v is not None and lo_hi and not lo_hi[0] <= v <= lo_hi[1]:
                issues["day_value"] += 1
                continue
            row[f] = v
        out[k] = row
    weights = sorted((k, r["weight_kg"]) for k, r in out.items() if r.get("weight_kg"))
    for k, w in weights:
        d0 = date.fromisoformat(k)
        near = [v for j, v in weights if abs((date.fromisoformat(j) - d0).days) <= 15]
        if len(near) >= 3 and abs(w - st.median(near)) / st.median(near) > WEIGHT_OUTLIER:
            out[k]["weight_kg"] = None
            issues["weight_outlier"] += 1
    return out, issues


def clean(acts: list[dict], days: dict[str, dict]) -> tuple[list[dict], dict[str, dict], list[dict], list[dict]]:
    """(usable activities, days, ignored-data summary, skipped activities). An activity that can't be
    read or processed is skipped with its reason; it never fails the rest."""
    good, issues, skipped = [], Counter(), []
    for a in acts:
        try:
            n, why = readable(a)
            if n is None:
                skipped.append(_skip(a, why))
                continue
            c, problems = clean_activity(n)
        except Exception as ex:  # an input nobody anticipated: skip it, keep the rest
            log.warning("records: skipped activity %s: %s", a.get("id") if isinstance(a, dict) else "?", ex)
            skipped.append(_skip(a, f"couldn't be processed ({type(ex).__name__})"))
            continue
        issues.update(problems)
        if c:
            good.append(c)
    days, di = clean_days(days)
    issues.update(di)
    ignored = [dict(kind=k, count=n, text=ISSUE_TEXT[k]) for k, n in issues.most_common()]
    return good, days, ignored, skipped


def _skip(a, why: str) -> dict:
    a = a if isinstance(a, dict) else {}
    return dict(id=a.get("id"), date=str(a.get("date") or a.get("start") or "")[:10] or None,
                name=a.get("name"), type=a.get("type") if isinstance(a.get("type"), str) else None, reason=why)


# ---------------------------------- helpers ----------------------------------- #
def since(period: str, today: date) -> date | None:
    if period == "year":
        return date(today.year, 1, 1)
    if period == "12m":
        return today - timedelta(days=365)
    return None


def monday(d: date) -> date:
    return d - timedelta(days=d.weekday())


def best(items: list[dict], lower: bool = False) -> tuple[dict | None, dict | None]:
    """(record, previous best): walk the items in date order keeping the best so far; the previous
    best is the one the record holder beat. Ties keep the earlier one."""
    cur = prev = None
    for it in sorted(items, key=lambda x: (x["date"], x.get("start") or "")):
        if it["value"] is None:
            continue
        if cur is None or (it["value"] < cur["value"] if lower else it["value"] > cur["value"]):
            prev, cur = cur, it
    return cur, prev


def _record(key, label, group, unit, items, lower=False, note=None) -> dict | None:
    rec, prev = best(items, lower)
    if not rec:
        return None
    out = dict(key=key, label=label, group=group, unit=unit, lower=lower, value=rec["value"], date=rec["date"],
               end=rec.get("end"), activity_id=rec.get("activity_id"), activity=rec.get("activity"),
               source=rec.get("source"), estimated=bool(rec.get("estimated")), note=note or rec.get("note"),
               prev=None, improvement=None)
    if prev:
        out["prev"] = dict(value=prev["value"], date=prev["date"], activity_id=prev.get("activity_id"), estimated=bool(prev.get("estimated")))
        diff = (prev["value"] - rec["value"]) if lower else (rec["value"] - prev["value"])
        out["improvement"] = dict(abs=round(diff, 6), pct=round(diff / prev["value"] * 100, 2) if prev["value"] else None)
    return out


def _act_item(a: dict, value, **extra) -> dict:
    return dict(value=value, date=a["date"], start=a.get("start"), activity_id=a.get("id"),
                activity=dict(id=a.get("id"), name=a.get("name"), type=a.get("type"), date=a["date"]), **extra)


def efforts(runs: list[dict]) -> dict[str, list[dict]]:
    """Best-effort candidates per distance: Garmin's fastest split (exact) or, for a run without
    splits that covers the distance, its whole-run time scaled to the distance (estimated)."""
    out = {k: [] for k in EFFORTS}
    for a in runs:
        dist, secs, splits = a.get("dist_m") or 0, a.get("secs") or 0, a.get("splits") or {}
        has_splits = any(splits.get(k) for k in EFFORTS)
        for k, (d, _) in EFFORTS.items():
            if splits.get(k):
                out[k].append(_act_item(a, float(splits[k]), source="split"))
            elif not has_splits and d <= dist <= d * WHOLE_RUN_MAX and secs:
                out[k].append(_act_item(a, round(secs * d / dist, 1), source="whole", estimated=True,
                                        note="Estimated from the whole run (no split data)"))
    return out


# ----------------------------------- records ---------------------------------- #
def compute(acts: list[dict], days: dict[str, dict], today: date, period: str = "all", fam: str | None = None) -> dict:
    """Records for a period ("all" | "year" | "12m") and an optional activity family. Inputs are
    already cleaned. Health records ignore the family filter."""
    s = since(period, today)
    in_period = lambda iso: s is None or iso >= s.isoformat()
    pool = [a for a in acts if in_period(a["date"]) and (not fam or family(a.get("type")) == fam)]
    dpool = {k: v for k, v in days.items() if in_period(k)}
    groups: dict[str, list] = {"running": [], "activity": [], "weekly": [], "health": []}

    def add(g, r):
        if r:
            groups[g].append(r)

    # ---- running (outdoor runs only: treadmill distance is estimated)
    if not fam or fam == "run":
        runs = [a for a in pool if family(a.get("type")) == "run" and not is_indoor_run(a.get("type"))]
        for k, items in efforts(runs).items():
            add("running", _record(k, EFFORTS[k][1], "running", "s", items, lower=True))
        add("running", _record("longest_run", "Longest run", "running", "m", [_act_item(a, a["dist_m"]) for a in runs if a.get("dist_m")]))
        add("running", _record("run_gain", "Most elevation gain", "running", "m", [_act_item(a, a["gain_m"]) for a in runs if a.get("gain_m")]))
        add("running", _record("run_pace", "Fastest average pace (3+ mi)", "running", "s_per_m",
                               [_act_item(a, round(a["secs"] / a["dist_m"], 5)) for a in runs if (a.get("dist_m") or 0) > PACE_MIN_M], lower=True))

    # ---- any activity
    add("activity", _record("longest", "Longest duration", "activity", "s", [_act_item(a, a["secs"]) for a in pool if a.get("secs")]))
    add("activity", _record("load", "Highest training load", "activity", "load", [_act_item(a, a["load"]) for a in pool if a.get("load")]))
    add("activity", _record("hr", "Highest average heart rate", "activity", "bpm",
                            [_act_item(a, a["hr"]) for a in pool if a.get("hr") and (a.get("secs") or 0) >= HR_MIN_SECS]))

    # ---- weekly and monthly bests
    dist_ok = (lambda a: True) if fam else (lambda a: is_distance(a.get("type")) and family(a.get("type")) != "walk")
    workout = (lambda a: True) if fam else (lambda a: family(a.get("type")) != "walk")
    for unit_key, bucket, word in (("week", lambda d: monday(d), "week"), ("month", lambda d: d.replace(day=1), "month")):
        miles, count, im = {}, {}, {}
        for a in pool:
            b = bucket(date.fromisoformat(a["date"])).isoformat()
            if dist_ok(a) and is_distance(a.get("type")):
                miles[b] = miles.get(b, 0.0) + (a.get("dist_m") or 0)
            if workout(a):
                count[b] = count.get(b, 0) + 1
        for k, r in dpool.items():
            if r.get("im") is not None:
                b = bucket(date.fromisoformat(k)).isoformat()
                im[b] = im.get(b, 0.0) + r["im"]
        # a week or month that began before the history (or the period) did is partial: never a record or a previous best
        first = min([a["date"] for a in pool] + list(dpool), default=None)
        first_b = first and bucket(date.fromisoformat(first)).isoformat()
        whole = lambda b: b > first_b or first == first_b
        miles, count, im = ({b: v for b, v in x.items() if whole(b)} for x in (miles, count, im))
        span = lambda b: (date.fromisoformat(b) + timedelta(days=6)).isoformat() if word == "week" else None
        add("weekly", _record(f"{word}_distance", f"Most distance in a {word}", "weekly", "m",
                              [dict(value=v, date=b, end=span(b)) for b, v in miles.items() if v > 0]))
        add("weekly", _record(f"{word}_im", f"Most active minutes in a {word}", "weekly", "min",
                              [dict(value=v, date=b, end=span(b)) for b, v in im.items() if v > 0], note="Garmin intensity minutes, all activities"))
        add("weekly", _record(f"{word}_workouts", f"Most workouts in a {word}", "weekly", "count",
                              [dict(value=v, date=b, end=span(b)) for b, v in count.items()], note=None if fam else "Walks not counted"))
    add("weekly", _record("streak", "Longest streak of active days", "weekly", "days", streaks(pool),
                          note="A day with a workout, or a walk of 20+ minutes"))

    # ---- health
    for f, key, label, unit, lower in (("steps", "steps", "Most steps in a day", "steps", False), ("hrv", "hrv", "Best HRV", "ms", False),
                                       ("sleep_score", "sleep", "Best sleep score", "score", False), ("rhr", "rhr", "Lowest resting HR", "bpm", True),
                                       ("weight_kg", "weight", "Lowest weight", "kg", True)):
        add("health", _record(key, label, "health", unit, [dict(value=r[f], date=k) for k, r in dpool.items() if r.get(f) is not None], lower=lower))
    return groups


def streaks(acts: list[dict]) -> list[dict]:
    """Every run of consecutive active days as dict(value=days, date=first day, end=last day)."""
    active = sorted({a["date"] for a in acts if family(a.get("type")) != "walk" or (a.get("secs") or 0) >= STREAK_WALK_SECS})
    out, start, prev = [], None, None
    for k in active:
        d = date.fromisoformat(k)
        if prev and d - prev == timedelta(days=1):
            prev = d
            continue
        if start:
            out.append(dict(value=(prev - start).days + 1, date=start.isoformat(), end=prev.isoformat()))
        start = prev = d
    if start:
        out.append(dict(value=(prev - start).days + 1, date=start.isoformat(), end=prev.isoformat()))
    return out


def progress(acts: list[dict], today: date, period: str = "all") -> dict[str, list[dict]]:
    """Per distance: the best effort of each month, plus the all-time best as it stood that month."""
    s = since(period, today)
    runs = [a for a in acts if family(a.get("type")) == "run" and not is_indoor_run(a.get("type"))]
    out = {}
    for k, items in efforts(runs).items():
        months: dict[str, dict] = {}
        for it in items:
            m = it["date"][:7]
            if m not in months or it["value"] < months[m]["value"]:
                months[m] = it
        pts, so_far = [], None
        for m in sorted(months):
            it = months[m]
            so_far = it["value"] if so_far is None else min(so_far, it["value"])
            if s is None or m >= s.isoformat()[:7]:
                pts.append(dict(month=m, value=it["value"], best=so_far, activity_id=it["activity_id"], date=it["date"],
                                estimated=bool(it.get("estimated"))))
        out[k] = pts
    return out


def types(acts: list[dict]) -> list[dict]:
    c = Counter(family(a.get("type")) for a in acts)
    return [dict(family=f, n=n) for f, n in sorted(c.items(), key=lambda x: -x[1])]


def badges(acts: list[dict], days: dict[str, dict], today: date) -> dict[str, dict]:
    """Activity id -> the all-time records it holds now (all types). "New PR" if set in the last 30 days."""
    groups = compute(acts, days, today, "all", None)
    out: dict[str, dict] = {}
    for g in groups.values():
        for r in g:
            if r.get("activity_id") is None:
                continue
            b = out.setdefault(str(r["activity_id"]), dict(records=[], new=r["date"] >= (today - timedelta(days=NEW_DAYS)).isoformat()))
            b["records"].append(r["label"])
    return out


def signature(r: dict) -> str:
    return f"{r.get('activity_id') or r['date']}:{round(r['value'], 3)}"


def all_records(groups: dict) -> list[dict]:
    return [r for g in groups.values() for r in g]


def new_records(groups: dict, seen: dict[str, str] | None, today: date) -> list[dict]:
    """Records whose holder changed since they were last seen, set in the last 14 days. With nothing
    seen yet (the first sync) nothing is new; the caller then saves the current state as seen."""
    if not seen:
        return []
    cutoff = (today - timedelta(days=14)).isoformat()
    return [r for r in all_records(groups) if r["key"] in seen and seen[r["key"]] != signature(r) and r["date"] >= cutoff]


def seen_state(groups: dict) -> dict[str, str]:
    return {r["key"]: signature(r) for r in all_records(groups)}
