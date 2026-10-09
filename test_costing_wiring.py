"""
test_costing_wiring.py — the costing workbook in the product writeback.

The rules that matter: the extraction's rows reach the right tabs without any
bookkeeping columns, the uploaded file's id and link land in ttqlU / Vr8gz on
the RFQ row, and nothing in this path can cost an extraction that succeeded.

Run:
    python test_costing_wiring.py
"""
import io
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

import openpyxl

from rfq_summary import writer
from rfq_summary.config import Settings
from rfq_summary.costing_workbook import tabs_from_extraction
from rfq_summary.onedrive import UploadedFile
from rfq_summary.schema import (
    ExtractedProduct, ProductAnnexure, ProductExtractionHeader, ProductExtractionResult, TriageOutputPayload,
)

ok = True


def check(label, cond, detail=""):
    global ok
    ok &= bool(cond)
    print(("PASS  " if cond else "FAIL  ") + label + (f"  — {detail}" if detail and not cond else ""))


def _settings(**kw):
    base = dict(GLIDE_API_KEY="k", GLIDE_APP_ID="a",
                MS_GRAPH_TENANT_ID="t", MS_GRAPH_CLIENT_ID="c", MS_GRAPH_CLIENT_SECRET="s",
                EMAIL_FROM_ADDRESS="technology@wootz.work")
    base.update(kw)
    return Settings(**base)


COLS = ["variant_ref", "sr_no", "part_number", "description", "finish", "quantity", "target_price"]
ROWS = [[f"V{i}", str(i + 1), f"HB-{i:03d}", f"Hex bolt M{8 + 2 * i}", "HDG", f"{100 * (i + 1)} pcs", "0.40"]
        for i in range(5)]


def extraction():
    return ProductExtractionResult(
        header=ProductExtractionHeader(rfq_title="Project Falcon - Fasteners"),
        products=[
            ExtractedProduct(index=1, name="Hex Bolts", structure="family", variant_count=5,
                             annexure=ProductAnnexure(required=True, columns=COLS, rows=ROWS)),
            ExtractedProduct(index=2, name="Threaded Stud M56", structure="single", quantity="1,200 pcs",
                             details="- Stud M56 x 310\n- ASTM A193 B7",
                             specs={"material": "Alloy steel", "grade_standard": "ASTM A193 B7", "finish": "",
                                    "key_dimensions": "M56 × 310", "drawing_no": "",
                                    "extra": {"Thread": "UNC", "Heat treatment": "Q&T"},
                                    "processes": ["Cutting", "machining", "Thread rolling", "Heat treatment"]},
                             provenance={"quantity": "derived", "material": "derived", "grade_standard": "verbatim",
                                         "finish": "unknown", "key_dimensions": "verbatim", "thread": "derived"}),
            ExtractedProduct(index=4, name="Half Coupling 1\" 304 SS", structure="single", quantity="170 pcs",
                             specs={"material": "304 SS", "grade_standard": "MSS SP-114", "key_dimensions": "1\"",
                                    "extra": {"thread": "NPT", "Pressure rating": "3000 lb"},
                                    "processes": "1. Forging > 2. Machining > Threading > Pickling & passivation > "
                                                 "Leak test > Marking > Grinding > Brushing > Assembly > Polishing"},
                             provenance={"material": "verbatim", "processes": "verbatim"}),
            ExtractedProduct(index=3, name="Their Washers", structure="family", quantity="As per annexure",
                             annexure=ProductAnnexure(required=True, by_reference=True)),
        ],
    )


# ---- extraction -> tabs ----------------------------------------------------
tabs = tabs_from_extraction(extraction())
check("one tab per family with rows, one for everything else", [t.name for t in tabs] == ["Hex Bolts", "Individual items"],
      str([t.name for t in tabs]))
fam = tabs[0]
headers = [h for h, _ in fam.columns]
check("no made-up reference or serial number columns", not any(h.lower().startswith(("variant", "sr")) for h in headers),
      str(headers))
check("part number and description shown in plain words", headers[:2] == ["Part number", "Description"], str(headers))
check("quantity is split into number and unit, not a column", "Quantity" not in headers
      and fam.lines[2].qty.value == 300 and fam.lines[2].qty_unit == "pcs")
check("each row is labelled for the Quotation by code and description", fam.lines[0].label == "HB-000 Hex bolt M8",
      fam.lines[0].label)
check("a family all in one finish has one legend rate, named by it", fam.rate_groups == ["HDG"], str(fam.rate_groups))
check("every row is priced at that rate", {l.rate_group for l in fam.lines} == {"HDG"})

# A level-2 group mixes materials and finishes: one rate per combination, in first-seen order.
mixed = tabs_from_extraction(ProductExtractionResult(products=[ExtractedProduct(
    index=1, name="Micro Screws", structure="family", quantity="As per annexure",
    specs={"material": "316 SS"},
    annexure=ProductAnnexure(required=True, columns=["part_number", "type", "size", "Material", "Surface finish", "qty"],
                             rows=[["MS-1", "Pan head", "M2 x 4", "", "PVD black", "500"],
                                   ["MS-2", "Flat head", "M2 x 6", "", "PVD black", "500"],
                                   ["MS-3", "Pan head", "M3 x 6", "A2 SS", "Plain", "1000"],
                                   ["MS-4", "Pan head", "M3 x 8", "", "Plain", "1000"],
                                   ["MS-5", "Pan head", "M3 x 10", "A2 SS", "Plain", "1000"]]))]))[0]
check("a mixed family gets one rate per material and finish",
      mixed.rate_groups == ["316 SS · PVD black", "A2 SS · Plain", "316 SS · Plain"], str(mixed.rate_groups))
check("a row without its own material uses the family's", mixed.lines[0].rate_group == "316 SS · PVD black"
      and mixed.lines[3].rate_group == "316 SS · Plain", str([l.rate_group for l in mixed.lines]))
check("differing attributes stay as columns", [h for h, _ in mixed.columns][:3] == ["Part number", "Type", "Size"],
      str(mixed.columns))
plain = tabs_from_extraction(ProductExtractionResult(products=[ExtractedProduct(
    index=1, name="Spacers", structure="family",
    annexure=ProductAnnexure(required=True, columns=["description", "qty"], rows=[["Spacer 5", "10"], ["Spacer 8", "10"]]))]))[0]
check("with no material or finish anywhere, the family has one rate named after it", plain.rate_groups == ["Spacers"])
check("source lines taken from the variant count", fam.source_lines == 5)
ind = tabs[1]
check("single lines go to Individual items",
      [l.label for l in ind.lines] == ["Threaded Stud M56", 'Half Coupling 1" 304 SS', "Their Washers"],
      str([l.label for l in ind.lines]))
heads = [h for h, _ in ind.columns]
check("the five spec columns are always there, in order",
      heads[:6] == ["Part name", "Material", "Grade / Standard", "Finish", "Key dimensions", "Drawing no."], str(heads))
check("no Details paragraph any more", "Details" not in heads, str(heads))
check("product-specific specs become dynamic columns", heads[6:] == ["Thread", "Heat treatment", "Pressure rating"],
      str(heads))
check("one column per spec name, whatever its case", heads.count("Thread") == 1 and "thread" not in heads)
stud, coupling = ind.lines[0].fields, ind.lines[1].fields
check("a spec the customer stated is black", stud["Grade / Standard"].kind == "data"
      and stud["Grade / Standard"].value == "ASTM A193 B7")
check("a spec read off a standard is red", stud["Material"].kind == "assume" and stud["Thread"].kind == "assume")
check("a spec needed but unknown is orange and empty", stud["Finish"].kind == "input" and stud["Finish"].value is None)
check("each line fills only its own dynamic columns",
      coupling["Thread"].value == "NPT" and "Heat treatment" not in coupling and stud.get("Pressure rating") is None)
check("a derived quantity is red", ind.lines[0].qty.kind == "assume" and ind.lines[0].qty.value == 1200)
check("'As per annexure' leaves qty for the team", ind.lines[2].qty.kind == "input")
check("a by-reference family says why it is not expanded", "own workbook" in ind.lines[2].remarks)
check("the process route fills Process 1..n in order",
      [v.value for v in ind.lines[0].processes] == ["Cutting", "Machining", "Thread rolling", "Heat treatment"],
      str([v.value for v in ind.lines[0].processes]))
check("a route we read off the part is red", all(v.kind == "assume" for v in ind.lines[0].processes))
check("a route the customer stated is black, parsed from one string",
      [v.kind for v in ind.lines[1].processes] == ["data"] * 8 and ind.lines[1].processes[0].value == "Forging")
check("steps past eight go to Remarks", ind.lines[1].remarks.startswith("Also: Assembly, Polishing"), ind.lines[1].remarks)
check("a line with no route leaves the slots empty", ind.lines[2].processes == [])
check("with no weight in the extraction, weight stays orange", all(l.weight.kind == "input" for t in tabs for l in t.lines))
check("a single item's material rate comes from the default table, in red",
      ind.lines[1].material_rate.kind == "assume" and ind.lines[1].material_rate.value == 280
      and "SS 304" in ind.lines[1].remarks, str(ind.lines[1].material_rate))
check("an unknown material leaves the material rate orange", ind.lines[2].material_rate.kind == "input")

# Weight, rates and doubts — the gasket family.
gaskets = tabs_from_extraction(ProductExtractionResult(products=[ExtractedProduct(
    index=1, name="Spiral Wound Gaskets", structure="family", quantity="As per annexure",
    specs={"material": "SS316L / graphite", "processes": ["Winding", "Assembly"],
           "doubts": {"Finish": "graphite filler above 450 °C in oxidising service"}},
    provenance={"weight": "derived"},
    annexure=ProductAnnexure(required=True, columns=["description", "size", "finish", "quantity", "weight_kg", "doubt"],
                             rows=[["SPIRAL-WOUND GASKET,IR/CR: SS316L,CL150,-ASME B16.5,.75,SS-GRAPHITE", '3/4"',
                                    "Graphite", "40", "0.08", ""],
                                   ["SPIRAL-WOUND GASKET,IR/CR: SS316L,CL300,-ASME B16.5,16,SS-GRAPHITE", '16"',
                                    "Graphite", "4", "2.6 kg", "Size: 16\" CL300 needs B16.20 confirmation"]]))]))[0]
g0, g1 = gaskets.lines
check("estimated weight is red, per piece", g0.weight.kind == "assume" and g0.weight.value == 0.08
      and g1.weight.value == 2.6, str((g0.weight, g1.weight)))
check("weight and doubt are not shown as customer columns",
      [h for h, _ in gaskets.columns] == ["Description", "Size", "Finish"], str(gaskets.columns))
rate, basis = gaskets.rates["SS316L / graphite · Graphite"]
check("a family rate is material plus its route, with the sum spelled out", rate == 380 + 40 + 15
      and "SS 316 380 + Winding 40 + Assembly 15" in basis, basis)
check("a family-level doubt turns its column pink on every row, reason in Remarks",
      g0.fields["Finish"].kind == "doubt" and "Check Finish: graphite filler" in g0.remarks, g0.remarks)
check("a row's own doubt turns that cell pink", g1.fields["Size"].kind == "doubt" and "B16.20" in g1.remarks
      and g0.fields["Size"].kind == "data")

from rfq_summary.costing_workbook import build_costing_workbook
_wb = openpyxl.load_workbook(io.BytesIO(build_costing_workbook([gaskets, ind])))
_g = _wb[gaskets.title]
check("a long description wraps from the top and its row grows to fit",
      _g["A3"].alignment.wrap_text and _g["A3"].alignment.vertical == "top"
      and (_g.row_dimensions[3].height or 0) >= 30, str(_g.row_dimensions[3].height))
_legend = {_g.cell(r, 1).value: _g.cell(r, 2) for r in range(1, _g.max_row + 1) if _g.cell(r, 3).value == "INR / kg"}
check("the family's legend rate is filled, in red", _legend["SS316L / graphite · Graphite"].value == 435
      and _legend["SS316L / graphite · Graphite"].font.color.rgb.endswith("C00000"))
_i = _wb[ind.title]
_plegend = {_i.cell(r, 1).value: _i.cell(r, 2).value for r in range(1, _i.max_row + 1) if _i.cell(r, 3).value == "INR / kg"}
check("process rates come from the default table", _plegend.get("Welding") == 35 and _plegend.get("Thread rolling") == 15,
      str(_plegend))
_orig = [_wb[n] for n in _wb.sheetnames if n.startswith("zai_orig_")]
check("one hidden copy of the red and pink values per tab", len(_orig) == 2
      and all(o.sheet_state == "veryHidden" for o in _orig))
_wcol = {c.value: c.column_letter for c in _g[2]}["Weight"]
check("the copy holds the value we wrote, at the same address", _orig[0][f"{_wcol}3"].value == 0.08)
check("formulas and black data are not copied", _orig[0]["A3"].value is None)
_rules = [r for cf in _g.conditional_formatting for r in cf.rules]
check("an edited red or pink cell turns black",
      any(r.dxf and r.dxf.font and str(r.dxf.font.color.rgb).endswith("000000") and _orig[0].title in r.formula[0]
          for r in _rules), str([r.formula for r in _rules]))


check("the log says how far the extraction grouped",
      writer.grouping_summary(extraction()) == "4 line(s) for 8 item(s): 2 family (6 items), 2 single",
      writer.grouping_summary(extraction()))


# ---- the writeback step ----------------------------------------------------
def run(settings=None, dest=("b!DRIVE", "01FOLDERIDAAAAAAAAAAAAAAAAAAAA", ""), upload=None, glide=None, template=None,
        ext=None, title=""):
    calls = {"upload": [], "glide": []}

    def fake_upload(s, drive_id, folder_id, filename, data):
        calls["upload"].append({"name": filename, "data": data, "folder": folder_id})
        if upload is not None:
            return upload(filename)
        return UploadedFile(id="01COSTINGFILEIDAAAAAAAAAAAAAAA", url="https://wootz-my.sharepoint.com/x/cost.xlsx",
                            name=filename)

    def fake_glide(s, row_id, values):
        calls["glide"].append((row_id, values))
        if glide is not None:
            return glide()
        return True

    saved = (writer.glide_fetch_rfq_folder, writer.upload_file, writer.glide_set_all_rfq_columns,
             writer._costing_template)
    writer.glide_fetch_rfq_folder = lambda s, r: dest
    writer.upload_file = fake_upload
    writer.glide_set_all_rfq_columns = fake_glide
    writer._costing_template = lambda s, run_id: template
    try:
        out = TriageOutputPayload(run_id="r1", row_id="RFQ1")
        calls["result"] = writer._attach_costing_workbook(settings or _settings(), out, "RFQ1", ext or extraction(), title)
        return calls
    finally:
        (writer.glide_fetch_rfq_folder, writer.upload_file, writer.glide_set_all_rfq_columns,
         writer._costing_template) = saved


calls = run()
check("one workbook uploaded for the RFQ", len(calls["upload"]) == 1, str(calls["upload"]))
check("named 'Int costing (New) - <title>', from the extraction when no title is sent",
      calls["upload"][0]["name"] == "Int costing (New) - Project Falcon - Fasteners.xlsx", calls["upload"][0]["name"])
check("file id to ttqlU and link to Vr8gz, on the RFQ row",
      calls["glide"] == [("RFQ1", {"ttqlU": "01COSTINGFILEIDAAAAAAAAAAAAAAA",
                                   "Vr8gz": "https://wootz-my.sharepoint.com/x/cost.xlsx"})], str(calls["glide"]))
check("reports success", calls["result"] is True)
book = openpyxl.load_workbook(io.BytesIO(calls["upload"][0]["data"]))
sheet = book["(Zai) Individual items"]
row2 = [sheet.cell(2, c).value for c in range(1, 12)]
check("the uploaded sheet carries the spec columns", row2[:6] == ["Part name", "Material", "Grade / Standard", "Finish",
                                                                  "Key dimensions", "Drawing no."], str(row2))
check("the uploaded file is the generated workbook",
      [w.title for w in book.worksheets if w.sheet_state == "visible"] == ["(Zai) Summary", "(Zai) Hex Bolts", "(Zai) Individual items"], str(book.sheetnames))

c = run(title="Malabar 1 - Fasteners and Fixings Price List")
c2 = run(dest=("b!DRIVE", "01FOLDERIDAAAAAAAAAAAAAAAAAAAA", "Malabar 1 - From Glide"),
         title="Sent in the payload")
check("the title on the RFQ row (QdiyR) wins over everything",
      c2["upload"][0]["name"] == "Int costing (New) - Malabar 1 - From Glide.xlsx", c2["upload"][0]["name"])
check("the RFQ's own title wins when it is sent",
      c["upload"][0]["name"] == "Int costing (New) - Malabar 1 - Fasteners and Fixings Price List.xlsx", c["upload"][0]["name"])
check("a slash in a title cannot make a path",
      writer.costing_workbook_filename("A/B: C", "r") == "Int costing (New) - A B C.xlsx",
      writer.costing_workbook_filename("A/B: C", "r"))
check("no title at all falls back to the RFQ id", writer.costing_workbook_filename("", "RFQ1") == "Int costing (New) - RFQ1.xlsx")
check("switched off means nothing happens", run(settings=_settings(ENABLE_COSTING_WORKBOOK="false"))["upload"] == [])
check("no graph credentials means nothing happens", run(settings=_settings(MS_GRAPH_CLIENT_SECRET=""))["upload"] == [])
check("no folder on the RFQ row means nothing is uploaded", run(dest=("b!DRIVE", "", ""))["upload"] == [])
c = run(upload=lambda n: None)
check("an upload that lands nowhere writes no link", c["glide"] == [] and c["result"] is False)


def _raise(*a, **k):
    raise RuntimeError("glide down")


c = run(glide=_raise)
check("a Glide failure after upload is contained", c["result"] is False and len(c["upload"]) == 1)
c = run(upload=_raise)
check("an upload that raises is contained", c["result"] is False and c["glide"] == [])
c = run(template=b"this is not a workbook")
check("a template we cannot open falls back to the generated tabs", len(c["upload"]) == 1 and c["result"] is True)
check("an extraction with no products uploads nothing",
      run(ext=ProductExtractionResult(header=ProductExtractionHeader(), products=[]))["upload"] == [])
check("the id and url columns can be renamed from config",
      run(settings=_settings(GLIDE_COL_ALL_RFQ_COSTING_FILE_ID="abc", GLIDE_COL_ALL_RFQ_COSTING_URL="xyz"))["glide"][0][1]
      .keys() == {"abc", "xyz"})

# ---- the template is looked for, and its absence is loud -----------------------
import contextlib, os, tempfile
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    missing = writer._costing_template(_settings(COSTING_TEMPLATE_PATH=""), "r1")
check("no template anywhere returns None", missing is None)
check("…and says plainly which tabs will be missing", "Quotation / Back-end / ExIm / Volza" in buf.getvalue())
import base64 as _b64
_x = io.BytesIO()
_wb = openpyxl.Workbook()
_wb.active.title = "Quotation"
_wb.create_sheet("Back-end")
for r in range(1, 400):                    # big enough that a cut-off paste loses real content
    _wb["Back-end"].cell(r, 1, f"line {r} " * 8)
_wb.save(_x)
REAL = _x.getvalue()


def _tmp(content, mode="wb"):
    f = tempfile.NamedTemporaryFile(suffix=".xlsx", delete=False, mode=mode)
    f.write(content)
    f.close()
    return f.name


def _template_from(content, mode="wb"):
    path = _tmp(content, mode)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        got = writer._costing_template(_settings(COSTING_TEMPLATE_PATH=path), "r1")
    os.unlink(path)
    return got, buf.getvalue()


got, log = _template_from(REAL)
check("a configured .xlsx is read", got == REAL)
check("…and the log says where from and how big", "costing template:" in log and f"{len(REAL):,} bytes" in log, log)
path = _tmp(REAL)
saved_paths = writer.DEFAULT_TEMPLATE_PATHS
writer.DEFAULT_TEMPLATE_PATHS = (path,)
check("a Render secret file is found with no env var at all",
      writer._costing_template(_settings(COSTING_TEMPLATE_PATH=""), "r1") == REAL)
writer.DEFAULT_TEMPLATE_PATHS = saved_paths
os.unlink(path)

text = _b64.b64encode(REAL).decode()
wrapped = "\n".join(text[i:i + 76] for i in range(0, len(text), 76)) + "\n"   # as the .txt I send is
got, log = _template_from(wrapped, "w")
check("a base64 secret file (text only) is decoded back to the workbook", got == REAL)
check("…and the log says it was decoded", "decoded from base64" in log, log)

lines = wrapped.splitlines()
got, log = _template_from("\n".join(lines[: len(lines) // 2]) + "\n", "w")
check("a paste cut off at a line break is refused, not opened", got is None)
check("…and the log says the paste was cut off", "cut off" in log and "characters" in log, log)
got, log = _template_from(text[: len(text) // 2 + 3], "w")
check("a paste cut off mid-line is caught too", got is None and "cut off" in log, log)
got, log = _template_from(REAL[: len(REAL) // 2])
check("an .xlsx upload that is incomplete is refused and named", got is None and "incomplete" in log, log)
got, log = _template_from("definitely not a workbook", "w")
check("text that is not a workbook is refused, and said so", got is None and "neither an .xlsx" in log, log)

sent = []
saved_dl = writer.download_file
writer.download_file = lambda s, d, i, path="": (sent.append(path), REAL)[1]
try:
    got = writer._costing_template(_settings(COSTING_TEMPLATE_URL="https://wootz-my.sharepoint.com/:x:/g/abc"), "r1")
finally:
    writer.download_file = saved_dl
check("a OneDrive share link is enough to fetch the template", got == REAL)
check("…through Graph's /shares endpoint", sent and sent[0].startswith("/shares/u!") and sent[0].endswith("/driveItem/content"),
      str(sent))

# ---- the Volza Insights tab link (3qby1) ------------------------------------------
_v = io.BytesIO()
_vw = openpyxl.Workbook()
_vw.active.title = "Quotation"
for n in ("Back-end", "ExIm Insights", "Volza Insights"):
    _vw.create_sheet(n)
_vw.save(_v)
c = run(template=_v.getvalue())
cols = c["glide"][0][1] if c["glide"] else {}
check("with the template, 3qby1 gets a link that opens on Volza Insights",
      cols.get("3qby1", "").startswith("https://wootz-my.sharepoint.com/x/cost.xlsx")
      and "activeCell=%27Volza%20Insights%27!A1" in cols.get("3qby1", ""), str(cols))
check("…written in the same Glide call as the file id and link", {"ttqlU", "Vr8gz", "3qby1"} == set(cols), str(cols))
check("without a Volza tab nothing is written to 3qby1", "3qby1" not in run()["glide"][0][1])
doc = "https://wootz-my.sharepoint.com/personal/t/_layouts/15/Doc.aspx?sourcedoc=%7BABC%7D&file=a.xlsx&action=default"
check("a Doc.aspx link gets activeCell appended",
      writer.sheet_link(doc, "Volza Insights") == doc + "&activeCell=%27Volza%20Insights%27!A1")
check("a plain file link opens in Excel for the web on that tab",
      writer.sheet_link("https://x/a.xlsx", "Volza Insights") == "https://x/a.xlsx?web=1&activeCell=%27Volza%20Insights%27!A1")
check("the tab name is read from the file itself", "Volza Insights" in writer.workbook_sheet_names(_v.getvalue()))

# ---- a product-extraction timeout is named as one -------------------------------
import time as _time
from concurrent.futures import ThreadPoolExecutor
from rfq_summary import task as _task

_pool = ThreadPoolExecutor(max_workers=1)
_slow = _pool.submit(lambda: (_time.sleep(0.5), ("late", 1))[1])
_out = TriageOutputPayload(run_id="rt", row_id="RFQ1")
_out.pending_products = _task.PendingProductExtraction(run_id="rt", future=_slow, executor=_pool,
                                                         started_at=_time.perf_counter())
with contextlib.redirect_stdout(io.StringIO()):
    _task.resolve_product_extraction(_out, 0.05)
errs = _out.product_extraction.parse_errors
check("a timeout reads as a timeout in the run log, not 'empty model output'",
      errs and "timed out" in errs[0] and "empty model output" not in errs, str(errs))
check("the new defaults leave room for a large package",
      _settings().product_extraction_timeout_sec == 900 and _settings().job_timeout_sec == 1500
      and _settings().anthropic_max_tokens == 24000)

# ---- the annexure is gone ------------------------------------------------------
import importlib.util
check("no annexure generator left", importlib.util.find_spec("rfq_summary.quote_sheet") is None)
check("no annexure step in the writeback", not hasattr(writer, "_attach_family_annexures"))
check("product rows no longer carry annexure fields",
      not {"annexure_url", "annexure_file_id"} & set(ExtractedProduct.model_fields))

# ---- the lookup reads the title off the same row ------------------------------
import httpx
from rfq_summary import glide_client


class _Resp:
    status_code = 200
    def raise_for_status(self): pass
    def json(self): return [{"rows": [{"zm9TN": "b!D", "QZRyl": "01F", "QdiyR": "  Malabar 1 - Fasteners  "}]}]


class _Client:
    sent = []
    def __init__(self, *a, **k): pass
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def post(self, url, headers=None, json=None):
        _Client.sent.append(json)
        return _Resp()


real = glide_client.httpx.Client
glide_client.httpx.Client = _Client
try:
    got = glide_client.glide_fetch_rfq_folder(_settings(), "RFQ1")
finally:
    glide_client.httpx.Client = real
check("drive, folder and title come back from one query",
      tuple(got) == ("b!D", "01F", "Malabar 1 - Fasteners") and len(_Client.sent) == 1, str(got))
check("QdiyR is the default title column", _settings().glide_col_all_rfq_title == "QdiyR")

print("\nALL PASSED" if ok else "\nFAILURES ABOVE")
raise SystemExit(0 if ok else 1)
