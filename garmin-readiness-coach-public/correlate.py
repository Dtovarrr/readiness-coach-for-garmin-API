"""Correlation Explorer: do two of your daily measures move together, same day or a few days apart?

Pure functions (stdlib only) over the per-day rows the charts use, plus check-ins. Values stay metric;
the page converts for display. The bar for calling something a pattern is deliberately high:
  - at least MIN_DAYS matched days, or no result at all ("not enough data yet");
  - Spearman |rho| >= FINDING_RHO *and* a 95% interval that excludes 0, or it's "no clear relationship".
Wording is about tendencies and associations, never causes.
"""
from __future__ import annotations

import math
import statistics as st
from datetime import date, timedelta

import charts
import units

MIN_DAYS = 21
FINDING_RHO = 0.3
MIN_GROUP = 5  # each side of the plain-language split needs this many days
LAGS = (0, 1, 2, 3)
RANGES = {"90D": 90, "6M": 183, "1Y": 365}
REMINDER = "Correlation isn’t causation: something else (stress, illness, a busy week) can drive both."

# key -> label, unit (metric), kind, chart definition for charts.day_value, a natural split for the sentence
GARMIN = {
    "sleep_h": dict(label="Sleep", noun="sleep", unit="h", metric="sleep_h", split=7.0, split_text="7+ hours of sleep"),
    "sleep_score": dict(label="Sleep score", noun="sleep score", unit="score", metric="sleep_score"),
    "hrv": dict(label="HRV", noun="HRV", unit="ms", metric="hrv"),
    "rhr": dict(label="Resting HR", noun="resting heart rate", unit="bpm", metric="rhr"),
    "bb_high": dict(label="Body Battery", noun="Body Battery", unit="score", metric="bb_high"),
    "stress": dict(label="Stress", noun="stress", unit="score", metric="stress"),
    "steps": dict(label="Steps", noun="steps", unit="steps", metric="steps"),
    "load": dict(label="Training load", noun="training load", unit="load", metric="load"),
    "weight": dict(label="Weight", noun="weight", unit="kg", metric="weight"),
}


# ---------------------------------- measures ---------------------------------- #
def _name(t: str) -> str:
    """Garmin type key -> words: "tennis_v2" -> "tennis", "strength_training" -> "strength training"."""
    return t.removesuffix("_v2").replace("_", " ")


def options(activity_types: list[str], checkin_inds: list[dict]) -> list[dict]:
    """Everything that can be compared, grouped for the pickers."""
    out = [dict(key=k, label=v["label"], unit=v["unit"], kind="continuous", group="Garmin") for k, v in GARMIN.items()]
    out += [dict(key="run_mi", label="Miles run", unit="m", kind="continuous", group="Activities"),
            dict(key="workout", label="Workout (yes/no)", unit="", kind="binary", group="Activities")]
    for t in dict.fromkeys(t for t in activity_types if t):  # your own types, most frequent first
        out.append(dict(key=f"hours:{t}", label=f"Hours of {_name(t)}", unit="h", kind="continuous", group="Activities"))
    out += [dict(key=f"checkin:{i['id']}", label=f"{i['label']} (check-in)", unit="rating", kind="rating", group="Check-ins")
            for i in checkin_inds if i.get("enabled", True)]
    return out


def measure(key: str, labels: dict[str, str] | None = None) -> dict | None:
    """Label, kind and the chart definition that reads it from a day row (None if unknown)."""
    labels = labels or {}
    if key in GARMIN:
        g = GARMIN[key]
        return dict(key=key, label=g["label"], noun=g["noun"], unit=g["unit"], kind="continuous", split=g.get("split"),
                    split_text=g.get("split_text"), defn=dict(metric=g["metric"]))
    if key == "run_mi":
        return dict(key=key, label="Miles run", noun="running distance", unit="m", kind="continuous",
                    defn=dict(metric="act_distance", types=["run"], agg="sum"))
    if key == "workout":
        return dict(key=key, label="Workout", noun="a workout", unit="", kind="binary",
                    defn=dict(metric="act_sessions", exclude_walks=True, agg="sum"))
    if key.startswith("hours:") and len(key) > 6:
        t = key[6:].lower()[:40]
        name = _name(t)
        return dict(key=key, label=f"Hours of {name}", noun=f"{name} time", unit="h", kind="continuous",
                    defn=dict(metric="act_hours", types=[t], agg="sum"))
    if key.startswith("checkin:") and key[8:] in labels:
        lab = labels[key[8:]]
        return dict(key=key, label=f"{lab} (check-in)", noun=lab.lower(), unit="rating", kind="rating", checkin=True,
                    defn=dict(metric=key))
    return None


def needs_daysum(*ms: dict) -> bool:
    return any((charts.metric_info(m["defn"]["metric"]) or {}).get("src") == "daysum" for m in ms if not m.get("checkin"))


def values(m: dict, rows: dict[str, dict], checkins: dict[str, dict], labels: dict[str, str]) -> dict[str, float]:
    """iso day -> value. Garmin days without a reading are absent; activity measures are 0 on a loaded
    day without a matching activity; check-ins only exist where you rated that indicator."""
    info = charts.metric_info(m["defn"]["metric"], labels)
    out = {}
    days = checkins if m.get("checkin") else rows
    for k in days:
        v = charts.day_value(m["defn"], info, rows.get(k), checkins.get(k))
        if v is None:
            continue
        out[k] = (1.0 if v > 0 else 0.0) if m["kind"] == "binary" else float(v)
    return out


def pairs(xs: dict[str, float], ys: dict[str, float], lag: int) -> list[tuple[str, float, float]]:
    """(Y's day, x, y) for every day where Y has a value and X has one `lag` days earlier."""
    out = []
    for k in sorted(ys):
        kx = (date.fromisoformat(k) - timedelta(days=lag)).isoformat()
        if kx in xs:
            out.append((k, xs[kx], ys[k]))
    return out


# --------------------------------- statistics --------------------------------- #
def ranks(v: list[float]) -> list[float]:
    """1-based ranks; tied values share the average of their ranks."""
    order = sorted(range(len(v)), key=lambda i: v[i])
    r = [0.0] * len(v)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and v[order[j + 1]] == v[order[i]]:
            j += 1
        for k in range(i, j + 1):
            r[order[k]] = (i + j) / 2 + 1
        i = j + 1
    return r


def pearson(a: list[float], b: list[float]) -> float | None:
    if len(a) < 3:
        return None
    ma, mb = st.fmean(a), st.fmean(b)
    sa = math.fsum((x - ma) ** 2 for x in a)
    sb = math.fsum((y - mb) ** 2 for y in b)
    if sa == 0 or sb == 0:
        return None
    return math.fsum((x - ma) * (y - mb) for x, y in zip(a, b)) / math.sqrt(sa * sb)


def spearman(x: list[float], y: list[float]) -> float | None:
    return pearson(ranks(x), ranks(y))


def interval(rho: float, n: int) -> tuple[float, float, float]:
    """(low, high, two-sided p) from the Fisher z transform with the Spearman standard error
    sqrt(1.06 / (n - 3)) (Fieller, Hartley & Pearson)."""
    z = math.atanh(max(-0.999999, min(0.999999, rho)))
    se = math.sqrt(1.06 / (n - 3))
    p = math.erfc(abs(z) / se / math.sqrt(2))
    return math.tanh(z - 1.96 * se), math.tanh(z + 1.96 * se), p


def trend(x: list[float], y: list[float]) -> tuple[float, float] | None:
    """Least-squares slope and intercept, for drawing the line only."""
    mx, my = st.fmean(x), st.fmean(y)
    sxx = math.fsum((a - mx) ** 2 for a in x)
    if not sxx:
        return None
    slope = math.fsum((a - mx) * (b - my) for a, b in zip(x, y)) / sxx
    return slope, my - slope * mx


def strength(rho: float) -> str:
    a = abs(rho)
    return "strong" if a >= 0.5 else "moderate" if a >= FINDING_RHO else "weak" if a >= 0.1 else "negligible"


def confidence(finding: bool, p: float, n: int) -> str:
    if not finding:
        return "Low"
    return "High" if p < 0.001 and n >= 45 else "Medium" if p < 0.01 else "Low"


# ---------------------------------- wording ----------------------------------- #
WHEN = {0: "On days with", 1: "On days after", 2: "Two days after", 3: "Three days after"}
LATER = {0: "on the same day", 1: "the next day", 2: "two days later", 3: "three days later"}


def _split(xm: dict, xs: list[float]) -> tuple[callable, str] | None:
    """(test for the "high" group, how to say it) for the plain-language sentence."""
    if xm["kind"] == "binary":
        return (lambda v: v >= 1), "a workout"
    if xm["kind"] == "rating":
        return (lambda v: v >= 1), f"a thumbs-up {xm['noun']} rating"
    if xm.get("split") is not None and min(xs) < xm["split"] <= max(xs):
        return (lambda v: v >= xm["split"]), xm["split_text"]
    med = st.median(xs)
    if max(xs) <= med:
        return None
    return (lambda v: v > med), f"higher-than-usual {xm['noun']}"


def _share(ym: dict, ys: list[float], y_med: float) -> tuple[callable, str]:
    if ym["kind"] == "rating":
        return (lambda v: v >= 1), f"your {ym['noun']} was rated thumbs up"
    if ym["kind"] == "binary":
        return (lambda v: v >= 1), "you worked out"
    return (lambda v: v > y_med), f"your {ym['noun']} was above your usual"


def describe(xm: dict, ym: dict, lag: int, x: list[float], y: list[float], rho: float, finding: bool) -> dict:
    """The sentence plus the two group shares behind it (None when a group is too small to say)."""
    out = dict(sentence=None, summary=None, groups=None)
    split = _split(xm, x)
    if split:
        hi, cond = split
        test, phrase = _share(ym, y, st.median(y))
        a = [yy for xx, yy in zip(x, y) if hi(xx)]
        b = [yy for xx, yy in zip(x, y) if not hi(xx)]
        if len(a) >= MIN_GROUP and len(b) >= MIN_GROUP:
            pa, pb = round(100 * sum(map(test, a)) / len(a)), round(100 * sum(map(test, b)) / len(b))
            out["groups"] = dict(condition=cond, with_days=len(a), without_days=len(b), with_pct=pa, without_pct=pb)
            if finding:
                out["sentence"] = f"{WHEN[lag]} {cond}, {phrase} on {pa}% of those days, vs {pb}% otherwise."
    direction = "higher" if rho > 0 else "lower"
    lead = f"{xm['label']} {'(yes vs no) ' if xm['kind'] == 'binary' else ''}"
    if finding:
        out["summary"] = (f"{lead}tends to go with {direction} {ym['noun']} {LATER[lag]}: a {strength(rho)} association "
                          f"(ρ = {rho:+.2f}).")
    else:
        out["summary"] = (f"No clear relationship: {xm['label'].lower()} and {ym['noun']} {LATER[lag]} don’t move together "
                          f"consistently here (ρ = {rho:+.2f}, {strength(rho)}). That’s not proof there’s no link, only that "
                          "this data doesn’t show one.")
    return out


# ---------------------------------- analysis ----------------------------------- #
def analyze(xm: dict, ym: dict, xs: dict[str, float], ys: dict[str, float], lag: int) -> dict:
    matched = pairs(xs, ys, lag)
    n = len(matched)
    base = dict(x=xm["key"], y=ym["key"], x_label=xm["label"], y_label=ym["label"], x_unit=xm["unit"], y_unit=ym["unit"],
                x_kind=xm["kind"], y_kind=ym["kind"], lag=lag, n=n, min_days=MIN_DAYS, reminder=REMINDER,
                points=[dict(day=k, x=a, y=b) for k, a, b in matched])
    if n < MIN_DAYS:
        return dict(base, status="not_enough", finding=False,
                    message=f"Not enough data yet: {n} matched day{'s' if n != 1 else ''} of the {MIN_DAYS} needed.")
    x, y = [a for _, a, _ in matched], [b for _, _, b in matched]
    rho = spearman(x, y)
    if rho is None:
        which = xm["label"] if len(set(x)) == 1 else ym["label"]
        return dict(base, status="no_variation", finding=False,
                    message=f"{which} is the same on every matched day, so there’s nothing to compare yet.")
    lo, hi, p = interval(rho, n)
    finding = abs(rho) >= FINDING_RHO and (lo > 0 or hi < 0)
    words = describe(xm, ym, lag, x, y, rho, finding)
    return dict(base, status="ok", finding=finding, rho=round(rho, 3), ci=[round(lo, 3), round(hi, 3)], p=p,
                strength=strength(rho), confidence=confidence(finding, p, n), trend=trend(x, y), **words)


# ---------------------------------- top patterns --------------------------------- #
TOP_PAIRS = [  # (x, y, lag)
    ("sleep_h", "hrv", 0), ("load", "hrv", 1), ("load", "rhr", 1), ("sleep_score", "hrv", 0),
    ("steps", "sleep_score", 1), ("stress", "sleep_score", 1), ("load", "bb_high", 1),
    ("sleep_h", "checkin:energy", 0), ("sleep_score", "checkin:mood", 0), ("hrv", "checkin:energy", 0),
    ("bb_high", "checkin:energy", 0), ("workout", "checkin:mood", 0), ("workout", "checkin:mood", 1),
    ("load", "checkin:fitness", 1), ("sleep_h", "checkin:mood", 0), ("stress", "checkin:mood", 0),
]


def top(series: dict[str, dict[str, float]], ms: dict[str, dict]) -> list[dict]:
    """Every curated pair that clears the bar, strongest first."""
    out = []
    for xk, yk, lag in TOP_PAIRS:
        if xk in ms and yk in ms:
            r = analyze(ms[xk], ms[yk], series[xk], series[yk], lag)
            if r["finding"]:
                out.append({k: v for k, v in r.items() if k != "points"})
    return sorted(out, key=lambda r: -abs(r["rho"]))


# ------------------------------------ coach ------------------------------------ #
def coach_payload(r: dict) -> dict:
    """Derived statistics only: labels, lag, sample size, rho, interval, p, confidence and group shares."""
    keep = ("x_label", "y_label", "lag", "n", "rho", "ci", "strength", "confidence", "finding", "groups", "summary")
    out = {k: r.get(k) for k in keep}
    out["p_value"] = round(r["p"], 4) if r.get("p") is not None else None
    out["lag_meaning"] = f"{r['x_label']} on one day vs {r['y_label']} {LATER[r['lag']]}"
    out["thresholds"] = dict(min_days=MIN_DAYS, min_abs_rho=FINDING_RHO, interval="95% Fisher z")
    return out


EXPLAIN_SYSTEM = (
    "You are an evidence-minded coach in a personal fitness dashboard. You receive the statistics of ONE correlation "
    "between two of the athlete's daily measures (Spearman rho, 95% interval, p-value, number of matched days, the lag in "
    "days, and the share of days above usual in two groups). All numbers were computed in code; use them as given. "
    "Write ~120-180 words of markdown with exactly these ### headings: 'What this shows' (plain language, using 'tends to' "
    "or 'is associated with'; never say one thing causes or leads to the other; mention how sure we can be from n, rho and "
    "the interval), 'Why it might happen' (one or two plausible explanations, including a third factor that could drive "
    "both), and 'Try this' (one small, safe two-week self-experiment with what to keep constant and what to compare). "
    "End with one line reminding that correlation isn't causation. If `finding` is false, say clearly that there's no "
    "reliable pattern yet and suggest how to collect better data. Not medical advice."
)


def explain(r: dict, system: str = units.DEFAULT) -> str:
    import coach
    return coach._claude(EXPLAIN_SYSTEM + units.instruction(system), f"DATA:\n{coach._data(coach_payload(r), system)}\n\nExplain this result.",
                         feature="insights")
