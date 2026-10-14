"""
past_quotes.py — the same or closely similar products we have quoted before,
and the past suppliers who could make the new ones.

The past quotes live in one Postgres table: a row per line of every quotation
we sent. This module reads it and, for each product of a new RFQ, finds:

  same        the part we quoted before: the same part number, or the same
              part type, size, material and finish
  similar     the same kind of product, ranked by how close it is: its style
              first, then a differentiating feature, then size, grade,
              standard, class and finish
  capability  a different part whose supplier has the capability this one
              needs (a rubber moulder for a moulded rubber part)

There is no hand-written list of part types, styles, materials or finishes
anywhere in this module. What a part *is* comes from the model's reading of
each line (capabilities.py); the only rules here read measurements — thread
and nominal sizes, lengths, schedules, pressure and property classes — and
compare them. Until a line has been read, it can still match on its part
number or a near-identical description, nothing looser.

The team sees one line per past RFQ at the end of the RFQ summary: its title,
linked to its OneDrive quotation folder, and which of this RFQ's products it
matches. The customer's name is read only to guess a missing currency from
their other quotes; it is never shown.
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
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Set, Tuple

from . import capabilities as cap

# ----------------------------------------------------------------------------- the table

# The export's two quantity columns are swapped: `unit_of_quantity` holds the
# number and `quantity_in_unit` the unit. Read them by what they hold.
COLUMNS = (
    "row_id", "line_no", "title", "customer_name", "rfq_sequence", "current_status", "quote_margin",
    "final_quote_number", "document_details", "part_name", "part_number", "remarks",
    "unit_of_quantity", "quantity_in_unit", "total_value", "ex_works_unit_price", "currency",
    "ex_works", "ex_works_unit", "lead_time", "unit_of_lead_time", "created_at", "quotation_folder_link",
)


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
    folder_link: str = ""                             # the RFQ's OneDrive quotation folder
    fp: "Fingerprint" = None  # type: ignore[assignment]
    tags: Optional[Dict[str, Any]] = None             # the model's profile, once read

    @property
    def tag_text(self) -> str:
        """What the model is shown — the RFQ title gives the part its context."""
        return f"RFQ: {self.title} — {self.part_name} | {self.remarks}"[:cap.TEXT_LIMIT]

    @property
    def tag_key(self) -> str:
        return cap.text_key(self.tag_text)


_INVISIBLE = re.compile(r"[​-‏‪-‮⁠-⁩﻿]")


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
    """Raw table rows -> quote lines with a unit price, a currency and a date."""
    rows = [dict(r) for r in rows]
    # Currency fallbacks: the RFQ's totals block, then its other lines, then the
    # customer's usual currency (marked as an assumption).
    by_rfq: Dict[str, Counter] = defaultdict(Counter)
    by_customer: Dict[str, Counter] = defaultdict(Counter)
    for r in rows:
        cur = _null(r.get("currency")).upper()
        if cur:
            by_rfq[_null(r.get("row_id"))][cur] += 1
            by_customer[_null(r.get("customer_name")).lower()][cur] += 1

    out: List[PastLine] = []
    for r in rows:
        name = _null(r.get("part_name"))
        if not name:
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
            margin_pct=margin, lead_weeks=lead,
            folder_link=link if (link := _null(r.get("quotation_folder_link"))).startswith("http") else "",
        )
        line.fp = fingerprint(f"{name} {line.remarks}", line.part_number)
        out.append(line)
    return out


# ----------------------------------------------------------------------------- measurements
#
# The only rules in this module: reading sizes. They are measurements, not
# categories — a thread is M10 whatever the part is called. Strength and
# property classes (8.8, A4-80) are part of the material, read by the model.

_METRIC = re.compile(r"\bm\s?(\d{1,3}(?:\.\d+)?)(?:\s*[x×]\s*(\d+(?:\.\d+)?)(?:\s*mm)?)?(?:\s*[x×]\s*(\d{2,4}(?:\.\d+)?)(?:\s*mm)?)?(?![\w.])",
                     re.IGNORECASE)
_INCH = re.compile(r"(?<![\d/.])(\d+(?:[\s-]+\d+/\d+)?|\d+/\d+|\d*\.\d+)\s*(?:\"|”|″|''|in\b|inch(?:es)?\b|-(?=\d+\s*(?:unc|unf|un|tpi)\b))",
                   re.IGNORECASE)
_THREAD_INCH = re.compile(r"(?<![\d/.])(\d+(?:[\s-]+\d+/\d+)?|\d+/\d+)\s*-\s*([4-9]|[1-8]\d)(?![/\d.])(?!\s*mm)", re.IGNORECASE)
_MM = re.compile(r"(?<![\d.])(\d+(?:\.\d+)?)\s*mm\b", re.IGNORECASE)
_DIA = re.compile(r"[Øø⌀]\s*(\d+(?:\.\d+)?)(?!\s*(?:\"|in\b))")       # Ø38 is millimetres
_DN = re.compile(r"\b(?:dn|nb)\s*(\d{1,4})\b", re.IGNORECASE)
_SCHEDULE = re.compile(r"\bsch(?:edule)?\.?\s*(\d{1,3}s?|xxs|xs|std)\b|\b(std|xs|xxs)\b", re.IGNORECASE)
# Pipe wall: STD is schedule 40 and XS schedule 80 up to 10" (a unit equivalence,
# like inches to millimetres); the S suffix only says stainless.
_SCHEDULE_SAME = {"std": "40", "xs": "80", "xxs": "160"}
_RATING = re.compile(r"(?:\bclass|\bcl\.?|#)\s*(\d{3,4})\b|\b(\d{3,4})\s*(?:#|lbs?\b)", re.IGNORECASE)


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
    # measured from the text
    sizes: Set[str] = field(default_factory=set)       # m10, l120, 0.3125in, sch40, cl150 …
    main_mm: Optional[float] = None                   # the one size that says how big the part is
    length_mm: Optional[float] = None
    system: str = ""                                  # "metric" | "inch"
    words: Set[str] = field(default_factory=set)       # the description's words
    numbers: Set[str] = field(default_factory=set)     # every number in it: sizes, grades, standards
    codes: Set[str] = field(default_factory=set)      # the customer's part / drawing numbers, normalised
    # from the model's reading (apply_profile)
    tagged: bool = False
    charge: bool = False
    kind: str = ""
    ptype: str = ""
    material: str = ""
    finish: str = ""
    standards: Set[str] = field(default_factory=set)
    features: Set[str] = field(default_factory=set)


def norm_code(code: str) -> str:
    """'MT_WST00122480', 'mt-wst 00122480 rev B' -> 'MTWST00122480'."""
    c = re.sub(r"\s*(rev(ision)?\.?\s*[a-z0-9]{1,3})\s*$", "", str(code or ""), flags=re.IGNORECASE)
    return re.sub(r"[^A-Z0-9]", "", c.upper())


def _add_code(fp: Fingerprint, code: str) -> None:
    n = norm_code(code)
    # A part number is long enough to be specific: a bare "77" is a line reference.
    if len(n) >= 6 and not n.isdigit() or len(n) >= 8:
        fp.codes.add(n)


def _number(n: str) -> str:
    """'08' and '8', '8.80' and '8.8' are the same number."""
    whole, _, frac = n.partition(".")
    return (whole.lstrip("0") or "0") + ("." + frac.rstrip("0") if frac.rstrip("0") else "")


def fingerprint(text: str, part_number: str = "") -> Fingerprint:
    """The measurements, words and numbers of a description, and its part number."""
    raw = re.sub(r"\s+", " ", str(text or "")).strip()
    t = raw.replace("×", "x").replace("”", '"').replace("″", '"').replace(",", " ")
    fp = Fingerprint(text=raw)
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
    for m in _MM.findall(t) + _DIA.findall(t):
        if f"{float(m):g}mm" not in fp.sizes:
            fp.sizes.add(f"{float(m):g}mm")
            sizes_mm.append(float(m))
    for m in _DN.findall(t):
        fp.sizes.add(f"dn{int(m)}")
        sizes_mm.append(float(m))
    for a, b in _SCHEDULE.findall(t):
        v = (a or b).lower()
        v = v if v in ("xs", "xxs") else v.rstrip("s")
        fp.sizes.add("sch" + _SCHEDULE_SAME.get(v, v))
    for a, b in _RATING.findall(t):
        fp.sizes.add("cl" + (a or b))
    fp.main_mm = sizes_mm[0] if sizes_mm else None
    if any(x.startswith("m") and x[1:2].isdigit() for x in fp.sizes):
        fp.system = "metric"
    elif any(x.endswith("in") for x in fp.sizes):
        fp.system = "inch"
    _add_code(fp, part_number)
    fp.words = {w for w in re.findall(r"[a-z][a-z0-9]+", t.lower()) if len(w) > 2}
    fp.numbers = {_number(n) for n in re.findall(r"(?<![\d.])\d+(?:\.\d+)?", t)}
    return fp


def apply_profile(fp: Fingerprint, profile: Optional[Dict[str, Any]]) -> None:
    """What the model read about the part: its type, grade, finish, standards, features."""
    if profile is None:
        return
    fp.tagged = True
    fp.charge = bool(profile.get("charge"))
    fp.kind = profile.get("kind") or ""
    fp.ptype = profile.get("part_type") or ""
    fp.material = profile.get("material") or ""
    fp.finish = (profile.get("coating") or [""])[0]
    fp.standards = set(profile.get("standards") or [])
    fp.features = set(profile.get("feature") or [])
    if profile.get("part_code"):
        _add_code(fp, profile["part_code"])


apply_tags = apply_profile          # older name


def _agreement(a: Set[str], b: Set[str]) -> str:
    """'same' when the two lists share a name, '' when either is empty, else 'different'."""
    if not (a and b):
        return ""
    return "same" if any(cap.tags_match(x, y) for x in a for y in b) else "different"


def _features_agree(a: Set[str], b: Set[str]) -> bool:
    return all(any(cap.tags_match(x, y) for y in b) for x in a) and \
        all(any(cap.tags_match(y, x) for x in a) for y in b)


def _jaccard(a: Set[str], b: Set[str]) -> float:
    return len(a & b) / len(a | b) if a and b else 0.0


def _contains(a: str, b: str) -> bool:
    ta, tb = cap.tag_tokens(a), cap.tag_tokens(b)
    return bool(ta and tb) and (ta <= tb or tb <= ta)


def type_rank(q: "Fingerprint", p: "Fingerprint") -> int:
    """3 the same part type, 2 one is the other with a style added, 1 the same kind only, 0 another product."""
    rel = cap.relation(q.ptype, p.ptype)
    if rel == "same":
        return 3
    if rel == "close":
        return 2
    return 1 if _contains(q.kind or q.ptype, p.kind or p.ptype) else 0


# ----------------------------------------------------------------------------- matching

SAME, SIMILAR = "same", "similar"
CANDIDATES = 20                # past lines ranked per item; the summary shows the best few


@dataclass
class Match:
    level: str
    line: PastLine
    score: float
    why: str


def compare(q: Fingerprint, p: Fingerprint) -> Optional[Tuple[str, float, str]]:
    """
    (level, score, why) for a past part against a new one; None when it is not
    the same kind of product.

    Only the kind of product decides whether a past part is shown. Everything
    else ranks it, in this order of weight: the product type (the same style
    ahead of another style of the same kind), then a feature that sets parts
    apart (a weld projection, a locking insert), then size, grade, standard,
    class, finish and wording. 'Same' is the part itself — the same part number,
    or the same type, feature, grade and sizes with nothing else differing.
    Before both lines have been read, only a shared part number or a
    near-identical description (the same words and every number equal) counts.
    """
    if q.charge or p.charge:
        return None
    shared_codes = q.codes & p.codes
    if shared_codes:
        return SAME, 1000.0, f"same part number {sorted(shared_codes)[0]}"
    words = _jaccard(q.words, p.words)
    if not (q.tagged and p.tagged):
        if (words >= 0.85 and len(q.words) >= 3 and (q.numbers or q.codes) and q.numbers == p.numbers
                and q.sizes == p.sizes and not (q.codes and p.codes)):
            return SAME, 900.0 + words, "same description"
        return None
    rank = type_rank(q, p)
    if not rank:
        return None
    features = _features_agree(q.features, p.features)

    # Size: how close the main size is (M10 vs M12 closer than M10 vs M20), and the length.
    sizes_equal = bool(q.sizes) and q.sizes == p.sizes
    if sizes_equal:
        size = 1.0
    elif q.main_mm and p.main_mm:
        size = 0.7 * min(q.main_mm, p.main_mm) / max(q.main_mm, p.main_mm)
        size += 0.3 * (min(q.length_mm, p.length_mm) / max(q.length_mm, p.length_mm)
                       if q.length_mm and p.length_mm else 0.5)
    else:
        size = 0.3                             # nothing to compare
    system = not (q.system and p.system and q.system != p.system)
    material = cap.relation(q.material, p.material)
    standards = _agreement(q.standards, p.standards)
    finish = _agreement({q.finish} - {""}, {p.finish} - {""})
    rating = ""
    for prefix in ("cl", "sch"):
        a = {x for x in q.sizes if x.startswith(prefix)}
        b = {x for x in p.sizes if x.startswith(prefix)}
        if a and b:
            rating = "same" if a & b and rating != "different" else "different"

    score = 100.0 * rank + 10.0 * features
    score += 3.0 * size * (1 if system else 0.6)
    score += 2.0 * {"same": 1.0, "close": 0.6, "": 0.4}.get(material, 0.0)
    score += 1.0 * {"same": 1.0, "": 0.5}.get(standards, 0.0)
    score += 1.0 * (rating != "different")
    score += 0.5 * {"same": 1.0, "": 0.5}.get(finish, 0.0)
    score += 1.0 * words

    differs = [name for name, d in (
        ("style", rank < 3), ("feature", not features), ("size", not sizes_equal and bool(q.sizes or p.sizes)),
        ("inch/metric", not system), ("grade", material == "different"), ("standard", standards == "different"),
        ("class", rating == "different"), ("finish", finish == "different")) if d]
    if (rank == 3 and features and sizes_equal and material == "same" and not differs
            and not (q.codes and p.codes)):
        return SAME, 500.0 + score, "same part type, size, material" + (", finish" if finish == "same" else "")
    return SIMILAR, score, ("differs in " + ", ".join(differs)) if differs else "same part type"


@dataclass
class CapMatch:
    line: PastLine
    score: float
    shared: List[str]          # the capabilities the past part and the new one have in common


class PastQuoteIndex:
    def __init__(self, lines: List[PastLine]):
        self.lines = lines
        self.by_code: Dict[str, List[PastLine]] = defaultdict(list)
        self.by_word: Dict[str, List[PastLine]] = defaultdict(list)
        self.by_type: Dict[str, List[PastLine]] = defaultdict(list)
        self.by_token: Dict[str, List[PastLine]] = defaultdict(list)
        for ln in lines:
            for c in ln.fp.codes:
                self.by_code[c].append(ln)
            for w in ln.fp.words:
                self.by_word[w].append(ln)

    def apply_tags(self, profiles: Dict[str, Dict[str, Any]]) -> int:
        """Attach the model's readings to the lines and rebuild the lookups. Returns lines read."""
        self.by_type = defaultdict(list)
        self.by_token = defaultdict(list)
        n = 0
        for ln in self.lines:
            prof = profiles.get(ln.tag_key)
            if prof is None:
                continue
            ln.tags = prof
            apply_profile(ln.fp, prof)
            for c in ln.fp.codes:
                if ln not in self.by_code[c]:
                    self.by_code[c].append(ln)
            n += 1
            if ln.fp.charge:
                continue
            for tok in cap.tag_tokens(ln.fp.kind) | cap.tag_tokens(ln.fp.ptype):
                self.by_type[tok].append(ln)
            for t in cap.all_tags(prof):
                for tok in cap.tag_tokens(t):
                    self.by_token[tok].append(ln)
        return n

    def find(self, q: Fingerprint, *, exclude_rfq: str = "", limit: int = CANDIDATES) -> List[Match]:
        cands: Dict[int, PastLine] = {}
        for c in q.codes:
            for ln in self.by_code.get(c, []):
                cands[id(ln)] = ln
        for w in q.words:                          # near-identical descriptions
            for ln in self.by_word.get(w, []):
                cands[id(ln)] = ln
        for tok in cap.tag_tokens(q.kind) | cap.tag_tokens(q.ptype):    # the same kind of product, once read
            for ln in self.by_type.get(tok, []):
                cands[id(ln)] = ln
        found: List[Match] = []
        for ln in cands.values():
            if exclude_rfq and ln.rfq_row_id == exclude_rfq:
                continue
            hit = compare(q, ln.fp)
            if hit:
                level, score, why = hit
                age = ((date.today() - ln.quote_date).days / 365) if ln.quote_date else 2
                score += 0.15 * (ln.status.lower() == "won") - 0.1 * min(age, 3)
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
        return ranked[:limit]                      # the same part first, then the closest

    def find_capability(self, tags: Optional[Dict[str, Any]], level: Callable[[str], str], *,
                        exclude_rfqs: Set[str] = frozenset(), limit: int = 2) -> List[CapMatch]:
        """
        Past parts whose suppliers can do what this part needs — any part type.

        A past part is a lead when it shares a capability that is specialist or
        rare among suppliers (judged on the market, not on how often we quote it),
        or when it shares the part's whole combination of process, material and
        coating. A part with a special feature needs a supplier who made that
        feature. Leads rank by how much specialist capability they share, then a
        won quote, then the most recent.
        """
        wanted = sorted(cap.all_tags(tags))
        if not wanted:
            return []
        features = set((tags or {}).get("feature") or [])
        combo = {t for kind in ("process", "special_material", "coating") for t in (tags or {}).get(kind) or []}
        weight = {"rare": 3, "specialist": 2}
        cands: Dict[int, PastLine] = {}
        for t in wanted:
            for tok in cap.tag_tokens(t):
                for ln in self.by_token.get(tok, []):
                    cands[id(ln)] = ln
        best: Dict[str, CapMatch] = {}
        for ln in cands.values():
            if ln.rfq_row_id in exclude_rfqs or ln.fp.charge:
                continue
            have = cap.all_tags(ln.tags)
            shared = [t for t in wanted if any(cap.tags_match(t, h) for h in have)]
            if not shared:
                continue
            if features and not any(t in features for t in shared):
                continue
            special = [t for t in shared if weight.get(level(t))]
            whole_combo = len(combo) >= 2 and combo <= set(shared)
            if not special and not whole_combo:
                continue
            age = ((date.today() - ln.quote_date).days / 365) if ln.quote_date else 3
            score = sum(weight.get(level(t), 0) for t in shared) + 0.5 * len(shared) \
                + 0.3 * (ln.status.lower() == "won") - 0.1 * min(age, 3)
            cur = best.get(ln.rfq_row_id)
            if cur is None or score > cur.score:
                best[ln.rfq_row_id] = CapMatch(ln, score, special or shared)
        return sorted(best.values(), key=lambda m: -m.score)[:limit]


# ----------------------------------------------------------------------------- a new RFQ's products

@dataclass
class Wanted:
    """One thing from the new RFQ to look up: a single product, or one row of a family."""
    product: str            # the product line it belongs to
    label: str              # what the reader sees
    fp: Fingerprint
    text: str = ""          # what the model is shown to read it

    @property
    def key(self) -> str:
        return cap.text_key(self.text or self.fp.text)


def wanted_from_extraction(extraction: Any, max_family_rows: int = 500) -> List[Wanted]:
    out: List[Wanted] = []
    for p in getattr(extraction, "products", None) or []:
        name = str(getattr(p, "name", "") or "").strip()
        specs = getattr(p, "specs", None)
        sp = {k: str(getattr(specs, k, "") or "") for k in ("material", "grade_standard", "finish", "key_dimensions", "drawing_no")} \
            if specs else {}
        processes = [str(x) for x in (getattr(specs, "processes", None) or []) if str(x).strip()] if specs else []
        if processes:
            sp["processes"] = " → ".join(processes)
        spec_text = " | ".join(f"{k.replace('_', ' ')}: {v}" for k, v in sp.items() if v)
        annexure = getattr(p, "annexure", None)
        rows = []
        if annexure is not None:
            cols = [str(c) for c in (getattr(annexure, "columns", None) or [])]
            for r in (getattr(annexure, "rows", None) or [])[:max_family_rows]:
                rows.append(dict(zip(cols, r)) if isinstance(r, (list, tuple)) else dict(r or {}))
        if rows:
            for r in rows:
                cells = " ".join(str(v) for k, v in r.items() if v and not re.search(r"qty|quantity|price|note|doubt|weight", k, re.I))
                label = str(r.get("description") or r.get("part_number") or cells)[:80]
                code = str(r.get("part_number") or r.get("drawing_ref") or "")
                # Measured from the row alone (the family's name may span sizes); the model sees both.
                out.append(Wanted(name, label, fingerprint(cells, code),
                                  text=f"{name} — {cells} | {spec_text}"[:cap.TEXT_LIMIT]))
        else:
            text = " ".join([name, sp.get("grade_standard", ""), sp.get("key_dimensions", "")])
            out.append(Wanted(name, name, fingerprint(text, sp.get("drawing_no", "")),
                              text=f"{name} | {spec_text}"[:cap.TEXT_LIMIT]))
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
            index = _cache["index"]
            if getattr(settings, "enable_past_quotes_capability", False):
                _start_backfill(settings, index)        # picks up anything a failed run left unread
            return index
        t0 = time.perf_counter()
        try:
            rows = _read_postgres(url, table)
        except Exception as e:
            print(f"[WARN] past quotes | could not read {table or 'the quote table'}: {type(e).__name__}: {e}")
            return _cache["index"]          # keep serving the last good copy
        index = PastQuoteIndex(clean_rows(rows))
        _cache.update(index=index, at=time.time())
        print(f"[INFO] past quotes | loaded {len(index.lines)} line(s) from {len(rows)} row(s) "
              f"in {int((time.perf_counter() - t0) * 1000)} ms")
        read = index.apply_tags(cap.store_for(settings).tags)
        if read:
            print(f"[INFO] past quotes | model readings known for {read} of {len(index.lines)} line(s)")
        if getattr(settings, "enable_past_quotes_capability", False):
            _start_backfill(settings, index)
        return index


_backfill_lock = threading.Lock()
_backfill_retry_at = {"t": 0.0}


def _start_backfill(settings: Any, index: "PastQuoteIndex") -> None:
    """Have the model read the past lines not read yet, in the background; matching uses whatever is ready."""
    todo = {ln.tag_key: ln.tag_text for ln in index.lines if ln.tags is None}
    levels = cap.levels_for(settings)
    unrated = any(not levels.level(t) for ln in index.lines for t in cap.all_tags(ln.tags))
    if not (todo or unrated) or time.time() < _backfill_retry_at["t"]:
        return
    if not _backfill_lock.acquire(blocking=False):
        return

    def run():
        try:
            print(f"[INFO] past quotes | reading {len(todo)} past line(s) in the background")
            found = cap.tag_texts(settings, todo, run_id="past-quotes-backfill")
            n = index.apply_tags(cap.store_for(settings).tags)
            cap.rate_tags(settings, {t for ln in index.lines for t in cap.all_tags(ln.tags)},
                          run_id="past-quotes-backfill")
            print(f"[INFO] past quotes | model readings ready: {n} of {len(index.lines)} line(s) "
                  f"({len(found)} read now)")
            for ln in [l for l in index.lines if l.tags and not l.fp.charge][:3]:
                print(f"[INFO] past quotes | e.g. {ln.part_name[:50]!r} -> type={ln.fp.ptype!r} "
                      f"material={ln.fp.material!r} capabilities={sorted(cap.all_tags(ln.tags))}")
            if len(found) < len(todo):
                _backfill_retry_at["t"] = time.time() + 1800     # some batches failed: try again in 30 min
        finally:
            _backfill_lock.release()

    threading.Thread(target=run, name="past-quotes-backfill", daemon=True).start()


# ----------------------------------------------------------------------------- the section

SECTION_HEADING = "#### 🔁 Quoted before"
MAX_RFQS = 15                  # past RFQs listed in the section; the rest are counted
_SECTION_RX = re.compile(r"\n*---\n+" + re.escape(SECTION_HEADING) + r".*?(?=\n</triage>|\Z)", re.DOTALL)


def render_section(results: List[Tuple[Wanted, List[Match]]],
                   capability: Sequence[Tuple[str, CapMatch]] = ()) -> str:
    """
    One bullet per past RFQ: its title linked to its OneDrive quotation folder,
    then which of this RFQ's products it matches — and for a similar one, what
    differs. Empty when nothing qualifies.
    """
    rank = {SAME: 0, SIMILAR: 1}
    rfqs: Dict[str, Dict[str, Any]] = {}

    def entry(ln: PastLine) -> Dict[str, Any]:
        return rfqs.setdefault(ln.rfq_row_id, {"title": ln.title, "link": ln.folder_link, "products": {},
                                               "best": (9, 0.0)})

    for w, ms in results:
        for m in ms:
            r = entry(m.line)
            have = r["products"].get(w.product)
            key = (rank[m.level], -m.score)
            if have is None or key < have[0]:          # a product's best match in this RFQ
                label = m.level if m.level == SAME or not m.why.startswith("differs") else f"{m.level} — {m.why}"
                r["products"][w.product] = (key, label)
                r["best"] = min(r["best"], key)
    for product, cm in capability:
        r = entry(cm.line)
        if product not in r["products"]:
            r["products"][product] = ((2, -cm.score), "capability: " + ", ".join(cm.shared[:2]))
            r["best"] = min(r["best"], (2, -cm.score))
    if not rfqs:
        return ""
    ranked = sorted(rfqs.values(), key=lambda r: (r["best"][0], -len(r["products"]), r["best"][1]))
    out = ["---", "", SECTION_HEADING, ""]
    for r in ranked[:MAX_RFQS]:
        title = r["title"] or "Untitled RFQ"
        head = f"[{title}]({r['link']})" if r["link"] else title
        products = sorted(r["products"].items(), key=lambda kv: kv[1][0])
        out.append(f"- {head} — " + ", ".join(f"{name} ({label})" for name, (_, label) in products))
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
