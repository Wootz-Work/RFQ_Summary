"""
test_past_quotes.py — "Quoted before" at the end of the RFQ summary.

The rules that matter: only the same part or one size away is ever shown, the
same part first; prices stay in their own currency with a guessed currency
marked; charges are never products; the customer is never named; and a missing
database, an empty match or a Glide failure never touches the summary.

Run:
    python test_past_quotes.py
"""
import re
import sys
import types

sys.path.insert(0, "src")

for name in ("fitz", "google", "google.oauth2", "google.oauth2.service_account",
             "google.api_core", "google.api_core.client_options",
             "google.cloud", "google.cloud.documentai_v1",
             "googleapiclient", "googleapiclient.discovery", "googleapiclient.http",
             "googleapiclient.errors"):
    sys.modules.setdefault(name, types.ModuleType(name))
sys.modules["google.oauth2.service_account"].Credentials = object
sys.modules["google.api_core.client_options"].ClientOptions = object
sys.modules["google.cloud.documentai_v1"].DocumentProcessorServiceClient = object
sys.modules["googleapiclient.discovery"].build = lambda *a, **k: None
sys.modules["googleapiclient.http"].MediaIoBaseDownload = object
sys.modules["googleapiclient.errors"].HttpError = Exception

from rfq_summary import capabilities as cap
from rfq_summary import past_quotes as pq
from rfq_summary import writer
from rfq_summary.config import Settings
from rfq_summary.schema import ExtractedProduct, ProductAnnexure, ProductExtractionResult, TriageOutputPayload

ok = True


def check(label, cond, detail=""):
    global ok
    ok &= bool(cond)
    print(("PASS  " if cond else "FAIL  ") + label + (f"  — {detail}" if detail and not cond else ""))


def row(rfq, name, qty="100.0000", unit="nos", total="NULL", price="NULL", cur="EUR", remarks="NULL",
        part_number="NULL", status="Quoted", customer="Alpha", margin="20.0000", date="2026-05-10",
        incoterm="EXW", title=None, folder="NULL"):
    return {
        "row_id": rfq, "line_no": "1", "title": title or f"Proj {rfq} - Parts", "customer_name": customer,
        "rfq_sequence": "1", "current_status": status, "quote_margin": margin, "final_quote_number": f"WZ-{rfq}",
        "document_details": '{"date": "%s", "incoterms": "%s"}' % (date, incoterm),
        "part_name": name, "part_number": part_number, "remarks": remarks,
        "unit_of_quantity": qty, "quantity_in_unit": unit, "total_value": total, "ex_works_unit_price": price,
        "currency": cur, "ex_works": "8.0000", "ex_works_unit": "weeks", "lead_time": "NULL",
        "unit_of_lead_time": "NULL", "created_at": "2026-09-24 06:00:00+00", "quotation_folder_link": folder,
    }


ROWS = [
    row("R1", 'M10 x 120 Hex Bolt DIN 931 8.8 HDG', qty="1000.0000", total="250.0000", status="Won",
        folder="https://onedrive.example/R1"),
    row("R1", "Air Freight", qty="NULL", unit="NULL", total="139.19"),
    row("R1", "One -Time Tooling Cost", qty="1.0000", total="500"),
    row("R2", 'M12 x 120 Hex Bolt DIN 931 8.8 HDG', qty="500.0000", total="180.0000", cur="NULL", customer="Alpha"),
    row("R3", 'M10 x 120 Hex Bolt DIN 931 8.8 HDG', qty="2000.0000", total="420.0000", cur="NULL", customer="Beta",
        margin="0.1500", incoterm="DAP"),
    row("R3", 'M10 x 120 Socket Head Cap Screw DIN 912 12.9', qty="100.0000", total="90", cur="USD", customer="Beta"),
    row("R4", '4" WN RF Flange, ASTM A105, Class 150, ASME B16.5', qty="10", total="400", cur="USD"),
    row("R4", '4" SO RF Flange, ASTM A105, Class 150, ASME B16.5', qty="10", total="300", cur="USD"),
    row("R4", '4" WN RF Flange, ASTM A105, Class 300, ASME B16.5', qty="10", total="500", cur="USD"),
    row("R5", "Deckel", part_number="MT_BGR00110836-brushed", qty="50", price="12.5", cur="EUR"),
    row("R6", "Cover plate", part_number="MT_BGR00110836-brushed", qty="80", price="11.9", cur="EUR"),
    row("R6", 'M10 x 120 Hex Bolt DIN 931 8.8 HDG', remarks="MOC- A4 Stainless Steel", qty="300", total="600", cur="GBP"),
    row("R7", "Bolzen MT_WST00122480", qty="40", total="80", cur="EUR"),
]
lines = pq.clean_rows(ROWS)
by = {(l.rfq_row_id, l.part_name[:20]): l for l in lines}


def P(part_type="", material="", standards=(), part_code="", charge=False, kind=None, **caps):
    """A profile as the model returns it (the kind defaults to the type's last word)."""
    kind = part_type.split()[-1] if kind is None and part_type else (kind or "")
    return cap.clean_profile(dict(kind=kind, part_type=part_type, material=material, standards=list(standards),
                                  part_code=part_code, charge=charge, **{k: list(v) for k, v in caps.items()}))


def Q(text, profile=None, code=""):
    """A new RFQ item: its measurements, and the model's reading when given."""
    fp = pq.fingerprint(text, code)
    pq.apply_profile(fp, profile)
    return fp


BOLT = P("hex bolt", "alloy steel 8.8", ["din 931"], process=["cold heading"], coating=["hot dip galvanising"])
WN = P("weld neck flange", "carbon steel a105", ["asme b16.5"], process=["forging", "cnc turning"])
# The model's reading of each past line (what capabilities.tag_texts would return).
READINGS = {
    ("R1", "M10 x 120 Hex Bolt D"): BOLT, ("R2", "M12 x 120 Hex Bolt D"): BOLT, ("R3", "M10 x 120 Hex Bolt D"): BOLT,
    ("R1", "Air Freight"): P(charge=True), ("R1", "One -Time Tooling Co"): P(charge=True),
    ("R3", "M10 x 120 Socket Hea"): P("socket head cap screw", "alloy steel 12.9", ["din 912"], process=["cold heading"]),
    ("R4", '4" WN RF Flange, AST'): WN,
    ("R4", '4" SO RF Flange, AST'): P("slip-on flange", "carbon steel a105", ["asme b16.5"], process=["forging"]),
    ("R5", "Deckel"): P("machined cover", "aluminium 6061", process=["cnc milling"]),
    ("R6", "Cover plate"): P("cover plate", "aluminium 6061", kind="cover", process=["cnc milling"]),
    ("R6", "M10 x 120 Hex Bolt D"): P("hex bolt", "stainless steel a4-80", ["din 931"], process=["cold heading"]),
    ("R7", "Bolzen MT_WST0012248"): P("pin", "steel", part_code="MT_WST00122480", process=["cnc turning"]),
}
profiles = {l.tag_key: READINGS[(l.rfq_row_id, l.part_name[:20])] for l in lines}

# ---- cleaning ----------------------------------------------------------------
r1 = by[("R1", "M10 x 120 Hex Bolt D")]
check("the swapped quantity columns are read by what they hold", r1.qty == 1000 and r1.unit == "nos")
check("the RFQ's OneDrive quotation folder is kept", r1.folder_link == "https://onedrive.example/R1")
check("a missing unit price is total ÷ quantity", abs(r1.unit_price - 0.25) < 1e-9)
r2 = by[("R2", "M12 x 120 Hex Bolt D")]
check("a missing currency takes the customer's usual one, marked as assumed",
      r2.currency == "EUR" and r2.currency_assumed, f"{r2.currency} {r2.currency_assumed}")
r3 = by[("R3", "M10 x 120 Hex Bolt D")]
check("a currency stated on another line of the same RFQ is not an assumption", r3.currency == "USD" and not r3.currency_assumed)
check("a margin stored as a fraction is read as a percentage", r3.margin_pct == 15)
check("the quote date comes from the quote document", r1.quote_date.isoformat() == "2026-05-10")
check("the customer's name is not kept on a past line", not hasattr(r1, "customer_name"))
check("invisible direction marks from PDFs are cleaned out of names",
      pq.clean_rows([row("R9", "Brass\u202dHex\u202dFull\u202dNut M8")])[0].part_name == "Brass Hex Full Nut M8")
check("the model is shown the RFQ title with the part", r1.tag_text.startswith("RFQ: Proj R1 - Parts — M10 x 120 Hex Bolt"))

# ---- measurements: the only rules ---------------------------------------------------
fp = pq.fingerprint('M10 x 120 Hex Bolt DIN 931 8.8 HDG')
check("thread size and length are measured", {"m10", "l120"} <= fp.sizes and fp.main_mm == 10 and fp.system == "metric",
      str(fp.sizes))
check("…and every number is kept, so 8.8 and 10.9 never read the same", fp.numbers == {"10", "120", "931", "8.8"},
      str(fp.numbers))
rod = pq.fingerprint("Spherical Rod End 5/16-24 Female LH, Zinc")
check("inch threads are sized", "0.3125in" in rod.sizes and rod.system == "inch", str(rod.sizes))
check("STD and schedule 40 are the same wall", "sch40" in pq.fingerprint('2" Tee STD').sizes
      and "sch40" in pq.fingerprint('2" Tee SCH 40').sizes)
check("a diameter mark is millimetres", "38mm" in pq.fingerprint("Moulded Rubber Part Ø38").sizes)
check("pressure class is measured", "cl150" in pq.fingerprint('4" Flange 150#').sizes
      and "cl300" in pq.fingerprint("Flange Class 300").sizes)
check("grades and standards in the text are not taken for part numbers",
      not pq.fingerprint('8" PIPE, SCH. 10S, ASTM A312, GR. TP316L, ASME B36.10M').codes)
check("the part number column is", "MTBGR00110836BRUSHED" in by[("R5", "Deckel")].fp.codes)
check("no list of part types, materials or finishes is left in the matcher",
      not any(hasattr(pq, n) for n in ("_TYPES", "_SUBTYPES", "_FINISHES", "_NOT_A_PRODUCT", "_STOP", "material_rate")))

# ---- before the model has read the lines -------------------------------------------
raw = pq.PastQuoteIndex(pq.clean_rows(ROWS))
ms = raw.find(Q('M10 x 120 Hex Bolt DIN 931 8.8 HDG'))
check("unread, the same description is found as the same part",
      {m.line.rfq_row_id for m in ms} == {"R1", "R3"} and all(m.level == "same" for m in ms),
      str([(m.level, m.line.rfq_row_id) for m in ms]))
check("…but not the stainless one, whose remarks differ, nor the M12", all(m.line.rfq_row_id not in ("R6", "R2") for m in ms))
check("unread, one number different is a different description",
      not raw.find(Q('M10 x 120 Hex Bolt DIN 931 10.9 HDG')))
check("unread, the same part number still matches", {m.line.rfq_row_id for m in raw.find(Q("Lid", code="MT-BGR00110836-brushed"))}
      == {"R5", "R6"})
check("unread, nothing looser: a similar size is not offered", not raw.find(Q('M12 x 110 Hex Bolt DIN 931 8.8 HDG')))

# ---- once read ---------------------------------------------------------------------------
index = pq.PastQuoteIndex(lines)
check("the model's readings are attached to every line", index.apply_tags(profiles) == len(lines))
freight = by[("R1", "Air Freight")]
check("a line the model reads as a charge is never a product", freight.fp.charge and pq.compare(freight.fp, freight.fp) is None)
ms = index.find(Q('M10 x 120 Hex Bolt DIN 931 Grade 8.8 Hot dip galvanised', BOLT))
order = [(m.level, m.line.rfq_row_id) for m in ms]
check("the same bolt is found as the same part, the won quote first",
      order[:2] == [("same", "R1"), ("same", "R3")], str(order))
check("then the closest: one size away ahead of another grade at the same size",
      order[2:] == [("similar", "R2"), ("similar", "R6")], str(order))
check("…each saying what differs", [m.why for m in ms[2:]] == ["differs in size", "differs in grade, finish"]
      or [m.why for m in ms[2:]] == ["differs in size", "differs in grade"], str([m.why for m in ms]))
check("a socket head cap screw is not a bolt, so it is not shown", all("Socket" not in m.line.part_name for m in ms))
far = index.find(Q('M20 x 120 Hex Bolt DIN 931 8.8 HDG', BOLT))
check("two sizes away is still shown, ranked by how close it is (M12 ahead of M10 for an M20)",
      far and all(m.level == "similar" for m in far) and far[0].line.rfq_row_id == "R2", str([(m.line.rfq_row_id, m.score) for m in far]))
cls = index.find(Q('M10 x 120 Hex Bolt DIN 931 10.9 HDG', P("hex bolt", "alloy steel 10.9", ["din 931"])))
check("another strength class is shown as similar, never the same",
      cls and all(m.level == "similar" and "grade" in m.why for m in cls), str([(m.level, m.why) for m in cls]))
loose = index.find(Q('M10 x 120 Hex Bolt DIN 931', P("hex bolt", "alloy steel", ["din 931"])))
check("a grade stated on one side only is similar, ranked above another grade",
      loose and all(m.level == "similar" for m in loose)
      and [m.line.rfq_row_id for m in loose].index("R6") > [m.line.rfq_row_id for m in loose].index("R1"),
      str([(m.line.rfq_row_id, m.why) for m in loose]))
iso = index.find(Q('M10 x 120 Hex Bolt ISO 4017 8.8 HDG', P("hex bolt", "alloy steel 8.8", ["iso 4017"])))
check("another product standard is similar, not the same",
      iso and all(m.level == "similar" and "standard" in m.why for m in iso), str([(m.level, m.why) for m in iso]))
fl = index.find(Q('4" Weld Neck Flange RF 150# A105 B16.5', WN))
check("flanges rank by style and class: the same flange, then another class, then another style",
      [(m.level, m.line.part_name[:16], m.why) for m in fl] == [
          ("same", '4" WN RF Flange,', "same part type, size, material"),
          ("similar", '4" WN RF Flange,', "differs in size, class"),
          ("similar", '4" SO RF Flange,', "differs in style")], str([(m.level, m.line.part_name, m.why) for m in fl]))
code = index.find(Q("Lid", P("machined cover", "aluminium 6061"), code="MT-BGR00110836-brushed"))
check("the same part number is the same part, whatever it is called",
      {m.line.rfq_row_id for m in code} == {"R5", "R6"} and all(m.level == "same" for m in code))
other = index.find(Q("Deckel", P("machined cover", "aluminium 6061"), code="MT_BGR00999999"))
check("another part number is never the same part — but the same kind of part is still similar",
      other and all(m.level == "similar" for m in other) and other[0].line.rfq_row_id == "R5",
      str([(m.level, m.line.rfq_row_id, m.why) for m in other]))
check("a part number the model found in the name is matched too",
      [m.line.rfq_row_id for m in index.find(Q("Pin", code="MT-WST00122480"))] == ["R7"])
check("the RFQ being quoted is left out of its own past", all(m.line.rfq_row_id != "R1" for m in
      index.find(Q('M10 x 120 Hex Bolt DIN 931 8.8 HDG', BOLT), exclude_rfq="R1")))
check("up to twenty candidates are ranked behind the scenes", pq.CANDIDATES == 20)
check("names relate by their words: a style added is close, a different grade or standard is not",
      cap.relation("rod end", "spherical rod end") == "close" and cap.relation("hex bolt", "hex bolt") == "same"
      and cap.relation("alloy steel 8.8", "alloy steel 10.9") == "different"
      and cap.relation("asme b16.5", "asme b16.9") == "different" and cap.relation("", "x y") == "")

# ---- the section ------------------------------------------------------------------
w = pq.Wanted("Hex Bolts M10", "Hex Bolts M10", Q('M10 x 120 Hex Bolt DIN 931 8.8 HDG', BOLT))
w2 = pq.Wanted("Flanges (family)", "4in WN", Q('4" Weld Neck Flange RF 150# A105 B16.5', WN))
sec = pq.render_section([(w, index.find(w.fp)), (w2, index.find(w2.fp))])
bullets = [x for x in sec.splitlines() if x.startswith("- ")]
check("one bullet per past RFQ, under the heading", sec.startswith("---") and pq.SECTION_HEADING in sec
      and len(bullets) == 5, sec)
check("each title links to its OneDrive quotation folder",
      bullets[0].startswith("- [Proj R1 - Parts](https://onedrive.example/R1)"), bullets[0])
check("…and says which of this RFQ's products match, at product level",
      bullets[0].endswith("— Hex Bolts M10 (same)"), bullets[0])
check("a similar one says what differs", bullets[3].endswith("— Hex Bolts M10 (similar — differs in size)"), bullets[3])
check("RFQs with the same part come before those with only a similar one",
      [re.match(r"- \[?(.*?)(\]\(.*?\))? — ", b).group(1) for b in bullets]
      == ["Proj R1 - Parts", "Proj R3 - Parts", "Proj R4 - Parts", "Proj R2 - Parts", "Proj R6 - Parts"],
      str(bullets))
check("no prices, quantities or suppliers — just the pointer",
      "EUR" not in sec and "@" not in sec and "margin" not in sec and "Shared with" not in sec, sec)
check("the customer is never named", "Alpha" not in sec and "Beta" not in sec, sec)
check("a past RFQ without a folder link shows its title plain",
      pq.render_section([(w, [pq.Match("same", by[("R3", "M10 x 120 Hex Bolt D")], 2, "x")])]).splitlines()[-1]
      .startswith("- Proj R3 - Parts — "))
check("nothing close means no section at all", pq.render_section([(w, [])]) == "")
lots = [(pq.Wanted(f"P{i}", f"P{i}", w.fp), [pq.Match("same", pq.PastLine(**{**by[("R1", "M10 x 120 Hex Bolt D")].__dict__,
                                                                         "rfq_row_id": f"X{i}", "title": f"T{i}"}), 2, "x")])
        for i in range(18)]
check("at most fifteen past RFQs, then a count", pq.render_section(lots).count("\n- [T") == 15
      and "and 3 more past RFQ(s)" in pq.render_section(lots))

summary = "<triage>\n**Two lines.**\n\n| a | b |\n</triage>"
joined = pq.attach_section(summary, sec)
check("the section goes at the end, inside the triage tag",
      joined.endswith("</triage>") and joined.index(pq.SECTION_HEADING) > joined.index("| a | b |"), joined[-200:])
check("adding it twice replaces, never duplicates", pq.attach_section(joined, sec).count(pq.SECTION_HEADING) == 1)
check("it can be lifted out and stripped again", pq.extract_section(joined).startswith("---")
      and pq.strip_section(joined).strip() == summary)

# ---- from an extraction ------------------------------------------------------------
ext = ProductExtractionResult(products=[
    ExtractedProduct(index=1, name="Hex Bolt M10 x 120 — 8.8 HDG", structure="single", quantity="1,000 pcs",
                     specs={"material": "Alloy steel", "grade_standard": "DIN 931 8.8", "finish": "HDG",
                            "key_dimensions": "M10 × 120"}),
    ExtractedProduct(index=2, name="Flanges (family)", structure="family", quantity="As per annexure",
                     annexure=ProductAnnexure(required=True, columns=["description", "material", "quantity"],
                                              rows=[['4" WN RF Flange Class 150 B16.5', "A105", "10"]])),
])
wanted = pq.wanted_from_extraction(ext)
check("a single product is one lookup, a family one per row", [x.product for x in wanted] == [
    "Hex Bolt M10 x 120 — 8.8 HDG", "Flanges (family)"])
check("the model is shown each item with its specs", "material: Alloy steel" in wanted[0].text
      and "finish: HDG" in wanted[0].text and '4" WN RF Flange Class 150 B16.5 A105' in wanted[1].text, str(wanted[1].text))
check("…and its measurements are read, without the quantity", "m10" in wanted[0].fp.sizes
      and {"4.0000in", "cl150"} <= wanted[1].fp.sizes and "10" not in wanted[1].fp.numbers, str(wanted[1].fp.numbers))
READ_NEW = {wanted[0].key: BOLT, wanted[1].key: WN}
routed = pq.wanted_from_extraction(ProductExtractionResult(products=[ExtractedProduct(
    index=1, name="Spherical Rod End 5/16-24", structure="single", quantity="200 pcs",
    specs={"material": "Steel", "processes": ["Cold heading", "Machining", "Swaging"]})]))
check("the extraction's process route is part of what the model reads",
      "processes: Cold heading → Machining → Swaging" in routed[0].text, routed[0].text)
long_family = ProductExtractionResult(products=[ExtractedProduct(
    index=1, name="Washers (family)", structure="family", quantity="As per annexure",
    annexure=ProductAnnexure(required=True, columns=["description"], rows=[[f"M{i} washer"] for i in range(120)]))])
check("every row of a long family is looked up (120 of 120)", len(pq.wanted_from_extraction(long_family)) == 120)
check("…up to the configured limit", len(pq.wanted_from_extraction(long_family, 50)) == 50
      and Settings().past_quotes_max_rows == 500)


# ---- the writeback step -------------------------------------------------------------
def _settings(**kw):
    base = dict(GLIDE_API_KEY="k", GLIDE_APP_ID="a", GLIDE_ZAI_REGENERATE_TABLE="t",
                GLIDE_COL_ZAI_REGENERATE_RESPONSE="resp", ENABLE_PAST_QUOTES="true",
                PAST_QUOTES_DB_URL="postgresql://x", PAST_QUOTES_TABLE="public.quotes", PAST_QUOTES_TAG_CACHE_PATH="")
    base.update(kw)
    return Settings(**base)


def no_model(*a, **k):
    raise AssertionError("the model must not be called while capability is switched off")


def run(settings, *, index_obj=index, ext_obj=ext, reader=no_model, levels=None):
    calls = {"set": [], "read": []}
    out = TriageOutputPayload(run_id="r1", row_id="NEW", triage_text=summary,
                              structured={"regenerate_row_id": "REGEN1"})
    out.product_extraction = ext_obj
    orig = (pq.load_index, writer.glide_set_regenerate_response, cap.tag_texts, cap.rate_tags)
    pq.load_index = lambda s, **k: index_obj
    writer.glide_set_regenerate_response = lambda s, rid, text: calls["set"].append((rid, text)) or True
    cap.tag_texts = lambda s, texts, **k: calls["read"].append(dict(texts)) or reader(s, texts, **k)
    cap.rate_tags = lambda s, tags, **k: cap.levels_for(s).put({t: (levels or {}).get(t, "common") for t in tags})
    try:
        n = writer.write_past_quotes(settings, out)
    finally:
        pq.load_index, writer.glide_set_regenerate_response, cap.tag_texts, cap.rate_tags = orig
    return n, out, calls


n, out, calls = run(_settings())
check("capability off: no model call, and only the same description matches (the bolt)",
      n == 1 and not calls["read"] and "Proj R1 - Parts" in calls["set"][0][1] and "Proj R4" not in calls["set"][0][1],
      str((n, calls["set"])))
n, out, calls = run(_settings(ENABLE_PAST_QUOTES_CAPABILITY="true"),
                    reader=lambda s, texts, **k: {key: READ_NEW[key] for key in texts if key in READ_NEW})
check("capability on: each item is read once, by its own text",
      len(calls["read"]) == 1 and set(calls["read"][0]) == set(READ_NEW), str(calls["read"]))
check("…and its reading finds the same parts, written onto the summary row already in Glide",
      n == 2 and calls["set"] and calls["set"][0][0] == "REGEN1" and pq.SECTION_HEADING in calls["set"][0][1]
      and "Proj R4 - Parts" in calls["set"][0][1], str((n, calls["set"])))
check("…and kept on the run's output", pq.SECTION_HEADING in out.triage_text)
fam = ProductExtractionResult(products=[ExtractedProduct(
    index=1, name="Hex bolts (family)", structure="family", quantity="As per annexure",
    annexure=ProductAnnexure(required=True, columns=["description"],
                             rows=[["M10 x 120 Hex Bolt DIN 931 8.8 HDG"], ["M12 x 120 Hex Bolt DIN 931 8.8 HDG"]]))])
n, out, calls = run(_settings(PAST_QUOTES_PER_PRODUCT="1"), ext_obj=fam)
check("the per-product limit holds across a family's rows, the same part first",
      n >= 1 and len([x for x in calls["set"][0][1].splitlines() if x.startswith("- ")]) == 1
      and "Proj R1 - Parts" in calls["set"][0][1], calls["set"][0][1][-300:] if calls["set"] else str(n))
n, out, calls = run(_settings(ENABLE_PAST_QUOTES="false"))
check("switched off: nothing looked up, nothing written", n == 0 and not calls["set"] and out.triage_text == summary)
n, out, calls = run(_settings(), index_obj=None)
check("no database: the summary is left alone", n == 0 and not calls["set"] and out.triage_text == summary)
n, out, calls = run(_settings(), index_obj=pq.PastQuoteIndex([]))
check("nothing close: the summary is left alone", n == 0 and not calls["set"])


check("past quotes are off by default", not Settings().enable_past_quotes)


class _Cur:
    def __init__(self, found): self.found = found
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def execute(self, q, params=None): self.q = q
    def fetchall(self): return self.found


class _Conn:
    def __init__(self, found): self.found = found
    def cursor(self): return _Cur(self.found)


check("with no table set, the one table with the quote-line columns is used",
      pq._find_table(_Conn([("public", "rfq_quote_lines")])) == "public.rfq_quote_lines")
try:
    pq._find_table(_Conn([("a", "x"), ("b", "y")]))
    check("two candidate tables ask for PAST_QUOTES_TABLE instead of guessing", False)
except LookupError as e:
    check("two candidate tables ask for PAST_QUOTES_TABLE instead of guessing", "PAST_QUOTES_TABLE" in str(e))

# ---- regeneration keeps the section ---------------------------------------------------
from rfq_summary import task

prev = pq.attach_section(summary, sec)
payload = types.SimpleNamespace(previous_response=prev, rfq_id="NEW")
fresh = "<triage>\n**Regenerated.**\n</triage>"
kept = task._carry_past_quotes(_settings(), "r2", payload, fresh)
check("a regenerated summary keeps the Quoted before section",
      "**Regenerated.**" in kept and kept.count(pq.SECTION_HEADING) == 1 and kept.endswith("</triage>"), kept[-120:])
check("…and stays as it was when the previous version had none",
      task._carry_past_quotes(_settings(), "r2", types.SimpleNamespace(previous_response=summary, rfq_id="NEW"), fresh) == fresh)

# ---- capability: who can make it -------------------------------------------------------
import json
import os
import tempfile

check("capability names match loosely, never across different things",
      cap.tags_match("rubber moulding", "rubber compression moulding") and cap.tags_match("zinc flake", "zinc flake coating")
      and not cap.tags_match("rubber moulding", "plastic injection moulding")
      and not cap.tags_match("cnc turning", "cnc milling"))
check("a reply is read as JSON, lower-cased, with nothing dropped by a list",
      cap.parse_reply('noise {"a": {"part_type": "Weld Washer", "material": "Mild Steel", "charge": "false", '
                      '"process": ["Stamping", "fastener"]}} tail')
      == {"a": {"kind": "", "part_type": "weld washer", "material": "mild steel", "standards": [], "part_code": "", "charge": False,
                "process": ["stamping", "fastener"], "feature": [], "coating": [], "special_material": []}})
check("an unreadable reply reads nothing", cap.parse_reply("sorry") == {})

crow = lambda rfq, name, title: row(rfq, name, title=title, folder=f"https://onedrive.example/{rfq}")
CAP_ROWS = [crow("G1", "Gasket, moulded rubber", "Shakti 27 - Aluminium Boxes"),
            crow("G2", "Bushing- Rubber Fully Bonded", "Entai 24 - Rubber-metal bonded component"),
            crow("G3", "HDPE moulded part", "FRUITLAND 34 - HDPE Molded Part"),
            crow("G4", "Woodgrip screw with bonded neoprene washer", "Entai 10 - Woodgrip Screws"),
            crow("G5", "M6 Weld Washer", "Weld 1 - Weld washers"),
            crow("G6", "M6 Flat Washer DIN 125", "Flat 1 - Washers"),
            crow("G7", "M6 Square Weld Nut", "Weld 2 - Weld nuts")] + \
           [crow(f"T{i}", f"Turned bush {i}", f"Turn {i} - Turned parts") for i in range(8)]
cl = pq.clean_rows(CAP_ROWS)
CAP_READ = {
    "Gasket, moulded rubber": P("gasket", "epdm rubber", process=["rubber moulding"], special_material=["epdm rubber"]),
    "Bushing- Rubber Fully Bonded": P("bonded rubber bush", "natural rubber",
                                      process=["rubber to metal bonding", "rubber moulding"]),
    "HDPE moulded part": P("hdpe moulded part", "hdpe", kind="plastic moulding", process=["plastic injection moulding"],
                           special_material=["hdpe"]),
    "Woodgrip screw with bonded neoprene washer": P("woodgrip screw", "carbon steel", process=["cold heading"],
                                                    coating=["zinc plating"]),
    "M6 Weld Washer": P("weld washer", "mild steel", process=["stamping"], feature=["projection weld nut"]),
    "M6 Flat Washer DIN 125": P("flat washer", "mild steel", ["din 125"], process=["stamping"]),
    "M6 Square Weld Nut": P("square weld nut", "mild steel", process=["cold heading"], feature=["projection weld nut"]),
}
store = {ln.tag_key: CAP_READ.get(ln.part_name, P("turned bush", "free cutting steel", process=["cnc turning"]))
         for ln in cl}
cidx = pq.PastQuoteIndex(cl)
check("known readings are attached to past lines", cidx.apply_tags(store) == len(cl))
# How hard each capability is to find among suppliers — the market, not our history.
LEVEL = {"rubber moulding": "specialist", "rubber compression moulding": "specialist", "rubber to metal bonding": "rare",
         "plastic injection moulding": "specialist", "epdm rubber": "specialist", "nbr rubber": "specialist",
         "projection weld nut": "specialist", "hastelloy": "rare", "cold heading": "common", "zinc plating": "common",
         "cnc turning": "common", "stamping": "common", "hdpe": "common", "free cutting brass": "common",
         "chrome plating": "common"}
level = lambda t: LEVEL.get(t, "")

rubber = P("moulded rubber part", "nbr rubber", kind="rubber moulding", process=["rubber compression moulding"],
           special_material=["nbr rubber"])
leads = cidx.find_capability(rubber, level)
check("a moulded rubber part finds the rubber moulders, best first",
      sorted(m.line.title for m in leads) == ["Entai 24 - Rubber-metal bonded component", "Shakti 27 - Aluminium Boxes"],
      str([(m.line.title, m.shared) for m in leads]))
check("…not the plastic moulder or the screw with a rubber washer",
      not any("HDPE" in m.line.title or "Woodgrip" in m.line.title for m in leads))
check("a common capability alone is not a lead",
      cidx.find_capability(P("turned bush", "steel", process=["cnc turning"]), level) == [])
weld = P("weld washer", "mild steel", process=["stamping"], feature=["projection weld nut"])
wl = cidx.find_capability(weld, level, exclude_rfqs={"G5"})
check("a weld washer is led to the weld nut supplier, not the flat washer",
      wl and wl[0].line.title == "Weld 2 - Weld nuts" and all("Flat" not in m.line.title for m in wl),
      str([(m.line.title, m.shared) for m in wl]))

# The two cases the DB-frequency rule got wrong.
many = pq.clean_rows([crow(f"H{i}", f"Hastelloy C276 part {i}", f"Trump {i} - Hastelloy") for i in range(10)] +
                     [crow("B1", "Brass turned bush", "Baba 1 - Brass")] +
                     [crow(f"S{i}", f"Steel bracket {i}", f"Steel {i} - Brackets") for i in range(5)])
mstore = {}
for ln in many:
    if "Hastelloy" in ln.part_name:
        t = P("machined part", "hastelloy c276", process=["cnc turning"], special_material=["hastelloy"])
    elif "Brass" in ln.part_name:
        t = P("turned bush", "free cutting brass", process=["cnc turning"], special_material=["free cutting brass"],
              coating=["chrome plating"])
    else:
        t = P("bracket", "mild steel", process=["sheet metal fabrication"])
    mstore[ln.tag_key] = t
midx = pq.PastQuoteIndex(many)
midx.apply_tags(mstore)
hl = midx.find_capability(P("machined part", "hastelloy c276", process=["cnc milling"], special_material=["hastelloy"]),
                          level, limit=5)
check("rare in the market but frequent in our quotes: still a lead (Hastelloy on 10 of 16 RFQs)",
      len(hl) == 5 and all("Hastelloy" in m.line.title for m in hl), str([m.line.title for m in hl]))
check("common in the market but rare in our quotes: not a lead (CNC turning on one RFQ)",
      midx.find_capability(P("pin", "mild steel", process=["cnc turning"]), level) == [])
combo = P("bush", "free cutting brass", process=["cnc turning"], special_material=["free cutting brass"],
          coating=["chrome plating"])
check("the whole combination of common capabilities is a lead (turned, brass, chrome plated)",
      [m.line.title for m in midx.find_capability(combo, level)] == ["Baba 1 - Brass"])
check("…but part of the combination is not",
      midx.find_capability(P("bush", "free cutting brass", process=["cnc turning"], special_material=["free cutting brass"],
                             coating=["nickel plating"]), level) == [])
check("up to the limit, and only leads that qualify",
      len(midx.find_capability(P("part", "hastelloy", special_material=["hastelloy"]), level, limit=5)) == 5
      and len(midx.find_capability(combo, level, limit=5)) == 1)

qf = Q("M6 Weld Washer", weld)
ff = next(l for l in cl if l.part_name == "M6 Flat Washer DIN 125").fp
flat_hit = pq.compare(qf, ff)
weld_hit = pq.compare(qf, next(l for l in cl if l.part_name == "M6 Weld Washer").fp)
check("a flat washer is still a washer for a weld washer: similar, saying the style and feature differ",
      flat_hit and flat_hit[0] == "similar" and flat_hit[2] == "differs in style, feature", str(flat_hit))
check("…ranked well below a weld washer", weld_hit and weld_hit[1] > flat_hit[1] + 10, str((weld_hit, flat_hit)))
check("a weld nut is another product: a capability lead, never a similar part",
      pq.compare(qf, next(l for l in cl if l.part_name == "M6 Square Weld Nut").fp) is None)

wr = pq.Wanted("Moulded Rubber Part Ø38", "Moulded Rubber Part Ø38", Q("Moulded Rubber Part Ø38 NBR 60 Shore A", rubber))
csec = pq.render_section([(wr, [])], [("Moulded Rubber Part Ø38", m) for m in leads])
check("capability leads are shown with the shared capability",
      "[Shakti 27 - Aluminium Boxes](https://onedrive.example/G1) — Moulded Rubber Part Ø38 (capability: rubber compression moulding)"
      in csec, csec)

# ---- the model's readings: cached, named consistently, never from a fixed list ------------
with tempfile.TemporaryDirectory() as d:
    path = os.path.join(d, "tags.json")
    s1 = cap.TagStore(path)
    s1.put({"k": P("gasket", "epdm rubber", process=["rubber moulding"])})
    check("the reading cache survives a restart", cap.TagStore(path).get("k")["process"] == ["rubber moulding"])
    old = os.path.join(d, "old.json")
    with open(old, "w") as f:
        json.dump({"k": {"process": ["rubber moulding"], "material": ["epdm rubber"]}}, f)
    check("a cache in the older format is read again rather than half-understood", cap.TagStore(old).get("k") is None)
    deep = cap.TagStore(os.path.join(d, "disk", "sub", "tags.json"))
    deep.put({"k": P(process=["x y"])})
    check("a cache folder that does not exist yet is created", os.path.exists(os.path.join(d, "disk", "sub", "tags.json")))

    vs = cap.TagStore("")
    vs.put({"a": P("weld washer", "mild steel", process=["stamping"]), "b": P("weld washer", "mild steel"),
            "c": P("hex bolt", "alloy steel 8.8", process=["cold heading"]), "d": P(charge=True)})
    voc = vs.vocabulary()
    check("the names in use are the history's own, most used first",
          voc["part_type"] == ["weld washer", "hex bolt"] and voc["material"][0] == "mild steel"
          and set(voc["process"]) == {"stamping", "cold heading"}, str(voc))

    import rfq_summary.llm as llm
    seen = {}
    orig_llm = (llm.generate_text, llm.load_prompt_file)
    llm.load_prompt_file = lambda path: open(path, encoding="utf-8").read()
    llm.generate_text = lambda st, system, user, **k: seen.update(system=system, user=user, k=k) or \
        '{"x1": {"part_type": "weld washer", "material": "mild steel"}}'
    try:
        got = cap._ask_model(_settings(), [{"id": "x1", "text": "M8 weld washer"}], "r", voc)
    finally:
        llm.generate_text, llm.load_prompt_file = orig_llm
    check("the model is shown the names in use, in place of the placeholder",
          "{{vocabulary}}" not in seen["system"] and '"weld washer"' in seen["system"]
          and got["x1"]["part_type"] == "weld washer", seen.get("system", "")[-300:])

    st_l = _settings(PAST_QUOTES_TAG_CACHE_PATH=os.path.join(d, "lv", "tags.json"))
    rated = []
    orig_lv = cap._ask_levels
    cap._ask_levels = lambda s, tags, run_id: rated.append(list(tags)) or {t: "specialist" for t in tags}
    try:
        cap.rate_tags(st_l, ["rubber moulding", "zinc flake coating"])
        cap.rate_tags(st_l, ["rubber moulding", "zinc flake"])       # 'zinc flake' matches a rated name
    finally:
        cap._ask_levels = orig_lv
    check("each capability is rated once; a matching name reuses the rating", len(rated) == 1, str(rated))
    check("ratings survive a restart", cap.LevelStore(os.path.join(d, "lv", "tags_scarcity.json")).level("rubber moulding")
          == "specialist")

    calls = []
    orig_ask = cap._ask_model
    cap._ask_model = lambda s, items, run_id, vocabulary=None: calls.append((len(items), dict(vocabulary or {}))) or \
        {i["id"]: P(f"part {i['text']}", "steel") for i in items}
    try:
        st = _settings(PAST_QUOTES_TAG_CACHE_PATH=os.path.join(d, "t2.json"))
        cap.tag_texts(st, {"a": "one", "b": "two"})
        cap.tag_texts(st, {"a": "one", "b": "two", "c": "three"})
        st3 = _settings(PAST_QUOTES_TAG_CACHE_PATH=os.path.join(d, "t3.json"))
        first = len(calls)
        cap.tag_texts(st3, {k: k for k in "pqrs"}, batch=1)
    finally:
        cap._ask_model = orig_ask
    check("each text is read once; the cache answers after that", [n for n, _ in calls[:first]] == [2, 1], str(calls))
    later = calls[first:]
    check("the first batch is read alone, and the rest are shown the names it chose",
          len(later) == 4 and not later[0][1] and all("part p" in v.get("part_type", []) for _, v in later[1:]),
          str(later))


def run_cap(enabled, reader):
    calls = {"set": []}
    out = TriageOutputPayload(run_id="r9", row_id="NEW", triage_text=summary, structured={"regenerate_row_id": "REGEN9"})
    out.product_extraction = ProductExtractionResult(products=[ExtractedProduct(
        index=1, name="Moulded Rubber Part Ø38", structure="single", quantity="500 pcs",
        specs={"material": "NBR", "grade_standard": "60 Shore A"})])
    orig = (pq.load_index, writer.glide_set_regenerate_response, cap.tag_texts, cap.rate_tags)
    pq.load_index = lambda s, **k: cidx
    writer.glide_set_regenerate_response = lambda s, rid, text: calls["set"].append(text) or True
    cap.tag_texts = reader
    st = _settings(ENABLE_PAST_QUOTES_CAPABILITY=str(enabled).lower())
    cap.rate_tags = lambda s, tags, **k: cap.levels_for(st).put({t: LEVEL[t] for t in tags if t in LEVEL})
    try:
        n = writer.write_past_quotes(st, out)
    finally:
        pq.load_index, writer.glide_set_regenerate_response, cap.tag_texts, cap.rate_tags = orig
    return n, calls


cap.levels_for(_settings()).put({t: v for t, v in LEVEL.items()})
n, calls = run_cap(False, no_model)
check("capability switched off: no model call, and this item shows nothing", n == 0 and not calls["set"])
n, calls = run_cap(True, lambda s, texts, **k: {key: rubber for key in texts})
check("capability on: the rubber moulders reach the summary", n == 1 and calls["set"]
      and "capability: rubber compression moulding" in calls["set"][0], str(calls))
check("capability is off by default", not Settings().enable_past_quotes_capability)

print("\nALL PASSED" if ok else "\nFAILURES ABOVE")
