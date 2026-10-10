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
]
lines = pq.clean_rows(ROWS)
by = {(l.rfq_row_id, l.part_name[:20]): l for l in lines}

# ---- cleaning ----------------------------------------------------------------
check("freight and tooling lines are not products", not any(l.part_name in ("Air Freight", "One -Time Tooling Cost") for l in lines))
r1 = by[("R1", "M10 x 120 Hex Bolt D")]
check("the swapped quantity columns are read by what they hold", r1.qty == 1000 and r1.unit == "nos")
check("the RFQ's OneDrive quotation folder is kept", r1.folder_link == "https://onedrive.example/R1")
check("a missing unit price is total ÷ quantity", abs(r1.unit_price - 0.25) < 1e-9)
check("an RFQ with a freight line is marked as quoting freight separately", r1.freight_quoted_separately)
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

# ---- what a part is ------------------------------------------------------------
fp = pq.fingerprint('M10 x 120 Hex Bolt DIN 931 8.8 HDG')
check("type, material, finish, sizes and standard are read from the text",
      fp.ptype == "bolt" and fp.material == "Alloy steel" and fp.finish == "HDG"
      and {"m10", "l120", "pc8.8"} <= fp.sizes and "din931" in fp.standards, str(fp))
rod = pq.fingerprint("Spherical Rod End 5/16-24 Female LH, Zinc")
check("inch threads are sized and typed", rod.ptype == "rod end" and "0.3125in" in rod.sizes and rod.system == "inch", str(rod.sizes))
check("grades and standards are not taken for part numbers",
      not pq.fingerprint('8" PIPE, SCH. 10S, ASTM A312, GR. TP316L, ASME B36.10M').codes)
check("a real part code in the text is", "MTWST00122480" in pq.fingerprint("Bolzen MT_WST00122480").codes)
check("STD and schedule 40 are the same wall", "sch40" in pq.fingerprint('2" Tee STD').sizes
      and "sch40" in pq.fingerprint('2" Tee SCH 40').sizes)
check("ASME B16.5 is not the bolt grade B16", pq.fingerprint('4" Blind Flange A105 ASME B16.5 Carbon Steel').material == "Carbon steel")

# ---- matching --------------------------------------------------------------------
index = pq.PastQuoteIndex(lines)
ms = index.find(pq.fingerprint('M10 x 120 Hex Bolt DIN 931 Grade 8.8 Hot dip galvanised'))
check("the same bolt is found as the same part, the won quote first",
      ms and ms[0].level == "same" and ms[0].line.rfq_row_id == "R1", str([(m.level, m.line.rfq_row_id) for m in ms]))
check("the same part in stainless is not the same part", all(m.line.rfq_row_id != "R6" for m in ms))
check("a socket head cap screw is not a hex bolt", all("Socket" not in m.line.part_name for m in ms))
check("at most one similar item is shown beside the same part",
      sum(m.level == "similar" for m in ms) <= 1 and any(m.line.rfq_row_id == "R2" and m.level == "similar" for m in ms),
      str([(m.level, m.line.rfq_row_id) for m in ms]))
only_similar = index.find(pq.fingerprint('M12 x 110 Hex Bolt DIN 931 8.8 HDG'), exclude_rfq="R2")
check("one size away is similar, not the same", only_similar and all(m.level == "similar" for m in only_similar),
      str([(m.level, m.line.part_name) for m in only_similar]))
check("two sizes away is not shown", not index.find(pq.fingerprint('M20 x 120 Hex Bolt DIN 931 8.8 HDG')))
fl = index.find(pq.fingerprint('4" Weld Neck Flange RF 150# A105 B16.5'))
check("flange style and pressure class must agree",
      [m.line.part_name for m in fl] == ['4" WN RF Flange, ASTM A105, Class 150, ASME B16.5'], str([m.line.part_name for m in fl]))
code = index.find(pq.fingerprint("Lid", "MT-BGR00110836-brushed"))
check("the same part number is the same part, whatever it is called",
      {m.line.rfq_row_id for m in code} == {"R5", "R6"} and all(m.level == "same" for m in code))
check("two different part numbers are never the same part",
      not index.find(pq.fingerprint("Deckel", "MT_BGR00999999")))
check("the RFQ being quoted is left out of its own past", all(m.line.rfq_row_id != "R1" for m in
      index.find(pq.fingerprint('M10 x 120 Hex Bolt DIN 931 8.8 HDG'), exclude_rfq="R1")))
check("a vague name matches nothing", not index.find(pq.fingerprint("Hex Head Cap Screw")))

# ---- the section ------------------------------------------------------------------
w = pq.Wanted("Hex Bolts M10", "Hex Bolts M10", pq.fingerprint('M10 x 120 Hex Bolt DIN 931 8.8 HDG'))
w2 = pq.Wanted("Flanges (family)", "4in WN", pq.fingerprint('4" Weld Neck Flange RF 150# A105 B16.5'))
sec = pq.render_section([(w, index.find(w.fp)), (w2, index.find(w2.fp))])
bullets = [x for x in sec.splitlines() if x.startswith("- ")]
check("one bullet per past RFQ, under the heading", sec.startswith("---") and pq.SECTION_HEADING in sec
      and len(bullets) == 4, sec)
check("each title links to its OneDrive quotation folder",
      bullets[0].startswith("- [Proj R1 - Parts](https://onedrive.example/R1)"), bullets[0])
check("…and says which of this RFQ's products match, at product level",
      bullets[0].endswith("— Hex Bolts M10 (same)"), bullets[0])
check("RFQs with the same part come before those with only a similar one",
      [re.match(r"- \[?(.*?)(\]\(.*?\))? — ", b).group(1) for b in bullets]
      == ["Proj R1 - Parts", "Proj R3 - Parts", "Proj R4 - Parts", "Proj R2 - Parts"],
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
        for i in range(11)]
check("at most eight past RFQs, then a count", pq.render_section(lots).count("\n- [T") == 8
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
check("specs feed the lookup", wanted[0].fp.material == "Alloy steel" and "m10" in wanted[0].fp.sizes
      and wanted[1].fp.material == "Carbon steel" and "wn" in wanted[1].fp.subtypes, str(wanted[1].fp))


# ---- the writeback step -------------------------------------------------------------
def _settings(**kw):
    base = dict(GLIDE_API_KEY="k", GLIDE_APP_ID="a", GLIDE_ZAI_REGENERATE_TABLE="t",
                GLIDE_COL_ZAI_REGENERATE_RESPONSE="resp", ENABLE_PAST_QUOTES="true",
                PAST_QUOTES_DB_URL="postgresql://x", PAST_QUOTES_TABLE="public.quotes")
    base.update(kw)
    return Settings(**base)


def run(settings, *, index_obj=index, ext_obj=ext):
    calls = {"set": []}
    out = TriageOutputPayload(run_id="r1", row_id="NEW", triage_text=summary,
                              structured={"regenerate_row_id": "REGEN1"})
    out.product_extraction = ext_obj
    orig = (pq.load_index, writer.glide_set_regenerate_response)
    pq.load_index = lambda s, **k: index_obj
    writer.glide_set_regenerate_response = lambda s, rid, text: calls["set"].append((rid, text)) or True
    try:
        n = writer.write_past_quotes(settings, out)
    finally:
        pq.load_index, writer.glide_set_regenerate_response = orig
    return n, out, calls


n, out, calls = run(_settings())
check("matches are written onto the summary row already in Glide",
      n == 2 and calls["set"] and calls["set"][0][0] == "REGEN1" and pq.SECTION_HEADING in calls["set"][0][1],
      str((n, calls)))
check("…and kept on the run's output", pq.SECTION_HEADING in out.triage_text)
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

print("\nALL PASSED" if ok else "\nFAILURES ABOVE")
