"""Optional sync of a workout to the Intervals.icu calendar (off by default).

Official API (https://intervals.icu/api/v1/docs): personal use is HTTP Basic auth with the username
"API_KEY" and your key as the password; athlete id "0" means the key's own athlete.
POST /athlete/{id}/events creates a planned workout and accepts a .fit file as base64; with
upsertOnUid=true and a stable `uid`, syncing again updates the same calendar entry instead of adding
another. If Intervals.icu is connected to Garmin Connect (in Intervals.icu's settings), it can push
the planned workout on to the watch.

The key lives only in .env (INTERVALS_API_KEY, optional INTERVALS_ATHLETE_ID). This app never writes,
shows or logs it.
"""
from __future__ import annotations

import base64
import os

import httpx

import workouts

BASE = "https://intervals.icu/api/v1"
TYPE = {"run": "Run", "ride": "Ride", "strength": "WeightTraining", "timed": "Workout"}
UA = "GarminCoach/1.0 (personal, local)"  # Cloudflare in front of the API blocks some default client agents


class IntervalsError(RuntimeError):
    pass


def key_set() -> bool:
    return bool(os.getenv("INTERVALS_API_KEY"))


def athlete() -> str:
    a = (os.getenv("INTERVALS_ATHLETE_ID") or "0").strip()
    return a if a.isalnum() else "0"


def payload(w: dict, fit: bytes, day: str) -> dict:
    est = workouts.estimate(w)
    return dict(category="WORKOUT", start_date_local=f"{day}T00:00:00", type=TYPE[w["sport"]], name=w["name"],
                description=w.get("notes") or "", moving_time=est["sec"] or None, uid=f"garmin-coach-{w['id']}",
                filename=f"{w['id']}.fit", file_contents_base64=base64.b64encode(fit).decode())


def push(w: dict, fit: bytes, day: str, post=httpx.post) -> dict:
    """Create or update the workout on the Intervals.icu calendar. Returns {id, day}."""
    key = os.getenv("INTERVALS_API_KEY")
    if not key:
        raise IntervalsError("Add INTERVALS_API_KEY to .env and restart the app.")
    try:
        r = post(f"{BASE}/athlete/{athlete()}/events", params=dict(upsertOnUid="true"), json=payload(w, fit, day),
                 auth=("API_KEY", key), headers={"User-Agent": UA, "Accept": "application/json"}, timeout=20)
    except httpx.HTTPError as ex:
        raise IntervalsError(f"Couldn't reach Intervals.icu ({type(ex).__name__}).") from ex
    if r.status_code in (401, 403):
        raise IntervalsError("Intervals.icu refused the key (check INTERVALS_API_KEY and INTERVALS_ATHLETE_ID in .env).")
    if r.status_code == 429:
        raise IntervalsError("Intervals.icu rate limit reached; try again later.")
    if r.status_code >= 400:
        raise IntervalsError(f"Intervals.icu answered {r.status_code}.")
    try:
        ev = r.json()
    except ValueError:
        ev = {}
    return dict(id=ev.get("id"), day=day)
