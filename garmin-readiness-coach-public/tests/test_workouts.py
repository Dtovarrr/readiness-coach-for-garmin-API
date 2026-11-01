"""Workout builder: builder -> FIT conversion read back with fitdecode (step order, repeats, targets,
miles and min/mi converted to meters and m/s, the +100 heart-rate offset), TCX, validation, plan
conversion, readiness suggestions, estimates, planned vs actual, the device copy (never deletes or
overwrites), Intervals.icu (mocked, off by default, key never echoed) and the endpoints in demo mode.

Run from the project folder:  .venv/bin/python -m unittest discover -s tests -v
Uses a throwaway COACH_DATA_DIR and cache folder; never calls Garmin, Claude or Intervals.icu.
"""
import base64
import io
import logging
import os
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
from datetime import date
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
os.environ.setdefault("COACH_DATA_DIR", tempfile.mkdtemp(prefix="coach-test-"))
sys.path.insert(0, str(ROOT))

import applog  # noqa: E402
import intervals  # noqa: E402
import store  # noqa: E402
import workout_export as X  # noqa: E402
import workouts as W  # noqa: E402

assert store.DATA_DIR != (ROOT / ".data").resolve(), "tests must never run against the real data folder"
MI = 1609.344
TODAY = date(2026, 10, 28)


def pace(min_sec: str) -> float:
    """min:sec per mile -> m/s, the way the page converts it."""
    m, s = min_sec.split(":")
    return MI / (int(m) * 60 + int(s))


def step(kind="work", dur=None, target=None, name=""):
    return dict(kind=kind, name=name, dur=dur or dict(type="time", sec=240), target=target or dict(type="open"))


def workout(steps, sport="run", name="Test"):
    return W.validate(dict(name=name, sport=sport, steps=steps))


class FitConversion(unittest.TestCase):
    def setUp(self):
        self.w = workout([
            step("warmup", dict(type="time", sec=600), dict(type="hr_zone", zone=2), "Warm up"),
            dict(kind="repeat", count=5, steps=[
                step("work", dict(type="distance", m=0.5 * MI), dict(type="pace", lo=pace("8:30"), hi=pace("8:00")), "Hard"),
                step("recovery", dict(type="time", sec=180), dict(type="hr_bpm", lo=120, hi=140), "Easy")]),
            step("cooldown", dict(type="open"), name="Cool down"),
        ], name="5 x half mile")
        self.back = X.read_back(X.to_fit(self.w))

    def test_file_header(self):
        self.assertEqual(self.back["file_type"], "workout")
        self.assertEqual(self.back["sport"], "running")
        self.assertEqual(self.back["name"], "5 x half mile")
        self.assertEqual(self.back["num_valid_steps"], 5)  # warm-up, 2 in the block, repeat marker, cool-down

    def test_step_order_and_intensity(self):
        s = self.back["steps"]
        self.assertEqual([x["index"] for x in s], [0, 1, 2, 3, 4])
        self.assertEqual([x["name"] for x in s], ["Warm up", "Hard", "Easy", None, "Cool down"])
        self.assertEqual([x["intensity"] for x in s], ["warmup", "active", "recovery", None, "cooldown"])

    def test_repeat_points_back_to_block_start(self):
        r = self.back["steps"][3]
        self.assertEqual(r["duration_type"], "repeat_until_steps_cmplt")
        self.assertEqual(r["back_to"], 1)
        self.assertEqual(r["count"], 5)

    def test_miles_to_meters_and_time(self):
        s = self.back["steps"]
        self.assertEqual(s[0]["duration_type"], "time")
        self.assertAlmostEqual(s[0]["sec"], 600)
        self.assertEqual(s[1]["duration_type"], "distance")
        self.assertAlmostEqual(s[1]["m"], 804.67, places=2)  # 0.5 mi
        self.assertEqual(s[4]["duration_type"], "open")

    def test_pace_range_to_speed(self):
        s = self.back["steps"][1]
        self.assertEqual(s["target_type"], "speed")
        self.assertAlmostEqual(s["speed_lo"], 3.156, places=3)  # 8:30 /mi, the slow end
        self.assertAlmostEqual(s["speed_hi"], 3.353, places=3)  # 8:00 /mi
        self.assertLess(s["speed_lo"], s["speed_hi"])

    def test_hr_targets(self):
        self.assertEqual(self.back["steps"][0]["target_type"], "heart_rate")
        self.assertEqual(self.back["steps"][0]["hr_zone"], 2)
        self.assertEqual((self.back["steps"][2]["hr_lo"], self.back["steps"][2]["hr_hi"]), (120, 140))

    def test_hr_bpm_offset_in_raw_file(self):
        """FIT stores a custom bpm as bpm + 100 (values up to 100 mean %HRmax)."""
        import fitdecode
        raw = []
        with fitdecode.FitReader(io.BytesIO(X.to_fit(self.w))) as fr:
            for f in fr:
                if isinstance(f, fitdecode.FitDataMessage) and f.name == "workout_step" and f.get_value("message_index") == 2:
                    raw = [f.get_value("custom_target_heart_rate_low"), f.get_value("custom_target_heart_rate_high")]
        self.assertEqual(raw, [220, 240])

    def test_every_template_round_trips(self):
        for k in W.TEMPLATE_ORDER:
            w = W.from_template(k)
            with self.subTest(template=k):
                X.check(w, X.read_back(X.to_fit(w)))

    def test_check_catches_mismatch(self):
        back = X.read_back(X.to_fit(self.w))
        back["steps"][3]["count"] = 4
        with self.assertRaises(X.ExportError):
            X.check(self.w, back)
        with self.assertRaises(X.ExportError):
            X.read_back(X.to_fit(self.w)[:-3] + b"\x00\x00\x00")  # bad CRC

    def test_sports(self):
        for sport, want in (("ride", ("cycling", "generic")), ("strength", ("training", "strength_training")), ("timed", ("training", "cardio_training"))):
            b = X.read_back(X.to_fit(workout([step()], sport)))
            self.assertEqual((b["sport"], b["sub_sport"]), want)


class Tcx(unittest.TestCase):
    def test_round_trip(self):
        w = W.from_template("intervals")
        root = ET.fromstring(X.to_tcx(w))
        ns = {"t": X.TCX_NS}
        top = root.findall("t:Workouts/t:Workout/t:Step", ns)
        self.assertEqual(len(top), 3)
        rep = top[1]
        self.assertEqual(rep.get(f"{{{X.XSI}}}type"), "Repeat_t")
        self.assertEqual(rep.find("t:Repetitions", ns).text, "5")
        self.assertEqual(len(rep.findall("t:Child", ns)), 2)
        self.assertEqual(root.find("t:Workouts/t:Workout", ns).get("Sport"), "Running")

    def test_pace_and_limits(self):
        w = workout([step(dur=dict(type="distance", m=MI), target=dict(type="pace", lo=pace("8:30"), hi=pace("8:00")))])
        self.assertIn(b"<LowInMetersPerSecond>3.156</LowInMetersPerSecond>", X.to_tcx(w))
        long = workout([dict(kind="repeat", count=2, steps=[step() for _ in range(20)])])
        with self.assertRaises(X.ExportError):
            X.to_tcx(long)


class Validation(unittest.TestCase):
    def bad(self, w, msg):
        with self.assertRaisesRegex(W.WorkoutError, msg):
            W.validate(w)

    def test_rejects(self):
        self.bad(dict(name="", sport="run", steps=[step()]), "name")
        self.bad(dict(name="x", sport="run", steps=[]), "at least one")
        self.bad(dict(name="x", sport="run", steps=[step(target=dict(type="hr_bpm", lo=150, hi=140))]), "low heart rate")
        self.bad(dict(name="x", sport="run", steps=[step(target=dict(type="pace", lo=3.4, hi=3.1))]), "slow pace")
        self.bad(dict(name="x", sport="run", steps=[step(target=dict(type="hr_zone", zone=6))]), "1 to 5")
        self.bad(dict(name="x", sport="timed", steps=[step(dur=dict(type="distance", m=1000))]), "running and cycling")
        self.bad(dict(name="x", sport="run", steps=[dict(kind="repeat", count=3, steps=[dict(kind="repeat", count=2, steps=[step()])])]), "other repeats")
        self.bad(dict(name="x", sport="run", steps=[dict(kind="repeat", count=1, steps=[step()])]), "2 to")
        self.bad(dict(name="x", sport="run", steps=[step() for _ in range(51)]), "keep it to")

    def test_estimate_and_timeline(self):
        w = W.from_template("intervals")
        self.assertEqual(len(W.timeline(w)), 12)
        self.assertEqual(W.estimate(w)["sec"], 600 + 5 * (240 + 180) + 600)
        d = workout([step(dur=dict(type="distance", m=MI), target=dict(type="pace", lo=pace("9:00"), hi=pace("8:00")))])
        self.assertAlmostEqual(W.estimate(d)["sec"], MI / ((pace("9:00") + pace("8:00")) / 2), delta=1)
        self.assertIsNone(W.estimate(W.from_template("tennis"))["m"])


class PlanAndSuggestions(unittest.TestCase):
    EV = dict(id="e1", name="Half")

    def pw(self, **kw):
        base = dict(id="p1", date="2026-11-03", sport="run", type="easy", title="Easy run", intensity="easy", distance_m=8000, detail={}, note="")
        return base | kw

    def test_from_plan(self):
        iv = W.from_plan(self.pw(type="intervals", title="Intervals", intensity="hard", detail=dict(reps=4, rep_m=1600)), self.EV)
        self.assertEqual([s["kind"] for s in iv["steps"]], ["warmup", "repeat", "cooldown"])
        self.assertEqual((iv["steps"][1]["count"], iv["steps"][1]["steps"][0]["dur"]), (4, dict(type="distance", m=1600.0)))
        self.assertEqual(iv["source"], dict(event_id="e1", workout_id="p1"))
        self.assertEqual(iv["scheduled"], "2026-11-03")
        tempo = W.from_plan(self.pw(type="tempo", intensity="hard", detail=dict(tempo_min=20)), self.EV)
        self.assertEqual(tempo["steps"][1]["dur"]["sec"], 1200)
        easy = W.from_plan(self.pw(detail=dict(strides=6)), self.EV)
        self.assertEqual(easy["steps"][-1]["count"], 6)
        st = W.from_plan(self.pw(sport="tennis", type="strength", minutes=40, distance_m=None, title="Strength"), self.EV)
        self.assertEqual((st["sport"], st["steps"][0]["dur"]), ("strength", dict(type="time", sec=2400)))
        for t in ("conditioning", "drills", "match", "hitting"):
            X.to_fit(W.from_plan(self.pw(sport="tennis", type=t, minutes=60, distance_m=None), self.EV))

    def test_readiness_zones(self):
        keys = {z: W.suggest_today(dict(zone=z, readiness=70), TODAY)["template"] for z in ("prime", "good", "caution", "recover")}
        self.assertEqual(keys, dict(prime="intervals", good="tempo", caution="easy_run", recover="easy_run"))
        rec = W.suggest_today(dict(zone="recover", readiness=30), TODAY)["workout"]
        self.assertLess(W.estimate(rec)["sec"], W.estimate(W.from_template("easy_run"))["sec"])
        self.assertIsNone(W.suggest_today(dict(), TODAY))

    def test_plan_session_eased_on_low_readiness(self):
        hard = self.pw(type="intervals", intensity="hard", title="Intervals", detail=dict(reps=5, rep_m=1000))
        on = W.suggest_today(dict(zone="good", readiness=70), TODAY, plan_today=[(self.EV, hard)])
        self.assertEqual(on["kind"], "plan")
        eased = W.suggest_today(dict(zone="recover", readiness=30), TODAY, plan_today=[(self.EV, hard)])
        self.assertEqual(eased["kind"], "plan_eased")
        rep = eased["workout"]["steps"][1]
        self.assertLess(rep["count"], 5)
        self.assertEqual(rep["steps"][0]["target"], dict(type="hr_zone", zone=2))


class Zones(unittest.TestCase):
    def test_garmin_zones(self):
        raw = [dict(sport="DEFAULT", trainingMethod="HR_MAX", maxHeartRateUsed=190, zone1Floor=95, zone2Floor=114, zone3Floor=133,
                    zone4Floor=152, zone5Floor=171), dict(sport="RUNNING", maxHeartRateUsed=188, zone1Floor=94, zone2Floor=113,
                                                           zone3Floor=132, zone4Floor=150, zone5Floor=169)]
        z = W.heart_zones(raw, "run")
        self.assertEqual(z["zones"][1], dict(zone=2, lo=113, hi=131))
        self.assertEqual(z["zones"][4]["hi"], 188)
        self.assertEqual(W.heart_zones(raw, "ride")["max"], 190)  # falls back to DEFAULT
        self.assertIsNone(W.heart_zones([dict(sport="DEFAULT", zone1Floor=0, zone2Floor=0, zone3Floor=0, zone4Floor=0, zone5Floor=0, maxHeartRateUsed=0)]))


class Compare(unittest.TestCase):
    def setUp(self):
        self.w = W.from_template("intervals")
        self.z = W.heart_zones(W.DEMO_ZONES)

    def test_laps_by_step_index(self):
        r = W.compare(self.w, W.demo_activity(self.w, "2026-10-22", self.z), self.z)
        self.assertEqual((r["alignment"], r["done_steps"], r["total_steps"]), ("laps", 12, 12))
        self.assertTrue(all(0 <= s["actual"]["in_target"] <= 100 for s in r["steps"]))
        self.assertEqual(r["steps"][1]["rep"], (1, 5))

    def test_in_target_counts(self):
        w = workout([step("work", dict(type="time", sec=100), dict(type="hr_bpm", lo=140, hi=150))])
        series = [dict(t=t, hr=145 if t <= 80 else 160) for t in range(10, 101, 10)]
        r = W.compare(w, dict(series=series, laps=[]), None)
        self.assertEqual(r["alignment"], "approx")
        self.assertEqual(r["steps"][0]["actual"]["in_target"], 80)
        self.assertEqual(r["in_target"], 80)

    def test_pace_in_target_and_unplaced_open(self):
        w = workout([step("work", dict(type="distance", m=1000), dict(type="pace", lo=3.0, hi=3.5)), step("cooldown", dict(type="open"))])
        series = [dict(t=t, d=t * 3.2, speed=3.2 if t < 250 else 2.5) for t in range(10, 400, 10)]
        r = W.compare(w, dict(series=series, laps=[]), None)
        self.assertGreaterEqual(r["steps"][0]["actual"]["in_target"], 75)
        self.assertIsNone(r["steps"][1]["actual"])


class Device(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.vol = self.tmp / "Volumes"
        (self.vol / "Macintosh HD").mkdir(parents=True)
        self.watch = self.vol / "GARMIN"
        (self.watch / "GARMIN" / "NewFiles").mkdir(parents=True)
        (self.watch / "GARMIN" / "Workouts").mkdir()
        self.src = self.tmp / "Tempo.fit"
        self.src.write_bytes(X.to_fit(W.from_template("tempo")))

    def test_find(self):
        d = X.find_devices(self.vol)
        self.assertEqual(len(d), 1)
        self.assertTrue(d[0]["folder"].endswith("GARMIN/NewFiles"))
        self.assertEqual(X.find_devices(self.tmp / "nope"), [])

    def test_confirm_required_and_no_overwrite(self):
        dev = X.find_devices(self.vol)[0]
        dry = X.install(self.src, dev, confirm=False)
        self.assertFalse(dry["copied"])
        self.assertFalse(Path(dry["path"]).exists())
        done = X.install(self.src, dev, confirm=True)
        self.assertTrue(done["copied"])
        self.assertEqual(Path(done["path"]).read_bytes(), self.src.read_bytes())
        self.assertTrue(X.install(self.src, dev, confirm=True)["already"])  # same file: nothing done
        other = self.watch / "GARMIN" / "NewFiles" / "Mine.fit"
        other.write_bytes(b"keep me")
        mine = self.tmp / "Mine.fit"
        mine.write_bytes(self.src.read_bytes())
        r = X.install(mine, dev, confirm=True)
        self.assertTrue(r["path"].endswith("Mine-2.fit"))
        self.assertEqual(other.read_bytes(), b"keep me")

    def test_no_workouts_folder(self):
        bare = self.vol / "OLD"
        (bare / "GARMIN").mkdir(parents=True)
        dev = next(d for d in X.find_devices(self.vol) if d["name"] == "OLD")
        self.assertIsNone(dev["folder"])
        with self.assertRaises(X.ExportError):
            X.install(self.src, dev, confirm=True)
        self.assertEqual(list((bare / "GARMIN").iterdir()), [])  # no folder made


class Intervals(unittest.TestCase):
    KEY = "abcdEFGH1234secretkey"

    def test_payload_and_auth(self):
        w = W.from_template("tempo")
        fit = X.to_fit(w)
        post = mock.Mock(return_value=mock.Mock(status_code=200, json=lambda: dict(id=42)))
        with mock.patch.dict(os.environ, {"INTERVALS_API_KEY": self.KEY}):
            r = intervals.push(w, fit, "2026-11-01", post=post)
        self.assertEqual(r, dict(id=42, day="2026-11-01"))
        url, kw = post.call_args[0][0], post.call_args[1]
        self.assertEqual(url, "https://intervals.icu/api/v1/athlete/0/events")
        self.assertEqual(kw["auth"], ("API_KEY", self.KEY))
        self.assertEqual(kw["params"], dict(upsertOnUid="true"))
        body = kw["json"]
        self.assertEqual((body["category"], body["type"], body["start_date_local"]), ("WORKOUT", "Run", "2026-11-01T00:00:00"))
        self.assertEqual(base64.b64decode(body["file_contents_base64"]), fit)
        self.assertEqual(body["uid"], f"garmin-coach-{w['id']}")
        self.assertNotIn(self.KEY, str(body))

    def test_errors_and_no_key(self):
        w = W.from_template("tempo")
        with mock.patch.dict(os.environ, {"INTERVALS_API_KEY": ""}):
            with self.assertRaisesRegex(intervals.IntervalsError, "INTERVALS_API_KEY"):
                intervals.push(w, b"", "2026-11-01", post=mock.Mock())
        with mock.patch.dict(os.environ, {"INTERVALS_API_KEY": self.KEY}):
            with self.assertRaises(intervals.IntervalsError) as cm:
                intervals.push(w, b"", "2026-11-01", post=mock.Mock(return_value=mock.Mock(status_code=403)))
            self.assertNotIn(self.KEY, str(cm.exception))

    def test_key_redacted_in_logs(self):
        with mock.patch.dict(os.environ, {"INTERVALS_API_KEY": self.KEY}):
            self.assertNotIn(self.KEY, applog.redact(f"failed with {self.KEY}"))
            self.assertNotIn("QVBJX0tFWTpzZWNyZXQ", applog.redact("Authorization: Basic QVBJX0tFWTpzZWNyZXQ="))


class Endpoints(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ["GARMIN_DEMO"] = "1"
        os.environ.setdefault("COACH_CACHE_DIR", tempfile.mkdtemp(prefix="coach-cache-"))
        logging.getLogger("garmin_coach").disabled = True
        from fastapi.testclient import TestClient
        import server
        cls.server = server
        cls.vol = Path(tempfile.mkdtemp()) / "Volumes"
        (cls.vol / "GARMIN" / "GARMIN" / "NewFiles").mkdir(parents=True)
        server.VOLUMES = cls.vol
        cls.c = TestClient(server.app, base_url=server.LOCAL_URL, headers={"Origin": server.LOCAL_URL})

    def make(self, key="intervals", **kw):
        t = W.TEMPLATES[key]
        r = self.c.post("/api/workouts", json=dict({k: t[k] for k in ("name", "sport", "notes", "steps")}, **kw))
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()

    def test_crud_export_install(self):
        w = self.make(scheduled="2026-10-22")
        self.assertEqual(w["est"]["sec"], 3300)
        lst = self.c.get("/api/workouts").json()
        self.assertIn(w["id"], [x["id"] for x in lst["workouts"]])
        self.assertEqual(lst["zones"]["run"]["zones"][3]["lo"], 150)
        self.assertFalse(lst["intervals"]["enabled"])
        ex = self.c.post(f"/api/workouts/{w['id']}/export?fmt=fit").json()
        self.assertTrue(Path(ex["path"]).is_file())
        self.assertTrue(Path(ex["path"]).is_relative_to(store.DATA_DIR))
        X.check(W.validate(w), X.read_back(Path(ex["path"]).read_bytes()))
        self.assertEqual(self.c.post(f"/api/workouts/{w['id']}/export?fmt=tcx").status_code, 200)
        dev = self.c.get("/api/devices").json()["devices"][0]
        dry = self.c.post(f"/api/workouts/{w['id']}/install", json=dict(mount=dev["mount"])).json()
        self.assertFalse(dry["copied"])
        self.assertFalse(any((self.vol / "GARMIN" / "GARMIN" / "NewFiles").iterdir()))
        self.assertTrue(self.c.post(f"/api/workouts/{w['id']}/install", json=dict(mount=dev["mount"], confirm=True)).json()["copied"])
        dup = self.c.post(f"/api/workouts/{w['id']}/duplicate").json()
        self.assertNotEqual(dup["id"], w["id"])
        self.assertTrue(dup["name"].endswith("(copy)"))
        edited = self.c.put(f"/api/workouts/{w['id']}", json=dict(name="Renamed", sport="run", steps=w["steps"])).json()
        self.assertEqual(edited["name"], "Renamed")
        self.assertEqual(self.c.delete(f"/api/workouts/{dup['id']}").status_code, 200)
        self.assertEqual(self.c.get(f"/api/workouts/{dup['id']}").status_code, 404)

    def test_bad_input_and_reveal_guard(self):
        r = self.c.post("/api/workouts", json=dict(name="x", sport="run", steps=[step(target=dict(type="hr_zone", zone=9))]))
        self.assertEqual(r.status_code, 422)
        self.assertEqual(self.c.post("/api/workouts/reveal", json=dict(path="/etc/hosts")).status_code, 404)

    def test_intervals_off_by_default(self):
        w = self.make("tempo")
        self.assertEqual(self.c.post(f"/api/workouts/{w['id']}/intervals").status_code, 409)
        s = self.c.get("/api/settings").json()
        self.assertFalse(s["intervals_sync"])
        self.assertNotIn("INTERVALS_API_KEY", str(s))

    def test_compare_demo_and_states(self):
        w = self.make(scheduled="2026-01-01")
        acts = {date.fromisoformat("2026-01-01").isoformat(): [dict(id=1, type="running", dist_m=9000, secs=3300)]}
        with mock.patch.object(self.server, "_acts_by_day", return_value=acts):
            r = self.c.get(f"/api/workouts/{w['id']}/compare").json()
        self.assertEqual((r["state"], r["alignment"], r["total_steps"]), ("done", "laps", 12))
        with mock.patch.object(self.server, "_acts_by_day", return_value={}):
            self.assertEqual(self.c.get(f"/api/workouts/{w['id']}/compare").json()["state"], "not_done")
        later = self.make(scheduled="2099-01-01")
        self.assertEqual(self.c.get(f"/api/workouts/{later['id']}/compare").json()["state"], "upcoming")

    def test_suggestion_accept_and_dismiss(self):
        with mock.patch.object(self.server, "_assessment", return_value=dict(zone="good", readiness=72)), \
                mock.patch.object(store, "events", return_value=[]):  # other suites leave events with plans behind
            s = self.c.get("/api/workouts/suggest").json()["suggestion"]
            self.assertEqual(s["kind"], "readiness")
            acc = self.c.post("/api/workouts/suggest", json=dict(action="accept")).json()
            self.assertEqual(acc["source"]["suggested"], date.today().isoformat())
            self.assertIsNone(self.c.get("/api/workouts/suggest").json()["suggestion"])  # once accepted, not offered again
        self.c.delete(f"/api/workouts/{acc['id']}")
        self.c.post("/api/workouts/suggest", json=dict(action="dismiss"))
        self.assertEqual(self.c.get("/api/workouts/suggest").json()["why"], "dismissed")
        store.settings_put(dict(workout_suggest_dismissed=None))


if __name__ == "__main__":
    unittest.main()
