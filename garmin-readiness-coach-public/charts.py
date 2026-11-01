"""Custom charts on Today: metric catalogue, date buckets, aggregation and the series for one chart.

Pure functions over per-day rows (the calendar month cache + per-day summaries), so they can be
tested without Garmin. Every value stays metric (m, kg, h); the page converts for display.

Missing data: a day that isn't loaded yet never counts. Within loaded days, a "sum" or "count"
treats a missing value as 0 (so an empty bucket is 0); "avg" and "max" skip it (an empty bucket is
a gap). Activity metrics are 0 on a loaded day without a matching activity.
"""
from __future__ import annotations

import statistics as st
import uuid
from calendar import monthrange
from datetime import date, timedelta

HISTORY_DAYS = 366  # never look further back than this
AGGS = ("sum", "avg", "max", "count")
GROUPINGS = ("day", "week", "month")
RANGES = {"7D": 7, "30D": 30, "12W": 84, "90D": 90, "6M": 183, "1Y": 365}
CHART_TYPES = ("bar", "line", "area", "stat")
SIZES = ("small", "wide")
ADDITIVE = AGGS
LEVEL = ("avg", "max", "count")  # summing a score or a body weight means nothing

# key -> label, unit (metric), source, row field, default aggregation, allowed aggregations, lower is better
METRICS = {
    "steps": dict(label="Steps", unit="steps", src="day", field="steps", agg="avg", aggs=ADDITIVE),
    "distance": dict(label="Distance", unit="m", src="day", field="distance_m", agg="sum", aggs=ADDITIVE),
    "active_min": dict(label="Active minutes", unit="min", src="daysum", field="im", agg="sum", aggs=ADDITIVE),
    "load": dict(label="Training load", unit="load", src="day", field="load", agg="sum", aggs=ADDITIVE, zero=True),
    "calories": dict(label="Calories", unit="kcal", src="daysum", field="kcal", agg="avg", aggs=ADDITIVE),
    "sleep_h": dict(label="Sleep", unit="h", src="day", field="sleep_h", agg="avg", aggs=ADDITIVE),
    "sleep_score": dict(label="Sleep score", unit="score", src="day", field="sleep_score", agg="avg", aggs=LEVEL),
    "hrv": dict(label="HRV", unit="ms", src="day", field="hrv", agg="avg", aggs=LEVEL),
    "rhr": dict(label="Resting HR", unit="bpm", src="daysum", field="rhr", agg="avg", aggs=LEVEL, lower=True),
    "bb_high": dict(label="Body Battery high", unit="score", src="day", field="bb_high", agg="avg", aggs=LEVEL),
    "bb_low": dict(label="Body Battery low", unit="score", src="day", field="bb_low", agg="avg", aggs=LEVEL),
    "stress": dict(label="Stress", unit="score", src="day", field="stress", agg="avg", aggs=LEVEL, lower=True),
    "weight": dict(label="Weight", unit="kg", src="day", field="weight_kg", agg="avg", aggs=LEVEL, lower=True),
    "body_fat": dict(label="Body fat", unit="%", src="day", field="fat", agg="avg", aggs=LEVEL, lower=True),
    "readiness": dict(label="Readiness", unit="score", src="day", field="readiness", agg="avg", aggs=LEVEL),
    "act_hours": dict(label="Activity time", unit="h", src="activity", field="hours", agg="sum", aggs=ADDITIVE),
    "act_distance": dict(label="Activity distance", unit="m", src="activity", field="dist_m", agg="sum", aggs=ADDITIVE),
    "act_sessions": dict(label="Sessions", unit="sessions", src="activity", field="sessions", agg="sum", aggs=ADDITIVE),
}
CHECKIN_METRIC = dict(unit="rating", src="checkin", agg="avg", aggs=("avg", "count"))

# Goal type -> the chart it can be drawn on (goal targets are stored metric, km for running)
GOAL_FITS = {
    "weekly_run_km": dict(metric="act_distance", grouping="week", agg="sum", types=["run"], factor=1000),
    "weekly_active_min": dict(metric="active_min", grouping="week", agg="sum"),
    "steps": dict(metric="steps", agg="avg"),
    "sleep_h": dict(metric="sleep_h", agg="avg"),
    "weight": dict(metric="weight", agg="avg"),
    "body_fat": dict(metric="body_fat", agg="avg"),
}

STARTERS = [
    dict(name="Daily steps", metric="steps", grouping="day", agg="avg", range="30D", type="bar", size="small"),
    dict(name="Miles run per week", metric="act_distance", types=["run"], grouping="week", agg="sum", range="12W",
         type="bar", size="small"),
    dict(name="Hours of tennis per week", metric="act_hours", types=["tennis"], grouping="week", agg="sum", range="12W",
         type="bar", size="small"),
]


def metric_info(key: str, checkin_labels: dict[str, str] | None = None) -> dict | None:
    if key.startswith("checkin:"):
        label = (checkin_labels or {}).get(key[8:])
        return dict(CHECKIN_METRIC, label=label, field=key[8:]) if label else None
    return METRICS.get(key)


# --------------------------------- validation -------------------------------- #
def clean(d: dict, checkin_labels: dict[str, str] | None = None, today: date | None = None) -> dict:
    """A stored chart definition from page input; raises ValueError with a readable message."""
    today = today or date.today()
    m = metric_info(d.get("metric") or "", checkin_labels)
    if not m:
        raise ValueError("Pick a metric.")
    out = dict(id=d.get("id") or uuid.uuid4().hex[:12], name=(d.get("name") or "").strip()[:80] or m["label"],
               metric=d["metric"], grouping=d.get("grouping") or "day", agg=d.get("agg") or m["agg"],
               range=d.get("range") or "30D", type=d.get("type") or "bar", size=d.get("size") or "small")
    for k, allowed in (("grouping", GROUPINGS), ("agg", m["aggs"]), ("range", (*RANGES, "custom")),
                       ("type", CHART_TYPES), ("size", SIZES)):
        if out[k] not in allowed:
            raise ValueError(f"{k.capitalize()} can't be “{out[k]}” for {m['label']}.")
    if m["src"] == "activity":
        out["types"] = sorted({str(t).strip().lower()[:40] for t in d.get("types") or [] if str(t).strip()})[:20]
        out["exclude_walks"] = bool(d.get("exclude_walks"))
    if out["range"] == "custom":
        try:
            s, e = date.fromisoformat(d.get("start") or ""), date.fromisoformat(d.get("end") or "")
        except ValueError:
            raise ValueError("Custom ranges need a start and an end date.")
        if s > e:
            s, e = e, s
        if e > today or s < today - timedelta(days=HISTORY_DAYS):
            raise ValueError("Custom ranges can go back up to a year and can't end in the future.")
        out.update(start=s.isoformat(), end=e.isoformat())
    g = d.get("goal") or {}
    if g.get("mode") == "value" and isinstance(g.get("value"), (int, float)):
        out["goal"] = dict(mode="value", value=float(g["value"]))
    elif g.get("mode") == "goal" and g.get("goal_id"):
        out["goal"] = dict(mode="goal", goal_id=str(g["goal_id"])[:40])
    else:
        out["goal"] = dict(mode="none")
    return out


# ---------------------------------- dates ------------------------------------ #
def monday(d: date) -> date:
    return d - timedelta(days=d.weekday())


def add_months(d: date, n: int) -> date:
    y, m = divmod(d.month - 1 + n, 12)
    return date(d.year + y, m + 1, 1)


def resolve_range(defn: dict, today: date) -> tuple[date, date]:
    """[start, end] for a chart. Presets end today; weekly/monthly ones snap to whole weeks/months
    (the current one is partial), so "12W" weekly is exactly 12 bars."""
    if defn.get("range") == "custom":
        return date.fromisoformat(defn["start"]), date.fromisoformat(defn["end"])
    days, g = RANGES[defn.get("range", "30D")], defn.get("grouping", "day")
    if g == "week":
        return monday(today) - timedelta(weeks=-(-days // 7) - 1), today
    if g == "month":
        return add_months(today.replace(day=1), -max(1, round(days / 30.4)) + 1), today
    return today - timedelta(days=days - 1), today


def buckets(start: date, end: date, grouping: str) -> list[tuple[date, date, bool]]:
    """(first day, last day, partial) per bucket. Weeks run Monday-Sunday and months by calendar
    month; buckets are clipped to [start, end] and flagged partial when clipped."""
    out, cur = [], start
    while cur <= end:
        if grouping == "week":
            b0, b1 = monday(cur), monday(cur) + timedelta(days=6)
        elif grouping == "month":
            b0, b1 = cur.replace(day=1), cur.replace(day=monthrange(cur.year, cur.month)[1])
        else:
            b0 = b1 = cur
        s, e = max(b0, start), min(b1, end)
        out.append((s, e, (s, e) != (b0, b1)))
        cur = e + timedelta(days=1)
    return out


# -------------------------------- aggregation -------------------------------- #
def aggregate(values: list[float | None], how: str, loaded: int | None = None) -> float | None:
    """`values` are the day values (None = no reading) of `loaded` loaded days (default: all).
    sum/count of a bucket with loaded days is a number (0 when empty); avg/max of nothing is None."""
    loaded = len(values) if loaded is None else loaded
    vals = [v for v in values if v is not None]
    if how == "count":
        return float(len(vals)) if loaded else None
    if how == "sum":
        return float(sum(vals)) if loaded else None
    if not vals:
        return None
    return float(max(vals)) if how == "max" else float(st.fmean(vals))


def matches(a: dict, types: list[str], exclude_walks: bool = False) -> bool:
    t = (a.get("type") or "").lower()
    if exclude_walks and "walk" in t:
        return False
    return not types or any(f in t for f in types)


def day_value(defn: dict, m: dict, row: dict | None, checkin: dict | None):
    """The metric for one day (for activity metrics counted with "count": the number of sessions)."""
    if m["src"] == "checkin":
        return (checkin or {}).get("ratings", {}).get(m["field"])
    if row is None:
        return None
    if m["src"] == "activity":
        acts = [a for a in row.get("activities") or [] if matches(a, defn.get("types") or [], defn.get("exclude_walks"))]
        if m["field"] == "sessions" or defn.get("agg") == "count":
            return float(len(acts))
        if m["field"] == "hours":
            return sum(a.get("secs") or 0 for a in acts) / 3600
        return float(sum(a.get("dist_m") or 0 for a in acts))
    v = row.get(m["field"])
    return (0.0 if m.get("zero") else None) if v is None else float(v)


def _window(defn, m, rows, checkins, s: date, e: date):
    """(values, loaded days) for [s, e]. Check-ins are local, so every day counts as loaded for them."""
    vals, loaded, d = [], 0, s
    while d <= e:
        k = d.isoformat()
        row = rows.get(k)
        if m["src"] == "checkin" or row is not None:
            loaded += 1
            vals.append(day_value(defn, m, row, checkins.get(k)))
        d += timedelta(days=1)
    return vals, loaded


def _bucket_label(s: date, e: date, grouping: str) -> str:
    if grouping == "month":
        return s.strftime("%Y-%m")
    return s.isoformat()


def goal_value(defn: dict, goal: dict | None) -> tuple[float | None, str | None]:
    """The goal line in the chart's own (metric) unit, or why it can't be drawn."""
    g = defn.get("goal") or {}
    if g.get("mode") == "value":
        return g.get("value"), None
    if g.get("mode") != "goal":
        return None, None
    if not goal or goal.get("target") is None:
        return None, "That goal no longer exists."
    fit = GOAL_FITS.get(goal.get("type"))
    if not fit or fit["metric"] != defn["metric"] or fit["agg"] != defn.get("agg") or \
            fit.get("grouping", defn.get("grouping")) != defn.get("grouping"):
        return None, "This goal doesn't match the chart's metric or grouping."
    return goal["target"] * fit.get("factor", 1), None


def series(defn: dict, rows: dict[str, dict], checkins: dict[str, dict], today: date,
           checkin_labels: dict[str, str] | None = None, goal: dict | None = None) -> dict:
    """Points (one per bucket), the whole-range total, the previous same-length window and the goal line."""
    m = metric_info(defn["metric"], checkin_labels)
    if not m:
        raise ValueError("Unknown metric.")
    start, end = resolve_range(defn, today)
    how, grouping = defn.get("agg") or m["agg"], defn.get("grouping", "day")
    defn = dict(defn, agg=how)
    if m["src"] == "activity" and how == "count":
        how = "sum"  # counting activities = adding up each day's number of sessions
    points = []
    for s, e, partial in buckets(start, end, grouping):
        vals, loaded = _window(defn, m, rows, checkins, s, e)
        points.append(dict(start=s.isoformat(), end=e.isoformat(), label=_bucket_label(s, e, grouping),
                           value=aggregate(vals, how, loaded), partial=partial or (e >= today and grouping != "day")))
    vals, loaded = _window(defn, m, rows, checkins, start, end)
    total = aggregate(vals, how, loaded)
    span = (end - start).days + 1
    p_end, p_start = start - timedelta(days=1), start - timedelta(days=span)
    prev = None
    if p_start >= today - timedelta(days=HISTORY_DAYS):
        pv, pl = _window(defn, m, rows, checkins, p_start, p_end)
        prev = aggregate(pv, how, pl) if pl >= span * 0.8 else None  # don't compare against a mostly empty window
    change = None if prev in (None, 0) or total is None else round((total - prev) / abs(prev) * 100, 1)
    gv, gnote = goal_value(defn, goal)
    return dict(points=points, start=start.isoformat(), end=end.isoformat(), total=total, prev_total=prev,
                prev_start=p_start.isoformat(), prev_end=p_end.isoformat(), change_pct=change,
                goal_value=gv, goal_note=gnote, unit=m["unit"], label=m["label"], agg=defn["agg"], grouping=grouping,
                lower_better=bool(m.get("lower")), src=m["src"])


def needed_window(defn: dict, today: date) -> tuple[date, date]:
    """Days a chart reads: its range plus the previous same-length window (within the history cap)."""
    start, end = resolve_range(defn, today)
    p_start = start - timedelta(days=(end - start).days + 1)
    return max(p_start, today - timedelta(days=HISTORY_DAYS)), end


def months_between(s: date, e: date) -> list[str]:
    out, cur = [], s.replace(day=1)
    while cur <= e:
        out.append(f"{cur:%Y-%m}")
        cur = add_months(cur, 1)
    return out
