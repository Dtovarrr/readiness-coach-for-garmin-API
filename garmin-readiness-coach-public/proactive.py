"""Coach noticed: notable, actionable patterns found by deterministic rules (the AI only words them).

Sparing on purpose: at most MAX_PER_DAY notices a day (highest severity first), the same finding isn't
repeated for DEDUPE_DAYS, a type can be snoozed, nothing new appears during quiet hours, and nothing at
all in illness mode. Settings live in store.settings (notice_*); everything is local.
"""
from __future__ import annotations

import statistics as st
import uuid
from datetime import date, datetime, time as dtime, timedelta

import store

MAX_PER_DAY = 2
DEDUPE_DAYS = 7
SNOOZE_CHOICES = (3, 7, 30)
TYPES = {
    "hrv_decline": "HRV trending down", "lift_stall": "Lift stalled", "goal_behind": "Goal falling behind",
    "dexa_lean_drop": "DEXA lean mass drop", "load_spike": "Training load spike", "short_sleep": "Short sleep streak",
}


# ------------------------------------ rules ------------------------------------ #
def _vals(hist, key):
    return [(h["date"], h[key]) for h in hist if h.get(key) is not None]


def rule_hrv_decline(hist: list[dict]) -> dict | None:
    v = _vals(hist, "hrv")[-4:]
    if len(v) == 4 and all(b[1] < a[1] for a, b in zip(v, v[1:])):
        return dict(type="hrv_decline", severity=3, key=f"hrv:{v[-1][0]}",
                    facts=dict(nights=[dict(date=d, hrv_ms=x) for d, x in v], drop_ms=v[0][1] - v[-1][1]),
                    text=f"HRV has fallen three nights running, from {v[0][1]:.0f} to {v[-1][1]:.0f} ms. Consider keeping today easy.",
                    link=dict(kind="metric", metric="hrv", start=v[0][0], end=v[-1][0]))
    return None


def rule_short_sleep(hist: list[dict]) -> dict | None:
    v = _vals(hist, "sleep_h")[-3:]
    if len(v) == 3 and all(x < 6.5 for _, x in v):
        avg = st.mean(x for _, x in v)
        return dict(type="short_sleep", severity=2, key=f"sleep:{v[-1][0]}", facts=dict(nights=[dict(date=d, sleep_h=x) for d, x in v], avg_h=round(avg, 1)),
                    text=f"Three short nights in a row (averaging {avg:.1f} h). An earlier bedtime tonight would help recovery.",
                    link=dict(kind="metric", metric="sleep_h", start=v[0][0], end=v[-1][0]))
    return None


def rule_load_spike(assessment: dict) -> dict | None:
    r = (assessment.get("strain") or {}).get("ratio")
    if r and r > 1.5:
        return dict(type="load_spike", severity=3, key=f"load:{date.today().isocalendar()[1]}", facts=dict(acute_chronic_ratio=r),
                    text=f"This week's training load is {r:.1f}× your recent average, a jump that tends to raise injury risk. "
                         "A lighter day or two would let it settle.", link=dict(kind="metric", metric="load"))
    return None


def rule_lift_stall(lifts: list[dict]) -> list[dict]:
    """lifts: [{exercise_id, name, status, sessions_since_progress, kg}] from the strength program states."""
    out = []
    for l in lifts:
        if l.get("status") == "stalled" and (l.get("sessions_since_progress") or 3) >= 3:
            out.append(dict(type="lift_stall", severity=2, key=f"lift:{l['exercise_id']}:{l.get('kg')}",
                            facts=dict(lift=l["name"], weight_kg=l.get("kg"), sessions=l.get("sessions_since_progress") or 3),
                            text=f"{l['name']} has stalled for {l.get('sessions_since_progress') or 3} sessions. A small deload often restarts progress.",
                            link=dict(kind="lift", id=l["exercise_id"])))
    return out


def rule_goal_behind(goal_views: list[dict]) -> list[dict]:
    out = []
    for v in goal_views or []:
        status = (v.get("status") or "").lower()
        if status in ("behind", "at_risk", "off_track"):
            out.append(dict(type="goal_behind", severity=2, key=f"goal:{v['goal']['id']}:{date.today().isocalendar()[1]}",
                            facts=dict(goal=v["label"][:80], status=v.get("status_text"), required_per_week=v.get("required_per_week"),
                                       recent_trend_per_week=v.get("actual_per_week"), unit=v.get("unit")),
                            text=f"{v['label'][:80]} is {v.get('status_text', 'behind').lower()}. The Goals tab has the pace it now needs.",
                            link=dict(kind="goals")))
    return out


def rule_dexa_lean_drop(dexa_payload: dict | None) -> dict | None:
    ch = (dexa_payload or {}).get("change_vs_previous_scan") or {}
    if ch.get("lean_loss_with_fat_loss"):
        sc = (dexa_payload or {}).get("latest_scan") or {}
        return dict(type="dexa_lean_drop", severity=2, key=f"dexa:{sc.get('date')}",
                    facts=dict(lean_change_kg=ch.get("lean_mass_kg"), fat_change_kg=ch.get("fat_mass_kg"), scan_date=sc.get("date")),
                    text="Your latest DEXA scan shows lean mass falling along with fat. Steady strength work and enough protein help protect muscle.",
                    link=dict(kind="dexa"))
    return None


def findings(inputs: dict) -> list[dict]:
    """Everything the rules see now, highest severity first."""
    hist, out = inputs.get("history") or [], []
    for f in (rule_hrv_decline(hist), rule_short_sleep(hist), rule_load_spike(inputs.get("assessment") or {}),
              rule_dexa_lean_drop(inputs.get("dexa"))):
        if f:
            out.append(f)
    out += rule_lift_stall(inputs.get("lifts") or []) + rule_goal_behind(inputs.get("goals") or [])
    return sorted(out, key=lambda f: -f["severity"])


# ----------------------------------- settings ----------------------------------- #
def settings() -> dict:
    s = store.settings()
    return dict(enabled=s.get("notice_enabled", True), illness=bool(s.get("notice_illness")), notify=bool(s.get("notice_notify")),
                quiet_start=s.get("notice_quiet_start") or "22:00", quiet_end=s.get("notice_quiet_end") or "07:00",
                snoozed={k: v for k, v in (s.get("notice_snooze") or {}).items() if v >= date.today().isoformat()})


def in_quiet_hours(now: datetime, start: str, end: str) -> bool:
    a, b, t = dtime.fromisoformat(start), dtime.fromisoformat(end), now.time()
    return (a <= t or t < b) if a > b else (a <= t < b)


def snooze(kind: str, days: int, today: date | None = None) -> dict:
    if kind not in TYPES or days not in SNOOZE_CHOICES:
        raise ValueError("Unknown type or snooze length.")
    sn = dict(store.settings().get("notice_snooze") or {})
    sn[kind] = ((today or date.today()) + timedelta(days=days)).isoformat()
    store.settings_put(dict(notice_snooze=sn))
    return settings()


# ------------------------------------ engine ------------------------------------ #
def today_notices(inputs: dict, now: datetime | None = None, phrase=None) -> list[dict]:
    """Today's notices: the ones already chosen today, plus new ones while under the daily limit
    (never during quiet hours or illness mode). `phrase(finding) -> str | None` words a new one."""
    now = now or datetime.now()
    today, cfg = now.date(), settings()
    if not cfg["enabled"] or cfg["illness"]:
        return []
    kept = [n for n in store.notices(today.isoformat()) if n["day"] == today.isoformat()]
    shown = [n for n in kept if n["status"] == "shown" and n["type"] not in cfg["snoozed"]]
    order = lambda ns: sorted(ns, key=lambda n: (-n.get("severity", 0), n["created"], n.get("rank", 0)))
    if len(kept) >= MAX_PER_DAY or in_quiet_hours(now, cfg["quiet_start"], cfg["quiet_end"]):
        return order(shown)
    recent = {n["key"] for n in store.notices((today - timedelta(days=DEDUPE_DAYS)).isoformat())}
    recent_types_today = {n["type"] for n in kept}
    for f in findings(inputs):
        if len(kept) >= MAX_PER_DAY:
            break
        if f["key"] in recent or f["type"] in cfg["snoozed"] or f["type"] in recent_types_today:
            continue
        text = None
        if phrase:
            try:
                text = phrase(f)
            except Exception:
                text = None
        n = dict(id=uuid.uuid4().hex[:10], day=today.isoformat(), type=f["type"], key=f["key"], status="shown",
                 title=TYPES[f["type"]], text=text or f["text"], source="claude" if text else "rules", facts=f["facts"],
                 link=f.get("link"), severity=f["severity"], rank=len(kept), notified=False, created=now.strftime("%Y-%m-%dT%H:%M:%S"))
        store.notice_put(n)
        kept.append(n)
        shown.append(n)
        recent_types_today.add(f["type"])
    return order(shown)


def dismiss(nid: str) -> None:
    for n in store.notices():
        if n["id"] == nid:
            n["status"] = "dismissed"
            store.notice_put(n)
            return
    raise ValueError("Notice not found.")
