"""Units: the one place metric values are converted or formatted for people and for the coach.

Everything stored or cached stays in the API's metric units (kg, m, m/s, s/km, deg C). Display
code and coach payloads go through these helpers, so "US" vs "metric" is decided here only.
The page has a small JS twin of this module (`Units` in static/index.html).
"""
from __future__ import annotations

import math

KG_LB = 2.20462262
M_MI = 1609.344
M_FT = 0.3048
M_YD = 0.9144
M_IN = 0.0254
SYSTEMS = ("us", "metric")
DEFAULT = "us"


def norm(system: str | None) -> str:
    return system if system in SYSTEMS else DEFAULT


def is_us(system: str | None) -> bool:
    return norm(system) == "us"


# ------------------------------- converters --------------------------------- #
def weight(kg, system):
    return None if kg is None else kg * KG_LB if is_us(system) else kg


def dist(m, system, swim=False):
    if m is None:
        return None
    if swim:
        return m / M_YD if is_us(system) else m
    return m / M_MI if is_us(system) else m / 1000


def elev(m, system):
    return None if m is None else m / M_FT if is_us(system) else m


def pace(s_per_km, system):
    """Seconds per mile (US) or per km; also right for signed pace deltas."""
    return None if s_per_km is None else s_per_km * M_MI / 1000 if is_us(system) else s_per_km


def speed(m_s, system):
    return None if m_s is None else m_s * 3600 / M_MI if is_us(system) else m_s * 3.6


def temp(c, system):
    return None if c is None else c * 9 / 5 + 32 if is_us(system) else c


def weight_unit(system):
    return "lb" if is_us(system) else "kg"


def dist_unit(system, swim=False):
    return ("yd" if is_us(system) else "m") if swim else ("mi" if is_us(system) else "km")


# ------------------------------- formatters --------------------------------- #
def _sign(x):
    return "+" if x > 0 else "−" if x < 0 else "±"


def fmt_mmss(sec) -> str:
    if sec is None or not math.isfinite(sec):
        return "–"
    neg, s = sec < 0, round(abs(sec))
    h, m, x = s // 3600, s % 3600 // 60, s % 60
    out = f"{h}:{m:02d}:{x:02d}" if h else f"{m}:{x:02d}"
    return "-" + out if neg else out


def fmt_weight(kg, system, d=1):
    return "–" if kg is None else f"{weight(kg, system):,.{d}f} {weight_unit(system)}"


def fmt_weight_delta(kg, system, d=1):
    return "–" if kg is None else f"{_sign(kg)}{abs(weight(kg, system)):,.{d}f} {weight_unit(system)}"


def fmt_dist(m, system, d=1, swim=False):
    return "–" if m is None else f"{dist(m, system, swim):,.{0 if swim else d}f} {dist_unit(system, swim)}"


def fmt_pace(s_per_km, system):
    return "–" if not s_per_km else f"{fmt_mmss(pace(s_per_km, system))} /{dist_unit(system)}"


def fmt_speed(m_s, system):
    return "–" if m_s is None else f"{speed(m_s, system):.1f} {'mph' if is_us(system) else 'km/h'}"


def fmt_elev(m, system):
    return "–" if m is None else f"{elev(m, system):,.0f} {'ft' if is_us(system) else 'm'}"


def fmt_temp(c, system):
    return "–" if c is None else f"{temp(c, system):.0f} {'°F' if is_us(system) else '°C'}"


def fmt_height(m, system):
    if not m:
        return "–"
    if not is_us(system):
        return f"{m * 100:.0f} cm"
    inches = round(m / M_IN)
    return f"{inches // 12}′{inches % 12}″"


# --------------------------------- goals ------------------------------------ #
_GOAL_UNITS = {"body_fat": "%", "fitness_age": "years", "weekly_active_min": "min/week", "sleep_h": "h/night",
               "steps": "steps/day", "custom": ""}
_GOAL_RATE_UNITS = {"body_fat": "pts", "fitness_age": "yrs", "weekly_active_min": "min", "sleep_h": "h", "steps": "steps"}


def goal_factor(goal_type: str, system) -> float:
    """Metric -> display multiplier for a goal type (only weight and running distance change)."""
    if not is_us(system):
        return 1.0
    return {"weight": KG_LB, "lean_mass": KG_LB, "weekly_run_km": 1000 / M_MI}.get(goal_type, 1.0)


def goal_to_display(goal_type, v, system):
    return None if v is None else v * goal_factor(goal_type, system)


def goal_from_display(goal_type, v, system):
    return None if v is None else v / goal_factor(goal_type, system)


def goal_unit(goal_type, system) -> str:
    if goal_type in ("weight", "lean_mass"):
        return weight_unit(system)
    if goal_type == "weekly_run_km":
        return f"{dist_unit(system)}/week"
    return _GOAL_UNITS.get(goal_type, "")


def goal_decimals(goal_type) -> int:
    return 0 if goal_type in ("weekly_active_min", "steps") else 1


def fmt_goal(goal_type, v, system, unit=True) -> str:
    if v is None:
        return "–"
    s = f"{goal_to_display(goal_type, v, system):,.{goal_decimals(goal_type)}f}"
    u = goal_unit(goal_type, system)
    return f"{s} {u}" if unit and u else s


def fmt_goal_rate(goal_type, v, system) -> str:
    if v is None:
        return "–"
    d = 0 if goal_type in ("weekly_active_min", "steps") else 2
    u = {"weight": weight_unit(system), "lean_mass": weight_unit(system), "weekly_run_km": dist_unit(system)}.get(goal_type, _GOAL_RATE_UNITS.get(goal_type, ""))
    return f"{_sign(v)}{abs(goal_to_display(goal_type, v, system)):,.{d}f} {u}/wk"


# ------------------------------ coach payloads ------------------------------ #
_VALUE_FIELDS = {"value", "current", "start", "target", "required_per_week", "recent_trend_per_week",
                 "projected_at_deadline", "trend_per_week", "projected"}
_GOAL_BY_UNIT = {"kg": "weight", "km/week": "weekly_run_km"}


def _is_num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def _convert_key(k: str, v):
    """Rename + convert one metric field by its key suffix; unknown keys pass through."""
    num = _is_num(v)
    if k.endswith("_s_per_km") or k == "s_per_km":
        return k[:-len("s_per_km")] + "per_mi", fmt_mmss(pace(v, "us")) if num else v
    toks = k.split("_")
    last = toks[-1]
    if "kg" in toks:
        return "_".join("lb" if t == "kg" else t for t in toks), round(v * KG_LB, 2) if num else v
    if last == "km":
        return "_".join(toks[:-1] + ["mi"]), round(v * 1000 / M_MI, 2) if num else v
    if last == "kmh":
        return "_".join(toks[:-1] + ["mph"]), round(v / (M_MI / 1000), 1) if num else v
    if k == "speed_ms":
        return "speed_mph", round(speed(v, "us"), 2) if num else v
    if last == "m" and len(toks) > 1:
        if "dist" in toks or "distance" in toks:
            return "_".join(toks[:-1] + ["mi"]), round(v / M_MI, 2) if num else v
        return "_".join(toks[:-1] + ["ft"]), round(v / M_FT) if num else v
    if last == "c" and len(toks) > 1:
        return "_".join(toks[:-1] + ["f"]), round(temp(v, "us")) if num else v
    return k, v


def localize(obj, system):
    """Return a copy of a coach payload in the user's units (metric: unchanged). Keys are
    renamed to match (weight_7d_avg_kg -> weight_7d_avg_lb, avg_pace_s_per_km -> avg_pace_per_mi
    as "m:ss"), and goal-like dicts carrying a `unit` of kg or km/week have their values converted."""
    if not is_us(system):
        return obj
    if isinstance(obj, list):
        return [localize(x, system) for x in obj]
    if not isinstance(obj, dict):
        return obj
    goal_t = _GOAL_BY_UNIT.get(obj.get("unit")) if isinstance(obj.get("unit"), str) else None
    out = {}
    for k, v in obj.items():
        if isinstance(v, (dict, list)):
            out[k] = localize(v, system)
        elif goal_t and k == "unit":
            out[k] = goal_unit(goal_t, system)
        elif goal_t and k in _VALUE_FIELDS and _is_num(v):
            out[k] = round(goal_to_display(goal_t, v, system), 2)
        else:
            nk, nv = _convert_key(k, v)
            out[nk] = nv
    return out


def instruction(system) -> str:
    """Appended to every coach system prompt."""
    if is_us(system):
        return (" Units: the athlete uses US units. Every number in DATA is already in US units (lb, miles, ft, mph, °F; "
                "fields ending _per_mi are paces as m:ss per mile). Write lb, mi, ft, mph and °F, and paces as m:ss /mi; "
                "do not convert anything to metric.")
    return (" Units: metric. Numbers in DATA are metric (kg, km, m, km/h, °C); fields ending _s_per_km are paces in "
            "seconds per km - write them as m:ss /km.")
