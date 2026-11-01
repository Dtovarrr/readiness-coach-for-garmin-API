"""Body composition + fitness age.

Parses the Personal API weight range (/weight-service/weight/range - weights, muscle and bone
mass in grams), the fitness-age endpoint and Garmin's weight goal into what the Body tab
renders, plus the derived stats the goals and the coach use. The profile settings carry no
height, so height is recovered from Garmin's own BMI (BMI = kg / m^2) unless overridden locally.
(/weight-service/weight/all answers 400 for long ranges, so only `range` is used.)
"""
from __future__ import annotations

import math
import statistics as st
from datetime import date, timedelta

import httpx

from garmin_client import GarminError

BMI_BANDS = ((18.5, "Underweight"), (25.0, "Healthy weight"), (30.0, "Overweight"), (math.inf, "Obesity"))
FA_COMPONENTS = {  # key -> (label, unit)
    "bodyFat": ("Body fat", "%"),
    "bmi": ("BMI", ""),
    "rhr": ("Resting heart rate", "bpm"),
    "vigorousDaysAvg": ("Vigorous days", "days/week"),
    "vigorousMinutesAvg": ("Vigorous intensity minutes", "min/week"),
}


def _r(x, n=1):
    return None if x is None else round(x, n)


def _d(s: str) -> date:
    return date.fromisoformat(str(s)[:10])


def bmi_category(bmi):
    return next(label for hi, label in BMI_BANDS if bmi < hi) if bmi else None


def slope_per_week(points) -> float | None:
    """Least-squares slope of [(date|iso, value)] in units per 7 days; None if too little data."""
    pts = [(_d(d).toordinal(), v) for d, v in points if v is not None]
    if len(pts) < 3 or pts[-1][0] - pts[0][0] < 7:
        return None
    mx, my = st.mean(p[0] for p in pts), st.mean(p[1] for p in pts)
    den = sum((x - mx) ** 2 for x, _ in pts)
    return sum((x - mx) * (y - my) for x, y in pts) / den * 7 if den else None


def avg_window(points, end: date, days: int = 7):
    """Mean of values dated within the `days` ending at `end`; widens to +/-10 days if empty."""
    xs = [v for d, v in points if v is not None and end - timedelta(days=days - 1) <= _d(d) <= end]
    if not xs:
        xs = [v for d, v in points if v is not None and abs((_d(d) - end).days) <= 10]
    return st.mean(xs) if xs else None


# --------------------------------------------------------------------------- #
WEIGHT_BLOCK_DAYS = 30  # Garmin's weight service times out (504) on cold long ranges; 30-day ranges answer in ~0.1 s
WEIGHT_PATH = "/weight-service/weight/range"


def weight_blocks(start: date, end: date, days: int = WEIGHT_BLOCK_DAYS) -> list[tuple[date, date]]:
    """Fixed day-number-aligned blocks covering [start, end], so a block's cache key never changes."""
    out, b = [], start.toordinal() // days
    while date.fromordinal(b * days) <= end:
        out.append((date.fromordinal(b * days), date.fromordinal(b * days + days - 1)))
        b += 1
    return out


def fetch_weight(get, start: date, end: date, today: date, cache_get=None, cache_put=None) -> tuple[dict, list[dict]]:
    """Weight summaries for [start, end] as one {"dailyWeightSummaries": [...]}, fetched in short blocks
    (settled blocks - ending 2+ days ago - are cached for good when a cache is given), plus a list of
    the blocks that failed ({start, end, status, error}). A failed block leaves the rest usable."""
    by_day, failed = {}, []
    for b0, b1 in weight_blocks(start, end):
        hi = min(b1, today)
        settled = b1 <= today - timedelta(days=2)
        key = f"{b0}_{b1}"
        hit = cache_get("weight", key) if cache_get and settled else None
        if hit is not None:
            rows = hit.get("rows") or []
        else:
            try:
                rows = (get(f"{WEIGHT_PATH}/{b0}/{hi}") or {}).get("dailyWeightSummaries") or []
            except (GarminError, httpx.HTTPError) as ex:
                failed.append(dict(start=b0.isoformat(), end=hi.isoformat(), status=getattr(ex, "status", None),
                                   error=str(ex)[:200] or type(ex).__name__))
                continue
            if cache_put and settled:
                cache_put("weight", key, {"rows": rows})
        for r in rows:
            d = str(r.get("summaryDate") or "")[:10]
            if start.isoformat() <= d <= end.isoformat():
                by_day[d] = r
    return {"dailyWeightSummaries": [by_day[d] for d in sorted(by_day, reverse=True)]}, failed


def weight_error(failed: list[dict]) -> str | None:
    """One readable line for the failed blocks (None when nothing failed)."""
    if not failed:
        return None
    codes = sorted({str(x["status"]) for x in failed if x.get("status")})
    why = f"Garmin answered {', '.join(codes)}" if codes else failed[0]["error"][:120]
    return f"Weight for {failed[0]['start']}…{failed[-1]['end']} didn't load ({why})"


def weight_series(raw: dict | None) -> list[dict]:
    rows = []
    for day in (raw or {}).get("dailyWeightSummaries") or []:
        w = day.get("latestWeight") or {}
        if not w.get("weight"):
            continue
        g = lambda k: w.get(k) or None  # Garmin reports "not measured" as null (or 0)
        rows.append(dict(
            date=str(day.get("summaryDate") or w.get("calendarDate"))[:10], kg=round(w["weight"] / 1000, 2),
            bmi=_r(g("bmi"), 2), fat=_r(g("bodyFat")), water=_r(g("bodyWater")),
            muscle_kg=_r(g("muscleMass") / 1000) if g("muscleMass") else None,
            bone_kg=_r(g("boneMass") / 1000, 2) if g("boneMass") else None,
            n=day.get("numOfWeightEntries") or 1,
        ))
    return sorted(rows, key=lambda r: r["date"])


def implied_height_m(rows: list[dict]) -> float | None:
    hs = [math.sqrt(r["kg"] / r["bmi"]) for r in rows if r.get("bmi")]
    return round(st.median(hs), 3) if hs else None


def _ma(rows, key, days=7):
    out = []
    for i, r in enumerate(rows):
        lo = _d(r["date"]) - timedelta(days=days - 1)
        xs = [x[key] for x in rows[max(0, i - days * 3):i + 1] if x.get(key) is not None and _d(x["date"]) >= lo]
        out.append(_r(st.mean(xs), 2) if xs else None)
    return out


def garmin_weight_goal(raw: dict | None) -> dict | None:
    g = next((x for x in (raw or {}).get("userGoals") or []
              if x.get("userGoalType") == "WEIGHT_GRAMS" and x.get("goalValue")), None)
    return dict(kg=round(g["goalValue"] / 1000, 2), start=g.get("startDate"), end=g.get("endDate")) if g else None


def fitness_age_view(fa: dict | None) -> dict | None:
    if not fa or fa.get("fitnessAge") is None:
        return None
    comps = []
    for key, c in (fa.get("components") or {}).items():
        if not isinstance(c, dict) or c.get("value") is None:
            continue
        label, unit = FA_COMPONENTS.get(key, (key, ""))
        # Garmin only attaches a target / potential age to components that are holding the score back.
        hurting = c.get("targetValue") is not None or c.get("potentialAge") is not None
        comps.append(dict(key=key, label=label, unit=unit, value=_r(c["value"]), target=_r(c.get("targetValue")),
                          improvement=_r(c.get("improvementValue")), potential_age=_r(c.get("potentialAge")),
                          priority=c.get("priority"), stale=bool(c.get("stale")),
                          last_measured=c.get("lastMeasurementDate"), status="hurting" if hurting else "helping"))
    comps.sort(key=lambda c: (c["status"] != "hurting", c["priority"] or 99))
    return dict(fitness_age=_r(fa["fitnessAge"]), age=fa.get("chronologicalAge"),
                achievable=_r(fa.get("achievableFitnessAge")), previous=_r(fa.get("previousFitnessAge")),
                updated=str(fa.get("lastUpdated") or "")[:10] or None, components=comps)


def build(rows: list[dict], fa_now: dict | None, fa_history: list[tuple[str, float]], goal: dict | None,
          height_override_m: float | None) -> dict:
    """Everything the Body tab needs, in metric units (the page converts for display)."""
    implied = implied_height_m(rows)
    height = height_override_m or implied
    for r, kg_ma, fat_ma in zip(rows, _ma(rows, "kg"), _ma(rows, "fat")):
        r["kg_ma"], r["fat_ma"] = kg_ma, fat_ma
        r["bmi_calc"] = _r(kg_ma / height ** 2, 2) if height and kg_ma else None
    out = dict(readings=len(rows), first_date=rows[0]["date"] if rows else None,
               height=dict(m=height, implied_m=implied, source="override" if height_override_m else "garmin_bmi" if implied else None),
               garmin_goal=goal, fitness_age=fitness_age_view(fa_now),
               fitness_age_history=[dict(date=d, value=_r(v)) for d, v in fa_history], series=rows, summary=None)
    if not rows:
        return out
    last = rows[-1]
    end = _d(last["date"])
    kg = [(r["date"], r["kg"]) for r in rows]
    fat = [(r["date"], r["fat"]) for r in rows]
    avg_kg, avg_fat = avg_window(kg, end), avg_window(fat, end)
    recent = [p for p in kg if _d(p[0]) >= end - timedelta(days=28)]
    rate = slope_per_week(recent)
    change = {}
    for n in (30, 90):
        then = avg_window(kg, end - timedelta(days=n))
        change[f"d{n}"] = _r(avg_kg - then, 2) if then is not None and _d(rows[0]["date"]) <= end - timedelta(days=n - 10) else None
    bmi = avg_kg / height ** 2 if height else None
    muscle = next((r["muscle_kg"] for r in reversed(rows) if r.get("muscle_kg")), None)
    out["summary"] = dict(
        latest=dict(date=last["date"], kg=last["kg"], fat=last["fat"]),
        days_since_reading=(date.today() - end).days,
        avg7=dict(kg=_r(avg_kg, 2), fat=_r(avg_fat)),
        change_kg=change,
        rate=dict(kg_per_week=_r(rate, 2), pct_per_week=_r(rate / avg_kg * 100, 2) if rate is not None else None,
                  window_days=28, readings=len(recent)),
        bmi=dict(value=_r(bmi), category=bmi_category(bmi)),
        fat_pts_per_week=_r(slope_per_week([p for p in fat if _d(p[0]) >= end - timedelta(days=42)]), 2),
        muscle_kg=muscle,
    )
    return out
