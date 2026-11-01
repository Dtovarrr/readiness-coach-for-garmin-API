"""Local FastAPI backend. Binds to 127.0.0.1 only."""
import hashlib
import uuid
import re
import zipfile
import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Literal

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, StreamingResponse
from pydantic import BaseModel, Field

load_dotenv(Path(__file__).parent / ".env")

import applog  # noqa: E402
import activity  # noqa: E402
import ai  # noqa: E402
import body  # noqa: E402
import coach  # noqa: E402
import coach_tools  # noqa: E402
import coach_actions  # noqa: E402
import coach_trace  # noqa: E402
import context_ext  # noqa: E402
import proactive  # noqa: E402
import goals  # noqa: E402
import health  # noqa: E402
import units  # noqa: E402
import calendar_data  # noqa: E402
import charts  # noqa: E402
import correlate  # noqa: E402
import dexa  # noqa: E402
import dexa_parse  # noqa: E402
import checkins  # noqa: E402
import photos  # noqa: E402
import plans  # noqa: E402
import intervals  # noqa: E402
import workout_export  # noqa: E402
import workouts  # noqa: E402
import records  # noqa: E402
import records_sync  # noqa: E402
import review  # noqa: E402
import store  # noqa: E402
import strength  # noqa: E402
import strength_lib  # noqa: E402

store.db().close()  # open the local DB now (first run migrates goals.json/settings.json, verified)
from demo import demo_snapshot  # noqa: E402
from demo_history import demo_rows  # noqa: E402
from garmin_client import GarminClient, GarminError  # noqa: E402

log = applog.setup()
app = FastAPI(title="Readiness Coach for Garmin")

# ------------------------------ local-only guard ------------------------------ #
# The server binds to 127.0.0.1, but a web page in any browser on this machine can still send it
# requests (and DNS rebinding can make a remote name resolve here). So every request must name this
# server in its Host header, and anything that changes state must come from the app's own page
# (Origin, or Referer when a client leaves Origin out). No CORS headers are ever sent, so other
# sites can't read responses either.
PORT = int(os.getenv("COACH_PORT") or 8765)
LOCAL_URL = f"http://127.0.0.1:{PORT}"
SAFE_METHODS = {"GET", "HEAD"}


def allowed_hosts() -> set[str]:
    return {f"127.0.0.1:{PORT}", f"localhost:{PORT}", f"[::1]:{PORT}"}


def _origin_of(url: str) -> str | None:
    from urllib.parse import urlsplit
    try:
        u = urlsplit(url)
    except ValueError:
        return None
    return f"{u.scheme}://{u.netloc}".lower() if u.scheme and u.netloc else None


def request_refusal(method: str, headers: dict[str, str]) -> str | None:
    """Why a request must be refused, or None. `headers` has lower-case names."""
    if headers.get("host", "").lower() not in allowed_hosts():
        return "This server only answers requests addressed to 127.0.0.1 or localhost on its own port."
    if method.upper() in SAFE_METHODS:
        return None
    allowed = {f"http://{h}" for h in allowed_hosts()}
    origin = headers.get("origin")
    if origin is not None:
        return None if origin.lower() in allowed else "Changes are only accepted from the app's own page (Origin mismatch)."
    referer = headers.get("referer")
    if referer and _origin_of(referer) in allowed:
        return None
    return "Changes are only accepted from the app's own page (no Origin or Referer)."


class LocalOnly:
    """Pure ASGI middleware (it doesn't buffer streamed responses) applying request_refusal()."""

    def __init__(self, inner):
        self.inner = inner

    async def __call__(self, scope, receive, send):
        if scope["type"] in ("http", "websocket"):
            headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])}
            why = request_refusal(scope.get("method", "GET"), headers)
            if why:
                log.warning("refused %s %s: %s", scope.get("method", "WS"), scope.get("path"), why)
                if scope["type"] == "websocket":
                    await send({"type": "websocket.close", "code": 1008})
                    return
                await JSONResponse(status_code=403, content=dict(detail=why))(scope, receive, send)
                return
        await self.inner(scope, receive, send)


app.add_middleware(LocalOnly)


def where(ex: BaseException) -> str:
    """The innermost line of this project's code in a traceback, e.g. "records.py:144"."""
    tb, here = ex.__traceback__, None
    while tb:
        f = tb.tb_frame.f_code.co_filename
        if f.startswith(str(Path(__file__).parent)) and "/.venv/" not in f:
            here = f"{Path(f).name}:{tb.tb_lineno}"
        tb = tb.tb_next
    return here or "unknown"


@app.exception_handler(Exception)
async def unexpected_error(request, ex: Exception):
    """Anything unhandled: the traceback goes to the log (secrets redacted) and the page gets a
    message it can show, instead of a bare "Internal Server Error"."""
    log.error("%s %s failed", request.method, request.url.path, exc_info=ex)
    return JSONResponse(status_code=500, content=dict(
        detail=f"Something went wrong on this computer ({type(ex).__name__} at {where(ex)}). The details are in logs/readiness_coach.log.",
        error=type(ex).__name__, where=where(ex)))
STATIC = Path(__file__).parent / "static"
CACHE_TTL = 600
_cache: dict = {"t": 0, "snap": None, "assess": None, "brief": None}
_client: GarminClient | None = None
CACHE = Path(os.getenv("COACH_CACHE_DIR") or Path(__file__).parent / ".cache")  # parsed activities + analyses (gitignored, local only)
HISTORY_DAYS = 90
health.init(CACHE)
_history: dict = {"t": 0, "rows": None}
_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


def demo_mode() -> bool:
    return os.getenv("GARMIN_DEMO") == "1" or not os.getenv("GARMIN_API_KEY")


def _new_client() -> GarminClient:
    c = GarminClient()
    c.on_call = health.record_call  # every request feeds the System status panel
    return c


def get_data(refresh=False):
    global _client
    if refresh or time.time() - _cache["t"] > CACHE_TTL or _cache["snap"] is None:
        if demo_mode():
            snap = demo_snapshot()
        else:
            try:
                _client = _client or _new_client()
                snap = _client.snapshot()
            except GarminError as ex:
                raise HTTPException(502, str(ex))
        _cache.update(t=time.time(), snap=snap, assess=coach.assess(snap), brief=None)
    return _cache["snap"], _cache["assess"]


@app.get("/api/dashboard")
def dashboard(refresh: bool = False):
    snap, a = get_data(refresh)
    return dict(snapshot=snap, assessment=a, llm=coach.llm_enabled(), demo=snap["source"] == "demo")


@app.get("/api/coach")
def coach_brief(refresh: bool = False):
    """Today's briefing. Cached by a hash of what it depends on (the day's computed data, goals, DEXA,
    shared check-ins, your profile + notes, units), so unchanged data never calls Claude again.
    Opening the page is an automatic call (paused by the spending cap); Regenerate always runs."""
    snap, a = get_data()
    if not refresh and _cache["brief"] is not None:
        return _cache["brief"]
    with _lock("briefing"):
        prof, system = dict(coach_profile(), plan_today=bool(_safe(lambda: _today_bundle()["today"], []))), units_system()
        key = ai.cache_key("briefing", coach.briefing_inputs(snap, a, prof), coach.memory_hash(), [system, checkins.sharing()])
        res = ai.cached("briefing", key, lambda: coach.briefing(snap, a, prof, system, coach_tools.Toolbox(coach_sources()),
                                                                essential=refresh), refresh)
        _cache["brief"] = res
        return res


class Q(BaseModel):
    question: str = Field(..., min_length=1, max_length=600)


@app.post("/api/coach/ask")
def coach_ask(q: Q):
    """Chat with tools. `remember` lists facts the coach suggests saving; nothing is stored until you confirm."""
    snap, a = get_data()
    try:
        return coach.ask(q.question, snap, a, coach_profile(), units_system(), coach_tools.Toolbox(coach_sources()))
    except ai.AIError as ex:
        raise HTTPException(502, f"{ex.info['title']}. {ex.info.get('fix') or ''}".strip())


def _lock(key: str) -> threading.Lock:
    with _locks_guard:
        return _locks.setdefault(key, threading.Lock())


def _cache_get(kind: str, aid: int | str):
    try:
        return json.loads((CACHE / kind / f"{aid}.json").read_text())
    except (OSError, ValueError):
        return None


def _cache_put(kind: str, aid: int | str, obj) -> None:
    path = CACHE / kind / f"{aid}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(obj, default=str))
    tmp.replace(path)


def client() -> GarminClient:
    global _client
    if demo_mode():
        raise HTTPException(404, "This needs a Garmin key (not available with demo data).")
    _client = _client or _new_client()
    return _client


def activity_view(aid: int, refresh: bool = False) -> dict:
    with _lock(f"view:{aid}"):
        view = None if refresh else _cache_get("activity", aid)
        if view and view.get("v") == activity.VERSION:
            return view
        c = client()
        try:
            det = c.get(f"/activity-service/activity/{aid}")
        except GarminError as ex:
            raise HTTPException(502, str(ex))
        try:
            blob = c.download(f"/download-service/files/activity/{aid}")
        except GarminError:
            blob = None
        view = activity.build(det, blob)
        if not view.get("note"):  # don't pin a partial result; retry the file next time
            _cache_put("activity", aid, view)
        return view


def history_rows() -> list[dict]:
    with _lock("history"):
        if _history["rows"] is None or time.time() - _history["t"] > CACHE_TTL:
            try:
                _history.update(t=time.time(), rows=[activity.history_row(a) for a in client().activities_since(HISTORY_DAYS)])
            except GarminError as ex:
                raise HTTPException(502, str(ex))
        return _history["rows"]


@app.get("/api/activities")
def activities_list():
    """The last HISTORY_DAYS of activities for the Activities view (metric; the page formats)."""
    keys = ("id", "name", "type", "start", "date", "dist_m", "secs", "speed_ms", "hr", "load", "te", "gain_m")
    if demo_mode():  # the same synthetic days as the calendar, charts and records (stable ids)
        rows = [dict({k: a.get(k) for k in keys}, date=d, speed_ms=a["dist_m"] / a["secs"] if a["dist_m"] and a["secs"] else None)
                for d, r in demo_rows(date.today() - timedelta(days=HISTORY_DAYS), date.today()).items() for a in r["activities"]]
    else:
        rows = [{k: r.get(k) for k in keys} for r in history_rows()]
    return dict(days=HISTORY_DAYS, activities=sorted(rows, key=lambda r: r["start"], reverse=True))


@app.get("/api/activity/{aid}")
def activity_detail(aid: int, refresh: bool = False):
    return activity_view(aid, refresh)


@app.get("/api/activity/{aid}/analysis")
def activity_analysis(aid: int, refresh: bool = False):
    """Computed comparison stats + (if ANTHROPIC_API_KEY is set) a Claude write-up, cached per activity."""
    with _lock(f"analysis:{aid}"):
        hit = None if refresh else _cache_get("analysis", aid)
        system = units_system()
        if hit and (hit.get("text") or not coach.llm_enabled()) and hit.get("units") == system:
            return hit  # written in other units (or before units existed) -> regenerated below
        stats = _activity_stats(aid, system)
        res = dict(stats=stats, source="stats", units=system, generated=time.strftime("%Y-%m-%d %H:%M"))
        if coach.llm_enabled():
            try:
                key = ai.cache_key("activity", stats, coach.memory_hash(), system)
                out = ai.cached("activity", key, lambda: coach.analyze_activity(stats, system), refresh)
                res.update(text=out["text"], data=out["data"], source="claude")
            except ai.AIError as ex:
                res["note"] = f"{ex.info['title']}; showing the computed comparison only."
                return res  # not cached, so the next open retries
        else:
            res["note"] = "Add ANTHROPIC_API_KEY to .env for a written analysis; computed comparison shown below."
        _cache_put("analysis", aid, res)
        return res


def _activity_stats(aid: int, system: str) -> dict:
    """Derived, location-free stats for one activity (and its comparison with similar ones): the
    analysis payload and the coach's activity tools both use this."""
    view = activity_view(aid)
    rows = history_rows()
    cur = next((r for r in rows if r["id"] == aid), None)
    if cur is None:  # older than the history window: build the same row shape from the detail summary
        sm = view.get("summary", {})
        cur = activity.history_row(dict(
            activityId=aid, startTimeLocal=str(view.get("start") or "").replace("T", " "), activityType=view["type"],
            distance=sm.get("distance"), movingDuration=sm.get("moving"), duration=sm.get("duration"),
            averageSpeed=sm.get("avg_speed"), averageHR=sm.get("avg_hr"), activityTrainingLoad=sm.get("load"),
            aerobicTrainingEffect=sm.get("te"), elevationGain=sm.get("elev_gain"), totalSets=sm.get("sets")))
    snap, a = get_data()
    return activity.coach_stats(view, cur, activity.compare(cur, rows, HISTORY_DAYS), snap["history"], a, system)


# ------------------------------ coach: tools + memory ------------------------------ #
# The coach's tools read the same cached data the pages use (nothing new is fetched for the model),
# and everything they return passes through privacy.scrub. The profile and notes are yours: stored
# here, sent only as part of the coach's instructions, and listed/editable in Settings.
def _safe(fn, default=None):
    try:
        return fn()
    except (HTTPException, GarminError, KeyError, ValueError, OSError):
        return default


def _records_summary() -> dict | None:
    data = _safe(records_clean)
    if not data:
        return None
    recs = records.compute(data[0], data[1], date.today())
    out = []
    for group in ("running", "activity", "weekly"):
        for r in (recs.get(group) or [])[:8]:
            item = dict(label=r["label"], date=r["date"], improved_pct=(r.get("improvement") or {}).get("pct"))
            v = r["value"]
            if r["unit"] == "s":
                item["time"] = units.fmt_mmss(v)
            elif r["unit"] == "m":
                item["dist_km"] = round(v / 1000, 2)
            elif r["unit"] == "s_per_m":
                item["pace_s_per_km"] = round(v * 1000)
            else:
                item[r["unit"] if r["unit"] in ("bpm", "load") else "value"] = v
            out.append(item)
    recent = [x for x in out if x["date"] >= (date.today() - timedelta(days=30)).isoformat()]
    return dict(records=out, new_in_last_30_days=len(recent))


def _strength_rows(ex: str, a: date, b: date) -> list[dict]:
    rows = []
    for s in sorted(store.strength_sessions(), key=lambda x: x["day"]):
        if not a.isoformat() <= s["day"] <= b.isoformat():
            continue
        for e in s.get("exercises") or []:
            name = strength_lib.lookup(e.get("exercise_id"), st_custom()) or {}
            label = (name.get("name") or e.get("exercise_id") or "").lower()
            if ex != "all" and label != ex and (e.get("exercise_id") or "").lower() != ex:
                continue
            sets = strength.done_sets(e)
            if not sets:
                continue
            top = max(sets, key=lambda z: ((z.get("kg") or 0), z["reps"]))
            best = max((strength.e1rm(z.get("kg"), z["reps"]) or 0 for z in sets), default=0) or None
            rows.append(dict(date=s["day"], exercise=name.get("name") or e.get("exercise_id"), sets=len(sets),
                             top_set_kg=top.get("kg") or 0, top_reps=top["reps"], e1rm_kg=round(best, 1) if best else None,
                             volume_kg=round(sum(z["reps"] * (z.get("kg") or 0) for z in sets))))
    return rows


def _exercise_names() -> list[str]:
    seen = []
    for s in store.strength_sessions():
        for e in s.get("exercises") or []:
            n = (strength_lib.lookup(e.get("exercise_id"), st_custom()) or {}).get("name") or e.get("exercise_id")
            if n and n not in seen:
                seen.append(n)
    return seen


def _upcoming_plans() -> list:
    today = date.today().isoformat()
    return [(e, plan_view(e["id"])) for e in store.events() if e["date"] >= today and store.plan_get(e["id"])]


def coach_sources(chat: bool = False) -> coach_tools.Sources:
    ctx = store.settings()
    return coach_tools.Sources(
        feeds=_feed_problems,
        weather=(lambda a, b: context_ext.forecast(a, b, units_system())) if ctx.get("ctx_weather_on") and ctx.get("ctx_weather_city") else None,
        calendar=context_ext.calendar if ctx.get("ctx_calendar_on") else None,
        handlers=coach_handlers() if chat else None,
        today=date.today,
        day_rows=lambda a, b: _chart_rows(a, b, False)[0],
        assessment=_assessment,
        activity_stats=lambda aid: _safe(lambda: _activity_stats(int(aid), units_system())),
        body=lambda: _safe(body_data),
        dexa=lambda: dexa.brief_payload(store.dexa_scans()),
        goals=lambda: _safe(lambda: goals_view()["goals"], []),
        plans=_upcoming_plans,
        strength=_strength_rows,
        exercises=_exercise_names,
        checkins=lambda a, b: store.checkins(a.isoformat(), b.isoformat()) if checkins.sharing() else [],
        sharing=checkins.sharing,
        records=_records_summary,
        profile=lambda: {k: v for k, v in store.coach_profile().items() if k in coach.PROFILE_FIELDS or k in coach.PREFERENCES},
        system=units_system,
    )


class CoachProfileIn(BaseModel):
    injuries: str = Field("", max_length=600)
    sports: str = Field("", max_length=400)
    equipment: str = Field("", max_length=300)
    schedule: str = Field("", max_length=400)
    goals_text: str = Field("", max_length=600)
    tone: Literal["friendly", "direct", "encouraging", ""] = ""
    brevity: Literal["short", "normal", "detailed", ""] = ""


class CoachNoteIn(BaseModel):
    text: str = Field(..., min_length=3, max_length=200)
    source: Literal["chat", "manual"] = "chat"


MAX_NOTES = 40


def _coach_memory_view() -> dict:
    return dict(profile=store.coach_profile(), notes=store.coach_notes(), max_notes=MAX_NOTES,
                prompt_preview=coach.memory_block())


def _memory_changed() -> None:
    _cache["brief"] = None  # the briefing is re-keyed by the new memory; drop the in-memory copy


@app.get("/api/coach/profile")
def get_coach_profile():
    return _coach_memory_view()


@app.put("/api/coach/profile")
def put_coach_profile(body_: CoachProfileIn):
    store.coach_profile_put({k: v.strip() for k, v in body_.model_dump().items()})
    _memory_changed()
    return _coach_memory_view()


@app.post("/api/coach/notes")
def add_coach_note(body_: CoachNoteIn):
    """Saves a note only when you confirm it ("Remember this?" or typed in Settings)."""
    text = " ".join(body_.text.split())
    notes = store.coach_notes()
    if any(n["text"].lower() == text.lower() for n in notes):
        return _coach_memory_view()
    if len(notes) >= MAX_NOTES:
        raise HTTPException(409, f"The coach keeps up to {MAX_NOTES} notes. Delete one first.")
    store.coach_note_put(dict(id=uuid.uuid4().hex[:10], text=text, source=body_.source))
    health.event("info", "AI coach", "Saved a coach note")
    _memory_changed()
    return _coach_memory_view()


@app.put("/api/coach/notes/{nid}")
def edit_coach_note(nid: str, body_: CoachNoteIn):
    if not any(n["id"] == nid for n in store.coach_notes()):
        raise HTTPException(404, "Note not found.")
    store.coach_note_put(dict(id=nid, text=" ".join(body_.text.split())))
    _memory_changed()
    return _coach_memory_view()


@app.delete("/api/coach/notes/{nid}")
def delete_coach_note(nid: str):
    if not store.coach_note_delete(nid):
        raise HTTPException(404, "Note not found.")
    _memory_changed()
    return _coach_memory_view()


@app.post("/api/coach/forget")
def forget_coach_memory(confirm: bool = False):
    """Clears About me and every coach note (needs confirm=true)."""
    if not confirm:
        raise HTTPException(422, "Confirm to clear everything the coach knows about you.")
    store.coach_forget()
    health.event("info", "AI coach", "Cleared the coach profile and notes")
    _memory_changed()
    return _coach_memory_view()


@app.get("/api/ai/usage")
def ai_usage():
    return ai.usage_summary()


# ------------------------- coach: proposals, threads, feedback, notices ------------------------- #
# Proposals are applied only from these routes (a click), through the same handlers the pages use.
def _plan_session(eid: str, wid: str) -> dict | None:
    e, p = store.event_get(eid), store.plan_get(eid)
    w = next((x for x in (p or {}).get("workouts", []) if x["id"] == wid), None) if e else None
    return dict(w, plan_start=p["weeks"][0]["start"] if p["weeks"] else w["date"], event_date=e["date"]) if w else None


def _active_lift(name: str) -> dict | None:
    prog = next((p for p in store.strength_programs() if p.get("active")), None)
    if not prog:
        return None
    for ex_id in (prog.get("lifts") or {}):
        ex = strength_lib.lookup(ex_id, st_custom()) or {}
        if name.strip().lower() in (ex_id.lower(), (ex.get("name") or "").lower()):
            return dict(program_id=prog["id"], exercise_id=ex_id, name=ex.get("name") or ex_id)
    return None


_SESSION_FIELDS = ("date", "type", "title", "intensity", "distance_m", "minutes", "status")


def _session_state(pp: dict, _r=None):
    w = _plan_session(pp["event_id"], pp["workout_id"]) or {}
    return {k: w.get(k) for k in _SESSION_FIELDS}


def _apply_swap(pp):
    before = _session_state(pp)
    w = _plan_session(pp["event_id"], pp["workout_id"])
    ch = dict(status="skipped") if pp["to"] == "rest" else plans.easier_changes(w)
    patch_workout(pp["event_id"], pp["workout_id"], WorkoutPatch(**ch))
    return dict(link=f"#training/{pp['event_id']}"), before


def _apply_move(pp):
    before = _session_state(pp)
    patch_workout(pp["event_id"], pp["workout_id"], WorkoutPatch(date=date.fromisoformat(pp["to_date"])))
    return dict(link=f"#training/{pp['event_id']}"), before


def _undo_session(pp, before, _r):
    ch = {k: v for k, v in before.items() if k in WorkoutPatch.model_fields and v is not None}
    patch_workout(pp["event_id"], pp["workout_id"], WorkoutPatch(**{**ch, "date": date.fromisoformat(ch["date"])} if "date" in ch else ch))


def _apply_goal(pp):
    before = {g["id"] for g in goals.list_goals()}
    tgt = pp.get("target")
    if tgt is not None and pp["type"] in goals.TYPES:
        tgt = units.goal_from_display(pp["type"], tgt, units_system())  # the coach speaks display units
    create_goal(GoalIn(type=pp["type"], title=pp["title"], target=tgt, deadline=date.fromisoformat(pp["deadline"])))
    new = next((g["id"] for g in goals.list_goals() if g["id"] not in before), None)
    return dict(goal_id=new, link="#goals"), None


def _undo_goal(pp, _u, r):
    if r and r.get("goal_id"):
        delete_goal(r["goal_id"])


def _apply_workout(pp):
    if pp["template"] == "plan":
        e, plan = _event(pp["event_id"]), _plan(pp["event_id"])
        pw = next(x for x in plan["workouts"] if x["id"] == pp["workout_id"])
        w = workouts.from_plan(pw, e)
    else:
        w = dict(workouts.from_template(pp["template"]), scheduled=pp["day"])
    w = workouts.validate(dict(w, source=dict(suggested=date.today().isoformat(), coach=True)))
    store.workout_put(w)
    return dict(workout_id=w["id"], link=f"#training/workouts/{w['id']}"), None


def _undo_workout(pp, _u, r):
    if r and r.get("workout_id"):
        store.workout_delete(r["workout_id"])


def _apply_checkin_note(pp):
    prev = (store.checkin_get(pp["day"]) or {}).get("note") or ""
    note = f"{prev} · {pp['text']}" if prev else pp["text"]
    put_checkin(pp["day"], CheckinIn(note=note[:1000]))
    return dict(link=f"#calendar/{pp['day'][:7]}"), dict(note=prev)


def _undo_checkin_note(pp, before, _r):
    put_checkin(pp["day"], CheckinIn(note=before.get("note") or ""))


def _apply_deload(pp):
    strength_program_adjust(pp["program_id"], StAdjust(exercise_id=pp["exercise_id"], kind="deload", note="coach suggestion"))
    adj = (store.strength_program_get(pp["program_id"]).get("adjustments") or [])[-1]
    return dict(adjustment_id=adj["id"], link="#training/programs"), dict(adjustment_id=adj["id"])


def _undo_deload(pp, u, _r):
    p = store.strength_program_get(pp["program_id"])
    p["adjustments"] = [a for a in p.get("adjustments") or [] if a["id"] != u["adjustment_id"]]
    store.strength_program_put(p)


def _apply_remember(pp):
    add_coach_note(CoachNoteIn(text=pp["text"], source="chat"))
    nid = next((n["id"] for n in store.coach_notes() if n["text"].lower() == " ".join(pp["text"].split()).lower()), None)
    return dict(note_id=nid, link="#settings"), None


def _undo_remember(pp, _u, r):
    if r and r.get("note_id"):
        delete_coach_note(r["note_id"])


def coach_handlers() -> coach_actions.Handlers:
    return coach_actions.Handlers(
        session=_plan_session, lift=_active_lift,
        apply=dict(swap=_apply_swap, move=_apply_move, goal=_apply_goal, workout=_apply_workout, checkin_note=_apply_checkin_note,
                   deload=_apply_deload, remember=_apply_remember),
        undo=dict(swap=_undo_session, move=_undo_session, goal=_undo_goal, workout=_undo_workout, checkin_note=_undo_checkin_note,
                  deload=_undo_deload, remember=_undo_remember),
        current=dict(swap=_session_state, move=_session_state,
                     checkin_note=lambda pp, _r: (store.checkin_get(pp["day"]) or {}).get("note") or ""))


def _proposal_out(pid: str) -> dict:
    return coach_actions.view(store.proposal_get(pid))


def _proposal_route(fn):
    try:
        return _proposal_out(fn()["id"])
    except coach_actions.ActionError as ex:
        raise HTTPException(409, str(ex))


@app.post("/api/coach/proposals/{pid}/apply")
def apply_proposal(pid: str):
    """Apply a coach suggestion: only ever from this click, through the normal code path."""
    return _proposal_route(lambda: coach_actions.apply(coach_handlers(), pid, lambda m: health.event("info", "AI coach", m)))


@app.post("/api/coach/proposals/{pid}/dismiss")
def dismiss_proposal(pid: str):
    return _proposal_route(lambda: coach_actions.dismiss(pid))


@app.post("/api/coach/proposals/{pid}/undo")
def undo_proposal(pid: str):
    return _proposal_route(lambda: coach_actions.undo(coach_handlers(), pid, lambda m: health.event("info", "AI coach", m)))


# ---- threads (streamed answers)
def _feed_problems() -> dict:
    out = {}
    for f in status_view()["feeds"]:
        if f["status"] in ("error", "warn") and not demo_mode():
            why = (f.get("error") or {}).get("title") or ("data is stale" if f.get("stale") else "needs attention")
            out[f["name"]] = f"{f['label']}: {why}" + (f", latest data {f['latest']}" if f.get("latest") else "")
    return out


class ThreadIn(BaseModel):
    title: str = Field("", max_length=80)


class AskIn(BaseModel):
    question: str = Field(..., min_length=1, max_length=1000)
    context: dict | None = None


def _thread(tid: str) -> dict:
    t = store.thread_get(tid)
    if not t:
        raise HTTPException(404, "Conversation not found.")
    return t


def _thread_view(t: dict) -> dict:
    msgs = store.messages(t["id"])
    props = {p["id"]: coach_actions.view(p) for p in store.proposals(t["id"])}
    for m in msgs:
        m["meta"]["proposals"] = [props[i] for i in m["meta"].get("proposal_ids") or [] if i in props]
    return dict(t, messages=msgs)


@app.get("/api/coach/threads")
def list_threads():
    return dict(threads=[dict(t, count=len(store.messages(t["id"]))) for t in store.threads()], llm=coach.llm_enabled())


@app.post("/api/coach/threads")
def new_thread(body_: ThreadIn):
    now = time.strftime("%Y-%m-%dT%H:%M:%S")
    t = dict(id=uuid.uuid4().hex[:10], title=body_.title.strip() or "New conversation", created=now, updated=now, summary="", summarized_upto=0)
    store.thread_put(t)
    return _thread_view(t)


@app.get("/api/coach/threads/{tid}")
def get_thread(tid: str):
    return _thread_view(_thread(tid))


@app.patch("/api/coach/threads/{tid}")
def rename_thread(tid: str, body_: ThreadIn):
    t = _thread(tid)
    t.update(title=body_.title.strip() or t["title"], updated=time.strftime("%Y-%m-%dT%H:%M:%S"))
    store.thread_put(t)
    return _thread_view(t)


@app.delete("/api/coach/threads/{tid}")
def delete_thread(tid: str):
    if not store.thread_delete(tid):
        raise HTTPException(404, "Conversation not found.")
    return dict(ok=True)


CONTEXT_KINDS = ("activity", "metric", "day", "lift", "goal")


def _context_preload(tb: coach_tools.Toolbox, ctx: dict | None) -> dict | None:
    """Run the matching read tool up front for an 'Ask about this' (it also becomes a source chip)."""
    if not ctx or ctx.get("kind") not in CONTEXT_KINDS:
        return None
    k, today = ctx["kind"], date.today()
    if k == "activity" and ctx.get("id"):
        return tb.run("get_activity_detail", dict(ref=tb.refs.ref(int(ctx["id"]))))
    if k == "metric":
        end = ctx.get("end") or today.isoformat()
        start = ctx.get("start") or (date.fromisoformat(end) - timedelta(days=29)).isoformat()
        if ctx.get("metric") not in coach_tools.METRICS:  # charts of workouts (miles, hours of tennis...): the workouts behind them
            start = max(start, (date.fromisoformat(end) - timedelta(days=89)).isoformat())
            return tb.run("get_activities", dict(start=start, end=end, type="any"))
        return tb.run("get_daily_metrics", dict(start=start, end=end, metrics=[ctx["metric"]]))
    if k == "day" and ctx.get("day"):
        r = tb.run("get_daily_metrics", dict(start=ctx["day"], end=ctx["day"], metrics=["readiness", "hrv", "sleep_h", "rhr", "load"]))
        return dict(day=r, workouts=tb.run("get_activities", dict(start=ctx["day"], end=ctx["day"], type="any")))
    if k == "lift" and ctx.get("name"):
        return tb.run("get_strength_history", dict(exercise=ctx["name"], start=(today - timedelta(days=120)).isoformat(), end=today.isoformat()))
    if k == "goal":
        return tb.run("get_goal_progress", {})
    return None


def _sse(event: str, data) -> str:
    return f"event: {event}\ndata: {json.dumps(data, default=str)}\n\n"


@app.post("/api/coach/threads/{tid}/ask")
def thread_ask(tid: str, body_: AskIn):
    """Streams the answer as server-sent events: status (tool running), text deltas, done (chips,
    number check, proposals, follow-ups) or error. Proposals are buttons; nothing is applied here."""
    t = _thread(tid)
    question = body_.question.strip()
    ctx = body_.context if body_.context and body_.context.get("kind") in CONTEXT_KINDS else None
    history = store.messages(tid)
    now = time.strftime("%Y-%m-%dT%H:%M:%S")
    store.message_add(dict(id=uuid.uuid4().hex[:12], thread_id=tid, role="user", text=question,
                           meta=dict(context=dict(label=ctx.get("label"), kind=ctx["kind"]) if ctx else None), created=now))
    if t["title"] == "New conversation":
        t["title"] = (ctx.get("label") + ": " if ctx and ctx.get("label") else "") + question[:60]
    t["updated"] = now
    store.thread_put(t)

    def gen():
        if not coach.llm_enabled():
            yield _sse("error", dict(title="Chat needs an ANTHROPIC_API_KEY in .env", fix="The rule-based coach still writes the daily briefing."))
            return
        older, recent = coach.thread_plan(history, t.get("summarized_upto") or 0)
        if older:
            yield _sse("status", dict(text="Summarizing earlier messages…"))
            t.update(summary=coach.summarize(t.get("summary") or "", older), summarized_upto=older[-1]["seq"])
            store.thread_put(t)
        snap, a = get_data()
        tb = coach_tools.Toolbox(coach_sources(chat=True), chat=True, thread_id=tid)
        pre = _context_preload(tb, ctx)
        seed = dict(today=date.today().isoformat(), readiness={k: a.get(k) for k in ("readiness", "zone", "recommendation", "flags")})
        if pre is not None:
            seed["context_data"] = pre
        sent: set = set()
        try:
            for kind, val in coach.thread_stream(question, recent, t.get("summary") or "", seed, tb, units_system(), ctx):
                if kind == "text":
                    yield _sse("text", dict(delta=val))
                elif kind == "tool":
                    yield _sse("status", dict(tool=val, text=coach_tools.STATUS_TEXT.get(val, "Looking at your data…")))
                    for p in tb.proposals:
                        if p["id"] not in sent:
                            sent.add(p["id"])
                            yield _sse("proposal", coach_actions.view(p))
                else:
                    chk = coach_trace.check(val["text"], tb.sources, extra=[question, seed.get("readiness")])
                    stale = coach_trace.stale_note(tb.sources, val["text"])
                    ctx_chip = next(({k: s[k] for k in ("id", "kind", "label", "link")} for s in tb.sources[:1]), None) if pre is not None else None
                    chips = chk["chips"] + ([ctx_chip] if ctx_chip and ctx_chip["id"] not in {c["id"] for c in chk["chips"]} else [])
                    meta = dict(chips=chips, unverified=chk["unverified"], stale=stale, followups=tb.followups,
                                proposal_ids=[p["id"] for p in tb.proposals], tools=tb.calls, model=val["model"], usage=val["usage"],
                                feature="thread")
                    m = store.message_add(dict(id=uuid.uuid4().hex[:12], thread_id=tid, role="assistant", text=chk["text"], meta=meta,
                                               created=time.strftime("%Y-%m-%dT%H:%M:%S")))
                    t["updated"] = m["created"]
                    store.thread_put(t)
                    yield _sse("done", dict(meta, id=m["id"], text=chk["text"],
                                            proposals=[coach_actions.view(p) for p in tb.proposals]))
        except ai.AIError as ex:
            yield _sse("error", {k: ex.info.get(k) for k in ("kind", "title", "explain", "fix")})

    return StreamingResponse(gen(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# ---- feedback
REASONS = tuple(coach.FEEDBACK_REASONS)


class FeedbackIn(BaseModel):
    feature: Literal["briefing", "activity", "dexa", "dexa_trend", "review", "goal_plan", "thread", "notice"]
    ref: str = Field(..., min_length=1, max_length=80)
    rating: Literal[1, -1, 0]
    reasons: list[Literal["too_generic", "wrong_numbers", "too_cautious", "not_realistic", "other"]] = Field(default_factory=list, max_length=5)


@app.post("/api/coach/feedback")
def give_feedback(body_: FeedbackIn):
    """Thumbs up/down (0 clears). Stored locally; only a short summary of recent reasons reaches the prompt."""
    fid = hashlib.sha1(f"{body_.feature}:{body_.ref}".encode()).hexdigest()[:16]
    store.feedback_add(dict(id=fid, ts=time.strftime("%Y-%m-%dT%H:%M:%S"), feature=body_.feature, ref=body_.ref, rating=body_.rating,
                            reasons=list(dict.fromkeys(body_.reasons)) if body_.rating < 0 else []))
    return feedback_summary()


@app.get("/api/coach/feedback")
def feedback_summary():
    since = (date.today() - timedelta(days=30)).isoformat()
    rows = [f for f in store.feedback(since) if f["rating"] != 0]
    by_reason = {r: sum(1 for f in rows if r in (f.get("reasons") or [])) for r in REASONS + ("other",)}
    by_feature: dict = {}
    for f in rows:
        x = by_feature.setdefault(f["feature"], dict(up=0, down=0))
        x["up" if f["rating"] > 0 else "down"] += 1
    return dict(days=30, up=sum(1 for f in rows if f["rating"] > 0), down=sum(1 for f in rows if f["rating"] < 0), by_reason=by_reason,
                by_feature=by_feature, hint=coach.feedback_hint(), labels={k: v[0] for k, v in coach.FEEDBACK_REASONS.items()},
                mine={f"{f['feature']}:{f['ref']}": dict(rating=f["rating"], reasons=f.get("reasons") or []) for f in store.feedback(since)})


@app.delete("/api/coach/feedback")
def clear_feedback():
    store.feedback_clear()
    return feedback_summary()


# ---- proactive notices
def _notice_inputs() -> dict:
    snap, a = get_data()
    prog = next((p for p in store.strength_programs() if p.get("active")), None)
    lifts = []
    if prog:
        for ex_id, s in _st_states(prog).items():
            done = [e for e in s.get("events") or [] if e["kind"] in ("success", "fail", "hold")]
            lifts.append(dict(exercise_id=ex_id, name=(strength_lib.lookup(ex_id, st_custom()) or {}).get("name") or ex_id,
                              status=s.get("status"), kg=s.get("last_kg"), sessions_since_progress=strength.STALL_SESSIONS if s.get("status") == "stalled" else len(done)))
    return dict(history=snap["history"], assessment=a, lifts=lifts, goals=_safe(lambda: goals_view()["goals"], []),
                dexa=dexa.brief_payload(store.dexa_scans()))


def _phrase(f: dict):
    key = ai.cache_key("notice", dict(type=f["type"], facts=f["facts"]), coach.memory_hash(), units_system())
    hit = store.ai_cache_get(key)
    if hit:
        return hit.get("text")
    text = coach.phrase_notice(f, units_system())
    if text:
        store.ai_cache_put(key, "notice", dict(source="claude", text=text))
    return text


def _notify_mac(title: str, body: str) -> None:
    if os.uname().sysname != "Darwin":
        return
    clean = lambda t: re.sub(r'["\\\n\r]', "", t)[:200]
    import subprocess
    try:
        subprocess.run(["osascript", "-e", f'display notification "{clean(body)}" with title "{clean(title)}"'], timeout=5, check=False,
                       capture_output=True)
    except (OSError, subprocess.SubprocessError):
        pass


@app.get("/api/coach/notices")
def coach_notices():
    """Today's 'Coach noticed' cards (rules decide; the AI only words them). At most 2 a day."""
    with _lock("notices"):
        try:
            inputs = _notice_inputs()
        except HTTPException:
            return dict(notices=[], settings=proactive.settings(), types=proactive.TYPES)
        shown = proactive.today_notices(inputs, phrase=_phrase)
        cfg = proactive.settings()
        now = datetime.now()
        for n in shown:
            if cfg["notify"] and not n.get("notified") and not proactive.in_quiet_hours(now, cfg["quiet_start"], cfg["quiet_end"]):
                _notify_mac(f"Coach noticed: {n['title']}", n["text"])
                n["notified"] = True
                store.notice_put(n)
        return dict(notices=shown, settings=cfg, types=proactive.TYPES)


@app.post("/api/coach/notices/{nid}/dismiss")
def dismiss_notice(nid: str):
    try:
        proactive.dismiss(nid)
    except ValueError as ex:
        raise HTTPException(404, str(ex))
    return dict(ok=True)


class SnoozeIn(BaseModel):
    type: str = Field(..., max_length=30)
    days: Literal[3, 7, 30] = 7


@app.post("/api/coach/notices/snooze")
def snooze_notice(body_: SnoozeIn):
    try:
        return proactive.snooze(body_.type, body_.days)
    except ValueError as ex:
        raise HTTPException(422, str(ex))


class NoticeSettingsIn(BaseModel):
    enabled: bool | None = None
    illness: bool | None = None
    notify: bool | None = None
    quiet_start: str | None = Field(None, pattern=r"^\d{2}:\d{2}$")
    quiet_end: str | None = Field(None, pattern=r"^\d{2}:\d{2}$")
    unsnooze: str | None = Field(None, max_length=30)


@app.put("/api/coach/notice-settings")
def put_notice_settings(body_: NoticeSettingsIn):
    ch = {f"notice_{k}": v for k, v in body_.model_dump().items() if v is not None and k != "unsnooze"}
    if body_.unsnooze:
        sn = dict(store.settings().get("notice_snooze") or {})
        sn.pop(body_.unsnooze, None)
        ch["notice_snooze"] = sn
    store.settings_put(ch)
    return proactive.settings()


# ---- outside context (weather, calendar): off by default
class ContextIn(BaseModel):
    weather_on: bool | None = None
    calendar_on: bool | None = None
    calendar_file: str | None = Field(None, max_length=400)


class CityIn(BaseModel):
    name: str = Field(..., min_length=2, max_length=80)


@app.get("/api/coach/context")
def get_context_settings():
    return context_ext.settings()


@app.put("/api/coach/context")
def put_context_settings(body_: ContextIn):
    ch = {f"ctx_{k}": v for k, v in body_.model_dump().items() if v is not None}
    if body_.calendar_file:
        p = Path(body_.calendar_file).expanduser()
        if p.suffix.lower() != ".ics" or not p.is_file():
            raise HTTPException(422, "Choose an existing .ics file.")
        ch["ctx_calendar_file"] = str(p)
    store.settings_put(ch)
    return context_ext.settings()


@app.post("/api/coach/context/city")
def set_weather_city(body_: CityIn):
    try:
        context_ext.set_city(body_.name)
    except ValueError as ex:
        raise HTTPException(422, str(ex))
    except httpx.HTTPError as ex:
        raise HTTPException(502, f"Couldn't reach the weather service ({type(ex).__name__}).")
    return context_ext.settings()


# ----------------------------- body & goals --------------------------------- #
_body: dict = {"t": 0, "data": None}
_metrics: dict[str, tuple] = {}  # goal type -> (fetched_at, metric | None, error | None)


def settings() -> dict:
    s = goals.load_settings()
    return dict(units=units.norm(s.get("units")), theme=s.get("theme") or "system", height_m=s.get("height_m"),
                share_checkins=bool(s.get("share_checkins", False)), data_dir=str(store.DATA_DIR),
                intervals_sync=bool(s.get("intervals_sync", False)), intervals_key_set=intervals.key_set(),
                ai=dict(models=ai.models(), routes=ai.routes(), cap_usd=ai.cap_usd(), choices=list(ai.MODEL_CHOICES),
                        prices={m: dict(input=p[0], output=p[1]) for m, p in ai.PRICES.items()},
                        features={f: ai.FEATURE_LABELS[f] for f in ai.ROUTES if f != "test"}, key_set=coach.llm_enabled()))


def units_system() -> str:
    """The user's display units ("us" by default); all stored data stays metric."""
    return settings()["units"]
BODY_KINDS = ("weight", "body_fat", "fitness_age")


def _fitness_age_history(c: GarminClient, end: date) -> list[tuple[str, float]]:
    """Fortnightly samples over the past year; past dates never change, so each is cached on disk."""
    def one(d):
        hit = _cache_get("fitnessage", d)
        if hit is not None:
            return hit.get("v")
        try:
            v = (c.get(f"/fitnessage-service/fitnessage/v2/{d}") or {}).get("fitnessAge")
        except GarminError:
            return None  # not cached, so it's retried next time
        _cache_put("fitnessage", d, {"v": v})
        return v

    days = [(end - timedelta(days=14 * k)).isoformat() for k in range(26, 0, -1)]
    with ThreadPoolExecutor(4) as ex:
        return [(d, v) for d, v in zip(days, ex.map(one, days)) if v is not None]


def _with_last_good_weight(raw: dict, failed: list[dict], errors: dict) -> dict:
    """Weigh-ins that didn't load this time are filled in from the last successful fetch, so a Garmin
    hiccup leaves the Body tab as it was instead of blank; the error still says what's missing."""
    if not failed:
        _body["weight_good"] = (time.time(), raw)
        return raw
    t, good = _body.get("weight_good") or (None, None)
    msg = body.weight_error(failed)
    if good:
        have = {r.get("summaryDate") for r in raw["dailyWeightSummaries"]}
        merged = raw["dailyWeightSummaries"] + [r for r in good["dailyWeightSummaries"] if r.get("summaryDate") not in have]
        raw = {"dailyWeightSummaries": sorted(merged, key=lambda r: str(r.get("summaryDate")), reverse=True)}
        msg += f"; showing weigh-ins saved at {time.strftime('%-I:%M %p', time.localtime(t))}"
    errors["weight"] = msg
    return raw


def body_data(refresh: bool = False) -> dict:
    with _lock("body"):
        if not refresh and _body["data"] and time.time() - _body["t"] < CACHE_TTL:
            return _body["data"]
        c, end = client(), date.today()
        start, errors = end - timedelta(days=365), {}

        def safe(name, fn):
            try:
                return fn()
            except (GarminError, httpx.HTTPError) as ex:
                errors[name] = str(ex)[:200] or type(ex).__name__
                return None

        raw_w, failed = body.fetch_weight(c.get, start, end, end, _cache_get, _cache_put)
        raw_w = _with_last_good_weight(raw_w, failed, errors)
        fa = safe("fitness_age", lambda: c.get(f"/fitnessage-service/fitnessage/v2/{end}"))
        goal = safe("weight_goal", lambda: c.get(f"/goal-service/goal/user/effective/weightgoal/{start}/{end}"))
        data = body.build(body.weight_series(raw_w), fa, _fitness_age_history(c, end), body.garmin_weight_goal(goal),
                          goals.load_settings().get("height_m"))
        data["errors"] = errors
        # A failed fetch is retried after a minute rather than pinned for the full TTL.
        _body.update(t=time.time() - (CACHE_TTL - 60 if errors else 0), data=data)
        for k in BODY_KINDS:  # goal metrics derived from body data are stale now
            _metrics.pop(k, None)
        return data


_daysum_mem: dict[str, tuple[float, dict]] = {}  # recent (unsettled) days, kept for CACHE_TTL


def _day_summary(c: GarminClient, d: str, today: date, fetch: bool = True) -> dict | None:
    """Calories, intensity minutes (moderate + 2 x vigorous) and resting HR for one day. Only a
    per-day endpoint exists, so settled days (2+ days old) are cached on disk for good.
    With fetch=False it only looks in the caches."""
    settled = d <= (today - timedelta(days=2)).isoformat()
    if settled:
        hit = _cache_get("daysummary", d)
    else:
        t, hit = _daysum_mem.get(d, (0, None))
        hit = hit if time.time() - t < CACHE_TTL else None
    if hit is not None or not fetch:
        return hit
    try:
        s = c.get("/usersummary-service/usersummary/daily", {"calendarDate": d}) or {}
    except GarminError:
        return None  # not cached, so it's retried next time
    row = dict(kcal=s.get("totalKilocalories"), active_kcal=s.get("activeKilocalories"), rhr=s.get("restingHeartRate"),
               im=(s.get("moderateIntensityMinutes") or 0) + 2 * (s.get("vigorousIntensityMinutes") or 0))
    if settled:
        _cache_put("daysummary", d, row)
    else:
        _daysum_mem[d] = (time.time(), row)
    return row


def _intensity_minutes(c: GarminClient, today: date, days: int = 28) -> dict[str, float]:
    """Garmin intensity minutes per day, from the shared per-day summary cache."""
    days_ = [(today - timedelta(days=i)).isoformat() for i in range(days)]
    with ThreadPoolExecutor(4) as ex:
        rows = ex.map(lambda d: (d, _day_summary(c, d, today)), days_)
        return {d: r["im"] for d, r in rows if r and r.get("im") is not None}


def _run_km(c, today):
    daily: dict[str, float] = {}
    for r in history_rows():
        if "run" in (r["type"] or ""):
            daily[r["date"]] = daily.get(r["date"], 0) + (r["dist_m"] or 0) / 1000
    return goals.weekly_metric(daily, today)


def _sleep(c, today):
    rows = c.chunked(lambda s, e: c.get("/sleep-service/sleep/dailySleepSummariesByDate", {"startDate": s, "endDate": e}),
                     today - timedelta(days=89), today)
    return goals.daily_metric([(r["calendarDate"], r["sleepTimeSeconds"] / 3600) for r in rows if r.get("sleepTimeSeconds")])


def _steps(c, today):  # today is partial, so it's left out
    rows = c.chunked(lambda s, e: c.get(f"/usersummary-service/stats/steps/daily/{s}/{e}"),
                     today - timedelta(days=89), today - timedelta(days=1))
    return goals.daily_metric([(r["calendarDate"], r["totalSteps"]) for r in rows if r.get("totalSteps")])


METRIC_FETCHERS = {
    **{k: (lambda kind: lambda c, today: goals.body_metrics(body_data()).get(kind))(k) for k in BODY_KINDS},
    "weekly_run_km": _run_km,
    "weekly_active_min": lambda c, today: goals.weekly_metric(_intensity_minutes(c, today), today, weeks=4),
    "sleep_h": _sleep,
    "steps": _steps,
}


def goal_metrics(types: set[str]) -> tuple[dict, dict]:
    """Current value + weekly trend per goal type, fetched lazily and cached for CACHE_TTL."""
    types = set(types) & set(METRIC_FETCHERS)
    with _lock("metrics"):
        c, today = client(), date.today()
        for t in types:
            hit = _metrics.get(t)
            if hit and time.time() - hit[0] < CACHE_TTL:
                continue
            try:
                m, err = METRIC_FETCHERS[t](c, today), None
            except (GarminError, HTTPException) as ex:
                m, err = None, str(getattr(ex, "detail", ex))[:200]
            err = err or (None if m else "No data from Garmin for this metric yet")
            _metrics[t] = (time.time() - (CACHE_TTL - 60 if err else 0), m, err)  # retry errors after a minute
        return ({t: _metrics[t][1] for t in types if _metrics[t][1]}, {t: _metrics[t][2] for t in types if _metrics[t][2]})


def goal_metrics_all(types: set[str]) -> tuple[dict, dict]:
    """goal_metrics plus DEXA: body fat % and lean mass come from the latest DEXA scan whenever there is
    one (it works without Garmin, e.g. in demo mode); body fat falls back to the scale otherwise."""
    dx = dexa.goal_metrics(store.dexa_scans())
    rest = {t for t in types if t not in dx and t != "lean_mass"}
    try:
        metrics, errors = goal_metrics(rest) if rest else ({}, {})
    except HTTPException as ex:  # demo mode
        metrics, errors = {}, ({"garmin": ex.detail} if rest & set(METRIC_FETCHERS) else {})
    metrics.update({t: dx[t] for t in types if t in dx})
    if "lean_mass" in types and "lean_mass" not in dx:
        errors["lean_mass"] = "Add a DEXA scan to track lean mass"
    return metrics, errors


def goal_ctx() -> dict:
    try:
        b = body_data()
    except HTTPException:
        return dict(units=units_system())
    return dict(height_m=b["height"]["m"], achievable_age=(b.get("fitness_age") or {}).get("achievable"), units=units_system())


def goals_view() -> dict:
    gl = goals.list_goals()
    metrics, errors = goal_metrics_all({g["type"] for g in gl})
    try:
        ctx, b = goal_ctx(), body_data()
        garmin_goal = b.get("garmin_goal")
        if garmin_goal and b.get("summary"):
            garmin_goal = dict(garmin_goal, current_kg=b["summary"]["avg7"]["kg"])
    except HTTPException:  # demo mode: goals still work, progress needs data
        ctx, garmin_goal = dict(units=units_system()), None
    return dict(goals=[goals.evaluate(g, metrics.get(g["type"]), ctx) for g in gl], garmin_goal=garmin_goal,
                types=goals.TYPES, errors=errors)


def coach_profile() -> dict:
    """Body + goal context for the coach; optional, so the briefing works without it."""
    try:
        b = body_data()
    except Exception:
        b = None
    try:
        gv = goals_view()["goals"]
    except Exception:
        gv = []
    p = coach.coach_profile(b, gv)
    dx = dexa.brief_payload(store.dexa_scans())  # derived numbers only: no name, birth date or report
    if dx:
        p["dexa"] = dx
    if checkins.sharing():  # off by default; ratings and tags only, never notes
        today, inds = date.today(), checkins.indicators()
        rows = store.checkins((today - timedelta(days=120)).isoformat(), today.isoformat())
        try:
            hist = get_data()[0]["history"]
        except Exception:
            hist = []
        p["checkins"] = checkins.coach_payload(rows, inds, today, checkins.compute(rows, hist, inds, today))
    return p


@app.get("/api/units")
def get_units():  # kept for older pages; the setting lives in /api/settings
    return dict(system=units_system())


@app.get("/api/body")
def body_view(refresh: bool = False):
    return body_data(refresh)


class SettingsIn(BaseModel):
    share_checkins: bool | None = None
    units: Literal["us", "metric"] | None = None
    theme: Literal["system", "light", "dark"] | None = None
    height_m: float | None = Field(None, ge=1.0, le=2.5)
    intervals_sync: bool | None = None
    ai_model_deep: str | None = Field(None, max_length=60, pattern=r"^claude-[a-z0-9.-]+$")
    ai_model_fast: str | None = Field(None, max_length=60, pattern=r"^claude-[a-z0-9.-]+$")
    ai_routes: dict[str, Literal["fast", "deep"]] | None = None
    ai_cap_usd: float | None = Field(None, ge=0, le=10_000)


@app.get("/api/settings")
def get_settings():
    return settings()


@app.put("/api/settings")
def put_settings(s: SettingsIn):
    """Partial update: only the fields sent change (height_m: null clears the override)."""
    sent = s.model_fields_set
    before = settings()
    fields = {k: getattr(s, k) for k in sent}
    if "ai_routes" in fields and fields["ai_routes"] is not None:
        fields["ai_routes"] = {f: t for f, t in fields["ai_routes"].items() if f in ai.ROUTES}
    if "ai_cap_usd" in fields and not fields["ai_cap_usd"]:
        fields["ai_cap_usd"] = None  # 0 or empty clears the cap
    goals.save_settings(fields)
    if "height_m" in sent:
        _body["t"] = 0
    if "height_m" in sent or settings()["units"] != before["units"] or settings()["share_checkins"] != before["share_checkins"]:
        _cache["brief"] = None  # the briefing quotes these numbers
    return dict(settings=settings(), body=body_data() if "height_m" in sent else None)


class GoalIn(BaseModel):
    type: Literal["weight", "body_fat", "lean_mass", "fitness_age", "weekly_run_km", "weekly_active_min", "sleep_h", "steps", "custom"]
    title: str = Field("", max_length=120)
    target: float | None = None
    deadline: date | None = None
    start_value: float | None = None
    notes: str = Field("", max_length=600)
    manual_progress: float | None = Field(None, ge=0, le=100)


def _clean_goal(g: GoalIn) -> dict:
    if g.type == "custom":
        if not g.title.strip():
            raise HTTPException(422, "A custom goal needs a title.")
    elif g.target is None or g.deadline is None:
        raise HTTPException(422, "Set a target and a deadline.")
    d = g.model_dump(mode="json")
    d["title"] = d["title"].strip()
    if d["start_value"] is None and g.type != "custom":  # start from where you are today
        try:
            cur = goal_metrics_all({g.type})[0].get(g.type, {}).get("current")
        except HTTPException:
            cur = None
        d["start_value"] = None if cur is None else round(cur, 3)
    return d


def _goals_changed():
    _cache["brief"] = None  # the briefing mentions goals


@app.get("/api/goals")
def list_goals():
    return goals_view()


@app.post("/api/goals/preview")
def preview_goal(g: GoalIn):
    """Live guardrails for the form: current value, warnings, suggested deadline."""
    metrics, _ = goal_metrics_all({g.type})
    try:
        ctx = goal_ctx()
    except HTTPException:
        ctx = {}
    return goals.evaluate(dict(g.model_dump(mode="json"), id="preview"), metrics.get(g.type), ctx)


@app.post("/api/goals")
def create_goal(g: GoalIn):
    goals.create(_clean_goal(g))
    _goals_changed()
    return goals_view()


@app.put("/api/goals/{goal_id}")
def update_goal(goal_id: str, g: GoalIn):
    d = _clean_goal(g)
    if g.start_value is None:
        d.pop("start_value")  # keep the original starting point
    if goals.update(goal_id, d) is None:
        raise HTTPException(404, "Goal not found")
    _goals_changed()
    return goals_view()


@app.delete("/api/goals/{goal_id}")
def delete_goal(goal_id: str):
    if not goals.delete(goal_id):
        raise HTTPException(404, "Goal not found")
    _goals_changed()
    return goals_view()


@app.get("/api/goals/coach")
def goal_coach(refresh: bool = False):
    """Short plan toward the goals (Claude, or rules without a key); cached per goal set per day."""
    with _lock("goalcoach"):
        gv = goals_view()["goals"]
        try:
            _, a = get_data()
        except HTTPException:
            a = {}
        try:
            b = body_data()
        except HTTPException:
            b = None
        prof, system = coach_profile(), units_system()
        if not gv or not coach.llm_enabled():
            return dict(coach.goal_plan(prof, a, gv, b, system), generated=time.strftime("%Y-%m-%d %H:%M"))
        inputs = [prof, dict(score=a.get("readiness"), zone=a.get("zone"))]
        key = ai.cache_key("goal_plan", inputs, coach.memory_hash(), system)
        return ai.cached("goal_plan", key, lambda: dict(coach.goal_plan(prof, a, gv, b, system, essential=refresh),
                                                         generated=time.strftime("%Y-%m-%d %H:%M")), refresh)


def _make_suggestions(key: str) -> dict:
    today, existing, system = date.today(), {g["type"] for g in goals.list_goals()}, units_system()
    try:
        metrics, _ = goal_metrics(set(goals.TYPES))
    except HTTPException:  # demo mode
        metrics = {}
    try:
        b = body_data()
    except HTTPException:
        b = None
    rules = lambda: goals.rule_suggestions(metrics, b, existing, today, system)
    res = dict(key=key, source="rules", generated=time.strftime("%Y-%m-%d %H:%M"))
    if coach.llm_enabled():
        try:
            _, a = get_data()
        except HTTPException:
            a = {}
        ctx = dict(today=today.isoformat(), **coach_profile(),
                   readiness=dict(score=a.get("readiness"), zone=a.get("zone"), strain=a.get("strain")),
                   metrics={t: dict(current=round(m["current"], 2), trend_per_week=None if m["rate"] is None else round(m["rate"], 3),
                                    unit=goals.TYPES[t]["unit"], basis=m["basis"])
                            for t, m in metrics.items() if m.get("current") is not None},
                   goal_types={t: dict(label=v["label"], unit=v["unit"]) for t, v in goals.TYPES.items()})
        try:
            raw, res["source"] = coach.suggest_goals(ctx, system), "claude"
        except (ai.AIError, ValueError) as ex:
            raw, res["note"] = rules(), f"{getattr(ex, 'info', {}).get('title') or 'Claude call failed'}; showing rule-based suggestions."
    else:
        raw = rules()
    # Claude answers in display units; rule-based targets are already metric
    res["suggestions"] = goals.clean_suggestions(raw, today, system if res["source"] == "claude" else None)
    if res["source"] == "claude" or not coach.llm_enabled():  # don't pin a fallback caused by an API error
        _cache_put("goalsuggest", "latest", res)
    return res


@app.get("/api/goals/suggestions")
def goal_suggestions(generate: bool = False, refresh: bool = False):
    """AI (or rule-based) goal ideas, cached for the day. Without generate/refresh this only
    returns what's cached, so opening the tab never triggers a Claude call by itself."""
    with _lock("goalsuggest"):
        key = f"{date.today()}:{coach.llm_enabled()}:{units_system()}"
        hit = _cache_get("goalsuggest", "latest")
        if not hit or hit.get("key") != key:
            hit = None
        if refresh or (generate and not hit):
            hit = _make_suggestions(key)
        if not hit:
            return dict(suggestions=None)
    gl, types = goals.list_goals(), {s["type"] for s in hit["suggestions"]}
    try:
        metrics, _ = goal_metrics(types)
        ctx = goal_ctx()
    except HTTPException:
        metrics, ctx = {}, {}
    out = []
    for i, s in enumerate(hit["suggestions"]):  # evaluated fresh, so current values and warnings stay up to date
        view = goals.evaluate(dict(s, id=f"s{i}"), metrics.get(s["type"]), ctx)
        adopted = any(g["type"] == s["type"] and g.get("target") == s["target"] and g.get("deadline") == s["deadline"]
                      and (s["type"] != "custom" or g.get("title") == s["title"]) for g in gl)
        out.append(dict(s, view=view, adopted=adopted))
    return dict(source=hit["source"], note=hit.get("note"), generated=hit.get("generated"), suggestions=out)


# --------------------------------- calendar ---------------------------------- #
DAY_RE, MONTH_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$"), re.compile(r"^\d{4}-\d{2}$")


def _parse_day(s: str) -> date:
    try:
        if DAY_RE.match(s or ""):
            return date.fromisoformat(s)
    except ValueError:
        pass
    raise HTTPException(422, "Dates look like 2026-10-27.")


def _parse_month(s: str) -> tuple[int, int]:
    if MONTH_RE.match(s or "") and 1 <= int(s[5:]) <= 12:
        return int(s[:4]), int(s[5:])
    raise HTTPException(422, "Months look like 2026-10.")


def _demo_month(y: int, m: int) -> dict:
    """The calendar month in demo mode, from the same synthetic days as charts and records."""
    first, last, today = date(y, m, 1), date(y + (m == 12), m % 12 + 1, 1) - timedelta(days=1), date.today()
    zone = lambda r: None if r is None else "prime" if r >= 80 else "good" if r >= 62 else "caution" if r >= 45 else "recover"
    days = {k: dict(r, zone=zone(r.get("readiness"))) for k, r in demo_rows(first, min(last, today)).items()} if first <= today else {}
    weighins = [dict(date=k, kg=r["weight_kg"]) for k, r in days.items() if r.get("weight_kg")]
    return dict(month=f"{y:04d}-{m:02d}", first=first.isoformat(), last=last.isoformat(), days=days,
                summary=calendar_data.summary(days, weighins), errors={}, **({} if first <= today else {"future": True}))


def month_data(month: str, refresh: bool = False) -> dict:
    y, m = _parse_month(month)
    if demo_mode():
        return _demo_month(y, m)
    with _lock(f"calendar:{month}"):
        try:
            return calendar_data.month(client(), y, m, date.today(), _cache_get, _cache_put, refresh)
        except GarminError as ex:
            raise HTTPException(502, str(ex))


@app.get("/api/calendar")
def calendar_month(month: str, refresh: bool = False):
    """One month of daily metrics + activities (cached; past months never refetched)."""
    data = month_data(month, refresh)
    counts: dict[str, int] = {}
    for p in store.photos(month=month):
        counts[p["day"]] = counts.get(p["day"], 0) + 1
    y, m = _parse_month(month)
    nxt = date(y + (m == 12), m % 12 + 1, 1)
    days = {r["day"]: dict(ratings=r["ratings"], note=bool(r["note"]), tags=r["tags"])
            for r in store.checkins(f"{month}-01", (nxt - timedelta(days=1)).isoformat())}
    planned, evs = training_overlay(f"{month}-01", (nxt - timedelta(days=1)).isoformat(), data.get("days") or {})
    scans = {x["measured_date"]: dict(id=x["id"], source=x["source"]) for x in store.dexa_scans() if x["measured_date"].startswith(month)}
    return dict(data, photos=counts, dexa=scans, checkins=days, checkin_summary=checkins.month_summary(month),
                indicators=[i for i in checkins.indicators() if i["enabled"]], planned=planned, events=evs)


@app.get("/api/calendar/day/{day}")
def calendar_day(day: str):
    """A day's metrics, workouts (with a takeaway from cached analyses only - never a new AI call) and photos."""
    d, today = _parse_day(day), date.today()
    pics = [photos.public(p) for p in store.photos(day=day)]
    scan_card = dexa_card(scan) if (scan := store.dexa_get_day(day)) else None
    if d > today:
        planned, evs = training_overlay(day, day, {})
        return dict(date=day, future=True, activities=[], photos=pics, dexa=scan_card, planned=planned.get(day, []), events=evs.get(day, []))
    row = month_data(f"{d:%Y-%m}")["days"].get(day) or dict(date=day, activities=[])
    system = units_system()
    acts = [dict(a, takeaway=calendar_data.takeaway(_cache_get("analysis", a["id"]), system)) for a in row.get("activities", [])]
    rhr = row.get("rhr") if demo_mode() else calendar_data.resting_hr(client(), d, today, _cache_get, _cache_put)
    planned, evs = training_overlay(day, day, {day: row})
    return dict(row, activities=acts, rhr=rhr, photos=pics, dexa=scan_card, planned=planned.get(day, []), events=evs.get(day, []))


# ---------------------------------- photos ----------------------------------- #
# Local only: served from .data/photos through this server, never sent to Claude or anywhere else.
def _photo(pid: str) -> dict:
    p = store.photo_get(pid) if photos.ID_RE.match(pid or "") else None
    if not p:
        raise HTTPException(404, "Photo not found")
    return p


@app.post("/api/photos")
async def upload_photos(day: str = Form(...), files: list[UploadFile] = File(...)):
    """Several files at once; each is validated on its own so one bad file doesn't sink the batch."""
    _parse_day(day)
    results = []
    for f in files[:30]:
        data = await f.read(photos.MAX_BYTES + 1)  # never more than the limit + 1 byte in memory
        name = (f.filename or "photo")[:80]
        try:
            row = await run_in_threadpool(photos.add, data, f.filename or "", day)
        except photos.PhotoError as ex:
            results.append(dict(ok=False, name=name, error=str(ex)))
            continue
        results.append(dict(ok=True, name=name, photo=photos.public(row), capture_date=row["capture_date"],
                            mismatch=bool(row["capture_date"] and row["capture_date"] != day)))
    return dict(results=results)


@app.get("/api/photos")
def list_photos(day: str | None = None, month: str | None = None):
    if day:
        _parse_day(day)
    if month:
        _parse_month(month)
    return dict(photos=[photos.public(p) for p in store.photos(day=day, month=month)])


@app.get("/api/photos/progress")
def progress_photos():
    """Progress-tagged photos with the weight, body fat and workouts around each one."""
    try:
        series = body_data().get("series") or []
    except HTTPException:
        series = []
    kg = [(r["date"], r["kg"]) for r in series]
    fat = [(r["date"], r["fat"]) for r in series if r.get("fat") is not None]
    today, out, months = date.today(), [], {}
    for p in store.photos(tag="Progress"):
        d = date.fromisoformat(p["day"])
        monday = d - timedelta(days=d.weekday())
        week = [monday + timedelta(days=i) for i in range(7) if monday + timedelta(days=i) <= today]
        workouts = None
        try:
            for m in {f"{x:%Y-%m}" for x in week}:
                months[m] = months.get(m) or month_data(m)
            workouts = sum(1 for x in week for a in (months[f"{x:%Y-%m}"]["days"].get(x.isoformat()) or {}).get("activities", [])
                           if not calendar_data.is_walk(a["type"]))
        except HTTPException:
            pass
        near = lambda pts: body.avg_window(pts, d) if pts else None
        out.append(dict(photos.public(p), weight_kg=near(kg), fat=near(fat), workouts_week=workouts))
    return dict(photos=out)


class PhotoPatch(BaseModel):
    day: str | None = None
    caption: str | None = Field(None, max_length=300)
    tag: Literal["Progress", "Event", "Milestone", "Other"] | None = None


@app.patch("/api/photos/{pid}")
def patch_photo(pid: str, patch: PhotoPatch):
    p = _photo(pid)
    if patch.day:
        _parse_day(patch.day)
        p = photos.move(p, patch.day)
    fields = {}
    if patch.caption is not None:
        fields["caption"] = patch.caption.strip()
    if patch.tag:
        fields["tag"] = patch.tag
    if fields:
        store.photo_update(pid, fields)
    return photos.public(_photo(pid))


@app.delete("/api/photos/{pid}")
def delete_photo(pid: str):
    photos.remove(_photo(pid))
    return dict(ok=True)


@app.get("/api/photos/{pid}/{which}")
def photo_file(pid: str, which: Literal["full", "thumb"]):
    path = photos.resolve(_photo(pid), which)
    if not path:
        raise HTTPException(404, "Photo file missing")
    return FileResponse(path, media_type="image/png" if path.suffix == ".png" else "image/jpeg",
                        headers={"X-Content-Type-Options": "nosniff", "Cache-Control": "private, max-age=86400"})


@app.get("/api/export")
def export_data(save: bool = False):
    """Everything the app keeps (photos + goals/settings/photo metadata) as one zip for backup.
    save=true writes it to ~/Downloads and returns the path (the desktop window can't download)."""
    out_dir = CACHE / "export"
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"readiness-coach-export-{date.today():%Y%m%d}.zip"
    rows = store.photos()
    meta = dict(exported=time.strftime("%Y-%m-%dT%H:%M:%S"), data_dir=str(store.DATA_DIR), goals=goals.list_goals(),
                settings=goals.load_settings(),
                photos=[{k: p[k] for k in ("id", "day", "capture_date", "caption", "tag", "width", "height", "created")}
                        | {"file": f"photos/{p['path']}"} for p in rows])
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        scans = store.dexa_scans()
        meta |= dict(coach_profile=store.coach_profile(), coach_notes=store.coach_notes(), ai_usage=store.ai_usage(),
                     checkins=store.checkins(), checkin_indicators=checkins.indicators(), events=store.events(), plans=store.plans(),
                     dexa_scans=[dict(x, pdf=f"dexa/{x['pdf']}" if x.get("pdf") else None) for x in scans])
        z.writestr("metadata.json", json.dumps(meta, indent=1))
        z.writestr("README.txt", "Readiness Coach export: metadata.json (goals, settings, photo details, DEXA scans) and photos/ "
                                 "(stored copies, already stripped of EXIF/GPS).\n"
                                 "dexa/ holds your original DEXA report PDFs, exactly as uploaded (they include your name and birth date).\n")
        for p in rows:
            full = photos.resolve(p, "full")
            if full:
                z.write(full, f"photos/{p['path']}", compress_type=zipfile.ZIP_STORED)  # already compressed
        for x in scans:
            if (f := dexa.pdf_path(x)):
                z.write(f, f"dexa/{x['pdf']}")
    health.event("info", "Backup", f"Exported {len(rows)} photos and {len(scans)} DEXA scans")
    if save:
        dest_dir = Path.home() / "Downloads"
        dest = (dest_dir if dest_dir.is_dir() else out_dir) / path.name
        if dest != path:
            dest.write_bytes(path.read_bytes())
        return dict(path=str(dest), photos=len(rows), dexa_scans=len(scans), bytes=dest.stat().st_size)
    return FileResponse(path, media_type="application/zip", filename=path.name)


# ----------------------------------- DEXA ------------------------------------ #
# Reports stay on this computer: PDFs live in .data/dexa/ and are served only by this server. The coach
# only ever gets derived numbers (dexa.coach_payload), never the PDF, the name or the birth date, and
# nothing from a report is logged except dates, counts and statuses.
def _scan(sid: str) -> dict:
    x = store.dexa_get(sid) if dexa.ID_RE.match(sid or "") else None
    if not x:
        raise HTTPException(404, "Scan not found")
    return x


def _scan_public(x: dict) -> dict:
    out = {k: v for k, v in x.items() if k not in ("pdf", "file_sha256")}
    return dict(out, has_pdf=bool(dexa.pdf_path(x)), pdf_url=f"/api/dexa/{x['id']}/pdf" if dexa.pdf_path(x) else None,
                totals=dexa.totals(x), imbalances=dexa.imbalances(x), percentile=dexa.percentile_band(x),
                missing_labels=[dexa_parse.SECTIONS.get(k, k) for k in x.get("missing") or []])


def _previous(x: dict, scans: list[dict] | None = None) -> dict | None:
    scans = scans if scans is not None else store.dexa_scans()
    return next((p for p in reversed(scans) if p["measured_date"] < x["measured_date"] and dexa.totals(p).get("fat_kg") is not None), None)


def dexa_card(x: dict) -> dict:
    """The calendar day panel's summary: totals plus the change since the previous scan."""
    prev = _previous(x)
    ch = dexa.change(prev, x) if prev else None
    return dict(id=x["id"], date=x["measured_date"], source=x["source"], totals=dexa.totals(x), previous=prev["measured_date"] if prev else None,
                change={k: ch["total"][k]["d"] for k in ("fat_pct", "fat_kg", "lean_kg", "total_kg")} if ch else None)


def _dexa_changed() -> None:
    _cache["brief"] = None  # the briefing quotes the latest scan


def _scale_series() -> list[dict]:
    try:
        return body_data().get("series") or []
    except HTTPException:  # demo mode / no Garmin key
        return []


def _dexa_payload(focus: dict | None = None) -> dict | None:
    scans = store.dexa_scans()
    try:
        hist = records_history()
    except Exception:
        hist = None
    return dexa.coach_payload(scans, focus, hist, _scale_series(), [g for g in goals.list_goals() if g["type"] in ("body_fat", "lean_mass")])


def _dexa_text(kind: str, file: str, payload: dict | None, refresh: bool, writer, rules) -> dict:
    """Claude (or the rule-based fallback) for a DEXA payload, cached until the inputs change."""
    with _lock(f"dexa:{file}"):
        system = units_system()
        if payload is None:
            return dict(source="rules", text="No scans yet.", generated=None)
        key = hashlib.sha1(json.dumps([payload, system, coach.llm_enabled()], sort_keys=True, default=str).encode()).hexdigest()
        hit = None if refresh else _cache_get(kind, file)
        if hit and hit.get("key") == key:
            return hit["res"]
        res = dict(source="rules", text=rules(payload, system), generated=time.strftime("%Y-%m-%d %H:%M"))
        if coach.llm_enabled():
            feature = "dexa" if kind == "dexa" else "dexa_trend"
            try:
                out = ai.cached(feature, ai.cache_key(feature, payload, coach.memory_hash(), system), lambda: writer(payload, system), refresh)
                res.update(text=out["text"], data=out["data"], source="claude")
            except ai.AIError as ex:
                res["note"] = f"{ex.info['title']}; showing the rule-based analysis."
                return res  # not cached, so the next open retries
        else:
            res["note"] = "Add ANTHROPIC_API_KEY to .env for a written analysis; this one is rule-based."
        _cache_put(kind, file, dict(key=key, res=res))
        return res


@app.post("/api/dexa/parse")
async def dexa_parse_upload(files: list[UploadFile] = File(...)):
    """Reads each PDF (nothing is saved yet): parsed values for the review screen, duplicate status,
    history scans that could be backfilled, and what's missing. Accepted files wait in staging."""
    existing = {x["measured_date"]: x for x in store.dexa_scans()}
    seen: dict[str, str] = {}  # measured date / file hash -> file name, within this upload
    results = []
    for f in files[:12]:
        name = (f.filename or "report.pdf")[:80]
        data = await f.read(dexa.MAX_BYTES + 1)
        if len(data) > dexa.MAX_BYTES:
            results.append(dict(ok=False, name=name, error="Too large; the limit is 25 MB."))
            continue
        if not name.lower().endswith(".pdf"):
            results.append(dict(ok=False, name=name, error="Only PDF reports are supported."))
            continue
        h = dexa.sha256(data)
        try:
            parsed = await run_in_threadpool(dexa_parse.parse, data)
        except dexa_parse.DexaParseError as ex:
            log.info("DEXA parse failed: %s", ex)
            results.append(dict(ok=False, name=name, error=str(ex)))
            continue
        day = parsed["measured_date"]
        plan = dexa.plan_import(parsed, existing)
        if plan["status"] != "duplicate" and (day in seen or h in seen):
            plan = dict(status="duplicate", backfill=[], message=f"Same scan ({day}) as {seen.get(day) or seen.get(h)} in this upload, so it will be skipped.")
        seen.setdefault(day, name)
        seen.setdefault(h, name)
        token = dexa.stage(data, parsed) if plan["status"] != "duplicate" else None
        log.info("DEXA parse: %s status=%s found=%d missing=%d warnings=%d history=%d", day, plan["status"], len(parsed["found"]),
                 len(parsed["missing"]), len(parsed["warnings"]), len(parsed.get("history") or []))
        results.append(dict(ok=True, name=name, token=token, measured_date=day, status=plan["status"], message=plan["message"],
                            backfill=plan["backfill"], scan={k: v for k, v in parsed.items() if k != "history"},
                            history=parsed.get("history") or [], missing_labels=[dexa_parse.SECTIONS[k] for k in parsed["missing"]],
                            percentile=dexa.percentile_band(dict(parsed, source="report"))))
    return dict(results=results)


class DexaConfirm(BaseModel):
    token: str
    scan: dict = Field(default_factory=dict)
    backfill: list[str] = Field(default_factory=list)


@app.post("/api/dexa/confirm")
def dexa_confirm(body: DexaConfirm):
    try:
        res = dexa.save(body.token, body.scan, body.backfill)
    except ValueError as ex:
        raise HTTPException(410, str(ex))
    log.info("DEXA save: %s %s backfilled=%d", ",".join(res["saved"]) or "-", res["status"], len(res["backfilled"]))
    _dexa_changed()
    return res


class DexaCancel(BaseModel):
    tokens: list[str]


@app.post("/api/dexa/cancel")
def dexa_cancel(body: DexaCancel):
    for t in body.tokens[:20]:
        dexa.discard(t)
    return dict(ok=True)


@app.get("/api/dexa")
def dexa_list():
    scans = store.dexa_scans()
    series = _scale_series()
    return dict(scans=[_scan_public(x) for x in scans], trend=dexa.trend(scans), llm=coach.llm_enabled(),
                scale=[d for x in scans if (d := dexa.scale_diff(x, series))],
                projections=dexa.projection(scans, [g for g in goals.list_goals() if g["type"] in ("body_fat", "lean_mass")]))


@app.get("/api/dexa/compare")
def dexa_compare(a: str, b: str):
    x, y = sorted((_scan(a), _scan(b)), key=lambda s: s["measured_date"])
    near = lambda d: [photos.public(p) for p in store.photos(tag="Progress")
                      if abs((date.fromisoformat(p["day"]) - date.fromisoformat(d)).days) <= dexa.PHOTO_WINDOW_DAYS]
    return dict(a=_scan_public(x), b=_scan_public(y), change=dexa.change(x, y), photos=dict(a=near(x["measured_date"]), b=near(y["measured_date"])),
                photo_window_days=dexa.PHOTO_WINDOW_DAYS)


@app.get("/api/dexa/trend/analysis")
def dexa_trend_analysis(refresh: bool = False):
    return _dexa_text("dexatrend", "all", _dexa_payload(), refresh, coach.dexa_trend, dexa.rule_trend)


@app.get("/api/dexa/{sid}")
def dexa_get(sid: str):
    x = _scan(sid)
    scans = store.dexa_scans()
    prev = _previous(x, scans)
    base = next((s for s in scans if dexa.totals(s).get("fat_kg") is not None), None)
    return dict(scan=_scan_public(x), previous=_scan_public(prev) if prev else None,
                change_previous=dexa.change(prev, x) if prev else None,
                change_baseline=dexa.change(base, x) if base and base["id"] not in (x["id"], (prev or {}).get("id")) else None,
                scale=dexa.scale_diff(x, _scale_series()), others=[dict(id=s["id"], date=s["measured_date"], source=s["source"]) for s in scans])


class DexaPatch(BaseModel):
    scan: dict


@app.patch("/api/dexa/{sid}")
def dexa_patch(sid: str, body: DexaPatch):
    x = dexa.update(_scan(sid), body.scan)
    _dexa_changed()
    return _scan_public(x)


@app.delete("/api/dexa/{sid}")
def dexa_delete(sid: str):
    x = _scan(sid)
    dexa.remove(x)
    (CACHE / "dexa" / f"{sid}.json").unlink(missing_ok=True)
    log.info("DEXA delete: %s", x["measured_date"])
    _dexa_changed()
    return dict(ok=True)


@app.get("/api/dexa/{sid}/pdf")
def dexa_pdf(sid: str):
    x = _scan(sid)
    path = dexa.pdf_path(x)
    if not path:
        raise HTTPException(404, "No PDF stored for this scan")
    return FileResponse(path, media_type="application/pdf", filename=f"dexa-{x['measured_date']}.pdf", content_disposition_type="inline",
                        headers={"X-Content-Type-Options": "nosniff", "Cache-Control": "private, no-store"})


@app.get("/api/dexa/{sid}/analysis")
def dexa_analysis(sid: str, refresh: bool = False):
    x = _scan(sid)
    return _dexa_text("dexa", sid, _dexa_payload(x), refresh, coach.analyze_dexa, dexa.rule_analysis)


# --------------------------------- check-ins --------------------------------- #
# Local only. The coach sees ratings and tags (never notes), and only when sharing is switched on.
def _checkin_view(day: str, d: date) -> dict:
    today = date.today()
    out = dict(store.checkin_get(day) or dict(day=day, ratings={}, note="", tags=[]),
               indicators=checkins.indicators(), tags_available=checkins.tags(), editable=d <= today, care=None)
    if d == today:
        out["care"] = checkins.care(store.checkins((today - timedelta(days=30)).isoformat(), day), today)
    return out


def _checkin_rows(days: int = 120) -> list[dict]:
    today = date.today()
    return store.checkins((today - timedelta(days=days)).isoformat(), today.isoformat())


@app.get("/api/checkins/config")
def checkin_config():
    return dict(indicators=checkins.indicators(), tags=checkins.tags(), share=checkins.sharing(), min_days=checkins.MIN_DAYS)


@app.get("/api/checkins/insights")
def checkin_insights():
    """Rule-based patterns vs Garmin data (from 14 check-in days on) and recent mismatches."""
    try:
        hist = get_data()[0]["history"]
    except HTTPException:
        hist = []
    return checkins.compute(_checkin_rows(), hist, checkins.indicators(), date.today())


class IndicatorIn(BaseModel):
    label: str | None = Field(None, max_length=60)
    enabled: bool | None = None


@app.post("/api/checkins/indicators")
def add_checkin_indicator(body: IndicatorIn):
    try:
        return dict(indicators=checkins.add_indicator(body.label or ""))
    except ValueError as ex:
        raise HTTPException(422, str(ex))


@app.patch("/api/checkins/indicators/{ind_id}")
def update_checkin_indicator(ind_id: str, body: IndicatorIn):
    try:
        items = checkins.update_indicator(ind_id, body.label, body.enabled)
    except KeyError:
        raise HTTPException(404, "Indicator not found")
    except ValueError as ex:
        raise HTTPException(422, str(ex))
    _cache["brief"] = None
    return dict(indicators=items)


@app.get("/api/checkins")
def list_checkins(start: str | None = None, end: str | None = None):
    for s_ in (start, end):
        if s_:
            _parse_day(s_)
    return dict(checkins=store.checkins(start, end))


@app.delete("/api/checkins")
def delete_all_checkins(confirm: bool = False):
    if not confirm:
        raise HTTPException(422, "Add ?confirm=true to delete all check-in data.")
    n = store.checkins_delete_all()
    store.settings_put({"care_dismissed": None})
    _cache["brief"] = None
    return dict(deleted=n)


class CareDismiss(BaseModel):
    since: str


@app.post("/api/checkins/care/dismiss")
def dismiss_care(body: CareDismiss):
    _parse_day(body.since)
    store.settings_put({"care_dismissed": body.since})
    return dict(ok=True)


@app.get("/api/checkins/{day}")
def get_checkin(day: str):
    return _checkin_view(day, _parse_day(day))


class CheckinIn(BaseModel):
    ratings: dict[str, int | None] | None = None
    note: str | None = Field(None, max_length=1000)
    tags: list[str] | None = None


@app.put("/api/checkins/{day}")
def put_checkin(day: str, body: CheckinIn):
    """Saves as you tap: send only what changed (a rating of null clears it)."""
    d = _parse_day(day)
    try:
        u = checkins.clean_update(d, date.today(), body.ratings, body.note, body.tags)
    except ValueError as ex:
        raise HTTPException(422, str(ex))
    store.checkin_put(day, u.get("ratings"), u.get("note"), u.get("tags_"))
    if checkins.sharing():
        _cache["brief"] = None  # the briefing quotes recent check-ins
    return _checkin_view(day, d)


# ---------------------------------- charts ----------------------------------- #
# Today's custom charts read cached data only. Whatever a chart still needs (calendar months,
# per-day summaries, at most a year back) is fetched by one background thread and cached; the
# page shows what's there and polls while `loading` is set. Settled data is never refetched.
_bf: dict = {"queue": [], "queued": set(), "failed": {}, "thread": None}
_bf_guard = threading.Lock()
_month_mem: dict[str, tuple[float, dict]] = {}  # months that came back with errors (not cached on disk)


def _bf_worker():
    while True:
        with _bf_guard:
            if not _bf["queue"]:
                _bf["thread"] = None
                return
            item = _bf["queue"].pop(0)
        kind, key = item
        today, ok = date.today(), False
        try:
            if kind == "month":
                y, m = _parse_month(key)
                with _lock(f"calendar:{key}"):
                    data = calendar_data.month(client(), y, m, today, _cache_get, _cache_put)
                if data.get("errors"):
                    _month_mem[key] = (time.time() - (CACHE_TTL - 60), data)  # errors: retry after a minute
                ok = True
            else:
                ok = _day_summary(client(), key, today) is not None
        except (GarminError, HTTPException):
            pass
        with _bf_guard:
            _bf["queued"].discard(item)
            if not ok:
                _bf["failed"][item] = time.time()  # retried after CACHE_TTL, not on every poll


def _bf_enqueue(items: list[tuple[str, str]]) -> None:
    with _bf_guard:
        now = time.time()
        for it in items:
            if it not in _bf["queued"] and now - _bf["failed"].get(it, 0) > CACHE_TTL:
                _bf["queue"].append(it)
                _bf["queued"].add(it)
        if _bf["queue"] and _bf["thread"] is None:
            _bf["thread"] = threading.Thread(target=_bf_worker, daemon=True, name="chart-backfill")
            _bf["thread"].start()


def _chart_rows(start: date, end: date, need_daysum: bool) -> tuple[dict[str, dict], int]:
    """Day rows for [start, end] from the caches (+ per-day summaries if needed) and how many
    fetches are still pending for them (they're queued, newest first)."""
    today = date.today()
    if demo_mode():
        return demo_rows(start, min(end, today)), 0
    rows, todo = {}, []
    for key in reversed(charts.months_between(start, end)):
        data, fresh = calendar_data.cached(key, _cache_get)
        mem = _month_mem.get(key)
        if data is None and mem and time.time() - mem[0] < CACHE_TTL:
            data, fresh = mem[1], True
        if data is None or not fresh:
            todo.append(("month", key))  # a stale current month is still shown while it refreshes
        for k, row in (data or {}).get("days", {}).items():
            if start.isoformat() <= k <= end.isoformat():
                rows[k] = dict(row)
    if need_daysum:
        for k in sorted(rows, reverse=True):
            ds = _day_summary(None, k, today, fetch=False)
            if ds is None:
                todo.append(("day", k))
            else:
                rows[k].update(ds)
    _bf_enqueue(todo)
    with _bf_guard:
        pending = sum(1 for it in todo if it in _bf["queued"])
    return rows, pending


def _checkin_labels() -> dict[str, str]:
    return {i["id"]: i["label"] for i in checkins.indicators()}


def _chart_types() -> list[dict]:
    """Activity types actually found in your cached months (or the demo data), most frequent first."""
    counts: dict[str, int] = {}
    if demo_mode():
        days = demo_rows(date.today() - timedelta(days=90), date.today()).values()
    else:
        days = [d for f in sorted((CACHE / "calendar").glob("*.json"))
                for d in ((_cache_get("calendar", f.stem) or {}).get("data") or {}).get("days", {}).values()]
    for d in days:
        for a in d.get("activities") or []:
            if a.get("type"):
                counts[a["type"]] = counts.get(a["type"], 0) + 1
    return [dict(type=t, n=n) for t, n in sorted(counts.items(), key=lambda x: -x[1])]


class ChartIn(BaseModel):
    name: str = Field("", max_length=80)
    metric: str = Field(..., max_length=60)
    types: list[str] = Field(default_factory=list, max_length=20)
    exclude_walks: bool = False
    grouping: str = "day"
    agg: str | None = None
    range: str = "30D"
    start: str | None = None
    end: str | None = None
    type: str = "bar"
    size: str = "small"
    goal: dict | None = None


def _clean_chart(c: ChartIn, chart_id: str | None = None) -> dict:
    try:
        return charts.clean(dict(c.model_dump(), id=chart_id), _checkin_labels())
    except ValueError as ex:
        raise HTTPException(422, str(ex))


def _saved_chart(chart_id: str) -> dict:
    c = next((x for x in store.charts() if x["id"] == chart_id), None)
    if c is None:
        raise HTTPException(404, "Chart not found.")
    return c


@app.get("/api/charts")
def list_charts():
    """Saved chart definitions + what the builder can offer (metrics, your activity types, goals)."""
    store.charts_seed([charts.clean(dict(s)) for s in charts.STARTERS])
    labels = _checkin_labels()
    metrics = [dict(key=k, label=m["label"], unit=m["unit"], src=m["src"], agg=m["agg"], aggs=list(m["aggs"]),
                    lower=bool(m.get("lower"))) for k, m in charts.METRICS.items()]
    metrics += [dict(key=f"checkin:{i['id']}", label=i["label"], unit="rating", src="checkin", agg="avg",
                     aggs=list(charts.CHECKIN_METRIC["aggs"]), lower=False)
                for i in checkins.indicators() if i["enabled"] and i["id"] in labels]
    gl = [dict(id=g["id"], type=g["type"], title=g.get("title") or goals.TYPES[g["type"]]["label"], target=g.get("target"),
               fit=charts.GOAL_FITS[g["type"]]) for g in goals.list_goals() if g["type"] in charts.GOAL_FITS and g.get("target") is not None]
    return dict(charts=store.charts(), metrics=metrics, activity_types=_chart_types(), goals=gl,
                ranges=list(charts.RANGES), history_days=charts.HISTORY_DAYS)


@app.post("/api/charts/series")
def chart_series(c: ChartIn):
    """The computed series for a chart definition (saved or not), from cached data only."""
    d, today = _clean_chart(c, "preview"), date.today()
    m = charts.metric_info(d["metric"], _checkin_labels())
    s, e = charts.needed_window(d, today)
    rows, pending = _chart_rows(s, e, m["src"] == "daysum") if m["src"] != "checkin" else ({}, 0)
    cis = {r["day"]: r for r in store.checkins(s.isoformat(), e.isoformat())}
    goal = next((g for g in goals.list_goals() if g["id"] == d["goal"].get("goal_id")), None)
    out = charts.series(d, rows, cis, today, _checkin_labels(), goal)
    with _bf_guard:
        queue = len(_bf["queue"])
    return dict(out, loading=dict(pending=pending, queue=queue) if pending else None)


@app.post("/api/charts")
def create_chart(c: ChartIn):
    d = _clean_chart(c)
    store.chart_insert(d)
    return d


@app.put("/api/charts/order")
def reorder_charts(ids: list[str]):
    store.charts_reorder(ids[:200])
    return dict(charts=store.charts())


@app.put("/api/charts/{chart_id}")
def update_chart(chart_id: str, c: ChartIn):
    _saved_chart(chart_id)
    d = _clean_chart(c, chart_id)
    store.chart_replace(d)
    return d


@app.post("/api/charts/{chart_id}/duplicate")
def duplicate_chart(chart_id: str):
    src = _saved_chart(chart_id)
    d = charts.clean(dict(src, id=None, name=src["name"][:75] + " copy"), _checkin_labels())
    store.chart_insert(d, after=chart_id)
    return d


@app.delete("/api/charts/{chart_id}")
def delete_chart(chart_id: str):
    if not store.chart_delete(chart_id):
        raise HTTPException(404, "Chart not found.")
    return dict(ok=True)


# ------------------------------- weekly review ------------------------------- #
# Built from the same cached day rows as the charts (fetched in the background the first time).
# The coach write-up is cached per week; the coach gets review.coach_payload() only.
EXPORT_MAX = 20 * 1024 * 1024
EXPORT_DIR: Path | None = None  # default ~/Downloads (tests point it elsewhere)
_review_auto: dict = {"running": set()}
_review_guard = threading.Lock()


def _review_week(week: str | None) -> date:
    if week:
        _parse_day(week)
    try:
        return review.parse_week(week, date.today())
    except ValueError as ex:
        raise HTTPException(422, str(ex))


def review_data(monday: date) -> tuple[dict, int]:
    """The review for a week and how many Garmin fetches it still waits on. Records look back a
    year (calendar months only); per-day summaries (resting HR, intensity minutes) cover 5 weeks."""
    today = date.today()
    end = min(monday + timedelta(days=6), today)
    rows, pending = _chart_rows(monday - timedelta(days=review.RECORD_DAYS - 7), end, False)
    short, pending2 = _chart_rows(monday - timedelta(days=max(review.BASELINE_DAYS, 7 * review.PRIOR_WEEKS)), end, True)
    rows.update(short)
    try:
        gv = goals_view()["goals"]
    except Exception:
        gv = []
    ci = store.checkins((monday - timedelta(days=7)).isoformat(), (monday + timedelta(days=6)).isoformat())
    return review.build(rows, ci, checkins.indicators(), gv, monday, today), pending + pending2


def _review_file(monday: date) -> str:
    return ("demo-" if demo_mode() else "") + monday.isoformat()


def _review_key(monday: date) -> str:
    """A finished week's write-up is kept until units, sharing or the Claude key change; a week
    still in progress is rewritten each day."""
    today = date.today()
    done = monday + timedelta(days=6) < today
    return f"{units_system()}:{checkins.sharing()}:{coach.llm_enabled()}:{'done' if done else today.isoformat()}"


def _review_cached(monday: date) -> dict | None:
    hit = _cache_get("weekreview", _review_file(monday))
    return hit["res"] if hit and hit.get("key") == _review_key(monday) else None


def review_coach_text(monday: date, refresh: bool = False) -> dict:
    with _lock(f"review:{monday}"):
        hit = None if refresh else _review_cached(monday)
        if hit:
            return hit
        rv, pending = review_data(monday)
        if pending:
            raise HTTPException(409, "Still loading this week’s data from Garmin; try again in a moment.")
        system = units_system()
        res = dict(source="rules", week=monday.isoformat(), units=system, generated=time.strftime("%Y-%m-%d %H:%M"))
        if coach.llm_enabled():
            payload = review.coach_payload(rv, checkins.sharing())
            try:
                out = ai.cached("review", ai.cache_key("review", payload, coach.memory_hash(), system),
                                lambda: review.claude_review(payload, system, essential=refresh), refresh)
                res.update(text=out["text"], data=out["data"], source="claude")
            except ai.AIError as ex:
                res.update(text=review.rule_review(rv, system), note=f"{ex.info['title']}; showing the rule-based review.")
        else:
            res["text"] = review.rule_review(rv, system)
        if res["source"] == "claude" or not coach.llm_enabled():  # don't pin a fallback caused by an API error
            _cache_put("weekreview", _review_file(monday), dict(key=_review_key(monday), res=res))
        return res


@app.get("/api/review")
def weekly_review(week: str | None = None):
    """Stats for one Monday-Sunday week (default: the last completed one) plus a cached write-up if
    there is one; this never triggers a Claude call by itself."""
    monday = _review_week(week)
    rv, pending = review_data(monday)
    with _bf_guard:
        queue = len(_bf["queue"])
    return dict(rv, coach=_review_cached(monday), llm=coach.llm_enabled(), share=checkins.sharing(),
                last_completed=review.last_completed(date.today()).isoformat(),
                loading=dict(pending=pending, queue=queue) if pending else None)


@app.get("/api/review/coach")
def weekly_review_coach(week: str | None = None, refresh: bool = False):
    return review_coach_text(_review_week(week), refresh)


def _review_auto_worker(monday: date):
    try:
        for _ in range(120):  # wait (up to ~10 min) for the week's history to arrive from Garmin
            try:
                if not review_data(monday)[1]:
                    review_coach_text(monday)
                    return
            except HTTPException:
                pass
            time.sleep(5)
    finally:
        with _review_guard:
            _review_auto["running"].discard(monday)


@app.post("/api/review/auto")
def weekly_review_auto():
    """Called when the app opens: the first open after a week ends writes that week's review."""
    monday = review.last_completed(date.today())
    if _review_cached(monday):
        return dict(week=monday.isoformat(), status="ready")
    with _review_guard:
        if monday in _review_auto["running"]:
            return dict(week=monday.isoformat(), status="running")
        _review_auto["running"].add(monday)
    threading.Thread(target=_review_auto_worker, args=(monday,), daemon=True, name="review-auto").start()
    return dict(week=monday.isoformat(), status="started")


@app.post("/api/review/export")
async def weekly_review_export(week: str = Form(...), fmt: str = Form(...), file: UploadFile = File(...)):
    """Saves a PDF/PNG the page rendered (the desktop window can't download) to ~/Downloads under a
    fixed name, and returns the path."""
    monday = _review_week(week)
    if fmt not in ("pdf", "png"):
        raise HTTPException(422, "Export as pdf or png.")
    data = await file.read(EXPORT_MAX + 1)
    if len(data) > EXPORT_MAX:
        raise HTTPException(413, "That export is too large.")
    if not data.startswith(b"%PDF-" if fmt == "pdf" else b"\x89PNG\r\n\x1a\n"):
        raise HTTPException(422, "That file isn't a valid " + fmt.upper() + ".")
    dest_dir = EXPORT_DIR or Path.home() / "Downloads"
    if not dest_dir.is_dir():
        dest_dir = CACHE / "export"
        dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"weekly-review-{monday}.{fmt}"
    tmp = dest.with_suffix(".tmp")
    tmp.write_bytes(data)
    tmp.replace(dest)
    return dict(path=str(dest), bytes=len(data))


# ------------------------------ workout builder ------------------------------ #
# Structured workouts exported as files for the watch (the Garmin Personal API is read-only, so
# nothing is pushed to Garmin). Files go to the exports folder; the device copy needs a confirm.
WORKOUT_EXPORTS = store.DATA_DIR / "exports" / "workouts"
VOLUMES = Path("/Volumes")  # tests point this at a temp folder
_zones_mem: dict = {}


class WorkoutIn(BaseModel):
    name: str = Field(..., max_length=60)
    sport: Literal["run", "ride", "strength", "timed"]
    notes: str = Field("", max_length=1000)
    scheduled: Day | None = None
    steps: list[dict] = Field(..., max_length=60)


class WorkoutActionIn(BaseModel):
    action: Literal["accept", "dismiss"]


class InstallIn(BaseModel):
    mount: str = Field(..., max_length=300)
    confirm: bool = False


class RevealIn(BaseModel):
    path: str = Field(..., max_length=1000)


def hr_zones(sport: str = "run") -> dict | None:
    """Your heart rate zones from Garmin (biometric-service), cached for a day; demo zones in demo mode."""
    if demo_mode():
        return workouts.heart_zones(workouts.DEMO_ZONES, sport)
    hit = _zones_mem.get("raw")
    if not hit or time.time() - hit[0] > 86400:
        try:
            raw = client().get("/biometric-service/heartRateZones", dict(generateDefaults="true"))
        except (GarminError, HTTPException) as ex:
            log.warning("heart rate zones unavailable: %s", type(ex).__name__)
            return workouts.heart_zones(hit[1], sport) if hit else None
        hit = _zones_mem["raw"] = (time.time(), raw)
    return workouts.heart_zones(hit[1], sport)


def _easy_mps() -> dict:
    """Your typical easy speed per sport from the last 4 weeks (the slower part of your sessions), used
    for time and distance estimates. Defaults when there's no history."""
    today = date.today()
    out = {}
    try:
        rows = _acts_by_day(today - timedelta(days=28), today)
    except HTTPException:
        rows = {}
    for sport in ("run", "ride"):
        sp = sorted(a["dist_m"] / a["secs"] for r in rows.values() for a in r
                    if plans.matches(dict(sport=sport, type="easy"), a) and a.get("dist_m") and a.get("secs"))
        out[sport] = round(sp[len(sp) // 4], 3) if sp else workouts.DEFAULT_EASY_MPS[sport]
    return out


def _wview(w: dict, easy: dict | None = None) -> dict:
    easy = easy or _easy_mps()
    return dict(w, est=workouts.estimate(w, easy.get(w["sport"])))


def _workout(wid: str) -> dict:
    w = store.workout_get(wid)
    if not w:
        raise HTTPException(404, "Workout not found.")
    return w


def _save_workout(body: dict, existing: dict | None = None) -> dict:
    try:
        w = workouts.validate(dict(body, id=existing["id"], created=existing["created"]) if existing else body)
    except workouts.WorkoutError as ex:
        raise HTTPException(422, str(ex))
    store.workout_put(w)
    return _wview(w)


def _intervals_state() -> dict:
    return dict(enabled=bool(store.settings().get("intervals_sync", False)), key_set=intervals.key_set())


@app.get("/api/workouts")
def list_workouts():
    easy = _easy_mps()
    return dict(workouts=[_wview(w, easy) for w in store.workouts()],
                templates=[dict(key=k, **workouts.TEMPLATES[k]) for k in workouts.TEMPLATE_ORDER],
                zones={s: hr_zones(s) for s in ("run", "ride")}, easy_mps=easy, intervals=_intervals_state(),
                export_dir=str(WORKOUT_EXPORTS), today=date.today().isoformat())


@app.post("/api/workouts")
def create_workout(body: WorkoutIn, suggested: bool = False):
    """A new workout. suggested=true marks it as today's (edited) coach suggestion."""
    w = body.model_dump(mode="json")
    if suggested:
        w["source"] = dict(suggested=date.today().isoformat())
    return _save_workout(w)


@app.get("/api/workouts/suggest")
def workout_suggestion():
    """Today's suggested workout from readiness (and today's plan session, if any). Nothing is saved
    until you accept; dismissing hides it for the rest of the day."""
    today = date.today()
    if store.settings().get("workout_suggest_dismissed") == today.isoformat():
        return dict(suggestion=None, why="dismissed")
    if any((w.get("source") or {}).get("suggested") == today.isoformat() for w in store.workouts()):
        return dict(suggestion=None, why="accepted")
    acts = _acts_by_day(today, today)
    plan_today, sport = [], None
    for e in store.events():
        if e["date"] < today.isoformat():
            continue
        sport = sport or workouts.PLAN_SPORT.get(e["kind"], "run")
        p = store.plan_get(e["id"])
        if not p:
            continue
        st = plans.statuses(p, acts, today)
        plan_today += [(e, w) for w in p["workouts"] if w["date"] == today.isoformat() and st[w["id"]]["state"] != "done"]
    s = workouts.suggest_today(_assessment(), today, sport or "run", plan_today)
    if s:
        s["workout"] = _wview(s["workout"])
    return dict(suggestion=s)


@app.post("/api/workouts/suggest")
def act_on_suggestion(body: WorkoutActionIn):
    today = date.today().isoformat()
    if body.action == "dismiss":
        store.settings_put(dict(workout_suggest_dismissed=today))
        return dict(ok=True)
    s = workout_suggestion()["suggestion"]  # worked out again here, never taken from the page
    if not s:
        raise HTTPException(409, "There's no suggestion to accept today.")
    w = {k: v for k, v in s["workout"].items() if k != "est"}
    w["source"] = dict(w.get("source") or {}, suggested=today)
    store.workout_put(w)
    return _wview(w)


@app.post("/api/workouts/from-plan/{eid}/{pwid}")
def workout_from_plan(eid: str, pwid: str):
    """The plan session as a structured workout (made once; later calls open the same one)."""
    e, p = _event(eid), _plan(eid)
    pw = next((x for x in p["workouts"] if x["id"] == pwid), None)
    if not pw:
        raise HTTPException(404, "Workout not found.")
    for w in store.workouts():
        if (w.get("source") or {}).get("workout_id") == pwid:
            return _wview(w)
    w = workouts.from_plan(pw, e)
    store.workout_put(w)
    return _wview(w)


@app.get("/api/workouts/{wid}")
def get_workout(wid: str):
    return _wview(_workout(wid))


@app.put("/api/workouts/{wid}")
def put_workout(wid: str, body: WorkoutIn):
    old = _workout(wid)
    return _save_workout(dict(body.model_dump(mode="json"), source=old.get("source")), old)


@app.delete("/api/workouts/{wid}")
def delete_built_workout(wid: str):
    if not store.workout_delete(wid):
        raise HTTPException(404, "Workout not found.")
    return dict(ok=True)


@app.post("/api/workouts/{wid}/duplicate")
def duplicate_workout(wid: str):
    w = _workout(wid)
    return _save_workout(dict(w, id=None, created=None, source=None, name=(w["name"][:53] + " (copy)")))


def _export(w: dict, fmt: str) -> Path:
    try:
        data = workout_export.to_fit(w) if fmt == "fit" else workout_export.to_tcx(w)
    except workout_export.ExportError as ex:
        raise HTTPException(422, str(ex))
    WORKOUT_EXPORTS.mkdir(parents=True, exist_ok=True)
    dest = WORKOUT_EXPORTS / workout_export.filename(w, fmt)
    tmp = dest.with_suffix(".tmp")
    tmp.write_bytes(data)
    tmp.replace(dest)
    return dest


@app.post("/api/workouts/{wid}/export")
def export_workout(wid: str, fmt: Literal["fit", "tcx"] = "fit"):
    """Writes the file (verified by reading it back) to the local exports folder."""
    dest = _export(_workout(wid), fmt)
    health.event("info", "Workouts", f"Exported {dest.name}")
    return dict(path=str(dest), name=dest.name, bytes=dest.stat().st_size, folder=str(WORKOUT_EXPORTS))


@app.post("/api/workouts/reveal")
def reveal_export(body: RevealIn):
    """Shows an exported file in Finder (only files inside the exports folder)."""
    path = Path(body.path).resolve()
    if WORKOUT_EXPORTS.resolve() not in path.parents or not path.is_file():
        raise HTTPException(404, "That file isn't in the workout exports folder.")
    import subprocess
    try:
        subprocess.run(["open", "-R", str(path)], timeout=5, check=False, capture_output=True)
    except (OSError, subprocess.SubprocessError):
        raise HTTPException(500, "Couldn't open Finder.")
    return dict(ok=True)


@app.get("/api/devices")
def list_devices():
    return dict(devices=workout_export.find_devices(VOLUMES))


@app.post("/api/workouts/{wid}/install")
def install_workout(wid: str, body: InstallIn):
    """Copies the .fit to a mounted Garmin. Without confirm, only says where it would go."""
    w = _workout(wid)
    dev = next((d for d in workout_export.find_devices(VOLUMES) if d["mount"] == body.mount), None)
    if not dev:
        raise HTTPException(404, "That device isn't connected any more.")
    src = _export(w, "fit")
    try:
        r = workout_export.install(src, dev, body.confirm)
    except workout_export.ExportError as ex:
        raise HTTPException(422, str(ex))
    except OSError as ex:
        raise HTTPException(422, f"Couldn't copy to the watch ({type(ex).__name__}).")
    if r["copied"]:
        health.event("info", "Workouts", f"Copied {Path(r['path']).name} to {dev['name']}")
    return dict(r, device=dev)


@app.post("/api/workouts/{wid}/intervals")
def sync_intervals(wid: str):
    w = _workout(wid)
    if not _intervals_state()["enabled"]:
        raise HTTPException(409, "Intervals.icu sync is off (Settings).")
    try:
        r = intervals.push(w, workout_export.to_fit(w), w.get("scheduled") or date.today().isoformat())
    except (intervals.IntervalsError, workout_export.ExportError) as ex:
        health.event("warn", "Intervals.icu", str(ex))
        raise HTTPException(502, str(ex))
    health.event("info", "Intervals.icu", f"Synced {w['name']} for {r['day']}")
    return r


COMPARE_PROBE = {"run": [dict(sport="run", type="easy")], "ride": [dict(sport="ride", type="easy")],
                 "strength": [dict(sport="general", type="strength")],
                 "timed": [dict(sport="tennis", type="drills"), dict(sport="general", type="cardio")]}


@app.get("/api/workouts/{wid}/compare")
def compare_workout(wid: str):
    """Planned vs actual: the activity on the workout's day that matches its sport."""
    w = _workout(wid)
    day, today = w.get("scheduled"), date.today()
    if not day:
        return dict(state="unscheduled")
    if day > today.isoformat():
        return dict(state="upcoming", day=day)
    acts = _acts_by_day(date.fromisoformat(day), date.fromisoformat(day)).get(day) or []
    est = workouts.estimate(w, _easy_mps().get(w["sport"]))
    hits = [a for a in acts if any(plans.matches(p, a) for p in COMPARE_PROBE[w["sport"]])]
    if not hits:
        return dict(state="not_done", day=day)
    a = min(hits, key=lambda x: abs((x.get("secs") or 0) - est["sec"]))
    zones = hr_zones(w["sport"])
    if demo_mode():
        view = workouts.demo_activity(w, day, zones)
    else:
        try:
            view = activity_view(int(a["id"]))
        except (HTTPException, TypeError, ValueError):
            return dict(state="no_detail", day=day, activity={k: a.get(k) for k in ("id", "name", "type", "dist_m", "secs")})
    return dict(state="done", day=day, zones=zones, **workouts.compare(w, view, zones))


# ----------------------------- personal records ------------------------------ #
# The full activity + daily-health history is synced in the background (all pages the first time,
# then only what's new) and cached in .cache/records/. Records are computed from the cleaned copy.
_rec: dict = {"state": "idle", "phase": None, "done": 0, "total": None, "error": None}
_rec_guard = threading.Lock()
_rec_mem: dict = {"key": None, "clean": None, "all": None}


def _rec_file() -> str:
    return ("demo-" if demo_mode() else "") + "history"


def records_history() -> dict | None:
    if demo_mode():
        hit = _rec_mem.get("demo")
        if not hit or hit["days_through"] != date.today().isoformat():
            hit = _rec_mem["demo"] = records_sync.demo_history(date.today())
        return hit
    return records_sync.upgrade(_cache_get("records", _rec_file()))


def records_clean() -> tuple | None:
    """(activities, days, ignored, skipped, history) cleaned once per synced version."""
    h = records_history()
    if not h or h.get("v") != records_sync.VERSION:
        return None
    key = (demo_mode(), h.get("synced"), len(h["activities"]), date.today())
    if _rec_mem["key"] != key:
        acts, days, ignored, skipped = records.clean(h["activities"], h["days"])
        if skipped:
            log.warning("records: skipped %d activities that couldn't be processed (first: %s)", len(skipped), skipped[0]["reason"])
        _rec_mem.update(key=key, clean=(acts, days, ignored, skipped, h), all=records.compute(acts, days, date.today()))
    return _rec_mem["clean"]


def _rec_worker(full: bool):
    def progress(**p):
        with _rec_guard:
            _rec.update(p)
    try:
        if not demo_mode():
            h = records_sync.sync(client().get, None if full else _cache_get("records", _rec_file()), date.today(), full, progress)
            _cache_put("records", _rec_file(), h)
        with _rec_guard:
            _rec.update(state="done", phase=None, error=None)
    except (GarminError, HTTPException) as ex:
        with _rec_guard:
            _rec.update(state="error", phase=None, error=str(getattr(ex, "detail", ex))[:200])
    except Exception as ex:  # a bug must not leave the sync "running" forever
        log.error("records sync failed", exc_info=ex)
        with _rec_guard:
            _rec.update(state="error", phase=None, error=f"{type(ex).__name__} at {where(ex)}; details in logs/readiness_coach.log")


def records_status() -> dict:
    h = records_history()
    with _rec_guard:
        s = {k: _rec[k] for k in ("state", "phase", "done", "total", "error")}
    skipped = (_rec_mem["clean"] or (None,) * 4)[3] if _rec_mem["key"] and h and _rec_mem["key"][1] == h.get("synced") else None
    return dict(s, synced=h and h.get("synced"), activities=len(h["activities"]) if h else 0, failed=(h or {}).get("failed") or [],
                failed_unknown=(h or {}).get("failed_unknown", 0), skipped=len(skipped) if skipped is not None else None, first_sync=not h)


@app.post("/api/records/sync")
def records_sync_start(full: bool = False):
    """Starts a background sync (incremental unless full=true); safe to call on every app open."""
    with _rec_guard:
        if _rec["state"] != "running":
            _rec.update(state="running", phase="activities", done=0, total=None, error=None)
            threading.Thread(target=_rec_worker, args=(full,), daemon=True, name="records-sync").start()
    return records_status()


@app.get("/api/records/status")
def records_sync_status():
    return records_status()


@app.get("/api/records")
def personal_records(type: str = "", period: str = "all"):
    if period not in records.PERIODS:
        raise HTTPException(422, "Period is all, year or 12m.")
    if type and type not in records.FAMILIES:
        raise HTTPException(422, "Unknown activity type.")
    try:
        data = records_clean()
        if not data:
            return dict(records=None, status=records_status())
        acts, days, ignored, skipped, _ = data
        today = date.today()
        return dict(records=records.compute(acts, days, today, period, type or None), progress=records.progress(acts, today, period),
                    types=records.types(acts), ignored=ignored, skipped=skipped, period=period, type=type or None,
                    status=records_status(), indoor_runs=sum(1 for a in acts if records.is_indoor_run(a.get("type"))))
    except Exception as ex:
        log.error("GET /api/records failed", exc_info=ex)
        raise HTTPException(500, f"Couldn't work out your records ({type(ex).__name__} at {where(ex)}). "
                                 "Your synced history is safe; the details are in logs/readiness_coach.log.")


@app.get("/api/records/badges")
def personal_record_badges():
    """Activity id -> records it holds now (all time, all types); from the cache only."""
    data = records_clean()
    return dict(badges=records.badges(data[0], data[1], date.today()) if data else {})


def _seen_key() -> str:
    return "records_seen_demo" if demo_mode() else "records_seen"


@app.get("/api/records/new")
def personal_records_new():
    """Records set since they were last seen (for a one-time toast). The first time, everything
    current is marked seen instead, so a first sync never floods you with toasts."""
    if not records_clean():
        return dict(new=[])
    seen = store.settings().get(_seen_key())
    if not seen:
        store.settings_put({_seen_key(): records.seen_state(_rec_mem["all"])})
        return dict(new=[])
    return dict(new=records.new_records(_rec_mem["all"], seen, date.today()))


@app.post("/api/records/seen")
def personal_records_seen():
    if records_clean():
        store.settings_put({_seen_key(): records.seen_state(_rec_mem["all"])})
    return dict(ok=True)


# ------------------------------ correlation explorer ------------------------------ #
# Two daily measures, same day or lagged, from the same cached rows as the charts (a year at most).
# The coach explanation gets correlate.coach_payload() only, and never check-ins unless sharing is on.
class CompareIn(BaseModel):
    x: str = Field(..., max_length=60)
    y: str = Field(..., max_length=60)
    lag: int = Field(0, ge=0, le=3)
    range: Literal["90D", "6M", "1Y"] = "1Y"


def _insight_measures(*keys: str) -> list[dict]:
    labels = _checkin_labels()
    ms = [correlate.measure(k, labels) for k in keys]
    if any(m is None for m in ms):
        raise HTTPException(422, "Pick two measures from the list.")
    return ms


def _insight_series(ms: list[dict], days: int, lag: int) -> tuple[dict[str, dict], int]:
    """key -> {day: value} over the range (X reaches `lag` days further back), and pending fetches."""
    today = date.today()
    start = today - timedelta(days=days - 1 + lag)
    rows, pending = _chart_rows(start, today, correlate.needs_daysum(*ms))
    cis = {r["day"]: r for r in store.checkins(start.isoformat(), today.isoformat())}
    labels = _checkin_labels()
    return {m["key"]: correlate.values(m, rows, cis, labels) for m in ms}, pending


def _insight(c: CompareIn) -> dict:
    if c.x == c.y and c.lag == 0:
        raise HTTPException(422, "Pick two different measures (or the same one with a lag).")
    xm, ym = _insight_measures(c.x, c.y)
    series, pending = _insight_series([xm, ym], correlate.RANGES[c.range], c.lag)
    r = correlate.analyze(xm, ym, series[xm["key"]], series[ym["key"]], c.lag)
    with _bf_guard:
        queue = len(_bf["queue"])
    return dict(r, range=c.range, loading=dict(pending=pending, queue=queue) if pending else None)


@app.get("/api/insights/options")
def insight_options():
    return dict(measures=correlate.options([t["type"] for t in _chart_types()], checkins.indicators()), lags=list(correlate.LAGS),
                ranges=list(correlate.RANGES), min_days=correlate.MIN_DAYS, llm=coach.llm_enabled(), share=checkins.sharing(),
                reminder=correlate.REMINDER)


@app.post("/api/insights/compare")
def insight_compare(c: CompareIn):
    return _insight(c)


@app.get("/api/insights/top")
def insight_top(refresh: bool = False):
    """Curated pairs that clear the bar, recomputed at most once a day (cached until tomorrow)."""
    key = ("demo-" if demo_mode() else "") + f"top-{date.today()}"
    hit = None if refresh else _cache_get("insights", key)
    if hit:
        return hit
    labels = _checkin_labels()
    ms = {k: m for k in {k for x, y, _ in correlate.TOP_PAIRS for k in (x, y)} if (m := correlate.measure(k, labels))}
    series, pending = _insight_series(list(ms.values()), correlate.RANGES["1Y"], max(correlate.LAGS))
    res = dict(patterns=correlate.top(series, ms), checked=sum(1 for x, y, _ in correlate.TOP_PAIRS if x in ms and y in ms),
               computed=time.strftime("%Y-%m-%d %H:%M"), loading=dict(pending=pending) if pending else None)
    if not pending:  # a partial history isn't pinned for the day
        _cache_put("insights", key, res)
    return res


@app.post("/api/insights/explain")
def insight_explain(c: CompareIn):
    """The coach's plain-language take and one experiment, from derived statistics only."""
    xm, ym = _insight_measures(c.x, c.y)
    if (xm.get("checkin") or ym.get("checkin")) and not checkins.sharing():
        raise HTTPException(403, "This compares a check-in rating, and sharing check-ins with the coach is off "
                                 "(Settings → Check-ins). Nothing was sent.")
    if not coach.llm_enabled():
        raise HTTPException(409, "Add ANTHROPIC_API_KEY to .env to get the coach’s explanation.")
    r = _insight(c)
    if r["status"] != "ok":
        raise HTTPException(409, r["message"])
    key = hashlib.sha1(json.dumps([correlate.coach_payload(r), units_system(), date.today().isoformat()], sort_keys=True,
                                  default=str).encode()).hexdigest()[:20]
    hit = _cache_get("insights", f"explain-{key}")
    if hit:
        return hit
    try:
        res = dict(text=correlate.explain(r, units_system()), generated=time.strftime("%Y-%m-%d %H:%M"))
    except ai.AIError as ex:
        raise HTTPException(502, f"The coach couldn’t answer right now: {ex.info['title']}. {ex.info.get('fix') or ''}".strip())
    _cache_put("insights", f"explain-{key}", res)
    return res


# ------------------------------ training: events + plans ------------------------------ #
# The plan's structure comes from plans.py rules; the coach only writes the overview (from derived
# numbers; the injury note text never leaves this computer). Suggestions never change a plan by themselves.
class EventIn(BaseModel):
    name: str = Field(..., min_length=1, max_length=80)
    date: date
    kind: Literal["run", "ride", "tennis", "general"]
    category: Literal["race", "tournament", "trip", "challenge"] = "race"
    distance_m: float | None = Field(None, gt=0, le=1_000_000)
    priority: Literal["A", "B", "C"] = "B"
    notes: str = Field("", max_length=300)


class TrainingSettingsIn(BaseModel):
    days: int | None = Field(None, ge=3, le=6)
    long_day: int | None = Field(None, ge=0, le=6)
    injured: bool | None = None
    injury_note: str | None = Field(None, max_length=300)


Day = date  # a field named `date` with a default would shadow the type in its own annotation (Python 3.14)


class WorkoutPatch(BaseModel):
    date: Day | None = None
    type: str | None = Field(None, max_length=20)
    title: str | None = Field(None, max_length=60)
    intensity: Literal["easy", "moderate", "hard", "race"] | None = None
    distance_m: float | None = Field(None, ge=0, le=500_000)
    minutes: float | None = Field(None, ge=0, le=600)
    note: str | None = Field(None, max_length=200)
    status: Literal["planned", "skipped"] | None = None


class SuggestionIn(BaseModel):
    event_id: str
    workout_id: str
    action: Literal["easier", "move", "skip", "dismiss", "reschedule", "dismiss_missed"]


def training_settings() -> dict:
    s = store.settings()
    return dict(days=s.get("training_days", 4), long_day=s.get("training_long_day", 5), injured=bool(s.get("training_injured")),
                injury_note=s.get("training_injury_note", ""))


def training_inputs(kind: str) -> dict:
    """Recent volume for this sport from Garmin: weekly average over 4 weeks and the longest single
    session over 6 (km for run/ride, minutes otherwise)."""
    today = date.today()
    rows, pending = _chart_rows(today - timedelta(days=42), today - timedelta(days=1), False)
    probe = dict(sport=kind, type="easy")
    amount = (lambda a: (a.get("dist_m") or 0) / 1000) if kind in ("run", "ride") else (lambda a: (a.get("secs") or 0) / 60)
    recent, longest = 0.0, 0.0
    for k, r in rows.items():
        for a in r.get("activities") or []:
            if plans.matches(probe, a):
                longest = max(longest, amount(a))
                if k >= (today - timedelta(days=28)).isoformat():
                    recent += amount(a)
    sport = {"run": "running", "ride": "cycling", "tennis": "tennis and racket sports", "general": "workouts"}[kind]
    return dict(current_weekly=round(recent / 4, 1), longest_recent=round(longest, 1), loading=bool(pending),
                basis=f"your last 4 weeks of {sport} from Garmin ({len(rows)} days loaded)")


def _event(eid: str) -> dict:
    e = store.event_get(eid)
    if not e:
        raise HTTPException(404, "Event not found.")
    return e


def _plan(eid: str) -> dict:
    p = store.plan_get(eid)
    if not p:
        raise HTTPException(404, "This event has no plan yet.")
    return p


def _acts_by_day(start: date, end: date) -> dict[str, list[dict]]:
    if end < start:
        return {}
    rows, _ = _chart_rows(start, end, False)
    return {k: r.get("activities") or [] for k, r in rows.items()}


def _overview(p: dict, e: dict, warnings: list[dict]) -> dict:
    if coach.llm_enabled():
        try:
            return dict(source="claude", text=plans.claude_overview(p, e, warnings, units_system()))
        except ai.AIError as ex:
            return dict(source="rules", text=plans.rule_overview(p, e, units_system()), note=f"{ex.info['title']}.")
    return dict(source="rules", text=plans.rule_overview(p, e, units_system()))


def _inputs_for(e: dict) -> dict:
    s = training_settings()
    return dict(training_inputs(e["kind"]), days=s["days"], long_day=s["long_day"], injured=s["injured"])


def plan_view(eid: str) -> dict:
    e, p, today = _event(eid), _plan(eid), date.today()
    start = date.fromisoformat(p["weeks"][0]["start"]) if p["weeks"] else today
    acts = _acts_by_day(start, today)
    st = plans.statuses(p, acts, today)
    totals = plans.week_totals(p)
    done = [0.0] * len(totals)
    idx = {w["start"]: w["index"] for w in p["weeks"]}
    for w in p["workouts"]:
        a = st[w["id"]].get("activity")
        k = plans.monday(date.fromisoformat(w["date"])).isoformat()
        if a and k in idx:
            done[idx[k]] += (a.get("dist_m") or 0) / 1000 if p["unit"] == "km" else (a.get("secs") or 0) / 60
    others = [(x["name"], q) for x in store.events() if x["id"] != eid and x["date"] >= today.isoformat() and (q := store.plan_get(x["id"]))]
    built = {(w.get("source") or {}).get("workout_id"): w["id"] for w in store.workouts() if (w.get("source") or {}).get("event_id") == eid}
    return dict(event=e, plan=p, statuses=st, warnings=plans.check(p, e, today, units_system(), plans.overlaps(p, others, today)),
                totals=totals, done=[round(x, 1) for x in done], today=today.isoformat(), built={k: v for k, v in built.items() if k})


@app.get("/api/training/settings")
def get_training_settings():
    return training_settings()


@app.put("/api/training/settings")
def put_training_settings(s: TrainingSettingsIn):
    names = dict(days="training_days", long_day="training_long_day", injured="training_injured", injury_note="training_injury_note")
    store.settings_put({names[k]: getattr(s, k) for k in s.model_fields_set})
    return training_settings()


@app.get("/api/events")
def list_events():
    today = date.today().isoformat()
    out = []
    for e in store.events():
        p = store.plan_get(e["id"])
        out.append(dict(e, days_left=(date.fromisoformat(e["date"]) - date.today()).days, past=e["date"] < today, has_plan=bool(p)))
    return dict(events=out, settings=training_settings())


def _event_from(body: EventIn, eid: str | None = None) -> dict:
    if body.kind in ("run", "ride") and body.category == "race" and not body.distance_m:
        raise HTTPException(422, "Add the race distance.")
    d = body.model_dump(mode="json")
    d["name"] = d["name"].strip()
    if body.kind in ("tennis", "general"):
        d["distance_m"] = None
    return dict(d, id=eid or uuid.uuid4().hex[:10])


@app.post("/api/events")
def create_event(body: EventIn):
    if body.date < date.today():
        raise HTTPException(422, "Pick today or a future date.")
    e = _event_from(body)
    store.event_put(e)
    return e


@app.put("/api/events/{eid}")
def update_event(eid: str, body: EventIn):
    """Saves the event; if the date (or distance) changed and it has a plan, the rest of the plan is
    rebuilt from today (past workouts are kept)."""
    old = _event(eid)
    e = _event_from(body, eid)
    store.event_put(e)
    p = store.plan_get(eid)
    regenerated = False
    if p and (old["date"] != e["date"] or old.get("distance_m") != e.get("distance_m")) and e["priority"] in ("A", "B") and e["date"] >= date.today().isoformat():
        p = plans.regenerate(p, e, _inputs_for(e), date.today())
        p["overview"] = _overview(p, e, plans.check(p, e, date.today(), units_system()))
        store.plan_put(eid, p)
        regenerated = True
    return dict(e, regenerated=regenerated)


@app.delete("/api/events/{eid}")
def delete_event(eid: str):
    if not store.event_delete(eid):
        raise HTTPException(404, "Event not found.")
    return dict(ok=True)


@app.get("/api/events/{eid}/inputs")
def plan_inputs(eid: str):
    """What the generator would use (recent volume from Garmin + your settings), before generating."""
    e = _event(eid)
    return dict(_inputs_for(e), injury_note=training_settings()["injury_note"])


@app.post("/api/events/{eid}/plan")
def create_plan(eid: str):
    e = _event(eid)
    try:
        p = plans.generate(e, _inputs_for(e), date.today())
    except ValueError as ex:
        raise HTTPException(422, str(ex))
    p["overview"] = _overview(p, e, plans.check(p, e, date.today(), units_system()))
    store.plan_put(eid, p)
    return plan_view(eid)


@app.post("/api/events/{eid}/plan/regenerate")
def regenerate_plan(eid: str):
    e, p = _event(eid), _plan(eid)
    try:
        p = plans.regenerate(p, e, _inputs_for(e), date.today())
    except ValueError as ex:
        raise HTTPException(422, str(ex))
    p["overview"] = _overview(p, e, plans.check(p, e, date.today(), units_system()))
    store.plan_put(eid, p)
    return plan_view(eid)


@app.delete("/api/events/{eid}/plan")
def delete_plan(eid: str):
    if not store.plan_delete(eid):
        raise HTTPException(404, "This event has no plan.")
    return dict(ok=True)


@app.get("/api/plan/{eid}")
def get_plan(eid: str):
    return plan_view(eid)


@app.patch("/api/plan/{eid}/workouts/{wid}")
def patch_workout(eid: str, wid: str, body: WorkoutPatch):
    e, p = _event(eid), _plan(eid)
    ch = {k: (v.isoformat() if isinstance(v, date) else v) for k, v in body.model_dump().items() if k in body.model_fields_set}
    if "date" in ch and not (p["weeks"][0]["start"] <= ch["date"] <= e["date"]):
        raise HTTPException(422, "Move it to a day between the plan's start and the event.")
    try:
        plans.edit(p, wid, ch)
    except KeyError:
        raise HTTPException(404, "Workout not found.")
    store.plan_put(eid, p)
    return plan_view(eid)


@app.delete("/api/plan/{eid}/workouts/{wid}")
def delete_workout(eid: str, wid: str):
    _event(eid)
    p = _plan(eid)
    before = len(p["workouts"])
    p["workouts"] = [w for w in p["workouts"] if w["id"] != wid]
    if len(p["workouts"]) == before:
        raise HTTPException(404, "Workout not found.")
    store.plan_put(eid, p)
    return plan_view(eid)


def _assessment() -> dict:
    try:
        return get_data()[1]
    except HTTPException:
        return {}


def _today_bundle() -> dict:
    """Upcoming events with countdowns, today's planned workouts with a readiness suggestion, and
    missed workouts from the last week (with a sensible reschedule day, if any)."""
    today = date.today()
    a = _assessment()
    evs, todays, missed = [], [], []
    acts = _acts_by_day(today - timedelta(days=7), today)
    for e in store.events():
        if e["date"] < today.isoformat():
            continue
        p = store.plan_get(e["id"])
        evs.append(dict(e, days_left=(date.fromisoformat(e["date"]) - today).days, has_plan=bool(p)))
        if not p:
            continue
        st = plans.statuses(p, acts, today)
        ev_day = date.fromisoformat(e["date"])
        for w in p["workouts"]:
            if w["date"] == today.isoformat():
                todays.append(dict(event=dict(id=e["id"], name=e["name"]), workout=w, state=st[w["id"]]["state"],
                                   suggestion=None if st[w["id"]]["state"] == "done" else plans.suggest(w, a, p, ev_day, today)))
        missed += [dict(m, event=dict(id=e["id"], name=e["name"])) for m in plans.missed(p, st, ev_day, today)]
    return dict(events=sorted(evs, key=lambda x: x["date"]), today=todays, missed=missed,
                readiness=dict(score=a.get("readiness"), zone=a.get("zone"), flags=a.get("flags") or []))


@app.get("/api/training/today")
def training_today():
    return _today_bundle()


@app.post("/api/training/act")
def training_act(body: SuggestionIn):
    """Accept or dismiss a suggestion, or handle a missed workout. The change is worked out again
    here (never trusted from the page), and only the one workout changes."""
    e, p, today = _event(body.event_id), _plan(body.event_id), date.today()
    w = next((x for x in p["workouts"] if x["id"] == body.workout_id), None)
    if not w:
        raise HTTPException(404, "Workout not found.")
    ev_day = date.fromisoformat(e["date"])
    if body.action == "dismiss":
        w["dismissed"] = today.isoformat()
    elif body.action == "dismiss_missed":
        w["dismissed_missed"] = True
    elif body.action == "reschedule":
        st = plans.statuses(p, _acts_by_day(today - timedelta(days=7), today), today)
        m = next((x for x in plans.missed(p, st, ev_day, today) if x["workout"]["id"] == w["id"]), None)
        if not m or not m["reschedule"]:
            raise HTTPException(409, "There's no good free day left this week; skipping it is the safer choice.")
        plans.edit(p, w["id"], dict(date=m["reschedule"]), reason=f"rescheduled from {w['date']}")
    elif body.action == "skip" and w["date"] < today.isoformat():
        plans.edit(p, w["id"], dict(status="skipped"), reason="missed")
    else:
        s = plans.suggest(w, _assessment(), p, ev_day, today)
        opt = next((o for o in (s or {}).get("options", []) if o["action"] == body.action), None)
        if not opt:
            raise HTTPException(409, "That suggestion no longer applies.")
        plans.edit(p, w["id"], opt["changes"], reason=f"readiness {s['text'].split('.')[0].lower()}")
    store.plan_put(body.event_id, p)
    return _today_bundle()


def training_overlay(start: str, end: str, days: dict[str, dict]) -> tuple[dict, dict]:
    """day -> planned workouts (with done / missed / planned state) and day -> events, for the calendar."""
    today, planned, evs = date.today(), {}, {}
    for e in store.events():
        if start <= e["date"] <= end:
            evs.setdefault(e["date"], []).append(dict(id=e["id"], name=e["name"], kind=e["kind"], priority=e["priority"]))
        p = store.plan_get(e["id"])
        if not p:
            continue
        inside = [w for w in p["workouts"] if start <= w["date"] <= end]
        st = plans.statuses(dict(p, workouts=inside), {k: (r or {}).get("activities") or [] for k, r in days.items()}, today)
        for w in inside:
            planned.setdefault(w["date"], []).append(dict(w, event_id=e["id"], event=e["name"], state=st[w["id"]]["state"]))
    return planned, evs


# --------------------------------- strength ---------------------------------- #
# Programs, the guided workout (autosaved on every tap), the log and progression. All loads are kg;
# targets are snapped to weights your bar/plates/dumbbells can make in your display unit.
_notify: dict = {"timer": None}


def st_settings() -> dict:
    return strength.settings_with_defaults(store.settings().get("strength"))


def st_unit() -> str:
    return strength.unit_of(units_system())


def st_custom() -> list[dict]:
    return store.strength_exercises()


def st_library() -> list[dict]:
    return strength_lib.LIBRARY + [dict(c, custom=True) for c in st_custom()]


def _st_program(pid: str) -> dict:
    p = store.strength_program_get(pid)
    if not p:
        raise HTTPException(404, "That program doesn't exist (any more).")
    return p


def _st_states(p: dict, sessions: list[dict] | None = None) -> dict:
    return strength.lift_states(p, store.strength_sessions() if sessions is None else sessions, st_settings(), st_unit(), st_custom())


def _st_program_view(p: dict, sessions: list[dict], today: date) -> dict:
    states = _st_states(p, sessions)
    return dict(p, next=strength.next_workout(p, sessions, today), states=states,
                sessions=sum(1 for x in sessions if x.get("program_id") == p["id"]))


def _sore_note(today: date) -> str | None:
    """A gentle note if soreness or pain was tagged in a check-in in the last two days."""
    rows = store.checkins((today - timedelta(days=2)).isoformat(), today.isoformat())
    tags = {t.lower() for r in rows for t in r.get("tags") or []}
    if any(any(w in t for w in ("sore", "pain", "injur", "hurt")) for t in tags):
        return ("You noted soreness or pain in a recent check-in. Go lighter if anything twinges, and stop and skip "
                "any lift that hurts; that's not something to push through.")
    return None


def _near_match(day: date) -> bool:
    """A tennis event, or a planned tennis session, within a day of `day`."""
    lo, hi = (day - timedelta(days=1)).isoformat(), (day + timedelta(days=1)).isoformat()
    if any(e.get("kind") == "tennis" and lo <= e.get("date", "") <= hi for e in store.events()):
        return True
    return any(w.get("sport") == "tennis" and w.get("type") not in ("strength", "mobility", "rest") and lo <= w.get("date", "") <= hi
               for pl in store.plans() for w in pl.get("workouts") or [])


def _bodyweight_series() -> list[dict]:
    """Bodyweight as a 7-day average (single weigh-ins swing too much to read next to strength)."""
    if demo_mode():
        today = date.today()
        raw = [dict(date=k, kg=r["weight_kg"]) for k, r in sorted(demo_rows(today - timedelta(days=365), today).items()) if r.get("weight_kg")]
        return [dict(date=r["date"], kg=round(sum(x["kg"] for x in raw[max(0, i - 6):i + 1]) / len(raw[max(0, i - 6):i + 1]), 2))
                for i, r in enumerate(raw)]
    try:
        return [dict(date=r["date"], kg=r.get("kg_ma") or r["kg"]) for r in body_data().get("series") or []]
    except Exception:
        return []


def _lean_series() -> list[dict]:
    return [dict(date=x["measured_date"], kg=dexa.totals(x).get("lean_kg")) for x in store.dexa_scans() if dexa.totals(x).get("lean_kg")]


@app.get("/api/strength")
def strength_overview():
    """Everything the Training › Programs page needs, in one call."""
    today, sessions = date.today(), store.strength_sessions()
    progs = [_st_program_view(p, sessions, today) for p in store.strength_programs()]
    return dict(programs=progs, active=store.strength_active(), settings=st_settings(), unit=st_unit(),
                templates=[dict(key=k, name=t["name"], description=t["description"]) for k, t in strength_lib.TEMPLATES.items()],
                library=st_library(), muscles=strength_lib.MUSCLES, sessions=len(sessions))


class StSettings(BaseModel):
    bar_lb: float | None = Field(None, ge=5, le=100)
    bar_kg: float | None = Field(None, ge=2, le=50)
    plates_lb: list[list[float]] | None = None
    plates_kg: list[list[float]] | None = None
    db_step_lb: float | None = Field(None, gt=0, le=25)
    db_step_kg: float | None = Field(None, gt=0, le=10)
    machine_step_lb: float | None = Field(None, gt=0, le=50)
    machine_step_kg: float | None = Field(None, gt=0, le=25)
    rest_s: int | None = Field(None, ge=0, le=900)
    rest_fail_s: int | None = Field(None, ge=0, le=900)
    sound: bool | None = None
    notify: bool | None = None
    warmups: bool | None = None
    safety_dismissed: bool | None = None
    standards: Literal["men", "women", "off"] | None = None
    age: int | None = Field(None, ge=13, le=100)
    overlay_weight: bool | None = None
    overlay_lean: bool | None = None


@app.put("/api/strength/settings")
def strength_settings_put(body_: StSettings):
    cur = st_settings()
    for k, v in body_.model_dump(exclude_none=True).items():
        if k.startswith("plates_"):
            v = [[float(w), int(n)] for w, n in v if 0 < w <= 100 and 0 <= n <= 20][:12]
            if not v:
                raise HTTPException(422, "Keep at least one plate size.")
        cur[k] = None if v == "off" else v
    store.settings_put({"strength": cur})
    return cur


class StExercise(BaseModel):
    name: str = Field(min_length=1, max_length=60)
    load: Literal["barbell", "dumbbell", "machine", "bodyweight"]
    primary: list[str] = Field(default_factory=list)
    secondary: list[str] = Field(default_factory=list)
    rest_s: int = Field(90, ge=0, le=900)


@app.post("/api/strength/exercises")
def strength_exercise_add(e: StExercise):
    ex = dict(id="c_" + uuid.uuid4().hex[:8], name=e.name.strip(), load=e.load,
              primary=[m for m in e.primary if m in strength_lib.MUSCLES][:4], secondary=[m for m in e.secondary if m in strength_lib.MUSCLES][:4],
              rest_s=e.rest_s, inc_lb=5.0, inc_kg=2.5, tags=[], lower=False)
    store.strength_exercise_put(ex)
    return ex


@app.delete("/api/strength/exercises/{eid}")
def strength_exercise_delete(eid: str):
    if not store.strength_exercise_delete(eid):
        raise HTTPException(404, "No such custom exercise.")
    return dict(ok=True)


class StProgramNew(BaseModel):
    template: str | None = None
    name: str | None = Field(None, max_length=80)


@app.post("/api/strength/programs")
def strength_program_new(b: StProgramNew):
    pid = uuid.uuid4().hex[:10]
    if b.template:
        if b.template not in strength_lib.TEMPLATES:
            raise HTTPException(422, "Unknown template.")
        p = strength_lib.from_template(b.template, units_system(), pid)
    else:
        p = dict(id=pid, name="New program", description="", template=None, active=False, schedule=dict(weekdays=[0, 2, 4]),
                 workouts=[dict(key="A", name="Workout A", exercises=[])], lifts={}, adjustments=[])
    if b.name:
        p["name"] = b.name.strip()
    if not any(x.get("active") for x in store.strength_programs()):
        p["active"] = True
    store.strength_program_put(p)
    return p


class StItem(BaseModel):
    exercise_id: str = Field(min_length=1, max_length=60)
    sets: int = Field(ge=1, le=20)
    reps: int = Field(ge=1, le=100)
    rest_s: int = Field(90, ge=0, le=900)
    rest_fail_s: int = Field(180, ge=0, le=900)
    warmup: bool = True
    trim_near_match: bool = False


class StWorkout(BaseModel):
    key: str = Field(min_length=1, max_length=10)
    name: str = Field(min_length=1, max_length=60)
    exercises: list[StItem] = Field(max_length=20)


class StLift(BaseModel):
    start_kg: float = Field(ge=0, le=500)
    increment_kg: float = Field(ge=0, le=50)
    deload_pct: float = Field(10, ge=0, le=50)
    fail_limit: int = Field(3, ge=1, le=10)


class StProgram(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    description: str = Field("", max_length=400)
    weekdays: list[int] = Field(min_length=1, max_length=7)
    workouts: list[StWorkout] = Field(min_length=1, max_length=7)
    lifts: dict[str, StLift] = Field(default_factory=dict)


@app.put("/api/strength/programs/{pid}")
def strength_program_save(pid: str, b: StProgram):
    p = _st_program(pid)
    known = {e["id"] for e in st_library()}
    ids = {it.exercise_id for w in b.workouts for it in w.exercises}
    if ids - known:
        raise HTTPException(422, f"Unknown exercise: {', '.join(sorted(ids - known))}.")
    if len({w.key for w in b.workouts}) != len(b.workouts):
        raise HTTPException(422, "Each workout needs its own letter.")
    lifts = {}
    for ex_id in ids:
        ex = strength_lib.lookup(ex_id, st_custom())
        rule = b.lifts.get(ex_id)
        old = p.get("lifts", {}).get(ex_id)
        lifts[ex_id] = rule.model_dump() if rule else old or dict(start_kg=0.0, increment_kg=strength.from_unit(
            ex["inc_lb"] if st_unit() == "lb" else ex["inc_kg"], st_unit()), deload_pct=10, fail_limit=3)
    p.update(name=b.name.strip(), description=b.description.strip(), schedule=dict(weekdays=sorted(set(d for d in b.weekdays if 0 <= d <= 6))),
             workouts=[w.model_dump() for w in b.workouts], lifts=lifts)
    p["adjustments"] = [a for a in p.get("adjustments") or [] if a.get("exercise_id") in lifts]
    store.strength_program_put(p)
    return _st_program_view(p, store.strength_sessions(), date.today())


@app.post("/api/strength/programs/{pid}/duplicate")
def strength_program_duplicate(pid: str):
    p = _st_program(pid)
    q = json.loads(json.dumps(p))
    q.update(id=uuid.uuid4().hex[:10], name=f"{p['name']} (copy)"[:80], active=False, adjustments=[], shift=None)
    store.strength_program_put(q)
    return q


@app.post("/api/strength/programs/{pid}/activate")
def strength_program_activate(pid: str):
    _st_program(pid)
    for p in store.strength_programs():
        if p.get("active") != (p["id"] == pid):
            p["active"] = p["id"] == pid
            store.strength_program_put(p)
    return dict(ok=True)


@app.delete("/api/strength/programs/{pid}")
def strength_program_delete(pid: str):
    a = store.strength_active()
    if a and a.get("program_id") == pid:
        raise HTTPException(409, "Finish or discard the workout in progress first.")
    if not store.strength_program_delete(pid):
        raise HTTPException(404, "That program doesn't exist (any more).")
    return dict(ok=True)


class StAdjust(BaseModel):
    exercise_id: str = Field(min_length=1, max_length=60)
    kind: Literal["set", "deload", "reps", "increment"]
    kg: float | None = Field(None, ge=0, le=500)
    note: str | None = Field(None, max_length=120)


@app.post("/api/strength/programs/{pid}/adjust")
def strength_program_adjust(pid: str, b: StAdjust):
    """Accepted stall options and manual next weights. Stored as dated adjustments, so the replay
    applies them in order (and editing older sessions can't silently undo them)."""
    p = _st_program(pid)
    rule = (p.get("lifts") or {}).get(b.exercise_id)
    if not rule:
        raise HTTPException(422, "That lift isn't in this program.")
    ex = strength_lib.lookup(b.exercise_id, st_custom())
    st_ = _st_states(p).get(b.exercise_id) or {}
    if b.kind in ("set", "deload"):
        kg = b.kg if b.kind == "set" else (st_.get("last_kg") or st_.get("next_kg") or 0) * 0.9
        if kg is None:
            raise HTTPException(422, "Give the next weight.")
        kg = strength.snap(kg, ex["load"], st_settings(), st_unit(), "down" if b.kind == "deload" else "nearest")
        p.setdefault("adjustments", []).append(dict(id=uuid.uuid4().hex[:8], exercise_id=b.exercise_id, kind=b.kind, kg=kg,
                                                    day=date.today().isoformat(), t=time.time(), note=b.note))
    elif b.kind == "reps":
        for w in p["workouts"]:
            for it in w["exercises"]:
                if it["exercise_id"] == b.exercise_id and it["sets"] > 1:
                    it["sets"], it["reps"] = 3, 5
    elif b.kind == "increment":
        rule["increment_kg"] = strength.from_unit(2.5 if st_unit() == "lb" else 1.25, st_unit())
    store.strength_program_put(p)
    return _st_program_view(p, store.strength_sessions(), date.today())


@app.get("/api/strength/programs/{pid}/start-options")
def strength_start_options(pid: str):
    """What to ask before starting: a restart after time off, a match nearby, soreness, low readiness."""
    p, today, sessions = _st_program(pid), date.today(), store.strength_sessions()
    mine = [x for x in sessions if x.get("program_id") == pid]
    readiness = None
    try:
        readiness = get_data()[1]
    except Exception:
        pass
    return dict(next=strength.next_workout(p, sessions, today), restart_pct=strength.restart_pct(mine[-1]["day"] if mine else None, today),
                days_off=(today - date.fromisoformat(mine[-1]["day"])).days if mine else None, near_match=_near_match(today),
                sore=_sore_note(today), low_readiness=bool(readiness and readiness.get("zone") == "recover"),
                readiness=readiness and readiness.get("readiness"))


class StStart(BaseModel):
    program_id: str
    workout_key: str | None = None
    restart: bool = False
    lighter: bool = False


@app.post("/api/strength/start")
def strength_start(b: StStart):
    if store.strength_active():
        raise HTTPException(409, "A workout is already in progress; resume or discard it first.")
    p, today = _st_program(b.program_id), date.today()
    if b.workout_key and b.workout_key not in {w["key"] for w in p["workouts"]}:
        raise HTTPException(422, "Unknown workout.")
    if not any(w["exercises"] for w in p["workouts"]):
        raise HTTPException(422, "Add some exercises to this program first.")
    a = strength.start(p, store.strength_sessions(), st_settings(), st_unit(), today, b.workout_key, b.restart, b.lighter,
                       _near_match(today), st_custom())
    store.strength_active_put(a)
    return _st_active_view(a)


def _st_active_view(a: dict) -> dict:
    """The in-progress workout plus what the page needs to draw it: the plates for each weight and
    each lift's previous result."""
    sessions, settings, unit = store.strength_sessions(), st_settings(), st_unit()
    out = dict(a, settings=settings, unit=unit, sore=_sore_note(date.today()))
    for e in out["exercises"]:
        e["plates"] = _st_plates(e["kg"], e["load"]) if e["load"] == "barbell" else None
        prev = strength.lift_series(e["exercise_id"], sessions)
        e["last"] = prev[-1] if prev else None
        for w in e.get("warmups") or []:
            w["plates"] = _st_plates(w["kg"], "barbell")
    return out


def _st_plates(kg: float, load: str) -> dict | None:
    if load != "barbell":
        return None
    s, unit = st_settings(), st_unit()
    return strength.plates(strength.to_unit(kg, unit), s[f"bar_{unit}"], s[f"plates_{unit}"])


@app.get("/api/strength/plates")
def strength_plates(kg: float = Query(ge=0, le=1000), load: Literal["barbell", "dumbbell", "machine", "bodyweight"] = "barbell"):
    s, unit = st_settings(), st_unit()
    snapped = strength.snap(kg, load, s, unit)
    return dict(kg=snapped, plates=_st_plates(snapped, load))


@app.get("/api/strength/active")
def strength_active_get():
    a = store.strength_active()
    return dict(active=_st_active_view(a) if a else None)


@app.put("/api/strength/active")
def strength_active_put(x: dict):
    """Autosave: the page sends the whole workout after every tap."""
    cur = store.strength_active()
    if not cur:
        raise HTTPException(404, "There's no workout in progress.")
    a = strength.clean_active(x)
    if a["id"] != cur["id"]:
        raise HTTPException(409, "This workout was finished or replaced in another window.")
    a["started"] = cur["started"]  # the start time can't be moved from the page
    for e in a["exercises"]:
        e["kg"] = strength.snap(e["kg"], e["load"], st_settings(), st_unit()) if e["load"] != "bodyweight" else 0.0
    store.strength_active_put(a)
    return dict(ok=True, saved=time.time())


@app.delete("/api/strength/active")
def strength_active_discard():
    store.strength_active_clear()
    _notify_cancel()
    return dict(ok=True)


@app.post("/api/strength/active/finish")
def strength_finish():
    a = store.strength_active()
    if not a:
        raise HTTPException(404, "There's no workout in progress.")
    session = strength.finish(a)
    if not any(z.get("reps") is not None for e in session["exercises"] for z in e["sets"]):
        raise HTTPException(422, "Log at least one set, or discard the workout instead.")
    history = store.strength_sessions()
    store.strength_finish(session)
    _notify_cancel()
    p = store.strength_program_get(session["program_id"]) if session.get("program_id") else None
    if p and p.get("shift"):
        p["shift"] = None
        store.strength_program_put(p)
    all_ = history + [session]
    states = _st_states(p, all_) if p else {}
    nxt = strength.next_workout(p, all_, date.today()) if p else None
    return strength.summary(session, history, states, nxt, st_unit(), p)


@app.get("/api/strength/sessions")
def strength_sessions_list():
    sessions = store.strength_sessions()
    pr, ms = strength.prs(sessions), strength.milestones(sessions, st_unit())
    out = [dict(id=s["id"], day=s["day"], program_id=s.get("program_id"), program_name=s.get("program_name"),
                workout_name=s.get("workout_name"), duration_s=s.get("duration_s"), volume_kg=round(strength.volume(s), 1),
                exercises=[dict(name=e["name"], kg=e["kg"], load=e.get("load"), reps=[z.get("reps") for z in e["sets"]], skipped=e.get("skipped"),
                                success=bool((r := strength.work_result(e)) and r["success"]), extra=e.get("extra"))
                           for e in s["exercises"]], prs=pr.get(s["id"], []), milestones=ms.get(s["id"], []))
           for s in reversed(sessions)]
    return dict(sessions=out)


@app.get("/api/strength/sessions/{sid}")
def strength_session_get(sid: str):
    s = store.strength_session_get(sid)
    if not s:
        raise HTTPException(404, "That session isn't in your log.")
    sessions = store.strength_sessions()
    return dict(s, prs=strength.prs(sessions).get(sid, []), volume_kg=round(strength.volume(s), 1))


def _next_targets(sessions: list[dict]) -> dict[str, dict]:
    out = {}
    for p in store.strength_programs():
        for ex_id, stt in _st_states(p, sessions).items():
            out[f"{p['id']}:{ex_id}"] = dict(program=p["name"], exercise_id=ex_id, kg=stt["next_kg"])
    return out


def _target_changes(before: list[dict], after: list[dict]) -> list[dict]:
    a, b = _next_targets(before), _next_targets(after)
    return [dict(v, before_kg=a[k]["kg"], after_kg=v["kg"],
                 name=(strength_lib.lookup(v["exercise_id"], st_custom()) or {}).get("name", v["exercise_id"]))
            for k, v in b.items() if k in a and abs(a[k]["kg"] - v["kg"]) > 1e-6]


class StSessionEdit(BaseModel):
    day: str | None = None
    notes: str | None = Field(None, max_length=500)
    exercises: list[dict] = Field(max_length=30)


@app.put("/api/strength/sessions/{sid}")
def strength_session_edit(sid: str, b: StSessionEdit, dry_run: bool = False):
    """Correct a past session. dry_run=true returns how the next targets would change, without saving."""
    s = store.strength_session_get(sid)
    if not s:
        raise HTTPException(404, "That session isn't in your log.")
    day = _parse_day(b.day).isoformat() if b.day else s["day"]
    if day > date.today().isoformat():
        raise HTTPException(422, "A logged session can't be in the future.")
    cleaned = strength.clean_active(dict(s, exercises=b.exercises))["exercises"]
    new = dict(s, day=day, exercises=cleaned, notes=(b.notes if b.notes is not None else s.get("notes", "")), edited=time.time())
    before = store.strength_sessions()
    after = [new if x["id"] == sid else x for x in before]
    after.sort(key=lambda x: (x["day"], x.get("started") or 0))
    changes = _target_changes(before, after)
    if not dry_run:
        store.strength_session_put(new)
    return dict(changes=changes, saved=not dry_run)


@app.delete("/api/strength/sessions/{sid}")
def strength_session_delete(sid: str, dry_run: bool = False):
    s = store.strength_session_get(sid)
    if not s:
        raise HTTPException(404, "That session isn't in your log.")
    before = store.strength_sessions()
    changes = _target_changes(before, [x for x in before if x["id"] != sid])
    if not dry_run:
        store.strength_session_delete(sid)
    return dict(changes=changes, deleted=not dry_run)


@app.get("/api/strength/lifts")
def strength_lifts():
    """A card per exercise you've logged or that's in the active program."""
    sessions, today, unit = store.strength_sessions(), date.today(), st_unit()
    active = next((p for p in store.strength_programs() if p.get("active")), None)
    states = _st_states(active, sessions) if active else {}
    ids = list(dict.fromkeys([e["exercise_id"] for s in sessions for e in s["exercises"] if strength.done_sets(e)] + list(states)))
    cards = []
    for ex_id in ids:
        ex = strength_lib.lookup(ex_id, st_custom()) or dict(id=ex_id, name=ex_id, load="machine")
        c = strength.lift_card(ex, strength.lift_series(ex_id, sessions), states.get(ex_id), today)
        c["options"] = strength.stall_options(c, (active or {}).get("lifts", {}).get(ex_id), unit)
        c["in_program"] = ex_id in states
        cards.append(c)
    order = {k: i for i, k in enumerate(states)}
    cards.sort(key=lambda c: (not c["in_program"], order.get(c["exercise_id"], 99), -(c["sessions"] or 0)))
    return dict(lifts=cards, program=dict(id=active["id"], name=active["name"]) if active else None, unit=unit)


@app.get("/api/strength/lifts/{ex_id}")
def strength_lift(ex_id: str):
    ex = strength_lib.lookup(ex_id, st_custom())
    if not ex:
        raise HTTPException(404, "Unknown exercise.")
    sessions, today, settings = store.strength_sessions(), date.today(), st_settings()
    active = next((p for p in store.strength_programs() if p.get("active") and ex_id in (p.get("lifts") or {})), None)
    stt = _st_states(active, sessions).get(ex_id) if active else None
    series = strength.lift_series(ex_id, sessions)
    bw = _bodyweight_series()
    latest_bw = bw[-1]["kg"] if bw else None
    return dict(exercise=ex, series=series, card=strength.lift_card(ex, series, stt, today),
                events=(stt or {}).get("events", []), program=dict(id=active["id"], name=active["name"]) if active else None,
                rule=(active or {}).get("lifts", {}).get(ex_id),
                bodyweight=bw if settings.get("overlay_weight") else [], lean=_lean_series() if settings.get("overlay_lean") else [],
                standards=strength.standards(ex_id, latest_bw, settings.get("standards"), settings.get("age")),
                bodyweight_kg=latest_bw, unit=st_unit())


class StNotify(BaseModel):
    seconds: float = Field(ge=1, le=900)
    title: str = Field(max_length=60)
    body: str = Field(max_length=120)


def _notify_cancel() -> None:
    t = _notify.get("timer")
    if t:
        t.cancel()
    _notify["timer"] = None


@app.post("/api/strength/notify")
def strength_notify(b: StNotify):
    """Schedule a macOS notification for the end of a rest (the page asks only while it's in the
    background). Local only: osascript on this Mac, nothing leaves the machine."""
    _notify_cancel()
    if not st_settings().get("notify") or os.uname().sysname != "Darwin":
        return dict(scheduled=False)
    clean = lambda t: re.sub(r'["\\\n\r]', "", t)
    script = f'display notification "{clean(b.body)}" with title "{clean(b.title)}" sound name "Glass"'

    def fire():
        import subprocess
        try:
            subprocess.run(["osascript", "-e", script], timeout=5, check=False, capture_output=True)
        except (OSError, subprocess.SubprocessError):
            pass
    t = threading.Timer(b.seconds, fire)
    t.daemon = True
    t.start()
    _notify["timer"] = t
    return dict(scheduled=True)


@app.delete("/api/strength/notify")
def strength_notify_cancel():
    _notify_cancel()
    return dict(ok=True)


# ------------------------------- system status ------------------------------- #
DAILY_FEEDS = [k for k, v in health.FEEDS.items() if v[4]]
FEED_CACHES = {"weight": "body", "fitness_age": "body"}  # anything else lives in the dashboard snapshot


def status_view() -> dict:
    v = health.view(key_set=bool(os.getenv("GARMIN_API_KEY")), demo=demo_mode(),
                    token_expires=_client.token_expires_at if _client else None, ai_key_set=coach.llm_enabled())
    last = ai.last_status()
    v["ai"].update(usage=ai.usage_summary(), models=ai.models(), last_call=last)
    if v["ai"]["key_set"] and last.get("ok") is False and (not v["ai"]["tested"] or (last.get("at") or 0) > v["ai"]["tested"]):
        v["ai"].update(status="error", error=last["error"])  # a real call failed after the last test: show why
    elif v["ai"]["key_set"] and last.get("ok") and v["ai"]["status"] == "unknown":
        v["ai"]["status"] = "ok"
    if v["ai"]["usage"]["cap_reached"]:
        v["ai"]["paused"] = "Monthly budget reached: automatic coach calls are paused."
    return v


def run_checks(light: bool, feeds: list[str] | None, port: int | None) -> dict:
    """Light: token + freshness of the daily feeds (the 30-minute background check). Full: a fresh token
    exchange and a probe of every feed. Neither ever calls the AI."""
    with _lock("status"):
        health.local_checks(store.DATA_DIR, store.DB, store.db, [CACHE / "export", Path.home() / "Downloads"],
                            "127.0.0.1", port or 8765)
        if not demo_mode():
            try:
                c = client()
                c.check_token(force=not light and feeds is None)
            except (GarminError, httpx.HTTPError):
                pass  # recorded by the client hook; probing feeds without a token would only repeat it
            else:
                names = [f for f in (feeds or (DAILY_FEEDS if light else list(health.FEEDS))) if f in health.FEEDS]
                today = date.today()
                with ThreadPoolExecutor(3) as ex:
                    list(ex.map(lambda n: health.probe(c.get, n, today), names))
        health.mark_checked(light)
        v = status_view()
        if not light or v["overall"] != "green":
            what = f"Retried {', '.join(health.FEEDS[f][0] for f in feeds)}" if feeds else "Background check" if light else "Checks run"
            health.event("info" if v["overall"] == "green" else "warning", "Status", f"{what}: {v['summary']}")
        return status_view()


def _drop_caches(feeds: list[str] | None) -> None:
    """Make the next page load fetch these feeds from Garmin again (None = everything)."""
    targets = {FEED_CACHES.get(f, "snapshot") for f in feeds} if feeds else {"body", "snapshot", "metrics", "calendar"}
    if "body" in targets:
        _body["t"] = 0
    if "snapshot" in targets:
        _cache["t"] = 0
    if "metrics" in targets:
        _metrics.clear()
        _daysum_mem.clear()
    if "calendar" in targets:
        _month_mem.clear()
        (CACHE / "calendar" / f"{date.today():%Y-%m}.json").unlink(missing_ok=True)
        with _bf_guard:
            _bf["failed"].clear()


class StatusCheck(BaseModel):
    light: bool = False
    feeds: list[str] | None = None


@app.get("/api/status")
def status():
    """The current status, from what the app has already seen (no network calls)."""
    return status_view()


@app.post("/api/status/check")
def status_check(body_: StatusCheck, request: Request):
    if body_.feeds:
        _drop_caches(body_.feeds)
    return run_checks(body_.light, body_.feeds, request.url.port)


@app.post("/api/status/resync")
def status_resync(request: Request):
    _drop_caches(None)
    health.event("info", "Status", "Re-sync everything: cached Garmin data cleared")
    return run_checks(False, None, request.url.port)


@app.post("/api/status/llm")
def status_llm():
    """The live AI test: one tiny request, only when the button is pressed."""
    health.test_ai(ai.ping, ai.classify, coach.llm_enabled(), ai.route("test")[0])
    return status_view()


@app.get("/api/status/diagnostics")
def status_diagnostics():
    return PlainTextResponse(health.report(status_view()))


# The Garmin services the app itself reads; /api/raw serves nothing else (and never the token service).
RAW_PREFIXES = ("activity-service", "activitylist-service", "biometric-service", "download-service", "fitnessage-service",
                "goal-service", "hrv-service", "sleep-service", "userprofile-service", "usersummary-service",
                "weight-service", "wellness-service")
RAW_PATH = re.compile(r"^/(?:%s)/[A-Za-z0-9._~\-/]*(?:\?[A-Za-z0-9._~\-=&,:]*)?$" % "|".join(map(re.escape, RAW_PREFIXES)))


def raw_path_ok(path: str) -> bool:
    return bool(RAW_PATH.match(path or "")) and "//" not in path and ".." not in path


@app.get("/api/raw")
def raw(path: str):
    """Debug helper, off unless COACH_DEBUG_RAW=1: inspect a raw Garmin response, e.g.
    /api/raw?path=/hrv-service/hrv/2026-10-27. Only paths under RAW_PREFIXES are fetched."""
    if os.getenv("COACH_DEBUG_RAW") != "1":
        raise HTTPException(404, "Not found")
    if not raw_path_ok(path):
        raise HTTPException(400, "Only paths under the Garmin services this app uses are allowed: "
                                 + ", ".join(f"/{p}/" for p in RAW_PREFIXES))
    if demo_mode():
        return JSONResponse({"demo": True})
    global _client
    _client = _client or _new_client()
    try:
        return _client.get(path)
    except GarminError as ex:
        raise HTTPException(502, str(ex))


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")
