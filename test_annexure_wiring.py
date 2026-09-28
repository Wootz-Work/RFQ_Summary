"""
test_annexure_wiring.py — generating and uploading a family annexure as part
of the product writeback.

The rule that matters: the link reaches the product row, and nothing in this
path can cost an extraction that otherwise succeeded.

Run:
    python test_annexure_wiring.py
"""
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

from rfq_summary import writer
from rfq_summary.config import Settings
from rfq_summary.onedrive import UploadedFile
from rfq_summary.schema import (
    ExtractedProduct, ProductAnnexure, ProductExtractionHeader,
    ProductExtractionResult, TriageOutputPayload,
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


COLS = ["part_number", "size", "finish", "quantity"]
ROWS = [[f"HB-{i:03d}", f"M{8 + i * 2}", "HDG 50 µm", str(100 + i)] for i in range(8)]


def _extraction(*products, common=""):
    return ProductExtractionResult(
        header=ProductExtractionHeader(common_conditions=common),
        products=list(products),
    )


def _family(name="Hex Bolts", rows=None, by_reference=False):
    return ExtractedProduct(
        name=name, structure="family",
        annexure=ProductAnnexure(required=True, by_reference=by_reference,
                                 columns=COLS, rows=ROWS if rows is None else rows),
    )


def _single(name="M56 Stud"):
    return ExtractedProduct(name=name, structure="single")


def run(extraction, settings=None, dest=("b!DRIVE", "01FOLDERIDAAAAAAAAAAAAAAAAAAAA"), upload=None):
    """Drive the wiring with Glide and Graph both stubbed out."""
    calls = []

    def fake_upload(s, drive_id, folder_id, filename, data):
        calls.append({"drive": drive_id, "folder": folder_id, "name": filename, "bytes": len(data)})
        if upload is not None:
            return upload(filename)
        return UploadedFile(id=f"01ID-{filename}", url=f"https://wootz-my.sharepoint.com/x/{filename}",
                            name=filename)

    real_dest, real_up = writer.glide_fetch_annexure_destination, writer.upload_annexure
    writer.glide_fetch_annexure_destination = lambda s, r: dest
    writer.upload_annexure = fake_upload
    try:
        out = TriageOutputPayload(run_id="r1", row_id="RFQ1")
        writer._attach_family_annexures(settings or _settings(), out, "RFQ1", extraction)
        return calls
    finally:
        writer.glide_fetch_annexure_destination, writer.upload_annexure = real_dest, real_up


# ---- the happy path --------------------------------------------------------
fam = _family()
calls = run(_extraction(fam, _single()))
check("one upload for the one family", len(calls) == 1, str(calls))
check("named as an annexure for that family",
      calls[0]["name"] == "Annexure 1 - Hex Bolts.xlsx", str(calls[0]["name"]))
check("sent to the drive and folder off the RFQ row",
      calls[0]["drive"] == "b!DRIVE" and calls[0]["folder"].startswith("01FOLDER"), str(calls[0]))
check("a real workbook was built", calls[0]["bytes"] > 4000, str(calls[0]["bytes"]))
check("the link lands on the product, ready for the row write",
      fam.annexure_url.endswith("Annexure 1 - Hex Bolts.xlsx"), fam.annexure_url)
check("the file id lands on it too — the handle Graph can address",
      fam.annexure_file_id == "01ID-Annexure 1 - Hex Bolts.xlsx", fam.annexure_file_id)

# Two families, two files, each link on its own product.
a, b = _family("Hex Bolts"), _family("Flat Washers")
calls = run(_extraction(a, _single(), b))
check("a second family gets its own workbook", len(calls) == 2, str(calls))
check("links do not cross products",
      "Hex Bolts" in a.annexure_url and "Flat Washers" in b.annexure_url,
      f"{a.annexure_url} | {b.annexure_url}")

# Conditions from the RFQ header reach the sheet.
fam = _family()
run(_extraction(fam, common="50 µm HDG is acceptable.\nAssembly not required."))
check("nothing raised when common conditions are present", fam.annexure_url != "")


# ---- when nothing should happen -------------------------------------------
check("an RFQ of single lines uploads nothing",
      run(_extraction(_single("M56 Stud"), _single("M56 Nut"))) == [])

s = _single()
check("a single line never gets a link or a file id",
      s.annexure_url == "" and s.annexure_file_id == "")

check("a by_reference family is left alone — the customer's own sheet travels",
      run(_extraction(_family(by_reference=True))) == [])
check("a family with no variant rows uploads nothing",
      run(_extraction(_family(rows=[]))) == [])
check("upload disabled means nothing is attempted",
      run(_extraction(_family()), settings=_settings(ENABLE_ANNEXURE_UPLOAD="false")) == [])
check("no graph credentials means nothing is attempted",
      run(_extraction(_family()), settings=_settings(MS_GRAPH_CLIENT_SECRET="")) == [])
check("no folder on the RFQ row means nothing is attempted",
      run(_extraction(_family()), dest=("b!DRIVE", "")) == [])


# ---- nothing here can cost the extraction ----------------------------------
fam = _family()
check("an upload that returns nothing is survivable",
      run(_extraction(fam), upload=lambda n: None) and fam.annexure_url == ""
      and fam.annexure_file_id == "")


def _raise(*a, **k):
    raise RuntimeError("graph down")


fam = _family()
crashed = False
try:
    run(_extraction(fam), upload=_raise)
except Exception:
    crashed = True
check("an upload that raises is contained", not crashed)
check("and the product simply has no link or id",
      fam.annexure_url == "" and fam.annexure_file_id == "", fam.annexure_url)

real_dest = writer.glide_fetch_annexure_destination
writer.glide_fetch_annexure_destination = _raise
try:
    crashed = False
    try:
        out = TriageOutputPayload(run_id="r1", row_id="RFQ1")
        writer._attach_family_annexures(_settings(), out, "RFQ1", _extraction(_family()))
    except Exception:
        crashed = True
    check("a Glide lookup that raises is contained", not crashed)
finally:
    writer.glide_fetch_annexure_destination = real_dest

print("\nALL PASSED" if ok else "\nFAILURES ABOVE")
raise SystemExit(0 if ok else 1)
