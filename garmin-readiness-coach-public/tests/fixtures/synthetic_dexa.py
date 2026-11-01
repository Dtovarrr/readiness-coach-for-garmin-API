"""A synthetic BodySpec-style DEXA report for the parser tests. Every value here is invented.

The client is "Test Person" (born 6/12/1987); the scans are dated 5/20/2025, with 1/15/2025 and
8/8/2024 as history rows. The layout follows a real BodySpec report's structure (7 US-letter
pages, the same section headings, column order and row labels, roughly the same positions) so the
parser is exercised the way a real report would exercise it, but no real person's data is in it.

The numbers are chosen to be internally consistent: fat + lean + BMC = total mass in every row,
fat % = fat / total, arms + legs + trunk < total, right + left = limb total, A/G = android / gynoid.
They also cover the analysis rules: between the two latest scans lean mass drops along with fat,
the first-to-last lean change is within DEXA noise, and the lean trend is falling.

    python tests/fixtures/synthetic_dexa.py      # rewrites tests/fixtures/synthetic_bodyspec.pdf

The PDF is written by hand (Helvetica text objects, no extra dependencies) and is byte-for-byte
deterministic; tests/test_dexa.py checks the committed copy matches this file.
"""
from __future__ import annotations

from datetime import date
from pathlib import Path

PDF_PATH = Path(__file__).with_name("synthetic_bodyspec.pdf")

NAME = ("Test", "Person")
BIRTH = "6/12/1987"
SEX = "male"
PHONE = "555.010.0199"  # 555-01xx: reserved for fiction
DATES = ("5/20/2025", "1/15/2025", "8/8/2024")  # newest first, like the report
ISO = tuple(date(int(y), int(m), int(d)).isoformat() for m, d, y in (s.split("/") for s in DATES))
MEASURED = ISO[0]
AGE = 37

COMP = ("fat_pct", "total", "fat", "lean", "bmc")  # report columns; masses in lb
REGIONS = ("arms", "legs", "trunk", "android", "gynoid", "total")


def comp(fat: float, lean: float, bmc: float) -> tuple:
    """(fat %, total, fat, lean, bmc) with total and % derived, rounded the way the report prints them."""
    total = round(fat + lean + bmc, 1)
    return round(fat / total * 100, 1), total, fat, lean, bmc


# Whole-body summary per scan date (newest first).
SUMMARY = {ISO[0]: comp(41.0, 133.2, 7.8), ISO[1]: comp(46.0, 134.6, 7.7), ISO[2]: comp(52.0, 133.8, 7.6)}
# Latest scan, by region. Arms + legs + trunk = 170.0 of 182.0 lb (the head is the rest).
REGION_COMP = {"arms": comp(4.6, 16.4, 1.0), "legs": comp(13.2, 46.0, 2.8), "trunk": comp(20.4, 63.2, 2.4),
               "android": comp(3.2, 9.1, 0.1), "gynoid": comp(6.3, 21.0, 0.7), "total": SUMMARY[ISO[0]]}
BALANCE = {"arms": REGION_COMP["arms"], "right_arm": comp(2.3, 8.4, 0.5), "left_arm": comp(2.3, 8.0, 0.5),
           "legs": REGION_COMP["legs"], "right_leg": comp(6.5, 23.4, 1.4), "left_leg": comp(6.7, 22.6, 1.4)}
# Newest first: RMR cal/day, android %, gynoid %, A/G ratio.
SUPPLEMENTAL = [dict(rmr_kcal=1810, android_pct=25.8, gynoid_pct=22.5, ag_ratio=1.15),
                dict(rmr_kcal=1795, android_pct=29.1, gynoid_pct=24.6, ag_ratio=1.18),
                dict(rmr_kcal=1770, android_pct=32.4, gynoid_pct=26.8, ag_ratio=1.21)]
VAT = [dict(mass_lb=0.95, volume_in3=28.40), dict(mass_lb=1.42, volume_in3=41.20), dict(mass_lb=1.95, volume_in3=56.80)]
BONE = {"head": (2.210, None, None), "arms": (0.905, None, None), "legs": (1.302, None, None), "trunk": (1.050, None, None),
        "ribs": (0.811, None, None), "spine": (1.188, None, None), "pelvis": (1.240, None, None), "total": (1.265, 1.1, 0.9)}
# BodySpec's published body-fat percentile chart (20th/40th/60th/80th cut-offs), not personal data.
PERCENTILES = {
    "female": {"20-29": [24, 28, 32, 37], "30-39": [25, 29, 33, 38], "40-49": [26, 31, 35, 39], "50-59": [27, 32, 37, 42], ">60": [30, 33, 38, 42]},
    "male": {"20-29": [16, 20, 24, 27], "30-39": [18, 22, 26, 30], "40-49": [20, 24, 27, 31], "50-59": [21, 25, 29, 33], ">60": [21, 25, 30, 33]},
}


def _trend() -> dict:
    """Regional fat/lean mass and % per scan date, for the REGIONAL FAT / LEAN TISSUE pages. Earlier
    scans scale the latest scan's regions by the whole-body fat and lean ratios."""
    out = {}
    for d in ISO:
        s = SUMMARY[d]
        rf, rl = s[2] / SUMMARY[ISO[0]][2], s[3] / SUMMARY[ISO[0]][3]
        out[d] = {}
        for r in REGIONS:
            _, _, f, l, b = REGION_COMP[r]
            f, l = (s[2], s[3]) if r == "total" else (round(f * rf, 1), round(l * rl, 1))
            t = s[1] if r == "total" else f + l + b
            out[d][r] = dict(fat=f, fat_pct=round(f / t * 100, 1), lean=l, lean_pct=round(l / t * 100, 1))
    return out


TREND = _trend()


# ------------------------------------ PDF ------------------------------------ #
W, H = 612, 792


def width(text: str, size: float, bold: bool = False) -> float:
    from pdfminer.fontmetrics import FONT_METRICS  # the metrics pdfplumber measures these fonts with
    widths = FONT_METRICS["Helvetica-Bold" if bold else "Helvetica"][1]
    return sum(widths.get(ch, 556) for ch in text) * size / 1000


def run(x: float, sentence: str, size: float, bold: bool = False) -> list[tuple[float, str]]:
    """(x, word) for each word of a phrase starting at x, spaced the way the font sets them."""
    items = []
    for w in sentence.split():
        items.append((x, w))
        x += width(w, size, bold) + size * 0.4
    return items


class Page:
    def __init__(self):
        self.ops: list[str] = []

    def text(self, top: float, items, size: float = 8, bold: bool = False):
        """One line of words: items are (x, text). `top` is pdfplumber's top (points from the page top).
        Words closer than pdfplumber's x tolerance would merge into one, so that's an error here."""
        y = H - top - size * 0.72  # baseline that puts the glyph tops at about `top`
        ordered = sorted(items, key=lambda it: it[0])
        for (xa, a), (xb, b) in zip(ordered, ordered[1:]):
            if xa + width(str(a), size, bold) + 2 > xb:
                raise ValueError(f"words overlap at top={top}: {a!r}@{xa} and {b!r}@{xb}")
        for x, t in items:
            s = str(t).replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
            self.ops.append(f"BT /{'F2' if bold else 'F1'} {size:g} Tf 1 0 0 1 {x:.2f} {y:.2f} Tm ({s}) Tj ET")

    def words(self, top: float, x: float, sentence: str, size: float = 7, bold: bool = False):
        """Running text, one word per text object so spacing is explicit."""
        self.text(top, run(x, sentence, size, bold), size, bold)


def _f(v: float | None, d: int = 1, pct: bool = False, sign: bool = False) -> str:
    if v is None:
        return "-"
    s = f"{v:+.{d}f}" if sign else f"{v:,.{d}f}"
    return s.replace("+", "") + ("%" if pct else "")


def _header(p: Page):
    p.text(30.5, [(472, "BodySpec")])
    p.text(44.2, [(480, "Phone"), (512, PHONE)])
    p.text(57.1, [(454, NAME[0]), (476, NAME[1])])
    p.text(90.4, [(36, "Client"), (206, "Sex"), (258, "Birth"), (283, "Date"), (326, "Intake"), (356, "Height"),
                  (404, "Intake"), (434, "Weight"), (494, "Measured"), (539, "Date")], bold=True)
    p.text(101.8, [(36, NAME[0]), (58, NAME[1]), (204, SEX.capitalize()), (258, BIRTH), (335, "64.3"), (355, "in."),
                   (410, "171.6"), (436, "lbs"), (505, DATES[0])])


def _footer(p: Page, n: int):
    p.text(754.0, [(449, "bodyspec.com"), (520, "Page"), (540, str(n))], size=7)


def _cover() -> Page:
    p = Page()
    p.words(120.4, 220, "BodySpec Results", 20, bold=True)
    p.words(252.3, 93, "Synthetic test report for the parser: this is the start of the report, not a real person's scan.", 9)
    p.words(430.2, 107, "DEXA measures body composition: bone, fat and lean tissue.", 9)
    p.words(451.1, 110, "All numbers in this document are invented for software testing.", 9)
    p.words(531.3, 110, "Read the summary first, then the detailed tables on the following pages.", 9)
    return p


def _summary_page() -> Page:
    p = Page()
    _header(p)
    p.words(131.1, 235, "SUMMARY RESULTS", 12, bold=True)
    p.words(146.9, 36, "This table gives an overview of your total body composition, broken into total body fat %, total mass, fat and lean")
    p.words(158.9, 36, "tissue, and bone mineral content. Results from future BodySpec scans will be added here.")
    p.text(188.8, [(499, "Bone"), (525, "Mineral")], bold=True)
    p.text(195.2, [(46, "Measured"), (92, "Date"), (130, "Total"), (156, "Body"), (183, "Fat"), (200, "%"), (223, "Total"),
                   (249, "Mass"), (276, "(lbs)"), (315, "Fat"), (332, "Tissue"), (364, "(lbs)"), (401, "Lean"), (426, "Tissue"),
                   (458, "(lbs)")], bold=True)
    p.text(201.8, [(495, "Content"), (532, "(BMC)")], bold=True)
    for n, (label, d) in enumerate(zip(DATES, ISO)):
        pct, tot, fat, lean, bmc = SUMMARY[d]
        p.text(215.6 + 13.3 * n, [(57, label), (154, _f(pct, pct=True)), (247, _f(tot)), (340, _f(fat)), (427, _f(lean)), (523, _f(bmc))])
    p.text(275.2, run(36, "Total Body Tissue Quantification", 11, True) + run(294, "Body Fat Percentile Chart", 11, True),
           size=11, bold=True)
    p.words(292.2, 171, "This table shows where your body fat percentage falls, based on your scan and age group.")
    p.text(343.3, [(194, "Age")])
    p.text(350.7, [(249, "Percentile"), (315, "Percentile"), (383, "Percentile"), (450, "Percentile"), (517, "Percentile")], size=7)
    for sex, label, top, first in (("female", "WOMEN", 323.8, 362.8), ("male", "MEN", 422.2, 434.4)):
        p.text(top, [(350, label)], bold=True)
        for n, (band, c) in enumerate(PERCENTILES[sex].items()):
            pc = [f"{v}%" for v in c]
            p.text(first + 11.7 * n, [(192, band), (257, "<"), (265, pc[0]), (315, pc[0]), (336, "-"), (341, pc[1]), (382, pc[1]),
                                      (403, "-"), (409, pc[2]), (449, pc[2]), (470, "-"), (476, pc[3]), (525, ">"), (533, pc[3])])
    p.words(506.2, 260, "REGIONAL ASSESSMENT", 12, bold=True)
    p.words(522.0, 192, "The table below breaks your body into key regions and shows the fat, lean and bone in each.")
    p.words(533.7, 192, "Arms and legs include both sides; see the balance table for left versus right.")
    p.text(577.8, [(244, "Total"), (267, "Region"), (315, "Total"), (339, "Mass"), (444, "Lean"), (466, "Tissue"), (507, "Bone"),
                   (531, "Mineral")], bold=True)
    p.text(583.6, [(199, "Region"), (373, "Fat"), (389, "Tissue"), (417, "(lbs)")], bold=True)
    p.text(589.5, [(258, "Fat"), (273, "%"), (328, "(lbs)"), (459, "(lbs)"), (504, "Content"), (538, "(BMC)")], bold=True)
    for n, r in enumerate(REGIONS):
        pct, tot, fat, lean, bmc = REGION_COMP[r]
        p.text(608.5 + 12.95 * n, [(200, r.capitalize()), (256, _f(pct, pct=True)), (328, _f(tot)), (398, _f(fat)), (458, _f(lean)),
                                   (528, _f(bmc))])
    _footer(p, 2)
    return p


def _details_page() -> Page:
    p = Page()
    _header(p)
    p.words(126.3, 215, "SUPPLEMENTAL RESULTS", 12, bold=True)
    p.words(149.5, 38, "Resting Metabolic Rate", 8, bold=True)
    p.text(155.9, [(196, "Android"), (239, "(A)"), (334, "Gynoid"), (372, "(G)"), (481, "A/G"), (503, "Ratio")], bold=True)
    p.text(162.4, [(76, "(RMR)")], bold=True)
    p.words(175.8, 43, "The estimated number of calories your body burns at rest, and the share of fat around the waist and hips.")
    for n, s in enumerate(SUPPLEMENTAL):
        p.text(227.6 + 13.05 * n, [(62, f"{s['rmr_kcal']:,}"), (90, "cal/day"), (210, _f(s["android_pct"], pct=True)),
                                   (346, _f(s["gynoid_pct"], pct=True)), (495, _f(s["ag_ratio"], 2))])
    p.text(280.2, run(114, "VAT", 12, True) + run(360, "BONE REPORT", 12, True), size=12, bold=True)
    p.words(295.8, 38, "VAT or Visceral Adipose Tissue is a type of fat stored around the organs.")
    p.words(307.5, 254, "This report shows a measure of bone mineral density by region.")
    p.words(330.9, 254, "Note: this is not a bone density exam, which is a separate clinical assessment.")
    p.text(395.7, [(360, "Young"), (390, "Adult"), (513, "%")], size=7)
    p.text(400.3, [(40, "Mass"), (65, "(lbs)"), (102, _f(VAT[0]["mass_lb"], 2)), (324, "BMD"), (392, "T-Score"), (438, "Z-Score")], bold=True)
    # VAT (left) and bone (right) rows interleave but never share a line: >= 4 pt apart.
    p.text(412.0, [(102, _f(VAT[1]["mass_lb"], 2))])
    p.text(422.0, [(102, _f(VAT[2]["mass_lb"], 2))])
    for n, (r, (bmd, t, z)) in enumerate(BONE.items()):
        p.text(428.4 + 11.5 * n, [(273, r.capitalize()), (324, _f(bmd, 3)), (382 if t is None else 374, _f(t)),
                                  (438 if z is None else 430, _f(z))])
    p.text(445.6, [(40, "Volume"), (76, "(in3)"), (102, _f(VAT[0]["volume_in3"], 2))])
    p.text(457.1, [(102, _f(VAT[1]["volume_in3"], 2))])
    p.text(468.6, [(102, _f(VAT[2]["volume_in3"], 2))])
    p.text(518.0, [(40, "Visceral"), (78, "Adipose"), (114, "Tissue")], size=7)
    p.words(550.6, 208, "MUSCLE BALANCE REPORT", 12, bold=True)
    p.words(566.1, 36, "The table below looks at your limbs and compares the fat and lean tissue on the right and left sides.")
    p.words(577.8, 36, "Differences of more than two pounds may point to a muscle imbalance worth training.")
    p.text(605.5, [(64, "Region"), (155, "%"), (167, "Fat"), (235, "Total"), (261, "Mass"), (329, "Fat"), (346, "Mass"), (415, "Lean"),
                   (440, "Mass"), (517, "BMC")], bold=True)
    labels = {"arms": ("Arms", "Total"), "right_arm": ("Right", "Arm"), "left_arm": ("Left", "Arm"),
              "legs": ("Legs", "Total"), "right_leg": ("Right", "Leg"), "left_leg": ("Left", "Leg")}
    for n, (k, (a, b)) in enumerate(labels.items()):
        pct, tot, fat, lean, bmc = BALANCE[k]
        p.text(618.4 + 13.04 * n, [(54, a), (82, b), (162, _f(pct)), (252, _f(tot)), (344, _f(fat)), (432, _f(lean)), (524, _f(bmc))])
    _footer(p, 3)
    return p


def _trend_page(kind: str, regions: tuple[str, ...], n: int, continued: bool) -> Page:
    p = Page()
    _header(p)
    p.words(126.2, 140, f"REGIONAL {kind.upper()} TISSUE REPORT" + (" (CONTINUED)" if continued else ""), 12, bold=True)
    if not continued:
        p.words(155.0, 36, f"The charts below show how {kind} tissue in different regions of your body has changed over time.")
        p.words(168.1, 36, f"Each region has its own table, with the change since your previous and your first scan.")
    top = 149.4 if continued else 209.6
    mass, pct = f"{kind}", f"{kind}_pct"
    for r in regions:
        p.text(top, [(36, r.capitalize())], size=11, bold=True)
        p.text(top + 20.5, [(453, "Change"), (483, "vs."), (521, "Change"), (551, "vs.")], size=7)
        p.text(top + 31.1, run(300, f"Measured Date Region {kind.capitalize()} Totals", 7, True) + [(458, "Previous"), (525, "Baseline")],
               size=7, bold=True)
        for unit, key, base, row_lbl in (("lbs", mass, top + 42.4, (f"{kind.capitalize()}", "Mass", "(lbs)")),
                                         ("%", pct, top + 85.0, (f"{kind.capitalize()}", None, "(%)"))):
            vals = [TREND[d][r][key] for d in ISO]
            for i, (label, v) in enumerate(zip(DATES, vals)):
                y = base + 10.6 * i
                items = []
                if i == 1:
                    items += run(232, " ".join(row_lbl), 7) if row_lbl[1] else [(244, "Region")]
                elif i == 2 and not row_lbl[1]:
                    items += [(258, row_lbl[2])]
                elif i == 0 and not row_lbl[1]:
                    items += [(258, row_lbl[0])]
                items += [(324, label), (390, _f(v, pct=unit == "%"))] + ([(413, "lbs")] if unit == "lbs" else [])
                if i < 2:
                    prev, first = vals[i + 1] - v, vals[2] - v
                    items += [(452, _f(-prev, sign=True, pct=unit == "%"))] + ([(476, "lbs")] if unit == "lbs" else [])
                    items += [(512, _f(-first, sign=True, pct=unit == "%"))] + ([(540, "lbs")] if unit == "lbs" else [])
                else:
                    items += [(459, "Baseline"), (539, "-")]
                p.text(y, items, size=7)
        top += 172.0
    if n == 7:
        p.words(667.5, 36, "BodySpec is not a medical provider. The data in this report is for informational purposes only.")
        p.words(677.0, 36, "This synthetic fixture is for software tests and describes no real person.")
    _footer(p, n)
    return p


def pages() -> list[Page]:
    return [_cover(), _summary_page(), _details_page(),
            _trend_page("fat", ("arms", "legs", "trunk"), 4, False), _trend_page("fat", ("android", "gynoid", "total"), 5, True),
            _trend_page("lean", ("arms", "legs", "trunk"), 6, False), _trend_page("lean", ("android", "gynoid", "total"), 7, True)]


def build_pdf() -> bytes:
    """A minimal PDF 1.4: standard Helvetica fonts (no embedding), one content stream per page."""
    objs: list[bytes] = []
    pg = pages()
    n_pages = len(pg)
    # 1 catalog, 2 pages, 3 F1, 4 F2, 5 info, then (page, content) pairs
    kids = " ".join(f"{6 + 2 * i} 0 R" for i in range(n_pages))
    objs.append(b"<< /Type /Catalog /Pages 2 0 R >>")
    objs.append(f"<< /Type /Pages /Kids [{kids}] /Count {n_pages} >>".encode())
    objs.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>")
    objs.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold /Encoding /WinAnsiEncoding >>")
    objs.append(b"<< /Title (Synthetic BodySpec-style DEXA report) /Producer (tests/fixtures/synthetic_dexa.py) >>")
    for i, p in enumerate(pg):
        stream = "\n".join(p.ops).encode("latin-1")
        objs.append(f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {W} {H}] /Resources << /Font << /F1 3 0 R /F2 4 0 R >> >> "
                    f"/Contents {7 + 2 * i} 0 R >>".encode())
        objs.append(b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream")
    out, offsets = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n"), []
    for n, body in enumerate(objs, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % n + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1)
    out += b"".join(b"%010d 00000 n \n" % o for o in offsets)
    out += b"trailer\n<< /Size %d /Root 1 0 R /Info 5 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objs) + 1, xref)
    return bytes(out)


if __name__ == "__main__":
    PDF_PATH.write_bytes(build_pdf())
    print(f"wrote {PDF_PATH} ({PDF_PATH.stat().st_size} bytes)")
