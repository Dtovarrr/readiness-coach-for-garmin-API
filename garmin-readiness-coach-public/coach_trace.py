"""Traceable coach answers: where each number came from, and which ones can't be traced.

Every tool result is registered as a source (built by the app, never by the model) with a label like
"Sleep, Oct 20 to 26", a link the page can open (a chart, an activity, a day, the System status
panel) and the numbers it contained. After the reply, every number in it is matched against those
numbers (and the question and starting data); a match turns its source into a chip under the reply,
and a number with no match is wrapped in ⟦ ⟧ so the page can flag it. Nothing is removed.
"""
from __future__ import annotations

import re
from datetime import date

GUIDELINE_CONSTANTS = {10.0, 0.5, 1.0, 100.0}  # the prompt's own rules (10% a week, 0.5-1% body weight) and "/100" scales
SMALL_COUNT = 5  # "2-3 sessions", "3 nights": counts this small aren't data claims
MONTHS = "jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec|january|february|march|april|june|july|august|september|october|november|december"
_DATE_RES = [
    re.compile(r"\b\d{4}-\d{2}-\d{2}\b"),
    re.compile(rf"\b(?:{MONTHS})\.?\s+\d{{1,2}}(?:st|nd|rd|th)?(?:\s*(?:-|–|to)\s*\d{{1,2}})?(?:,\s*\d{{4}})?\b", re.I),
    re.compile(rf"\b\d{{1,2}}(?:st|nd|rd|th)?\s+(?:of\s+)?(?:{MONTHS})\b", re.I),
    re.compile(r"\b\d{1,2}/\d{1,2}(?:/\d{2,4})?\b"),
    re.compile(r"\b(?:19|20)\d{2}\b"),
    re.compile(r"\b\d+(?:st|nd|rd|th)\b", re.I),
    re.compile(r"\b(?:zone|z)\s?\d\b", re.I),
    re.compile(r"\b\d{1,2}(?::\d{2})?\s?(?:am|pm)\b", re.I),  # clock times ("by 10:30 pm") are advice, not data
]
NUM_RE = re.compile(r"(?<![\w.])(\d{1,2}:\d{2}(?::\d{2})?|\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)(\s?%)?")
FMT_TIME = re.compile(r"^(\d{1,2}):(\d{2})(?::(\d{2}))?")


# --------------------------------- sources ------------------------------------- #
def _fmt_day(s: str) -> str:
    d = date.fromisoformat(s[:10])
    return f"{d:%b} {d.day}"


def span(a: str, b: str) -> str:
    if a[:10] == b[:10]:
        return _fmt_day(a)
    if a[:7] == b[:7]:
        return f"{_fmt_day(a)} to {date.fromisoformat(b[:10]).day}"
    return f"{_fmt_day(a)} to {_fmt_day(b)}"


METRIC_LABELS = {"readiness": "Readiness", "hrv": "HRV", "sleep_h": "Sleep", "sleep_score": "Sleep score", "rhr": "Resting HR",
                 "bb_high": "Body Battery", "bb_low": "Body Battery", "stress": "Stress", "steps": "Steps", "load": "Training load",
                 "active_min": "Intensity minutes", "weight_kg": "Weight", "body_fat_pct": "Body fat"}


def describe(tool: str, inp: dict, out: dict, activity_id=None) -> dict | None:
    """(label, link) for one tool result. Links are local (never sent to Claude): the page resolves them."""
    if out.get("error"):
        return None
    if tool == "get_daily_metrics":
        names = list(dict.fromkeys(METRIC_LABELS.get(m, m) for m in inp.get("metrics") or []))
        return dict(kind="metric", label=f"{', '.join(names[:2])}{'…' if len(names) > 2 else ''}, {span(inp['start'], inp['end'])}",
                    link=dict(kind="metric", metric=(inp.get("metrics") or ["hrv"])[0], start=inp["start"], end=inp["end"]))
    if tool == "get_activities":
        return dict(kind="day", label=f"Workouts, {span(inp['start'], inp['end'])}", link=dict(kind="day", day=inp["end"]))
    if tool in ("get_activity_detail", "compare_similar_workouts") and activity_id is not None:
        a = out.get("activity") or {}
        kind = (a.get("type") or "workout").replace("_", " ").split(" v")[0]
        when = f" on {_fmt_day(a['date'])}" if a.get("date") else ""
        return dict(kind="activity", label=("Similar workouts vs " if tool == "compare_similar_workouts" else "") + f"{kind.capitalize()}{when}",
                    link=dict(kind="activity", id=activity_id))
    simple = {"get_body_composition": ("body", "Body composition", dict(kind="body")),
              "get_dexa_scans": ("dexa", "DEXA scans", dict(kind="dexa")),
              "get_goals": ("goal", "Your goals", dict(kind="goals")), "get_goal_progress": ("goal", "Goal progress", dict(kind="goals")),
              "get_training_plan": ("plan", "Training plan", dict(kind="plan")),
              "get_records": ("records", "Personal records", dict(kind="records")),
              "get_user_profile": ("profile", "About me", dict(kind="settings")),
              "get_strength_history": ("lift", f"{inp.get('exercise', 'Strength').capitalize()} sessions", dict(kind="lifts")),
              "get_checkins": ("day", "Check-ins", dict(kind="calendar")),
              "get_weather": ("weather", "Weather forecast", dict(kind="settings")),
              "get_busy_blocks": ("calendar", "Calendar busy times", dict(kind="settings"))}
    if tool in simple:
        k, label, link = simple[tool]
        if "start" in inp and "end" in inp and tool != "get_strength_history":
            label += f", {span(inp['start'], inp['end'])}"
        return dict(kind=k, label=label, link=link)
    return None


# --------------------------------- numbers ------------------------------------- #
def _to_seconds(s: str) -> float | None:
    m = FMT_TIME.match(s)
    if not m:
        return None
    a, b, c = m.groups()
    return int(a) * 3600 + int(b) * 60 + int(c) if c else int(a) * 60 + int(b)


def numbers_in(obj) -> set[float]:
    """Every number in a (localized) tool result: values, and numbers inside strings such as "8:30"."""
    out: set[float] = set()

    def walk(v):
        if isinstance(v, bool) or v is None:
            return
        if isinstance(v, (int, float)):
            out.add(float(v))
        elif isinstance(v, str):
            for m in NUM_RE.finditer(v):
                t = m.group(1)
                sec = _to_seconds(t) if ":" in t else None
                out.add(sec if sec is not None else float(t.replace(",", "")))
        elif isinstance(v, dict):
            for x in v.values():
                walk(x)
        elif isinstance(v, (list, tuple)):
            for x in v:
                walk(x)
    walk(obj)
    return out


def _masked(text: str) -> str:
    """The text with dates, ordinals and zone names blanked (same length), so their digits aren't claims."""
    for rx in _DATE_RES:
        text = rx.sub(lambda m: " " * len(m.group(0)), text)
    return text


def claims(text: str) -> list[dict]:
    """Numbers stated in the reply: {start, end, raw, value, decimals, pct, time}."""
    masked, out = _masked(text), []
    for m in NUM_RE.finditer(masked):
        raw = m.group(1)
        if ":" in raw:
            out.append(dict(start=m.start(1), end=m.end(1), raw=raw, value=_to_seconds(raw), decimals=0, pct=False, time=True))
            continue
        v = float(raw.replace(",", ""))
        dec = len(raw.split(".")[1]) if "." in raw else 0
        out.append(dict(start=m.start(1), end=m.end(1), raw=raw, value=v, decimals=dec, pct=bool(m.group(2)), time=False))
    return out


def _supported(c: dict, pool: dict[float, set]) -> set | None:
    """Source ids whose numbers support this claim (rounded to the shown precision, or within 1%), else None."""
    v = c["value"]
    tol = 0.5 * 10 ** (-c["decimals"]) if not c["time"] else 0.5
    hits: set = set()
    for p, srcs in pool.items():
        cands = [p, p * 100] if c["pct"] and 0 < abs(p) <= 1.5 else [p]
        for q in cands:
            if abs(q - v) <= tol + 1e-9 or (abs(q) >= 20 and abs(q - v) <= 0.01 * abs(q)):
                hits |= srcs
    return hits or None


def check(text: str, sources: list[dict], extra: list | None = None) -> dict:
    """Validate the numbers in `text` against the sources' numbers (+ `extra`: the question, the
    starting data). Returns {text (unmatched numbers wrapped in ⟦⟧), unverified: [raw], chips: [source]}."""
    pool: dict[float, set] = {}
    for s in sources:
        for n in s.get("numbers") or ():
            pool.setdefault(round(n, 6), set()).add(s["id"])
    for n in numbers_in(extra or []):
        pool.setdefault(round(n, 6), set()).add("_context")
    used, unverified, marks = set(), [], []
    for c in claims(text):
        if not c["time"] and not c["pct"] and c["value"] == int(c["value"]) and c["value"] <= SMALL_COUNT:
            continue
        if not c["time"] and c["value"] in GUIDELINE_CONSTANTS and not _supported(c, pool):
            continue
        hit = _supported(c, pool)
        if hit:
            used |= hit
        else:
            unverified.append(c["raw"])
            marks.append(c)
    out = text
    for c in sorted(marks, key=lambda c: -c["start"]):
        out = out[:c["start"]] + "⟦" + out[c["start"]:c["end"]] + "⟧" + out[c["end"]:]
    chips = [{k: s[k] for k in ("id", "kind", "label", "link")} for s in sources if s["id"] in used]
    return dict(text=out, unverified=unverified, chips=chips)


# ------------------------------- stale backstop ---------------------------------- #
STALE_WORDS = re.compile(r"(?i)\b(stale|out of date|outdated|hasn't synced|has not synced|not synced|missing|no data|latest (?:data|reading)|"
                         r"couldn't|could not|isn't available|not available|failed)\b")


def stale_note(sources: list[dict], text: str) -> dict | None:
    """When a tool reported stale or failed data and the reply doesn't say so, a note to append (with a
    System status chip)."""
    bad = [s for s in sources if s.get("problem")]
    if not bad or STALE_WORDS.search(text):
        return None
    msg = "; ".join(dict.fromkeys(s["problem"] for s in bad))
    return dict(text=f"Some of the data behind this answer is out of date: {msg}",
                chip=dict(id="status", kind="status", label="Open System status", link=dict(kind="status")))
