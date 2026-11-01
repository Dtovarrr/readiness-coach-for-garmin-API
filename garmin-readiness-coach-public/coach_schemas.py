"""JSON shapes for the coach's answers, and the checks they must pass before anything is shown or cached.

The API enforces the schema's structure (structured outputs); the length and count limits below
aren't expressible there (no maxLength/maxItems), so `validate` checks them here. A result that fails
gets one repair round in ai.call, then the rule-based fallback.
"""
from __future__ import annotations

S = {"type": "string"}


def _list(items=S):
    return {"type": "array", "items": items}


def _obj(props: dict) -> dict:
    return {"type": "object", "properties": props, "required": list(props), "additionalProperties": False}


SCHEMAS = {
    "briefing": _obj(dict(
        verdict=S,                      # one line
        readiness_note=S,               # why, in a sentence, with the numbers
        plan=_obj(dict(type=S, duration_min={"type": "integer"}, intensity={"type": "string", "enum": ["rest", "easy", "moderate", "hard"]},
                       details=S)),
        recovery=_list(), watch_outs=_list(),
        goals=_list(_obj(dict(label=S, status=S, next_step=S))),
        reasons=_list())),
    "chat": _obj(dict(answer=S, remember=_list())),
    "activity": _obj(dict(headline=S, how_it_went=S, recovery_impact=S, compared=S, suggestions=_list())),
    "dexa": _obj(dict(headline=S, what_changed=S, suggests=S, vat_balance=S, bone=S, next_steps=_list())),
    "dexa_trend": _obj(dict(headline=S, rates=S, lined_up=S, goals=S)),
    "review": _obj(dict(headline=S, went_well=_list(), watch=_list(), next_week=_list(), reasons=_list())),
    "goal_plan": _obj(dict(where_you_stand=_list(), this_week=_list(), adjustments=_list())),
}

# field -> (max characters per string, max list length)
LIMITS = {
    "briefing": dict(verdict=(160, 0), readiness_note=(400, 0), plan=(400, 0), recovery=(240, 4), watch_outs=(240, 4),
                     goals=(300, 6), reasons=(240, 5)),
    "chat": dict(answer=(2400, 0), remember=(200, 2)),
    "activity": dict(headline=(160, 0), how_it_went=(1200, 0), recovery_impact=(900, 0), compared=(900, 0), suggestions=(300, 3)),
    "dexa": dict(headline=(160, 0), what_changed=(1200, 0), suggests=(1200, 0), vat_balance=(900, 0), bone=(600, 0), next_steps=(300, 6)),
    "dexa_trend": dict(headline=(160, 0), rates=(900, 0), lined_up=(900, 0), goals=(900, 0)),
    "review": dict(headline=(160, 0), went_well=(300, 5), watch=(300, 5), next_week=(300, 5), reasons=(240, 5)),
    "goal_plan": dict(where_you_stand=(300, 8), this_week=(300, 6), adjustments=(300, 5)),
}


def _check(v, schema: dict, path: str) -> str | None:
    t = schema.get("type")
    if t == "object":
        if not isinstance(v, dict):
            return f"{path} should be an object"
        missing = [k for k in schema["required"] if k not in v]
        if missing:
            return f"{path} is missing {', '.join(missing)}"
        extra = [k for k in v if k not in schema["properties"]]
        if extra:
            return f"{path} has unexpected {', '.join(extra)}"
        for k, sub in schema["properties"].items():
            p = _check(v[k], sub, f"{path}.{k}")
            if p:
                return p
    elif t == "array":
        if not isinstance(v, list):
            return f"{path} should be a list"
        for i, x in enumerate(v):
            p = _check(x, schema["items"], f"{path}[{i}]")
            if p:
                return p
    elif t == "string":
        if not isinstance(v, str):
            return f"{path} should be text"
        if "enum" in schema and v.lower() not in schema["enum"]:
            return f"{path} must be one of {', '.join(schema['enum'])}"
    elif t == "integer":
        if not isinstance(v, int) or isinstance(v, bool) or not 0 <= v <= 600:
            return f"{path} should be a whole number of minutes (0-600)"
    return None


def _length(v) -> int:
    if isinstance(v, dict):
        return max((_length(x) for x in v.values()), default=0)
    if isinstance(v, list):
        return max((_length(x) for x in v), default=0)
    return len(v) if isinstance(v, str) else 0


def validate(feature: str, data) -> str | None:
    """None if `data` is a usable answer for `feature`, else what's wrong (fed back for one repair)."""
    p = _check(data, SCHEMAS[feature], feature)
    if p:
        return p
    for field, (chars, items) in LIMITS[feature].items():
        v = data[field]
        if isinstance(v, list) and items and len(v) > items:
            return f"{feature}.{field} has {len(v)} items; keep it to {items}"
        if _length(v) > chars:
            return f"{feature}.{field} is too long; keep each item under {chars} characters"
    if feature == "briefing" and not data["verdict"].strip():
        return "briefing.verdict is empty"
    if feature == "chat" and not data["answer"].strip():
        return "chat.answer is empty"
    return None


def validator(feature: str):
    return lambda data: validate(feature, data)


def to_markdown(feature: str, d: dict) -> str:
    """A readable text version (logs, exports, and older pages that only render markdown)."""
    if feature == "briefing":
        p = d["plan"]
        lines = [f"**{d['verdict']}**", "", d["readiness_note"], "", "**Today's plan**",
                 f"- {p['type']} · {p['duration_min']} min · {p['intensity']}: {p['details']}"]
        for title, key in (("Recovery tonight", "recovery"), ("Watch-outs", "watch_outs"), ("Why", "reasons")):
            if d[key]:
                lines += ["", f"**{title}**"] + [f"- {x}" for x in d[key]]
        if d["goals"]:
            lines += ["", "**Goals**"] + [f"- {g['label']}: {g['status']}. {g['next_step']}" for g in d["goals"]]
        return "\n".join(lines)
    if feature == "chat":
        return d["answer"]
    out = []
    for k, v in d.items():
        title = k.replace("_", " ").capitalize()
        if isinstance(v, list):
            if v:
                out += [f"### {title}"] + [f"- {x}" for x in v]
        elif k == "headline":
            out.append(f"**{v}**")
        elif v:
            out += [f"### {title}", v]
    return "\n\n".join(out)
