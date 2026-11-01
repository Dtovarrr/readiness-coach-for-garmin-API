"""Full activity + daily-health history for Personal Records, fetched once and then incrementally.

Activities come from the paged activity list (100 per page, newest first). The first sync walks every
page; later syncs stop once a page reaches days already cached, keeping an OVERLAP_DAYS window that's
refetched so edits and deletions on Garmin show up. Daily health (steps, resting HR, intensity minutes,
HRV, sleep score, weight) is fetched from the first activity on, in the largest ranges each endpoint
accepts, and after that only from the last synced day minus OVERLAP_DAYS.

`get` is the client's get (or a fake in tests). Everything is stored metric in one cache entry.
"""
from __future__ import annotations

import random
import re
import time
from datetime import date, timedelta

import httpx

import body
from demo_history import demo_rows
from garmin_client import GarminError, first_list_of_dicts, normalize
from records import EFFORTS, SPLIT_FIELDS, number

VERSION = 1
PAGE = 100
OVERLAP_DAYS = 3
MAX_YEARS = 10
LIST = "/activitylist-service/activities/search/activities"
RETRY_WAITS = (5, 20, 60)  # seconds before each retry of a rate-limited / failing / timed-out request
RETRYABLE = {429, 500, 502, 503, 504}
SUMMARY_DAYS = 182  # Garmin allows a year, but answers 500 for some full-year ranges that work in halves
MIN_SPLIT_DAYS = 28  # a 5xx on a longer range is split in two rather than retried as is
ENDPOINTS = {"summary": "/usersummary-service/v2/dailysummaries", "weight": "/weight-service/weight/range",
             "hrv": "/hrv-service/hrv/daily", "sleep": "/sleep-service/sleep/dailySleepSummariesByDate"}
LABELS = {"summary": "daily summary (steps, resting HR, intensity minutes)", "weight": "weight", "hrv": "HRV", "sleep": "sleep"}


def trim(a: dict) -> dict:
    """The fields records need from an activity-list item (the name is for the local page only)."""
    typ = a["activityType"].get("typeKey") if isinstance(a.get("activityType"), dict) else a.get("activityType")
    start = str(a.get("startTimeLocal") or "")
    num = lambda k: float(a[k]) if isinstance(a.get(k), (int, float)) and not isinstance(a.get(k), bool) else None
    return dict(id=a.get("activityId"), name=a.get("activityName"), type=typ, start=start, date=start[:10],
                dist_m=num("distance"), secs=num("movingDuration") or num("duration"), gain_m=num("elevationGain"),
                load=num("activityTrainingLoad"), hr=num("averageHR"), max_hr=num("maxHR"), speed_ms=num("averageSpeed"),
                splits={k: num(f) for k, f in SPLIT_FIELDS.items() if num(f)})


def sync_activities(get, cached: list[dict] | None, progress=lambda n: None) -> list[dict]:
    """All activities (newest first). With a cache, only pages down to the newest cached day minus
    OVERLAP_DAYS are fetched; cached activities inside that window that Garmin no longer lists are
    dropped (deleted)."""
    cached = cached or []
    cutoff = None
    if cached:
        newest = max(a["date"] for a in cached if a.get("date"))
        cutoff = (date.fromisoformat(newest) - timedelta(days=OVERLAP_DAYS)).isoformat()
    fetched, start = [], 0
    while True:
        page = get(LIST, {"start": start, "limit": PAGE}) or []
        fetched += [trim(a) for a in page if isinstance(a, dict) and a.get("activityId") is not None]
        progress(len(fetched))
        if len(page) < PAGE or (cutoff and str(page[-1].get("startTimeLocal") or "")[:10] < cutoff):
            break
        start += PAGE
    ids = {a["id"] for a in fetched}
    floor = min((a["date"] for a in fetched), default=None)
    keep = [a for a in cached if a["id"] not in ids and not (floor and cutoff and a["date"] >= max(floor, cutoff))]
    return sorted(fetched + keep, key=lambda a: a["start"], reverse=True)


def _windows(start: date, end: date, days: int):
    s = start
    while s <= end:
        e = min(end, s + timedelta(days=days - 1))
        yield s, e
        s = e + timedelta(days=1)


def status_of(ex: Exception) -> int | None:
    m = re.search(r"-> (\d{3})", str(ex))
    return int(m.group(1)) if m else None


def splittable(ex: Exception, s: date, e: date) -> bool:
    return isinstance(ex, GarminError) and (status_of(ex) or 0) >= 500 and (e - s).days + 1 > MIN_SPLIT_DAYS


def fetch(get, kind: str, s: date, e: date, sleep=time.sleep):
    """One health range, retried with backoff when Garmin rate-limits (429), errors (5xx) or the
    request times out. A 4xx other than 429 is an answer, not a hiccup, so it isn't retried; nor is
    a 5xx on a long range, which the caller splits instead (retrying it as is fails the same way)."""
    for attempt in range(len(RETRY_WAITS) + 1):
        try:
            if kind in ("summary", "sleep"):
                return get(ENDPOINTS[kind], {"startDate": s.isoformat(), "endDate": e.isoformat()})
            return get(f"{ENDPOINTS[kind]}/{s}/{e}")
        except (GarminError, httpx.TimeoutException, httpx.TransportError) as ex:
            transient = not isinstance(ex, GarminError) or status_of(ex) in RETRYABLE
            if not transient or splittable(ex, s, e) or attempt == len(RETRY_WAITS):
                raise
            sleep(RETRY_WAITS[attempt])


def failure(kind: str, s: date, e: date, ex: Exception) -> dict:
    st_ = status_of(ex) if isinstance(ex, GarminError) else None
    why = ("rate limited by Garmin" if st_ == 429 else f"Garmin server error {st_}" if st_ and st_ >= 500
           else f"Garmin rejected the request ({st_})" if st_ else "timed out" if isinstance(ex, httpx.TimeoutException)
           else f"network error ({type(ex).__name__})")
    return dict(kind=kind, label=LABELS[kind], endpoint=ENDPOINTS[kind], start=s.isoformat(), end=e.isoformat(),
                status=st_, reason=why, detail=str(ex)[:200])


def parse(kind: str, raw, s: date, e: date, put) -> None:
    if kind == "summary":
        for it in first_list_of_dicts(raw):
            k = str(it.get("calendarDate") or "")[:10]
            if not k:
                continue
            im = it.get("intensityMinutes") or {}
            mod, vig = number(im.get("moderate")), number(im.get("vigorous"))
            # movement.steps is an object ({"value", "goal", "distanceInMeters"}); number() unwraps it
            put(k, steps=number((it.get("movement") or {}).get("steps")), rhr=number((it.get("heartRate") or {}).get("restingValue")),
                im=None if mod is None and vig is None else (mod or 0) + 2 * (vig or 0))
    elif kind == "weight":
        for w in body.weight_series(raw):
            put(w["date"], weight_kg=w["kg"])
    else:
        for h in normalize({kind: raw}, s, e)["history"]:
            if kind == "hrv":
                put(h["date"], hrv=h.get("hrv"))
            else:
                put(h["date"], sleep_score=h.get("sleep_score"))


def sync_days(get, days: dict[str, dict], start: date, end: date, progress=lambda done, total: None,
              retry: list[dict] | None = None, sleep=time.sleep) -> tuple[dict, list[dict]]:
    """Fill `days` (iso -> fields) for [start, end], plus any previously failed ranges in `retry`.
    Returns (days, failures): each failure names the endpoint, date range and why, and is retried on
    the next sync instead of in a loop now."""
    days = {k: dict(v) for k, v in days.items()}
    put = lambda k, **f: days.setdefault(k, {}).update({x: v for x, v in f.items() if v is not None})
    plan = [(f["kind"], date.fromisoformat(f["start"]), date.fromisoformat(f["end"])) for f in retry or [] if f.get("kind") in ENDPOINTS]
    plan += ([("summary", s, e) for s, e in _windows(start, end, SUMMARY_DAYS)] + [("weight", s, e) for s, e in _windows(start, end, 365)]
             + [(kind, s, e) for s, e in _windows(start, end, 28) for kind in ("hrv", "sleep")])
    failures, i = [], 0
    while i < len(plan):
        kind, s, e = plan[i]
        try:
            parse(kind, fetch(get, kind, s, e, sleep), s, e, put)
        except (GarminError, httpx.TimeoutException, httpx.TransportError) as ex:
            if splittable(ex, s, e):  # fetch the two halves instead
                mid = s + timedelta(days=((e - s).days + 1) // 2 - 1)
                plan[i + 1:i + 1] = [(kind, s, mid), (kind, mid + timedelta(days=1), e)]
            else:
                failures.append(failure(kind, s, e, ex))
        i += 1
        progress(i, len(plan))
    return days, failures


def upgrade(h: dict | None) -> dict | None:
    """Repair caches written before steps were unwrapped (they hold the whole movement.steps object)."""
    if not h:
        return h
    for d in (h.get("days") or {}).values():
        if isinstance(d, dict) and isinstance(d.get("steps"), dict):
            d["steps"] = number(d["steps"])
    if not isinstance(h.get("failed"), list):  # older caches only kept a count, with no range to retry
        h["failed_unknown"] = h.get("failed") or 0
        h["failed"] = []
    return h


def sync(get, cached: dict | None, today: date, full: bool = False, progress=lambda **k: None) -> dict:
    """One sync pass over both histories; returns the new cache entry."""
    cached = None if full or not cached or cached.get("v") != VERSION else upgrade(cached)
    acts = sync_activities(get, cached and cached["activities"], lambda n: progress(phase="activities", done=n, total=None))
    first = min((date.fromisoformat(a["date"]) for a in acts if a.get("date")), default=today - timedelta(days=365))
    first = max(first, today - timedelta(days=365 * MAX_YEARS))
    days = (cached or {}).get("days") or {}
    through = (cached or {}).get("days_through")
    start = first if not through else max(first, date.fromisoformat(through) - timedelta(days=OVERLAP_DAYS))
    retry = list((cached or {}).get("failed") or [])
    if (cached or {}).get("failed_unknown") and start > first:  # an older sync lost a range without saying which: refill the summaries
        retry += [dict(kind="summary", start=s.isoformat(), end=e.isoformat()) for s, e in _windows(first, start - timedelta(days=1), SUMMARY_DAYS)]
    days, failed = sync_days(get, days, start, today, lambda d, t: progress(phase="health", done=d, total=t), retry=retry)
    return dict(v=VERSION, activities=acts, days=days, days_through=today.isoformat(), synced=time.time(), failed=failed)


# ------------------------------------ demo ------------------------------------ #
def demo_history(today: date) -> dict:
    """Two years of synthetic history in the cached shape, with a few planted glitches so the
    data-quality filters have something to catch."""
    rows = demo_rows(today - timedelta(days=730), today)
    acts, days = [], {}
    for k, r in rows.items():
        days[k] = {f: r.get(f) for f in ("steps", "rhr", "im", "hrv", "sleep_score", "weight_kg")}
        for a in r["activities"]:
            rnd = random.Random(a["id"])
            secs, dist = a["secs"], a["dist_m"]
            splits = {}
            old = k < (today - timedelta(days=540)).isoformat()  # the oldest runs have no split data
            if "run" in a["type"] and dist and not old:
                pace = secs / dist
                for key, (d, _) in EFFORTS.items():
                    if dist >= d:
                        splits[key] = round(d * pace * {"mile": 0.92, "5k": 0.96, "10k": 0.985, "half": 1.0}[key] * rnd.uniform(0.97, 1.03), 1)
            acts.append(dict(id=a["id"], name=a["name"], type=a["type"], start=a["start"], date=k, dist_m=float(dist), secs=float(secs),
                             gain_m=round(dist / 1000 * rnd.uniform(4, 14)) if dist else None, load=float(a["load"]), hr=float(a["hr"]),
                             max_hr=float(a["hr"] + 25), speed_ms=dist / secs if dist else None, splits=splits))
    run = lambda i, d, dist, secs, splits: dict(id=i, name="Running", type="running", start=f"{d} 07:00:00", date=d, dist_m=dist, secs=secs,
                                               gain_m=round(dist / 90), load=round(secs / 25), hr=152.0, max_hr=178.0, speed_ms=dist / secs, splits=splits)
    long_day = (today - timedelta(days=200)).isoformat()
    acts.append(run(9_000_000_010, long_day, 22100.0, 7750.0, {"mile": 470.0, "5k": 1610.0, "10k": 3380.0, "half": 7390.0}))  # a half marathon
    # The latest 5K+ run in the last 2 weeks sets fresh mile and 5K PRs (it's in the Activities list too)
    recent = [a for a in acts if a["type"] == "running" and (a["dist_m"] or 0) >= 5000 and a["date"] >= (today - timedelta(days=14)).isoformat()]
    if recent:
        pr = max(recent, key=lambda a: a["start"])
        for key in ("mile", "5k"):
            pr["splits"][key] = round(min(a["splits"][key] for a in acts if a["splits"].get(key)) * 0.97, 1)
    # planted glitches
    g1, g2 = (today - timedelta(days=90)).isoformat(), (today - timedelta(days=300)).isoformat()
    acts.append(dict(id=9_000_000_001, name="Running", type="running", start=f"{g1} 06:00:00", date=g1, dist_m=8000.0, secs=2700.0,
                     gain_m=60, load=90.0, hr=150.0, max_hr=175.0, speed_ms=2.96, splits={"mile": 140.0, "5k": 1500.0}))  # GPS spike mile
    acts.append(dict(id=9_000_000_002, name="Walk", type="walking", start=f"{g2} 18:00:00", date=g2, dist_m=42000.0, secs=1800.0,
                     gain_m=10, load=4.0, hr=95.0, max_hr=110.0, speed_ms=23.3, splits={}))  # left recording in the car
    if g2 in days:
        days[g2]["weight_kg"] = 118.0  # someone else on the scale
    return dict(v=VERSION, activities=sorted(acts, key=lambda a: a["start"], reverse=True), days=days,
                days_through=today.isoformat(), synced=time.time(), failed=[])
