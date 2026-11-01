"""Local goals: storage, progress tracking, safety guardrails and a rule-based plan.

Garmin's goal API is read-only for us, so goals live in the local SQLite store (.data/coach.db,
git-ignored, kept apart from the disposable .cache/). All values are stored in metric units
(kg, km); the page converts for display.
"""
from __future__ import annotations

import math
import time
import uuid
from datetime import date, timedelta

import store
import units
from body import avg_window, slope_per_week

TYPES = {  # type -> label, unit (metric), decimals
    "weight": dict(label="Target weight", unit="kg", decimals=1),
    "body_fat": dict(label="Body fat", unit="%", decimals=1),
    "lean_mass": dict(label="Lean mass", unit="kg", decimals=1),
    "fitness_age": dict(label="Fitness age", unit="years", decimals=1),
    "weekly_run_km": dict(label="Weekly running distance", unit="km/week", decimals=1),
    "weekly_active_min": dict(label="Weekly intensity minutes", unit="min/week", decimals=0),
    "sleep_h": dict(label="Average sleep", unit="h/night", decimals=1),
    "steps": dict(label="Average daily steps", unit="steps/day", decimals=0),
    "custom": dict(label="Custom goal", unit="", decimals=0),
}


# ------------------------------- storage ------------------------------------ #
# Goals and settings live in the local SQLite store (store.py); this module keeps its API.
def list_goals() -> list[dict]:
    return store.goals()


def create(fields: dict) -> dict:
    now = time.strftime("%Y-%m-%dT%H:%M:%S")
    g = dict(fields, id=uuid.uuid4().hex[:12], start_date=date.today().isoformat(), created=now, updated=now)
    store.goal_insert(g)
    return g


def update(goal_id: str, fields: dict) -> dict | None:
    g = next((x for x in store.goals() if x["id"] == goal_id), None)
    if g is None:
        return None
    g.update(fields, updated=time.strftime("%Y-%m-%dT%H:%M:%S"))
    return g if store.goal_replace(g) else None


def delete(goal_id: str) -> bool:
    return store.goal_delete(goal_id)


def load_settings() -> dict:
    return store.settings()


def save_settings(fields: dict) -> dict:
    return store.settings_put(fields)  # None clears a key


# ------------------------------- metrics ------------------------------------ #
def daily_metric(points, avg_days=7, rate_days=28) -> dict | None:
    """points: [(iso_date, value)] -> current (recent average) + trend per week."""
    pts = sorted((d, v) for d, v in points if v is not None)
    if not pts:
        return None
    end = date.fromisoformat(pts[-1][0][:10])
    recent = [p for p in pts if date.fromisoformat(p[0][:10]) >= end - timedelta(days=rate_days)]
    return dict(current=avg_window(pts, end, avg_days), rate=slope_per_week(recent), as_of=end.isoformat(),
                n=len(recent), basis=f"{avg_days}-day average, trend over {rate_days} days")


def weekly_metric(daily: dict[str, float], today: date, weeks=8) -> dict:
    """Rolling 7-day totals ending today (missing days count as 0): current = mean of the
    last 4 weeks, trend = slope across `weeks` weekly totals."""
    basis = f"average of the last 4 rolling weeks, trend over {weeks} weeks"
    totals = []
    for k in range(weeks):
        end = today - timedelta(days=7 * k)
        tot = sum(daily.get((end - timedelta(days=i)).isoformat(), 0) for i in range(7))
        totals.append((end.isoformat(), tot))
    totals.reverse()
    return dict(current=sum(t for _, t in totals[-4:]) / 4, rate=slope_per_week(totals), as_of=today.isoformat(),
                n=weeks, weekly=[dict(week_ending=d, total=round(t, 1)) for d, t in totals],
                basis=basis)


def body_metrics(body: dict) -> dict:
    rows = body.get("series") or []
    out = {}
    if rows:
        out["weight"] = daily_metric([(r["date"], r["kg"]) for r in rows])
        out["body_fat"] = daily_metric([(r["date"], r["fat"]) for r in rows], rate_days=42)
        if out["body_fat"]:
            out["body_fat"]["source"] = "Garmin scale (7-day average)"
    fa = body.get("fitness_age")
    hist = [(h["date"], h["value"]) for h in body.get("fitness_age_history") or []]
    if fa:
        hist.append((date.today().isoformat(), fa["fitness_age"]))
        m = daily_metric(hist, avg_days=1, rate_days=90)
        m.update(current=fa["fitness_age"], basis="latest Garmin fitness age, trend over 90 days")
        out["fitness_age"] = m
    return {k: v for k, v in out.items() if v}


# --------------------------- progress + status ------------------------------ #
HIGHER_IS_BETTER = {"weekly_run_km", "weekly_active_min", "sleep_h", "steps"}
STATUS_TEXT = {"achieved": "Achieved", "on_track": "On track", "behind": "Behind", "at_risk": "At risk",
               "no_data": "No data yet", "unknown": "Not enough history", "in_progress": "In progress"}


def _guardrails(g: dict, cur: float | None, ctx: dict, today: date) -> tuple[list[dict], str | None]:
    """Warnings only - never block. Returns (warnings, suggested_deadline)."""
    warns, suggest = [], None
    t, target, deadline = g["type"], g.get("target"), g.get("deadline")
    system = ctx.get("units")
    W = lambda level, text: warns.append(dict(level=level, text=text))
    if not deadline or target is None:
        return warns, None
    dl = date.fromisoformat(str(deadline)[:10])
    weeks = (dl - today).days / 7
    if weeks <= 0:
        W("warn", "The deadline is today or already past.")
    if cur is None:
        return warns, None
    change = target - cur
    after = lambda w: (today + timedelta(days=math.ceil(w * 7))).isoformat()

    if t == "weight":
        h = ctx.get("height_m")
        if h and target / h ** 2 < 18.5:
            W("warn", f"That target is a BMI of {target / h ** 2:.1f}, below 18.5 (underweight). "
                      f"The lowest weight in the healthy BMI range at your height is about {units.fmt_weight(18.5 * h ** 2, system)}.")
        if change and weeks > 0:
            pct = abs(change) / weeks / cur * 100
            if change < 0:
                if pct > 1:
                    W("warn", f"Losing {pct:.1f}% of body weight per week is faster than the ~0.5–1%/week generally considered safe; it risks muscle loss and rebound.")
                elif pct > 0.5:
                    W("caution", f"{pct:.2f}%/week is at the aggressive end of the 0.5–1%/week range - doable short-term, harder to sustain.")
                if pct > 0.5:
                    suggest = after(abs(change) / (0.005 * cur))
            elif pct > 0.5:
                W("caution", f"Gaining {pct:.1f}%/week is fast; beyond ~0.25–0.5%/week most extra gain tends to be fat.")
                suggest = after(change / (0.0035 * cur))
    elif t == "body_fat":
        if target < 8:
            W("warn", f"{target:g}% is near the essential-fat range (about 2–5% for men, 10–13% for women) and isn't a healthy long-term target for most people.")
        if change and weeks > 0 and abs(change) / weeks > 0.5:
            W("caution", f"Changing body fat by {abs(change) / weeks:.1f} points/week is unrealistic for most people; ~0.25–0.5 points/week is typical.")
            suggest = after(abs(change) / 0.3)
    elif t == "lean_mass":
        if change > 0 and weeks > 0 and change / weeks > 0.25:
            W("caution", f"Gaining {units.fmt_weight(change / weeks, system, 2)} of lean mass a week is faster than most people manage; "
                         f"about {units.fmt_weight(0.1, system, 2)}–{units.fmt_weight(0.25, system, 2)}/week tends to be realistic, less with experience.")
            suggest = after(change / 0.2)
    elif t == "fitness_age":
        ach = ctx.get("achievable_age")
        if ach and target < ach:
            W("caution", f"Garmin estimates your achievable fitness age at {ach:g} with its current inputs, so {target:g} may not be reachable.")
    elif t in ("weekly_run_km", "weekly_active_min", "steps") and change > 0 and weeks > 0:
        if cur > 0:
            need = math.log(target / cur) / math.log(1.10)  # weeks at the 10%/week ramp guideline
            if need > weeks:
                W("caution", f"Getting from {units.fmt_goal(t, cur, system)} to {units.fmt_goal(t, target, system)} by then needs more than "
                             "~10%/week of increase, which raises injury and burnout risk.")
                suggest = after(need)
        else:
            W("info", "You're starting from zero, so build up gradually; aim for about 10% more each week once you have a base.")
        if t == "weekly_run_km" and target > 100:
            W("info", f"{units.fmt_goal(t, 100, system)}+ is elite-level volume; make sure recovery, sleep and fuelling keep up.")
        if t == "weekly_active_min" and target > 600:
            W("info", "That's more than 4× the 150 min/week guideline; more isn't always better without recovery.")
        if t == "steps" and target > 25000:
            W("info", "25,000+ steps/day is very high; check that it fits around training and recovery.")
    elif t == "sleep_h" and not 7 <= target <= 9:
        W("info", "Most adults do best with 7–9 hours of sleep.")
    return warns, suggest


def evaluate(g: dict, metric: dict | None, ctx: dict, today: date | None = None) -> dict:
    today = today or date.today()
    meta = TYPES.get(g["type"], TYPES["custom"])
    out = dict(goal=g, label=g.get("title") or meta["label"], unit=meta["unit"], decimals=meta["decimals"],
               start=g.get("start_value"), target=g.get("target"), current=None, progress_pct=None, weeks_left=None,
               required_per_week=None, actual_per_week=None, projected=None, as_of=None, basis=None)
    dl = date.fromisoformat(str(g["deadline"])[:10]) if g.get("deadline") else None
    if dl:
        out["weeks_left"] = round(max(0.0, (dl - today).days / 7), 1)
    if g["type"] == "custom":
        p = g.get("manual_progress") or 0
        out["progress_pct"] = p
        out["status"] = "achieved" if p >= 100 else "at_risk" if dl and dl < today else "in_progress"
        out["status_text"] = STATUS_TEXT[out["status"]]
        out["warnings"], out["suggested_deadline"] = ([dict(level="warn", text="The deadline has passed.")] if dl and dl < today and p < 100 else []), None
        return out

    cur = metric.get("current") if metric else None
    out.update(current=cur, actual_per_week=metric.get("rate") if metric else None,
               as_of=metric.get("as_of") if metric else None, basis=metric.get("basis") if metric else None,
               source=metric.get("source") if metric else None)
    out["warnings"], out["suggested_deadline"] = _guardrails(g, cur, ctx, today)
    start, target = g.get("start_value"), g.get("target")
    if start is None:
        start = out["start"] = cur
    if cur is None or target is None:
        out["status"] = "no_data"
    else:
        if g["type"] in HIGHER_IS_BETTER:  # "average at least X": already being above it counts
            direction = 1
        elif g["type"] == "fitness_age":
            direction = -1
        else:  # weight / body fat can go either way
            ref = start if start is not None else cur
            direction = (target > ref) - (target < ref) or -1
        if start is not None and target != start:
            out["progress_pct"] = round(max(0.0, min(100.0, (cur - start) / (target - start) * 100)), 1)
        achieved = cur >= target if direction > 0 else cur <= target
        wl = out["weeks_left"]
        if wl:
            out["required_per_week"] = (target - cur) / wl
        rate = out["actual_per_week"]
        if rate is not None and wl:
            out["projected"] = cur + rate * wl
        if achieved:
            out["status"] = "achieved"
            out["progress_pct"] = 100.0
        elif not wl:
            out["status"] = "at_risk"
        elif rate is None:
            out["status"] = "unknown"
        else:
            ratio = rate / out["required_per_week"] if out["required_per_week"] else 0
            out["status"] = "on_track" if ratio >= 1 else "behind" if ratio >= 0.5 else "at_risk"
    out["status_text"] = STATUS_TEXT[out["status"]]
    for k in ("current", "start", "required_per_week", "actual_per_week", "projected"):
        if out[k] is not None:
            out[k] = round(out[k], 3)
    return out


# ------------------------------ rule-based plan ----------------------------- #
def rule_goal_plan(goal_views: list[dict], body: dict | None, assessment: dict | None, system: str = units.DEFAULT) -> str:
    if not goal_views:
        return "No goals yet. Add one on the left and the coach will build a plan around it."
    zone = (assessment or {}).get("zone")
    ratio = ((assessment or {}).get("strain") or {}).get("ratio")
    lines = ["### Where you stand"]
    for v in goal_views:
        g, t = v["goal"], v["goal"]["type"]
        if t == "custom":
            lines.append(f"- **{v['label']}**: {v['progress_pct']:.0f}% done (self-reported), {v['status_text'].lower()}.")
            continue
        bits = [f"now {units.fmt_goal(t, v['current'], system)}", f"target {units.fmt_goal(t, v['target'], system)}"]
        if g.get("deadline"):
            bits.append(f"by {g['deadline']}")
        if v["required_per_week"] is not None and v["actual_per_week"] is not None:
            bits.append(f"needs {units.fmt_goal_rate(t, v['required_per_week'], system)} vs recent "
                        f"{units.fmt_goal_rate(t, v['actual_per_week'], system)}")
        lines.append(f"- **{v['label']}** - {v['status_text']}: " + ", ".join(bits) + ".")
    lines += ["", "### Next steps"]
    types = {v["goal"]["type"] for v in goal_views}
    if "weight" in types:
        w = next(v for v in goal_views if v["goal"]["type"] == "weight")
        if w["current"]:
            lines.append(f"- **Weight pacing:** aim for about {units.fmt_weight(0.005 * w['current'], system, 2)} to "
                         f"{units.fmt_weight(0.0075 * w['current'], system, 2)} a week "
                         "(0.5–0.75% of body weight). A moderate daily deficit plus 2–3 strength sessions per week helps keep muscle.")
    if "body_fat" in types:
        lines.append("- **Body fat:** keep protein high and lift 2–3×/week; expect roughly 0.25–0.5 points/week at best, so judge it by the 4-week trend.")
    if "lean_mass" in types:
        lines.append("- **Lean mass:** progressive strength training 3×/week (10–20 hard sets per muscle group), protein around 0.7–1 g per lb "
                     "of goal body weight, and a small surplus or maintenance; check it with your next DEXA scan, not the scale.")
    if "fitness_age" in types and body and body.get("fitness_age"):
        hurt = [c for c in body["fitness_age"]["components"] if c["status"] == "hurting"]
        if hurt:
            c = hurt[0]
            lines.append(f"- **Fitness age:** your biggest lever is {c['label'].lower()} ({c['value']:g}{c['unit']} → "
                         f"{c['target']:g}{c['unit']} would take it to about {c['potential_age']:g}).")
        else:
            lines.append("- **Fitness age:** keep vigorous minutes up (75+/week) and resting HR low through consistent aerobic work.")
    if "weekly_run_km" in types:
        lines.append("- **Running volume:** add at most ~10%/week, keep ~80% of runs easy, and take a lighter week every 3–4 weeks.")
    if "weekly_active_min" in types:
        lines.append("- **Intensity minutes:** vigorous minutes count double; one or two harder sessions per week move this fastest.")
    if "sleep_h" in types:
        lines.append("- **Sleep:** fix your wake time, count back 8.5 h for lights-out, and keep caffeine before ~2 pm.")
    if "steps" in types:
        lines.append("- **Steps:** add ~1,000/day per week; a 10–15 min walk after meals is the easiest win.")
    lines.append({"prime": "- **Today:** readiness is high, so a quality session fits.",
                  "good": "- **Today:** moderate training; keep it aerobic.",
                  "caution": "- **Today:** easy day; protect recovery so you can train well later this week.",
                  "recover": "- **Today:** recovery day; progress comes from absorbing training, not adding more."}.get(zone, "- **Today:** sync your watch for a readiness-based recommendation."))
    if ratio and ratio > 1.3:
        lines.append(f"- **Load:** your 7:28-day load ratio is {ratio}, so hold volume steady this week before adding more.")
    lines += ["", "_Fitness guidance only, not medical advice._"]
    return "\n".join(lines)


# ------------------------------- suggestions -------------------------------- #
def clean_suggestions(raw: list, today: date, system: str | None = None) -> list[dict]:
    """Keep only well-formed, future-dated suggestions, one per metric (or per custom title).
    With `system`, targets arrive in that display unit (Claude's) and are stored back as metric."""
    out, seen = [], set()
    for s in raw or []:
        t = s.get("type") if isinstance(s, dict) else None
        if t not in TYPES:
            continue
        try:
            dl = date.fromisoformat(str(s.get("deadline"))[:10])
        except ValueError:
            continue
        target = s.get("target")
        if t == "custom":
            target = None
        elif isinstance(target, bool) or not isinstance(target, (int, float)) or not math.isfinite(target) or target <= 0:
            continue
        elif system:
            target = units.goal_from_display(t, target, system)
        title = str(s.get("title") or "").strip()[:120]
        key = (t, title.lower()) if t == "custom" else t
        if dl <= today or (t == "custom" and not title) or key in seen:
            continue
        seen.add(key)
        out.append(dict(type=t, title=title, target=None if target is None else round(float(target), 2),
                        deadline=dl.isoformat(), rationale=str(s.get("rationale") or "")[:600]))
    return out[:5]


def rule_suggestions(metrics: dict, body: dict | None, existing: set[str], today: date, system: str = units.DEFAULT) -> list[dict]:
    """Data-driven goal ideas without an API key, using the same safe paces as the guardrails."""
    out = []
    dl = lambda weeks: (today + timedelta(weeks=weeks)).isoformat()
    cur = lambda t: (metrics.get(t) or {}).get("current")
    s = (body or {}).get("summary") or {}
    fa = (body or {}).get("fitness_age") or {}
    w, bmi = cur("weight"), (s.get("bmi") or {}).get("value")
    if "weight" not in existing and w and bmi and bmi >= 25:
        # just under 0.5%/week, so the suggestion doesn't trip its own pace guardrail after rounding
        out.append(dict(type="weight", title="Lose weight at a sustainable pace", target=round(w * (1 - 0.0045 * 12), 1), deadline=dl(12),
                        rationale=f"Your BMI is {bmi:.1f}. Losing a bit under 0.5% of body weight a week "
                                  f"(about {units.fmt_weight(w * 0.0045, system, 2)}) "
                                  "for 12 weeks is a pace that protects muscle and tends to stick."))
    fat = cur("body_fat")
    bf = next((c for c in fa.get("components", []) if c["key"] == "bodyFat" and c["status"] == "hurting"), None)
    if "body_fat" not in existing and fat and bf:
        out.append(dict(type="body_fat", title="Bring body fat down", target=round(max(bf["target"] or 0, fat - 3), 1), deadline=dl(12),
                        rationale=f"Body fat ({fat:.1f}%) is what's holding your fitness age back (Garmin's target is {bf['target']}%). "
                                  "About 0.25 points a week is realistic."))
    if "fitness_age" not in existing and fa.get("achievable") and fa.get("fitness_age") and fa["achievable"] < fa["fitness_age"] - 0.5:
        out.append(dict(type="fitness_age", title="Reach Garmin's achievable fitness age", target=fa["achievable"], deadline=dl(26),
                        rationale=f"Garmin estimates {fa['achievable']} is reachable from your current {fa['fitness_age']}, mainly by "
                                  "improving the components marked as holding you back."))
    run = cur("weekly_run_km")
    if "weekly_run_km" not in existing and run and run >= 3:
        out.append(dict(type="weekly_run_km", title="Build running volume", target=round(run * 1.1 ** 8), deadline=dl(8),
                        rationale=f"You average {units.fmt_goal('weekly_run_km', run, system)}. Adding about 10% a week for 8 weeks "
                                  "builds fitness with low injury risk."))
    im = cur("weekly_active_min")
    if "weekly_active_min" not in existing and im is not None and im < 150:
        weeks = max(4, math.ceil(math.log(150 / max(im, 30)) / math.log(1.1)))
        out.append(dict(type="weekly_active_min", title="Hit 150 intensity minutes a week", target=150, deadline=dl(weeks),
                        rationale=f"You average {im:.0f} intensity minutes a week; 150 is the common health guideline."))
    sl = cur("sleep_h")
    if "sleep_h" not in existing and sl and sl < 7.5:
        out.append(dict(type="sleep_h", title="Average 7.5 hours of sleep", target=7.5, deadline=dl(6),
                        rationale=f"You're averaging {sl:.1f} h a night; 7.5+ h supports recovery and HRV."))
    st_ = cur("steps")
    if "steps" not in existing and st_ and st_ < 10000:
        out.append(dict(type="steps", title="Walk more each day", target=min(10000, round(st_ * 1.1 ** 6, -2)), deadline=dl(6),
                        rationale=f"You average {st_:,.0f} steps a day; adding about 10% a week is easy to sustain."))
    return out[:4]
