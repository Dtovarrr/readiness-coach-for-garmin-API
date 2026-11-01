"""Calendar: one month of Garmin data at a time, plus a day's detail.

A month is fetched with the same endpoints as the dashboard over [1st - 28 days, last day] (the
lead-in lets readiness use its 21-day HRV baseline and 28-day load), then cached in
.cache/calendar/YYYY-MM.json. Months that ended more than SETTLE_DAYS ago are reused for good; the
current month is refetched after CACHE_TTL. A whole year is never loaded at once.
"""
from __future__ import annotations

import re
import statistics as st
import time
from calendar import monthrange
from datetime import date, timedelta

import body
import coach
from activity import history_row
from garmin_client import GarminClient, GarminError, normalize

LEAD_IN_DAYS = 28
SETTLE_DAYS = 3
CACHE_TTL = 600
VERSION = 2  # bump when day rows gain fields; cached months are fetched again once
DIST_TYPES = ("run", "walk", "hik", "cycl", "bik", "ride", "swim")


def is_walk(type_key: str | None) -> bool:
    return "walk" in (type_key or "")


def _activities(c: GarminClient, start: date, end: date) -> list[dict]:
    out, offset = [], 0
    while True:
        page = c.get("/activitylist-service/activities/search/activities",
                     {"startDate": start.isoformat(), "endDate": end.isoformat(), "start": offset, "limit": 100}) or []
        out += page
        if len(page) < 100:
            return out
        offset += 100


def fetch_month(c: GarminClient, year: int, month: int, today: date, cache_get=None, cache_put=None) -> dict:
    first, last = date(year, month, 1), date(year, month, monthrange(year, month)[1])
    start, end = first - timedelta(days=LEAD_IN_DAYS), min(last, today)
    errors: dict[str, str] = {}

    def safe(name, fn):
        try:
            return fn()
        except GarminError as ex:
            errors[name] = str(ex)[:200]
            return None

    chunk = lambda path: (lambda a, b: c.get(path, {"startDate": a, "endDate": b}))
    raw_acts = safe("activities", lambda: _activities(c, start, end)) or []
    raw = {
        "hrv": safe("hrv", lambda: c.get(f"/hrv-service/hrv/daily/{start}/{end}")),
        "sleep": safe("sleep", lambda: c.chunked(chunk("/sleep-service/sleep/dailySleepSummariesByDate"), start, end)),
        "bb": safe("bodyBattery", lambda: c.chunked(chunk("/wellness-service/wellness/bodyBattery/reports/daily"), start, end)),
        "steps": safe("steps", lambda: c.chunked(lambda a, b: c.get(f"/usersummary-service/stats/steps/daily/{a}/{b}"), start, end)),
        "stress": safe("stress", lambda: c.chunked(lambda a, b: c.get(f"/usersummary-service/stats/stress/daily/{a}/{b}"), start, end)),
        "acts": raw_acts, "hrv_today": None, "rhr": [],
    }
    hist = normalize(raw, start, end)["history"]
    dist = {str(it.get("calendarDate")): it.get("totalDistance") for it in raw["steps"] or [] if isinstance(it, dict)}
    raw_w, failed = body.fetch_weight(c.get, first, end, today, cache_get, cache_put)  # short blocks, shared cache
    if failed:
        errors["weight"] = body.weight_error(failed)
    weighins = body.weight_series(raw_w)
    by_day_w = {w["date"]: w for w in weighins}
    acts: dict[str, list[dict]] = {}
    for a in raw_acts:
        r = history_row(a)
        acts.setdefault(r["date"], []).append({k: r[k] for k in ("id", "name", "type", "start", "dist_m", "secs", "hr", "load", "te")})

    days = {}
    for i, h in enumerate(hist):
        d = date.fromisoformat(h["date"])
        if d < first:
            continue
        has_data = h.get("hrv") is not None or h.get("sleep_h") is not None
        a = coach.assess({"history": hist[:i + 1]}) if has_data else None  # readiness as it stood that day
        w = by_day_w.get(h["date"]) or {}
        days[h["date"]] = dict(
            date=h["date"], readiness=a and a["readiness"], zone=a and a["zone"],
            hrv=h.get("hrv"), hrv_status=h.get("hrv_status"), sleep_score=h.get("sleep_score"), sleep_h=h.get("sleep_h"),
            bb_high=h.get("bb_high"), bb_low=h.get("bb_low"), steps=h.get("steps"), stress=h.get("stress"),
            distance_m=dist.get(h["date"]), load=h.get("load"),
            weight_kg=w.get("kg"), fat=w.get("fat"),
            activities=sorted(acts.get(h["date"], []), key=lambda x: x["start"]))
    return dict(month=f"{year:04d}-{month:02d}", first=first.isoformat(), last=last.isoformat(), days=days,
                summary=summary(days, weighins), errors=errors)


def summary(days: dict, weighins: list[dict]) -> dict:
    acts = [a for d in days.values() for a in d["activities"]]
    vals = lambda k: [d[k] for d in days.values() if d.get(k) is not None]
    pts = [(w["date"], w["kg"]) for w in weighins]
    change = None
    if len(pts) >= 2:
        d0, d1 = date.fromisoformat(pts[0][0]), date.fromisoformat(pts[-1][0])
        a0, a1 = body.avg_window(pts, d0 + timedelta(days=6)), body.avg_window(pts, d1)
        if d1 > d0 and a0 is not None and a1 is not None:
            change = round(a1 - a0, 2)
    sleep, ready = vals("sleep_h"), vals("readiness")
    return dict(
        workouts=sum(1 for a in acts if not is_walk(a["type"])), walks=sum(1 for a in acts if is_walk(a["type"])),
        distance_m=round(sum(a["dist_m"] or 0 for a in acts if any(t in (a["type"] or "") for t in DIST_TYPES))),
        active_min=round(sum((a["secs"] or 0) for a in acts) / 60),
        avg_sleep_h=round(st.mean(sleep), 2) if sleep else None, avg_readiness=round(st.mean(ready)) if ready else None,
        weight_change_kg=change, weighins=len(pts))


def cached(key: str, cache_get) -> tuple[dict | None, bool]:
    """(month data, fresh) from the cache without fetching; None if it was never fetched in this version."""
    hit = cache_get("calendar", key)
    if not hit or hit.get("v") != VERSION:
        return None, False
    return hit["data"], hit["settled"] or time.time() - hit["fetched"] < CACHE_TTL


def month(c: GarminClient, year: int, month_: int, today: date, cache_get, cache_put, refresh: bool = False) -> dict:
    key = f"{year:04d}-{month_:02d}"
    if date(year, month_, 1) > today:
        return dict(month=key, first=f"{key}-01", last=date(year, month_, monthrange(year, month_)[1]).isoformat(),
                    days={}, summary=summary({}, []), errors={}, future=True)
    hit = cache_get("calendar", key)
    if hit and hit.get("v") == VERSION and (hit["settled"] or (not refresh and time.time() - hit["fetched"] < CACHE_TTL)):  # settled months never refetch
        return hit["data"]
    data = fetch_month(c, year, month_, today, cache_get, cache_put)
    settled = date.fromisoformat(data["last"]) < today - timedelta(days=SETTLE_DAYS)
    if not data["errors"] or not settled:  # don't pin a partial month forever
        # a month with errors counts as fetched CACHE_TTL - 60 s ago, so the next look after a minute retries it
        fetched = time.time() - (CACHE_TTL - 60 if data["errors"] else 0)
        cache_put("calendar", key, dict(v=VERSION, fetched=fetched, settled=settled and not data["errors"], data=data))
    return data


def resting_hr(c: GarminClient, day: date, today: date, cache_get, cache_put) -> int | None:
    settled = day <= today - timedelta(days=2)
    hit = cache_get("rhr", day.isoformat()) if settled else None
    if hit is not None:
        return hit.get("rhr")
    try:
        rhr = (c.get("/wellness-service/wellness/dailyHeartRate", {"date": day.isoformat()}) or {}).get("restingHeartRate")
    except GarminError:
        return None
    if settled:
        cache_put("rhr", day.isoformat(), {"rhr": rhr})
    return rhr


def takeaway(analysis: dict | None, system: str) -> str | None:
    """First sentence of a cached Claude analysis ("How it went"), only if written in these units."""
    if not analysis or not analysis.get("text") or analysis.get("units") != system:
        return None
    text = analysis["text"]
    m = re.search(r"#+\s*How it went\s*\n+((?:.+\n?)+)", text, re.I)
    para = re.sub(r"\*\*|__|^[-*]\s+", "", (m.group(1) if m else text).strip(), flags=re.M).replace("\n", " ")
    s = re.match(r"(.+?[.!?])(\s|$)", para)
    out = (s.group(1) if s else para).strip()
    return out if len(out) <= 260 else out[:257].rsplit(" ", 1)[0] + "…"
