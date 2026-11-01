"""Daily check-ins: how the day felt, in your own words and taps.

Ratings are 1 (thumbs up), 0 (neutral) or -1 (thumbs down) per indicator; "up" always means "good
for you" (for Stress: felt calm). Everything is stored locally in the SQLite store. Insights are
rule-based comparisons with Garmin data and are only shown once there are MIN_DAYS of check-ins,
phrased as tendencies ("tends to"), never as causes. Notes never leave this computer: the coach,
when sharing is switched on, only gets ratings and tags.
"""
from __future__ import annotations

import statistics as st
import uuid
from datetime import date, timedelta

import store

MIN_DAYS = 14
CARE_STREAK = 5
MAX_INDICATORS = 12
NOTE_MAX = 200
DEFAULT_INDICATORS = [
    dict(id="mood", label="Mood", hint="mental health"),
    dict(id="energy", label="Energy", hint=""),
    dict(id="fitness", label="Fitness", hint="how your body feels"),
    dict(id="sleep", label="Sleep quality", hint="how it felt"),
    dict(id="stress", label="Stress", hint="thumbs up = felt calm"),
    dict(id="motivation", label="Motivation", hint=""),
]
DEFAULT_TAGS = ["Sore", "Sick", "Traveling", "Great workout", "Poor sleep", "Rest day", "Busy day"]
WORD = {1: "good", 0: "neutral", -1: "poor"}


# --------------------------------- config ----------------------------------- #
def indicators() -> list[dict]:
    """Built-ins (renamable, can be switched off) + your own, in display order."""
    saved = {i["id"]: i for i in store.settings().get("checkin_indicators", [])}
    out = [dict(d, enabled=True, builtin=True) | {k: saved[d["id"]][k] for k in ("label", "enabled") if k in saved.get(d["id"], {})}
           for d in DEFAULT_INDICATORS]
    out += [dict(i, builtin=False, hint="") for i in saved.values() if not i.get("builtin") and i["id"] not in {d["id"] for d in DEFAULT_INDICATORS}]
    return out


def _save_indicators(items: list[dict]) -> None:
    store.settings_put({"checkin_indicators": [{k: i[k] for k in ("id", "label", "enabled", "builtin")} for i in items]})


def add_indicator(label: str) -> list[dict]:
    label = " ".join((label or "").split())[:24]
    items = indicators()
    if not label:
        raise ValueError("Give the indicator a name.")
    if any(i["label"].lower() == label.lower() for i in items):
        raise ValueError("You already have an indicator with that name.")
    if len(items) >= MAX_INDICATORS:
        raise ValueError(f"Up to {MAX_INDICATORS} indicators.")
    items.append(dict(id="c" + uuid.uuid4().hex[:10], label=label, enabled=True, builtin=False, hint=""))
    _save_indicators(items)
    return indicators()


def update_indicator(ind_id: str, label: str | None = None, enabled: bool | None = None) -> list[dict]:
    items = indicators()
    item = next((i for i in items if i["id"] == ind_id), None)
    if not item:
        raise KeyError(ind_id)
    if label is not None:
        label = " ".join(label.split())[:24]
        if not label:
            raise ValueError("Give the indicator a name.")
        item["label"] = label
    if enabled is not None:
        item["enabled"] = bool(enabled)
    _save_indicators(items)
    return indicators()


def tags() -> list[str]:
    return DEFAULT_TAGS


def sharing() -> bool:
    return bool(store.settings().get("share_checkins", False))


def clean_update(day: date, today: date, ratings=None, note=None, tags_=None) -> dict:
    """Validate a partial update; raises ValueError with a user-facing message."""
    if day > today:
        raise ValueError("Check-ins are for today or earlier days.")
    ids = {i["id"] for i in indicators()}
    out = {}
    if ratings is not None:
        for k, v in ratings.items():
            if k not in ids:
                raise ValueError("Unknown indicator.")
            if v not in (1, 0, -1, None) or isinstance(v, bool):
                raise ValueError("Ratings are up, neutral or down.")
        out["ratings"] = ratings
    if note is not None:
        out["note"] = " ".join(note.split())[:NOTE_MAX]  # one line
    if tags_ is not None:
        allowed = set(tags())
        if any(t not in allowed for t in tags_) or len(tags_) > len(allowed):
            raise ValueError("Unknown tag.")
        out["tags_"] = list(dict.fromkeys(tags_))
    return out


# --------------------------------- summary ---------------------------------- #
def _month_bounds(month: str) -> tuple[str, str]:
    y, m = int(month[:4]), int(month[5:7])
    nxt = date(y + (m == 12), m % 12 + 1, 1)
    return f"{month}-01", (nxt - timedelta(days=1)).isoformat()


def _counts(rows: list[dict], ind: str) -> dict:
    vals = [r["ratings"][ind] for r in rows if ind in r["ratings"]]
    return dict(up=vals.count(1), neutral=vals.count(0), down=vals.count(-1), n=len(vals),
                score=(vals.count(1) - vals.count(-1)) / len(vals) if vals else None)


def month_summary(month: str) -> dict:
    """Per enabled indicator: thumbs-up vs thumbs-down days, and a trend arrow vs last month."""
    start, end = _month_bounds(month)
    prev = (date.fromisoformat(start) - timedelta(days=1)).isoformat()[:7]
    rows, prev_rows = store.checkins(start, end), store.checkins(*_month_bounds(prev))
    out = []
    for i in indicators():
        if not i["enabled"]:
            continue
        c, p = _counts(rows, i["id"]), _counts(prev_rows, i["id"])
        trend = None
        if c["score"] is not None and p["score"] is not None:
            d = c["score"] - p["score"]
            trend = "up" if d >= 0.15 else "down" if d <= -0.15 else "flat"
        out.append(dict(id=i["id"], label=i["label"], **{k: c[k] for k in ("up", "neutral", "down", "n")}, trend=trend))
    return dict(days=len(rows), indicators=out)


# --------------------------------- insights --------------------------------- #
def _streak(rows: list[dict], ind: str, value: int) -> list[str]:
    """Consecutive calendar days, ending at the most recent day rated for `ind`, all rated `value`."""
    days = [r for r in rows if ind in r["ratings"]]
    out = []
    for r in reversed(days):
        if r["ratings"][ind] != value or (out and date.fromisoformat(out[-1]) - date.fromisoformat(r["day"]) != timedelta(days=1)):
            break
        out.append(r["day"])
    return out


def _median(vals):
    vals = [v for v in vals if v is not None]
    return st.median(vals) if vals else None


def _mismatches(rows, inds, hist, today):
    flags, label = [], {i["id"]: i["label"] for i in inds if i["enabled"]}
    hrv_med = _median(h.get("hrv") for h in hist.values())
    for ind in ("energy", "mood", "fitness"):
        if ind not in label:
            continue
        for value in (-1, 1):
            days = _streak(rows, ind, value)
            if len(days) < 3 or date.fromisoformat(days[0]) < today - timedelta(days=2):
                continue
            sleep = _median(hist.get(d, {}).get("sleep_h") for d in days)
            hrv = _median(hist.get(d, {}).get("hrv") for d in days)
            if sleep is None or hrv is None or hrv_med is None:
                continue
            name = label[ind].lower()
            if value == -1 and sleep >= 7 and hrv >= hrv_med:
                why = ("How you feel matters as much as the numbers, and a tough stretch is worth taking seriously." if ind == "mood" else
                       "Stress, a busy stretch, under-eating or a bug coming on can show up in how you feel before your watch picks it up.")
                flags.append(dict(kind="feels_worse", indicator=ind, days=len(days), text=(
                    f"Your HRV and sleep look good, but you've rated {name} low for {len(days)} days in a row. {why}")))
            if value == 1 and (sleep < 6.5 or hrv < 0.9 * hrv_med):
                flags.append(dict(kind="feels_better", indicator=ind, days=len(days), text=(
                    f"You've rated {name} high for {len(days)} days while your sleep or HRV has been below your usual. "
                    "Great that you feel good; it may still be worth easing in so recovery can catch up.")))
    return flags


def compute(rows: list[dict], history: list[dict], inds: list[dict], today: date) -> dict:
    """Patterns between check-ins and Garmin data (only from MIN_DAYS check-in days on) + mismatch flags."""
    rows = [r for r in rows if r["ratings"]]
    hist = {h["date"]: h for h in history}
    out = dict(days=len(rows), min_days=MIN_DAYS, ready=len(rows) >= MIN_DAYS, patterns=[],
               mismatches=_mismatches(rows, inds, hist, today))
    if not out["ready"]:
        return out
    prev = lambda d: hist.get((date.fromisoformat(d) - timedelta(days=1)).isoformat(), {})
    get = lambda k: (lambda d: hist.get(d, {}).get(k))
    prev_load = lambda d: (prev(d).get("load") or 0) if prev(d) else None  # no activities that day = 0 load
    med = {k: _median(h.get(k) for h in history) for k in ("hrv", "rhr", "bb_high")}
    hard = _median(h.get("load") for h in history if (h.get("load") or 0) > 0)
    conds = [  # (metric for a day, test, "when true" phrase, "when false" phrase)
        (get("sleep_h"), lambda v: v >= 7, "after 7+ hours of sleep", "after shorter nights"),
        (get("hrv"), lambda v: v >= med["hrv"], "on mornings your HRV is at or above your usual", "when it's lower"),
        (get("rhr"), lambda v: v <= med["rhr"], "when your resting heart rate is at or below your usual", "when it's higher"),
        (get("bb_high"), lambda v: v >= med["bb_high"], "on days your Body Battery peaks higher", "on lower Body Battery days"),
        (prev_load, lambda v: hard is not None and v > hard, "the day after a harder training day", "after easier days"),
        (prev_load, lambda v: v == 0, "the day after a rest day", "the day after training"),
    ]
    found = []
    for i in inds:
        if not i["enabled"]:
            continue
        for metric, test, when, otherwise in conds:
            a, b = [], []
            for r in rows:
                v = r["ratings"].get(i["id"])
                m = metric(r["day"])
                if v is None or m is None:
                    continue
                try:
                    (a if test(m) else b).append(v)
                except TypeError:  # missing baseline
                    break
            if len(a) < 5 or len(b) < 5:
                continue
            diff = st.mean(a) - st.mean(b)
            if abs(diff) < 0.35:
                continue
            pa, pb = round(100 * a.count(1) / len(a)), round(100 * b.count(1) / len(b))
            found.append(dict(indicator=i["id"], strength=round(abs(diff), 2), days=len(a) + len(b), text=(
                f"Your {i['label'].lower()} tends to get {'more' if diff > 0 else 'fewer'} thumbs up {when}: "
                f"{pa}% of those days vs {pb}% {otherwise}.")))
    out["patterns"] = sorted(found, key=lambda x: -x["strength"])[:4]
    return out


def care(rows: list[dict], today: date) -> dict | None:
    """A gentle note when mood has been rated down CARE_STREAK+ days in a row, recently, and not dismissed."""
    days = _streak(rows, "mood", -1)
    if len(days) < CARE_STREAK or date.fromisoformat(days[0]) < today - timedelta(days=2):
        return None
    start = days[-1]
    if store.settings().get("care_dismissed") == start:
        return None
    return dict(days=len(days), since=start)


def coach_payload(rows: list[dict], inds: list[dict], today: date, insights: dict, days: int = 14) -> dict:
    """Ratings and tags only - never notes - for the coach, when sharing is on."""
    label = {i["id"]: i["label"] for i in inds if i["enabled"]}
    since = (today - timedelta(days=days - 1)).isoformat()
    recent = [dict(date=r["day"], ratings={label[k]: WORD[v] for k, v in r["ratings"].items() if k in label}, tags=r["tags"])
              for r in rows if r["day"] >= since and (r["ratings"] or r["tags"])]
    return dict(about="Self-reported daily wellness check-ins (good / neutral / poor) and tags. Wellness context only, not medical data.",
                recent=recent, patterns=[p["text"] for p in insights.get("patterns", [])],
                mismatches=[m["text"] for m in insights.get("mismatches", [])])
