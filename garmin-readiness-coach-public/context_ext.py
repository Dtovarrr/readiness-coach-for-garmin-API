"""Optional context beyond Garmin, both off by default.

Weather (Open-Meteo, no key): you type a city; the city centre from the geocoding API is stored
rounded to 0.1 degree, and only those coordinates go to Open-Meteo. The coach gets forecast numbers
only (no city, no coordinates).

Calendar (ICS): a private ICS link (COACH_CALENDAR_ICS in .env) or a local .ics file. Only busy
blocks (start/end) are kept; event titles, descriptions, attendees and locations are dropped while
parsing and never stored or sent.
"""
from __future__ import annotations

import os
import re
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx

import store

GEO_URL = "https://geocoding-api.open-meteo.com/v1/search"
WX_URL = "https://api.open-meteo.com/v1/forecast"
WX_TTL, CAL_TTL = 3 * 3600, 30 * 60
MAX_DAYS = 14
_mem: dict = {}


def settings() -> dict:
    s = store.settings()
    city = s.get("ctx_weather_city") or None
    return dict(weather=bool(s.get("ctx_weather_on")), city=dict(name=city["name"], region=city.get("region")) if city else None,
                calendar=bool(s.get("ctx_calendar_on")), calendar_file=s.get("ctx_calendar_file") or "",
                calendar_url_set=bool(os.getenv("COACH_CALENDAR_ICS")))


# ----------------------------------- weather ----------------------------------- #
def set_city(name: str, get=httpx.get) -> dict:
    """Look the city up and store its centre (rounded to 0.1 degree). Only the name you typed is sent."""
    name = " ".join(name.split())[:80]
    if len(name) < 2:
        raise ValueError("Type a city name.")
    r = get(GEO_URL, params=dict(name=name, count=1, language="en", format="json"), timeout=10)
    r.raise_for_status()
    res = (r.json().get("results") or [None])[0]
    if not res:
        raise ValueError("Couldn't find that city. Try adding the country, e.g. 'Portland, US'.")
    city = dict(name=res.get("name") or name, region=", ".join(x for x in (res.get("admin1"), res.get("country")) if x),
                lat=round(float(res["latitude"]), 1), lon=round(float(res["longitude"]), 1))
    store.settings_put(dict(ctx_weather_city=city))
    _mem.pop("wx", None)
    return city


def forecast(start: date, end: date, system: str = "us", get=httpx.get) -> dict:
    """Daily forecast for [start, end] (today to 14 days out), in the user's units."""
    s = store.settings()
    city = s.get("ctx_weather_city")
    if not s.get("ctx_weather_on") or not city:
        return dict(status="no_data", reason="Weather is off or no city is set (Settings › Coach context).")
    us = system == "us"
    hit = _mem.get("wx")
    if not hit or time.time() - hit[0] > WX_TTL or hit[1] != (city["lat"], city["lon"], us):
        params = dict(latitude=city["lat"], longitude=city["lon"], timezone="auto", forecast_days=16,
                      daily="temperature_2m_max,temperature_2m_min,precipitation_probability_max,precipitation_sum,wind_speed_10m_max,weather_code")
        if us:
            params.update(temperature_unit="fahrenheit", wind_speed_unit="mph", precipitation_unit="inch")
        r = get(WX_URL, params=params, timeout=10)
        r.raise_for_status()
        hit = _mem["wx"] = (time.time(), (city["lat"], city["lon"], us), r.json().get("daily") or {})
    d = hit[2]
    u = ("f", "mph", "in") if us else ("c", "kmh", "mm")
    days = []
    for i, day in enumerate(d.get("time") or []):
        if str(start) <= day <= str(end):
            g = lambda k: (d.get(k) or [None] * (i + 1))[i]
            days.append({"date": day, f"high_{u[0]}": g("temperature_2m_max"), f"low_{u[0]}": g("temperature_2m_min"),
                         "rain_chance_pct": g("precipitation_probability_max"), f"rain_{u[2]}": g("precipitation_sum"),
                         f"wind_max_{u[1]}": g("wind_speed_10m_max"), "conditions": CODES.get(g("weather_code"), "mixed")})
    return dict(status="ok", days=days) if days else dict(status="no_data", reason="The forecast covers today to 16 days ahead.")


CODES = {0: "clear", 1: "mostly clear", 2: "partly cloudy", 3: "overcast", 45: "fog", 48: "fog", 51: "light drizzle", 53: "drizzle",
         55: "heavy drizzle", 61: "light rain", 63: "rain", 65: "heavy rain", 71: "light snow", 73: "snow", 75: "heavy snow",
         80: "showers", 81: "showers", 82: "heavy showers", 95: "thunderstorms", 96: "thunderstorms with hail", 99: "thunderstorms with hail"}


# ---------------------------------- calendar ----------------------------------- #
def _unfold(text: str) -> list[str]:
    return re.sub(r"\r?\n[ \t]", "", text).splitlines()


def _dt(value: str, params: str) -> tuple[datetime, bool]:
    """An ICS DATE/DATE-TIME as an aware local datetime, plus whether it's an all-day date."""
    tzid = re.search(r"TZID=([^;:]+)", params or "")
    if re.fullmatch(r"\d{8}", value):
        return datetime.strptime(value, "%Y%m%d").astimezone(), True
    utc = value.endswith("Z")
    dt = datetime.strptime(value.rstrip("Z")[:15], "%Y%m%dT%H%M%S")
    if utc:
        dt = dt.replace(tzinfo=timezone.utc)
    elif tzid:
        try:
            dt = dt.replace(tzinfo=ZoneInfo(tzid.group(1)))
        except Exception:
            dt = dt.astimezone()
    else:
        dt = dt.astimezone()
    return dt.astimezone(), False


def busy_blocks(ics: str, start: date, end: date) -> list[dict]:
    """[{start, end, all_day}] overlapping [start, end]. Reads only DTSTART/DTEND/DURATION/RRULE/STATUS/
    TRANSP: titles, descriptions, attendees and locations are never read."""
    out, ev = [], None
    lo = datetime.combine(start, datetime.min.time()).astimezone()
    hi = datetime.combine(end + timedelta(days=1), datetime.min.time()).astimezone()
    for line in _unfold(ics):
        if line == "BEGIN:VEVENT":
            ev = {}
        elif line == "END:VEVENT" and ev is not None:
            if "start" in ev and ev.get("status") != "CANCELLED" and ev.get("transp") != "TRANSPARENT":
                s0, allday = ev["start"]
                e0 = ev.get("end") or (s0 + timedelta(days=1) if allday else s0 + timedelta(hours=1))
                step = {"DAILY": timedelta(days=1), "WEEKLY": timedelta(weeks=1)}.get(ev.get("freq"))
                count, until, n = ev.get("count") or 500, ev.get("until"), 0
                s, e = s0, e0
                while s < hi and n < count and (until is None or s <= until):
                    if e > lo:
                        out.append(dict(start=s.isoformat(timespec="minutes"), end=e.isoformat(timespec="minutes"), all_day=allday))
                    if not step:
                        break
                    s, e, n = s + step, e + step, n + 1
            ev = None
        elif ev is not None and ":" in line:
            head, value = line.split(":", 1)
            name, _, params = head.partition(";")
            if name == "DTSTART":
                ev["start"] = _dt(value.strip(), params)
            elif name == "DTEND":
                ev["end"] = _dt(value.strip(), params)[0]
            elif name == "STATUS":
                ev["status"] = value.strip().upper()
            elif name == "TRANSP":
                ev["transp"] = value.strip().upper()
            elif name == "RRULE":
                rule = dict(x.split("=", 1) for x in value.split(";") if "=" in x)
                ev["freq"] = rule.get("FREQ")
                if rule.get("COUNT", "").isdigit():
                    ev["count"] = int(rule["COUNT"])
                if rule.get("UNTIL"):
                    ev["until"] = _dt(rule["UNTIL"], "")[0]
    return sorted(out, key=lambda b: b["start"])


def _ics_text(get=httpx.get) -> str | None:
    s = store.settings()
    path, url = (s.get("ctx_calendar_file") or "").strip(), os.getenv("COACH_CALENDAR_ICS")
    if path:
        p = Path(path).expanduser()
        return p.read_text(errors="ignore") if p.is_file() and p.suffix.lower() == ".ics" else None
    if url:
        hit = _mem.get("ics")
        if not hit or time.time() - hit[0] > CAL_TTL:
            r = get(url.replace("webcal://", "https://"), timeout=15, follow_redirects=True)
            r.raise_for_status()
            hit = _mem["ics"] = (time.time(), r.text)
        return hit[1]
    return None


def calendar(start: date, end: date, get=httpx.get) -> dict:
    """Busy blocks and free daytime windows (7:00-21:00) for up to 14 days."""
    if not store.settings().get("ctx_calendar_on"):
        return dict(status="no_data", reason="Calendar context is off (Settings › Coach context).")
    if (end - start).days + 1 > MAX_DAYS:
        end = start + timedelta(days=MAX_DAYS - 1)
    text = _ics_text(get)
    if text is None:
        return dict(status="no_data", reason="No calendar source: add COACH_CALENDAR_ICS to .env or choose an .ics file.")
    blocks = busy_blocks(text, start, end)
    days = []
    d = start
    while d <= end:
        day_lo = datetime.combine(d, datetime.min.time()).astimezone().replace(hour=7)
        day_hi = day_lo.replace(hour=21)
        busy = sorted((max(datetime.fromisoformat(b["start"]), day_lo), min(datetime.fromisoformat(b["end"]), day_hi))
                      for b in blocks if datetime.fromisoformat(b["start"]) < day_hi and datetime.fromisoformat(b["end"]) > day_lo)
        free, cur = [], day_lo
        for a, b in busy:
            if a > cur and (a - cur).total_seconds() >= 1800:
                free.append(f"{cur:%H:%M}-{a:%H:%M}")
            cur = max(cur, b)
        if day_hi > cur and (day_hi - cur).total_seconds() >= 1800:
            free.append(f"{cur:%H:%M}-{day_hi:%H:%M}")
        days.append(dict(date=d.isoformat(), busy=[f"{a:%H:%M}-{b:%H:%M}" for a, b in busy], free=free))
        d += timedelta(days=1)
    return dict(status="ok", days=days)
