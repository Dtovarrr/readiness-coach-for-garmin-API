"""BodySpec DEXA report parser: PDF text layer -> scan dict. Deterministic; no AI.

Words come from pdfplumber with their x/y boxes, are grouped into lines by y, and every table is
found by its section heading and row labels, with columns matched to header phrases by x position
(falling back to the report's column order). Nothing depends on page numbers or fixed coordinates.

Privacy: the "Client ..." header row and the row of values under it (name, sex, birth date, height,
weight, measured date) are read first and then dropped. Only the sex and the age at each scan date
are kept (for the report's age/sex body-fat table); the name and birth date never leave this module,
not even in warnings or exception text.

Everything is returned in metric: report pounds are converted to kg without rounding, so values
convert back to the report's 0.1 lb exactly. VAT volume stays in cubic inches (the report has no
metric figure).
"""
from __future__ import annotations

import io
import re
from datetime import date

from units import KG_LB

DATE_RE = re.compile(r"^(\d{1,2})/(\d{1,2})/(\d{4})$")
NUM_RE = re.compile(r"^[<>]?[-+]?\(?-?\d[\d,]*\.?\d*\)?%?$")
REGIONS = ("arms", "legs", "trunk", "android", "gynoid", "total")
BONE_REGIONS = ("head", "arms", "legs", "trunk", "ribs", "spine", "pelvis", "total")
LIMBS = {("right", "arm"): "right_arm", ("left", "arm"): "left_arm", ("right", "leg"): "right_leg", ("left", "leg"): "left_leg",
         ("arms", "total"): "arms", ("legs", "total"): "legs"}
COMP = ("fat_pct", "total_kg", "fat_kg", "lean_kg", "bmc_kg")  # the report's column order, used as a fallback
COMP_ANCHORS = {"fat_pct": [("body", "fat", "%"), ("fat", "%"), ("%", "fat"), ("%",)],
                "total_kg": [("total", "mass")], "fat_kg": [("fat", "tissue"), ("fat", "mass")],
                "lean_kg": [("lean", "tissue"), ("lean", "mass")], "bmc_kg": [("bmc",), ("(bmc)",), ("bone", "mineral")]}
SUPP = ("rmr_kcal", "android_pct", "gynoid_pct", "ag_ratio")
SUPP_ANCHORS = {"rmr_kcal": [("metabolic", "rate"), ("(rmr)",)], "android_pct": [("android", "(a)")],
                "gynoid_pct": [("gynoid", "(g)")], "ag_ratio": [("a/g", "ratio")]}
SECTIONS = {  # key -> what the user sees if it's missing
    "summary": "Summary results (total body fat %, mass, fat, lean, BMC)",
    "regions": "Regional assessment (arms, legs, trunk, android, gynoid)",
    "supplemental": "RMR and android/gynoid fat",
    "vat": "Visceral fat (VAT)",
    "bone": "Bone density",
    "balance": "Muscle balance (left vs right)",
    "history": "History tables (earlier scans)",
    "percentile_table": "Body fat percentile chart",
}
LB = 1 / KG_LB


class DexaParseError(ValueError):
    """A user-facing reason the file couldn't be read at all."""


# ------------------------------- words -> lines ------------------------------ #
class Line:
    __slots__ = ("top", "words")

    def __init__(self, top: float, words: list[dict]):
        self.top, self.words = top, sorted(words, key=lambda w: w["x0"])

    @property
    def low(self) -> list[str]:
        return [w["text"].lower() for w in self.words]

    @property
    def text(self) -> str:
        return " ".join(w["text"] for w in self.words)

    def find(self, seq: tuple[str, ...]) -> tuple[float, float] | None:
        """(x0, x1) of the first run of words equal to `seq` (lower-case), or None."""
        low, n = self.low, len(seq)
        for i in range(len(low) - n + 1):
            if tuple(low[i:i + n]) == seq:
                return self.words[i]["x0"], self.words[i + n - 1]["x1"]
        return None

    def has(self, *seq: str) -> bool:
        return self.find(tuple(s.lower() for s in seq)) is not None


def lines_of(words: list[dict], tol: float = 2.5) -> list[Line]:
    """Groups words whose tops are within `tol` points into lines, top to bottom."""
    out: list[Line] = []
    cur: list[dict] = []
    for w in sorted(words, key=lambda w: (w["top"], w["x0"])):
        if cur and w["top"] - cur[0]["top"] > tol:
            out.append(Line(cur[0]["top"], cur))
            cur = []
        cur.append(w)
    if cur:
        out.append(Line(cur[0]["top"], cur))
    return out


def num(tok: str) -> float | None:
    t = tok.strip().replace(",", "").rstrip("%").lstrip("<>")
    if t.startswith("(") and t.endswith(")"):
        t = t[1:-1]
    try:
        return float(t)
    except ValueError:
        return None


def parse_date(tok: str) -> str | None:
    m = DATE_RE.match(tok.strip())
    if not m:
        return None
    try:
        return date(int(m[3]), int(m[1]), int(m[2])).isoformat()
    except ValueError:
        return None


def _mid(w: dict) -> float:
    return (w["x0"] + w["x1"]) / 2


def _section(lines: list[Line], start: int, stops: tuple[tuple[str, ...], ...]) -> list[Line]:
    """Lines after `start` up to the next heading in `stops`."""
    out = []
    for ln in lines[start + 1:]:
        if any(ln.has(*s) for s in stops):
            break
        out.append(ln)
    return out


def _heading(lines: list[Line], *seq: str, after: int = -1) -> int | None:
    return next((i for i, ln in enumerate(lines) if i > after and ln.has(*seq)), None)


def _anchors(header: list[Line], spec: dict[str, list[tuple[str, ...]]]) -> dict[str, float]:
    """x-centre of each column's header phrase. Lines nearest the data win, so a phrase in the
    explanatory paragraph above a table ("compares the fat and lean tissue") isn't mistaken for it."""
    out: dict[str, float] = {}
    for key, alts in spec.items():
        for ln in reversed(header):
            hit = next((h for alt in alts if (h := ln.find(alt))), None)
            if hit:
                out[key] = (hit[0] + hit[1]) / 2
                break
    return out


def _assign(values: list[dict], keys: tuple[str, ...], anchors: dict[str, float]) -> dict[str, dict]:
    """Maps value words to columns by nearest header x when every column has an anchor and no two
    values land on one column; otherwise by the report's column order."""
    if len(anchors) == len(keys):
        out: dict[str, dict] = {}
        for w in values:
            k = min(anchors, key=lambda k: abs(anchors[k] - _mid(w)))
            if k in out:
                break
            out[k] = w
        else:
            return out
    return dict(zip(keys, values)) if len(values) == len(keys) else {}


# ------------------------------ identity (dropped) --------------------------- #
def _identity(lines: list[Line]) -> tuple[list[Line], dict]:
    """Pulls sex, birth date and measured date out of the client header, then removes every
    client header row and the row under it. The birth date is returned only so the caller can
    work out ages; it is never stored."""
    info: dict = {}
    keep: list[Line] = []
    hdr: Line | None = None
    for ln in lines:
        if hdr is not None and ln.words:
            if not info:
                info = _identity_values(hdr, ln)
            hdr = None
            continue
        if ln.words and ln.low[0] == "client":
            hdr = ln
            continue
        keep.append(ln)
    return keep, info


def _identity_values(hdr: Line, row: Line) -> dict:
    out: dict = {}
    cols = {k: x for k, alt in (("birth", ("birth",)), ("measured", ("measured",)), ("sex", ("sex",)))
            if (x := (hdr.find(alt) or (None,))[0]) is not None}
    dates = [(w["x0"], parse_date(w["text"])) for w in row.words if parse_date(w["text"])]
    for w in row.words:
        if w["text"].lower() in ("male", "female"):
            out["sex"] = w["text"].lower()
    if dates:
        near = lambda x: min(dates, key=lambda d: abs(d[0] - x))[1]
        if "birth" in cols:
            out["dob"] = near(cols["birth"])
        if "measured" in cols:
            out["measured"] = near(cols["measured"])
        if "dob" in out and out.get("measured") == out["dob"]:
            out.pop("measured")
    return out


def age_on(dob: str | None, day: str | None) -> int | None:
    if not dob or not day:
        return None
    b, d = date.fromisoformat(dob), date.fromisoformat(day)
    return d.year - b.year - ((d.month, d.day) < (b.month, b.day))


# ---------------------------------- tables ----------------------------------- #
def _kg(v: float | None, unit_lb: bool) -> float | None:
    return None if v is None else v * LB if unit_lb else v


def _comp_row(cells: dict[str, dict], unit_lb: bool) -> dict:
    out = {}
    for k, w in cells.items():
        v = num(w["text"])
        if v is not None:
            out[k] = v if k == "fat_pct" else _kg(v, unit_lb)
    return out


def _units_lb(lines: list[Line]) -> bool | None:
    words = {w for ln in lines for w in ln.low}
    if words & {"(lbs)", "lbs", "lbs.", "(lb)", "lb"}:
        return True
    if words & {"(kg)", "kg"}:
        return False
    return None


def _summary(lines: list[Line], res: dict) -> None:
    i = _heading(lines, "summary", "results")
    if i is None:
        return
    sec = _section(lines, i, (("total", "body", "tissue", "quantification"), ("body", "fat", "percentile"), ("regional", "assessment")))
    first = next((j for j, ln in enumerate(sec) if ln.words and parse_date(ln.words[0]["text"])), None)
    if first is None:
        return
    header = sec[:first]
    unit_lb = _units_lb(header)
    res["_unit_lb"] = unit_lb if unit_lb is not None else True
    if unit_lb is False:
        res["warnings"].append("The summary table is in kg, not lb; values were read as kg.")
    anchors = _anchors(header, COMP_ANCHORS)
    rows = {}
    for ln in sec[first:]:
        d = parse_date(ln.words[0]["text"]) if ln.words else None
        if not d:
            continue
        vals = [w for w in ln.words[1:] if num(w["text"]) is not None]
        row = _comp_row(_assign(vals, COMP, anchors), res["_unit_lb"])
        if row:
            rows[d] = row
    if rows:
        res["_summary_rows"] = rows


def _percentile_table(lines: list[Line], res: dict) -> None:
    i = _heading(lines, "percentile", "chart")
    if i is None:
        return
    table: dict[str, dict] = {}
    sex = None
    for ln in _section(lines, i, (("regional", "assessment"),)):
        if ln.low in (["women"], ["men"]):
            sex = "female" if ln.low == ["women"] else "male"
            continue
        if not sex or not ln.words:
            continue
        band = ln.words[0]["text"]
        if not re.match(r"^(\d{2}-\d{2}|>\d{2}|\d{2}\+)$", band):
            continue
        cuts = []
        for w in ln.words[1:]:
            v = num(w["text"]) if w["text"].endswith("%") else None
            if v is not None and (not cuts or cuts[-1] != v):
                cuts.append(v)
        if len(cuts) == 4 and cuts == sorted(cuts):
            table.setdefault(sex, {})[band] = cuts
    if table:
        res["percentile_table"] = table


def _regions(lines: list[Line], res: dict) -> None:
    i = _heading(lines, "regional", "assessment")
    if i is None:
        return
    sec = _section(lines, i, (("supplemental", "results"), ("regional", "fat", "tissue")))
    first = next((j for j, ln in enumerate(sec) if ln.words and ln.low[0] in REGIONS and len(ln.words) > 3), None)
    if first is None:
        return
    unit_lb = _units_lb(sec[:first])
    anchors = _anchors(sec[:first], COMP_ANCHORS)
    out = {}
    for ln in sec[first:]:
        if ln.words and ln.low[0] in REGIONS:
            vals = [w for w in ln.words[1:] if num(w["text"]) is not None]
            row = _comp_row(_assign(vals, COMP, anchors), unit_lb if unit_lb is not None else res.get("_unit_lb", True))
            if row:
                out[ln.low[0]] = row
    if out:
        res["regions"] = out


def _supplemental(lines: list[Line], res: dict) -> None:
    i = _heading(lines, "supplemental", "results")
    if i is None:
        return
    sec = _section(lines, i, (("bone", "report"), ("muscle", "balance")))
    first = next((j for j, ln in enumerate(sec) if "cal/day" in ln.low), None)
    if first is None:
        return
    anchors = _anchors(sec[:first], SUPP_ANCHORS)
    rows = []
    for ln in sec[first:]:
        if "cal/day" not in ln.low:
            if rows:
                break
            continue
        vals = [w for w in ln.words if num(w["text"]) is not None]
        cells = _assign(vals, SUPP, anchors)
        row = {k: num(w["text"]) for k, w in cells.items() if num(w["text"]) is not None}
        if row:
            rows.append(row)
    if rows:
        res["_supp_rows"] = rows


def _column_below(lines: list[Line], start: int, x: float, stop, tol: float = 14) -> list[float]:
    """Numbers sitting in the same column as x on the lines after `start`, until stop(line)."""
    out = []
    for ln in lines[start + 1:]:
        if stop(ln):
            break
        for w in ln.words:
            if abs(w["x0"] - x) <= tol and num(w["text"]) is not None and not w["text"].endswith("%"):
                out.append(num(w["text"]))
    return out


def _vat(lines: list[Line], res: dict) -> None:
    i = next((j for j, ln in enumerate(lines) if ln.has("visceral", "adipose", "tissue") or ln.low[:1] == ["vat"]), None)
    if i is None:
        return
    j = next((j for j in range(i, min(i + 40, len(lines))) if lines[j].find(("mass", "(lbs)")) or lines[j].find(("mass", "(kg)"))), None)
    k = next((k for k in range(i, min(i + 60, len(lines))) if lines[k].low and lines[k].find(("volume",))), None)
    if j is None:
        return
    unit_lb = lines[j].find(("mass", "(lbs)")) is not None
    mass_line, (_, mx1) = lines[j], lines[j].find(("mass", "(lbs)")) or lines[j].find(("mass", "(kg)"))
    first = next((w for w in mass_line.words if w["x0"] > mx1 and num(w["text"]) is not None), None)
    if first is None:
        return
    is_vol = lambda ln: ln.find(("volume",)) is not None or ln.has("adipose", "tissue") or ln.has("muscle", "balance")
    mass = [num(first["text"])] + _column_below(lines, j, first["x0"], is_vol)
    vol = []
    if k is not None:
        vl = lines[k]
        vx1 = vl.find(("volume",))[1]
        vfirst = next((w for w in vl.words if w["x0"] > vx1 and num(w["text"]) is not None), None)
        if vfirst:
            vol = [num(vfirst["text"])] + _column_below(
                lines, k, vfirst["x0"], lambda ln: ln.has("adipose", "tissue") or ln.has("muscle", "balance"))
    res["_vat_rows"] = [dict(mass_kg=_kg(m, unit_lb), volume_in3=vol[n] if n < len(vol) else None) for n, m in enumerate(mass)]


def _bone(lines: list[Line], res: dict) -> None:
    i = _heading(lines, "bone", "report")
    if i is None:
        return
    sec = _section(lines, i, (("muscle", "balance"),))
    hdr = next((ln for ln in sec if ln.has("bmd")), None)
    x_min = (hdr.find(("bmd",))[0] - 70) if hdr else 0  # the region labels sit just left of BMD
    out = {}
    for ln in sec:
        ws = [w for w in ln.words if w["x0"] >= x_min]
        if not ws or ws[0]["text"].lower() not in BONE_REGIONS:
            continue
        cells = [w["text"] for w in ws[1:4]]
        if not cells or num(cells[0]) is None:
            continue
        v = lambda n: num(cells[n]) if len(cells) > n else None
        out[ws[0]["text"].lower()] = dict(bmd=v(0), t=v(1), z=v(2))
    if out:
        res["bone"] = out


def _balance(lines: list[Line], res: dict) -> None:
    i = _heading(lines, "muscle", "balance", "report")
    if i is None:
        return
    sec = _section(lines, i, (("regional", "fat", "tissue"), ("regional", "lean", "tissue")))
    first = next((j for j, ln in enumerate(sec) if tuple(ln.low[:2]) in LIMBS), None)
    if first is None:
        return
    anchors = _anchors(sec[:first], COMP_ANCHORS)
    unit_lb = res.get("_unit_lb", True)
    out = {}
    for ln in sec[first:]:
        key = LIMBS.get(tuple(ln.low[:2]))
        if key:
            vals = [w for w in ln.words[2:] if num(w["text"]) is not None]
            row = _comp_row(_assign(vals, COMP, anchors), unit_lb)
            if row:
                out[key] = row
    if out:
        res["balance"] = out


def _regional_history(lines: list[Line], res: dict) -> None:
    """REGIONAL FAT / LEAN TISSUE REPORT: per region, mass and % for every scan date."""
    kind = region = None
    hist: dict[str, dict[str, dict]] = {}  # date -> region -> {fat_kg, fat_pct, lean_kg, lean_pct}
    unit_lb = res.get("_unit_lb", True)
    for ln in lines:
        if ln.has("regional") and ln.has("report"):
            kind = "fat" if "fat" in ln.low else "lean" if "lean" in ln.low else None
            region = None
            continue
        if kind is None or not ln.words:
            continue
        if len(ln.words) == 1 and ln.low[0] in REGIONS:
            region = ln.low[0]
            continue
        if region is None:
            continue
        idx = next((n for n, w in enumerate(ln.words) if parse_date(w["text"])), None)
        if idx is None or idx + 1 >= len(ln.words):
            continue
        d, tok = parse_date(ln.words[idx]["text"]), ln.words[idx + 1]["text"]
        v = num(tok)
        if v is None:
            continue
        unit = ln.words[idx + 2]["text"].lower() if idx + 2 < len(ln.words) else ""
        cell = hist.setdefault(d, {}).setdefault(region, {})
        if tok.endswith("%"):
            cell[f"{kind}_pct"] = v
        elif unit in ("lbs", "lb", "kg"):
            cell[f"{kind}_kg"] = _kg(v, unit != "kg" and unit_lb)
    if hist:
        res["_regional_hist"] = hist


# ----------------------------------- parse ----------------------------------- #
def extract_words(data: bytes) -> list[list[dict]]:
    """Words with boxes, one list per page."""
    if not data[:1024].lstrip().startswith(b"%PDF"):
        raise DexaParseError("That file isn't a PDF.")
    try:
        import pdfplumber
        with pdfplumber.open(io.BytesIO(data)) as pdf:
            return [[dict(text=w["text"], x0=w["x0"], x1=w["x1"], top=w["top"]) for w in p.extract_words(x_tolerance=1.5)]
                    for p in pdf.pages]
    except DexaParseError:
        raise
    except Exception as ex:  # pdfminer raises a zoo of types for damaged files
        raise DexaParseError(f"The PDF couldn't be opened ({type(ex).__name__}).") from None


def parse(data: bytes) -> dict:
    return parse_pages(extract_words(data))


def parse_pages(pages: list[list[dict]]) -> dict:
    if not any(pages):
        raise DexaParseError("This PDF has no text layer (it may be a scan or a photo), so it can't be read.")
    lines: list[Line] = []
    for words in pages:
        lines += lines_of(words)
    lines, ident = _identity(lines)
    all_low = " ".join(" ".join(ln.low) for ln in lines)
    if "bodyspec" not in all_low and not ("summary results" in all_low and "regional" in all_low):
        raise DexaParseError("This doesn't look like a BodySpec DEXA report.")
    res: dict = dict(warnings=[])
    for step in (_summary, _percentile_table, _regions, _supplemental, _vat, _bone, _balance, _regional_history):
        try:
            step(lines, res)
        except Exception as ex:  # one odd table must not sink the rest; never echo report text
            res["warnings"].append(f"Part of the report couldn't be read ({step.__name__.strip('_')}: {type(ex).__name__}).")
    dob = ident.get("dob")
    rows = res.pop("_summary_rows", {})
    measured = ident.get("measured") or (max(rows) if rows else None) or max(res.get("_regional_hist", {}) or [None])
    if not measured:
        raise DexaParseError("No measured date was found in this report.")
    order = sorted(set(rows) | set(res.get("_regional_hist", {})), reverse=True)  # newest first, like the report
    supp, vat = res.pop("_supp_rows", []), res.pop("_vat_rows", [])
    rhist = res.pop("_regional_hist", {})
    res.pop("_unit_lb", None)

    def at(seq: list, d: str):
        """Supplemental/VAT rows carry no dates: they line up with the summary rows (newest first)."""
        if d in rows and len(seq) == len(rows):
            return seq[sorted(rows, reverse=True).index(d)]
        return seq[0] if seq and d == measured else None

    scan = dict(measured_date=measured, sex=ident.get("sex"), age=age_on(dob, measured))
    if measured in rows:
        scan["summary"] = rows[measured]
    if res.get("regions"):
        scan["regions"] = res.pop("regions")
    if (s := at(supp, measured)):
        scan["supplemental"] = s
    if (v := at(vat, measured)):
        scan["vat"] = v
    for k in ("bone", "balance", "percentile_table"):
        if res.get(k):
            scan[k] = res.pop(k)
    if measured in rhist:  # keep the regional % from the fat/lean history pages alongside the snapshot
        scan["regional_trend"] = rhist[measured]
    history = []
    for d in order:
        if d >= measured:
            if d > measured:
                res["warnings"].append(f"A history row ({d}) is dated after the scan itself; it was ignored.")
            continue
        h = dict(measured_date=d, sex=ident.get("sex"), age=age_on(dob, d))
        if d in rows:
            h["summary"] = rows[d]
        if (s := at(supp, d)):
            h["supplemental"] = s
        if (v := at(vat, d)):
            h["vat"] = v
        if d in rhist:
            h["regional_trend"] = rhist[d]
        history.append(h)
    if history:
        scan["history"] = history
    if supp and len(supp) != max(len(rows), 1):
        res["warnings"].append("The RMR / android-gynoid table has a different number of rows than the summary; only the latest row was used.")
    if vat and len(vat) != max(len(rows), 1):
        res["warnings"].append("The VAT table has a different number of rows than the summary; only the latest row was used.")
    present = dict(summary="summary" in scan, regions="regions" in scan, supplemental="supplemental" in scan, vat="vat" in scan,
                   bone="bone" in scan, balance="balance" in scan, history=bool(history), percentile_table="percentile_table" in scan)
    scan["found"] = [k for k, ok in present.items() if ok]
    scan["missing"] = [k for k, ok in present.items() if not ok and k != "history"]
    scan["warnings"] = res["warnings"] + validate(scan)
    return scan


# --------------------------------- validation -------------------------------- #
def _lb(kg):
    return None if kg is None else kg * KG_LB


def validate(scan: dict) -> list[str]:
    """Sanity checks on a parsed (or user-edited) scan. Returns plain-language warnings."""
    w: list[str] = []
    label = lambda k: k.replace("_", " ")

    def pct_ok(where: str, v):
        if v is not None and not 0 <= v <= 100:
            w.append(f"{where}: {v:g}% is outside 0–100%.")

    def comp_ok(where: str, r: dict | None, tol_lb=0.5):
        if not r:
            return
        pct_ok(f"{where} body fat", r.get("fat_pct"))
        parts = [r.get(k) for k in ("fat_kg", "lean_kg", "bmc_kg")]
        if None not in parts and r.get("total_kg") is not None:
            diff = _lb(sum(parts) - r["total_kg"])
            if abs(diff) > tol_lb:
                w.append(f"{where}: fat + lean + bone ({_lb(sum(parts)):.1f} lb) doesn't match the total ({_lb(r['total_kg']):.1f} lb).")
        if r.get("fat_kg") is not None and r.get("total_kg") and r.get("fat_pct") is not None:
            calc = r["fat_kg"] / r["total_kg"] * 100
            if abs(calc - r["fat_pct"]) > 1.0:
                w.append(f"{where}: fat ÷ total is {calc:.1f}%, but the report says {r['fat_pct']:g}%.")
        for k in ("total_kg", "fat_kg", "lean_kg", "bmc_kg"):
            if r.get(k) is not None and r[k] < 0:
                w.append(f"{where}: {label(k[:-3])} is negative.")

    comp_ok("Total", scan.get("summary"))
    regs = scan.get("regions") or {}
    for k, r in regs.items():
        comp_ok(k.capitalize(), r)
    tot = (scan.get("summary") or {}).get("total_kg") or (regs.get("total") or {}).get("total_kg")
    parts = [(regs.get(k) or {}).get("total_kg") for k in ("arms", "legs", "trunk")]
    if tot and None not in parts:
        share = sum(parts) / tot
        if share > 1.01:
            w.append(f"Arms + legs + trunk ({_lb(sum(parts)):.1f} lb) is more than the total ({_lb(tot):.1f} lb).")
        elif share < 0.85:
            w.append(f"Arms + legs + trunk is only {share * 100:.0f}% of the total; the head is usually only ~5–8%.")
    for k, key in (("total_kg", "total"), ("fat_kg", "fat"), ("lean_kg", "lean")):
        a, b = (scan.get("summary") or {}).get(k), (regs.get("total") or {}).get(k)
        if a is not None and b is not None and abs(_lb(a - b)) > 0.3:
            w.append(f"The summary and regional totals disagree on {key} mass.")
    bal = scan.get("balance") or {}
    for limb in ("arm", "leg"):
        r, l, t = bal.get(f"right_{limb}"), bal.get(f"left_{limb}"), bal.get(f"{limb}s")
        if r and l and t and None not in (r.get("total_kg"), l.get("total_kg"), t.get("total_kg")):
            if abs(_lb(r["total_kg"] + l["total_kg"] - t["total_kg"])) > 0.3:
                w.append(f"Right + left {limb} doesn't add up to the {limb}s total.")
        for side in (r, l):
            comp_ok(f"{limb.capitalize()} ({'right' if side is r else 'left'})", side, tol_lb=0.3)
    s = scan.get("supplemental") or {}
    for k in ("android_pct", "gynoid_pct"):
        pct_ok(k.split("_")[0].capitalize() + " fat", s.get(k))
    if s.get("android_pct") and s.get("gynoid_pct") and s.get("ag_ratio") is not None:
        if abs(s["android_pct"] / s["gynoid_pct"] - s["ag_ratio"]) > 0.03:
            w.append(f"A/G ratio {s['ag_ratio']:g} doesn't match android ÷ gynoid ({s['android_pct'] / s['gynoid_pct']:.2f}).")
    if s.get("rmr_kcal") is not None and not 800 <= s["rmr_kcal"] <= 5000:
        w.append(f"RMR {s['rmr_kcal']:g} cal/day looks implausible.")
    v = scan.get("vat") or {}
    if v.get("mass_kg") is not None and not 0 <= _lb(v["mass_kg"]) <= 20:
        w.append("VAT mass looks implausible (expected 0–20 lb).")
    for k, b in (scan.get("bone") or {}).items():
        if b.get("bmd") is not None and not 0.3 <= b["bmd"] <= 3.5:
            w.append(f"Bone density for {k} ({b['bmd']:g} g/cm²) looks implausible.")
    for h in scan.get("history") or []:
        if h["measured_date"] >= scan["measured_date"]:
            w.append(f"History scan {h['measured_date']} isn't earlier than this scan.")
        comp_ok(f"History {h['measured_date']}", h.get("summary"))
    return list(dict.fromkeys(w))
