"""Weekly Review: a Monday-Sunday look back built from cached day rows, plus the coach write-up.

Pure functions over the same per-day rows the charts use (calendar month cache + per-day
summaries), so they can be tested without Garmin. Every value stays metric (m, kg, h); the page
converts for display. The coach gets `coach_payload()` only: derived numbers, activity types (never
names), goal figures (never goal notes) and, only when sharing is on, check-in rating counts. Check-in
notes are never read here at all.
"""
from __future__ import annotations

import statistics as st
from datetime import date, timedelta

import coach
import units
from calendar_data import DIST_TYPES, is_walk

PRIOR_WEEKS = 4  # load is compared with the average of this many weeks before
BASELINE_DAYS = 28  # HRV / resting HR baseline: the 4 weeks before the week
RECORD_DAYS = 365  # "best in 12 months"
RECORD_MIN_WEEKS = 8  # need this much earlier history before calling something a record
MAX_WEEKS_BACK = 52
LOADED_MIN_DAYS = 4  # a prior week counts towards the load average once this many days are loaded

# Tiles: key -> label, unit (metric), lower is better (None = neither direction is "good"), flat band
TILES = {
    "workouts": dict(label="Workouts", unit="sessions", lower=False, flat=0.5),
    "distance": dict(label="Distance", unit="m", lower=None, flat=0.03),
    "hours": dict(label="Training time", unit="h", lower=None, flat=0.03),
    "load": dict(label="Training load", unit="load", lower=None, flat=0.03),
    "hrv": dict(label="HRV", unit="ms", lower=False, flat=1.0),
    "rhr": dict(label="Resting HR", unit="bpm", lower=True, flat=1.0),
    "sleep_h": dict(label="Sleep", unit="h", lower=False, flat=0.1),
    "sleep_sd": dict(label="Sleep consistency", unit="h", lower=True, flat=0.1),
    "bb_high": dict(label="Body Battery high", unit="score", lower=False, flat=2.0),
    "weight": dict(label="Weight", unit="kg", lower=True, flat=0.1),
    "fat": dict(label="Body fat", unit="%", lower=True, flat=0.2),
    "ci_up": dict(label="Thumbs up", unit="ratings", lower=False, flat=0.5),
    "ci_down": dict(label="Thumbs down", unit="ratings", lower=True, flat=0.5),
}
RELATIVE = {"distance", "hours", "load"}  # their flat band is a fraction of last week's value


# ---------------------------------- weeks ------------------------------------ #
def monday(d: date) -> date:
    return d - timedelta(days=d.weekday())


def week_of(d: date) -> tuple[date, date]:
    m = monday(d)
    return m, m + timedelta(days=6)


def last_completed(today: date) -> date:
    """Monday of the latest week whose Sunday is already over. On a Sunday the current week is
    still running, so that's the week before."""
    return monday(today) - timedelta(days=7)


def parse_week(s: str | None, today: date) -> date:
    """Any ISO date -> the Monday of its week (default: the last completed week). Raises ValueError
    for future weeks or weeks more than MAX_WEEKS_BACK back."""
    if not s:
        return last_completed(today)
    m = monday(date.fromisoformat(s))
    if m > monday(today):
        raise ValueError("That week hasn't started yet.")
    if m < monday(today) - timedelta(weeks=MAX_WEEKS_BACK):
        raise ValueError(f"Reviews go back up to {MAX_WEEKS_BACK} weeks.")
    return m


def days(start: date, end: date) -> list[date]:
    return [start + timedelta(days=i) for i in range((end - start).days + 1)]


# --------------------------------- helpers ----------------------------------- #
def _mean(vals) -> float | None:
    vals = [v for v in vals if v is not None]
    return float(st.fmean(vals)) if vals else None


def _r(v, d=2):
    return None if v is None else round(v, d)


def _vals(rows: dict, start: date, end: date, key: str) -> list:
    return [rows[k].get(key) for k in (d.isoformat() for d in days(start, end)) if k in rows]


def _loaded(rows: dict, start: date, end: date) -> int:
    return sum(1 for d in days(start, end) if d.isoformat() in rows)


def _acts(rows: dict, start: date, end: date) -> list[dict]:
    return [dict(a, date=d.isoformat()) for d in days(start, end) for a in (rows.get(d.isoformat()) or {}).get("activities") or []]


def is_distance(t: str | None) -> bool:
    return any(x in (t or "") for x in DIST_TYPES)


def is_run(t: str | None) -> bool:
    return "run" in (t or "")


def is_ride(t: str | None) -> bool:
    return any(x in (t or "") for x in ("cycl", "bik", "ride"))


def tile(key: str, value, prev, lower=...) -> dict:
    """A stat tile: value, last week's value, the change and whether that change is good.
    direction is "flat" inside the metric's dead band; good is None when flat or when neither
    direction is better (training load)."""
    meta = TILES[key]
    lower = meta["lower"] if lower is ... else lower
    out = dict(key=key, label=meta["label"], unit=meta["unit"], value=value, prev=prev, delta=None, pct=None,
               direction=None, good=None, lower=lower)
    if value is None or prev is None:
        return out
    delta = value - prev
    band = meta["flat"] * abs(prev) if key in RELATIVE else meta["flat"]
    out.update(delta=round(delta, 3), pct=round(delta / abs(prev) * 100, 1) if prev else None)
    if abs(delta) < band or (key in RELATIVE and not prev and not value):
        out["direction"] = "flat"
    else:
        out["direction"] = "up" if delta > 0 else "down"
        out["good"] = None if lower is None else (delta < 0) == lower
    return out


# --------------------------------- sections ---------------------------------- #
def training_stats(rows: dict, start: date, end: date) -> dict:
    acts = _acts(rows, start, end)
    work = [a for a in acts if not is_walk(a.get("type"))]
    loads = _vals(rows, start, end, "load")
    return dict(workouts=len(work), walks=len(acts) - len(work),
                distance_m=float(sum(a.get("dist_m") or 0 for a in work if is_distance(a.get("type")))),
                hours=sum(a.get("secs") or 0 for a in work) / 3600,
                load=float(sum(v or 0 for v in loads)) if loads else None, loaded=len(loads))


def by_type(rows: dict, start: date, end: date) -> list[dict]:
    out: dict[str, dict] = {}
    for a in _acts(rows, start, end):
        t = a.get("type") or "other"
        x = out.setdefault(t, dict(type=t, sessions=0, hours=0.0, distance_m=0.0, walk=is_walk(t)))
        x["sessions"] += 1
        x["hours"] += (a.get("secs") or 0) / 3600
        x["distance_m"] += (a.get("dist_m") or 0) if is_distance(t) else 0
    return sorted(out.values(), key=lambda x: (x["walk"], -x["hours"]))


def training(rows: dict, mon: date, today: date) -> dict:
    sun = mon + timedelta(days=6)
    cur, prev = training_stats(rows, mon, sun), training_stats(rows, mon - timedelta(days=7), mon - timedelta(days=1))
    weeks = []
    for k in range(PRIOR_WEEKS, -1, -1):
        s = mon - timedelta(weeks=k)
        t = training_stats(rows, s, s + timedelta(days=6))
        weeks.append(dict(start=s.isoformat(), load=t["load"], hours=t["hours"], loaded=t["loaded"], current=k == 0))
    prior = [w["load"] for w in weeks[:-1] if w["loaded"] >= LOADED_MIN_DAYS and w["load"] is not None]
    avg4 = _mean(prior)
    ratio = round(cur["load"] / avg4, 2) if avg4 and cur["load"] is not None else None
    return dict(
        tiles=[tile("workouts", cur["workouts"], prev["workouts"] if prev["loaded"] else None),
               tile("distance", cur["distance_m"], prev["distance_m"] if prev["loaded"] else None),
               tile("hours", cur["hours"], prev["hours"] if prev["loaded"] else None),
               tile("load", cur["load"], prev["load"])],
        workouts=cur["workouts"], walks=cur["walks"], distance_m=cur["distance_m"], hours=cur["hours"], load=cur["load"],
        prev=prev, prior_avg_load=_r(avg4, 1), prior_weeks=len(prior), load_ratio=ratio, weeks=weeks,
        by_type=by_type(rows, mon, sun))


def recovery(rows: dict, mon: date, today: date) -> dict:
    sun, pm = mon + timedelta(days=6), mon - timedelta(days=7)
    b0, b1 = mon - timedelta(days=BASELINE_DAYS), mon - timedelta(days=1)
    week = lambda k, s=mon, e=sun: _mean(_vals(rows, s, e, k))
    sleep = [v for v in _vals(rows, mon, sun, "sleep_h") if v is not None]
    psleep = [v for v in _vals(rows, pm, pm + timedelta(days=6), "sleep_h") if v is not None]
    sd = lambda xs: round(st.pstdev(xs), 2) if len(xs) >= 3 else None
    base = dict(hrv=_r(week("hrv", b0, b1), 1), rhr=_r(week("rhr", b0, b1), 1))
    vals = {k: week(k) for k in ("hrv", "rhr", "sleep_h", "bb_high", "bb_low")}
    prev = {k: week(k, pm, pm + timedelta(days=6)) for k in ("hrv", "rhr", "sleep_h", "bb_high")}
    tiles = [tile("hrv", _r(vals["hrv"], 1), _r(prev["hrv"], 1)), tile("rhr", _r(vals["rhr"], 1), _r(prev["rhr"], 1)),
             tile("sleep_h", _r(vals["sleep_h"]), _r(prev["sleep_h"])), tile("sleep_sd", sd(sleep), sd(psleep)),
             tile("bb_high", _r(vals["bb_high"], 1), _r(prev["bb_high"], 1))]
    for t in tiles[:2]:  # HRV and resting HR are also compared with the 4-week baseline
        b = base[t["key"]]
        t.update(base=b, base_delta=None if b is None or t["value"] is None else round(t["value"] - b, 1))
    return dict(tiles=tiles, hrv=_r(vals["hrv"], 1), rhr=_r(vals["rhr"], 1), hrv_base=base["hrv"], rhr_base=base["rhr"],
                sleep_h=_r(vals["sleep_h"]), sleep_sd=sd(sleep), nights=len(sleep), nights_7h=sum(1 for v in sleep if v >= 7),
                bb_high=_r(vals["bb_high"], 1), bb_low=_r(vals["bb_low"], 1),
                daily=[dict(date=d.isoformat(), **{k: (rows.get(d.isoformat()) or {}).get(k) for k in ("hrv", "rhr", "sleep_h", "bb_high", "bb_low")})
                       for d in days(mon, sun)])


def body(rows: dict, mon: date, goal_views: list[dict]) -> dict:
    sun, pm = mon + timedelta(days=6), mon - timedelta(days=7)
    readings = [dict(date=d.isoformat(), kg=(rows.get(d.isoformat()) or {}).get("weight_kg"), fat=(rows.get(d.isoformat()) or {}).get("fat"))
                for d in days(pm, sun)]
    readings = [r for r in readings if r["kg"] is not None or r["fat"] is not None]
    avg = lambda k, s, e: _mean(r[k] for r in readings if s.isoformat() <= r["date"] <= e.isoformat())
    kg, pkg = avg("kg", mon, sun), avg("kg", pm, mon - timedelta(days=1))
    fat, pfat = avg("fat", mon, sun), avg("fat", pm, mon - timedelta(days=1))
    # Weight change is only "good" or "bad" relative to a weight goal; without one the arrow stays neutral
    wg = next((v for v in goal_views if v["goal"]["type"] == "weight" and v["goal"].get("target") is not None), None)
    lower = None
    if wg and pkg is not None:
        lower = wg["goal"]["target"] < (wg.get("start") if wg.get("start") is not None else pkg)
    return dict(tiles=[tile("weight", _r(kg), _r(pkg), lower=lower), tile("fat", _r(fat, 1), _r(pfat, 1))],
                weight_kg=_r(kg), weight_change_kg=None if kg is None or pkg is None else round(kg - pkg, 2),
                fat=_r(fat, 1), fat_change=None if fat is None or pfat is None else round(fat - pfat, 1) + 0.0,
                weighins=sum(1 for r in readings if r["date"] >= mon.isoformat() and r["kg"] is not None), readings=readings)


def checkin_counts(ci_rows: list[dict], ind: str) -> dict:
    vals = [r["ratings"][ind] for r in ci_rows if ind in (r.get("ratings") or {})]
    return dict(up=vals.count(1), neutral=vals.count(0), down=vals.count(-1), n=len(vals))


def checkin_section(ci_rows: list[dict], inds: list[dict], mon: date) -> dict:
    """Ratings only (the notes are never read): per enabled indicator this week vs last week."""
    sun, pm = (mon + timedelta(days=6)).isoformat(), (mon - timedelta(days=7)).isoformat()
    cur = [r for r in ci_rows if mon.isoformat() <= r["day"] <= sun and r.get("ratings")]
    prev = [r for r in ci_rows if pm <= r["day"] < mon.isoformat() and r.get("ratings")]
    out = []
    for i in inds:
        if not i.get("enabled", True):
            continue
        c, p = checkin_counts(cur, i["id"]), checkin_counts(prev, i["id"])
        trend = None
        if c["n"] and p["n"]:
            d = (c["up"] - c["down"]) / c["n"] - (p["up"] - p["down"]) / p["n"]
            trend = "up" if d >= 0.15 else "down" if d <= -0.15 else "flat"
        out.append(dict(id=i["id"], label=i["label"], **c, prev_up=p["up"], prev_down=p["down"], prev_n=p["n"], trend=trend))
    up, down = sum(x["up"] for x in out), sum(x["down"] for x in out)
    pup, pdown = sum(x["prev_up"] for x in out), sum(x["prev_down"] for x in out)
    return dict(days=len(cur), prev_days=len(prev), indicators=out, up=up, down=down,
                tiles=[tile("ci_up", up, pup if prev else None), tile("ci_down", down, pdown if prev else None)])


def goal_section(goal_views: list[dict], rows: dict, mon: date) -> list[dict]:
    """Each goal's figure for this week next to its overall status. Weekly goals are judged on the week;
    weight and body fat on whether the week's average moved towards the target."""
    sun, pm = mon + timedelta(days=6), mon - timedelta(days=7)
    run_km = lambda s, e: sum((a.get("dist_m") or 0) for a in _acts(rows, s, e) if is_run(a.get("type"))) / 1000 if _loaded(rows, s, e) else None
    out = []
    for v in goal_views:
        g, t = v["goal"], v["goal"]["type"]
        target, week, prev, kind = g.get("target"), None, None, "level"
        if t == "weekly_run_km":
            week, prev, kind = run_km(mon, sun), run_km(pm, mon - timedelta(days=1)), "total"
        elif t == "weekly_active_min":
            im = _vals(rows, mon, sun, "im")
            week, kind = (float(sum(x or 0 for x in im)) if any(x is not None for x in im) else None), "total"
            pim = _vals(rows, pm, mon - timedelta(days=1), "im")
            prev = float(sum(x or 0 for x in pim)) if any(x is not None for x in pim) else None
        elif t in ("sleep_h", "steps"):
            f = "sleep_h" if t == "sleep_h" else "steps"
            week, prev, kind = _mean(_vals(rows, mon, sun, f)), _mean(_vals(rows, pm, mon - timedelta(days=1), f)), "average"
        elif t in ("weight", "body_fat"):
            f = "weight_kg" if t == "weight" else "fat"
            week, prev, kind = _mean(_vals(rows, mon, sun, f)), _mean(_vals(rows, pm, mon - timedelta(days=1), f)), "trend"
        status = None
        if week is not None and target is not None:
            if kind in ("total", "average"):
                status = "met" if week >= target else "short"
            elif prev is not None:
                towards, change = target - prev, week - prev
                status = "flat" if abs(change) < (0.1 if t == "weight" else 0.2) else "towards" if towards * change > 0 else "away"
        out.append(dict(id=g["id"], type=t, label=v["label"], kind=kind, target=target, week=_r(week, 3), prev=_r(prev, 3),
                        week_status=status, status=v.get("status"), status_text=v.get("status_text"), progress_pct=v.get("progress_pct"),
                        deadline=g.get("deadline")))
    return out


def highlights(rows: dict, mon: date, today: date) -> dict:
    sun = mon + timedelta(days=6)
    work = [a for a in _acts(rows, mon, sun) if not is_walk(a.get("type"))]
    best = max(work, key=lambda a: ((a.get("load") or 0), (a.get("secs") or 0)), default=None)
    rec_start = mon - timedelta(days=RECORD_DAYS - 7)
    before = _acts(rows, rec_start, mon - timedelta(days=1))
    history_weeks = sum(1 for k in range(1, RECORD_DAYS // 7) if _loaded(rows, mon - timedelta(weeks=k), mon - timedelta(weeks=k) + timedelta(days=6)) >= LOADED_MIN_DAYS)
    records = []
    if history_weeks >= RECORD_MIN_WEEKS:
        for label, test in (("Longest run", is_run), ("Longest ride", is_ride)):
            this = max((a for a in work if test(a.get("type"))), key=lambda a: a.get("dist_m") or 0, default=None)
            prior = max((a.get("dist_m") or 0 for a in before if test(a.get("type"))), default=0)
            if this and (this.get("dist_m") or 0) > prior > 0:
                records.append(dict(kind="distance", label=label, value=this["dist_m"], prev_best=prior, date=this["date"], type=this.get("type")))
        weeks = [training_stats(rows, mon - timedelta(weeks=k), mon - timedelta(weeks=k) + timedelta(days=6)) for k in range(1, RECORD_DAYS // 7)]
        cur = training_stats(rows, mon, sun)
        for key, label, kind in (("load", "Biggest training week", "load"), ("hours", "Most training hours in a week", "hours")):
            prior = max((w[key] or 0 for w in weeks if w["loaded"] >= LOADED_MIN_DAYS), default=0)
            if (cur[key] or 0) > prior > 0:
                records.append(dict(kind=kind, label=label, value=cur[key], prev_best=prior, date=None, type=None))
    strip = []
    for d in days(mon, sun):
        r = rows.get(d.isoformat())
        strip.append(dict(date=d.isoformat(), loaded=r is not None, future=d > today, readiness=(r or {}).get("readiness"),
                          activities=[{k: a.get(k) for k in ("type", "secs", "dist_m", "load")} for a in (r or {}).get("activities") or []]))
    return dict(best=best and {k: best.get(k) for k in ("id", "name", "type", "date", "start", "dist_m", "secs", "hr", "load", "te")},
                records=records, history_weeks=history_weeks, days=strip)


def build(rows: dict, ci_rows: list[dict], inds: list[dict], goal_views: list[dict], mon: date, today: date) -> dict:
    sun = mon + timedelta(days=6)
    return dict(start=mon.isoformat(), end=sun.isoformat(), complete=sun < today, current=mon == monday(today),
                loaded_days=_loaded(rows, mon, min(sun, today)),
                prev_start=(mon - timedelta(days=7)).isoformat(), next_start=(mon + timedelta(days=7)).isoformat() if mon < monday(today) else None,
                training=training(rows, mon, today), recovery=recovery(rows, mon, today), body=body(rows, mon, goal_views),
                checkins=checkin_section(ci_rows, inds, mon), goals=goal_section(goal_views, rows, mon),
                highlights=highlights(rows, mon, today))


# ----------------------------------- coach ----------------------------------- #
def coach_payload(rv: dict, share_checkins: bool) -> dict:
    """Whitelisted, derived numbers only. Keys carry their metric unit (_kg, _km) so units.localize
    converts them; activity names, goal notes and check-in notes are never included."""
    km = lambda m: None if m is None else round(m / 1000, 2)
    t, r, b, h = rv["training"], rv["recovery"], rv["body"], rv["highlights"]
    goals_out = []
    for g in rv["goals"]:
        x = dict(type=g["type"], label=(g["label"] or "")[:120], week_status=g["week_status"], overall_status=g["status_text"],
                 progress_pct=g["progress_pct"], deadline=g["deadline"])
        if g["type"] == "weight":
            x.update(week_avg_kg=_r(g["week"]), prev_week_avg_kg=_r(g["prev"]), target_kg=g["target"])
        elif g["type"] == "weekly_run_km":
            x.update(week_km=_r(g["week"]), prev_week_km=_r(g["prev"]), target_km=g["target"])
        else:
            x.update(this_week=_r(g["week"]), prev_week=_r(g["prev"]), target=g["target"],
                     unit={"body_fat": "%", "weekly_active_min": "intensity min/week", "sleep_h": "h/night avg",
                           "steps": "steps/day avg", "fitness_age": "years"}.get(g["type"]))
        goals_out.append(x)
    best = h["best"]
    out = dict(
        week=dict(start=rv["start"], end=rv["end"], complete=rv["complete"], days_with_data=rv["loaded_days"]),
        training=dict(workouts=t["workouts"], prev_week_workouts=t["prev"]["workouts"], walks=t["walks"],
                      distance_km=km(t["distance_m"]), prev_week_distance_km=km(t["prev"]["distance_m"]),
                      hours=_r(t["hours"]), prev_week_hours=_r(t["prev"]["hours"]),
                      load=t["load"], prev_week_load=t["prev"]["load"], prior_4_week_avg_load=t["prior_avg_load"],
                      load_vs_4_week_avg=t["load_ratio"],
                      by_type=[dict(type=x["type"], sessions=x["sessions"], hours=_r(x["hours"]), distance_km=km(x["distance_m"]) or None)
                               for x in t["by_type"]]),
        recovery=dict(hrv_avg_ms=r["hrv"], hrv_baseline_ms=r["hrv_base"], resting_hr_avg_bpm=r["rhr"], resting_hr_baseline_bpm=r["rhr_base"],
                      sleep_avg_h=r["sleep_h"], sleep_night_to_night_sd_h=r["sleep_sd"], nights_7h_plus=r["nights_7h"], nights=r["nights"],
                      body_battery_high_avg=r["bb_high"], body_battery_low_avg=r["bb_low"],
                      prev_week={x["key"]: x["prev"] for x in r["tiles"]}),
        body=dict(weight_avg_kg=b["weight_kg"], weight_change_vs_prev_week_kg=b["weight_change_kg"], weighins=b["weighins"],
                  body_fat_avg_pct=b["fat"], body_fat_change_pts=b["fat_change"]),
        goals=goals_out,
        highlights=dict(
            best_workout=best and dict(type=best["type"], weekday=date.fromisoformat(best["date"]).strftime("%A"),
                                       distance_km=km(best["dist_m"]) if is_distance(best["type"]) else None,
                                       minutes=round((best["secs"] or 0) / 60), avg_hr=best["hr"], load=best["load"], training_effect=best["te"]),
            records_12_months=[dict(label=x["label"], **({"distance_km": km(x["value"]), "previous_best_km": km(x["prev_best"])} if x["kind"] == "distance"
                                                        else {"value": _r(x["value"], 1), "previous_best": _r(x["prev_best"], 1)}))
                               for x in h["records"]]))
    if share_checkins:
        c = rv["checkins"]
        out["checkins"] = dict(about="Self-reported daily ratings (thumbs up / neutral / thumbs down). Wellness context only, not medical data.",
                               days=c["days"], prev_week_days=c["prev_days"],
                               indicators=[{k: i[k] for k in ("label", "up", "neutral", "down", "prev_up", "prev_down")} for i in c["indicators"] if i["n"] or i["prev_n"]])
    return out


REVIEW_SYSTEM = (
    "You are an evidence-minded endurance and health coach embedded in a personal Garmin dashboard, writing the athlete's "
    "weekly review. You receive derived stats for one Monday-Sunday week compared with the week before, a 4-week load average and "
    "a 4-week HRV / resting HR baseline, plus their goals with this week's figures. All numbers were computed in code: use them as "
    "given, cite them, and never invent data that is missing. "
    "Answer with: headline (one line), went_well (2-4 items), watch (1-3 items; say so plainly if nothing stands out), "
    "next_week (3-5 concrete items: sessions, volume vs this week, intensity, sleep and recovery, and one step per goal that is "
    "behind) and reasons (2-4 short data points behind the plan). Keep training volume increases to ~10% a week, and suggest holding or "
    "reducing load when this week's load is well above the 4-week average or HRV is below baseline. If `week.complete` is false the "
    "week is still in progress; say so. If `checkins` is present it is self-reported wellness context: weave in at most one short "
    "observation, never diagnose. Fitness guidance, not medical advice; mention a professional only if something looks concerning."
)


def claude_review(payload: dict, system: str = units.DEFAULT, essential: bool = True) -> dict:
    """{source, data, text, model, usage} (schema-checked). Raises ai.AIError."""
    return coach.structured("review", REVIEW_SYSTEM, payload, "Write the weekly review.", system, essential)


# ------------------------------ rule-based review ---------------------------- #
def rule_review(rv: dict, system: str = units.DEFAULT) -> str:
    t, r, b, c, h = rv["training"], rv["recovery"], rv["body"], rv["checkins"], rv["highlights"]
    good, watch, plan = [], [], []
    dist = lambda m: units.fmt_dist(m, system)
    ratio, avg4 = t["load_ratio"], t["prior_avg_load"]
    pw = t["prev"]["workouts"]

    # ---- what went well
    if t["workouts"]:
        bit = f"{t['workouts']} workout{'s' if t['workouts'] != 1 else ''}, {t['hours']:.1f} h"
        if t["distance_m"]:
            bit += f", {dist(t['distance_m'])}"
        if t["prev"]["loaded"] and t["workouts"] >= pw:
            bit += f" (last week: {pw})"
        good.append(f"**Consistency:** {bit}.")
    if ratio is not None and 0.8 <= ratio <= 1.3:
        good.append(f"**Balanced load:** {t['load']:.0f} vs your 4-week average of {avg4:.0f} ({ratio:.2f}×), right in the productive range.")
    if r["hrv"] is not None and r["hrv_base"] and r["hrv"] >= r["hrv_base"]:
        good.append(f"**HRV:** averaged {r['hrv']:.0f} ms, at or above your {r['hrv_base']:.0f} ms baseline, so you absorbed the week well.")
    if r["rhr"] is not None and r["rhr_base"] and r["rhr"] <= r["rhr_base"]:
        good.append(f"**Resting HR:** {r['rhr']:.0f} bpm, at or below your {r['rhr_base']:.0f} bpm baseline.")
    if r["sleep_h"] is not None and r["sleep_h"] >= 7:
        good.append(f"**Sleep:** {r['sleep_h']:.1f} h a night on average, {r['nights_7h']} of {r['nights']} nights at 7 h or more.")
    for g in rv["goals"]:
        if g["week_status"] in ("met", "towards"):
            good.append(f"**{g['label']}:** {_goal_week_text(g, system)}.")
    for x in h["records"]:
        good.append(f"**Best in 12 months:** {x['label'].lower()}, {_record_value(x, system)}.")
    if c["days"] and c["up"] > c["down"]:
        good.append(f"**Check-ins:** {c['up']} thumbs up vs {c['down']} down across {c['days']} day{'s' if c['days'] != 1 else ''}.")

    # ---- what to watch
    if ratio is not None and ratio > 1.3:
        watch.append(f"**Load spike:** {t['load']:.0f} is {ratio:.2f}× your 4-week average ({avg4:.0f}). Jumps like this raise injury and illness risk.")
    elif ratio is not None and ratio < 0.7 and rv["complete"]:
        watch.append(f"**Light week:** load {t['load']:.0f} vs a {avg4:.0f} average ({ratio:.2f}×). Fine as a deload; if not planned, fitness will start to slip.")
    if r["hrv"] is not None and r["hrv_base"] and r["hrv"] < 0.95 * r["hrv_base"]:
        watch.append(f"**HRV below baseline:** {r['hrv']:.0f} ms vs {r['hrv_base']:.0f} ms. Recovery is lagging behind the training.")
    if r["rhr"] is not None and r["rhr_base"] and r["rhr"] >= r["rhr_base"] + 3:
        watch.append(f"**Resting HR up:** {r['rhr']:.0f} bpm vs {r['rhr_base']:.0f} bpm baseline; often stress, poor sleep or a bug coming on.")
    if r["sleep_h"] is not None and r["sleep_h"] < 7:
        watch.append(f"**Short sleep:** {r['sleep_h']:.1f} h a night; only {r['nights_7h']} of {r['nights']} nights reached 7 h.")
    if r["sleep_sd"] is not None and r["sleep_sd"] > 1:
        watch.append(f"**Irregular sleep:** night-to-night swing of ±{r['sleep_sd']:.1f} h. A steadier wake time helps HRV.")
    for g in rv["goals"]:
        if g["week_status"] in ("short", "away"):
            watch.append(f"**{g['label']}:** {_goal_week_text(g, system)}.")
    if c["days"] and c["down"] > c["up"]:
        watch.append(f"**Check-ins:** more thumbs down ({c['down']}) than up ({c['up']}) this week.")
    if not watch:
        watch.append("Nothing stands out. Recovery kept up with training this week.")

    # ---- next week
    if ratio is not None and ratio > 1.3:
        plan.append(f"**Volume:** hold load near your 4-week average (about {avg4:.0f}) instead of building on this week.")
    elif ratio is not None and ratio < 0.8 and avg4:
        plan.append(f"**Volume:** build back gradually, to about {min(avg4, (t['load'] or 0) * 1.1 or avg4):.0f} load.")
    elif t["load"]:
        plan.append(f"**Volume:** up to ~10% more than this week (about {t['load'] * 1.1:.0f} load), or repeat it if you felt flat.")
    else:
        plan.append("**Volume:** start with 2–3 easy sessions to rebuild a base.")
    hrv_low = r["hrv"] is not None and r["hrv_base"] and r["hrv"] < 0.95 * r["hrv_base"]
    plan.append("**Intensity:** keep the first 2 days easy, then add one quality session once HRV is back near baseline." if hrv_low
                else "**Intensity:** one or two quality sessions (intervals or tempo), the rest easy and conversational.")
    if r["sleep_h"] is not None and r["sleep_h"] < 7.5:
        plan.append(f"**Sleep:** aim for 30 minutes more a night than this week's {r['sleep_h']:.1f} h, with a fixed wake time.")
    for g in rv["goals"]:
        step = _goal_step(g, system)
        if step:
            plan.append(f"**{g['label']}:** {step}")
    plan.append("**Recovery:** at least one full rest day, plus mobility after hard sessions.")

    head = "" if rv["complete"] else "_This week is still in progress, so the numbers so far are partial._\n\n"
    return head + "\n".join(["### What went well", *[f"- {x}" for x in (good or ["You showed up and logged the week. That's the base everything builds on."])],
                             "", "### What to watch", *[f"- {x}" for x in watch],
                             "", "### Next week plan", *[f"- {x}" for x in plan],
                             "", "_Fitness guidance only, not medical advice._"])


def _goal_value(g: dict, v, system) -> str:
    t = g["type"]
    if t == "weekly_active_min":
        return f"{v:.0f} min"
    if t == "steps":
        return f"{v:,.0f} steps/day"
    if t == "sleep_h":
        return f"{v:.1f} h/night"
    if t == "body_fat":
        return f"{v:.1f}%"
    if t == "weight":
        return units.fmt_weight(v, system)
    if t == "weekly_run_km":
        return units.fmt_dist(v * 1000, system)
    return f"{v:g}"


def _goal_week_text(g: dict, system) -> str:
    if g["week"] is None or g["target"] is None:
        return g.get("status_text") or "no data this week"
    w, tgt = _goal_value(g, g["week"], system), _goal_value(g, g["target"], system)
    s = g["week_status"]
    if s == "met":
        return f"{w} this week, target {tgt} met"
    if s == "short":
        return f"{w} this week vs a {tgt} target"
    delta = (g["week"] - g["prev"]) if g["prev"] is not None else None
    moved = (units.fmt_weight_delta(delta, system) if g["type"] == "weight" else f"{delta:+.1f} pts") if delta is not None else ""
    return f"week average {w} ({moved} vs last week), " + ("moving towards" if s == "towards" else "moving away from" if s == "away" else "holding near") + f" the {tgt} target"


def _goal_step(g: dict, system) -> str | None:
    if g["week_status"] != "short" and g["week_status"] != "away":
        return None
    t, tgt = g["type"], g["target"]
    if t == "weekly_run_km" and g["week"] is not None:
        nxt = min(tgt, g["week"] * 1.1) if g["week"] > 0 else min(tgt, 5)
        return f"run about {units.fmt_dist(nxt * 1000, system)} next week (≤10% more) on the way to {units.fmt_dist(tgt * 1000, system)}."
    if t == "weekly_active_min":
        return "add one harder session; vigorous minutes count double."
    if t == "sleep_h":
        return f"move lights-out earlier to reach {tgt:.1f} h a night."
    if t == "steps":
        return "add a 10–15 minute walk after meals."
    if t == "weight":
        return "keep the pace to about 0.5–0.75% of body weight a week; weigh in on the same mornings to see the trend."
    if t == "body_fat":
        return "keep protein high and lift 2–3 times; judge it on the 4-week trend."
    return None


def _record_value(x: dict, system) -> str:
    if x["kind"] == "distance":
        return f"{units.fmt_dist(x['value'], system)} (previous best {units.fmt_dist(x['prev_best'], system)})"
    if x["kind"] == "hours":
        return f"{x['value']:.1f} h (previous best {x['prev_best']:.1f} h)"
    return f"{x['value']:.0f} (previous best {x['prev_best']:.0f})"
