"""
test_zip_attachments.py — reading a customer drawing package that arrives as
a zip: files inside folders, folders inside folders, a zip inside the zip.

The rules that matter: every readable file inside is parsed exactly as it
would be if it had been sent on its own; the path it sits at is carried
through so a reader knows what came from where; packaging junk is dropped;
and no archive — however hostile — can exhaust memory or take the run down.

Run:
    python test_zip_attachments.py
"""
import io
import sys
import types
import zipfile

sys.path.insert(0, "src")

# The attachment stack's optional deps are not installed here; the real
# parsers are stubbed below, so none of them is exercised.
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

from rfq_summary import attachments
from rfq_summary.config import Settings
from rfq_summary.schema import AttachmentFinding

S = Settings(GLIDE_API_KEY="k", GLIDE_APP_ID="a")

ok = True


def check(label, cond, detail=""):
    global ok
    ok &= bool(cond)
    print(("PASS  " if cond else "FAIL  ") + label + (f"  — {detail}" if detail and not cond else ""))


def make_zip(entries: dict) -> bytes:
    """entries: {path_inside_archive: bytes}"""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for path, payload in entries.items():
            z.writestr(path, payload)
    return buf.getvalue()


def stub_parsers(fn):
    """Replace the real parsers with ones that echo which parser ran."""
    def fake(kind):
        def parser(settings, url, data):
            return AttachmentFinding(
                url=url, kind=kind,
                summary=f"parsed {url} as {kind}",
                data={"extracted_text": f"<{kind} text from {url}>"},
            )
        return parser

    real = (attachments.analyze_pdf_bytes, attachments.analyze_excel_bytes,
            attachments.analyze_image_bytes)
    attachments.analyze_pdf_bytes = fake("pdf")
    attachments.analyze_excel_bytes = fake("excel")
    attachments.analyze_image_bytes = fake("image")
    try:
        return fn()
    finally:
        (attachments.analyze_pdf_bytes, attachments.analyze_excel_bytes,
         attachments.analyze_image_bytes) = real


def analyze(data, settings=S, url="https://x.test/package.zip"):
    return stub_parsers(lambda: attachments._dispatch_finding(settings, url, data, "application/zip"))


# ---- 1. A zip is recognised at all -----------------------------------------
check("a .zip url is kind=zip", attachments._guess_kind("https://x.test/a.zip", None) == "zip")
check("an application/zip content-type is kind=zip",
      attachments._guess_kind("https://x.test/nameless", "application/zip") == "zip")
check("windows' x-zip-compressed is kind=zip",
      attachments._guess_kind("https://x.test/n", "application/x-zip-compressed") == "zip")
check("a pdf is still a pdf", attachments._guess_kind("https://x.test/a.pdf", None) == "pdf")


# ---- 2. Files inside folders inside folders --------------------------------
f = analyze(make_zip({
    "drawings/part1.pdf": b"%PDF-1.4 fake",
    "drawings/revB/part2.pdf": b"%PDF-1.4 fake",
    "commercial/bom.xlsx": b"PK fake xlsx",
    "photos/sample.png": b"\x89PNG fake",
}))
text = (f.data or {}).get("extracted_text", "")
check("the archive itself reports kind=zip", f.kind == "zip", f.kind)
check("a file at the top folder is read", "drawings/part1.pdf" in text)
check("a file two folders deep is read", "drawings/revB/part2.pdf" in text, text[:200])
check("every member is parsed by the right parser",
      "<pdf text" in text and "<excel text" in text and "<image text" in text, text[:300])
check("all four members counted", (f.data or {}).get("members_read") == 4,
      str((f.data or {}).get("members_read")))
check("each member's path is carried through", text.count("=== ") == 4, text[:300])


# ---- 3. Packaging junk is dropped ------------------------------------------
f = analyze(make_zip({
    "drawings/part1.pdf": b"%PDF fake",
    "__MACOSX/._part1.pdf": b"junk",
    "drawings/.DS_Store": b"junk",
    "Thumbs.db": b"junk",
}))
check("mac and windows junk never reaches a parser", (f.data or {}).get("members_read") == 1,
      str((f.data or {}).get("members_read")))


# ---- 4. A zip inside a zip -------------------------------------------------
inner = make_zip({"revC/part9.pdf": b"%PDF fake"})
f = analyze(make_zip({"outer.pdf": b"%PDF fake", "nested/inner.zip": inner}))
text = (f.data or {}).get("extracted_text", "")
check("a nested zip is opened and its contents read", "revC/part9.pdf" in text, text[:300])

# ...but not past the depth limit.
deep = make_zip({"a.zip": make_zip({"b.zip": make_zip({"c.pdf": b"%PDF fake"})})})
f = analyze(deep, settings=Settings(GLIDE_API_KEY="k", GLIDE_APP_ID="a", ZIP_MAX_DEPTH="1"))
text = (f.data or {}).get("extracted_text", "")
check("nesting past the depth limit is not followed", "c.pdf" not in text, text[:300])
# The refusal is recorded against the archive that actually hit the limit,
# which surfaces in the outer archive's text as that member's summary.
check("and the refusal is reported rather than silent", "depth limit" in text, text[:300])


# ---- 5. Hostile archives cannot exhaust us ---------------------------------
many = Settings(GLIDE_API_KEY="k", GLIDE_APP_ID="a", ZIP_MAX_MEMBERS="3")
f = analyze(make_zip({f"f{i}.pdf": b"%PDF fake" for i in range(10)}), settings=many)
check("member count is capped", (f.data or {}).get("members_read") == 3,
      str((f.data or {}).get("members_read")))
check("and the cap is reported, not silent", "only the first 3" in str((f.data or {}).get("notes")),
      str((f.data or {}).get("notes")))

# A zip bomb: highly compressible payload far exceeding the uncompressed cap.
bomb = Settings(GLIDE_API_KEY="k", GLIDE_APP_ID="a", ZIP_MAX_TOTAL_UNCOMPRESSED_BYTES="1024")
f = analyze(make_zip({f"big{i}.pdf": b"\0" * 500_000 for i in range(4)}), settings=bomb)
check("expansion past the size cap stops the walk",
      (f.data or {}).get("members_read", 99) <= 1, str((f.data or {}).get("members_read")))
check("and says why", "expands past" in str((f.data or {}).get("notes")),
      str((f.data or {}).get("notes")))


# ---- 6. Nothing here can take the run down ---------------------------------
f = analyze(b"this is not a zip at all")
check("a corrupt archive degrades to kind=unknown", f.kind == "unknown", f.kind)
check("and says nothing inside was read", "could not be opened" in f.summary, f.summary)

f = analyze(make_zip({}))
check("an empty archive is survivable", f.kind == "zip" and (f.data or {}).get("members_read") == 0)

# One unreadable drawing must not cost us the rest of the package.
def boom(settings, url, data):
    raise RuntimeError("parser exploded")

real_pdf, real_xl = attachments.analyze_pdf_bytes, attachments.analyze_excel_bytes
attachments.analyze_pdf_bytes = boom
attachments.analyze_excel_bytes = lambda s, url, d: AttachmentFinding(
    url=url, kind="excel", summary="ok", data={"extracted_text": "<the bom survived>"})
try:
    crashed = False
    try:
        f = attachments._dispatch_finding(
            S, "https://x.test/p.zip",
            make_zip({"bad.pdf": b"%PDF", "good/bom.xlsx": b"PK"}), "application/zip")
    except Exception:
        crashed = True
    check("a parser that raises inside a zip is contained", not crashed)
    if not crashed:
        text = (f.data or {}).get("extracted_text", "")
        check("the failing member is reported", "could not be parsed" in str((f.data or {}).get("notes")),
              str((f.data or {}).get("notes")))
        check("and every other member is still read", "<the bom survived>" in text, text[:200])
finally:
    attachments.analyze_pdf_bytes, attachments.analyze_excel_bytes = real_pdf, real_xl

print("\nALL PASSED" if ok else "\nFAILURES ABOVE")
raise SystemExit(0 if ok else 1)
