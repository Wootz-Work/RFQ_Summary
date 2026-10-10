"""
past_quotes.py — the same or closely similar products we have quoted before.

The past quotes live in one Postgres table: a row per line of every quotation
we sent (part name, quantity, our price, margin, the RFQ's status). This module
reads that table, cleans it, and for each product of a new RFQ finds:

  same     — the part we quoted before: the same part number, or the same
             type, size, standard, material and finish
  similar  — the same kind of part in the same material, one size away

Nothing looser is ever shown: a list of vaguely related parts costs the reader
more time than it saves. The team sees one line per past RFQ at the end of the
RFQ summary: its title, linked to its OneDrive quotation folder, and which of
this RFQ's products it matches. The customer's name is read only to guess a
missing currency from their other quotes; it is never shown.
"""
from __future__ import annotations

import json
import math
import re
import threading
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

from .costing_rates import material_rate

# ----------------------------------------------------------------------------- the table

# The export's two quantity columns are swapped: `unit_of_quantity` holds the
# number and `quantity_in_unit` the unit. Read them by what they hold.
COLUMNS = (
    "row_id", "line_no", "title", "customer_name", "rfq_sequence", "current_status", "quote_margin",
    "final_quote_number", "document_details", "part_name", "part_number", "remarks",
    "unit_of_quantity", "quantity_in_unit", "total_value", "ex_works_unit_price", "currency",
    "ex_works", "ex_works_unit", "lead_time", "unit_of_lead_time", "created_at", "quotation_folder_link",
)

# Lines that are charges on the quote, not products.
_NOT_A_PRODUCT = re.compile(
    r"^\s*(air|sea|ocean|road|inland|local)?\s*(freight|shipping|transport(ation)?|courier|logistics|"
    r"duty|duties|customs|insurance|packing|packaging|pallet(isation|ization)?|handling|"
    r"inspection charges?|testing charges?|documentation|bank charges?|discount|sub-?total|grand total|total|"
    r"tooling( cost| charges?)?|die cost|mou?ld cost|setup|set-up|ddp charges?|dap charges?|cif charges?|"
    r"one[\s-]*time\b.*\b(tool|cost|charge)|ppap|first article|fai\b|sample charges?|certification charges?)\b",
    re.IGNORECASE,
)


_CHARGE_WORD = re.compile(r"\b(freight|shipping|courier|duty|duties|customs|insurance|tooling|ppap|logistics)\b", re.I)


def is_charge(name: str) -> bool:
    """A quote line that is a charge, not a product: freight, duty, tooling, PPAP …"""
    return bool(_NOT_A_PRODUCT.match(name) or (len(name.split()) <= 5 and _CHARGE_WORD.search(name)))


@dataclass
class PastLine:
    rfq_row_id: str
    title: str
    status: str
    quote_date: Optional[date]
    quote_number: str
    incoterm: str
    line_no: Optional[int]
    part_name: str
    part_number: str
    remarks: str
    qty: Optional[float]
    unit: str
    unit_price: Optional[float]
    currency: str
    currency_assumed: bool
    margin_pct: Optional[float]
    lead_weeks: Optional[float]
    freight_quoted_separately: bool
    folder_link: str = ""                             # the RFQ's OneDrive quotation folder
    fp: "Fingerprint" = None  # type: ignore[assignment]


_INVISIBLE = re.compile(r"[\u200b-\u200f\u202a-\u202e\u2060-\u2069\ufeff]")


def _null(v: Any) -> str:
    # Text pulled from PDFs carries invisible direction marks between words.
    s = "" if v is None else re.sub(r"[ \t]+", " ", _INVISIBLE.sub(" ", str(v))).strip()
    return "" if s.upper() == "NULL" else s


def _num(v: Any) -> Optional[float]:
    s = _null(v).replace(",", "")
    try:
        n = float(s)
    except ValueError:
        return None
    return n if math.isfinite(n) else None


def _details(v: Any) -> Dict[str, Any]:
    if isinstance(v, dict):
        return v
    try:
        d = json.loads(_null(v) or "{}")
    except ValueError:
        return {}
    return d if isinstance(d, dict) else {}


def _date(v: Any) -> Optional[date]:
    s = _null(v)[:10]
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except ValueError:
        return None


def clean_rows(rows: Iterable[Dict[str, Any]]) -> List[PastLine]:
    """Raw table rows -> product lines with a unit price, a currency and a date."""
    rows = [dict(r) for r in rows]
    # Currency fallbacks: the RFQ's totals block, then its other lines, then the
    # customer's usual currency (marked as an assumption).
    by_rfq: Dict[str, Counter] = defaultdict(Counter)
    by_customer: Dict[str, Counter] = defaultdict(Counter)
    freight_rfqs: Set[str] = set()
    for r in rows:
        cur = _null(r.get("currency")).upper()
        if cur:
            by_rfq[_null(r.get("row_id"))][cur] += 1
            by_customer[_null(r.get("customer_name")).lower()][cur] += 1
        if is_charge(_null(r.get("part_name"))) and re.search(
                r"freight|shipping|transport|courier|duty|duties|customs|ddp|dap|cif", _null(r.get("part_name")), re.I):
            freight_rfqs.add(_null(r.get("row_id")))

    out: List[PastLine] = []
    for r in rows:
        name = _null(r.get("part_name"))
        if not name or is_charge(name):
            continue
        rfq = _null(r.get("row_id"))
        d = _details(r.get("document_details"))
        cur, assumed = _null(r.get("currency")).upper(), False
        if not cur:
            cur = _null(d.get("grand_total_currency")).upper()
        if not cur and len(by_rfq[rfq]) == 1:
            cur = next(iter(by_rfq[rfq]))
        if not cur:
            usual = by_customer[_null(r.get("customer_name")).lower()].most_common(1)
            cur, assumed = (usual[0][0], True) if usual else ("", False)
        qty = _num(r.get("unit_of_quantity"))
        unit = _null(r.get("quantity_in_unit")) or "pcs"
        # Total ÷ quantity is the price per unit. The stored unit price is per 100 on
        # about a quarter of the lines (the quotation's "per 100 pcs" layout), so it
        # is used only when there is no total to divide.
        total = _num(r.get("total_value"))
        price = total / qty if total is not None and qty else _num(r.get("ex_works_unit_price"))
        margin = _num(r.get("quote_margin"))
        if margin is not None and 0 < margin < 1:
            margin *= 100                     # 0.15 means 15 %
        lead = _num(r.get("ex_works")) or _num(r.get("lead_time"))
        unit_lead = (_null(r.get("ex_works_unit")) or _null(r.get("unit_of_lead_time"))).lower()
        if lead is not None and unit_lead.startswith("day"):
            lead = lead / 7
        line_no = _num(r.get("line_no"))
        line = PastLine(
            rfq_row_id=rfq, title=_null(r.get("title")), status=_null(r.get("current_status")),
            quote_date=_date(d.get("date")) or _date(r.get("created_at")),
            quote_number=_null(r.get("final_quote_number")),
            incoterm=_null(d.get("incoterms")).split(":")[0].split("-")[0].strip().upper()[:12],
            line_no=int(line_no) if line_no is not None else None,
            part_name=name, part_number=_null(r.get("part_number")), remarks=_null(r.get("remarks")),
            qty=qty, unit=unit, unit_price=price, currency=cur, currency_assumed=assumed,
            margin_pct=margin, lead_weeks=lead, freight_quoted_separately=rfq in freight_rfqs,
            folder_link=link if (link := _null(r.get("quotation_folder_link"))).startswith("http") else "",
        )
        line.fp = fingerprint(f"{name} {line.remarks}", line.part_number, name=name)
        out.append(line)
    return out


# ----------------------------------------------------------------------------- what a part is

# Part types, most specific first. One text gets one primary type: the first
# that matches. Names are what the reader sees in the section.
_TYPES: Sequence[Tuple[str, str]] = (
    ("rod end", r"rod[\s-]*ends?|heim joints?|spherical (plain )?bearings?"),
    ("eye bolt", r"eye[\s-]*bolts?"),
    ("u-bolt", r"u[\s-]*bolts?"),
    ("anchor", r"anchor( bolt)?s?|wedge anchors?|chemical anchors?"),
    ("threaded rod", r"threaded (rod|bar)s?|studding|all[\s-]*thread"),
    ("stud", r"stud bolts?|studs?\b"),
    ("rivet", r"rivets?"),
    ("standoff", r"stand[\s-]*offs?|spacers?|pcb spacers?"),
    ("insert", r"inserts?|rivet nuts?|nutserts?|pem\b"),
    ("nut", r"\bnuts?\b"),
    ("washer", r"washers?"),
    ("screw", r"screws?"),
    ("bolt", r"bolts?"),
    ("pin", r"\bpins?\b|dowels?"),
    ("elbow", r"elbows?|\bbends?\b"),
    ("tee", r"\btees?\b"),
    ("reducer", r"reducers?|reducing"),
    ("flange", r"flanges?"),
    ("gasket", r"gaskets?"),
    ("coupling", r"couplings?|couplers?"),
    ("union", r"\bunions?\b"),
    ("nipple", r"nipples?"),
    ("cap", r"\bcaps?\b|end caps?"),
    ("plug", r"\bplugs?\b"),
    ("valve", r"valves?"),
    ("pipe", r"\bpipes?\b|\btubes?\b|tubing"),
    ("saddle", r"saddles?"),
    ("clamp", r"clamps?|clips?"),
    ("bracket", r"brackets?"),
    ("hinge", r"hinges?"),
    ("bush", r"bush(es|ing|ings)?\b"),
    ("bearing", r"bearings?"),
    ("spring", r"springs?"),
    ("shaft", r"shafts?|axles?"),
    ("gear", r"gears?|sprockets?|pulleys?"),
    ("hook", r"hooks?|shackles?"),
    ("channel", r"channels?|strut"),
    ("angle", r"\bangles?\b"),
    ("plate", r"plates?|sheets?"),
    ("extrusion", r"extrusions?|profiles?"),
    ("housing", r"housings?|enclosures?|boxes?|cabinets?"),
    ("casting", r"castings?"),
    ("forging", r"forgings?"),
    ("conduit fitting", r"conduit"),
    ("frame", r"frames?|structures?|skids?"),
)
_TYPE_RX = [(name, re.compile(rf"\b(?:{rx})", re.IGNORECASE)) for name, rx in _TYPES]

_FINISHES: Sequence[Tuple[str, str]] = (
    ("zinc flake", r"zinc[\s-]*flake|geomet|dacromet|delta[\s-]*(protekt|tone)|magni"),
    ("pre-galvanised", r"pre[\s-]*galv"),
    ("HDG", r"\bhdg\b|hot[\s-]*dip|galvani[sz]ed|galvanis|galvaniz|\bgalv\b"),
    ("zinc", r"\bzinc\b|\bbzp\b|\bzp\b|\bznp?\b|zinc[\s-]*plat|electro[\s-]*galv|yellow zinc|clear zinc"),
    ("PVD", r"\bpvd\b"),
    ("black oxide", r"black[\s-]*oxide|blackodi|\bblacken"),
    ("anodised", r"anodi[sz]"),
    ("powder coated", r"powder[\s-]*coat"),
    ("painted", r"\bpaint"),
    ("nickel", r"nickel[\s-]*plat|\benp\b|electroless"),
    ("chrome", r"chrom(e|ium)[\s-]*plat"),
    ("passivated", r"passivat|pickl"),
    ("brushed", r"brushed"),
    ("plain", r"\bplain\b|self[\s-]*colou?r|uncoated|no (finish|coating|anodising)|machined finish|mill finish"),
)
_FINISH_RX = [(name, re.compile(rx, re.IGNORECASE)) for name, rx in _FINISHES]

# What makes two parts of one type different products: flange style, elbow angle,
# head and drive, washer and nut kind. Equal when both name one.
_SUBTYPES: Sequence[Tuple[str, str]] = (
    ("wn", r"\b(rf|ff)?wn(rf|ff)?\b|weld(ing)?[\s-]*neck"), ("so", r"\b(rf|ff)?so(rf|ff)?\b|slip[\s-]*on"),
    ("blind", r"\bblind\b|\b(rf)?bl(rf)?\b"),
    ("lap", r"lap[\s-]*joint"), ("sw", r"\bsw\b|socket[\s-]*weld"), ("thd", r"\bthd\b|\bnpt\b|threaded|screwed"),
    ("bw", r"\bbw\b|butt[\s-]*weld|\bbe\b|bevel"),
    ("90", r"\b90\b|90°|90 ?deg"), ("45", r"\b45\b|45°|45 ?deg"), ("lr", r"\blr\b|long radius"), ("sr", r"\bsr\b|short radius"),
    ("equal", r"equal tee|straight tee"), ("reducing", r"reducing"), ("conc", r"concentric"), ("ecc", r"eccentric"),
    ("hex", r"\bhex|hexagon"), ("socket head", r"socket[\s-]*(head|cap)|\bshcs\b|allen"), ("pan", r"\bpan\b"),
    ("csk", r"countersunk|\bcsk\b|flat head"), ("button", r"button"), ("cheese", r"cheese"),
    ("shoulder", r"shoulder (screw|bolt)"), ("set screw", r"set[\s-]*screw|grub"), ("cap screw", r"cap[\s-]*screw"),
    ("machine screw", r"machine[\s-]*screw"), ("self tapping", r"self[\s-]*tap|tapping screw|tek screw"),
    ("wood screw", r"wood[\s-]*screw"), ("hex sets", r"hex sets"),
    ("flat", r"\bflat washer|plain washer"), ("spring", r"spring washer|split washer|lock washer"),
    ("fender", r"fender|penny"), ("nyloc", r"nyloc|nylon insert|lock ?nut"), ("flange nut", r"flange nut|serrated"),
    ("dome", r"dome|acorn"), ("wing", r"\bwing\b"), ("heavy", r"\bheavy\b"),
    ("smls", r"\bsmls\b|seamless"), ("welded", r"\berw\b|\befw\b|welded"),
)
_SUBTYPE_RX = [(name, re.compile(rx, re.IGNORECASE)) for name, rx in _SUBTYPES]
_DIMENSIONAL_ORGS = ("asme", "ansi", "din", "iso", "en", "bs", "jis", "mssp", "is")

_STANDARD = re.compile(
    r"\b(din|iso|en|astm|asme|ansi|bs|is|jis|mss[\s-]*sp)\s*[-:]?\s*([a-z]?\d+(?:[.\-/]\d+)*[a-z]?)", re.IGNORECASE)
_NUT_CLASS = re.compile(r"\b(?:class|grade|gr|cl)\.?\s*[-:]?\s*(4|5|6|8|10|12)\b(?![.\d])", re.IGNORECASE)
_PROPERTY_CLASS = re.compile(r"\b(4\.6|4\.8|5\.6|5\.8|6\.8|8\.8|10\.9|12\.9|a[24]-?(?:50|70|80))\b", re.IGNORECASE)
_METRIC = re.compile(r"\bm\s?(\d{1,3}(?:\.\d+)?)(?:\s*[x×]\s*(\d+(?:\.\d+)?)(?:\s*mm)?)?(?:\s*[x×]\s*(\d{2,4}(?:\.\d+)?)(?:\s*mm)?)?(?![\w.])",
                     re.IGNORECASE)
_INCH = re.compile(r"(?<![\d/.])(\d+(?:[\s-]+\d+/\d+)?|\d+/\d+|\d*\.\d+)\s*(?:\"|”|″|''|in\b|inch(?:es)?\b|-(?=\d+\s*(?:unc|unf|un|tpi)\b))",
                   re.IGNORECASE)
_THREAD_INCH = re.compile(r"(?<![\d/.])(\d+(?:[\s-]+\d+/\d+)?|\d+/\d+)\s*-\s*([4-9]|[1-8]\d)(?![/\d.])(?!\s*mm)", re.IGNORECASE)
_MM = re.compile(r"(?<![\d.])(\d+(?:\.\d+)?)\s*mm\b", re.IGNORECASE)
_DN = re.compile(r"\b(?:dn|nb)\s*(\d{1,4})\b", re.IGNORECASE)
_SCHEDULE = re.compile(r"\bsch(?:edule)?\.?\s*(\d{1,3}s?|xxs|xs|std)\b|\b(std|xs|xxs)\b", re.IGNORECASE)
# STD = 40 and XS = 80 up to 10"; the S suffix only says stainless.
_SCHEDULE_SAME = {"std": "40", "xs": "80", "xxs": "160"}
_RATING = re.compile(r"(?:\bclass|\bcl\.?|#)\s*(150|300|400|600|900|1500|2500|3000|6000|9000)\b|\b(150|300|600|900|1500|2500|3000|6000)\s*(?:#|lbs?\b)",
                     re.IGNORECASE)
_CODE = re.compile(r"\b[A-Z0-9][A-Z0-9_./-]{6,}\b")
# Grades, standards and dimensions look like codes but are not part numbers.
_NOT_CODE = re.compile(r"^(M\d|\d+(\.\d+)?X\d)|X\d+(\.\d+)?$|^(ASTM|ASME|ANSI|DIN|ISO|EN|BS|JIS|IS)\d|^(MOC|GR|GRADE|SCH|CL|CLASS)", re.I)
_STOP = set("""a an and as at by for from in into of on or per the to with without x mm inch nos pcs pc each ea
               set sets type grade class qty quantity size sizes material moc finish as per drawing dwg rev
               std standard complete assembly incl inclusive including supply""".split())


def _inch(text: str) -> Optional[float]:
    t = text.strip().replace("-", " ")
    try:
        if " " in t:
            whole, frac = t.split()
            a, b = frac.split("/")
            return int(whole) + int(a) / int(b)
        if "/" in t:
            a, b = t.split("/")
            return int(a) / int(b)
        return float(t)
    except (ValueError, ZeroDivisionError):
        return None


@dataclass
class Fingerprint:
    text: str
    ptype: str = ""
    material: str = ""
    finish: str = ""
    standards: Set[str] = field(default_factory=set)
    sizes: Set[str] = field(default_factory=set)       # exact size tokens: m10, l120, 0.3125in, sch40, cl150 …
    main_mm: Optional[float] = None                   # the one size that says how big the part is
    codes: Set[str] = field(default_factory=set)      # part / drawing numbers, normalised
    subtypes: Set[str] = field(default_factory=set)
    length_mm: Optional[float] = None
    system: str = ""                                  # "metric" | "inch" — threads and nominal sizes
    name_words: Set[str] = field(default_factory=set)
    words: Set[str] = field(default_factory=set)


def norm_code(code: str) -> str:
    """'MT_WST00122480', 'mt-wst 00122480 rev B' -> 'MTWST00122480'."""
    c = re.sub(r"\s*(rev(ision)?\.?\s*[a-z0-9]{1,3})\s*$", "", str(code or ""), flags=re.IGNORECASE)
    return re.sub(r"[^A-Z0-9]", "", c.upper())


def fingerprint(text: str, part_number: str = "", material: str = "", finish: str = "",
                name: str = "") -> Fingerprint:
    name_text = name                    # the loops below reuse `name`
    raw = re.sub(r"\s+", " ", str(text or "")).strip()
    t = raw.replace("×", "x").replace("”", '"').replace("″", '"').replace(",", " ")
    fp = Fingerprint(text=raw)
    for name, rx in _TYPE_RX:
        if rx.search(t):
            fp.ptype = name
            break
    moc = re.search(r"\b(?:moc|material)\s*[-:–]?\s*([^|\n;]+)", t, re.IGNORECASE)
    hit = material_rate(material or (moc.group(1) if moc else "")) or material_rate(t)
    fp.material = hit[0] if hit else ""
    for name, rx in _FINISH_RX:
        if rx.search(finish or "") or (not finish and rx.search(t)):
            fp.finish = name
            break
    fp.subtypes = {name for name, rx in _SUBTYPE_RX if rx.search(t)}
    for org, num in _STANDARD.findall(t):
        fp.standards.add(re.sub(r"[\s-]", "", org.lower()) + num.lower().replace("-", "."))
    for pc in _PROPERTY_CLASS.findall(t):
        fp.sizes.add("pc" + pc.lower().replace("-", ""))
    for pc in _NUT_CLASS.findall(t):
        fp.sizes.add("pc" + pc)
    sizes_mm: List[float] = []
    for d, pitch, length in _METRIC.findall(t):
        fp.sizes.add(f"m{float(d):g}")
        sizes_mm.append(float(d))
        ln = float(length) if length else (float(pitch) if pitch and float(pitch) >= 6 else None)
        if ln:                                      # "M10 x 120": the second number is a length, not a pitch
            fp.sizes.add(f"l{ln:g}")
            fp.length_mm = fp.length_mm or ln
    for whole, tpi in _THREAD_INCH.findall(t):
        v = _inch(whole)
        if v and v < 4:
            fp.sizes.add(f"{v:.4f}in")
            fp.sizes.add(f"tpi{tpi}")
            sizes_mm.append(v * 25.4)
    for m in _INCH.findall(t):
        v = _inch(m)
        if v and 0 < v < 200:
            fp.sizes.add(f"{v:.4f}in")
            sizes_mm.append(v * 25.4)
    for m in _MM.findall(t):
        fp.sizes.add(f"{float(m):g}mm")
        sizes_mm.append(float(m))
    for m in _DN.findall(t):
        fp.sizes.add(f"dn{int(m)}")
        sizes_mm.append(float(m))
    for a, b in _SCHEDULE.findall(t):
        v = (a or b).lower().rstrip("s") if (a or b).lower() not in ("xs", "xxs") else (a or b).lower()
        fp.sizes.add("sch" + _SCHEDULE_SAME.get(v, v))
    for a, b in _RATING.findall(t):
        fp.sizes.add("cl" + (a or b))
    fp.main_mm = sizes_mm[0] if sizes_mm else None
    if any(x.startswith("m") and x[1:2].isdigit() for x in fp.sizes):
        fp.system = "metric"
    elif any(x.endswith("in") for x in fp.sizes):
        fp.system = "inch"
    if fp.ptype in ("tee", "reducer") or "reducing" in fp.subtypes:
        nominal = {x for x in fp.sizes if x.endswith("in")} | {x for x in fp.sizes if x.startswith("dn")}
        if fp.ptype == "tee":
            fp.subtypes.add("reducing" if len(nominal) >= 2 or "reducing" in fp.subtypes else "equal")
    pn = norm_code(part_number)
    if len(pn) >= 6 and not pn.isdigit() or len(pn) >= 8:
        fp.codes.add(pn)
    for c in _CODE.findall(raw.upper()):
        n = norm_code(c)
        # A code in free text needs five or more digits: that keeps out grades
        # (WP316L, S355JR), standards (B36.10M) and short sizes.
        if sum(ch.isdigit() for ch in n) >= 5 and len(n) >= 8 and not _NOT_CODE.search(c):
            fp.codes.add(n)
    fp.words = {w for w in re.findall(r"[a-z][a-z0-9]+", t.lower()) if w not in _STOP and len(w) > 1}
    fp.name_words = {w for w in re.findall(r"[a-z][a-z0-9]+", (name_text or t).lower()) if w not in _STOP and len(w) > 1}
    return fp


def _jaccard(a: Set[str], b: Set[str]) -> float:
    return len(a & b) / len(a | b) if a and b else 0.0


# ----------------------------------------------------------------------------- matching

SAME, SIMILAR = "same", "similar"


@dataclass
class Match:
    level: str
    line: PastLine
    score: float
    why: str


def compare(q: Fingerprint, p: Fingerprint) -> Optional[Tuple[str, float, str]]:
    """(level, score, why) when p is the same or a closely similar part to q; else None."""
    shared_codes = q.codes & p.codes
    if shared_codes:
        return SAME, 3.0, f"same part number {sorted(shared_codes)[0]}"
    if q.codes and p.codes:
        return None                            # two different part numbers are two different parts
    words = _jaccard(q.words, p.words)
    names = _jaccard(q.name_words, p.name_words)
    if (names >= 0.85 and len(q.name_words) >= 3 and (q.sizes or q.material or q.codes)
            and q.sizes == p.sizes and q.ptype == p.ptype and q.material == p.material
            and not (q.finish and p.finish and q.finish != p.finish)):
        return SAME, 2.0 + names, "same description"
    if q.system and p.system and q.system != p.system:
        return None                            # an inch part is not a metric part
    if not q.ptype or q.ptype != p.ptype:
        return None
    if q.material and p.material and q.material != p.material:
        return None
    qd = {s for s in q.standards if s.startswith(_DIMENSIONAL_ORGS)}
    pd = {s for s in p.standards if s.startswith(_DIMENSIONAL_ORGS)}
    if qd and pd and not (qd & pd):
        return None
    # Different style (WN vs SO flange, 90 vs 45 elbow, hex vs socket head) is a different product.
    for group in (("wn", "so", "blind", "lap", "sw", "thd"), ("90", "45"), ("lr", "sr"), ("equal", "reducing"),
                  ("conc", "ecc"), ("hex", "socket head", "pan", "csk", "button", "cheese"),
                  ("shoulder", "set screw", "cap screw", "machine screw", "self tapping", "wood screw"),
                  ("flat", "spring", "fender"), ("nyloc", "flange nut", "dome", "wing"), ("smls", "welded")):
        a, b = q.subtypes & set(group), p.subtypes & set(group)
        if a and b and not (a & b):
            return None
    # Pressure class, schedule and property class must agree when both state one.
    for prefix in ("cl", "sch", "pc"):
        a = {x for x in q.sizes if x.startswith(prefix)}
        b = {x for x in p.sizes if x.startswith(prefix)}
        if a and b and not (a & b):
            return None
    words = _jaccard(q.words, p.words)
    finish_ok = not (q.finish and p.finish and q.finish != p.finish)
    if (q.sizes and q.sizes == p.sizes and q.material and q.material == p.material and finish_ok
            and words >= 0.4):
        return SAME, 2.0 + words, "same type, size, material" + (", finish" if q.finish and p.finish else "")
    # Similar: same type and material, one size step away — never a different size class.
    if not (q.material and q.material == p.material):
        return None
    if q.main_mm and p.main_mm:
        ratio = max(q.main_mm, p.main_mm) / max(min(q.main_mm, p.main_mm), 1e-6)
        if ratio > 1.35:                        # one size step: M10 -> M12, 6" -> 8"
            return None
        closeness = 1 - (ratio - 1) / 0.35
    elif q.sizes or p.sizes:
        return None
    else:
        closeness = 0.5
    if q.length_mm and p.length_mm and max(q.length_mm, p.length_mm) / min(q.length_mm, p.length_mm) > 1.5:
        return None
    if words < 0.3:
        return None
    score = 1.0 + 0.5 * words + 0.3 * closeness + (0.2 if finish_ok and q.finish and p.finish else 0)
    diff = []
    if q.main_mm and p.main_mm and abs(q.main_mm - p.main_mm) > 0.01:
        diff.append("size")
    if q.finish and p.finish and q.finish != p.finish:
        diff.append("finish")
    return SIMILAR, score, "same type and material" + (f"; differs in {', '.join(diff)}" if diff else "")


class PastQuoteIndex:
    def __init__(self, lines: List[PastLine]):
        self.lines = lines
        self.by_type: Dict[str, List[PastLine]] = defaultdict(list)
        self.by_code: Dict[str, List[PastLine]] = defaultdict(list)
        for ln in lines:
            self.by_type[ln.fp.ptype].append(ln)
            for c in ln.fp.codes:
                self.by_code[c].append(ln)

    def find(self, q: Fingerprint, *, exclude_rfq: str = "", limit: int = 3) -> List[Match]:
        cands: Dict[int, PastLine] = {}
        for c in q.codes:
            for ln in self.by_code.get(c, []):
                cands[id(ln)] = ln
        # Parts without a recognised type can still match on near-identical text.
        for ln in self.by_type.get(q.ptype or "", []):
            cands[id(ln)] = ln
        found: List[Match] = []
        for ln in cands.values():
            if exclude_rfq and ln.rfq_row_id == exclude_rfq:
                continue
            hit = compare(q, ln.fp)
            if hit:
                level, score, why = hit
                age = ((date.today() - ln.quote_date).days / 365) if ln.quote_date else 2
                score += 0.15 * (ln.status.lower() == "won") - 0.1 * min(age, 3) + 0.05 * (ln.unit_price is not None)
                found.append(Match(level, ln, score, why))
        # One entry per past RFQ and part: the best line of each.
        best: Dict[Tuple[str, str], Match] = {}
        for m in found:
            key = (m.line.rfq_row_id, m.line.fp.text.lower())
            if key not in best or m.score > best[key].score:
                best[key] = m
        # The same line quoted again unchanged (same part, quantity and price) shows once: the latest.
        latest: Dict[Tuple[str, Any, Any, str], Match] = {}
        for m in best.values():
            ln = m.line
            key2 = (ln.fp.text.lower(), ln.qty, round(ln.unit_price, 6) if ln.unit_price is not None else None, ln.currency)
            have = latest.get(key2)
            if have is None or (ln.quote_date or date.min) > (have.line.quote_date or date.min):
                latest[key2] = m
        ranked = sorted(latest.values(), key=lambda m: (m.level != SAME, -m.score))
        if any(m.level == SAME for m in ranked):        # the same part first; similar only beside it
            ranked = [m for m in ranked if m.level == SAME] + [m for m in ranked if m.level != SAME][:1]
        return ranked[:limit]


# ----------------------------------------------------------------------------- a new RFQ's products

@dataclass
class Wanted:
    """One thing from the new RFQ to look up: a single product, or one row of a family."""
    product: str            # the product line it belongs to
    label: str              # what the reader sees
    fp: Fingerprint


def wanted_from_extraction(extraction: Any, max_family_rows: int = 60) -> List[Wanted]:
    out: List[Wanted] = []
    for p in getattr(extraction, "products", None) or []:
        name = str(getattr(p, "name", "") or "").strip()
        specs = getattr(p, "specs", None)
        sp = {k: str(getattr(specs, k, "") or "") for k in ("material", "grade_standard", "finish", "key_dimensions", "drawing_no")} \
            if specs else {}
        annexure = getattr(p, "annexure", None)
        rows = []
        if annexure is not None:
            cols = [str(c) for c in (getattr(annexure, "columns", None) or [])]
            for r in (getattr(annexure, "rows", None) or [])[:max_family_rows]:
                rows.append(dict(zip(cols, r)) if isinstance(r, (list, tuple)) else dict(r or {}))
        if rows:
            for r in rows:
                text = " ".join(str(v) for k, v in r.items() if v and not re.search(r"qty|quantity|price|note|doubt|weight", k, re.I))
                label = str(r.get("description") or r.get("part_number") or text)[:80]
                out.append(Wanted(name, label, fingerprint(f"{name} {text}", str(r.get("part_number") or r.get("drawing_ref") or ""),
                                                           str(r.get("material") or sp.get("material", "")),
                                                           str(r.get("finish") or sp.get("finish", "")))))
        else:
            text = " ".join([name] + [sp.get(k, "") for k in ("grade_standard", "key_dimensions")])
            out.append(Wanted(name, name, fingerprint(text, sp.get("drawing_no", ""), sp.get("material", ""), sp.get("finish", ""))))
    return out


# ----------------------------------------------------------------------------- loading

_lock = threading.Lock()
_cache: Dict[str, Any] = {"index": None, "at": 0.0}
_TABLE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)?$")


def _find_table(conn: Any) -> str:
    """The one table that has the quote-line columns, when PAST_QUOTES_TABLE is not set."""
    needed = ("row_id", "part_name", "total_value", "quotation_folder_link", "current_status")
    with conn.cursor() as cur:
        cur.execute(
            "SELECT table_schema, table_name FROM information_schema.columns WHERE column_name = ANY(%s) "
            "AND table_schema NOT IN ('pg_catalog', 'information_schema') "
            "GROUP BY table_schema, table_name HAVING count(DISTINCT column_name) = %s",
            (list(needed), len(needed)))
        found = cur.fetchall()
    if len(found) != 1:
        raise LookupError(f"set PAST_QUOTES_TABLE — {len(found)} tables have the quote-line columns")
    return f"{found[0][0]}.{found[0][1]}"


def _read_postgres(url: str, table: str) -> List[Dict[str, Any]]:
    import psycopg
    from psycopg import sql

    # The session is read-only whatever the URL's user may do: this code never writes.
    with psycopg.connect(url, connect_timeout=15,
                         options="-c default_transaction_read_only=on -c statement_timeout=30000") as conn:
        table = table or _find_table(conn)
        if not _TABLE.match(table):
            raise ValueError(f"PAST_QUOTES_TABLE {table!r} is not a plain [schema.]table name")
        ident = sql.Identifier(*table.split("."))
        query = sql.SQL("SELECT {} FROM {}").format(sql.SQL(", ").join(sql.Identifier(c) for c in COLUMNS), ident)
        with conn.cursor() as cur:
            cur.execute(query)
            names = [d.name for d in cur.description]
            return [dict(zip(names, row)) for row in cur.fetchall()]


def load_index(settings: Any, *, force: bool = False) -> Optional[PastQuoteIndex]:
    """The cached index, re-read from Postgres when older than PAST_QUOTES_REFRESH_HOURS."""
    url = (getattr(settings, "past_quotes_db_url", "") or "").strip()
    table = (getattr(settings, "past_quotes_table", "") or "").strip()
    if not url:
        print("[WARN] past quotes | PAST_QUOTES_DB_URL not set — skipped")
        return None
    ttl = float(getattr(settings, "past_quotes_refresh_hours", 24) or 24) * 3600
    with _lock:
        if not force and _cache["index"] is not None and time.time() - _cache["at"] < ttl:
            return _cache["index"]
        t0 = time.perf_counter()
        try:
            rows = _read_postgres(url, table)
        except Exception as e:
            print(f"[WARN] past quotes | could not read {table or 'the quote table'}: {type(e).__name__}: {e}")
            return _cache["index"]          # keep serving the last good copy
        index = PastQuoteIndex(clean_rows(rows))
        _cache.update(index=index, at=time.time())
        print(f"[INFO] past quotes | loaded {len(index.lines)} product line(s) from {len(rows)} row(s) "
              f"in {int((time.perf_counter() - t0) * 1000)} ms")
        return index


# ----------------------------------------------------------------------------- the section

SECTION_HEADING = "#### 🔁 Quoted before"
MAX_RFQS = 8                   # past RFQs listed; the rest are counted
_SECTION_RX = re.compile(r"\n*---\n+" + re.escape(SECTION_HEADING) + r".*?(?=\n</triage>|\Z)", re.DOTALL)


def render_section(results: List[Tuple[Wanted, List[Match]]]) -> str:
    """
    One bullet per past RFQ: its title linked to its OneDrive quotation folder,
    then which of this RFQ's products it matches. Empty when nothing is the same
    or closely similar.
    """
    rfqs: Dict[str, Dict[str, Any]] = {}
    for w, ms in results:
        for m in ms:
            ln = m.line
            r = rfqs.setdefault(ln.rfq_row_id, {"title": ln.title, "link": ln.folder_link, "products": {},
                                                "date": ln.quote_date or date.min})
            have = r["products"].get(w.product)
            if have != SAME:                       # same beats similar for a product
                r["products"][w.product] = m.level
    if not rfqs:
        return ""
    ranked = sorted(rfqs.values(), key=lambda r: (SAME not in r["products"].values(), -len(r["products"]),
                                                 -r["date"].toordinal()))
    out = ["---", "", SECTION_HEADING, ""]
    for r in ranked[:MAX_RFQS]:
        title = r["title"] or "Untitled RFQ"
        head = f"[{title}]({r['link']})" if r["link"] else title
        products = sorted(r["products"].items(), key=lambda kv: kv[1] != SAME)
        out.append(f"- {head} — " + ", ".join(f"{name} ({level})" for name, level in products))
    if len(ranked) > MAX_RFQS:
        out.append(f"- …and {len(ranked) - MAX_RFQS} more past RFQ(s)")
    return "\n".join(out)


def attach_section(summary: str, section: str) -> str:
    """Put the section at the end of the summary, inside its <triage> tag, replacing any earlier one."""
    base = strip_section(summary)
    if not section:
        return base
    if "</triage>" in base:
        head, tail = base.rsplit("</triage>", 1)
        return f"{head.rstrip()}\n\n{section}\n</triage>{tail}"
    return f"{base.rstrip()}\n\n{section}"


def strip_section(summary: str) -> str:
    return _SECTION_RX.sub("", summary or "")


def extract_section(summary: str) -> str:
    m = _SECTION_RX.search(summary or "")
    return m.group(0).strip() if m else ""
