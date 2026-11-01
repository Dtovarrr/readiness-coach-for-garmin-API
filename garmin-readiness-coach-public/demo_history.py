"""A year of synthetic day rows (calendar-cache shape) for charts in demo mode and tests.

Each day is seeded by its date, so a day always looks the same however it's requested.
"""
import math
import random
from datetime import date, timedelta

PLAN = {0: [("running", 6000, 1900)], 1: [("strength_training", 0, 2700)], 2: [("tennis_v2", 2500, 5400)],
        3: [("running", 9000, 2900)], 5: [("running", 14000, 4600)], 6: [("tennis_v2", 2200, 4800)]}


def demo_day(d: date) -> dict:
    rnd = random.Random(d.toordinal())
    i = d.toordinal()
    acts = []
    for typ, dist, secs in PLAN.get(d.weekday(), []):
        if rnd.random() < 0.8:  # skip the odd session
            k = rnd.uniform(0.8, 1.2)
            acts.append(dict(id=None, name=typ.replace("_v2", "").replace("_", " ").title(), type=typ,
                             start=f"{d} 07:30:00", dist_m=round(dist * k), secs=round(secs * k), hr=rnd.randint(118, 158),
                             load=round(secs * k / 30), te=round(rnd.uniform(2.2, 4.2), 1)))
    if rnd.random() < 0.6:
        acts.append(dict(id=None, name="Walk", type="walking", start=f"{d} 18:00:00", dist_m=rnd.randint(1800, 4200),
                         secs=rnd.randint(1500, 3300), hr=rnd.randint(88, 104), load=4, te=0.6))
    for k, a in enumerate(acts):  # stable ids, so records, badges and the lists agree
        a["id"] = int(d.strftime("%Y%m%d")) * 10 + k
    sleep_h = round(rnd.uniform(6.0, 8.4), 2)
    weigh = rnd.random() < 0.45
    return dict(
        date=d.isoformat(), readiness=round(66 + 14 * math.sin(i / 5) + rnd.uniform(-8, 8)),
        hrv=round(58 + 6 * math.sin(i / 3) + rnd.uniform(-4, 4)), sleep_score=round(55 + sleep_h * 4 + rnd.uniform(-5, 5)),
        sleep_h=sleep_h, bb_high=round(rnd.uniform(55, 95)), bb_low=round(rnd.uniform(8, 25)),
        steps=rnd.randint(5500, 15000), distance_m=rnd.randint(4200, 12000), stress=round(rnd.uniform(22, 45)),
        load=sum(a["load"] for a in acts), weight_kg=round(79 + (date.today() - d).days * 0.006 + rnd.uniform(-0.6, 0.6), 1) if weigh else None,
        fat=round(19.5 + rnd.uniform(-0.8, 0.8), 1) if weigh else None, activities=acts)


def demo_summary(d: date) -> dict:
    rnd = random.Random(d.toordinal() * 7)
    return dict(kcal=rnd.randint(2200, 3200), active_kcal=rnd.randint(300, 1100), im=rnd.randint(0, 70), rhr=rnd.randint(46, 54))


def demo_rows(start: date, end: date) -> dict[str, dict]:
    out, d = {}, start
    while d <= end:
        out[d.isoformat()] = dict(demo_day(d), **demo_summary(d))
        d += timedelta(days=1)
    return out
