"""AI Readiness & Strain Coach.

Two layers:
  1. A deterministic scoring engine (always on, fully local).
  2. An optional Claude layer (set ANTHROPIC_API_KEY): the daily briefing and chat use read-only tools
     over local data (coach_tools.py), analyses get computed payloads, every answer is schema-checked
     (coach_schemas.py) and every call goes through ai.py (SDK, caching, usage log, budget).
Not medical advice - it is a training/recovery heuristic.
"""
from __future__ import annotations

import hashlib
import json
import statistics as st

import ai
import coach_schemas
import coach_tools
import goals
import health
import privacy
import store
import units

WEIGHTS = {"hrv": 0.35, "sleep": 0.30, "battery": 0.20, "load": 0.15}


def _clamp(x, lo=0.0, hi=100.0):
    return max(lo, min(hi, x))


def _vals(hist, key, n=None):
    xs = [h.get(key) for h in hist if h.get(key) is not None]
    return xs[-n:] if n else xs


def latest(hist, key):
    """Most recent non-null value (today's may not have synced yet)."""
    for h in reversed(hist):
        if h.get(key) is not None:
            return h[key], h["date"]
    return None, None


# ----------------------------- component scores ----------------------------- #
def score_hrv(hist):
    v, _ = latest(hist, "hrv")
    if v is None:
        return None, "No HRV reading"
    lo, _ = latest(hist, "hrv_low")
    hi, _ = latest(hist, "hrv_high")
    base = _vals(hist, "hrv", 21)[:-1]
    if lo is not None and hi is not None and hi > lo:
        if v < lo:
            s = _clamp(70 - (lo - v) / lo * 250)
        elif v <= hi:
            s = 70 + 30 * (v - lo) / (hi - lo)
        else:
            s = 100
        return s, f"{v:.0f} ms vs balanced range {lo:.0f}-{hi:.0f}"
    if len(base) >= 5:
        mean = st.mean(base)
        s = _clamp(75 + (v / mean - 1) * 250)
        return s, f"{v:.0f} ms vs your {mean:.0f} ms average"
    return 70.0, f"{v:.0f} ms (not enough baseline yet)"


def score_sleep(hist):
    sc, _ = latest(hist, "sleep_score")
    hrs, _ = latest(hist, "sleep_h")
    if sc is not None:
        return _clamp(sc), f"score {sc:.0f}" + (f", {hrs:.1f} h" if hrs else "")
    if hrs is not None:
        return _clamp(hrs / 8 * 100), f"{hrs:.1f} h"
    return None, "No sleep data"


def score_battery(hist):
    v, _ = latest(hist, "bb_high")
    if v is None:
        return None, "No Body Battery"
    return _clamp(v), f"peak {v:.0f}"


def load_stats(hist):
    loads = [h.get("load") or 0 for h in hist]
    acute = sum(loads[-7:])
    chronic_week = sum(loads[-28:]) / max(1, min(4, len(loads) / 7))
    ratio = acute / chronic_week if chronic_week > 0 else None
    yesterday = loads[-2] if len(loads) >= 2 else 0
    today = loads[-1] if loads else 0
    return dict(acute=round(acute), chronic_week=round(chronic_week), ratio=None if ratio is None else round(ratio, 2),
                yesterday=round(yesterday), today=round(today))


def score_load(hist):
    ls = load_stats(hist)
    r = ls["ratio"]
    if r is None:
        return None, "No training-load history"
    if r < 0.8:
        s, tag = 85, "detraining zone (light recent load)"
    elif r <= 1.3:
        s, tag = 92, "sweet spot"
    elif r <= 1.5:
        s, tag = 55, "ramping fast"
    else:
        s, tag = 25, "spike - injury/overreach risk"
    if ls["yesterday"] > 1.5 * (ls["chronic_week"] / 7 or 1) and ls["yesterday"] > 80:
        s -= 10
    return _clamp(s), f"7d:28d ratio {r} - {tag}"


# --------------------------------- main API --------------------------------- #
def assess(snap: dict) -> dict:
    hist = snap["history"]
    comps = {}
    for name, fn in (("hrv", score_hrv), ("sleep", score_sleep), ("battery", score_battery), ("load", score_load)):
        s, note = fn(hist)
        comps[name] = dict(score=None if s is None else round(s), note=note, weight=WEIGHTS[name])
    have = {k: v for k, v in comps.items() if v["score"] is not None}
    wsum = sum(v["weight"] for v in have.values())
    readiness = round(sum(v["score"] * v["weight"] for v in have.values()) / wsum) if wsum else None

    if readiness is None:
        zone, rec = "unknown", "Not enough data yet"
    elif readiness >= 80:
        zone, rec = "prime", "Green light - quality or high-intensity session"
    elif readiness >= 62:
        zone, rec = "good", "Moderate training - steady aerobic or technique work"
    elif readiness >= 45:
        zone, rec = "caution", "Easy day - Zone 1-2, mobility, short duration"
    else:
        zone, rec = "recover", "Recovery day - rest, walk, sleep extension"

    ls = load_stats(hist)
    flags = []
    hrv_t = _vals(hist, "hrv", 4)
    if len(hrv_t) >= 3 and all(b < a for a, b in zip(hrv_t, hrv_t[1:])):
        flags.append("HRV has dropped three nights running")
    rhr = _vals(hist, "rhr", 14)
    if len(rhr) >= 6 and rhr[-1] - st.mean(rhr[:-1]) >= 4:
        flags.append(f"Resting HR is {rhr[-1] - st.mean(rhr[:-1]):.0f} bpm above your recent average")
    sl = _vals(hist, "sleep_h", 5)
    if len(sl) >= 3 and st.mean(sl) < 6.5:
        flags.append(f"Sleep averaging {st.mean(sl):.1f} h over the last {len(sl)} nights")
    if ls["ratio"] and ls["ratio"] > 1.5:
        flags.append("Training load spike (7d:28d > 1.5)")
    hard_days = sum(1 for x in [h.get("load") or 0 for h in hist[-5:]] if x > 100)
    if hard_days >= 3:
        flags.append(f"{hard_days} hard sessions in the last 5 days")

    strain_label = ("Very high" if ls["ratio"] and ls["ratio"] > 1.5 else "High" if ls["ratio"] and ls["ratio"] > 1.3
                    else "Optimal" if ls["ratio"] and ls["ratio"] >= 0.8 else "Low" if ls["ratio"] else "n/a")
    return dict(readiness=readiness, zone=zone, recommendation=rec, components=comps,
                strain=dict(ls, label=strain_label), flags=flags)


def rule_briefing(snap: dict, a: dict, profile: dict | None = None, system: str = units.DEFAULT) -> str:
    c, ls = a["components"], a["strain"]
    lines = [f"**{a['recommendation']}** (readiness {a['readiness']}/100)", ""]
    lines.append("**What your body is saying**")
    for k, title in (("hrv", "HRV"), ("sleep", "Sleep"), ("battery", "Body Battery"), ("load", "Load")):
        if c[k]["score"] is not None:
            lines.append(f"- {title}: {c[k]['score']}/100 - {c[k]['note']}")
    lines += ["", "**Today's plan**"]
    plan = {
        "prime": ["Quality session OK: intervals, threshold, heavy lifts or a race-pace effort.",
                  "Warm up 15 min; stop intervals if HR/effort feels unusually high."],
        "good": ["Steady aerobic 45-75 min or skill/technique work; keep it conversational.",
                 "Optional short strides/strength - avoid maximal efforts."],
        "caution": ["Zone 1-2 for 30-45 min, or mobility/yoga.", "Skip intensity; reassess tomorrow morning."],
        "recover": ["Rest or a 20-30 min easy walk.", "Prioritise food, hydration and an early bedtime."],
        "unknown": ["Sync your watch and refresh."],
    }[a["zone"]]
    lines += [f"- {p}" for p in plan]
    lines += ["", "**Recovery levers tonight**"]
    sl = c["sleep"]["score"]
    lines.append("- Aim for a bedtime that gives 8+ h; last caffeine before ~2 pm." if sl is not None and sl < 75
                 else "- Keep your current sleep routine - it is working.")
    if ls["ratio"] and ls["ratio"] > 1.3:
        lines.append("- Load is climbing fast: make the next 1-2 days easy to absorb it.")
    elif ls["ratio"] and ls["ratio"] < 0.8:
        lines.append("- Recent load is light: safe to add volume gradually (~10%/week).")
    if a["flags"]:
        lines += ["", "**Watch-outs**"] + [f"- {f}" for f in a["flags"]]
    ci = (profile or {}).get("checkins") or {}
    if ci.get("mismatches") or ci.get("patterns"):
        lines += ["", "**Check-ins**"] + [f"- {t}" for t in (ci.get("mismatches") or [])[:2] + (ci.get("patterns") or [])[:1]]
    gl = (profile or {}).get("goals") or []
    if gl:
        lines += ["", "**Goals**"] + [
            f"- {g['label']}: {g['status'].lower()}" + (f" ({g['progress_pct']:.0f}% of the way)" if g.get("progress_pct") is not None else "")
            for g in gl] + ["- See the Goals tab for the plan and weekly pacing."]
    dx = (profile or {}).get("dexa") or {}
    if dx.get("latest_scan"):
        sc, w = dx["latest_scan"], lambda kg: units.fmt_weight(kg, system)
        line = f"- Latest DEXA ({sc['date']}, {dx['days_since_scan']} days ago): {sc.get('body_fat_pct', '–')}% body fat, lean {w(sc.get('lean_mass_kg'))}"
        ch = dx.get("change_vs_previous_scan")
        if ch:
            line += f"; since the previous scan fat {units.fmt_weight_delta(ch['fat_mass_kg'], system)}, lean {units.fmt_weight_delta(ch['lean_mass_kg'], system)}"
        lines += ["", "**Body composition**", line + "."]
        if ch and ch.get("lean_loss_with_fat_loss"):
            lines.append("- Lean mass fell along with fat: keep strength work and protein up to protect muscle.")
    lines += ["", "_Heuristic guidance, not medical advice. Persistent HRV/RHR changes or illness symptoms -> rest and consult a professional._"]
    return "\n".join(lines)


COACH_RULES = (
    "You are an evidence-minded endurance, strength and health coach embedded in a personal Garmin dashboard that runs on the "
    "athlete's own computer. Ground every claim in the data you are given or fetch with your tools; cite the numbers; never "
    "invent data that is missing - say what is missing instead. The data is derived numbers only: there are no names, "
    "locations or free-text notes, and you should not ask for them. Tools are read-only; call them when they would change "
    "your advice, and don't call ones that won't. Fitness guidance, not medical advice: mention a professional only if "
    "something looks concerning, and never diagnose. If `checkins` data is present it is self-reported wellness context "
    "(ratings and tags): at most one short observation, framed as how they feel. If mood has been consistently poor, gently "
    "encourage talking to someone they trust or a professional, without alarm. Safe pacing: weight change at most ~0.5-1% "
    "of body weight a week, training volume up at most ~10% a week. Follow the athlete's stated preferences for tone and "
    "length, and respect their injuries and limitations in every recommendation."
)

BRIEFING_SYSTEM = (
    "Task: today's briefing. DATA has today's computed readiness (score 0-100, zone, component notes, strain, flags), the "
    "last 7 days, and what else exists (goals, a plan session today, DEXA). Use tools only for what would change today's "
    "advice: get_training_plan when a session is planned, get_goal_progress when goals exist, get_activities for the last "
    "few days' sessions, get_user_profile is already in your instructions. Then answer with: verdict (one line), "
    "readiness_note (why, citing the numbers), plan (type, duration_min, intensity rest/easy/moderate/hard, details), "
    "recovery (2-3 actions for tonight), watch_outs (0-3), goals (one per goal you looked at: status and one next step; "
    "empty if none), reasons (2-4 short data points behind the plan)."
)

CHAT_SYSTEM = (
    "Task: answer the athlete's question in `answer` (under ~180 words, light markdown allowed), using tools to look up "
    "what you need. In `remember`, list at most 2 short facts the athlete stated about themselves that would matter for "
    "future coaching (an injury pattern, a schedule constraint, a preference - e.g. 'Knee flares up after running on "
    "pavement'), each under 120 characters and in third person; only things they said, never your own advice or "
    "something already in their profile or notes. Usually it is empty. The athlete confirms each one before it is saved."
)

ACTIVITY_SYSTEM = (
    "Task: analyse ONE workout from DATA (derived stats plus a comparison with similar past workouts; all computed in code). "
    "headline: one line. how_it_went: effort, pacing, HR drift/decoupling, splits - only what the data supports. "
    "recovery_impact: training effect, load vs usual, body battery, sleep/HRV after, and what the next 24-48 h should look "
    "like. compared: trends in pace, HR and efficiency vs similar workouts; say plainly if there are too few to judge. "
    "suggestions: 1-2 concrete, specific points. efficiency_m_per_beat = distance per heartbeat (higher is better at a "
    "given effort); decoupling under ~5% means good aerobic durability."
)

DEXA_SYSTEM = (
    "Task: analyse the athlete's latest DEXA scan from DATA (derived numbers from their scans, no report text, plus Garmin "
    "training, sleep and weight between scans). headline: one line. what_changed: fat vs lean since the previous and first "
    "scan, by region; say plainly when a change is inside the noise in noise_note. suggests: what the changes may suggest "
    "about training and nutrition, tied to training_between_previous_and_this_scan; if lean_loss_with_fat_loss is true call "
    "it out and say what tends to help (a smaller deficit, progressive strength training, enough protein). vat_balance: "
    "visceral fat, android/gynoid ratio against the report's < 1.0 guide, left/right differences over 2 lb. bone: one or "
    "two sentences, and say the report itself is not a bone density exam - anything concerning goes to a doctor. "
    "next_steps: 3-5 concrete points (weekly strength volume, a protein target, cardio, sleep, when to rescan). Use 'may' "
    "and 'tends to' language; small DEXA changes shift with hydration and timing."
)

DEXA_TREND_SYSTEM = (
    "Task: the trend across every DEXA scan in DATA (rates per month overall and between scans, Garmin training between "
    "scans, scale-vs-DEXA comparisons, goal projections - all computed in code). headline: one line. rates: the rates. "
    "lined_up: which stretches of training went with fat loss and lean gain or loss (correlation, not proof). goals: "
    "projected timelines from goal_projections with honest uncertainty (few scans, measurement noise, progress slows); say "
    "so when the trend isn't heading toward a goal."
)

GOAL_SYSTEM = (
    "Task: a short plan for the athlete's goals from DATA (goals with computed progress and guardrail warnings, body "
    "composition trends, fitness age and today's readiness). where_you_stand: one item per goal with status and the key "
    "numbers. this_week: concrete actions (training volume and intensity, sleep, recovery, weekly weight pacing where "
    "relevant). adjustments: only if a goal is behind, at risk or unrealistic - a realistic pace or date; else empty."
)

SUGGEST_SYSTEM = (
    "Task: propose 3-4 goals the athlete could adopt, based only on DATA (body composition trends, fitness age and its "
    "components, current value and weekly trend per metric, readiness, and their existing goals). Prefer goals that address "
    "what is lagging: fitness-age components holding them back, trends moving the wrong way, metrics below common "
    "guidelines. Do not repeat a goal type they already have; a complementary custom goal is fine. The target field uses the "
    "unit listed for that goal type in goal_types (weekly_active_min = Garmin intensity minutes per week); custom goals use "
    "target null. Deadlines are ISO dates after `today`. Keep every goal safe and realistic: never a target BMI below 18.5, "
    "body fat down at most ~0.5 points a week, fitness age not below Garmin's achievable estimate. Title: short (under 60 "
    "characters), in the same units as the target. Rationale: 1-2 sentences citing their numbers and why the pace is realistic."
)
GOAL_TYPES = ["weight", "body_fat", "fitness_age", "weekly_run_km", "weekly_active_min", "sleep_h", "steps", "custom"]
SUGGEST_SCHEMA = {
    "type": "object",
    "properties": {"suggestions": {"type": "array", "items": {
        "type": "object",
        "properties": {
            "type": {"type": "string", "enum": GOAL_TYPES},
            "title": {"type": "string"},
            "target": {"anyOf": [{"type": "number"}, {"type": "null"}]},
            "deadline": {"type": "string", "format": "date"},
            "rationale": {"type": "string"},
        },
        "required": ["type", "title", "target", "deadline", "rationale"],
        "additionalProperties": False,
    }}},
    "required": ["suggestions"],
    "additionalProperties": False,
}


def llm_enabled() -> bool:
    return ai.enabled()


def _data(obj, system) -> str:
    """Every coach payload goes out in the user's units, through the privacy filter."""
    return json.dumps(privacy.scrub(units.localize(obj, system)), default=str, separators=(",", ":"))


def coach_profile(body: dict | None, goal_views: list[dict] | None) -> dict:
    """Whitelisted, identifier-free body + goal context for the model: derived numbers only
    (no names, IDs, height or raw readings)."""
    out: dict = {}
    s = (body or {}).get("summary")
    if s:
        out["body"] = dict(
            latest_reading_date=s["latest"]["date"], days_since_reading=s["days_since_reading"],
            weight_7d_avg_kg=s["avg7"]["kg"], change_30d_kg=s["change_kg"]["d30"], change_90d_kg=s["change_kg"]["d90"],
            trend_kg_per_week=s["rate"]["kg_per_week"], trend_pct_bodyweight_per_week=s["rate"]["pct_per_week"],
            bmi=s["bmi"]["value"], bmi_category=s["bmi"]["category"], body_fat_7d_avg_pct=s["avg7"]["fat"],
            body_fat_trend_pts_per_week=s["fat_pts_per_week"], muscle_mass_kg=s["muscle_kg"])
    fa = (body or {}).get("fitness_age")
    if fa:
        out["fitness_age"] = dict(
            fitness_age=fa["fitness_age"], actual_age=fa["age"], achievable=fa["achievable"], previous=fa["previous"],
            components=[{k: c[k] for k in ("label", "unit", "value", "target", "potential_age", "status")} for c in fa["components"]])
    if goal_views:
        out["goals"] = [dict(
            type=v["goal"]["type"], label=v["label"][:200], unit=v["unit"], start=v["start"], current=v["current"],
            target=v["target"], deadline=v["goal"].get("deadline"), weeks_left=v["weeks_left"], progress_pct=v["progress_pct"],
            required_per_week=v["required_per_week"], recent_trend_per_week=v["actual_per_week"],
            projected_at_deadline=v["projected"], status=v["status_text"], warnings=[w["text"] for w in v.get("warnings") or []])
            for v in goal_views]  # goal notes are free text: they stay on this computer
    return out


# --------------------------------- memory ------------------------------------ #
PROFILE_FIELDS = {  # field -> (label shown to the coach, max characters)
    "injuries": ("Injuries and limitations", 600), "sports": ("Sports and match days", 400), "equipment": ("Equipment", 300),
    "schedule": ("Schedule constraints", 400), "goals_text": ("Goals in their own words", 600),
}
PREFERENCES = {"tone": ("friendly", "direct", "encouraging"), "brevity": ("short", "normal", "detailed")}


def memory_block() -> str:
    """About me + confirmed coach notes, as the second (cached) system block. The athlete wrote these
    for the coach; emails, phone numbers and keys are still redacted."""
    p, notes = store.coach_profile(), store.coach_notes()
    lines = [f"- {label}: {p[k]}" for k, (label, _) in PROFILE_FIELDS.items() if (p.get(k) or "").strip()]
    pref = [f"{k} {p[k]}" for k in PREFERENCES if p.get(k) in PREFERENCES[k]]
    if pref:
        lines.append("- Preferences: " + ", ".join(pref))
    if notes:
        lines.append("- Things the coach has learned (confirmed by the athlete):")
        lines += [f"  - {n['text']}" for n in notes]
    hint = feedback_hint()
    if hint:
        lines.append(f"- {hint}")
    if not lines:
        return ""
    return health.redact_text("About the athlete (their own words):\n" + "\n".join(lines))


FEEDBACK_REASONS = {
    "too_generic": ("too generic", "be specific: cite their numbers and give concrete sessions"),
    "wrong_numbers": ("wrong numbers", "double-check every number against the tool results, and say when you're unsure"),
    "too_cautious": ("too cautious", "when the data supports it, be more ambitious rather than defaulting to rest"),
    "not_realistic": ("not realistic for me", "fit suggestions to their schedule, equipment and constraints"),
}


def feedback_hint(days: int = 30) -> str:
    """One short style/accuracy line from recent thumbs-down reasons (2+ of a kind). Feedback never
    edits the profile; it only nudges the prompt."""
    from datetime import date, timedelta
    rows = store.feedback((date.today() - timedelta(days=days)).isoformat())
    counts: dict = {}
    for f in rows:
        if f["rating"] < 0:
            for r in f.get("reasons") or []:
                counts[r] = counts.get(r, 0) + 1
    top = [k for k, n in sorted(counts.items(), key=lambda x: -x[1]) if n >= 2 and k in FEEDBACK_REASONS][:2]
    if not top:
        return ""
    return "Recent feedback from the athlete: " + "; ".join(f"often '{FEEDBACK_REASONS[k][0]}', so {FEEDBACK_REASONS[k][1]}" for k in top) + "."


def memory_hash() -> str:
    return hashlib.sha256(memory_block().encode()).hexdigest()[:16]


def _system(task: str, system: str) -> list[str]:
    """[stable instructions + task + units, memory]: two cached system blocks."""
    return [COACH_RULES + "\n\n" + task + units.instruction(system), memory_block()]


# ------------------------------ structured calls ------------------------------ #
def structured(feature: str, task: str, payload: dict, ask: str, system: str = units.DEFAULT, essential: bool = True) -> dict:
    """One schema-checked answer from a computed payload: {source, data, text, model, usage}. Raises ai.AIError."""
    r = ai.call(feature, _system(task, system), f"DATA:\n{_data(payload, system)}\n\n{ask}",
                schema=coach_schemas.SCHEMAS[feature], validate=coach_schemas.validator(feature), essential=essential)
    return dict(source="claude", data=r["data"], text=coach_schemas.to_markdown(feature, r["data"]), model=r["model"], usage=r["usage"])


def _claude(system: str, content: str, effort: str = "medium", schema: dict | None = None, feature: str = "insights",
            essential: bool = True) -> str:
    """Plain-text (or raw-schema) call kept for the short write-ups (plan notes, insights). Raises ai.AIError."""
    r = ai.call(feature, [COACH_RULES + "\n\n" + system, memory_block()], content, schema=schema, essential=essential)
    return json.dumps(r["data"]) if schema else r["text"]


def _briefing_seed(snap: dict, a: dict, profile: dict | None) -> dict:
    """Small, computed starting point; the coach pulls anything else it needs with tools."""
    keep = ("date", "readiness", "hrv", "hrv_status", "sleep_h", "sleep_score", "rhr", "bb_high", "load", "stress")
    p = profile or {}
    return dict(
        today=snap["history"][-1]["date"] if snap.get("history") else None,
        assessment={k: a.get(k) for k in ("readiness", "zone", "recommendation", "components", "strain", "flags")},
        last_7_days=[{k: h.get(k) for k in keep if h.get(k) is not None} for h in snap["history"][-7:]],
        recent_activity_types=[x.get("type") for x in snap.get("activities", [])[:5]],
        available=dict(goals=len(p.get("goals") or []), dexa=bool(p.get("dexa")), checkins_shared=bool(p.get("checkins")),
                       plan_today=bool(p.get("plan_today"))),
    )


def briefing_inputs(snap: dict, a: dict, profile: dict | None) -> dict:
    """What the briefing depends on: the cache key hashes this (plus memory and units)."""
    return dict(seed=_briefing_seed(snap, a, profile), goals=(profile or {}).get("goals"), dexa=(profile or {}).get("dexa"),
                checkins=(profile or {}).get("checkins"))


def briefing(snap: dict, a: dict, profile: dict | None = None, system: str = units.DEFAULT, toolbox=None,
             essential: bool = False) -> dict:
    """{source, text, data?, note?}. Claude with tools when there's a key (and budget), else the rules."""
    rules = lambda note=None: dict(source="rules", text=rule_briefing(snap, a, profile, system), **({"note": note} if note else {}))
    if not llm_enabled():
        return rules()
    seed = _briefing_seed(snap, a, profile)
    # Tool definitions are ~2.4K prompt tokens: offer them only when there's something to look up
    # (goals, a plan session, DEXA, shared check-ins); otherwise the seed already has the whole day.
    if not any(seed["available"].values()):
        toolbox = None
    try:
        r = ai.call("briefing", _system(BRIEFING_SYSTEM, system),
                    f"DATA:\n{_data(seed, system)}\n\nWrite today's briefing.",
                    schema=coach_schemas.SCHEMAS["briefing"], validate=coach_schemas.validator("briefing"),
                    tools=coach_tools.definitions(toolbox.src) if toolbox else None, run_tool=toolbox.run if toolbox else None,
                    essential=essential)
    except ai.AIError as ex:
        return rules(f"{ex.info['title']}; showing the rule-based briefing." if ex.kind != "cap" else ex.info["explain"])
    return dict(source="claude", data=r["data"], text=coach_schemas.to_markdown("briefing", r["data"]), model=r["model"],
                usage=r["usage"], steps=r["steps"], tools_used=list(toolbox.calls) if toolbox else [])


def ask(question: str, snap: dict, a: dict, profile: dict | None = None, system: str = units.DEFAULT, toolbox=None) -> dict:
    """{text, remember[]} - chat with tools. Without a key, a plain explanation."""
    if not llm_enabled():
        return dict(text="Chat needs an ANTHROPIC_API_KEY in your .env - the rule-based coach can only produce the daily briefing.",
                    remember=[], source="rules")
    seed = dict(today=snap["history"][-1]["date"] if snap.get("history") else None,
                readiness={k: a.get(k) for k in ("readiness", "zone", "recommendation", "flags")})
    r = ai.call("chat", _system(CHAT_SYSTEM, system), f"DATA:\n{_data(seed, system)}\n\nQUESTION: {question}",
                schema=coach_schemas.SCHEMAS["chat"], validate=coach_schemas.validator("chat"),
                tools=coach_tools.definitions(toolbox.src) if toolbox else None, run_tool=toolbox.run if toolbox else None)
    known = memory_block().lower()
    rem = [x.strip() for x in r["data"]["remember"] if x.strip() and x.strip().lower() not in known][:2]
    return dict(text=r["data"]["answer"], remember=rem, source="claude", model=r["model"], usage=r["usage"], steps=r["steps"],
                tools_used=list(toolbox.calls) if toolbox else [])


def analyze_activity(stats: dict, system: str = units.DEFAULT) -> dict:
    return structured("activity", ACTIVITY_SYSTEM, stats, "Analyse this workout.", system)


def analyze_dexa(payload: dict, system: str = units.DEFAULT) -> dict:
    return structured("dexa", DEXA_SYSTEM, payload, "Analyse this DEXA scan.", system)


def dexa_trend(payload: dict, system: str = units.DEFAULT) -> dict:
    return structured("dexa_trend", DEXA_TREND_SYSTEM, payload, "Analyse the trend across these scans.", system)


def goal_plan(profile: dict, a: dict, goal_views: list[dict], body: dict | None, system: str = units.DEFAULT,
              essential: bool = False) -> dict:
    rules = lambda note=None: dict(source="rules", text=goals.rule_goal_plan(goal_views, body, a, system), **({"note": note} if note else {}))
    if not goal_views or not llm_enabled():
        return rules()
    readiness = dict(score=a.get("readiness"), zone=a.get("zone"), strain=a.get("strain"), flags=a.get("flags"))
    try:
        return structured("goal_plan", GOAL_SYSTEM, dict(profile, readiness=readiness), "Write the goal plan.", system, essential)
    except ai.AIError as ex:
        return rules(f"{ex.info['title']}; showing the rule-based plan." if ex.kind != "cap" else ex.info["explain"])


def suggest_goals(ctx: dict, system: str = units.DEFAULT) -> list[dict]:
    """Structured goal ideas (targets in the user's display units); the server converts them back to
    metric, then validates and guardrail-checks each one."""
    text = _claude(SUGGEST_SYSTEM + units.instruction(system), f"DATA:\n{_data(ctx, system)}\n\nSuggest goals.",
                   schema=SUGGEST_SCHEMA, feature="goal_suggest")
    return json.loads(text).get("suggestions") or []


# ------------------------------- conversations ------------------------------- #
THREAD_SYSTEM = (
    "Task: a conversation with the athlete. Answer in markdown, under ~200 words unless they ask for more. Look things up with "
    "tools before stating them: every number you write must come from a tool result, DATA, or their message - never estimate "
    "or invent one. If a tool result has status stale or no_data, or a data_feed_problem, say so plainly (what's missing and "
    "since when) and suggest checking System status. You cannot change anything yourself: to suggest a change (swap or move a "
    "planned session, a new goal, a workout to open in the builder, a check-in note, a deload, or something to remember about "
    "them), call the matching propose_* tool - it becomes a button they can press - and say so in one short sentence; never "
    "claim something was changed. Use propose_remember only for facts they stated about themselves. End by calling "
    "offer_followups with 2-3 short follow-up questions."
)
SUMMARY_SYSTEM = (
    "Summarize the earlier part of a coaching conversation in under 120 words for the coach's own reference: the athlete's "
    "questions, facts they stated, advice given, and anything they agreed to or declined. Plain sentences, no numbers that "
    "weren't in the text."
)
KEEP_MESSAGES = 6
SUMMARY_AT_TOKENS = 6000


def est_tokens(text: str) -> int:
    return len(text) // 4 + 1


def thread_plan(history: list[dict], summarized_upto: int) -> tuple[list[dict], list[dict]]:
    """(older messages to fold into the summary, recent messages kept verbatim). Everything stays
    verbatim while the history is small; past SUMMARY_AT_TOKENS the last KEEP_MESSAGES stay."""
    pending = [m for m in history if m["seq"] > summarized_upto]
    if sum(est_tokens(m["text"]) for m in pending) <= SUMMARY_AT_TOKENS:
        return [], pending
    keep = pending[-KEEP_MESSAGES:]
    while keep and keep[0]["role"] != "user":
        keep = keep[1:]
    older = [m for m in pending if m["seq"] < (keep[0]["seq"] if keep else 10**9)]
    return older, keep


def summarize(prev_summary: str, older: list[dict]) -> str:
    """Fold `older` turns into the running summary (fast model); without Claude, a plain extract."""
    convo = "\n".join(f"{m['role'].upper()}: {m['text'][:1500]}" for m in older)
    try:
        r = ai.call("summary", [SUMMARY_SYSTEM], f"EARLIER SUMMARY:\n{prev_summary or '(none)'}\n\nMORE TURNS:\n{convo}\n\nWrite the updated summary.",
                    essential=False)
        return r["text"][:1200]
    except ai.AIError:
        asked = [m["text"][:140] for m in older if m["role"] == "user"]
        return ((prev_summary + " ") if prev_summary else "") + "Earlier the athlete asked: " + " | ".join(asked)[:900]


def thread_messages(summary: str, recent: list[dict], question: str, seed: dict, system: str, context: dict | None = None) -> list[dict]:
    """API messages: the running summary and today's starting data ride in the first user turn (the system
    prompt stays byte-identical, so it stays cached)."""
    head = []
    if summary:
        head.append(f"Summary of earlier in this conversation: {summary}")
    msgs: list[dict] = []
    for m in recent:
        if msgs and msgs[-1]["role"] == m["role"]:
            msgs[-1]["content"] += "\n\n" + m["text"]
        else:
            msgs.append(dict(role=m["role"], content=m["text"]))
    ask = f"DATA:\n{_data(seed, system)}\n\n"
    if context:
        ask += f"The athlete opened this from: {context.get('label')}. Look at it first.\n\n"
    ask += f"QUESTION: {question}"
    if msgs and msgs[-1]["role"] == "user":
        msgs[-1]["content"] += "\n\n" + ask
    else:
        msgs.append(dict(role="user", content=ask))
    if head:
        msgs[0]["content"] = "\n\n".join(head) + "\n\n" + msgs[0]["content"]
    return msgs


def thread_stream(question: str, history: list[dict], summary: str, seed: dict, toolbox, system: str = units.DEFAULT,
                  context: dict | None = None):
    """Streams one answer: ("text", delta), ("tool", name), then ("done", {...}). Raises ai.AIError."""
    msgs = thread_messages(summary, history, question, seed, system, context)
    yield from ai.stream("thread", _system(THREAD_SYSTEM, system), msgs,
                         tools=coach_tools.definitions(toolbox.src, chat=True), run_tool=toolbox.run)


NOTICE_SYSTEM = (
    "Task: word ONE short note (max 2 sentences, under 45 words) for a 'Coach noticed' card from the computed facts given. "
    "Say what was noticed with the numbers in the facts and one concrete, gentle next step. No greeting, no headline, no "
    "numbers that aren't in the facts. Fitness guidance, not medical advice."
)


def phrase_notice(finding: dict, system: str = units.DEFAULT) -> str | None:
    """The AI wording for a proactive notice (fast model, non-essential); None falls back to the template."""
    if not llm_enabled():
        return None
    r = ai.call("notice", [COACH_RULES + "\n\n" + NOTICE_SYSTEM + units.instruction(system), memory_block()],
                f"DATA:\n{_data(dict(type=finding['type'], facts=finding['facts']), system)}\n\nWrite the note.", essential=False)
    return " ".join(r["text"].split())[:300]

