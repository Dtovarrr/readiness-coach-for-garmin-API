"""Activity drill-down.

Turns the Personal API activity detail (/activity-service/activity/{id}) plus the original
activity file (/download-service/files/activity/{id}: a zip holding a FIT, or GPX/TCX) into
the view model the dashboard's detail panel renders, and derives the stats the coach
analysis is given.

Privacy: GPS coordinates only ever go to the local page (for the map). `coach_stats()` is
built from an explicit whitelist - no coordinates, activity names or location fields.
"""
from __future__ import annotations

import io
import math
import statistics as st
import xml.etree.ElementTree as ET
import zipfile
from datetime import datetime, timedelta

import fitdecode

VERSION = 3                 # bump to invalidate cached views when parsing changes
SEMI = 180 / 2**31          # FIT semicircles -> degrees
MILE = 1609.344
PAUSE_GAP = 30              # seconds between records treated as an auto-pause
DISTANCE_SPORTS = ("run", "walk", "hik", "cycl", "bik", "ride")


def is_distance_sport(type_key: str | None) -> bool:
    return any(s in (type_key or "") for s in DISTANCE_SPORTS)


def uses_speed(type_key: str | None) -> bool:
    return any(s in (type_key or "") for s in ("cycl", "bik", "ride"))


def _pretty(key) -> str | None:
    return str(key).replace("_", " ").capitalize() if key else None


def _r(x, n=1):
    return None if x is None else round(x, n)


# --------------------------------------------------------------------------- #
# File parsing
# --------------------------------------------------------------------------- #
def read_file(blob: bytes) -> dict:
    """Unzip if needed and parse FIT, GPX or TCX into records (+ sets/zones for FIT)."""
    if blob[:2] == b"PK":
        with zipfile.ZipFile(io.BytesIO(blob)) as z:
            name = max(z.namelist(), key=lambda n: z.getinfo(n).file_size)
            blob = z.read(name)
    if blob[8:12] == b".FIT":
        return _read_fit(blob)
    if blob.lstrip()[:1] == b"<":
        return _read_xml(blob)
    return {}


def _read_fit(data: bytes) -> dict:
    out: dict = dict(records=[], sets=[], zones=None, laps=[])
    steps, titles = {}, {}
    with fitdecode.FitReader(io.BytesIO(data)) as fr:
        for f in fr:
            if not isinstance(f, fitdecode.FitDataMessage):
                continue
            v = {fd.name: fd.value for fd in f.fields if fd.value is not None}
            if f.name == "record" and "timestamp" in v:
                lat, lon = v.get("position_lat"), v.get("position_long")
                cad = v.get("cadence")
                if cad is not None:
                    cad += v.get("fractional_cadence") or 0
                out["records"].append(dict(
                    ts=v["timestamp"], dist=v.get("distance"),
                    speed=v.get("enhanced_speed", v.get("speed")), hr=v.get("heart_rate"), cad=cad,
                    alt=v.get("enhanced_altitude", v.get("altitude")),
                    lat=lat * SEMI if isinstance(lat, int) else None,
                    lon=lon * SEMI if isinstance(lon, int) else None,
                ))
            elif f.name == "set":
                out["sets"].append(v)
            elif f.name == "lap" and "start_time" in v:
                out["laps"].append(dict(start=v["start_time"], secs=v.get("total_timer_time"), dist=v.get("total_distance"),
                                        hr=v.get("avg_heart_rate"), speed=v.get("enhanced_avg_speed", v.get("avg_speed")),
                                        step=v.get("wkt_step_index")))
            elif f.name == "workout_step":
                steps[v.get("message_index")] = v
            elif f.name == "exercise_title":
                titles[(v.get("exercise_category"), v.get("exercise_name"))] = v.get("wkt_step_name")
            elif f.name == "time_in_zone" and v.get("reference_mesg") == "session":
                out["zones"] = (v.get("time_in_hr_zone"), v.get("hr_zone_high_boundary"))
    # Sets only carry a workout-step index; the step holds the exercise + planned reps.
    for s in out["sets"]:
        stp = steps.get(s.get("wkt_step_index")) or {}
        key = (stp.get("exercise_category"), stp.get("exercise_name"))
        s["exercise"] = titles.get(key) or _pretty(stp.get("exercise_category"))
        s["target_reps"] = stp.get("duration_reps")
    return out


def _read_xml(data: bytes) -> dict:
    """GPX (<trkpt>) or TCX (<Trackpoint>) fallback; distance/speed are derived later."""
    tag = lambda el: el.tag.rsplit("}", 1)[-1]
    recs = []
    for el in ET.fromstring(data).iter():
        t = tag(el)
        if t not in ("trkpt", "Trackpoint"):
            continue
        rec = dict(lat=float(el.get("lat")), lon=float(el.get("lon"))) if t == "trkpt" else {}
        for ch in el.iter():
            c, txt = tag(ch), (ch.text or "").strip()
            if not txt:
                continue
            if c in ("time", "Time"):
                rec["ts"] = datetime.fromisoformat(txt.replace("Z", "+00:00"))
            elif c in ("ele", "AltitudeMeters"):
                rec["alt"] = float(txt)
            elif c == "LatitudeDegrees":
                rec["lat"] = float(txt)
            elif c == "LongitudeDegrees":
                rec["lon"] = float(txt)
            elif c == "DistanceMeters":
                rec["dist"] = float(txt)
            elif c in ("hr", "Value"):
                rec["hr"] = int(float(txt))
            elif c in ("cad", "Cadence", "RunCadence"):
                rec["cad"] = float(txt)
            elif c == "Speed":
                rec["speed"] = float(txt)
        if "ts" in rec:
            recs.append(rec)
    return dict(records=recs, sets=[], zones=None)


def _haversine(a, b):
    la1, lo1, la2, lo2 = map(math.radians, (a["lat"], a["lon"], b["lat"], b["lon"]))
    h = math.sin((la2 - la1) / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2
    return 2 * 6371000 * math.asin(math.sqrt(h))


def _prep(recs: list[dict], cad_factor: float) -> list[dict]:
    """Sort, add moving time `t` / step `dt` (pauses dropped), fill distance & speed gaps."""
    recs = sorted(recs, key=lambda r: r["ts"])
    t, dist, prev = 0.0, 0.0, None
    for r in recs:
        dt = (r["ts"] - prev["ts"]).total_seconds() if prev else 0.0
        r["dt"] = dt if 0 < dt <= PAUSE_GAP else 0.0
        t += r["dt"]
        r["t"] = t
        if r.get("dist") is None:
            if prev and r.get("lat") is not None and prev.get("lat") is not None:
                dist += _haversine(prev, r)
            r["dist"] = dist
        dist = r["dist"]
        if r.get("speed") is None and prev and r["dt"]:
            r["speed"] = max(0.0, (r["dist"] - prev["dist"]) / r["dt"])
        if r.get("cad") is not None:
            r["cad"] *= cad_factor
        prev = r
    return recs


# --------------------------------------------------------------------------- #
# Derived views
# --------------------------------------------------------------------------- #
def _wmean(recs, key):
    num = sum(r[key] * r["dt"] for r in recs if r.get(key) is not None and r["dt"])
    den = sum(r["dt"] for r in recs if r.get(key) is not None and r["dt"])
    return num / den if den else None


def _gain(recs, hysteresis=3.0):
    """Climb with a dead band, so altimeter noise doesn't add up (Garmin-like totals)."""
    alts = [r["alt"] for r in recs if r.get("alt") is not None]
    if len(alts) < 2:
        return None
    gain, ref = 0.0, alts[0]
    for a in alts[1:]:
        if a - ref >= hysteresis:
            gain, ref = gain + a - ref, a
        elif a < ref:
            ref = a
    return round(gain)


def _splits(recs, unit):
    pts = [r for r in recs if r.get("dist") is not None]
    out, start, begin_d, nxt = [], 0, 0.0, unit

    def close(end, d):
        seg = pts[start + 1:end + 1]
        secs = sum(r["dt"] for r in seg)
        if secs > 0 and d > 0:
            out.append(dict(n=len(out) + 1, dist=round(d, 1), secs=round(secs, 1), speed=round(d / secs, 3),
                            hr=_r(_wmean(seg, "hr"), 0), cad=_r(_wmean(seg, "cad"), 0),
                            gain=_gain(pts[start:end + 1])))

    for i, r in enumerate(pts):
        if r["dist"] >= nxt:
            close(i, r["dist"] - begin_d)
            start, begin_d = i, r["dist"]
            nxt = (math.floor(r["dist"] / unit) + 1) * unit
    if pts and pts[-1]["dist"] - begin_d >= 0.05 * unit:
        close(len(pts) - 1, pts[-1]["dist"] - begin_d)
    return out


def _buckets(recs, n):
    size = max(1, math.ceil(len(recs) / n))
    return [recs[i:i + size] for i in range(0, len(recs), size)]


def _series(recs, n=400):
    """Downsampled timeline for charts: distance, moving time, speed, HR, altitude."""
    out = []
    for b in _buckets(recs, n):
        sp = [r["speed"] for r in b if r.get("speed") is not None]
        hr = [r["hr"] for r in b if r.get("hr")]
        alt = [r["alt"] for r in b if r.get("alt") is not None]
        out.append(dict(d=_r(b[-1].get("dist")), t=round(b[-1]["t"]), speed=_r(st.mean(sp), 3) if sp else None,
                        hr=round(st.mean(hr)) if hr else None, alt=_r(st.mean(alt)) if alt else None))
    return out


def _route(recs, n=1500):
    """[[lat, lon, speed, hr], ...] for the local map only."""
    gps = [r for r in recs if r.get("lat") is not None]
    out = []
    for b in _buckets(gps, n):
        sp = [r["speed"] for r in b if r.get("speed") is not None]
        hr = [r["hr"] for r in b if r.get("hr")]
        out.append([round(b[0]["lat"], 6), round(b[0]["lon"], 6),
                    _r(st.mean(sp), 3) if sp else None, round(st.mean(hr)) if hr else None])
    return out


def _drift(recs):
    """Aerobic decoupling: efficiency (speed/HR) in the 1st vs 2nd half, first 10% skipped as warm-up."""
    mv = [r for r in recs if r["dt"] and (r.get("speed") or 0) > 0.5 and r.get("hr")]
    total = sum(r["dt"] for r in mv)
    if total < 15 * 60:
        return None
    skip, acc, halves = total * 0.1, 0.0, ([], [])
    mid = (total + skip) / 2
    for r in mv:
        acc += r["dt"]
        if acc > skip:
            halves[acc > mid].append(r)
    (s1, h1), (s2, h2) = [(_wmean(h, "speed"), _wmean(h, "hr")) for h in halves]
    if not all((s1, h1, s2, h2)):
        return None
    ef1, ef2 = s1 / h1, s2 / h2
    return dict(decoupling_pct=round((ef1 - ef2) / ef1 * 100, 1), first_half=dict(speed=round(s1, 3), hr=round(h1)),
                second_half=dict(speed=round(s2, 3), hr=round(h2)))


def _zones(z):
    if not z or not z[0] or not z[1]:
        return None
    times, bounds = z
    out = [dict(zone=0, lo=None, hi=bounds[0], secs=round(times[0] or 0))]
    for i in range(1, min(len(times), len(bounds))):
        out.append(dict(zone=i, lo=bounds[i - 1], hi=bounds[i], secs=round(times[i] or 0)))
    return out if any(x["secs"] for x in out) else None


def _sets(sets, recs):
    out = []
    for s in sets or []:
        if s.get("set_type") == "rest":
            if out:
                out[-1]["rest"] = round(s.get("duration") or 0)
            continue
        t0, dur = s.get("start_time"), s.get("duration") or 0
        hr = None
        if t0 and recs:
            t1 = t0 + timedelta(seconds=dur)
            win = [r["hr"] for r in recs if r.get("hr") and t0 <= r["ts"] <= t1]
            hr = round(st.mean(win)) if win else None
        out.append(dict(n=len(out) + 1, exercise=s.get("exercise") or "Set", reps=s.get("repetitions"),
                        target_reps=s.get("target_reps"), weight_kg=_r(s.get("weight"), 2),
                        weight_unit="lb" if s.get("weight_display_unit") == "pound" else "kg",
                        secs=round(dur), rest=None, hr=hr))
    return out


def _laps(laps, recs):
    """Laps placed on the moving-time axis the series uses (t0/t1), so a step's window can be cut
    from the series. `step` is the workout step the watch was on (structured workouts only)."""
    if not laps or not recs:
        return None
    out = []
    for lp in sorted(laps, key=lambda x: x["start"]):
        r = next((r for r in recs if r["ts"] >= lp["start"]), recs[-1])
        out.append(dict(t0=r["t"], secs=_r(lp.get("secs")), dist=_r(lp.get("dist")), hr=lp.get("hr"), speed=_r(lp.get("speed"), 3),
                        step=lp.get("step")))
    for a, b in zip(out, out[1:] + [None]):
        a["t1"] = b["t0"] if b else recs[-1]["t"]
    return out


def _summary(s: dict) -> dict:
    g = lambda *ks: next((s[k] for k in ks if s.get(k) is not None), None)
    out = dict(
        distance=g("distance"), duration=g("duration"), moving=g("movingDuration"), elapsed=g("elapsedDuration"),
        avg_speed=g("averageSpeed"), moving_speed=g("averageMovingSpeed"), max_speed=g("maxSpeed"),
        gap_speed=g("avgGradeAdjustedSpeed"), elev_gain=g("elevationGain"), elev_loss=g("elevationLoss"),
        avg_hr=g("averageHR"), max_hr=g("maxHR"), min_hr=g("minHR"),
        cadence=g("averageRunCadence", "averageBikingCadenceInRevPerMinute", "averageBikeCadence"),
        max_cadence=g("maxRunCadence", "maxBikingCadenceInRevPerMinute", "maxBikeCadence"),
        calories=g("calories"), te=g("trainingEffect"), te_anaer=g("anaerobicTrainingEffect"),
        te_label=g("trainingEffectLabel"), load=g("activityTrainingLoad"), power=g("averagePower"),
        avg_temp=g("averageTemperature"), min_temp=g("minTemperature"), max_temp=g("maxTemperature"),
        bb_delta=g("differenceBodyBattery"), steps=g("steps"), sets=g("activeSets"), reps=g("totalExerciseReps"),
        rpe=g("directWorkoutRpe"),
    )
    return {k: v for k, v in out.items() if v is not None and not (k in ("steps", "reps") and v == 0)}


def build(det: dict, blob: bytes | None) -> dict:
    """View model for the detail panel."""
    s = det.get("summaryDTO") or {}
    typ = (det.get("activityTypeDTO") or {}).get("typeKey") or "activity"
    fit, file_note = {}, None
    if blob:
        try:
            fit = read_file(blob)
        except Exception as ex:
            file_note = f"Couldn't parse the activity file ({type(ex).__name__}); showing summary data only."
    else:
        file_note = "Activity file unavailable; showing summary data only."
    recs = _prep(fit.get("records") or [], cad_factor=1 if uses_speed(typ) else 2)
    dist_sport = is_distance_sport(typ) and (s.get("distance") or 0) > 200
    has_gps = sum(1 for r in recs if r.get("lat") is not None) > 10
    return dict(
        v=VERSION, id=det.get("activityId"), name=det.get("activityName") or _pretty(typ), type=typ,
        start=s.get("startTimeLocal"), mode="speed" if uses_speed(typ) else "pace", distance_sport=dist_sport,
        summary=_summary(s), zones=_zones(fit.get("zones")),
        series=_series(recs) if recs else [],
        splits=dict(km=_splits(recs, 1000), mi=_splits(recs, MILE)) if dist_sport and recs else None,
        route=_route(recs) if dist_sport and has_gps else None,
        sets=_sets(fit.get("sets"), recs) or None,
        laps=_laps(fit.get("laps"), recs),
        drift=_drift(recs) if dist_sport else None,
        note=file_note,
    )


# --------------------------------------------------------------------------- #
# Comparisons + coach payload
# --------------------------------------------------------------------------- #
def history_row(a: dict) -> dict:
    """Comparable stats from an activity-list item (same source for current + past workouts).
    `name` is for the local list only; coach payloads pick explicit keys and never include it."""
    secs = a.get("movingDuration") or a.get("duration")
    typ = a["activityType"].get("typeKey") if isinstance(a.get("activityType"), dict) else a.get("activityType")
    hr = a.get("averageHR")
    speed = a.get("averageSpeed") if is_distance_sport(typ) else None  # court/gym "pace" is meaningless
    return dict(id=a.get("activityId"), name=a.get("activityName"), start=str(a.get("startTimeLocal") or ""),
                date=str(a.get("startTimeLocal") or "")[:10],
                type=typ, dist_m=_r(a.get("distance") or 0), secs=_r(secs, 0), speed_ms=_r(speed, 3),
                pace_s_per_km=round(1000 / speed) if speed and speed > 0.3 else None, hr=_r(hr, 0),
                ef=_r(speed * 60 / hr, 2) if speed and hr else None,  # metres per heartbeat
                load=_r(a.get("activityTrainingLoad")), te=_r(a.get("aerobicTrainingEffect")),
                gain_m=_r(a.get("elevationGain"), 0), cadence=_r(a.get("averageRunningCadenceInStepsPerMinute"), 0),
                sets=a.get("totalSets") or a.get("activeSets"))


def _slope_per_30d(rows, key):
    pts = [(datetime.fromisoformat(r["date"]).toordinal(), r[key]) for r in rows if r.get(key) is not None]
    if len(pts) < 4 or pts[-1][0] - pts[0][0] < 14:  # too few / too short a span to call a trend
        return None
    mx, my = st.mean(p[0] for p in pts), st.mean(p[1] for p in pts)
    den = sum((x - mx) ** 2 for x, _ in pts)
    return None if not den else sum((x - mx) * (y - my) for x, y in pts) / den * 30


def compare(cur: dict, rows: list[dict], window_days: int) -> dict:
    """Similar = same type, earlier, and distance (or duration when distance is ~0) within +/-30%."""
    by_dist = cur["dist_m"] > 500 and is_distance_sport(cur["type"])
    basis = "dist_m" if by_dist else "secs"

    def similar(r):
        if r["id"] == cur["id"] or r["type"] != cur["type"] or r["start"] >= cur["start"] or not r.get(basis):
            return False
        return cur.get(basis) and abs(r[basis] / cur[basis] - 1) <= 0.3

    sims = sorted((r for r in rows if similar(r)), key=lambda r: r["start"])
    res = dict(n=len(sims), window_days=window_days,
               criteria=f"same type, {'distance' if by_dist else 'duration'} within ±30%, last {window_days} days")
    if not sims:
        return res
    avg = lambda k: (lambda xs: _r(st.mean(xs), 3) if xs else None)([r[k] for r in sims if r.get(k) is not None])
    res["avg"] = {k: avg(k) for k in ("dist_m", "secs", "speed_ms", "pace_s_per_km", "hr", "ef", "load", "te")}
    a, d = res["avg"], {}
    if cur.get("pace_s_per_km") and a["pace_s_per_km"]:
        d["pace_s_per_km"] = round(cur["pace_s_per_km"] - a["pace_s_per_km"])
    if cur.get("hr") and a["hr"]:
        d["hr_bpm"] = round(cur["hr"] - a["hr"])
    for k in ("ef", "load", "secs"):
        if cur.get(k) and a[k]:
            d[f"{k}_pct"] = round((cur[k] / a[k] - 1) * 100, 1)
    res["delta_vs_avg"] = d
    if cur.get("speed_ms"):
        speeds = sorted([r["speed_ms"] for r in sims if r.get("speed_ms")] + [cur["speed_ms"]], reverse=True)
        res["speed_rank"] = dict(position=speeds.index(cur["speed_ms"]) + 1, of=len(speeds))
    if len(sims) >= 2:  # trend across the similar workouts *including* this one
        series = sims + [cur]
        tr = {k: _slope_per_30d(series, k) for k in ("pace_s_per_km", "hr", "ef")}
        res["trend_per_30_days"] = {k: _r(v, 2) for k, v in tr.items() if v is not None}
    keys = ("date", "dist_m", "secs", "speed_ms", "pace_s_per_km", "hr", "ef", "load", "te", "sets")
    res["current"] = {k: cur.get(k) for k in keys}
    res["workouts"] = [{k: r[k] for k in keys} for r in sims[-8:]]
    return res


def coach_stats(view: dict, cur: dict, comparison: dict, history: list[dict], assessment: dict, system: str = "us") -> dict:
    """Whitelisted, location-free payload for Claude (and the stats-only fallback in the UI). Values
    are metric; `units.localize` converts them on the way out. `system` picks mile vs km splits."""
    s = view.get("summary", {})
    pace = view["mode"] == "pace" and view["distance_sport"]
    act = dict(
        type=view["type"], date=str(view.get("start") or "")[:10],
        distance_km=_r((s.get("distance") or 0) / 1000, 2) if view["distance_sport"] else None,
        moving_min=_r((s.get("moving") or s.get("duration") or 0) / 60),
        avg_pace_s_per_km=round(1000 / s["avg_speed"]) if pace and s.get("avg_speed") else None,
        grade_adjusted_pace_s_per_km=round(1000 / s["gap_speed"]) if pace and s.get("gap_speed") else None,
        avg_speed_kmh=_r(s["avg_speed"] * 3.6) if not pace and view["distance_sport"] and s.get("avg_speed") else None,
        avg_hr=s.get("avg_hr"), max_hr=s.get("max_hr"), cadence=_r(s.get("cadence"), 0),
        elev_gain_m=_r(s.get("elev_gain"), 0), calories=s.get("calories"),
        aerobic_te=_r(s.get("te")), anaerobic_te=_r(s.get("te_anaer")), te_label=s.get("te_label"),
        training_load=_r(s.get("load")), body_battery_change=s.get("bb_delta"), rpe=s.get("rpe"),
        efficiency_m_per_beat=cur.get("ef"), avg_temp_c=_r(s.get("avg_temp"), 0),
    )
    out = dict(activity={k: v for k, v in act.items() if v is not None})
    if view.get("zones"):
        out["hr_zone_minutes"] = {(f"Z{z['zone']}" if z["zone"] else "below_Z1") + f" ({z['lo'] or ''}-{z['hi']} bpm)":
                                  _r(z["secs"] / 60) for z in view["zones"]}
    per, unit_m = ("mile", MILE) if system == "us" else ("km", 1000)
    sp = (view.get("splits") or {}).get("mi" if per == "mile" else "km") or []
    if sp:
        out[f"{per}_splits"] = [{per: x["n"], "dist_m": x["dist"], "pace_s_per_km": round(x["secs"] / x["dist"] * 1000),
                                 "hr": x["hr"], "gain_m": x["gain"]} for x in sp]
        full = [x["secs"] / x["dist"] * 1000 for x in sp if x["dist"] >= 0.95 * unit_m]
        if len(full) >= 3:
            half = len(full) // 2
            out["pacing"] = dict(split_cv_pct=round(st.pstdev(full) / st.mean(full) * 100, 1),
                                 fastest_s_per_km=round(min(full)), slowest_s_per_km=round(max(full)),
                                 first_half_avg_s_per_km=round(st.mean(full[:half])),
                                 second_half_avg_s_per_km=round(st.mean(full[half:])))
    if view.get("drift"):
        d = view["drift"]
        half = lambda h: dict(pace_s_per_km=round(1000 / h["speed"]) if h["speed"] else None, hr=h["hr"])
        out["hr_drift"] = dict(decoupling_pct=d["decoupling_pct"], first_half=half(d["first_half"]), second_half=half(d["second_half"]))
    if view.get("sets"):
        ex: dict = {}
        for x in view["sets"]:
            e = ex.setdefault(x["exercise"], dict(sets=0, reps=0, top_weight_kg=0, avg_hr=[]))
            e["sets"] += 1
            e["reps"] += x["reps"] or x["target_reps"] or 0
            e["top_weight_kg"] = max(e["top_weight_kg"], x["weight_kg"] or 0)
            if x["hr"]:
                e["avg_hr"].append(x["hr"])
        for e in ex.values():
            e["avg_hr"] = round(st.mean(e["avg_hr"])) if e["avg_hr"] else None
        out["strength"] = dict(note="reps are the planned workout targets when the watch didn't log them", exercises=ex)
    out["comparison"] = comparison
    day = out["activity"]["date"]
    idx = next((i for i, h in enumerate(history) if h["date"] == day), None)
    keep = ("date", "hrv", "hrv_status", "sleep_score", "sleep_h", "rhr", "bb_high", "bb_low", "load")
    out["recovery_context"] = dict(
        days_from_activity_onward=[{k: h.get(k) for k in keep} for h in history[idx:idx + 3]] if idx is not None else [],
        current_readiness=dict(score=assessment.get("readiness"), zone=assessment.get("zone"),
                               acute_chronic_ratio=(assessment.get("strain") or {}).get("ratio")),
    )
    return out
