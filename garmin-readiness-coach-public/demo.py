"""Synthetic data so the dashboard can be explored without a Garmin key (GARMIN_DEMO=1)."""
import math
import random
from datetime import date, timedelta


def demo_snapshot(days: int = 28) -> dict:
    rnd = random.Random(42)
    end = date.today()
    hist, acts = [], []
    for i in range(days):
        d = end - timedelta(days=days - 1 - i)
        hard = d.weekday() in (1, 3, 5)
        load = rnd.randint(90, 190) if hard else (rnd.randint(0, 40) if d.weekday() != 6 else 0)
        hrv = round(58 + 6 * math.sin(i / 3) + rnd.uniform(-4, 4) - (6 if i >= days - 3 else 0))
        sleep_h = round(rnd.uniform(6.1, 8.2), 2)
        hist.append(dict(
            date=d.isoformat(), hrv=hrv, hrv_weekly=58, hrv_low=52, hrv_high=66, hrv_status="BALANCED",
            sleep_score=round(55 + sleep_h * 4 + rnd.uniform(-5, 5)), sleep_h=sleep_h,
            deep_h=round(sleep_h * 0.18, 2), rem_h=round(sleep_h * 0.22, 2), rhr=round(51 + rnd.uniform(-2, 3)),
            bb_high=round(rnd.uniform(55, 95)), bb_low=round(rnd.uniform(8, 25)),
            bb_charged=round(rnd.uniform(40, 80)), bb_drained=round(rnd.uniform(45, 85)),
            steps=rnd.randint(5500, 15000), stress=round(rnd.uniform(22, 45)), load=load,
            active_min=round(load * 0.55, 1),
        ))
        if load > 60:
            acts.append(dict(date=d.isoformat(), name=rnd.choice(["Tempo Run", "Intervals", "Long Ride", "Strength"]),
                             type="running", minutes=round(load * 0.5), km=round(load / 14, 1),
                             avg_hr=rnd.randint(138, 162), load=load, te=round(rnd.uniform(2.4, 4.4), 1)))
    return dict(history=hist, activities=sorted(acts, key=lambda a: a["date"], reverse=True)[:15],
                errors={}, source="demo", generated=end.isoformat())
