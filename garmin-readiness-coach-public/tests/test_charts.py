"""Custom charts: weekly/monthly buckets, aggregation and gaps, activity filters, previous period,
goal lines, storage and the series endpoint (demo data).

Run from the project folder:  .venv/bin/python -m unittest discover -s tests -v
Uses a throwaway COACH_DATA_DIR, never the real .data/ folder.
"""
import os
import sys
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
os.environ.setdefault("COACH_DATA_DIR", tempfile.mkdtemp(prefix="coach-test-"))
sys.path.insert(0, str(ROOT))

import charts  # noqa: E402
import store  # noqa: E402

assert store.DATA_DIR != (ROOT / ".data").resolve(), "tests must never run against the real data folder"
WED = date(2026, 10, 28)  # a Wednesday


def rows(start: date, end: date, **per_day):
    """Loaded day rows; per_day values are callables of the date (None = no reading)."""
    out, d = {}, start
    while d <= end:
        out[d.isoformat()] = dict(date=d.isoformat(), activities=[], **{k: f(d) for k, f in per_day.items()})
        d += timedelta(days=1)
    return out


def act(typ, dist_m=0, secs=0):
    return dict(type=typ, dist_m=dist_m, secs=secs)


class Buckets(unittest.TestCase):
    def test_weeks_run_monday_to_sunday(self):
        b = charts.buckets(date(2026, 10, 5), date(2026, 10, 25), "week")
        self.assertEqual([(s.isoformat(), e.isoformat()) for s, e, _ in b],
                         [("2026-10-05", "2026-10-11"), ("2026-10-12", "2026-10-18"), ("2026-10-19", "2026-10-25")])
        self.assertTrue(all(s.weekday() == 0 and e.weekday() == 6 for s, e, _ in b))
        self.assertFalse(any(p for *_, p in b))

    def test_mid_week_start_and_end_are_clipped_and_partial(self):
        b = charts.buckets(date(2026, 10, 8), WED, "week")  # Thu .. Wed three weeks later
        self.assertEqual((b[0][0], b[0][1], b[0][2]), (date(2026, 10, 8), date(2026, 10, 11), True))
        self.assertEqual((b[-1][0], b[-1][1], b[-1][2]), (date(2026, 10, 26), WED, True))
        self.assertEqual(len(b), 4)

    def test_week_across_a_year_boundary(self):
        b = charts.buckets(date(2025, 12, 29), date(2026, 1, 4), "week")
        self.assertEqual(b, [(date(2025, 12, 29), date(2026, 1, 4), False)])

    def test_months_including_february(self):
        b = charts.buckets(date(2026, 1, 15), date(2026, 3, 31), "month")
        self.assertEqual([(s.isoformat(), e.isoformat(), p) for s, e, p in b],
                         [("2026-01-15", "2026-01-31", True), ("2026-02-01", "2026-02-28", False), ("2026-03-01", "2026-03-31", False)])

    def test_daily(self):
        self.assertEqual(len(charts.buckets(WED - timedelta(days=6), WED, "day")), 7)

    def test_presets_snap_to_whole_weeks_and_months(self):
        s, e = charts.resolve_range(dict(range="12W", grouping="week"), WED)
        self.assertEqual((s, e), (date(2026, 8, 10), WED))
        self.assertEqual(s.weekday(), 0)
        self.assertEqual(len(charts.buckets(s, e, "week")), 12)
        s, _ = charts.resolve_range(dict(range="6M", grouping="month"), WED)
        self.assertEqual(s, date(2026, 5, 1))
        s, _ = charts.resolve_range(dict(range="1Y", grouping="month"), date(2026, 1, 10))
        self.assertEqual(s, date(2025, 2, 1))
        self.assertEqual(charts.resolve_range(dict(range="30D", grouping="day"), WED)[0], WED - timedelta(days=29))


class Aggregate(unittest.TestCase):
    def test_functions(self):
        v = [3.0, None, 5.0, 1.0]
        self.assertEqual(charts.aggregate(v, "sum"), 9.0)
        self.assertEqual(charts.aggregate(v, "avg"), 3.0)
        self.assertEqual(charts.aggregate(v, "max"), 5.0)
        self.assertEqual(charts.aggregate(v, "count"), 3.0)

    def test_empty_bucket_is_zero_for_sums_and_a_gap_otherwise(self):
        self.assertEqual(charts.aggregate([None, None], "sum"), 0.0)
        self.assertEqual(charts.aggregate([None, None], "count"), 0.0)
        self.assertIsNone(charts.aggregate([None, None], "avg"))
        self.assertIsNone(charts.aggregate([None, None], "max"))

    def test_nothing_loaded_is_always_a_gap(self):
        for how in charts.AGGS:
            self.assertIsNone(charts.aggregate([], how, loaded=0), how)


class Series(unittest.TestCase):
    def test_weekly_sum_and_average_of_steps(self):
        start = date(2026, 10, 12)  # Monday
        r = rows(start, date(2026, 10, 25), steps=lambda d: 1000.0 * (d.day % 3))
        r["2026-10-14"]["steps"] = None  # watch not worn
        base = dict(metric="steps", grouping="week", range="custom", start=start.isoformat(), end="2026-10-25")
        s = charts.series(dict(base, agg="sum"), r, {}, WED)
        week1 = [1000.0 * (d % 3) for d in range(12, 19) if d != 14]
        self.assertEqual([p["value"] for p in s["points"]], [sum(week1), sum(1000.0 * (d % 3) for d in range(19, 26))])
        a = charts.series(dict(base, agg="avg"), r, {}, WED)
        self.assertAlmostEqual(a["points"][0]["value"], sum(week1) / 6)  # the missing day is skipped, not a zero

    def test_daily_average_shows_gaps_for_missing_days(self):
        r = rows(WED - timedelta(days=6), WED, weight_kg=lambda d: 80.0 if d.day % 2 else None)
        s = charts.series(dict(metric="weight", grouping="day", agg="avg", range="7D"), r, {}, WED)
        self.assertEqual([p["value"] for p in s["points"]], [None if d.day % 2 == 0 else 80.0
                                                             for d in (WED - timedelta(days=i) for i in range(6, -1, -1))])

    def test_days_not_loaded_are_gaps_even_for_sums(self):
        r = rows(date(2026, 10, 19), WED, steps=lambda d: 5000.0)  # the week before isn't fetched yet
        s = charts.series(dict(metric="steps", grouping="week", agg="sum", range="custom", start="2026-10-12", end="2026-10-25"),
                          r, {}, WED)
        self.assertEqual([p["value"] for p in s["points"]], [None, 35000.0])

    def test_activity_filters_and_units(self):
        r = rows(date(2026, 10, 19), date(2026, 10, 25))
        r["2026-10-19"]["activities"] = [act("running", 5000, 1800), act("tennis_v2", 2000, 5400)]
        r["2026-10-21"]["activities"] = [act("trail_running", 8000, 3600), act("walking", 3000, 2400)]
        r["2026-10-24"]["activities"] = [act("strength_training", 0, 2700)]
        wk = dict(grouping="week", range="custom", start="2026-10-19", end="2026-10-25")
        val = lambda **d: charts.series(dict(wk, **d), r, {}, WED)["points"][0]["value"]
        self.assertEqual(val(metric="act_distance", types=["run"], agg="sum"), 13000.0)  # metres; the page converts
        self.assertAlmostEqual(val(metric="act_hours", types=["tennis"], agg="sum"), 1.5)
        self.assertEqual(val(metric="act_sessions", types=["strength_training"], agg="sum"), 1.0)
        self.assertEqual(val(metric="act_sessions", exclude_walks=True, agg="sum"), 4.0)
        self.assertEqual(val(metric="act_distance", types=["walk"], agg="count"), 1.0)  # count = sessions
        self.assertEqual(val(metric="act_hours", types=["tennis"], agg="max"), 1.5)
        empty = charts.series(dict(wk, metric="act_hours", types=["golf"], agg="sum"), r, {}, WED)
        self.assertEqual(empty["points"][0]["value"], 0.0)  # loaded days without golf are 0 hours, not a gap

    def test_total_previous_period_and_change(self):
        r = rows(WED - timedelta(days=59), WED, steps=lambda d: 6000.0 if d > WED - timedelta(days=30) else 5000.0)
        s = charts.series(dict(metric="steps", grouping="day", agg="avg", range="30D"), r, {}, WED)
        self.assertEqual((s["total"], s["prev_total"], s["change_pct"]), (6000.0, 5000.0, 20.0))
        self.assertEqual(s["prev_end"], (WED - timedelta(days=30)).isoformat())

    def test_no_previous_period_beyond_the_history_cap_or_without_data(self):
        r = rows(WED - timedelta(days=364), WED, steps=lambda d: 1.0)
        s = charts.series(dict(metric="steps", grouping="day", agg="sum", range="1Y"), r, {}, WED)
        self.assertEqual(s["total"], 365.0)
        self.assertIsNone(s["prev_total"])
        s = charts.series(dict(metric="steps", grouping="day", agg="sum", range="7D"), rows(WED - timedelta(days=6), WED, steps=lambda d: 1.0), {}, WED)
        self.assertIsNone(s["prev_total"])  # the week before isn't loaded

    def test_checkin_ratings(self):
        cis = {"2026-10-26": dict(ratings=dict(mood=1)), "2026-10-27": dict(ratings=dict(mood=-1)), "2026-10-28": dict(ratings=dict(mood=1))}
        d = dict(metric="checkin:mood", grouping="week", agg="avg", range="custom", start="2026-10-26", end="2026-10-28")
        s = charts.series(d, {}, cis, WED, {"mood": "Mood"})
        self.assertAlmostEqual(s["points"][0]["value"], 1 / 3)
        self.assertEqual(charts.series(dict(d, agg="count"), {}, cis, WED, {"mood": "Mood"})["points"][0]["value"], 3.0)

    def test_goal_lines(self):
        run = dict(metric="act_distance", types=["run"], grouping="week", agg="sum", goal=dict(mode="goal", goal_id="g1"))
        self.assertEqual(charts.goal_value(run, dict(type="weekly_run_km", target=40.0)), (40000.0, None))
        self.assertIsNotNone(charts.goal_value(dict(run, grouping="day"), dict(type="weekly_run_km", target=40.0))[1])
        self.assertEqual(charts.goal_value(dict(goal=dict(mode="value", value=10000.0)), None), (10000.0, None))
        self.assertEqual(charts.goal_value(dict(metric="steps", agg="avg", grouping="day", goal=dict(mode="goal")), None)[0], None)


class Validation(unittest.TestCase):
    def test_clean(self):
        d = charts.clean(dict(metric="steps"), today=WED)
        self.assertEqual((d["name"], d["agg"], d["grouping"], d["goal"]), ("Steps", "avg", "day", dict(mode="none")))
        with self.assertRaises(ValueError):
            charts.clean(dict(metric="hrv", agg="sum"), today=WED)  # summing HRV means nothing
        with self.assertRaises(ValueError):
            charts.clean(dict(metric="nope"), today=WED)
        with self.assertRaises(ValueError):
            charts.clean(dict(metric="steps", range="custom", start="2024-01-01", end="2024-02-01"), today=WED)
        c = charts.clean(dict(metric="steps", range="custom", start="2026-10-20", end="2026-10-01"), today=WED)
        self.assertEqual((c["start"], c["end"]), ("2026-10-01", "2026-10-20"))


class Storage(unittest.TestCase):
    def setUp(self):
        for c in store.charts():
            store.chart_delete(c["id"])
        store._write("DELETE FROM meta WHERE key = 'charts_seeded'")  # the endpoint test may have seeded already

    def test_seed_once_reorder_duplicate_position_and_delete(self):
        starters = [charts.clean(dict(s)) for s in charts.STARTERS]
        store.charts_seed(starters)
        self.assertFalse(store.charts_seed(starters))  # only ever once, so deleting them sticks
        ids = [c["id"] for c in store.charts()]
        self.assertEqual(len(ids), 3)
        store.charts_reorder([ids[2], ids[0]])
        self.assertEqual([c["id"] for c in store.charts()], [ids[2], ids[0], ids[1]])
        dup = dict(store.charts()[0], id="dup1")
        store.chart_insert(dup, after=ids[2])
        self.assertEqual([c["id"] for c in store.charts()], [ids[2], "dup1", ids[0], ids[1]])
        store.chart_delete(ids[0])
        self.assertNotIn(ids[0], [c["id"] for c in store.charts()])


class Endpoint(unittest.TestCase):
    def test_series_in_demo_mode(self):
        os.environ["GARMIN_DEMO"] = "1"
        from fastapi.testclient import TestClient
        import server
        c = TestClient(server.app, base_url=server.LOCAL_URL, headers={"Origin": server.LOCAL_URL})
        body = c.get("/api/charts").json()
        self.assertIn("tennis_v2", [t["type"] for t in body["activity_types"]])
        r = c.post("/api/charts/series", json=dict(metric="act_hours", types=["tennis"], grouping="week", agg="sum", range="12W"))
        self.assertEqual(r.status_code, 200)
        s = r.json()
        self.assertEqual(len(s["points"]), 12)
        self.assertTrue(s["points"][-1]["partial"] or date.today().weekday() == 6)
        self.assertIsNone(s["loading"])
        self.assertEqual(c.post("/api/charts/series", json=dict(metric="hrv", agg="sum")).status_code, 422)

    def test_delete_and_undo(self):
        from fastapi.testclient import TestClient
        import server
        c = TestClient(server.app, base_url=server.LOCAL_URL, headers={"Origin": server.LOCAL_URL})
        a = c.post("/api/charts", json=dict(name="A", metric="steps")).json()
        b = c.post("/api/charts", json=dict(name="B", metric="hrv")).json()
        ids = lambda: [x["id"] for x in store.charts()]
        self.assertIsInstance(a["id"], str)

        r = c.delete(f"/api/charts/{a['id']}")
        self.assertEqual((r.status_code, r.json()), (200, {"ok": True}))
        self.assertNotIn(a["id"], ids())  # committed: a fresh connection no longer sees it
        self.assertIn(b["id"], ids())
        self.assertEqual(c.delete(f"/api/charts/{a['id']}").status_code, 404)
        self.assertEqual(c.delete("/api/charts/does-not-exist").status_code, 404)
        self.assertEqual(c.post(f"/api/charts/{a['id']}").status_code, 405)

        # Undo, as the page does it: re-create the same definition, then restore the order
        back = c.post("/api/charts", json=a).json()
        self.assertEqual({k: back[k] for k in ("name", "metric", "agg")}, {k: a[k] for k in ("name", "metric", "agg")})
        order = [back["id"]] + [i for i in ids() if i != back["id"]]
        self.assertEqual(c.put("/api/charts/order", json=order).status_code, 200)
        self.assertEqual(ids()[0], back["id"])
        for i in (back["id"], b["id"]):
            c.delete(f"/api/charts/{i}")


if __name__ == "__main__":
    unittest.main()
