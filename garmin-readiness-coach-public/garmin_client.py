"""Garmin Connect Personal API client + tolerant normalizer.

Auth flow (per Garmin's Personal API FAQ):
  POST {BASE}/personalapitoken-service/personal-api/personal-api-key/token
  Authorization: ApiKey <your key>   ->  OAuth2 access token (valid 24h, no refresh)

The key is read from the GARMIN_API_KEY environment variable / .env file and is
never written to disk or logged by this code.
"""
from __future__ import annotations

import base64
import json
import os
import threading
import time
from collections import defaultdict, deque
from datetime import date, timedelta
from typing import Any

import httpx

BASE = "https://personal.connectapi.garmin.com"
RATE_LIMIT = (90, 60.0)  # requests per window: Garmin answers 429 above 100/min
TOKEN_PATH = "/personalapitoken-service/personal-api/personal-api-key/token"
TOKEN_URL = BASE + TOKEN_PATH
# Garmin's gateway (Cloudflare) gives up after ~8 s and answers 504 when the origin is slow (the weight
# service often is on a cold request); one short retry almost always gets through.
TRANSIENT = {502, 503, 504}
TRANSIENT_WAIT = 2.0
KEY_REJECTED_HOLD = 60.0  # after Garmin rejects the key, don't ask again for this long (every fetch would)


class GarminError(Exception):
    def __init__(self, msg: str, status: int | None = None):
        super().__init__(msg)
        self.status = status


# --------------------------------------------------------------------------- #
# Generic helpers for schemas we can't see in the docs (Swagger lists DTO names
# only), so parsing is key-name based and recursive rather than positional.
# --------------------------------------------------------------------------- #
def find(obj: Any, *names: str, default=None):
    """Breadth-first search for the first non-null value whose key matches."""
    wanted = {n.lower() for n in names}
    queue = [obj]
    while queue:
        cur = queue.pop(0)
        if isinstance(cur, dict):
            for k, v in cur.items():
                if k.lower() in wanted and v is not None and not isinstance(v, (dict, list)):
                    return v
            queue.extend(v for v in cur.values() if isinstance(v, (dict, list)))
        elif isinstance(cur, list):
            queue.extend(x for x in cur if isinstance(x, (dict, list)))
    return default


def first_list_of_dicts(obj: Any) -> list[dict]:
    if isinstance(obj, list):
        return [x for x in obj if isinstance(x, dict)]
    if isinstance(obj, dict):
        for v in obj.values():
            if isinstance(v, list) and v and isinstance(v[0], dict):
                return v
        for v in obj.values():
            if isinstance(v, dict):
                r = first_list_of_dicts(v)
                if r:
                    return r
    return []


def _num(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _retry_after(r) -> float | None:
    """Seconds to wait on a 429 if Garmin says it's short (<= 65 s), else None."""
    if r.status_code != 429:
        return None
    try:
        wait = float(r.json().get("retryInSeconds", 0))
    except (ValueError, AttributeError):
        return None
    return wait if 0 < wait <= 65 else None


def _jwt_ttl(tok: str) -> float | None:
    """Seconds until the JWT's `exp` claim (unverified decode; only used for caching)."""
    try:
        payload = tok.split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        return float(claims["exp"]) - time.time()
    except Exception:
        return None


def _day_of(item: dict) -> str | None:
    d = find(item, "calendarDate", "date", "summaryDate", "startTimeLocal")
    return str(d)[:10] if d else None


# --------------------------------------------------------------------------- #
class GarminClient:
    def __init__(self, api_key: str | None = None):
        self.api_key = (api_key or os.getenv("GARMIN_API_KEY", "")).strip()
        if not self.api_key:
            raise GarminError("GARMIN_API_KEY is not set (see .env.example).")
        self._token: str | None = None
        self._token_exp = 0.0
        self._http = httpx.Client(timeout=30)
        self._sent: deque[float] = deque()
        self._rate_lock = threading.Lock()
        # Optional observer, called after every request as on_call(path, params, status, ms, value, error, retries)
        # (used by the System status panel; it never sees the key or the token).
        self.on_call = None
        self._rejected: tuple[float, GarminError] | None = None  # (when, error) of the last refused key

    def _report(self, path, params, status, t0, value=None, error=None, retries=()):
        if self.on_call:
            try:
                self.on_call(path, params, status, round((time.time() - t0) * 1000), value, error, list(retries))
            except Exception:  # an observer must never break a fetch
                pass

    @property
    def token_expires_at(self) -> float | None:
        """When the cached token stops being used (epoch seconds), or None if there isn't one."""
        return self._token_exp + 300 if self._token else None

    # -- auth ---------------------------------------------------------------
    def _refresh_token(self) -> None:
        t0, retries = time.time(), []

        def post():
            self._throttle()
            return self._http.post(TOKEN_URL, headers={"Authorization": f"ApiKey {self.api_key}"})

        try:
            r = post()
            wait = _retry_after(r)
            if wait:
                retries.append("429")
                time.sleep(wait + 1)
                r = post()
        except httpx.HTTPError as ex:
            self._report(TOKEN_PATH, None, None, t0, error=ex, retries=retries)
            raise
        err = None
        if r.status_code == 429:
            err = GarminError("Garmin rate limit reached (about 100 requests/minute); try again in a minute.", 429)
        elif r.status_code != 200:
            err = GarminError(
                f"Token request failed ({r.status_code}). Key expired/deleted? Create a new one in Garmin Connect Web.",
                r.status_code)
        if err:
            if r.status_code in (400, 401, 403):
                self._rejected = (time.time(), err)
            self._report(TOKEN_PATH, None, r.status_code, t0, error=err, retries=retries)
            raise err
        self._rejected = None
        # The endpoint returns the bare JWT as text/plain; accept JSON too in case that changes.
        if "json" in r.headers.get("content-type", ""):
            data = r.json()
            tok = find(data, "access_token", "accessToken", "token")
            ttl = _num(find(data, "expires_in", "expiresIn"))
        else:
            tok = r.text.strip().strip('"')
            ttl = _jwt_ttl(tok)
        if not tok:
            err = GarminError("Token response contained no access token.", r.status_code)
            self._report(TOKEN_PATH, None, r.status_code, t0, error=err, retries=retries)
            raise err
        self._token = tok
        self._token_exp = time.time() + min(ttl or 86400, 86400) - 300
        self._report(TOKEN_PATH, None, r.status_code, t0, retries=retries)

    def check_token(self, force: bool = False) -> None:
        """Make sure a token can be had (a fresh exchange with force=True); raises GarminError if not."""
        if force:
            self._token, self._rejected = None, None
        self._headers()

    def _headers(self):
        if not self._token or time.time() >= self._token_exp:
            if self._rejected and time.time() - self._rejected[0] < KEY_REJECTED_HOLD:
                raise self._rejected[1]  # the same key was refused moments ago; asking again only burns quota
            self._refresh_token()
        return {"Authorization": f"Bearer {self._token}", "Accept": "application/json"}

    def _request(self, path: str, params: dict | None = None, accept: str = "application/json", parse=None):
        """GET `path` and return parse(response) (the response itself without `parse`). Retries once each
        for a rejected token (401), a short rate-limit wait (429) and a gateway hiccup (5xx / timeout)."""
        if not path.startswith("/"):
            raise GarminError("path must start with /")
        t0, retries = time.time(), []

        def send():
            self._throttle()
            return self._http.get(BASE + path, params=params, headers={**self._headers(), "Accept": accept},
                                  follow_redirects=True)

        try:
            try:
                r = send()
            except (httpx.TimeoutException, httpx.TransportError) as ex:
                retries.append(type(ex).__name__)
                time.sleep(TRANSIENT_WAIT)
                r = send()
            if r.status_code == 401:  # token rejected -> mint a new one once
                retries.append("401")
                self._token = None
                r = send()
            wait = _retry_after(r)
            if wait:  # over the per-minute quota: wait it out once
                retries.append("429")
                time.sleep(wait + 1)
                r = send()
            if r.status_code in TRANSIENT:
                retries.append(str(r.status_code))
                time.sleep(TRANSIENT_WAIT)
                r = send()
        except GarminError as ex:  # token exchange failed (already reported for the token itself)
            self._report(path, params, ex.status, t0, error=ex, retries=retries)
            raise
        except httpx.HTTPError as ex:  # still unreachable / timing out after the retry
            self._report(path, params, None, t0, error=ex, retries=retries)
            raise
        if r.status_code >= 400:
            err = GarminError(f"GET {path} -> {r.status_code}: {r.text[:200]}", r.status_code)
            self._report(path, params, r.status_code, t0, error=err, retries=retries)
            raise err
        value = parse(r) if parse else r
        self._report(path, params, r.status_code, t0, value=value if parse else None, retries=retries)
        return value

    def _throttle(self) -> None:
        """Sliding-window limiter shared by all threads, so bursts (history backfills) stay under quota."""
        n, window = RATE_LIMIT
        while True:
            with self._rate_lock:
                now = time.time()
                while self._sent and now - self._sent[0] > window:
                    self._sent.popleft()
                if len(self._sent) < n:
                    self._sent.append(now)
                    return
                wait = window - (now - self._sent[0]) + 0.05
            time.sleep(wait)

    def get(self, path: str, params: dict | None = None):
        return self._request(path, params, parse=lambda r: None if r.status_code == 204 else r.json())

    def download(self, path: str) -> bytes:
        """Binary endpoints (activity files) answer 406 to Accept: application/json."""
        return self._request(path, accept="*/*").content

    def activities_since(self, days: int) -> list[dict]:
        """Activity-list items from the last `days` days (newest first), paging as needed."""
        cutoff = (date.today() - timedelta(days=days)).isoformat()
        out, start = [], 0
        while True:
            page = self.get("/activitylist-service/activities/search/activities", {"start": start, "limit": 100}) or []
            out += page
            if len(page) < 100 or str(page[-1].get("startTimeLocal", ""))[:10] < cutoff:
                break
            start += 100
        return [a for a in out if str(a.get("startTimeLocal", ""))[:10] >= cutoff]

    def chunked(self, fetch, start: date, end: date, days: int = 28) -> list:
        """Concatenate list results of `fetch(start_iso, end_iso)` over windows of `days`
        (the daily stats endpoints reject ranges longer than ~28 days)."""
        out, s = [], start
        while s <= end:
            e = min(end, s + timedelta(days=days - 1))
            out += fetch(s.isoformat(), e.isoformat()) or []
            s = e + timedelta(days=1)
        return out

    # -- snapshot -----------------------------------------------------------
    def snapshot(self, days: int = 90) -> dict:
        end = date.today()
        start = end - timedelta(days=days - 1)
        s, e = start.isoformat(), end.isoformat()
        errors: dict[str, str] = {}

        def safe(name, path, params=None):
            try:
                return self.get(path, params)
            except Exception as ex:  # keep dashboard alive if one endpoint differs
                errors[name] = str(ex)[:220]
                return None

        def chunks(name, fetch):  # these endpoints cap ranges at ~28 days
            try:
                return self.chunked(fetch, start, end)
            except Exception as ex:
                errors[name] = str(ex)[:220]
                return None

        raw = {
            "hrv": safe("hrv", f"/hrv-service/hrv/daily/{s}/{e}"),  # accepts 90 days in one call
            "sleep": chunks("sleep", lambda a, b: self.get("/sleep-service/sleep/dailySleepSummariesByDate",
                                                           {"startDate": a, "endDate": b})),
            "bb": chunks("bodyBattery", lambda a, b: self.get("/wellness-service/wellness/bodyBattery/reports/daily",
                                                              {"startDate": a, "endDate": b})),
            "steps": chunks("steps", lambda a, b: self.get(f"/usersummary-service/stats/steps/daily/{a}/{b}")),
            "stress": chunks("stress", lambda a, b: self.get(f"/usersummary-service/stats/stress/daily/{a}/{b}")),
            "acts": safe("activities", "/activitylist-service/activities/search/activities",
                         {"start": 0, "limit": 80}),
            "hrv_today": safe("hrv_today", f"/hrv-service/hrv/{e}"),
            # Resting HR isn't in the sleep summaries and has no range endpoint; the coach only
            # looks at the last 14 days, so fetch just those.
            "rhr": [safe("rhr", "/wellness-service/wellness/dailyHeartRate",
                         {"date": (end - timedelta(days=i)).isoformat()}) for i in range(min(14, days))],
        }
        snap = normalize(raw, start, end)
        snap["errors"] = errors
        snap["source"] = "garmin"
        return snap


# --------------------------------------------------------------------------- #
def normalize(raw: dict, start: date, end: date) -> dict:
    days: dict[str, dict] = defaultdict(dict)
    n = (end - start).days + 1
    for i in range(n):
        days[(start + timedelta(days=i)).isoformat()] = {}

    # HRV (nightly average + baseline band)
    for it in first_list_of_dicts(raw.get("hrv")):
        d = _day_of(it)
        if d in days:
            days[d].update(
                hrv=_num(find(it, "lastNightAvg")),
                hrv_weekly=_num(find(it, "weeklyAvg")),
                hrv_status=find(it, "status"),
                hrv_low=_num(find(it, "balancedLow")),
                hrv_high=_num(find(it, "balancedUpper")),
            )
    ht = raw.get("hrv_today")
    if ht:
        d = end.isoformat()
        cur = days[d]
        cur.setdefault("hrv", _num(find(ht, "lastNightAvg")))
        for k, names in (("hrv_weekly", ("weeklyAvg",)), ("hrv_low", ("balancedLow",)),
                         ("hrv_high", ("balancedUpper",)), ("hrv_status", ("status",))):
            if cur.get(k) is None:
                v = find(ht, *names)
                cur[k] = v if k == "hrv_status" else _num(v)

    # Sleep
    for it in first_list_of_dicts(raw.get("sleep")):
        d = _day_of(it)
        if d in days:
            secs = _num(find(it, "sleepTimeSeconds", "totalSleepSeconds"))
            days[d].update(
                sleep_score=_num(((it.get("sleepScores") or {}).get("overall") or {}).get("value")),
                sleep_h=round(secs / 3600, 2) if secs else None,
                deep_h=_hours(find(it, "deepSleepSeconds")),
                rem_h=_hours(find(it, "remSleepSeconds")),
            )
    for it in raw.get("rhr") or []:
        d = _day_of(it) if isinstance(it, dict) else None
        if d in days:
            days[d]["rhr"] = _num(it.get("restingHeartRate"))

    # Body Battery
    for it in first_list_of_dicts(raw.get("bb")):
        d = _day_of(it)
        if d in days:
            vals = []
            for pair in it.get("bodyBatteryValuesArray") or []:
                if isinstance(pair, (list, tuple)) and len(pair) >= 2 and pair[1] is not None:
                    vals.append(pair[1])
            days[d].update(
                bb_high=max(vals) if vals else _num(find(it, "highest", "bodyBatteryHighestValue")),
                bb_low=min(vals) if vals else _num(find(it, "lowest", "bodyBatteryLowestValue")),
                bb_charged=_num(find(it, "charged")),
                bb_drained=_num(find(it, "drained")),
            )

    for it in first_list_of_dicts(raw.get("steps")):
        d = _day_of(it)
        if d in days:
            days[d]["steps"] = _num(find(it, "totalSteps", "steps"))
            days[d]["step_goal"] = _num(find(it, "stepGoal"))
    for it in first_list_of_dicts(raw.get("stress")):
        d = _day_of(it)
        if d in days:
            days[d]["stress"] = _num(find(it, "overallStressLevel", "avgStressLevel", "averageStressLevel"))

    # Activities -> per-day load + list
    acts = []
    for a in first_list_of_dicts(raw.get("acts")):
        d = _day_of(a)
        if not d:
            continue
        dur = _num(a.get("duration")) or 0
        load = _num(a.get("activityTrainingLoad"))
        avg_hr = _num(a.get("averageHR"))
        if load is None:  # fallback proxy: minutes scaled by relative intensity
            load = round(dur / 60 * (0.6 + max(0.0, ((avg_hr or 110) - 100)) / 100), 1) if dur else 0
        typ = (a.get("activityType") or {}).get("typeKey") if isinstance(a.get("activityType"), dict) else a.get("activityType")
        acts.append(dict(
            id=a.get("activityId"), date=d, name=a.get("activityName") or typ or "Activity", type=typ,
            minutes=round(dur / 60, 1), km=round((_num(a.get("distance")) or 0) / 1000, 2),
            avg_hr=avg_hr, load=round(load, 1),
            te=_r1(a.get("aerobicTrainingEffect")),
        ))
        if d in days:
            days[d]["load"] = round(days[d].get("load", 0) + load, 1)
            days[d]["active_min"] = round(days[d].get("active_min", 0) + dur / 60, 1)

    history = [dict(date=d, **v) for d, v in sorted(days.items())]
    return dict(history=history, activities=sorted(acts, key=lambda x: x["date"], reverse=True)[:60],
                generated=date.today().isoformat())


def _hours(secs):
    s = _num(secs)
    return round(s / 3600, 2) if s else None


def _r1(x):
    v = _num(x)
    return None if v is None else round(v, 1)
