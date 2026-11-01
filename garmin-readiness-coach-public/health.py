"""System status: Garmin connection, each data feed, the AI coach and this computer.

Every Garmin request reports here through GarminClient.on_call (status, timing, record count, error),
so the panel reflects the app's real fetches; explicit checks add a cheap probe per feed that also
looks at freshness and values. Nothing here ever sees the Garmin key or token: the client hands over
paths, statuses and parsed payloads only. The AI test is a separate, explicit call (it costs credits).
"""
from __future__ import annotations

import json
import os
import re
import shutil
import statistics as st
import threading
import time
from collections import deque
from datetime import date, datetime, timedelta
from pathlib import Path

import httpx

import applog
from garmin_client import TOKEN_PATH, first_list_of_dicts, normalize

APP_VERSION = "1.0.0"
STARTED = time.time()
LOG_SIZE = 100
STALE_DAYS = 2
REPEATED = 3  # consecutive failures that count as "repeated"
DEFAULT_MODEL = "claude-sonnet-5-5"  # same default as ai.DEFAULTS["deep"]

# name -> (label, path prefix, raw key for garmin_client.normalize, day field, expects new data daily)
FEEDS = {
    "hrv": ("HRV", "/hrv-service", "hrv", "hrv", True),
    "sleep": ("Sleep", "/sleep-service", "sleep", "sleep_h", True),
    "body_battery": ("Body Battery", "/wellness-service/wellness/bodyBattery", "bb", "bb_high", True),
    "stress": ("Stress", "/usersummary-service/stats/stress", "stress", "stress", True),
    "steps": ("Steps", "/usersummary-service/stats/steps", "steps", "steps", True),
    "activities": ("Activities", "/activitylist-service", None, None, False),
    "weight": ("Weight & body composition", "/weight-service", None, None, False),
    "fitness_age": ("Fitness age", "/fitnessage-service", None, None, False),
    "profile": ("User profile", "/userprofile-service", None, None, False),
}
# Plausible ranges; anything outside is flagged as "looks wrong".
LIMITS = {"hrv": (5, 250), "sleep": (0.5, 16), "body_battery": (1, 100), "stress": (1, 100), "steps": (1, 100_000),
          "weight": (25, 300), "fitness_age": (10, 100)}

_lock = threading.RLock()
_events: deque[dict] = deque(maxlen=LOG_SIZE)
_state: dict = {}
_log_path: Path | None = None


def _fresh_state() -> dict:
    return dict(
        # records/range/ms describe the status check's own request once there is one (the app's background
        # history syncs ask for old ranges, which would read oddly next to "newest data today"); `call` is
        # always the latest request of any kind.
        feeds={k: dict(name=k, label=v[0], last_attempt=None, last_ok=None, records=None, range=None, ms=None,
                       retries=[], failures=0, error=None, latest=None, anomalies=[], stale=False, probed=None, call=None)
               for k, v in FEEDS.items()},
        token=dict(last_ok=None, last_attempt=None, error=None, ms=None),
        ai=dict(tested=None, ok=None, ms=None, model=None, error=None),
        checked=None, light_checked=None, local=None)


_state = _fresh_state()


def init(cache_dir: Path) -> None:
    """Load the persisted event log from cache_dir/health/events.json."""
    global _log_path
    _log_path = Path(cache_dir) / "health" / "events.json"
    try:
        saved = json.loads(_log_path.read_text())
        with _lock:
            _events.clear()
            _events.extend(saved[-LOG_SIZE:])
    except (OSError, ValueError, TypeError):
        pass


def reset() -> None:
    """Forget everything (tests)."""
    global _state
    with _lock:
        _state = _fresh_state()
        _events.clear()


# --------------------------------------------------------------------------- event log
def event(level: str, source: str, message: str, now: float | None = None) -> None:
    """Add to the rolling log; a repeat of the last message within 5 minutes bumps its count instead."""
    now = now or time.time()
    message = redact_text(message)[:300]
    with _lock:
        last = _events[-1] if _events else None
        if last and last["source"] == source and last["message"] == message and now - last["t"] < 300:
            last["count"] = last.get("count", 1) + 1
            last["t"] = now
        else:
            _events.append(dict(t=now, level=level, source=source, message=message))
        snapshot = list(_events)
    if _log_path:
        try:
            _log_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = _log_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(snapshot))
            tmp.replace(_log_path)
        except OSError:
            pass


def events(level: str = "all") -> list[dict]:
    """Newest first; level is all | errors | warnings."""
    want = {"errors": {"error"}, "warnings": {"warning"}}.get(level)
    with _lock:
        return [dict(e) for e in reversed(_events) if not want or e["level"] in want]


# --------------------------------------------------------------------------- classification
def _ex_status(ex) -> int | None:
    s = getattr(ex, "status", None)
    if s is None and ex is not None:
        m = re.search(r"-> (\d{3})", str(ex))
        s = int(m.group(1)) if m else None
    return s


def classify(status: int | None = None, ex: BaseException | None = None, token: bool = False) -> dict:
    """A Garmin failure in plain words: {kind, title, explain, fix}."""
    status = status if status is not None else _ex_status(ex)
    if isinstance(ex, httpx.TimeoutException):
        return dict(kind="timeout", title="Garmin took too long to answer",
                    explain="The request timed out before Garmin replied.", fix="This is usually temporary. Try again in a minute.")
    if status is None and isinstance(ex, httpx.HTTPError):
        return dict(kind="network", title="Couldn't reach Garmin",
                    explain="The connection failed before Garmin answered.", fix="Check that this computer is online, then retry.")
    if status is None and ex is not None and "not set" in str(ex):
        return no_key()
    if token and status in (400, 401, 403):
        return dict(kind="key_rejected", title="Garmin rejected the API key",
                    explain="The key may have expired, been deleted, or been copied incompletely.",
                    fix="Create a new key in Garmin Connect (web) under the Personal API settings, paste it into .env as "
                        "GARMIN_API_KEY, then restart the app.")
    if status in (401, 403):
        return dict(kind="unauthorized", title="Garmin refused this request",
                    explain="The sign-in token was rejected even after getting a new one.",
                    fix="Run the checks again; if it keeps happening, create a new API key in Garmin Connect.")
    if status == 404:
        return dict(kind="not_found", title="Garmin has no such data",
                    explain="Garmin answered 404 - this data may not exist for your account or device.",
                    fix="Nothing to fix unless you expect this data; check the device syncs it in Garmin Connect.")
    if status == 429:
        return dict(kind="rate_limited", title="Garmin is rate limiting",
                    explain="Too many requests in a short time (Garmin allows about 100 a minute).",
                    fix="Wait a minute; the app slows itself down and retries automatically.")
    if status == 504:
        return dict(kind="gateway_timeout", title="Garmin's server timed out",
                    explain="Garmin's gateway gave up waiting (about 8 seconds) for its own servers. This is on Garmin's "
                            "side and usually clears within minutes.",
                    fix="The app retries automatically; you can also press Retry now.")
    if status and status >= 500:
        return dict(kind="server_error", title="Garmin had a server problem",
                    explain=f"Garmin answered {status}, an error on its side.", fix="Usually temporary. Retry in a few minutes.")
    if status and status >= 400:
        return dict(kind="bad_request", title="Garmin didn't accept the request",
                    explain=f"Garmin answered {status}. The date range or parameters may not be supported.",
                    fix="Retry; if it keeps failing, copy the diagnostics and ask for help.")
    return dict(kind="unknown", title="Something went wrong", explain=str(ex or "")[:160] or "Unknown error.",
                fix="Retry; if it keeps failing, copy the diagnostics and ask for help.")


def no_key() -> dict:
    return dict(kind="no_key", title="No Garmin API key",
                explain="GARMIN_API_KEY isn't set, so the app is showing demo data.",
                fix="Create a key in Garmin Connect (web) under the Personal API settings, put it in .env as GARMIN_API_KEY, "
                    "then restart the app.")


def classify_ai(status: int | None = None, body: str = "", ex: BaseException | None = None) -> dict:
    """An Anthropic API failure in plain words."""
    etype = ""
    try:
        etype = (json.loads(body).get("error") or {}).get("type", "")
    except (ValueError, AttributeError):
        pass
    low = body.lower()
    if isinstance(ex, httpx.TimeoutException):
        return dict(kind="timeout", title="The AI service took too long", explain="No reply before the time limit.",
                    fix="Try again in a minute.")
    if status is None and ex is not None:
        return dict(kind="network", title="Couldn't reach Anthropic", explain="The connection failed.",
                    fix="Check that this computer is online, then retry.")
    if status == 401 or etype == "authentication_error":
        return dict(kind="invalid_key", title="The Anthropic API key is invalid",
                    explain="Anthropic didn't recognise ANTHROPIC_API_KEY (revoked, mistyped or from another account).",
                    fix="Create a key at console.anthropic.com → API keys, put it in .env as ANTHROPIC_API_KEY and restart.")
    if status == 402 or etype == "billing_error" or "credit balance" in low:
        return dict(kind="billing", title="Out of Anthropic credits",
                    explain="The account's credit balance is too low or there's a billing problem.",
                    fix="Add credits or update billing at console.anthropic.com → Billing.")
    if status == 403 or etype == "permission_error":
        return dict(kind="permission", title="This key isn't allowed to do that",
                    explain="The key lacks permission for this model or workspace.", fix="Check the key's workspace in the Anthropic console.")
    if status == 404 or etype == "not_found_error":
        return dict(kind="model_not_found", title="Model not found",
                    explain="The configured model doesn't exist or isn't available to this key.",
                    fix=f"Pick a model your key can use in Settings › Coach (or COACH_MODEL_DEEP / COACH_MODEL_FAST in .env); the default is {DEFAULT_MODEL}.")
    if status == 429 or etype == "rate_limit_error":
        return dict(kind="rate_limited", title="Anthropic rate limit reached",
                    explain="Too many requests for this key right now.", fix="Wait a minute and try again.")
    if status == 529 or etype == "overloaded_error":
        return dict(kind="overloaded", title="Anthropic is overloaded", explain="The service is busy right now.",
                    fix="Try again in a few minutes.")
    if status and status >= 500:
        return dict(kind="server_error", title="Anthropic had a server problem", explain=f"The API answered {status}.",
                    fix="Try again in a few minutes.")
    return dict(kind="bad_request", title="The AI request was rejected", explain=f"The API answered {status}.",
                fix="Copy the diagnostics and ask for help.")


# --------------------------------------------------------------------------- passive recording
DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")


def feed_of(path: str) -> str | None:
    for name, (_, prefix, *_rest) in FEEDS.items():
        if path.startswith(prefix):
            return name
    return None


def describe_range(path: str, params: dict | None) -> str | None:
    p = params or {}
    ds = DATE_RE.findall(path) or [str(p[k]) for k in ("startDate", "endDate", "date", "calendarDate") if p.get(k)]
    if ds:
        return ds[0] if len(ds) == 1 or ds[0] == ds[-1] else f"{ds[0]} → {ds[-1]}"
    if "limit" in p:
        return f"latest {p['limit']}" + (f" from #{p['start']}" if p.get("start") else "")
    return None


def count_records(value) -> int:
    if value is None:
        return 0
    if isinstance(value, list):
        return len(value)
    if isinstance(value, dict):
        rows = (value.get("dailyWeightSummaries") if "dailyWeightSummaries" in value else first_list_of_dicts(value))
        return len(rows) if rows else (1 if value else 0)
    return 1


def _short_error(status, ex, path, token=False) -> dict:
    return dict(classify(status, ex, token=token), status=status, endpoint=path, t=time.time(),
                detail=redact_text(f"{type(ex).__name__}: {ex}")[:240] if ex else None)


def record_call(path, params, status, ms, value, error, retries) -> None:
    """GarminClient.on_call hook: update the feed (or the token) and log failures, retries and recoveries."""
    now = time.time()
    if path == TOKEN_PATH:
        with _lock:
            tok = _state["token"]
            was_failing = tok["error"] is not None
            tok.update(last_attempt=now, ms=ms)
            if error is None:
                tok.update(last_ok=now, error=None)
            else:
                tok["error"] = _short_error(status, error, path, token=True)
        if error is not None:
            event("error", "Garmin", f"Sign-in failed: {tok['error']['title']}" + (f" ({status})" if status else ""))
        else:
            event("info", "Garmin", "Signed in again" if was_failing else "Garmin sign-in token renewed")
        return
    name = feed_of(path)
    if not name:
        return
    label = FEEDS[name][0]
    rng = describe_range(path, params)
    with _lock:
        f = _state["feeds"][name]
        prev_failures = f["failures"]
        f["call"] = dict(t=now, range=rng, ms=ms, records=count_records(value) if error is None else None, status=status)
        f.update(last_attempt=now, retries=list(retries or []))
        if f["probed"] is None:
            f.update(range=rng, ms=ms)
        if error is None:
            f.update(last_ok=now, failures=0, error=None)
            if f["probed"] is None:
                f["records"] = f["call"]["records"]
        else:
            f["failures"] += 1
            f["error"] = _short_error(status, error, path, token="Token request" in str(error))
        failures = f["failures"]
    if error is not None:
        c = f["error"]
        event("error", label, f"{c['title']}" + (f" ({status})" if status else "") + (f" for {rng}" if rng else "")
              + (f" - failed {failures} times in a row" if failures >= REPEATED else ""))
    elif prev_failures:
        event("info", label, "Loading again")
    if retries:
        event("warning", label, f"Retried after {', '.join(retries)}" + (" and recovered" if error is None else ""))


# --------------------------------------------------------------------------- freshness + anomalies
def stale(feed: str, latest: str | None, today: date, weigh_dates: list[str] | None = None) -> str | None:
    """A sentence if the feed has gone quiet when it shouldn't have, else None."""
    if not latest:
        return None
    gap = (today - date.fromisoformat(latest[:10])).days
    if FEEDS[feed][4]:
        return f"No new data for {gap} days" if gap > STALE_DAYS else None
    if feed == "weight" and weigh_dates and len(weigh_dates) >= 4:
        ds = sorted(date.fromisoformat(d) for d in weigh_dates)
        cadence = st.median((b - a).days for a, b in zip(ds, ds[1:]))
        allowed = max(STALE_DAYS, 3 * cadence)
        return f"No weigh-in for {gap} days (you usually weigh in every {cadence:g} days)" if gap > allowed else None
    return None


def anomalies(feed: str, points: list[tuple[str, float | None]], today: date) -> list[str]:
    """Values that look wrong: zeros on finished days, out-of-range values, sudden jumps."""
    out = []
    lo, hi = LIMITS.get(feed, (None, None))
    pts = [(d, v) for d, v in points if v is not None]
    for d, v in pts:
        if d >= today.isoformat():  # today is still filling in
            continue
        if v == 0:
            out.append(f"Zero on {d}" + (" (watch not worn?)" if feed in ("steps", "body_battery", "stress") else ""))
        elif lo is not None and not lo <= v <= hi:
            out.append(f"{v:g} on {d} is outside the plausible range {lo:g}–{hi:g}")
    if feed == "weight":
        for (d0, a), (d1, b) in zip(pts, pts[1:]):
            days = max(1, (date.fromisoformat(d1) - date.fromisoformat(d0)).days)
            if days <= 2 and abs(b - a) > 3:
                out.append(f"Weight jumped {b - a:+.1f} kg between {d0} and {d1}")
    if feed == "hrv" and len(pts) >= 4:
        med = st.median(v for _, v in pts if v)
        for d, v in pts:
            if med and v and (v > 2.5 * med or v < 0.4 * med):
                out.append(f"HRV {v:g} on {d} is far from your usual {med:g}")
    return out[:5]


def evaluate(feed: str, value, today: date, start: date) -> dict:
    """Probe result -> {records, latest, anomalies, stale}."""
    recs = count_records(value)
    found: dict = dict(records=recs, latest=None, anomalies=[], stale=None)
    _, _, raw_key, field, daily = FEEDS[feed]
    if raw_key:
        rows = normalize({raw_key: value}, start, today)["history"]
        pts = [(r["date"], r.get(field)) for r in rows]
        if feed == "stress":  # Garmin uses negative levels for "not enough data"
            pts = [(d, v if v is None or v >= 0 else None) for d, v in pts]
        have = [d for d, v in pts if v is not None]
        found["latest"] = have[-1] if have else None
        found["anomalies"] = anomalies(feed, pts, today)
        if daily and not have:
            found["anomalies"].insert(0, f"Nothing came back for {start} → {today}")
        found["stale"] = stale(feed, found["latest"], today)
    elif feed == "weight":
        import body  # local import: body imports garmin_client too
        rows = body.weight_series(value)
        found["latest"] = rows[-1]["date"] if rows else None
        found["anomalies"] = anomalies("weight", [(r["date"], r["kg"]) for r in rows], today)
        found["stale"] = stale("weight", found["latest"], today, [r["date"] for r in rows])
    elif feed == "activities":
        acts = value if isinstance(value, list) else first_list_of_dicts(value)
        found["latest"] = max((str(a.get("startTimeLocal", ""))[:10] for a in acts), default=None) or None
    elif feed == "fitness_age":
        fa = value.get("fitnessAge") if isinstance(value, dict) else None
        found["latest"] = (str(value.get("lastUpdated") or "")[:10] or None) if isinstance(value, dict) else None
        if fa is None:
            found["anomalies"].append("Garmin returned no fitness age")
        elif not 10 <= fa <= 100:
            found["anomalies"].append(f"Fitness age {fa:g} looks wrong")
    elif feed == "profile":
        if not value:
            found["anomalies"].append("Empty profile response")
    return found


def probe(get, feed: str, today: date) -> None:
    """One cheap request for `feed`; the call itself is recorded by the client hook, this adds the evaluation."""
    start = today - timedelta(days=6)
    s, e = start.isoformat(), today.isoformat()
    paths = {
        "hrv": (f"/hrv-service/hrv/daily/{s}/{e}", None),
        "sleep": ("/sleep-service/sleep/dailySleepSummariesByDate", {"startDate": s, "endDate": e}),
        "body_battery": ("/wellness-service/wellness/bodyBattery/reports/daily", {"startDate": s, "endDate": e}),
        "stress": (f"/usersummary-service/stats/stress/daily/{s}/{e}", None),
        "steps": (f"/usersummary-service/stats/steps/daily/{s}/{e}", None),
        "activities": ("/activitylist-service/activities/search/activities", {"start": 0, "limit": 20}),
        "weight": (f"/weight-service/weight/range/{today - timedelta(days=89)}/{e}", None),
        "fitness_age": (f"/fitnessage-service/fitnessage/v2/{e}", None),
        "profile": ("/userprofile-service/userprofile/settings", None),
    }
    path, params = paths[feed]
    try:
        value = get(path, params)
    except Exception:  # already recorded (status, error) by the hook
        with _lock:
            f = _state["feeds"][feed]
            f.update(probed=time.time(), range=describe_range(path, params), ms=(f["call"] or {}).get("ms"))
        return
    found = evaluate(feed, value, today, start if feed != "weight" else today - timedelta(days=89))
    with _lock:
        f = _state["feeds"][feed]
        call = f["call"] or {}
        f.update(latest=found["latest"], anomalies=found["anomalies"], stale=found["stale"], probed=time.time(),
                 records=found["records"], range=describe_range(path, params), ms=call.get("ms"))
    for msg in ([found["stale"]] if found["stale"] else []) + found["anomalies"][:2]:
        event("warning", FEEDS[feed][0], msg)


# --------------------------------------------------------------------------- AI test
def test_ai(ping, classify, key_set: bool, model: str) -> dict:
    """The smallest real request (via ai.ping, the SDK): confirms the key works and the model answers.
    Only called on demand."""
    res = dict(tested=time.time(), ok=False, ms=None, model=model, error=None)
    if not key_set:
        res["error"] = dict(kind="no_key", title="No Anthropic API key",
                            explain="ANTHROPIC_API_KEY isn't set, so the coach uses its built-in rules instead of Claude.",
                            fix="Create a key at console.anthropic.com, put it in .env as ANTHROPIC_API_KEY and restart.")
    else:
        t0 = time.time()
        try:
            r = ping()
            res.update(ok=True, ms=r.get("ms"), model=r.get("model") or model)
        except Exception as ex:
            res["ms"] = round((time.time() - t0) * 1000)
            info = classify(ex)
            res["error"] = dict(info, status=getattr(ex, "status_code", None), detail=redact_text(type(ex).__name__))
    with _lock:
        _state["ai"] = res
    if res["ok"]:
        event("info", "AI coach", f"Test passed: {res['model']} answered in {res['ms']} ms")
    else:
        event("error" if key_set else "info", "AI coach", f"Test: {res['error']['title']}")
    return res


# --------------------------------------------------------------------------- local system
def local_checks(data_dir: Path, db_path: Path, db_connect, export_dirs: list[Path], host: str, port: int) -> dict:
    out = dict(server=dict(ok=True, listen=f"{host}:{port}", pid=os.getpid(), uptime_s=round(time.time() - STARTED)),
               version=APP_VERSION)
    try:
        con = db_connect()
        con.execute("select 1").fetchone()
        con.close()
        out["db"] = dict(ok=True, size=db_path.stat().st_size if db_path.exists() else 0)
    except Exception as ex:
        out["db"] = dict(ok=False, error=f"{type(ex).__name__}: {redact_text(str(ex))[:160]}")
    try:
        du = shutil.disk_usage(data_dir)
        out["disk"] = dict(ok=du.free > 1_000_000_000, free=du.free, total=du.total)
    except OSError as ex:
        out["disk"] = dict(ok=False, error=type(ex).__name__)
    zips = [p for d in export_dirs if d.is_dir() for p in d.glob("*-coach-export-*.zip")]  # readiness-coach- (and garmin-coach- from older versions)
    newest = max(zips, key=lambda p: p.stat().st_mtime, default=None)
    out["backup"] = dict(last=newest.stat().st_mtime if newest else None)
    with _lock:
        _state["local"] = out
    return out


# --------------------------------------------------------------------------- views
def feed_status(f: dict) -> str:
    if f["last_attempt"] is None:
        return "unknown"
    if f["error"] and (f["last_ok"] is None or f["error"]["t"] >= f["last_ok"]):
        return "error"
    if f["anomalies"] or f["stale"] or (f["error"] and f["error"].get("status") == 429):
        return "warn"
    return "ok"


def view(key_set: bool, demo: bool, token_expires: float | None, ai_key_set: bool) -> dict:
    """Everything the panel shows, with an overall colour and one-line summary."""
    with _lock:
        s = json.loads(json.dumps(_state))
    feeds = []
    for f in s["feeds"].values():
        f["status"] = feed_status(f)
        if f["failures"] >= REPEATED:
            f["anomalies"] = [f"Failed {f['failures']} times in a row"] + f["anomalies"]
        feeds.append(f)
    tok = s["token"]
    garmin = dict(key_set=key_set, demo=demo, token_ok=bool(tok["last_ok"]) and not tok["error"],
                  last_ok=tok["last_ok"], last_attempt=tok["last_attempt"], ms=tok["ms"], expires_at=token_expires,
                  error=no_key() if not key_set else tok["error"])
    garmin["status"] = ("warn" if demo else "error" if garmin["error"] else "ok" if garmin["token_ok"] else "unknown")
    ai = dict(s["ai"], key_set=ai_key_set, model=s["ai"]["model"] or os.getenv("COACH_MODEL", DEFAULT_MODEL))
    ai["status"] = ("off" if not ai_key_set else "unknown" if not ai["tested"] else "ok" if ai["ok"] else "error")
    color, summary = overall(garmin, feeds if not demo else [], ai, s["local"])
    return dict(overall=color, summary=summary, checked=s["checked"], light_checked=s["light_checked"], garmin=garmin,
                feeds=feeds, ai=ai, local=s["local"], version=APP_VERSION, events=events())


def overall(garmin: dict, feeds: list[dict], ai: dict, local: dict | None) -> tuple[str, str]:
    if garmin.get("demo"):
        return "amber", "Demo data - no Garmin key"
    if garmin["status"] == "error":
        return "red", garmin["error"]["title"]
    failing = [f for f in feeds if f["status"] == "error"]
    if any(f["failures"] >= REPEATED for f in failing):
        return "red", f"{failing[0]['label']} keeps failing"
    if local and not (local.get("db") or {}).get("ok", True):
        return "red", "The local database isn't reachable"
    warn = [f for f in feeds if f["status"] == "warn"]
    if failing or warn:
        n = len(failing) + len(warn)
        first = (failing or warn)[0]["label"]
        return "amber", f"{first} needs attention" if n == 1 else f"{n} data feeds need attention"
    if ai["status"] == "error":
        return "amber", "AI coach test failed"
    if local and not (local.get("disk") or {}).get("ok", True):
        return "amber", "Disk space is low"
    return "green", "All systems normal"


def mark_checked(light: bool) -> None:
    with _lock:
        _state["light_checked" if light else "checked"] = time.time()
        if not light:
            _state["light_checked"] = _state["checked"]


# --------------------------------------------------------------------------- redaction + report
SENSITIVE_KEYS = re.compile(r"(?i)^(display_?name|full_?name|user_?name|first_?name|last_?name|email|birth_?date|"
                            r"date_?of_?birth|dob|.*latitude|.*longitude|lat|lon|lng|polyline|gps.*|address|"
                            r"profile_?image.*|token|access_?token|api_?key)$")
JWT_RE = re.compile(r"\beyJ[\w-]{6,}\.[\w-]{6,}\.[\w-]{6,}")
EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
JSON_PII_RE = re.compile(r'(?i)"(displayName|fullName|userName|firstName|lastName|email|birthDate|dateOfBirth|'
                         r'profileImageUrl\w*)"\s*:\s*"[^"]*"')
JSON_GEO_RE = re.compile(r'(?i)"(\w*latitude|\w*longitude|lat|lon|lng)"\s*:\s*-?\d+(\.\d+)?')
COORD_RE = re.compile(r"-?\b\d{1,3}\.\d{4,}\s*,\s*-?\d{1,3}\.\d{4,}\b")
BIRTH_RE = re.compile(r"(?i)\b(birth\s*date|date of birth|dob|born)(\W{0,4})(\d{4}-\d{2}-\d{2}|\d{1,2}/\d{1,2}/\d{2,4})")
SECRET_RE = re.compile(r"\b(sk-ant-[\w-]{8,})")


def redact_text(text: str) -> str:
    text = applog.redact(str(text))
    text = SECRET_RE.sub("[key redacted]", text)
    text = JWT_RE.sub("[token redacted]", text)
    text = JSON_PII_RE.sub(lambda m: f'"{m.group(1)}": "[redacted]"', text)
    text = JSON_GEO_RE.sub(lambda m: f'"{m.group(1)}": "[redacted]"', text)
    text = COORD_RE.sub("[coordinates redacted]", text)
    text = BIRTH_RE.sub(lambda m: f"{m.group(1)}{m.group(2)}[redacted]", text)
    return EMAIL_RE.sub("[email redacted]", text)


def scrub(obj):
    """Drop personally identifying / secret keys anywhere in a structure and redact the strings."""
    if isinstance(obj, dict):
        return {k: scrub(v) for k, v in obj.items() if not SENSITIVE_KEYS.match(str(k))}
    if isinstance(obj, list):
        return [scrub(x) for x in obj]
    return redact_text(obj) if isinstance(obj, str) else obj


def _ts(t) -> str:
    return datetime.fromtimestamp(t).strftime("%Y-%m-%d %H:%M:%S") if t else "never"


def report(v: dict) -> str:
    """Plain-text diagnostics for pasting into a help request; everything passes through scrub/redact_text."""
    v = scrub(v)
    g, ai, loc = v["garmin"], v["ai"], v.get("local") or {}
    L = [f"Readiness Coach for Garmin diagnostics - {_ts(time.time())}", f"App version {v['version']} · overall: {v['overall'].upper()} "
         f"({v['summary']})", f"Last full check: {_ts(v['checked'])} · last background check: {_ts(v['light_checked'])}", "",
         "== Garmin connection ==", f"API key set: {'yes' if g['key_set'] else 'no'}{' (demo data)' if g['demo'] else ''}",
         f"Token exchange: {g['status']} · last OK {_ts(g['last_ok'])} · expires {_ts(g['expires_at'])}"]
    if g.get("error"):
        e = g["error"]
        L.append(f"Error: {e['title']} ({e.get('status') or '-'}) - {e['explain']}")
    L += ["", "== Data feeds =="]
    for f in v["feeds"]:
        L.append(f"- {f['label']}: {f['status']} · last OK {_ts(f['last_ok'])} · records {f['records'] if f['records'] is not None else '-'}"
                 f" · range {f['range'] or '-'} · {f['ms'] if f['ms'] is not None else '-'} ms · latest data {f['latest'] or '-'}")
        if f.get("error"):
            e = f["error"]
            L.append(f"    error: {e.get('status') or '-'} {e['kind']} at {_ts(e.get('t'))} - {e.get('detail') or e['title']}")
        for a in f["anomalies"] + ([f["stale"]] if f["stale"] else []):
            L.append(f"    note: {a}")
        if f.get("call"):
            c = f["call"]
            L.append(f"    latest app request: {_ts(c['t'])} · range {c['range'] or '-'} · status {c['status'] or '-'} · {c['ms']} ms")
        if f["retries"]:
            L.append(f"    last call retried after: {', '.join(f['retries'])}")
    L += ["", "== AI coach ==", f"API key set: {'yes' if ai['key_set'] else 'no'} · model {ai['model']}",
          f"Last test: {_ts(ai['tested'])}" + (f" · {'OK' if ai['ok'] else 'failed'} · {ai['ms']} ms" if ai['tested'] else "")]
    if ai.get("error"):
        L.append(f"Error: {ai['error']['title']} ({ai['error'].get('status') or '-'})")
    L += ["", "== This computer =="]
    if loc:
        db, disk = loc.get("db") or {}, loc.get("disk") or {}
        L += [f"Server: {loc['server']['listen']} · up {loc['server']['uptime_s']} s",
              f"Database: {'reachable' if db.get('ok') else 'NOT reachable ' + str(db.get('error'))} · {db.get('size', 0) / 1e6:.1f} MB",
              f"Disk free: {disk.get('free', 0) / 1e9:.1f} GB" if disk.get("free") is not None else f"Disk: {disk.get('error')}",
              f"Last backup: {_ts((loc.get('backup') or {}).get('last'))}"]
    else:
        L.append("(not checked yet)")
    L += ["", "== Recent events (newest first) =="]
    for e in v["events"]:
        L.append(f"{_ts(e['t'])} {e['level'].upper():7} {e['source']}: {e['message']}" + (f" (x{e['count']})" if e.get("count") else ""))
    return redact_text("\n".join(L))
