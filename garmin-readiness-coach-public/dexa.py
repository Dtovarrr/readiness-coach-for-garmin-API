"""DEXA scans: import planning (duplicates, history backfill), stored PDFs, the derived views the
pages show (changes, imbalances, percentile band, trends, projections), the training between scans,
the identifier-free coach payload and the rule-based analysis used without an Anthropic key.

Scans are keyed by measured date. A date stored from its own report is never overwritten by a
re-upload or by another report's history table; a date known only from history is upgraded when its
own report arrives. Everything is metric (kg); pages convert through Units.
"""
from __future__ import annotations

import hashlib
import math
import re
import shutil
import statistics as st
import time
import uuid
from datetime import date, timedelta
from pathlib import Path

import store
from body import avg_window, slope_per_week
from dexa_parse import BONE_REGIONS, COMP, LIMBS, REGIONS, SECTIONS, SUPP, validate
from records import family
from units import KG_LB

ID_RE = re.compile(r"^[0-9a-f]{32}$")
MAX_BYTES = 25 * 1024 * 1024
IMBALANCE_KG = 2 / KG_LB  # the report's own threshold: >2 lb left vs right
NOISE = dict(lean_kg=1 / KG_LB, fat_kg=1 / KG_LB, fat_pct=0.5)  # smaller changes are within day-to-day DEXA variation
PHOTO_WINDOW_DAYS = 5
STAGING_TTL = 24 * 3600
# BodySpec's age/sex body fat chart (as printed in the report), used when a report's copy can't be read.
PERCENTILE_TABLE = {
    "female": {"20-29": [24, 28, 32, 37], "30-39": [25, 29, 33, 38], "40-49": [26, 31, 35, 39], "50-59": [27, 32, 37, 42], ">60": [30, 33, 38, 42]},
    "male": {"20-29": [16, 20, 24, 27], "30-39": [18, 22, 26, 30], "40-49": [20, 24, 27, 31], "50-59": [21, 25, 29, 33], ">60": [21, 25, 30, 33]},
}
BANDS = ("0–20th", "20th–40th", "40th–60th", "60th–80th", "80th–99th")
DISCLAIMER = "Fitness guidance, not medical advice."
BONE_NOTE = ("The report itself notes this is not a bone density exam (that's a separate assessment with a medical "
             "professional); if anything here concerns you, talk to your doctor.")


# --------------------------------- files ------------------------------------- #
def dexa_dir() -> Path:
    return store.DATA_DIR / "dexa"


def staging_dir() -> Path:
    return dexa_dir() / "staging"


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def stage(data: bytes, parsed: dict | None) -> str:
    """Keeps an upload (and its parse) until it's confirmed or cancelled. Old leftovers are swept."""
    d = staging_dir()
    d.mkdir(parents=True, exist_ok=True)
    for f in d.iterdir():
        if time.time() - f.stat().st_mtime > STAGING_TTL:
            f.unlink(missing_ok=True)
    token = uuid.uuid4().hex
    (d / f"{token}.pdf").write_bytes(data)
    if parsed is not None:
        import json
        (d / f"{token}.json").write_text(json.dumps(parsed))
    return token


def staged(token: str) -> tuple[Path, dict | None] | None:
    if not ID_RE.match(token or ""):
        return None
    pdf = staging_dir() / f"{token}.pdf"
    if not pdf.is_file():
        return None
    import json
    try:
        parsed = json.loads((staging_dir() / f"{token}.json").read_text())
    except (OSError, ValueError):
        parsed = None
    return pdf, parsed


def discard(token: str) -> None:
    if ID_RE.match(token or ""):
        for ext in ("pdf", "json"):
            (staging_dir() / f"{token}.{ext}").unlink(missing_ok=True)


def pdf_path(scan: dict) -> Path | None:
    name = scan.get("pdf") or ""
    if not re.match(r"^[0-9a-f]{32}\.pdf$", name):
        return None
    p = dexa_dir() / name
    return p if p.is_file() else None


# ------------------------------ import planning ------------------------------ #
def plan_import(parsed: dict, existing: dict[str, dict]) -> dict:
    """What saving this report would do. `existing` maps measured date -> stored scan."""
    day = parsed["measured_date"]
    ex = existing.get(day)
    if ex and ex["source"] == "report":
        status, msg = "duplicate", f"A scan from {day} is already saved, so this file will be skipped."
    elif ex:
        status, msg = "upgrade", f"{day} is saved from an older report's history; this full report will replace it."
    else:
        status, msg = "new", None
    backfill = [dict(measured_date=h["measured_date"], fields=sorted(k for k in ("summary", "supplemental", "vat", "regional_trend") if h.get(k)))
                for h in parsed.get("history") or [] if h["measured_date"] not in existing and h["measured_date"] != day]
    return dict(status=status, message=msg, backfill=backfill)


def _num(v) -> float | None:
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) else None


def _clean_comp(r, keys=COMP) -> dict:
    return {k: v for k in keys if isinstance(r, dict) and (v := _num(r.get(k))) is not None}


def _strip(v):
    """A parsed section in the same whitelisted shape clean_scan produces (for the `edited` flag)."""
    if isinstance(v, dict):
        out = {k: _strip(x) for k, x in v.items()}
        return {k: x for k, x in out.items() if x is not None and x != {}}
    return _num(v)


def clean_scan(edit: dict, parsed: dict) -> dict:
    """The stored scan: numbers from the (user-reviewed) `edit`, whitelisted by structure; sex,
    age and the percentile table only from the server-side parse."""
    e = edit if isinstance(edit, dict) else {}
    out: dict = dict(sex=parsed.get("sex"), age=parsed.get("age"))
    if (s := _clean_comp(e.get("summary"))):
        out["summary"] = s
    regs = {k: r for k in REGIONS if (r := _clean_comp((e.get("regions") or {}).get(k)))}
    if regs:
        out["regions"] = regs
    if (s := _clean_comp(e.get("supplemental"), SUPP)):
        out["supplemental"] = s
    if (v := _clean_comp(e.get("vat"), ("mass_kg", "volume_in3"))):
        out["vat"] = v
    bone = {k: {f: v for f in ("bmd", "t", "z") if (v := _num(b.get(f))) is not None} for k in BONE_REGIONS
            if isinstance(b := (e.get("bone") or {}).get(k), dict) and _num(b.get("bmd")) is not None}
    if bone:
        out["bone"] = bone
    bal = {k: r for k in LIMBS.values() if (r := _clean_comp((e.get("balance") or {}).get(k)))}
    if bal:
        out["balance"] = bal
    trend = {k: r for k in REGIONS if (r := _clean_comp((parsed.get("regional_trend") or {}).get(k), ("fat_kg", "fat_pct", "lean_kg", "lean_pct")))}
    if trend:
        out["regional_trend"] = trend
    if parsed.get("percentile_table"):
        out["percentile_table"] = parsed["percentile_table"]
    present = {k: k in out for k in SECTIONS if k != "history"}
    out["missing"] = [k for k, ok in present.items() if not ok]
    out["warnings"] = validate(dict(out, measured_date=parsed["measured_date"]))
    out["edited"] = any(out.get(k) != _strip(parsed.get(k)) for k in ("summary", "regions", "supplemental", "vat", "bone", "balance"))
    return out


def history_scan(h: dict, from_id: str) -> dict:
    out = dict(sex=h.get("sex"), age=h.get("age"), from_scan=from_id)
    for k, keys in (("summary", COMP), ("supplemental", SUPP), ("vat", ("mass_kg", "volume_in3"))):
        if (r := _clean_comp(h.get(k), keys)):
            out[k] = r
    trend = {k: r for k in REGIONS if (r := _clean_comp((h.get("regional_trend") or {}).get(k), ("fat_kg", "fat_pct", "lean_kg", "lean_pct")))}
    if trend:
        out["regional_trend"] = trend
    out["missing"] = [k for k in ("summary", "regions", "supplemental", "vat", "bone", "balance") if k not in out]
    out["warnings"] = validate(dict(out, measured_date=h["measured_date"]))
    return out


def save(token: str, edit: dict, backfill_days: list[str]) -> dict:
    """Stores a staged report (and the chosen history scans). Re-checks duplicates at save time."""
    got = staged(token)
    if not got or not got[1]:
        raise ValueError("This upload expired or was already saved; please add the file again.")
    pdf, parsed = got
    existing = {s["measured_date"]: s for s in store.dexa_scans()}
    plan = plan_import(parsed, existing)
    if plan["status"] == "duplicate":
        discard(token)
        return dict(status="duplicate", message=plan["message"], saved=[], backfilled=[])
    old = existing.get(parsed["measured_date"])
    scan_id = old["id"] if old else uuid.uuid4().hex
    dexa_dir().mkdir(parents=True, exist_ok=True)
    name = f"{uuid.uuid4().hex}.pdf"
    shutil.move(str(pdf), dexa_dir() / name)
    if old and (p := pdf_path(old)):
        p.unlink(missing_ok=True)
    scan = dict(clean_scan(edit, parsed), id=scan_id, measured_date=parsed["measured_date"], source="report", pdf=name,
                file_sha256=sha256((dexa_dir() / name).read_bytes()), created=old["created"] if old else None)
    store.dexa_put(scan)
    allowed = {b["measured_date"] for b in plan["backfill"]}
    filled = []
    for h in parsed.get("history") or []:
        if h["measured_date"] in allowed and h["measured_date"] in set(backfill_days or []):
            store.dexa_put(dict(history_scan(h, scan_id), id=uuid.uuid4().hex, measured_date=h["measured_date"], source="history", pdf=None))
            filled.append(h["measured_date"])
    discard(token)
    return dict(status="upgraded" if old else "saved", id=scan_id, saved=[parsed["measured_date"]], backfilled=sorted(filled))


def update(scan: dict, edit: dict) -> dict:
    """Corrections from the detail view (same whitelist as the review screen)."""
    base = dict(scan, regional_trend=scan.get("regional_trend"), percentile_table=scan.get("percentile_table"))
    fresh = clean_scan(edit, base)
    if scan["source"] == "history":  # keep history scans to the fields history carries
        fresh = {k: v for k, v in fresh.items() if k not in ("bone", "balance", "regions")}
    keep = {k: scan[k] for k in ("id", "measured_date", "source", "pdf", "created", "file_sha256", "from_scan") if k in scan}
    new = dict(fresh, **keep, edited=True)
    store.dexa_put(new)
    return store.dexa_get(scan["id"])


def remove(scan: dict) -> None:
    if (p := pdf_path(scan)):
        p.unlink(missing_ok=True)
    store.dexa_delete(scan["id"])


# ---------------------------------- views ------------------------------------ #
def totals(scan: dict) -> dict:
    """fat_pct / total / fat / lean / bmc, from the summary or (history scans) the regional pages."""
    s = dict(scan.get("summary") or {})
    t = (scan.get("regional_trend") or {}).get("total") or {}
    for k in ("fat_pct", "fat_kg", "lean_kg"):
        if s.get(k) is None and t.get(k) is not None:
            s[k] = t[k]
    return s


def region(scan: dict, k: str) -> dict:
    r = dict((scan.get("regions") or {}).get(k) or {})
    t = (scan.get("regional_trend") or {}).get(k) or {}
    for f in ("fat_kg", "lean_kg", "fat_pct"):
        if r.get(f) is None and t.get(f) is not None:
            r[f] = t[f]
    return r


def _d(a, b):
    return None if a is None or b is None else b - a


def _rel(a, b):
    return None if a in (None, 0) or b is None else (b - a) / a * 100


def change(a: dict, b: dict) -> dict:
    """b minus a, per total and per region (kg / points), with relative %."""
    ta, tb = totals(a), totals(b)
    out = dict(days=(date.fromisoformat(b["measured_date"]) - date.fromisoformat(a["measured_date"])).days,
               total={k: dict(a=ta.get(k), b=tb.get(k), d=_d(ta.get(k), tb.get(k)), pct=None if k == "fat_pct" else _rel(ta.get(k), tb.get(k)))
                      for k in COMP},
               regions={})
    for k in REGIONS:
        ra, rb = region(a, k), region(b, k)
        out["regions"][k] = {f: dict(a=ra.get(f), b=rb.get(f), d=_d(ra.get(f), rb.get(f)), pct=None if f == "fat_pct" else _rel(ra.get(f), rb.get(f)))
                             for f in ("fat_pct", "total_kg", "fat_kg", "lean_kg", "bmc_kg")}
    sa, sb = a.get("supplemental") or {}, b.get("supplemental") or {}
    out["supplemental"] = {k: dict(a=sa.get(k), b=sb.get(k), d=_d(sa.get(k), sb.get(k))) for k in SUPP}
    va, vb = a.get("vat") or {}, b.get("vat") or {}
    out["vat"] = {k: dict(a=va.get(k), b=vb.get(k), d=_d(va.get(k), vb.get(k)), pct=_rel(va.get(k), vb.get(k))) for k in ("mass_kg", "volume_in3")}
    out["flags"] = flags(out)
    return out


def flags(ch: dict) -> dict:
    t = ch["total"]
    fat, lean, bf = t["fat_kg"]["d"], t["lean_kg"]["d"], t["fat_pct"]["d"]
    meaningful = lambda v, k: v is not None and abs(v) >= NOISE[k]
    return dict(lean_loss_with_fat_loss=bool(fat is not None and lean is not None and fat < -NOISE["fat_kg"] and lean <= -NOISE["lean_kg"]),
                fat_change_meaningful=meaningful(fat, "fat_kg"), lean_change_meaningful=meaningful(lean, "lean_kg"),
                bf_change_meaningful=meaningful(bf, "fat_pct"))


def imbalances(scan: dict) -> list[dict]:
    """Right vs left arm and leg; `flag` when lean or total mass differs by more than 2 lb."""
    bal, out = scan.get("balance") or {}, []
    for limb in ("arm", "leg"):
        r, l = bal.get(f"right_{limb}"), bal.get(f"left_{limb}")
        if not r or not l:
            continue
        d = {k: _d(l.get(k), r.get(k)) for k in ("total_kg", "fat_kg", "lean_kg", "bmc_kg", "fat_pct")}
        flagged = [k for k in ("lean_kg", "fat_kg", "total_kg") if d[k] is not None and abs(d[k]) > IMBALANCE_KG]
        out.append(dict(limb=limb, right=r, left=l, diff=d, flagged=flagged, flag=bool(flagged)))
    return out


def _age_band(age: int) -> str:
    return ">60" if age >= 60 else f"{max(20, age // 10 * 10)}-{max(20, age // 10 * 10) + 9}"


def percentile_band(scan: dict) -> dict | None:
    bf, sex, age = totals(scan).get("fat_pct"), scan.get("sex"), scan.get("age")
    if bf is None or sex not in ("male", "female") or age is None:
        return None
    own = (scan.get("percentile_table") or {}).get(sex) or {}
    band = _age_band(age)
    cuts = own.get(band) or PERCENTILE_TABLE[sex].get(band)
    if not cuts:
        return None
    i = next((n for n, c in enumerate(cuts) if bf < c), 4)
    lo, hi = (cuts[i - 1] if i else None), (cuts[i] if i < 4 else None)
    return dict(band=BANDS[i], index=i, sex=sex, age_band=band, cuts=cuts, low=lo, high=hi, fat_pct=bf,
                source="report" if own.get(band) else "BodySpec chart")


def _ord(s: dict) -> int:
    return date.fromisoformat(s["measured_date"]).toordinal()


def _slope_month(pts: list[tuple[int, float]]) -> float | None:
    if len(pts) < 2 or pts[-1][0] == pts[0][0]:
        return None
    mx, my = st.mean(p[0] for p in pts), st.mean(p[1] for p in pts)
    den = sum((x - mx) ** 2 for x, _ in pts)
    return sum((x - mx) * (y - my) for x, y in pts) / den * 30.44 if den else None


def trend(scans: list[dict]) -> dict:
    """Rates per month across every scan (least squares) and between each pair of scans."""
    s = sorted((x for x in scans if totals(x).get("fat_kg") is not None), key=_ord)
    out = dict(scans=len(s), pairs=[], overall={}, first=None, last=None)
    if not s:
        return out
    out["first"], out["last"] = s[0]["measured_date"], s[-1]["measured_date"]
    for k in ("fat_kg", "lean_kg", "fat_pct", "total_kg"):
        out["overall"][k] = _slope_month([(_ord(x), totals(x)[k]) for x in s if totals(x).get(k) is not None])
    for a, b in zip(s, s[1:]):
        days = _ord(b) - _ord(a)
        ch = change(a, b)
        out["pairs"].append(dict(a=a["measured_date"], b=b["measured_date"], days=days,
                                 per_month={k: (ch["total"][k]["d"] / days * 30.44 if ch["total"][k]["d"] is not None and days else None)
                                            for k in ("fat_kg", "lean_kg", "fat_pct", "total_kg")},
                                 flags=ch["flags"]))
    return out


def projection(scans: list[dict], goal_list: list[dict], today: date | None = None) -> list[dict]:
    """Body fat % / lean mass goals projected from the scan trend, with a range from the slowest
    and fastest stretch between scans. Linear and rough by nature."""
    today = today or date.today()
    tr = trend(scans)
    if tr["scans"] < 2:
        return []
    last = sorted(scans, key=_ord)[-1]
    out = []
    for g in goal_list:
        key = {"body_fat": "fat_pct", "lean_mass": "lean_kg"}.get(g.get("type"))
        if not key or g.get("target") is None:
            continue
        cur, target, rate = totals(last).get(key), g["target"], tr["overall"].get(key)
        rates = [p["per_month"][key] for p in tr["pairs"] if p["per_month"].get(key) is not None]
        if cur is None:
            continue
        need = target - cur

        def months(r):
            return need / r if r and need and (need > 0) == (r > 0) else None

        m, ms = months(rate), [x for x in (months(r) for r in rates) if x is not None]
        dl = g.get("deadline")
        out.append(dict(type=g["type"], goal_id=g.get("id"), current=cur, target=target, as_of=last["measured_date"], per_month=rate,
                        months=m, months_low=min(ms) if len(ms) > 1 else None, months_high=max(ms) if len(ms) > 1 else None,
                        mixed=len(rates) > 1 and len({r > 0 for r in rates}) > 1,
                        eta=(date.fromisoformat(last["measured_date"]) + timedelta(days=round(m * 30.44))).isoformat() if m else None,
                        deadline=dl))
    return out


def scale_diff(scan: dict, series: list[dict]) -> dict | None:
    """Garmin scale readings around the scan date (7-day window, widening to ±10 days) vs DEXA."""
    d = date.fromisoformat(scan["measured_date"])
    near = [r for r in series if abs((date.fromisoformat(r["date"]) - d).days) <= 10]
    if not near:
        return None
    fat = avg_window([(r["date"], r.get("fat")) for r in near], d + timedelta(days=3))
    kg = avg_window([(r["date"], r.get("kg")) for r in near], d + timedelta(days=3))
    t = totals(scan)
    return dict(date=scan["measured_date"], scale_fat_pct=fat, dexa_fat_pct=t.get("fat_pct"),
                fat_diff_pts=_d(t.get("fat_pct"), fat), scale_kg=kg, dexa_kg=t.get("total_kg"), weight_diff_kg=_d(t.get("total_kg"), kg),
                readings=len(near))


# ------------------------------ training context ----------------------------- #
def training_between(a: str, b: str, hist: dict | None) -> dict | None:
    """What the cached Garmin history shows between two scan dates (a, b]. Never triggers a sync."""
    if not hist:
        return None
    da, db = date.fromisoformat(a), date.fromisoformat(b)
    weeks = max((db - da).days / 7, 1 / 7)
    acts = [x for x in hist.get("activities") or [] if x.get("date") and a < x["date"] <= b]
    days = {k: v for k, v in (hist.get("days") or {}).items() if a < k <= b}
    if not acts and not days:
        return None
    fam: dict[str, int] = {}
    for x in acts:
        f = family(x.get("type"))
        fam[f] = fam.get(f, 0) + 1
    run_m = sum(x.get("dist_m") or 0 for x in acts if family(x.get("type")) == "run")
    load = [x["load"] for x in acts if x.get("load") is not None]
    sleep = [v["sleep_score"] for v in days.values() if v.get("sleep_score") is not None]
    im = [v["im"] for v in days.values() if v.get("im") is not None]
    w = [(k, v["weight_kg"]) for k, v in sorted(days.items()) if v.get("weight_kg") is not None]
    covered = (min((x["date"] for x in acts), default=b), max((x["date"] for x in acts), default=a))
    return dict(start=a, end=b, weeks=round(weeks, 1), activities=len(acts),
                sessions_per_week={f: round(n / weeks, 1) for f, n in sorted(fam.items(), key=lambda kv: -kv[1])},
                strength_per_week=round(fam.get("strength", 0) / weeks, 1),
                run_km_per_week=round(run_m / 1000 / weeks, 1), load_per_week=round(sum(load) / weeks) if load else None,
                hours_per_week=round(sum((x.get("secs") or 0) for x in acts) / 3600 / weeks, 1),
                intensity_min_per_week=round(sum(im) / len(im) * 7) if im else None,
                sleep_score_avg=round(st.mean(sleep)) if sleep else None,
                weight_start_kg=w[0][1] if w else None, weight_end_kg=w[-1][1] if w else None,
                weight_trend_kg_per_week=round(s, 2) if (s := slope_per_week(w)) is not None else None,
                coverage_note=None if acts and covered[0] <= (da + timedelta(days=14)).isoformat() else "Garmin history covers only part of this stretch.")


# -------------------------------- coach payload ------------------------------ #
def _r(x, n=2):
    return None if x is None else round(x, n)


def _snap(scan: dict) -> dict:
    t, s, v = totals(scan), scan.get("supplemental") or {}, scan.get("vat") or {}
    tb = (scan.get("bone") or {}).get("total") or {}
    pb = percentile_band(scan)
    return {k: x for k, x in dict(
        date=scan["measured_date"], from_history_table=scan["source"] == "history",
        body_fat_pct=_r(t.get("fat_pct"), 1), total_mass_kg=_r(t.get("total_kg")), fat_mass_kg=_r(t.get("fat_kg")),
        lean_mass_kg=_r(t.get("lean_kg")), bone_mineral_content_kg=_r(t.get("bmc_kg")), rmr_kcal=s.get("rmr_kcal"),
        android_fat_pct=s.get("android_pct"), gynoid_fat_pct=s.get("gynoid_pct"), ag_ratio=s.get("ag_ratio"),
        vat_mass_kg=_r(v.get("mass_kg")), vat_volume_in3=v.get("volume_in3"),
        bone_total_bmd_g_cm2=tb.get("bmd"), bone_t_score=tb.get("t"), bone_z_score=tb.get("z"),
        body_fat_percentile_band=f"{pb['band']} percentile for {pb['sex']}s aged {pb['age_band']} (lower = leaner)" if pb else None,
        regions={k: {f: _r(region(scan, k).get(f), 1 if f == "fat_pct" else 2) for f in ("fat_kg", "lean_kg", "fat_pct")}
                 for k in REGIONS if region(scan, k)} or None,
        limb_balance=[dict(limb=i["limb"], right_minus_left_lean_kg=_r(i["diff"]["lean_kg"]), right_minus_left_fat_kg=_r(i["diff"]["fat_kg"]),
                           over_2lb_threshold=i["flag"]) for i in imbalances(scan)] or None,
    ).items() if x is not None}


def _change_payload(ch: dict) -> dict:
    t = ch["total"]
    return dict(days=ch["days"], fat_mass_kg=_r(t["fat_kg"]["d"]), lean_mass_kg=_r(t["lean_kg"]["d"]), total_mass_kg=_r(t["total_kg"]["d"]),
                body_fat_pts=_r(t["fat_pct"]["d"], 1), vat_mass_kg=_r(ch["vat"]["mass_kg"]["d"]), ag_ratio=_r(ch["supplemental"]["ag_ratio"]["d"]),
                regions={k: dict(fat_kg=_r(r["fat_kg"]["d"]), lean_kg=_r(r["lean_kg"]["d"])) for k, r in ch["regions"].items()
                         if r["fat_kg"]["d"] is not None or r["lean_kg"]["d"] is not None},
                **ch["flags"])


def coach_payload(scans: list[dict], focus: dict | None = None, hist: dict | None = None, series: list[dict] | None = None,
                  goal_list: list[dict] | None = None) -> dict | None:
    """Derived numbers only - no name, birth date, file names or report text. `focus` defaults to the latest scan."""
    s = sorted(scans, key=_ord)
    if not s:
        return None
    cur = focus or s[-1]
    i = next(n for n, x in enumerate(s) if x["id"] == cur["id"])
    prev, base = (s[i - 1] if i else None), (s[0] if i else None)
    tr = trend(s[:i + 1])
    out = dict(scan=_snap(cur), scans_on_file=len(s), scan_dates=[x["measured_date"] for x in s],
               noise_note="DEXA readings shift with hydration, food and timing: changes under ~1 lb (0.45 kg) of lean or fat, or ~0.5 body fat points, aren't meaningful.")
    if prev:
        out["change_vs_previous"] = _change_payload(change(prev, cur))
        out["training_between_previous_and_this_scan"] = training_between(prev["measured_date"], cur["measured_date"], hist)
    if base and base is not prev:
        out["change_vs_baseline"] = _change_payload(change(base, cur))
    if tr["scans"] >= 2:
        out["rates_per_month"] = dict(overall={f"{k}_per_month": _r(v) for k, v in tr["overall"].items()},
                                      between_scans=[dict(start=p["a"], end=p["b"], **{f"{k}_per_month": _r(v) for k, v in p["per_month"].items()},
                                                          lean_loss_with_fat_loss=p["flags"]["lean_loss_with_fat_loss"]) for p in tr["pairs"]])
        out["training_by_stretch"] = [t for p in tr["pairs"] if (t := training_between(p["a"], p["b"], hist))]
    if series:
        out["scale_vs_dexa"] = [dict(date=d["date"], scale_body_fat_pct=_r(d["scale_fat_pct"], 1), dexa_body_fat_pct=d["dexa_fat_pct"],
                                     scale_weight_kg=_r(d["scale_kg"]), dexa_total_mass_kg=_r(d["dexa_kg"]))
                                for x in s[:i + 1] if (d := scale_diff(x, series))]
        recent = [(r["date"], r["kg"]) for r in series if r["date"] > cur["measured_date"]]
        if recent:
            out["scale_since_scan"] = dict(readings=len(recent), latest_weight_kg=recent[-1][1], days=(date.today() - date.fromisoformat(cur["measured_date"])).days)
    if goal_list:
        out["goal_projections"] = [{k: (_r(v) if isinstance(v, float) else v) for k, v in p.items() if k != "goal_id"}
                                   for p in projection(s[:i + 1], goal_list)]
    return out


def brief_payload(scans: list[dict]) -> dict | None:
    """The short version for the daily briefing and Ask the coach."""
    s = sorted(scans, key=_ord)
    if not s:
        return None
    out = dict(latest_scan=_snap(s[-1]), scans_on_file=len(s),
               note="DEXA is the body composition source of truth here; Garmin scale body fat is a rougher estimate.")
    full = [x for x in s if totals(x).get("fat_kg") is not None]
    if len(full) >= 2:
        out["change_vs_previous_scan"] = _change_payload(change(full[-2], full[-1]))
        out["rates_per_month_all_scans"] = {f"{k}_per_month": _r(v) for k, v in trend(full)["overall"].items()}
    out["days_since_scan"] = (date.today() - date.fromisoformat(s[-1]["measured_date"])).days
    return out


# ------------------------------- rule-based text ----------------------------- #
def _lb(kg, d=1, sign=False):
    if kg is None:
        return "–"
    v = kg * KG_LB
    return f"{'+' if sign and v > 0 else '−' if sign and v < 0 else ''}{abs(v) if sign else v:.{d}f} lb"


def _kgs(kg, d=1, sign=False):
    if kg is None:
        return "–"
    return f"{'+' if sign and kg > 0 else '−' if sign and kg < 0 else ''}{abs(kg) if sign else kg:.{d}f} kg"


def _pts(v, sign=True):
    return "–" if v is None else f"{'+' if sign and v > 0 else '−' if sign and v < 0 else ''}{abs(v):.1f}"


def _day(iso: str) -> str:
    d = date.fromisoformat(iso)
    return f"{d:%b} {d.day}, {d.year}"


def rule_analysis(p: dict, system: str = "us") -> str:
    """Plain, hedged write-up from coach_payload() for one scan."""
    m = _lb if system == "us" else _kgs
    sc, out = p["scan"], []
    out.append(f"**Scan {_day(sc['date'])}**: {sc.get('body_fat_pct', '–')}% body fat · lean {m(sc.get('lean_mass_kg'))} · fat {m(sc.get('fat_mass_kg'))}.")
    for label, key in (("since your previous scan", "change_vs_previous"), ("since your first scan", "change_vs_baseline")):
        c = p.get(key)
        if not c:
            continue
        line = f"- **{label.capitalize()}** ({c['days']} days): fat {m(c['fat_mass_kg'], sign=True)}, lean {m(c['lean_mass_kg'], sign=True)}, body fat {_pts(c['body_fat_pts'])} pts."
        regs = c.get("regions") or {}
        big = sorted(((k, r["fat_kg"]) for k, r in regs.items() if k != "total" and r.get("fat_kg") is not None), key=lambda kv: kv[1])
        if big and big[0][1] < 0:
            line += f" Most fat came off the {big[0][0]} ({m(big[0][1], sign=True)})."
        out.append(line)
    c = p.get("change_vs_previous")
    if c:
        if c.get("lean_loss_with_fat_loss"):
            out.append("- **Lean mass dropped along with fat.** Some of that may be water and glycogen, but it can also mean the "
                       "deficit is a bit steep or strength work is light. A smaller deficit, 2–3 full-body strength sessions a week "
                       "with progressive overload, and protein around 0.7–1 g per lb of goal body weight tend to protect muscle.")
        elif c["fat_mass_kg"] is not None and c["fat_mass_kg"] < 0 and (c["lean_mass_kg"] or 0) >= 0:
            out.append("- Fat came down while lean mass held or rose, which is what a well-set deficit with strength training tends to look like.")
        if not c.get("fat_change_meaningful") and not c.get("lean_change_meaningful"):
            out.append("- These changes are small enough to sit inside normal DEXA variation, so treat them as 'about the same'.")
    if sc.get("vat_mass_kg") is not None:
        vc = (c or {}).get("vat_mass_kg")
        out.append(f"- **Visceral fat**: {m(sc['vat_mass_kg'], 2)}{f' ({m(vc, 2, sign=True)} since the previous scan)' if vc is not None else ''}. "
                   "Lower tends to be better; regular cardio, strength work, sleep and a modest deficit usually move it down.")
    if sc.get("ag_ratio") is not None:
        out.append(f"- **Android/gynoid**: A/G {sc['ag_ratio']:.2f}{' (the report suggests under 1.0)' if sc['ag_ratio'] >= 1 else ''}. "
                   "Midsection fat is often the last to go, and tends to follow overall fat loss.")
    flagged = [x for x in sc.get("limb_balance") or [] if x["over_2lb_threshold"]]
    if flagged:
        out.append("- **Muscle balance**: " + "; ".join(f"{x['limb']}s differ by {m(abs(x['right_minus_left_lean_kg'] or 0))} of lean" for x in flagged)
                   + " — over the report's 2 lb threshold. Some unilateral work (single-arm/single-leg) may help; a sudden change is worth checking with a professional.")
    elif sc.get("limb_balance"):
        out.append("- **Muscle balance**: left and right are within the report's 2 lb threshold.")
    if sc.get("bone_z_score") is not None:
        out.append(f"- **Bone**: Z-score {sc['bone_z_score']:+.1f}, T-score {sc.get('bone_t_score', 0):+.1f}. Strength and impact training tend to support bone. {BONE_NOTE}")
    t = p.get("training_between_previous_and_this_scan")
    if t:
        dist = t["run_km_per_week"] * (0.621371 if system == "us" else 1)
        sleep = f", sleep score avg {t['sleep_score_avg']}" if t.get("sleep_score_avg") else ""
        out.append(f"- **Training in between** ({t['weeks']} weeks): {t['strength_per_week']} strength sessions/week, "
                   f"{dist:.1f} {'mi' if system == 'us' else 'km'} running/week{sleep}.")
    out += ["", "**Next steps**",
            "- Keep 2–3 strength sessions a week, adding a little load or reps over time.",
            "- Protein around 0.7–1 g per lb of goal body weight, spread over the day.",
            "- Cardio for health and visceral fat: mostly easy, plus some harder work if recovery allows.",
            "- Sleep 7–9 hours; recovery tends to show up in lean mass.",
            "- Rescan in 8–12 weeks under the same conditions (time of day, hydration, food) so changes are comparable.",
            "", f"_DEXA readings can shift a little with hydration, food and timing, so small changes aren't meaningful. {DISCLAIMER}_"]
    return "\n".join(out)


def rule_trend(p: dict, system: str = "us") -> str:
    m = _lb if system == "us" else _kgs
    r = p.get("rates_per_month")
    if not r:
        return "Add a second scan to see trends. " + f"_{DISCLAIMER}_"
    o = r["overall"]
    out = [f"Across {p['scans_on_file']} scans ({_day(p['scan_dates'][0])} → {_day(p['scan_dates'][-1])}): fat {m(o.get('fat_kg_per_month'), 1, True)}/month, "
           f"lean {m(o.get('lean_kg_per_month'), 1, True)}/month, body fat {_pts(o.get('fat_pct_per_month'))} pts/month."]
    for b in r["between_scans"]:
        tr = next((t for t in p.get("training_by_stretch") or [] if t["start"] == b["start"]), None)
        line = f"- {_day(b['start'])} → {_day(b['end'])}: fat {m(b.get('fat_kg_per_month'), 1, True)}/mo, lean {m(b.get('lean_kg_per_month'), 1, True)}/mo"
        if tr:
            line += f"; {tr['strength_per_week']} strength/wk, {tr['load_per_week'] or '–'} load/wk"
        if b["lean_loss_with_fat_loss"]:
            line += " — lean dropped too"
        out.append(line + ".")
    for g in p.get("goal_projections") or []:
        unit = "%" if g["type"] == "body_fat" else ""
        cur = f"{g['current']:.1f}%" if unit else m(g["current"])
        tgt = f"{g['target']:.1f}%" if unit else m(g["target"])
        if g.get("months"):
            rng = f" (somewhere between {g['months_low']:.0f} and {g['months_high']:.0f} months on the slowest/fastest stretch)" if g.get("months_low") else ""
            out.append(f"- Goal {tgt}: from {cur}, the scan trend points to about {g['months']:.0f} months{rng}. "
                       "That's a straight-line guess from a few scans; real progress tends to slow as you get closer.")
        else:
            out.append(f"- Goal {tgt}: the scan trend isn't moving toward it yet, so no honest date can be projected.")
    out.append(f"\n_Rates come from a handful of scans, each with some measurement noise. {DISCLAIMER}_")
    return "\n".join(out)


# ----------------------------------- goals ----------------------------------- #
def goal_metrics(scans: list[dict]) -> dict:
    """Body fat % and lean mass goal metrics from the scans: the latest scan is the current value,
    the trend across scans the weekly rate. Empty without scans (body fat then falls back to the scale)."""
    s = sorted((x for x in scans if totals(x).get("fat_pct") is not None or totals(x).get("lean_kg") is not None), key=_ord)
    if not s:
        return {}
    last, tr, out = s[-1], trend(s), {}
    when = date.fromisoformat(last["measured_date"]).strftime("%b %-d, %Y")
    for t, k in (("body_fat", "fat_pct"), ("lean_mass", "lean_kg")):
        v = totals(last).get(k)
        if v is None:
            continue
        rate = tr["overall"].get(k)
        out[t] = dict(current=v, rate=rate / 30.44 * 7 if rate is not None else None, as_of=last["measured_date"], n=len(s),
                      source=f"DEXA scan {when}",
                      basis=f"latest DEXA scan{f', trend across {len(s)} scans' if len(s) > 1 else ''}")
    return out
