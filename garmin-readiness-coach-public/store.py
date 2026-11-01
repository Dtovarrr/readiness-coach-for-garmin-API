"""Local SQLite store (.data/coach.db): goals, settings, photo metadata, daily check-ins, Today's charts,
events and their training plans, DEXA scans, strength programs and the strength log.

Everything the app *keeps* lives here; Garmin data is only cached (in .cache/). COACH_DATA_DIR
overrides the folder (tests, screenshot sandbox). On first use, an existing goals.json /
settings.json is imported in one transaction and verified record by record; the JSON files are
left untouched as backups. If verification fails nothing is committed and an error is raised,
so data is never half-migrated.
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from pathlib import Path

DATA_DIR = Path(os.getenv("COACH_DATA_DIR") or Path(__file__).parent / ".data").resolve()
DB = DATA_DIR / "coach.db"
PHOTO_TAGS = ("Progress", "Event", "Milestone", "Other")
_lock = threading.Lock()  # serialises writes (and first-time setup)
_ready = False

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS goals (id TEXT PRIMARY KEY, pos INTEGER NOT NULL, data TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS photos (
  id TEXT PRIMARY KEY, day TEXT NOT NULL, capture_date TEXT, caption TEXT NOT NULL DEFAULT '',
  tag TEXT NOT NULL DEFAULT 'Other', width INTEGER, height INTEGER, bytes INTEGER,
  path TEXT NOT NULL, thumb TEXT NOT NULL, created TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS photos_day ON photos(day);
CREATE TABLE IF NOT EXISTS checkins (
  day TEXT PRIMARY KEY, ratings TEXT NOT NULL DEFAULT '{}', note TEXT NOT NULL DEFAULT '',
  tags TEXT NOT NULL DEFAULT '[]', updated TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS charts (id TEXT PRIMARY KEY, pos INTEGER NOT NULL, data TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS events (id TEXT PRIMARY KEY, day TEXT NOT NULL, data TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS plans (event_id TEXT PRIMARY KEY, data TEXT NOT NULL, updated TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS dexa_scans (
  id TEXT PRIMARY KEY, day TEXT NOT NULL UNIQUE, source TEXT NOT NULL, data TEXT NOT NULL, pdf TEXT,
  created TEXT NOT NULL, updated TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS strength_programs (id TEXT PRIMARY KEY, pos INTEGER NOT NULL, data TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS strength_sessions (
  id TEXT PRIMARY KEY, day TEXT NOT NULL, program_id TEXT, data TEXT NOT NULL, updated TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS strength_sessions_day ON strength_sessions(day);
CREATE TABLE IF NOT EXISTS strength_exercises (id TEXT PRIMARY KEY, data TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS strength_active (id INTEGER PRIMARY KEY CHECK (id = 1), data TEXT NOT NULL, updated TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS workouts (id TEXT PRIMARY KEY, day TEXT, data TEXT NOT NULL, updated TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS coach_profile (id INTEGER PRIMARY KEY CHECK (id = 1), data TEXT NOT NULL, updated TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS coach_notes (id TEXT PRIMARY KEY, text TEXT NOT NULL, source TEXT NOT NULL DEFAULT 'chat', created TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS ai_cache (key TEXT PRIMARY KEY, feature TEXT NOT NULL, created TEXT NOT NULL, data TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS ai_usage (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, day TEXT NOT NULL, feature TEXT NOT NULL, model TEXT NOT NULL,
  input INTEGER NOT NULL DEFAULT 0, output INTEGER NOT NULL DEFAULT 0, cache_read INTEGER NOT NULL DEFAULT 0,
  cache_write INTEGER NOT NULL DEFAULT 0, cost_usd REAL, steps INTEGER NOT NULL DEFAULT 1, ok INTEGER NOT NULL, error_kind TEXT);
CREATE INDEX IF NOT EXISTS ai_usage_day ON ai_usage(day);
CREATE TABLE IF NOT EXISTS coach_proposals (id TEXT PRIMARY KEY, thread_id TEXT, created TEXT NOT NULL, status TEXT NOT NULL, data TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS coach_threads (id TEXT PRIMARY KEY, updated TEXT NOT NULL, data TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS coach_messages (
  id TEXT PRIMARY KEY, thread_id TEXT NOT NULL, seq INTEGER NOT NULL, role TEXT NOT NULL, text TEXT NOT NULL, meta TEXT NOT NULL, created TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS coach_messages_thread ON coach_messages(thread_id, seq);
CREATE TABLE IF NOT EXISTS coach_feedback (id TEXT PRIMARY KEY, ts TEXT NOT NULL, feature TEXT NOT NULL, ref TEXT, rating INTEGER NOT NULL, data TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS coach_notices (id TEXT PRIMARY KEY, day TEXT NOT NULL, type TEXT NOT NULL, key TEXT NOT NULL, status TEXT NOT NULL, data TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS coach_notices_day ON coach_notices(day);
"""


class MigrationError(RuntimeError):
    pass


def _connect() -> sqlite3.Connection:
    con = sqlite3.connect(DB, timeout=10)
    con.row_factory = sqlite3.Row
    return con


def _read_json(name: str, default):
    try:
        return json.loads((DATA_DIR / name).read_text())
    except (OSError, ValueError):
        return default


def _migrate(con: sqlite3.Connection) -> None:
    goals, settings = _read_json("goals.json", []), _read_json("settings.json", {})
    try:
        with con:  # one transaction: committed only if the round-trip matches exactly
            for i, g in enumerate(goals):
                con.execute("INSERT INTO goals (id, pos, data) VALUES (?, ?, ?)", (g["id"], i, json.dumps(g)))
            for k, v in settings.items():
                con.execute("INSERT INTO settings (key, value) VALUES (?, ?)", (k, json.dumps(v)))
            back = [json.loads(r["data"]) for r in con.execute("SELECT data FROM goals ORDER BY pos")]
            back_s = {r["key"]: json.loads(r["value"]) for r in con.execute("SELECT key, value FROM settings")}
            if back != goals or back_s != settings:
                raise MigrationError("imported goals/settings differ from the JSON files")
            con.execute("INSERT INTO meta (key, value) VALUES ('json_migrated', ?)",
                        (json.dumps(dict(goals=len(goals), settings=len(settings), at=time.strftime("%Y-%m-%dT%H:%M:%S"))),))
    except (sqlite3.Error, KeyError, MigrationError) as ex:
        raise MigrationError(f"Couldn't move goals.json/settings.json into coach.db ({ex}); the JSON files are unchanged.") from ex


def _init() -> None:
    global _ready
    if _ready:
        return
    with _lock:
        if _ready:
            return
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        con = _connect()
        try:
            con.execute("PRAGMA journal_mode=WAL")
            con.executescript(SCHEMA)
            if not con.execute("SELECT 1 FROM meta WHERE key = 'json_migrated'").fetchone():
                _migrate(con)
        finally:
            con.close()
        _ready = True


def db() -> sqlite3.Connection:
    _init()
    return _connect()


def _write(sql: str, args=()) -> int:
    with _lock:
        con = db()
        try:
            with con:
                return con.execute(sql, args).rowcount
        finally:
            con.close()


def _rows(sql: str, args=()) -> list[sqlite3.Row]:
    con = db()
    try:
        return con.execute(sql, args).fetchall()
    finally:
        con.close()


# ---------------------------------- goals ----------------------------------- #
def goals() -> list[dict]:
    return [json.loads(r["data"]) for r in _rows("SELECT data FROM goals ORDER BY pos")]


def goal_insert(g: dict) -> None:
    _write("INSERT INTO goals (id, pos, data) VALUES (?, (SELECT COALESCE(MAX(pos), -1) + 1 FROM goals), ?)", (g["id"], json.dumps(g)))


def goal_replace(g: dict) -> bool:
    return _write("UPDATE goals SET data = ? WHERE id = ?", (json.dumps(g), g["id"])) > 0


def goal_delete(goal_id: str) -> bool:
    return _write("DELETE FROM goals WHERE id = ?", (goal_id,)) > 0


# --------------------------------- settings --------------------------------- #
def settings() -> dict:
    return {r["key"]: json.loads(r["value"]) for r in _rows("SELECT key, value FROM settings")}


def settings_put(fields: dict) -> dict:
    """None deletes a key (e.g. clears the height override)."""
    with _lock:
        con = db()
        try:
            with con:
                for k, v in fields.items():
                    if v is None:
                        con.execute("DELETE FROM settings WHERE key = ?", (k,))
                    else:
                        con.execute("INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                                    (k, json.dumps(v)))
        finally:
            con.close()
    return settings()


# ---------------------------------- photos ---------------------------------- #
PHOTO_COLS = ("id", "day", "capture_date", "caption", "tag", "width", "height", "bytes", "path", "thumb", "created")


def photo_insert(p: dict) -> None:
    _write(f"INSERT INTO photos ({', '.join(PHOTO_COLS)}) VALUES ({', '.join('?' * len(PHOTO_COLS))})", tuple(p[c] for c in PHOTO_COLS))


def photo_get(photo_id: str) -> dict | None:
    r = _rows("SELECT * FROM photos WHERE id = ?", (photo_id,))
    return dict(r[0]) if r else None


def photos(day: str | None = None, month: str | None = None, tag: str | None = None) -> list[dict]:
    where, args = [], []
    if day:
        where.append("day = ?"), args.append(day)
    if month:
        where.append("substr(day, 1, 7) = ?"), args.append(month)
    if tag:
        where.append("tag = ?"), args.append(tag)
    sql = "SELECT * FROM photos" + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY day, created"
    return [dict(r) for r in _rows(sql, args)]


def photo_update(photo_id: str, fields: dict) -> bool:
    cols = [c for c in fields if c in ("day", "caption", "tag", "path", "thumb")]
    if not cols:
        return False
    return _write(f"UPDATE photos SET {', '.join(f'{c} = ?' for c in cols)} WHERE id = ?",
                  (*[fields[c] for c in cols], photo_id)) > 0


def photo_delete(photo_id: str) -> bool:
    return _write("DELETE FROM photos WHERE id = ?", (photo_id,)) > 0


# --------------------------------- check-ins -------------------------------- #
def _checkin(r: sqlite3.Row) -> dict:
    return dict(day=r["day"], ratings=json.loads(r["ratings"]), note=r["note"], tags=json.loads(r["tags"]), updated=r["updated"])


def checkin_get(day: str) -> dict | None:
    r = _rows("SELECT * FROM checkins WHERE day = ?", (day,))
    return _checkin(r[0]) if r else None


def checkins(start: str | None = None, end: str | None = None) -> list[dict]:
    """Oldest first; `start`/`end` are inclusive ISO dates."""
    return [_checkin(r) for r in _rows("SELECT * FROM checkins WHERE day >= ? AND day <= ? ORDER BY day",
                                       (start or "0000-00-00", end or "9999-99-99"))]


def checkin_put(day: str, ratings: dict | None = None, note: str | None = None, tags: list | None = None) -> dict | None:
    """Partial update: a rating of None clears it; a day left with nothing is removed."""
    with _lock:
        con = db()
        try:
            with con:
                r = con.execute("SELECT * FROM checkins WHERE day = ?", (day,)).fetchone()
                cur = _checkin(r) if r else dict(day=day, ratings={}, note="", tags=[])
                for k, v in (ratings or {}).items():
                    if v is None:
                        cur["ratings"].pop(k, None)
                    else:
                        cur["ratings"][k] = v
                if note is not None:
                    cur["note"] = note
                if tags is not None:
                    cur["tags"] = tags
                if not cur["ratings"] and not cur["note"] and not cur["tags"]:
                    con.execute("DELETE FROM checkins WHERE day = ?", (day,))
                    return None
                cur["updated"] = time.strftime("%Y-%m-%dT%H:%M:%S")
                con.execute("INSERT INTO checkins (day, ratings, note, tags, updated) VALUES (?, ?, ?, ?, ?) "
                            "ON CONFLICT(day) DO UPDATE SET ratings = excluded.ratings, note = excluded.note, "
                            "tags = excluded.tags, updated = excluded.updated",
                            (day, json.dumps(cur["ratings"]), cur["note"], json.dumps(cur["tags"]), cur["updated"]))
                return cur
        finally:
            con.close()


def checkins_delete_all() -> int:
    return _write("DELETE FROM checkins")


# ---------------------------------- charts ---------------------------------- #
def charts() -> list[dict]:
    return [json.loads(r["data"]) for r in _rows("SELECT data FROM charts ORDER BY pos")]


def chart_insert(c: dict, after: str | None = None) -> None:
    """Appended, or placed right after chart `after` (duplicates sit next to their original)."""
    with _lock:
        con = db()
        try:
            with con:
                r = con.execute("SELECT pos FROM charts WHERE id = ?", (after,)).fetchone() if after else None
                if r:
                    con.execute("UPDATE charts SET pos = pos + 1 WHERE pos > ?", (r["pos"],))
                    pos = r["pos"] + 1
                else:
                    pos = con.execute("SELECT COALESCE(MAX(pos), -1) + 1 FROM charts").fetchone()[0]
                con.execute("INSERT INTO charts (id, pos, data) VALUES (?, ?, ?)", (c["id"], pos, json.dumps(c)))
        finally:
            con.close()


def chart_replace(c: dict) -> bool:
    return _write("UPDATE charts SET data = ? WHERE id = ?", (json.dumps(c), c["id"])) > 0


def chart_delete(chart_id: str) -> bool:
    return _write("DELETE FROM charts WHERE id = ?", (chart_id,)) > 0


def charts_reorder(ids: list[str]) -> None:
    """`ids` in their new order; charts left out keep their relative order after them."""
    with _lock:
        con = db()
        try:
            with con:
                cur = [r["id"] for r in con.execute("SELECT id FROM charts ORDER BY pos")]
                order = [i for i in ids if i in cur] + [i for i in cur if i not in ids]
                for pos, i in enumerate(dict.fromkeys(order)):
                    con.execute("UPDATE charts SET pos = ? WHERE id = ?", (pos, i))
        finally:
            con.close()


def charts_seed(starters: list[dict]) -> bool:
    """Adds the starter charts once; after that, deleting them sticks."""
    with _lock:
        con = db()
        try:
            with con:
                if con.execute("SELECT 1 FROM meta WHERE key = 'charts_seeded'").fetchone():
                    return False
                for pos, c in enumerate(starters):
                    con.execute("INSERT INTO charts (id, pos, data) VALUES (?, ?, ?)", (c["id"], pos, json.dumps(c)))
                con.execute("INSERT INTO meta (key, value) VALUES ('charts_seeded', ?)", (json.dumps(time.strftime("%Y-%m-%dT%H:%M:%S")),))
                return True
        finally:
            con.close()


# ------------------------------ events + plans ------------------------------ #
def events() -> list[dict]:
    return [json.loads(r["data"]) for r in _rows("SELECT data FROM events ORDER BY day, id")]


def event_get(event_id: str) -> dict | None:
    r = _rows("SELECT data FROM events WHERE id = ?", (event_id,))
    return json.loads(r[0]["data"]) if r else None


def event_put(e: dict) -> None:
    _write("INSERT INTO events (id, day, data) VALUES (?, ?, ?) ON CONFLICT(id) DO UPDATE SET day = excluded.day, data = excluded.data",
           (e["id"], e["date"], json.dumps(e)))


def event_delete(event_id: str) -> bool:
    """Removes the event and its plan together."""
    with _lock:
        con = db()
        try:
            with con:
                con.execute("DELETE FROM plans WHERE event_id = ?", (event_id,))
                return con.execute("DELETE FROM events WHERE id = ?", (event_id,)).rowcount > 0
        finally:
            con.close()


def plan_get(event_id: str) -> dict | None:
    r = _rows("SELECT data FROM plans WHERE event_id = ?", (event_id,))
    return json.loads(r[0]["data"]) if r else None


def plan_put(event_id: str, plan: dict) -> None:
    _write("INSERT INTO plans (event_id, data, updated) VALUES (?, ?, ?) ON CONFLICT(event_id) DO UPDATE SET data = excluded.data, "
           "updated = excluded.updated", (event_id, json.dumps(plan), time.strftime("%Y-%m-%dT%H:%M:%S")))


def plan_delete(event_id: str) -> bool:
    return _write("DELETE FROM plans WHERE event_id = ?", (event_id,)) > 0


def plans() -> list[dict]:
    return [json.loads(r["data"]) for r in _rows("SELECT data FROM plans")]


# ---------------------------- structured workouts ---------------------------- #
def workouts() -> list[dict]:
    """Newest first (scheduled day, then last edit)."""
    return [json.loads(r["data"]) for r in _rows("SELECT data FROM workouts ORDER BY COALESCE(day, '') DESC, updated DESC")]


def workout_get(wid: str) -> dict | None:
    r = _rows("SELECT data FROM workouts WHERE id = ?", (wid,))
    return json.loads(r[0]["data"]) if r else None


def workout_put(w: dict) -> None:
    _write("INSERT INTO workouts (id, day, data, updated) VALUES (?, ?, ?, ?) ON CONFLICT(id) DO UPDATE SET day = excluded.day, "
           "data = excluded.data, updated = excluded.updated", (w["id"], w.get("scheduled"), json.dumps(w), w.get("updated") or time.strftime("%Y-%m-%dT%H:%M:%S")))


def workout_delete(wid: str) -> bool:
    return _write("DELETE FROM workouts WHERE id = ?", (wid,)) > 0


# -------------------------------- DEXA scans -------------------------------- #
# One row per measured date (the unique key). source is "report" (from its own PDF) or "history"
# (backfilled from a newer report's history tables); pdf is the stored file name, if any.
def _dexa(r: sqlite3.Row) -> dict:
    return dict(json.loads(r["data"]), id=r["id"], measured_date=r["day"], source=r["source"], pdf=r["pdf"],
                created=r["created"], updated=r["updated"])


def dexa_scans() -> list[dict]:
    """Oldest first."""
    return [_dexa(r) for r in _rows("SELECT * FROM dexa_scans ORDER BY day")]


def dexa_get(scan_id: str) -> dict | None:
    r = _rows("SELECT * FROM dexa_scans WHERE id = ?", (scan_id,))
    return _dexa(r[0]) if r else None


def dexa_get_day(day: str) -> dict | None:
    r = _rows("SELECT * FROM dexa_scans WHERE day = ?", (day,))
    return _dexa(r[0]) if r else None


def dexa_put(scan: dict) -> None:
    """Insert, or replace the row with the same id. The fixed columns are kept out of `data`."""
    now = time.strftime("%Y-%m-%dT%H:%M:%S")
    data = {k: v for k, v in scan.items() if k not in ("id", "measured_date", "source", "pdf", "created", "updated")}
    _write("INSERT INTO dexa_scans (id, day, source, data, pdf, created, updated) VALUES (?, ?, ?, ?, ?, ?, ?) "
           "ON CONFLICT(id) DO UPDATE SET day = excluded.day, source = excluded.source, data = excluded.data, "
           "pdf = excluded.pdf, updated = excluded.updated",
           (scan["id"], scan["measured_date"], scan["source"], json.dumps(data), scan.get("pdf"), scan.get("created") or now, now))


def dexa_delete(scan_id: str) -> bool:
    return _write("DELETE FROM dexa_scans WHERE id = ?", (scan_id,)) > 0


# --------------------------------- strength --------------------------------- #
# Programs (ordered), finished sessions (one row each; progression is replayed from these, never
# stored), custom exercises, and the single in-progress workout (autosaved on every tap).
def strength_programs() -> list[dict]:
    return [json.loads(r["data"]) for r in _rows("SELECT data FROM strength_programs ORDER BY pos, id")]


def strength_program_get(pid: str) -> dict | None:
    r = _rows("SELECT data FROM strength_programs WHERE id = ?", (pid,))
    return json.loads(r[0]["data"]) if r else None


def strength_program_put(p: dict) -> None:
    with _lock:
        con = db()
        try:
            with con:
                row = con.execute("SELECT pos FROM strength_programs WHERE id = ?", (p["id"],)).fetchone()
                pos = row["pos"] if row else (con.execute("SELECT COALESCE(MAX(pos), -1) + 1 FROM strength_programs").fetchone()[0])
                con.execute("INSERT INTO strength_programs (id, pos, data) VALUES (?, ?, ?) ON CONFLICT(id) DO UPDATE SET data = excluded.data",
                            (p["id"], pos, json.dumps(p)))
        finally:
            con.close()


def strength_program_delete(pid: str) -> bool:
    """Removes the program; its logged sessions stay in the log."""
    return _write("DELETE FROM strength_programs WHERE id = ?", (pid,)) > 0


def strength_sessions(program_id: str | None = None) -> list[dict]:
    """Oldest first (by day, then start time)."""
    rows = (_rows("SELECT data FROM strength_sessions WHERE program_id = ? ORDER BY day, id", (program_id,)) if program_id
            else _rows("SELECT data FROM strength_sessions ORDER BY day, id"))
    out = [json.loads(r["data"]) for r in rows]
    return sorted(out, key=lambda x: (x["day"], x.get("started") or 0))


def strength_session_get(sid: str) -> dict | None:
    r = _rows("SELECT data FROM strength_sessions WHERE id = ?", (sid,))
    return json.loads(r[0]["data"]) if r else None


def strength_session_put(x: dict) -> None:
    _write("INSERT INTO strength_sessions (id, day, program_id, data, updated) VALUES (?, ?, ?, ?, ?) ON CONFLICT(id) DO UPDATE SET "
           "day = excluded.day, program_id = excluded.program_id, data = excluded.data, updated = excluded.updated",
           (x["id"], x["day"], x.get("program_id"), json.dumps(x), time.strftime("%Y-%m-%dT%H:%M:%S")))


def strength_session_delete(sid: str) -> bool:
    return _write("DELETE FROM strength_sessions WHERE id = ?", (sid,)) > 0


def strength_exercises() -> list[dict]:
    return [json.loads(r["data"]) for r in _rows("SELECT data FROM strength_exercises ORDER BY id")]


def strength_exercise_put(e: dict) -> None:
    _write("INSERT INTO strength_exercises (id, data) VALUES (?, ?) ON CONFLICT(id) DO UPDATE SET data = excluded.data",
           (e["id"], json.dumps(e)))


def strength_exercise_delete(eid: str) -> bool:
    return _write("DELETE FROM strength_exercises WHERE id = ?", (eid,)) > 0


def strength_active() -> dict | None:
    r = _rows("SELECT data FROM strength_active WHERE id = 1")
    return json.loads(r[0]["data"]) if r else None


def strength_active_put(x: dict) -> None:
    _write("INSERT INTO strength_active (id, data, updated) VALUES (1, ?, ?) ON CONFLICT(id) DO UPDATE SET data = excluded.data, "
           "updated = excluded.updated", (json.dumps(x), time.strftime("%Y-%m-%dT%H:%M:%S")))


def strength_active_clear() -> None:
    _write("DELETE FROM strength_active WHERE id = 1")


def strength_finish(session: dict) -> None:
    """Save the finished session and clear the in-progress one in a single transaction, so a crash
    can never leave both (a double log) or neither (a lost workout)."""
    with _lock:
        con = db()
        try:
            with con:
                con.execute("INSERT INTO strength_sessions (id, day, program_id, data, updated) VALUES (?, ?, ?, ?, ?) ON CONFLICT(id) DO UPDATE SET "
                            "day = excluded.day, program_id = excluded.program_id, data = excluded.data, updated = excluded.updated",
                            (session["id"], session["day"], session.get("program_id"), json.dumps(session), time.strftime("%Y-%m-%dT%H:%M:%S")))
                con.execute("DELETE FROM strength_active WHERE id = 1")
        finally:
            con.close()


# ------------------------------- coach memory ------------------------------- #
def coach_profile() -> dict:
    r = _rows("SELECT data FROM coach_profile WHERE id = 1")
    return json.loads(r[0]["data"]) if r else {}


def coach_profile_put(p: dict) -> None:
    _write("INSERT INTO coach_profile (id, data, updated) VALUES (1, ?, ?) ON CONFLICT(id) DO UPDATE SET data = excluded.data, "
           "updated = excluded.updated", (json.dumps(p), time.strftime("%Y-%m-%dT%H:%M:%S")))


def coach_notes() -> list[dict]:
    return [dict(r) for r in _rows("SELECT id, text, source, created FROM coach_notes ORDER BY created, id")]


def coach_note_put(n: dict) -> None:
    _write("INSERT INTO coach_notes (id, text, source, created) VALUES (?, ?, ?, ?) ON CONFLICT(id) DO UPDATE SET text = excluded.text",
           (n["id"], n["text"], n.get("source") or "chat", n.get("created") or time.strftime("%Y-%m-%dT%H:%M:%S")))


def coach_note_delete(nid: str) -> bool:
    return _write("DELETE FROM coach_notes WHERE id = ?", (nid,)) > 0


def coach_forget() -> None:
    """Clears the About me profile and every coach note."""
    with _lock:
        con = db()
        try:
            with con:
                con.execute("DELETE FROM coach_profile")
                con.execute("DELETE FROM coach_notes")
        finally:
            con.close()


# ----------------------------- AI output cache ------------------------------ #
def ai_cache_get(key: str) -> dict | None:
    r = _rows("SELECT data FROM ai_cache WHERE key = ?", (key,))
    return json.loads(r[0]["data"]) if r else None


def ai_cache_put(key: str, feature: str, data: dict) -> None:
    _write("INSERT INTO ai_cache (key, feature, created, data) VALUES (?, ?, ?, ?) ON CONFLICT(key) DO UPDATE SET "
           "data = excluded.data, created = excluded.created", (key, feature, time.strftime("%Y-%m-%dT%H:%M:%S"), json.dumps(data)))


def ai_cache_prune(keep_days: int = 60) -> int:
    return _write("DELETE FROM ai_cache WHERE created < date('now', ?)", (f"-{int(keep_days)} days",))


# -------------------------------- AI usage log ------------------------------ #
USAGE_COLS = ("ts", "day", "feature", "model", "input", "output", "cache_read", "cache_write", "cost_usd", "steps", "ok", "error_kind")


def ai_usage_add(row: dict) -> None:
    _write(f"INSERT INTO ai_usage ({', '.join(USAGE_COLS)}) VALUES ({', '.join('?' * len(USAGE_COLS))})",
           tuple(row.get(c) for c in USAGE_COLS))


def ai_usage(since: str | None = None) -> list[dict]:
    """Rows from `since` (YYYY-MM-DD) on, oldest first."""
    if since:
        return [dict(r) for r in _rows("SELECT * FROM ai_usage WHERE day >= ? ORDER BY id", (since,))]
    return [dict(r) for r in _rows("SELECT * FROM ai_usage ORDER BY id")]


# ------------------------------- coach proposals ------------------------------ #
def proposal_put(p: dict) -> None:
    _write("INSERT INTO coach_proposals (id, thread_id, created, status, data) VALUES (?, ?, ?, ?, ?) ON CONFLICT(id) DO UPDATE SET "
           "status = excluded.status, data = excluded.data", (p["id"], p.get("thread_id"), p["created"], p["status"], json.dumps(p)))


def proposal_get(pid: str) -> dict | None:
    r = _rows("SELECT data FROM coach_proposals WHERE id = ?", (pid,))
    return json.loads(r[0]["data"]) if r else None


def proposals(thread_id: str | None = None) -> list[dict]:
    if thread_id:
        return [json.loads(r["data"]) for r in _rows("SELECT data FROM coach_proposals WHERE thread_id = ? ORDER BY created", (thread_id,))]
    return [json.loads(r["data"]) for r in _rows("SELECT data FROM coach_proposals ORDER BY created")]


# -------------------------------- coach threads ------------------------------- #
def thread_put(t: dict) -> None:
    _write("INSERT INTO coach_threads (id, updated, data) VALUES (?, ?, ?) ON CONFLICT(id) DO UPDATE SET updated = excluded.updated, "
           "data = excluded.data", (t["id"], t["updated"], json.dumps(t)))


def thread_get(tid: str) -> dict | None:
    r = _rows("SELECT data FROM coach_threads WHERE id = ?", (tid,))
    return json.loads(r[0]["data"]) if r else None


def threads() -> list[dict]:
    return [json.loads(r["data"]) for r in _rows("SELECT data FROM coach_threads ORDER BY updated DESC")]


def thread_delete(tid: str) -> bool:
    with _lock:
        con = db()
        try:
            with con:
                con.execute("DELETE FROM coach_messages WHERE thread_id = ?", (tid,))
                return con.execute("DELETE FROM coach_threads WHERE id = ?", (tid,)).rowcount > 0
        finally:
            con.close()


def message_add(m: dict) -> dict:
    with _lock:
        con = db()
        try:
            with con:
                seq = con.execute("SELECT COALESCE(MAX(seq), 0) + 1 FROM coach_messages WHERE thread_id = ?", (m["thread_id"],)).fetchone()[0]
                m = dict(m, seq=seq)
                con.execute("INSERT INTO coach_messages (id, thread_id, seq, role, text, meta, created) VALUES (?, ?, ?, ?, ?, ?, ?)",
                            (m["id"], m["thread_id"], seq, m["role"], m["text"], json.dumps(m.get("meta") or {}), m["created"]))
        finally:
            con.close()
    return m


def message_update_meta(mid: str, meta: dict) -> None:
    _write("UPDATE coach_messages SET meta = ? WHERE id = ?", (json.dumps(meta), mid))


def messages(tid: str) -> list[dict]:
    return [dict(r, meta=json.loads(r["meta"])) for r in
            (dict(x) for x in _rows("SELECT * FROM coach_messages WHERE thread_id = ? ORDER BY seq", (tid,)))]


# ------------------------------- coach feedback ------------------------------- #
def feedback_add(f: dict) -> None:
    _write("INSERT INTO coach_feedback (id, ts, feature, ref, rating, data) VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(id) DO UPDATE SET "
           "rating = excluded.rating, data = excluded.data", (f["id"], f["ts"], f["feature"], f.get("ref"), f["rating"], json.dumps(f)))


def feedback(since: str | None = None) -> list[dict]:
    q, a = ("SELECT data FROM coach_feedback WHERE ts >= ? ORDER BY ts", (since,)) if since else ("SELECT data FROM coach_feedback ORDER BY ts", ())
    return [json.loads(r["data"]) for r in _rows(q, a)]


def feedback_clear() -> int:
    return _write("DELETE FROM coach_feedback")


# -------------------------------- coach notices ------------------------------- #
def notice_put(n: dict) -> None:
    _write("INSERT INTO coach_notices (id, day, type, key, status, data) VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(id) DO UPDATE SET "
           "status = excluded.status, data = excluded.data", (n["id"], n["day"], n["type"], n["key"], n["status"], json.dumps(n)))


def notices(since: str | None = None) -> list[dict]:
    q, a = ("SELECT data FROM coach_notices WHERE day >= ? ORDER BY day, id", (since,)) if since else ("SELECT data FROM coach_notices ORDER BY day, id", ())
    return [json.loads(r["data"]) for r in _rows(q, a)]

