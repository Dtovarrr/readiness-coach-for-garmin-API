"""DEXA scans: the BodySpec parser, layout tolerance, validation, duplicate handling, history backfill
and upgrade, privacy (name and birth date never stored, served, logged or sent to the coach), the API,
export, goals and the rule-based analysis.

Everything runs on tests/fixtures/synthetic_bodyspec.pdf, a BodySpec-style report for an invented
"Test Person" with invented numbers (tests/fixtures/synthetic_dexa.py writes it and holds the values
the tests expect). Your own reports can go in samples/ (git-ignored): SamplesStructure then checks
that each one parses into a complete, internally consistent scan, without ever comparing values
(COACH_SAMPLES_DIR=/some/folder points it elsewhere).

Run from the project folder:  .venv/bin/python -m unittest discover -s tests -v
Uses a throwaway COACH_DATA_DIR and cache folder, never the real .data/ or .cache/, and never calls Claude.
"""
import io
import json
import logging
import os
import re
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
os.environ.setdefault("COACH_DATA_DIR", tempfile.mkdtemp(prefix="coach-test-"))
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import dexa  # noqa: E402
import dexa_parse as P  # noqa: E402
import store  # noqa: E402
from fixtures import synthetic_dexa as F  # noqa: E402
from units import KG_LB  # noqa: E402

assert store.DATA_DIR != (ROOT / ".data").resolve(), "tests must never run against the real data folder"
FIXTURE = F.PDF_PATH
DATA = FIXTURE.read_bytes()
D0, D1, D2 = F.ISO  # the scan and its two history rows, newest first
SOURCE = f"DEXA scan {F.date.fromisoformat(D0):%b} {F.date.fromisoformat(D0).day}, {D0[:4]}"
SAMPLES = sorted(Path(os.getenv("COACH_SAMPLES_DIR") or ROOT / "samples").glob("*.pdf"))  # optional: your own reports, never committed
ENV = {"GARMIN_DEMO": "1", "ANTHROPIC_API_KEY": ""}


def lb(kg, d=1):
    return None if kg is None else round(kg * KG_LB, d)


def comp_lb(r: dict) -> tuple:
    return (r.get("fat_pct"), lb(r.get("total_kg")), lb(r.get("fat_kg")), lb(r.get("lean_kg")), lb(r.get("bmc_kg")))


def identity_tokens(data: bytes) -> list[str]:
    """The client's name words and birth date, read from the PDF at runtime (never hard-coded here)."""
    lines = []
    for words in P.extract_words(data):
        lines += P.lines_of(words)
    i = next(n for n, ln in enumerate(lines) if ln.low and ln.low[0] == "client")
    hdr, row = lines[i], lines[i + 1]
    sex_x = hdr.find(("sex",))[0]
    birth_x = hdr.find(("birth",))[0]
    name = [w["text"].strip(",") for w in row.words if w["x1"] < sex_x - 2]
    dob = min((w for w in row.words if P.parse_date(w["text"])), key=lambda w: abs(w["x0"] - birth_x))["text"]
    iso = P.parse_date(dob)
    return [t for t in name if len(t) > 2] + [dob, iso]


def mentions(blob: str, token: str) -> bool:
    """`token` appears in `blob` as a whole word ("Test" in "latest_scan" doesn't count), any case."""
    return re.search(rf"(?<![a-z0-9]){re.escape(token.lower())}(?![a-z0-9])", blob.lower()) is not None


def clear_scans():
    for s in store.dexa_scans():
        dexa.remove(s)


# --------------------------------- parser ------------------------------------ #
def trend_lb(rt: dict) -> dict:
    """regional_trend in the fixture's terms: lb masses (as printed) and percentages."""
    return {r: dict(fat=lb(v.get("fat_kg")), fat_pct=v.get("fat_pct"), lean=lb(v.get("lean_kg")), lean_pct=v.get("lean_pct"))
            for r, v in rt.items()}


class ParserOnFixture(unittest.TestCase):
    def test_fixture_is_current(self):
        self.assertEqual(DATA, F.build_pdf(), "run: .venv/bin/python tests/fixtures/synthetic_dexa.py")

    def test_full_report(self):
        s = P.parse(DATA)
        self.assertEqual(s["measured_date"], D0)
        self.assertEqual(s["missing"], [])
        self.assertEqual(s["warnings"], [])
        self.assertEqual((s["sex"], s["age"]), (F.SEX, F.AGE))
        self.assertEqual(comp_lb(s["summary"]), F.SUMMARY[D0])
        self.assertEqual({k: comp_lb(v) for k, v in s["regions"].items()}, F.REGION_COMP)
        self.assertEqual(s["supplemental"], F.SUPPLEMENTAL[0])
        self.assertEqual(lb(s["vat"]["mass_kg"], 2), F.VAT[0]["mass_lb"])
        self.assertEqual(s["vat"]["volume_in3"], F.VAT[0]["volume_in3"])
        self.assertEqual({k: (b["bmd"], b["t"], b["z"]) for k, b in s["bone"].items()}, F.BONE)
        self.assertIsNone(s["bone"]["arms"]["t"])  # "-" in the report
        self.assertEqual({k: comp_lb(v) for k, v in s["balance"].items()}, F.BALANCE)
        self.assertEqual(s["percentile_table"], F.PERCENTILES)
        self.assertEqual(trend_lb(s["regional_trend"]), F.TREND[D0])

    def test_history_tables(self):
        s = P.parse(DATA)
        H = {h["measured_date"]: h for h in s["history"]}
        self.assertEqual(sorted(H), [D2, D1])
        for n, d in ((1, D1), (2, D2)):
            with self.subTest(date=d):
                self.assertEqual(comp_lb(H[d]["summary"]), F.SUMMARY[d])
                self.assertEqual(H[d]["supplemental"], F.SUPPLEMENTAL[n])
                self.assertEqual(lb(H[d]["vat"]["mass_kg"], 2), F.VAT[n]["mass_lb"])
                self.assertEqual(H[d]["vat"]["volume_in3"], F.VAT[n]["volume_in3"])
                self.assertEqual(trend_lb(H[d]["regional_trend"]), F.TREND[d])
                self.assertNotIn("regions", H[d])  # history rows carry only what the trend pages print

    def test_byte_different_copy_is_the_same_scan(self):
        self.assertEqual(P.parse(DATA), P.parse(DATA + b"\n% re-saved copy\n"))

    def test_name_and_birth_date_never_parsed(self):
        data = DATA
        toks = identity_tokens(data)
        self.assertEqual(toks, [*F.NAME, F.BIRTH, "1987-06-12"])  # the helper really finds them
        out = json.dumps(P.parse(data)).lower()
        for t in toks:
            self.assertFalse(mentions(out, t), t)
        self.assertNotIn("dob", out)
        self.assertNotIn("birth", out)

    def test_rejects_non_reports(self):
        with self.assertRaises(P.DexaParseError):
            P.parse(b"not a pdf at all")
        with self.assertRaises(P.DexaParseError):
            P.parse_pages([[], []])
        with self.assertRaises(P.DexaParseError):
            P.parse_pages([[dict(text="Hello", x0=10, x1=40, top=10)]])


# ----------------------------- layout tolerance ------------------------------ #
def W(text, x, top, w=None):
    return dict(text=text, x0=x, x1=x + (w or 6 * len(text)), top=top)


def synthetic_page(dx=0.0, jitter=0.0, swap_cols=False):
    """A small BodySpec-like page; columns can be shifted, rows jittered, and the fat/lean columns swapped."""
    fat_x, lean_x = (400, 330) if swap_cols else (330, 400)
    words = [W("BodySpec", 40, 10), W("SUMMARY", 200 + dx, 40), W("RESULTS", 260 + dx, 40),
             W("Measured", 40 + dx, 60), W("Date", 90 + dx, 60), W("Total", 130 + dx, 60), W("Body", 160 + dx, 60), W("Fat", 190 + dx, 60), W("%", 210 + dx, 60),
             W("Total", 250 + dx, 60), W("Mass", 280 + dx, 60), W("(lbs)", 305 + dx, 60),
             W("Fat", fat_x + dx, 60), W("Tissue", fat_x + 20 + dx, 60), W("Lean", lean_x + dx, 60), W("Tissue", lean_x + 26 + dx, 60),
             W("BMC", 480 + dx, 60)]
    printed = lambda c: (f"{c[0]}%", *(f"{v:.1f}" for v in c[1:]))
    for n, (d, row) in enumerate(((F.DATES[0], printed(F.SUMMARY[D0])), (F.DATES[1], printed(F.SUMMARY[D1])))):
        top = 80 + 14 * n + (jitter if n else -jitter)
        xs = (170, 270, fat_x + 20, lean_x + 20, 485)  # each value sits under its own header, wherever that is
        words += [W(d, 45 + dx, top)] + [W(c, x + dx, top + (0.8 if i % 2 else 0)) for i, (c, x) in enumerate(zip(row, xs))]
    return words


class LayoutTolerance(unittest.TestCase):
    def check(self, words):
        import random
        random.Random(3).shuffle(words)  # word order from the PDF doesn't matter
        s = P.parse_pages([words])
        self.assertEqual(s["measured_date"], D0)
        self.assertEqual(comp_lb(s["summary"]), F.SUMMARY[D0])
        self.assertEqual(comp_lb(s["history"][0]["summary"]), F.SUMMARY[D1])
        self.assertIn("regions", s["missing"])  # a missing section is reported, not fatal

    def test_shifted_columns(self):
        self.check(synthetic_page(dx=37))

    def test_jittered_rows(self):
        self.check(synthetic_page(jitter=1.6))

    def test_columns_matched_by_header_not_order(self):
        self.check(synthetic_page(swap_cols=True))

    def test_lines_group_by_y(self):
        lines = P.lines_of([W("b", 50, 100.9), W("a", 10, 100), W("c", 10, 120)])
        self.assertEqual([ln.text for ln in lines], ["a b", "c"])


# --------------------------------- validation -------------------------------- #
def kg(lb_):
    return lb_ / KG_LB


def comp_kg(c: tuple) -> dict:
    pct, tot, fat, lean, bmc = c
    return dict(fat_pct=pct, total_kg=kg(tot), fat_kg=kg(fat), lean_kg=kg(lean), bmc_kg=kg(bmc))


def good_scan():
    return dict(measured_date=D0, summary=comp_kg(F.SUMMARY[D0]),
                regions={k: comp_kg(F.REGION_COMP[k]) for k in ("arms", "legs", "trunk")},
                supplemental=dict(F.SUPPLEMENTAL[0]),
                balance={k: dict(total_kg=kg(F.BALANCE[k][1])) for k in ("arms", "right_arm", "left_arm")})


class Validation(unittest.TestCase):
    def test_clean_scan_has_no_warnings(self):
        self.assertEqual(P.validate(good_scan()), [])

    def test_percentage_out_of_range(self):
        s = good_scan()
        s["supplemental"]["android_pct"] += 100
        self.assertTrue(any("outside 0–100%" in w for w in P.validate(s)))
        s = good_scan()
        s["summary"]["fat_pct"] = -2
        self.assertTrue(any("outside 0–100%" in w for w in P.validate(s)))

    def test_parts_must_add_up(self):
        s = good_scan()
        s["summary"]["lean_kg"] += kg(10)
        self.assertTrue(any("doesn't match the total" in w for w in P.validate(s)))
        s = good_scan()
        s["balance"]["left_arm"]["total_kg"] += kg(2)
        self.assertTrue(any("Right + left arm" in w for w in P.validate(s)))
        s = good_scan()
        s["regions"]["trunk"]["total_kg"] += kg(100)
        s["regions"]["trunk"]["lean_kg"] += kg(100)
        self.assertTrue(any("more than the total" in w for w in P.validate(s)))

    def test_ag_ratio_checked(self):
        s = good_scan()
        s["supplemental"]["ag_ratio"] = round(1 / s["supplemental"]["ag_ratio"], 2)  # android and gynoid swapped
        self.assertTrue(any("A/G ratio" in w for w in P.validate(s)))

    def test_kg_report_is_flagged(self):
        words = [w if w["text"] != "(lbs)" else dict(w, text="(kg)") for w in synthetic_page()]
        s = P.parse_pages([words])
        self.assertTrue(any("kg, not lb" in w for w in s["warnings"]))
        self.assertAlmostEqual(s["summary"]["total_kg"], F.SUMMARY[D0][1])  # read as kg, not converted twice

    def test_missing_sections_listed(self):
        s = P.parse_pages([synthetic_page()])
        self.assertEqual(set(s["missing"]), {"regions", "supplemental", "vat", "bone", "balance", "percentile_table"})
        self.assertIn("summary", s["found"])


# ------------------------- duplicates, backfill, upgrade --------------------- #
class ImportRules(unittest.TestCase):
    def setUp(self):
        clear_scans()
        self.data = DATA
        self.parsed = P.parse(self.data)

    def tearDown(self):
        clear_scans()

    def save(self, parsed=None, backfill=(D1, D2), data=None):
        parsed = parsed or self.parsed
        tok = dexa.stage(data or self.data, parsed)
        edit = {k: parsed[k] for k in ("summary", "regions", "supplemental", "vat", "bone", "balance") if k in parsed}
        return dexa.save(tok, edit, list(backfill))

    def test_backfill_from_history(self):
        r = self.save()
        self.assertEqual(r["status"], "saved")
        self.assertEqual(r["backfilled"], [D2, D1])
        S = {s["measured_date"]: s for s in store.dexa_scans()}
        self.assertEqual({d: s["source"] for d, s in S.items()}, {D2: "history", D1: "history", D0: "report"})
        h = S[D1]
        self.assertEqual(comp_lb(h["summary"]), F.SUMMARY[D1])
        self.assertIsNone(h["pdf"])
        self.assertNotIn("bone", h)
        self.assertEqual(set(h["missing"]), {"regions", "bone", "balance"})
        self.assertFalse(S[D0]["edited"])
        self.assertTrue(dexa.pdf_path(S[D0]))

    def test_backfill_is_optional(self):
        self.save(backfill=[D1])
        self.assertEqual([s["measured_date"] for s in store.dexa_scans()], [D1, D0])

    def test_same_report_twice_is_a_duplicate(self):
        self.save()
        before = store.dexa_scans()
        plan = dexa.plan_import(self.parsed, {s["measured_date"]: s for s in before})
        self.assertEqual(plan["status"], "duplicate")
        self.assertEqual(plan["backfill"], [])  # history dates are already stored
        r = self.save()
        self.assertEqual(r["status"], "duplicate")
        self.assertEqual(store.dexa_scans(), before)
        self.assertEqual(len(list(dexa.dexa_dir().glob("*.pdf"))), 1)
        self.assertEqual(list(dexa.staging_dir().glob("*")), [])  # the skipped upload isn't left behind

    def test_full_report_upgrades_history_only_scan(self):
        self.save()
        hist = store.dexa_get_day(D1)
        older = {k: v for k, v in self.parsed["history"][0].items()}  # pretend the D1 report itself arrives
        older.update(regions=self.parsed["regions"], bone=self.parsed["bone"], balance=self.parsed["balance"], history=[])
        plan = dexa.plan_import(older, {s["measured_date"]: s for s in store.dexa_scans()})
        self.assertEqual(plan["status"], "upgrade")
        r = self.save(parsed=older, backfill=[])
        self.assertEqual(r["status"], "upgraded")
        new = store.dexa_get_day(D1)
        self.assertEqual((new["id"], new["source"]), (hist["id"], "report"))  # same id, so links keep working
        self.assertIn("bone", new)

    def test_history_never_overwrites_a_report(self):
        older = dict(self.parsed["history"][0], regions=self.parsed["regions"], history=[])
        self.save(parsed=older, backfill=[])
        report_row = store.dexa_get_day(D1)
        self.save()  # the D0 report: its history row for D1 must not replace the stored report
        self.assertEqual(store.dexa_get_day(D1), report_row)
        self.assertEqual(store.dexa_get_day(D2)["source"], "history")

    def test_edits_are_whitelisted_and_marked(self):
        tok = dexa.stage(self.data, self.parsed)
        edit = {k: json.loads(json.dumps(self.parsed[k])) for k in ("summary", "regions")}
        edit["summary"]["lean_kg"] = kg(F.SUMMARY[D0][3] + 1.0)
        edit["summary"]["name"] = "x"
        edit["regions"]["head"] = dict(fat_pct=1)
        edit["notes"] = "y"
        dexa.save(tok, edit, [])
        s = store.dexa_get_day(D0)
        self.assertTrue(s["edited"])
        self.assertEqual(lb(s["summary"]["lean_kg"]), round(F.SUMMARY[D0][3] + 1.0, 1))
        self.assertNotIn("name", s["summary"])
        self.assertNotIn("head", s["regions"])
        self.assertNotIn("notes", s)
        self.assertTrue(any("doesn't match the total" in w for w in s["warnings"]))


# ------------------------------ views + analysis ----------------------------- #
class Views(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        clear_scans()
        p = P.parse(DATA)
        dexa.save(dexa.stage(DATA, p), {k: p[k] for k in ("summary", "regions", "supplemental", "vat", "bone", "balance")}, [D1, D2])
        cls.S = store.dexa_scans()

    @classmethod
    def tearDownClass(cls):
        clear_scans()

    def test_percentile_band_from_report_table(self):
        pb = dexa.percentile_band(self.S[-1])
        self.assertEqual((pb["band"], pb["age_band"], pb["low"], pb["high"], pb["source"]), ("40th–60th", "30-39", 22, 26, "report"))  # 22.5% at 37

    def test_imbalances_use_two_pound_threshold(self):
        imb = {i["limb"]: i for i in dexa.imbalances(self.S[-1])}
        B = F.BALANCE
        self.assertAlmostEqual(lb(imb["arm"]["diff"]["lean_kg"]), round(B["right_arm"][3] - B["left_arm"][3], 1))
        self.assertFalse(imb["arm"]["flag"] or imb["leg"]["flag"])
        s = json.loads(json.dumps(self.S[-1]))
        s["balance"]["left_leg"]["lean_kg"] = kg(B["right_leg"][3] - 3.0)  # 3 lb apart: over the 2 lb threshold
        self.assertEqual(next(i for i in dexa.imbalances(s) if i["limb"] == "leg")["flagged"], ["lean_kg"])

    def test_change_and_lean_loss_flag(self):
        ch = dexa.change(self.S[1], self.S[2])
        self.assertEqual(ch["days"], (F.date.fromisoformat(D0) - F.date.fromisoformat(D1)).days)
        self.assertAlmostEqual(lb(ch["total"]["fat_kg"]["d"]), round(F.SUMMARY[D0][2] - F.SUMMARY[D1][2], 1))
        self.assertAlmostEqual(lb(ch["total"]["lean_kg"]["d"]), round(F.SUMMARY[D0][3] - F.SUMMARY[D1][3], 1))
        self.assertTrue(ch["flags"]["lean_loss_with_fat_loss"])
        self.assertAlmostEqual(lb(ch["regions"]["trunk"]["fat_kg"]["d"]), round(F.REGION_COMP["trunk"][2] - F.TREND[D1]["trunk"]["fat"], 1))
        base = dexa.change(self.S[0], self.S[2])
        self.assertFalse(base["flags"]["lean_loss_with_fat_loss"])
        self.assertFalse(base["flags"]["lean_change_meaningful"])

    def test_trend_and_projection(self):
        tr = dexa.trend(self.S)
        self.assertEqual(tr["scans"], 3)
        self.assertLess(tr["overall"]["fat_kg"], 0)
        pr = dexa.projection(self.S, [dict(type="body_fat", target=18.0, deadline="2027-06-01"), dict(type="lean_mass", target=kg(150))])
        bf, lean = pr
        self.assertGreater(bf["months"], 0)
        self.assertLessEqual(bf["months_low"], bf["months"])
        self.assertIsNone(lean["months"])  # lean isn't trending toward it: no date promised

    def test_goal_metrics_use_latest_scan(self):
        m = dexa.goal_metrics(self.S)
        self.assertEqual(m["body_fat"]["current"], F.SUMMARY[D0][0])
        self.assertEqual(m["body_fat"]["source"], SOURCE)
        self.assertAlmostEqual(lb(m["lean_mass"]["current"]), F.SUMMARY[D0][3])
        self.assertEqual(dexa.goal_metrics([]), {})

    def test_rule_analysis_is_hedged(self):
        p = dexa.coach_payload(self.S)
        for system in ("us", "metric"):
            t = dexa.rule_analysis(p, system)
            self.assertIn("Lean mass dropped along with fat", t)
            self.assertIn("not a bone density exam", t)
            self.assertIn("hydration", t)
            self.assertIn("not medical advice", t)
            self.assertIn("tend", t)
        self.assertIn("lb", dexa.rule_analysis(p, "us"))
        self.assertIn("kg", dexa.rule_analysis(p, "metric"))
        self.assertIn("months", dexa.rule_trend(dexa.coach_payload(self.S, goal_list=[dict(type="body_fat", target=18.0)])))

    def test_coach_payload_is_derived_numbers_only(self):
        toks = identity_tokens(DATA)
        p = json.dumps([dexa.coach_payload(self.S), dexa.brief_payload(self.S)]).lower()
        for t in toks:
            self.assertFalse(mentions(p, t), t)
        for k in ("pdf", "sha256", "file_", "from_scan", '"age"', '"sex"', "percentile_table"):
            self.assertNotIn(k, p)


# ------------------------------------ API ------------------------------------ #
class Api(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.env = mock.patch.dict(os.environ, ENV)
        cls.env.start()
        import server
        from fastapi.testclient import TestClient
        cls.server = server
        cls.cache = mock.patch.object(server, "CACHE", Path(tempfile.mkdtemp(prefix="coach-cache-")))
        cls.cache.start()
        cls.c = TestClient(server.app, base_url=server.LOCAL_URL, headers={"Origin": server.LOCAL_URL})
        cls.logs = io.StringIO()
        cls.handler = logging.StreamHandler(cls.logs)
        server.log.addHandler(cls.handler)

    @classmethod
    def tearDownClass(cls):
        cls.server.log.removeHandler(cls.handler)
        cls.cache.stop()
        cls.env.stop()
        clear_scans()

    def setUp(self):
        clear_scans()

    def upload(self, *names):
        files = [("files", (n, DATA, "application/pdf")) for n in names or (FIXTURE.name,)]
        r = self.c.post("/api/dexa/parse", files=files)
        self.assertEqual(r.status_code, 200)
        return r.json()["results"]

    def confirm(self, res, backfill=None):
        b = [x["measured_date"] for x in res["backfill"]] if backfill is None else backfill
        r = self.c.post("/api/dexa/confirm", json=dict(token=res["token"], scan=res["scan"], backfill=b))
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()

    def test_same_report_twice_in_one_upload(self):
        res = self.upload(FIXTURE.name, f"{FIXTURE.stem} copy.pdf")
        self.assertEqual(res[0]["status"], "new")
        self.assertEqual(len(res[0]["backfill"]), 2)
        for dup in res[1:]:  # the identical copy is skipped and says so
            self.assertEqual(dup["status"], "duplicate")
            self.assertIn("Same scan", dup["message"])
            self.assertIsNone(dup["token"])
        self.assertEqual(self.confirm(res[0])["backfilled"], [D2, D1])
        again = self.upload()[0]
        self.assertEqual(again["status"], "duplicate")
        self.assertIn("already saved", again["message"])
        self.assertEqual(len(store.dexa_scans()), 3)

    def test_round_trip_calendar_compare_delete_export(self):
        sid = self.confirm(self.upload()[0])["id"]
        L = self.c.get("/api/dexa").json()
        self.assertEqual([s["source"] for s in L["scans"]], ["history", "history", "report"])
        first = L["scans"][0]["id"]
        self.assertEqual(self.c.get(f"/api/calendar?month={D0[:7]}").json()["dexa"], {D0: dict(id=sid, source="report")})
        day = self.c.get(f"/api/calendar/day/{D0}").json()["dexa"]
        self.assertEqual(day["previous"], D1)
        self.assertAlmostEqual(lb(day["change"]["lean_kg"]), round(F.SUMMARY[D0][3] - F.SUMMARY[D1][3], 1))
        d = self.c.get(f"/api/dexa/{sid}").json()
        self.assertEqual(d["previous"]["measured_date"], D1)
        self.assertIsNotNone(d["change_baseline"])
        cmp = self.c.get(f"/api/dexa/compare?a={sid}&b={first}").json()  # order doesn't matter
        self.assertEqual((cmp["a"]["measured_date"], cmp["b"]["measured_date"]), (D2, D0))
        pdf = self.c.get(f"/api/dexa/{sid}/pdf")
        self.assertEqual((pdf.status_code, pdf.headers["content-type"]), (200, "application/pdf"))
        self.assertTrue(pdf.headers["content-disposition"].startswith("inline"))
        self.assertEqual(self.c.get(f"/api/dexa/{first}/pdf").status_code, 404)  # history scans have no PDF
        self.assertEqual(self.c.get("/api/dexa/../../etc/passwd").status_code, 404)
        an = self.c.get(f"/api/dexa/{sid}/analysis").json()
        self.assertEqual(an["source"], "rules")
        self.assertIn("not a bone density exam", an["text"])
        self.assertEqual(self.c.get("/api/dexa/trend/analysis").json()["source"], "rules")
        j = self.c.get("/api/export")  # the download form; save=true would copy it into ~/Downloads
        self.assertEqual(j.status_code, 200)
        with zipfile.ZipFile(io.BytesIO(j.content)) as z:
            meta = json.loads(z.read("metadata.json"))
            self.assertEqual(len(meta["dexa_scans"]), 3)
            self.assertIn(meta["dexa_scans"][-1]["pdf"], z.namelist())
            self.assertEqual(z.read(meta["dexa_scans"][-1]["pdf"]), DATA)
        p = dexa.pdf_path(store.dexa_get(sid))
        self.assertEqual(self.c.delete(f"/api/dexa/{sid}").status_code, 200)
        self.assertFalse(p.exists())
        self.assertIsNone(store.dexa_get(sid))
        self.assertEqual(self.c.get(f"/api/dexa/{sid}").status_code, 404)

    def test_patch_corrects_values(self):
        sid = self.confirm(self.upload()[0], backfill=[])["id"]
        x = self.c.get(f"/api/dexa/{sid}").json()["scan"]
        x["summary"]["fat_pct"] = F.SUMMARY[D0][0] + 0.1
        r = self.c.patch(f"/api/dexa/{sid}", json=dict(scan=x)).json()
        self.assertEqual(r["summary"]["fat_pct"], F.SUMMARY[D0][0] + 0.1)
        self.assertTrue(r["edited"])

    def test_bad_files_are_reported_not_saved(self):
        r = self.c.post("/api/dexa/parse", files=[("files", ("notes.txt", b"hello", "text/plain")),
                                                 ("files", ("fake.pdf", b"%PDF-1.4 broken", "application/pdf"))]).json()["results"]
        self.assertEqual([x["ok"] for x in r], [False, False])
        self.assertIn("Only PDF", r[0]["error"])
        self.assertEqual(store.dexa_scans(), [])
        self.assertEqual(self.c.post("/api/dexa/confirm", json=dict(token="0" * 32)).status_code, 410)

    def test_goals_name_their_source(self):
        g = self.c.post("/api/goals", json=dict(type="body_fat", target=18, deadline="2027-06-01")).json()
        gid = g["goals"][-1]["goal"]["id"]
        try:
            v = next(x for x in self.c.get("/api/goals").json()["goals"] if x["goal"]["id"] == gid)
            self.assertIsNone(v["current"])  # demo mode, no scans: no source yet
            self.confirm(self.upload()[0])
            v = next(x for x in self.c.get("/api/goals").json()["goals"] if x["goal"]["id"] == gid)
            self.assertEqual((v["current"], v["source"]), (F.SUMMARY[D0][0], SOURCE))
            self.assertIsNotNone(v["actual_per_week"])
        finally:
            self.c.delete(f"/api/goals/{gid}")

    def test_privacy_end_to_end(self):
        toks = identity_tokens(DATA)
        res = self.upload()
        sid = self.confirm(res[0])["id"]
        blobs = [json.dumps(res), json.dumps(store.dexa_scans()), self.c.get("/api/dexa").text, self.c.get(f"/api/dexa/{sid}").text,
                 self.c.get(f"/api/calendar/day/{D0}").text, json.dumps(self.server.coach_profile(), default=str),
                 self.c.get(f"/api/dexa/{sid}/analysis").text, self.logs.getvalue()]
        con = store.db()
        try:
            blobs += [json.dumps([dict(r) for r in con.execute("SELECT * FROM dexa_scans")])]
        finally:
            con.close()
        for blob in blobs:
            for t in toks:
                self.assertFalse(mentions(blob, t), t)


# ------------------------- your own reports (optional) ----------------------- #
def _close(a, b, tol):
    return a is not None and b is not None and abs(a - b) <= tol


@unittest.skipUnless(SAMPLES, "no reports in samples/ (your own BodySpec PDFs, git-ignored); the fixture covers the parser")
class SamplesStructure(unittest.TestCase):
    """Your own reports parse into a complete, self-consistent scan. Only structure, sums and ranges
    are checked: no value from any report is written here, and failure messages never include one."""

    def comp(self, where: str, r: dict, tol_lb: float = 0.5):
        self.assertTrue({"fat_pct", "total_kg", "fat_kg", "lean_kg", "bmc_kg"} <= set(r), f"{where}: columns missing")
        self.assertTrue(0 < r["fat_pct"] < 100, f"{where}: body fat % out of range")
        self.assertTrue(all(r[k] > 0 for k in ("total_kg", "fat_kg", "lean_kg", "bmc_kg")), f"{where}: a mass isn't positive")
        self.assertTrue(_close(lb(r["fat_kg"] + r["lean_kg"] + r["bmc_kg"], 2), lb(r["total_kg"], 2), tol_lb), f"{where}: parts don't sum to total")
        self.assertTrue(_close(r["fat_kg"] / r["total_kg"] * 100, r["fat_pct"], 1.0), f"{where}: fat % isn't fat / total")

    def test_each_report(self):
        for path in SAMPLES:
            with self.subTest(sample=path.name):
                data = path.read_bytes()
                s = P.parse(data)
                self.assertTrue(s["missing"] == [], "some sections weren't found")
                self.assertTrue(s["warnings"] == [] and P.validate(s) == [], "the parser or validator raised warnings")
                self.assertIn(s["sex"], ("male", "female"))
                self.assertTrue(s["age"] is None or 10 <= s["age"] <= 110, "age out of range")
                self.comp("summary", s["summary"])
                R, tot = s["regions"], s["summary"]["total_kg"]
                self.assertTrue(set(F.REGIONS) <= set(R), "regions missing")
                for k, r in R.items():
                    self.comp(f"region {k}", r)
                self.assertTrue(0.85 * tot <= sum(R[k]["total_kg"] for k in ("arms", "legs", "trunk")) <= 1.01 * tot,
                                "arms + legs + trunk vs total")
                for k in ("total_kg", "fat_kg", "lean_kg"):
                    self.assertTrue(_close(lb(R["total"][k]), lb(s["summary"][k]), 0.3), f"summary vs regional total ({k})")
                B = s["balance"]
                for limb in ("arm", "leg"):
                    self.comp(f"right {limb}", B[f"right_{limb}"], 0.3)
                    self.comp(f"left {limb}", B[f"left_{limb}"], 0.3)
                    self.assertTrue(_close(lb(B[f"right_{limb}"]["total_kg"] + B[f"left_{limb}"]["total_kg"]), lb(B[f"{limb}s"]["total_kg"]), 0.3),
                                    f"right + left {limb} vs {limb}s total")
                sp = s["supplemental"]
                self.assertTrue(800 <= sp["rmr_kcal"] <= 5000, "RMR out of range")
                self.assertTrue(0 < sp["android_pct"] < 100 and 0 < sp["gynoid_pct"] < 100, "android/gynoid out of range")
                self.assertTrue(_close(sp["android_pct"] / sp["gynoid_pct"], sp["ag_ratio"], 0.03), "A/G ratio isn't android / gynoid")
                self.assertTrue(0 <= lb(s["vat"]["mass_kg"]) <= 20 and s["vat"]["volume_in3"] >= 0, "VAT out of range")
                self.assertIn("total", s["bone"])
                self.assertTrue(all(0.3 <= b["bmd"] <= 3.5 for b in s["bone"].values()), "BMD out of range")
                for sex, rows in s["percentile_table"].items():
                    self.assertIn(sex, ("male", "female"))
                    self.assertTrue(all(len(c) == 4 and c == sorted(c) for c in rows.values()), "percentile cut-offs")
                for h in s.get("history", []):
                    self.assertTrue(h["measured_date"] < s["measured_date"], "a history row isn't earlier than the scan")
                    self.comp("history row", h["summary"])
                out = json.dumps(s).lower()
                leaked = sum(mentions(out, t) for t in identity_tokens(data))
                self.assertTrue(leaked == 0, "the client's name or birth date reached the parsed scan")


if __name__ == "__main__":
    unittest.main()
